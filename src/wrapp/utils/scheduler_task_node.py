# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import asyncio
import contextvars
from abc import (
    ABC,
    abstractmethod,
)
from typing import (
    Any,
    Coroutine,
    Optional,
    Self,
    TypeVar,
)

from wrapp.utils.scheduler_task_group import (
    SchedulerTaskGroup,
    TaskGroupState,
)

_TaskReturnType = TypeVar("_TaskReturnType")


class SchedulerTreeNode:
    """
    Base class for building a hierarchical tree structure in WRAPP's
    scheduler system.

    This class provides organizational hierarchy without task execution
    capabilities. It can only create child nodes (either SchedulerTreeNode
    or SchedulerTaskGroupNode), but cannot create tasks directly.

    Key characteristics:
    - Cannot be instantiated directly (use create_root(), create_tree_node(),
      or create_task_group_node())
    - Provides hierarchical organization without context manager
      functionality
    - Does not enforce completion of child nodes before continuing
    - Useful for grouping related task groups without blocking execution
      flow

    This differs from SchedulerTaskGroupNode, which:
    - Can create and manage tasks directly
    - Provides async context manager functionality
    - Enforces completion of all tasks and child nodes before exiting its
      context

    Primarily used in SchedulerContext for non-blocking hierarchical
    organization.

    Each SchedulerTreeNode will appear as a "job" in the ProgressReport class,
    the tasks which are created with SchedulerTaskGroupNode.create_task are
    counted as tasks for the jobs in the ProgressReport class.
    """

    def __init__(self):
        raise RuntimeError("Use SchedulerTaskNode.create_root or SchedulerTaskNode.create_task_group or SchedulerTaskNode.create_tree_node")

    def _shared_init(self: Self, description: Optional[str] = None, *, expected_num_sub_jobs: Optional[int] = None):
        self._description: str = description or "<n/a>"
        self._expected_num_sub_jobs = expected_num_sub_jobs
        self._children: list[SchedulerTreeNode] = []

    def description(self) -> str:
        return self._description

    def expected_num_sub_jobs(self) -> Optional[int]:
        return self._expected_num_sub_jobs

    @classmethod
    def create_root(cls, description: Optional[str] = None) -> SchedulerRootNode:
        root = object.__new__(SchedulerRootNode)
        root._shared_init(description or "WRAPP")
        return root

    def create_task_group_node(
        self,
        description: Optional[str] = None,
        task_results: Optional[dict[asyncio.Task, Any]] = None,
        *,
        expected_num_sub_jobs: Optional[int] = None,
    ) -> SchedulerTaskGroupNode:
        child = object.__new__(SchedulerTaskGroupNode)
        child._shared_init(description, task_results, expected_num_sub_jobs=expected_num_sub_jobs)
        self._children.append(child)
        return child

    def create_tree_node(self, description: Optional[str] = None, *, expected_num_sub_jobs: Optional[int] = None) -> SchedulerTreeNode:
        child = object.__new__(SchedulerTreeNode)
        child._shared_init(description, expected_num_sub_jobs=expected_num_sub_jobs)
        self._children.append(child)
        return child

    def _associated_task_group(self) -> Optional[SchedulerTaskGroup]:
        return None

    def _all_task_groups(self) -> set[SchedulerTaskGroup]:
        all_groups: set[SchedulerTaskGroup] = set()
        for n in self._children:
            all_groups |= n._all_task_groups()
        return all_groups

    def all_tasks(self) -> set[asyncio.Task]:
        all_tasks: set[asyncio.Task] = set()
        for g in self._all_task_groups():
            all_tasks |= g.task_set()
        return all_tasks

    def all_open_tasks(self) -> set[asyncio.Task]:
        all_open_tasks: set[asyncio.Task] = set()
        for g in self._all_task_groups():
            all_open_tasks |= {t for t in g.task_set() if not t.done()}
        return all_open_tasks

    def done(self) -> bool:
        return all(tg.done() for tg in self._all_task_groups())

    async def cancel_all_tasks(self, msg: Optional[str] = None):
        await asyncio.gather(*[g.cancel(msg) for g in self._all_task_groups()])

    async def wait_for_all_tasks(self):
        unfinished_tasks = self.all_open_tasks()
        if unfinished_tasks:
            await asyncio.wait(unfinished_tasks, return_when=asyncio.ALL_COMPLETED)

    def __repr__(self) -> str:
        return f"SchedulerTreeNode(description={self._description!r}, expected_num_sub_jobs={self._expected_num_sub_jobs!r}, children={self._children!r})"


class SchedulerTaskGroupNode(SchedulerTreeNode):
    """
    A tree node that manages a group of asyncio tasks with structured
    concurrency.

    This node extends SchedulerTreeNode with task execution capabilities
    through an embedded SchedulerTaskGroup. It provides async context
    manager functionality to ensure proper task lifecycle management.

    Key features:
    - Can create and manage asyncio tasks via create_task()
    - Provides async context manager (async with) for structured
      concurrency
    - Automatically awaits all tasks when exiting the context
    - Enforces proper nesting: all child SchedulerTaskGroupNodes must be
      awaited before the parent can exit
    - Prevents task creation after the node has been awaited/completed

    This is the primary building block for WRAPP's hierarchical task
    execution, where each WRAPP command typically creates a child node in
    the scheduler's task tree to manage its concurrent operations.
    """

    def _shared_init(
        self,
        description: Optional[str] = None,
        task_results: Optional[dict[asyncio.Task, Any]] = None,
        *,
        expected_num_sub_jobs: Optional[int] = None,
    ):
        super()._shared_init(description=description, expected_num_sub_jobs=expected_num_sub_jobs)
        self._task_group = SchedulerTaskGroup.__new__(SchedulerTaskGroup)
        self._task_group._internal_init(task_results)
        self._exited = False

    def create_task_group_node(
        self,
        description: Optional[str] = None,
        task_results: Optional[dict[asyncio.Task, Any]] = None,
        *,
        expected_num_sub_jobs: Optional[int] = None,
    ) -> SchedulerTaskGroupNode:
        if self._task_group.state() in [TaskGroupState.Completed, TaskGroupState.Exception]:
            raise RuntimeError(f"Cannot create sub group {description} for task group {self.description()} which has already been awaited!")
        if not description:
            description = self._description
        return super().create_task_group_node(description, task_results, expected_num_sub_jobs=expected_num_sub_jobs)

    def create_task(
        self, coro: Coroutine[Any, Any, _TaskReturnType], *, name: Optional[str] = None, context: Optional[contextvars.Context] = None
    ) -> asyncio.Task[_TaskReturnType]:
        return self._task_group.create_task(coro, name=name, context=context)

    def _associated_task_group(self) -> Optional[SchedulerTaskGroup]:
        return self._task_group

    def _all_task_groups(self) -> set[SchedulerTaskGroup]:
        return super()._all_task_groups() | {self._task_group}

    async def __aenter__(self) -> SchedulerTaskGroupNode:
        await self._task_group.__aenter__()
        return self

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None):
        try:
            await self._task_group.__aexit__(exc_type, exc_value, traceback)
            # This is a relatively crude check for unusual usage patterns. The usual usage patterns are
            # async def do_something(task_node: SchedulerTaskNode, ...):
            #   async with task_node.create_child() as sub_sub_task_node:
            #      ...
            #
            # async with task_node.create_child() as sub_task_node:
            #   # This is the 80% use case:
            #   sub_task_node.create_task(do_something(sub_task_node, ...))
            #   # or if a chunk of work needs to be grouped in the progress report:
            #   async with sub_sub_task_node.create_child("subsubtask_subdivision") as sub_division_of_sub_task_node:
            #      ...
            #
            # In both patterns the subnodes are awaited before the parent is awaited.
            #
            # What this check prevents is somebody waiting for a parent and expecting all children to be completed even though
            # they have not explicitly been awaited. This is actually not so easy to do in code, but can happen for loops.
            # And it is a fairly natural assumption that this function should ensure that all task groups are actually
            # awaited - but given that this is not the intended usage, I did not want to overcomplicate this function
            # by waiting for all subtask nodes and doing the exception handling for that case correctly.
            if exc_type is None:
                for child in self._children:
                    if isinstance(child, SchedulerTaskGroupNode) and not child._exited:
                        raise RuntimeError(
                            f"Child task node '{child.description()}' has not been awaited yet before the parent task node '{self.description()}' was awaited! This is unexpected for the usual usage patterns of ScheduleTaskNode?!"
                        )
        finally:
            self._exited = True

    def __repr__(self) -> str:
        return f"SchedulerTaskGroupNode(description={self._description!r}, expected_num_sub_jobs={self._expected_num_sub_jobs!r}, children={self._children!r}, task_group={self._task_group!r})"


class SchedulerRootNode(SchedulerTreeNode):
    """
    The root node of the WRAPP scheduler's task tree.

    This special node serves as the top-level container for the entire task
    hierarchy. It cannot create tasks directly but can create child nodes
    that organize and execute tasks. Defaults to the description "WRAPP"
    unless a custom job_name is provided via SchedulingParameters.
    """
