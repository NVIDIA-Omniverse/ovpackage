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
import logging
from builtins import BaseExceptionGroup
from enum import (
    Enum,
)
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Optional,
    ParamSpec,
    Self,
    TypeVar,
)

_CallableParams = ParamSpec("_CallableParams")
_CallableReturnType = TypeVar("_CallableReturnType")

ScheduledTaskCoroutine = Callable[_CallableParams, Awaitable[_CallableReturnType]]
_TaskReturnType = TypeVar("_TaskReturnType")


logger = logging.getLogger("omni.wrapp")


class TaskGroupState(Enum):
    Initialized = "Initialized"
    Entered = "Entered"
    Waiting = "Waiting"
    Completed = "Completed"
    Exception = "Exception"


class SchedulerTaskGroup:
    """
    A group of tasks which can be awaited together, while other tasks in other task groups are still being executed.
    Can be used as an asynchronous context manager or as a normal class. All tasks in the group are awaited when the context manager exits.

    Functions like asyncio.TaskGroup, but makes the set of tasks in the group accessible, and stores the results of the tasks.

    Only used by SchedulerTreeNode - if it would be used elsewhere, the tasks would not be captured for the progress report.
    """

    # noinspection PyUnreachableCode
    def __init__(self: Self):
        # SchedulerTaskGroup should only be created vie SchedulerTaskNode - otherwise its tasks are not tracked as part
        # of the overarching executed WRAPP command.
        raise RuntimeError("Only create SchedulerTaskGroup via SchedulerTaskNode")

    def _internal_init(self: Self, task_results: Optional[dict[asyncio.Task, Any]] = None):
        self._task_set: set[asyncio.Task] = set()
        self._state = TaskGroupState.Initialized
        self._task_results: Optional[dict[asyncio.Task, Any]] = task_results
        self._asyncio_task_group = asyncio.TaskGroup()

    def state(self) -> TaskGroupState:
        return self._state

    def task_set(self) -> set[asyncio.Task]:
        return self._task_set

    def done(self):
        return self._state in [TaskGroupState.Completed, TaskGroupState.Exception]

    async def cancel(self, msg: Optional[str] = None):
        """Cancel does only trigger the cancellation but does not wait until the tasks have been all cancelled"""
        if self._state not in [TaskGroupState.Entered, TaskGroupState.Waiting]:
            # Task Group has either not started, or already finished
            if self._state == TaskGroupState.Initialized:
                self._state = TaskGroupState.Exception
            return
        # Cancel all tasks: if we are already Waiting, the __aexit__ of the _asyncio_task_group
        # will raise an exception, causing the task group to exit.
        # If the state is "Entered", the tasks are currently all not awaited, and just cancelled,
        # and once self.__aexit__ is called, the __aexit__ of the _asyncio_task_group will raise
        # an exception.
        for t in self._task_set:
            if not t.done() and not t.cancelling():
                t.cancel(msg)

    def create_task(
        self, coroutine: Coroutine[Any, Any, _TaskReturnType], *, name: Optional[str] = None, context: Optional[contextvars.Context] = None
    ) -> asyncio.Task[_TaskReturnType]:
        if self._state not in [TaskGroupState.Entered, TaskGroupState.Waiting]:
            coroutine.close()
            raise RuntimeError(f"SchedulerTaskGroup has not yet been entered, or has already been completed (state={self._state})!")

        # Schedule the coroutine directly on the asyncio TaskGroup; its result, if any, is collected by
        # the done callback below. If scheduling is rejected, close() the coroutine so a coroutine we
        # accepted but never ran is not later reported as "coroutine ... was never awaited".
        #
        # The coroutine is passed as-is and is never wrapped in a result-storing coroutine. The TaskGroup
        # cancels not-yet-started tasks when a sibling fails; asyncio discards a directly-scheduled
        # coroutine cleanly in that case, whereas a wrapper coroutine would be cancelled at its own entry
        # and leak the wrapped coroutine un-awaited.
        try:
            t: asyncio.Task = self._asyncio_task_group.create_task(coroutine, name=name, context=context)
        except BaseException:
            coroutine.close()
            raise

        if self._task_results is not None:
            task_results = self._task_results

            def _store_result(task: asyncio.Task[_TaskReturnType]) -> None:
                # Record the result of a task that completed successfully. A cancelled or failed task has
                # no result to record - the task group propagates its exception - so it is skipped; the
                # cancelled() check also guards exception()/result() from re-raising.
                if not task.cancelled() and task.exception() is None:
                    task_results[task] = task.result()

            t.add_done_callback(_store_result)

        self._task_set.add(t)
        return t

    async def wait_for_tasks(self):
        await self.__aexit__(None, None, None)

    async def __aenter__(self) -> SchedulerTaskGroup:
        if self._state != TaskGroupState.Initialized:
            raise RuntimeError(f"ScheduledTaskGroup has already been entered before (state={self._state})!")
        await self._asyncio_task_group.__aenter__()
        self._state = TaskGroupState.Entered
        return self

    async def __aexit__(self, exc_type=None, exc_value=None, traceback=None):
        if self._state != TaskGroupState.Entered:
            self._state = TaskGroupState.Exception
            raise RuntimeError(
                f"SchedulerTaskGroup has not been entered yet, is already awaited or has already been completed (state={self._state})!"
            ) from exc_value
        self._state = TaskGroupState.Waiting
        if self._task_set:
            try:
                await self._asyncio_task_group.__aexit__(exc_type, exc_value, traceback)
            except BaseException as e:
                self._state = TaskGroupState.Exception
                # For now, to stay compatible with the previous scheduler, extract one of the exception in exception group and raise it.
                if isinstance(e, BaseExceptionGroup):
                    # Log all exceptions before raising only the first one for backwards compatibility:
                    # if the calling code is not built to handle ExceptionGroup (i.e. does not use except*),
                    # an exception handler for e.g. a StorageOperationError will not capture an ExceptionGroup
                    # containing a StorageOperationError.
                    # Keep the exception group as a chained exception.
                    for exc in e.exceptions[1:]:
                        logger.error(f"Additional exception in task group {self}: {exc}")
                    raise e.exceptions[0] from e
                raise
        self._state = TaskGroupState.Completed
