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
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Optional,
    TypeAlias,
    TypeVar,
    Union,
)

import pytest

from wrapp import (
    ProgressReportVerbosity,
    SchedulerContext,
    SchedulingParameters,
    WrappScheduler,
)
from wrapp.utils.progress_report import (
    ProgressReport,
)
from wrapp.utils.scheduler_task_node import (
    SchedulerTaskGroupNode,
    SchedulerTreeNode,
)
from wrapp.utils.utils import random_string

_TaskReturnType = TypeVar("_TaskReturnType")

DelayPerTask: TypeAlias = Union[float, list["DelayPerTask"]]


@dataclass
class TaskState:
    _task: asyncio.Task
    task_started: bool
    task_finished: bool = False
    task_cancelled: bool = False
    task_done: bool = False
    task_done_order: Optional[int] = None
    task_exception: Optional[BaseException] = None
    task_name: str = ""

    def finalize(self, allow_unfinished_tasks: bool = False):
        # At this point we require that the tasks are all done - either cancelled or completed
        if self._task is None:
            # Already processed
            return
        self.task_done = self._task.done()
        self.task_cancelled = self._task.cancelled()
        if not self.task_cancelled:
            self.task_exception = self._task.exception()
        self.task_name = self._task.get_name()
        assert allow_unfinished_tasks or self.task_done
        # Avoid keeping task objects artifically alive
        self._task = None

    def assert_finished(self):
        assert self.task_finished
        assert not self.task_cancelled
        assert self.task_exception is None

    def assert_raised_exception(self, raised_exception: BaseException):
        assert not self.task_finished
        assert not self.task_cancelled
        assert self.task_exception == raised_exception

    def assert_cancelled(self):
        assert not self.task_finished
        assert self.task_cancelled
        assert self.task_exception is None


class SchedulingTestHelpers:
    def __init__(self, allow_unfinished_tasks_on_exit: bool = False, progress_report_interval: float = -1):
        self.num_tasks = 0
        self.num_tasks_done = 0
        self.task_state: Dict[str, TaskState] = {}
        self._wrapp_scheduler: Optional[WrappScheduler] = None
        self._allow_unfinished_tasks_on_exit = allow_unfinished_tasks_on_exit
        self._progress_reports: list[ProgressReport] = []
        self._progress_report_interval = progress_report_interval

    def verify_finish_order(self, task_names: list[str]):
        for n in task_names:
            assert n in self.task_state, f"Cannot verify order for task '{n}' - no task with this name has been captured."
            assert self.task_state[n].task_done_order is not None, f"Cannot verify order for task '{n}' - task is not done?"
        for i in range(len(task_names)):
            task = self.task_state.get(task_names[i], None)
            prev_task = self.task_state.get(task_names[i - 1], None) if i > 0 else None
            next_task = self.task_state.get(task_names[i + 1], None) if i < len(task_names) - 1 else None
            assert (
                prev_task is None or task.task_done_order > prev_task.task_done_order
            ), f"Task '{task.task_name}' - which was the {task.task_done_order} task to be done - was expected to finish after task '{prev_task.task_name}' which was the {prev_task.task_done_order} task to be done."
            assert (
                next_task is None or next_task.task_done_order > task.task_done_order
            ), f"Task '{task.task_name}' - which was the {task.task_done_order} task to be done - was expected to finish before task '{next_task.task_name}' which was the {next_task.task_done_order} task to be done."

    async def tracked_task(self, coro: Awaitable[_TaskReturnType]) -> _TaskReturnType:
        self.num_tasks += 1
        n = asyncio.current_task().get_name()
        if n in self.task_state:
            n += f"#{random_string()}"
        self.task_state[n] = TaskState(_task=asyncio.current_task(), task_started=True)
        try:
            r = await coro
            self.task_state[asyncio.current_task().get_name()].task_finished = True
        finally:
            self.num_tasks_done += 1
            self.task_state[asyncio.current_task().get_name()].task_done_order = self.num_tasks_done
        return r

    async def task_func(self, sleep_time: float, *, exception: Optional[BaseException] = None, return_value: Any = None) -> Any:
        async def payload():
            await asyncio.sleep(sleep_time)
            if exception:
                raise exception

        await self.tracked_task(payload())
        return return_value

    def finalize_tasks(self):
        for ts in self.task_state.values():
            ts.finalize(self._allow_unfinished_tasks_on_exit)

    def clear(self):
        self.task_state.clear()
        self._progress_reports.clear()

    def progress_reports(self) -> list[ProgressReport]:
        return self._progress_reports

    def store_progress_report(self, progress_report: ProgressReport):
        self._progress_reports.append(progress_report)

    async def __aenter__(self) -> SchedulerContext:
        assert self._wrapp_scheduler is None
        scheduling_params = SchedulingParameters()
        if self._progress_report_interval >= 0:
            scheduling_params.progress_report = ProgressReportVerbosity.verbose
            scheduling_params.progress_report_callback = self.store_progress_report
            scheduling_params.progress_report_interval = self._progress_report_interval
        self._wrapp_scheduler = WrappScheduler(context=SchedulerContext(params=scheduling_params))
        return await self._wrapp_scheduler.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        assert self._wrapp_scheduler is not None
        try:
            await self._wrapp_scheduler.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            self.finalize_tasks()
            assert self._allow_unfinished_tasks_on_exit or self._wrapp_scheduler.context.root_task_node().done()
        self._wrapp_scheduler = None

    def run_with_task_tracking(self, coro: Callable[[SchedulerContext], Awaitable[None]]):
        async def coro_wrapper():
            # If we do not add this exception handler, the tests will still succeed, but the
            # ones testing with KeyboardInterrupt will at the end log "Task exception never retrieved"
            # for the task created by asyncio.run itself which is confusing.
            # This is caused by the default exception handler for the main event loop
            # logging the KeyboardInterrupt as unhandled, even though it will be passed on to
            # asyncio.run. See for example https://github.com/python/cpython/issues/108549
            # This does not happen with normal exceptions.
            # See also the test "test_task_exception_was_never_retrieved" for a simple example.
            # WRAPP itself interestingly does not log that message on Ctrl+C. Not sure why, it
            # might be because most of WRAPP is running in a separate thread?
            main_task_created_by_asyncio = asyncio.current_task()
            assert main_task_created_by_asyncio is not None

            def _squelch_main_kbdint(loop, ctx):
                # Apparently in Python 3.11, "task" is not reliably set, and we need to use "future".
                # The "if" tries to make sure that we are only suppressing the exception we really
                # want to suppress.
                t = ctx.get("task") or ctx.get("future")
                if (
                    ctx.get("message") == "Task exception was never retrieved"
                    and isinstance(ctx.get("exception"), KeyboardInterrupt)
                    and t is main_task_created_by_asyncio
                ):
                    return
                loop.default_exception_handler(ctx)

            asyncio.get_running_loop().set_exception_handler(_squelch_main_kbdint)

            async with self as ctxt:
                await coro(ctxt)

        asyncio.run(coro_wrapper())

    async def generate_large_task_set(
        self,
        parent_task_group: SchedulerTaskGroupNode,
        delay_per_task: Optional[list[tuple[int, int]] | DelayPerTask] = None,
        deep_nesting_depth: int = 100,
        deep_nesting_tasks: int = 1,
    ) -> int:
        # Generate many tasks in a tree - first number is the number of task groups generated
        # on the level, second number the number of tasks, so in total there are
        # 8192+4*(256+8*(0+4*(8+2*(2+4*16))=8192+(1024+32*(0+32+8*(2+64)))=27136 tasks
        if delay_per_task is None:
            delay_per_task = [(8192, 4), (256, 8), (0, 4), (8, 2), (2, 4), (16, 0)]
            # delay_per_task = [(2, 0)]  # more suitable for debugging
        if delay_per_task and isinstance(delay_per_task, list) and isinstance(delay_per_task[0], tuple):

            def construct_delay_per_tasks(tasks_per_level: list[tuple[int, int]], depth: int = 0) -> DelayPerTask:
                num_tasks, num_groups = tasks_per_level[depth]
                r: DelayPerTask = []
                for i in range(num_tasks):
                    r.append(3600.0)
                for i in range(num_groups):
                    r.append(construct_delay_per_tasks(tasks_per_level, depth + 1))
                return r

            delay_per_task = construct_delay_per_tasks(delay_per_task)
        # deep_nesting_depth = 3  # more suitable for debugging

        def count_tasks(local_delay_per_task: DelayPerTask) -> int:
            if isinstance(local_delay_per_task, float):
                return 1
            return sum([count_tasks(d) for d in local_delay_per_task]) + 1

        def total_num_tasks() -> int:
            return 1 + (deep_nesting_depth + deep_nesting_tasks) + count_tasks(delay_per_task)

        async def generate_task_tree(task_node: SchedulerTaskGroupNode, local_delay_per_task: DelayPerTask, depth: int = 0):
            async with task_node.create_task_group_node(f"task-tree-{depth}-{random_string()}") as sub_task_node:
                for task_index in range(len(local_delay_per_task)):
                    t: DelayPerTask = local_delay_per_task[task_index]
                    if isinstance(t, float):
                        sub_task_node.create_task(
                            self.task_func(t), name=f"{sub_task_node.description()}-task-{task_index}-{random_string()}"
                        )
                    else:
                        sub_task_node.create_task(
                            self.tracked_task(generate_task_tree(sub_task_node, t, depth + 1)),
                            name=f"{sub_task_node.description()}-subjob-{task_index}-{random_string()}",
                        )

        async def generate_deep_nesting(task_node: SchedulerTaskGroupNode, depth: int = 0):
            async with task_node.create_task_group_node(f"deep-nesting-rec-{depth}-{random_string()}") as sub_task_node:
                if depth < deep_nesting_depth:
                    sub_task_node.create_task(
                        self.tracked_task(generate_deep_nesting(sub_task_node, depth + 1)),
                        name=f"{sub_task_node.description()}-task-{random_string()}",
                    )
                else:
                    for i in range(deep_nesting_tasks):
                        sub_task_node.create_task(
                            self.task_func(3600.0), name=f"{sub_task_node.description()}-subjob-{i}-{random_string()}"
                        )

        asyncio.get_event_loop().set_debug(False)
        num_tasks = len(self.task_state)
        parent_task_group.create_task(self.tracked_task(generate_deep_nesting(parent_task_group)), name="root-deep-nesting")
        parent_task_group.create_task(self.tracked_task(generate_task_tree(parent_task_group, delay_per_task)), name="root-task-tree")
        while len(self.task_state) - num_tasks < total_num_tasks():
            await asyncio.sleep(0.1)

        return total_num_tasks()


@pytest.fixture()
def scheduling_test_helper(wrapp_init):
    return SchedulingTestHelpers()


@pytest.fixture()
def scheduling_test_helper_with_unfinished_tasks(wrapp_init):
    return SchedulingTestHelpers(allow_unfinished_tasks_on_exit=True)


@pytest.fixture()
def scheduling_test_helper_with_progress_report(wrapp_init):
    # Generate progress reports with every async cycle.
    # Note that the progress reporter will only call the callback if a task has been started or completed
    return SchedulingTestHelpers(progress_report_interval=0)
