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
import inspect
import os
from concurrent.futures.thread import ThreadPoolExecutor
from enum import (
    Enum,
    unique,
)
from types import NotImplementedType
from typing import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Iterable,
    Optional,
    ParamSpec,
    Self,
    TypeVar,
)

_CallableParams = ParamSpec("_CallableParams")
_CallableReturnType = TypeVar("_CallableReturnType")
_TaskReturnType = TypeVar("_TaskReturnType")


class TaskThrottler:
    """
    Class to ensure that a limited resource is only used by a maximum number of concurrent tasks simultaneously.
    """

    def __init__(self, throttler_type: ThrottlerType, server_backend_name: str, max_parallel_tasks: int):
        self.throttler_type = throttler_type
        self.server_backend_name = server_backend_name
        self.max_parallel_tasks = max_parallel_tasks
        self.semaphore = asyncio.Semaphore(self.max_parallel_tasks)
        self.pending_tasks: set[asyncio.Task] = set()
        self.processing_tasks: set[asyncio.Task] = set()

    def description(self):
        return f"{self.throttler_type.name} ({self.server_backend_name})"

    async def cancel_all_tasks(self: Self):
        while self.pending_tasks:
            pending_task: asyncio.Task = next(iter(self.pending_tasks))
            pending_task.cancel()
        while self.processing_tasks:
            procesing_task: asyncio.Task = next(iter(self.processing_tasks))
            procesing_task.cancel()

    async def run(self, coroutine: Awaitable[_TaskReturnType]) -> _TaskReturnType:
        t = asyncio.current_task()
        coroutine_started = False
        if t is None:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError(
                f"TaskThrottler.run called outside of an active asyncio.Task for '{self.throttler_type}' '{self.server_backend_name}'."
            )
        if t in self.pending_tasks:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError(f"{id(t)} {t} already in pending_tasks in {id(self)} {self}")
        # If the following assert fires, a throttled coroutine has triggered another throttled coroutine.
        # Do not throttle higher calls to functions and use inside the function the same throttler again.
        if t in self.processing_tasks:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError(f"{id(t)} {t} already in processing_tasks in {id(self)} {self}")
        self.pending_tasks.add(t)
        try:
            async with self.semaphore:
                self.pending_tasks.remove(t)
                self.processing_tasks.add(t)
                try:
                    coroutine_started = True
                    return await coroutine
                finally:
                    self.processing_tasks.remove(t)
        finally:
            if not coroutine_started and inspect.iscoroutine(coroutine):
                coroutine.close()
            self.pending_tasks.discard(t)

    async def iterate(self, async_gen: AsyncGenerator[_CallableReturnType, None]) -> AsyncGenerator[_CallableReturnType, None]:
        # Create the generator - we do not use the semaphore, as the function is not async
        gen = async_gen.__aiter__()

        # Wrap each yield with semaphore acquisition
        try:
            while True:
                # Acquire semaphore for each server operation
                item: _CallableReturnType = await self.run(gen.__anext__())
                # Yield it to caller
                yield item
        except StopAsyncIteration:
            # Generator exhausted
            pass

    def __eq__(self, other) -> bool | NotImplementedType:
        if not isinstance(other, TaskThrottler):
            return NotImplemented
        return (self.throttler_type, self.server_backend_name) == (other.throttler_type, other.server_backend_name)

    def __hash__(self):
        return hash((self.throttler_type, self.server_backend_name))


@unique
class ThrottlerType(Enum):
    ServerRoundTrip = "Server Round Trip"
    NetworkDataTransfer = "Network Data Transfer"
    LongRunningServerOp = "Long Running Server Op"
    CPUThread = "CPU Thread"


class CPUThrottler:
    """
    Helper functions to run tasks multi-threaded.
    """

    _instance: Optional["CPUThrottler"] = None

    @classmethod
    def me(cls):
        if cls._instance is None:
            cls._instance = CPUThrottler()
        return cls._instance

    def __init__(self, *, max_num_jobs: int = 0):
        # Use many CPUs, but not all on machines with many CPUs
        self._max_concurrent_local_cpu_tasks: int = max(min(os.cpu_count() or 1, 64) - 2, 1)
        if max_num_jobs <= 0:
            # to avoid an empty queue, queue more than twice the number of tasks we can concurrently process
            self._max_num_jobs = self._max_concurrent_local_cpu_tasks * 2
        else:
            self._max_num_jobs = max_num_jobs
            self._max_concurrent_local_cpu_tasks = min(self._max_num_jobs >> 1, self._max_concurrent_local_cpu_tasks)
        self._cpu_throttler = TaskThrottler(ThrottlerType.CPUThread, "CPU", self._max_num_jobs)
        self._thread_pool = ThreadPoolExecutor(max_workers=self._max_concurrent_local_cpu_tasks)

    def throttler(self) -> TaskThrottler:
        return self._cpu_throttler

    async def close(self) -> None:
        await self._cpu_throttler.cancel_all_tasks()
        self._thread_pool.shutdown(True, cancel_futures=True)

    async def _process_local_task_multi_threaded(
        self,
        collect_result: Optional[Callable[[_CallableReturnType], None]],
        callable: Callable[_CallableParams, _CallableReturnType],
        *args: _CallableParams.args,
        **kwargs: _CallableParams.kwargs,
    ) -> None:
        concurrent_future = self._thread_pool.submit(callable, *args, **kwargs)
        r: _CallableReturnType = await asyncio.wrap_future(concurrent_future)
        if collect_result:
            collect_result(r)

    # Add a task to the work queues but split it into a part which can be executed multithreaded, and one for collecting the results.
    # Note that there is no guarantee that the tasks are executed in the order they are added.
    # This method first calls coroutine with the given args, kwargs multithreaded, and then calls collector single-threaded with the result from the coroutine.
    async def process_local_task_multi_threaded(
        self,
        collect_result: Optional[Callable[[_CallableReturnType], None]],
        callable: Callable[_CallableParams, _CallableReturnType],
        *args: _CallableParams.args,
        **kwargs: _CallableParams.kwargs,
    ) -> None:
        await self._cpu_throttler.run(self._process_local_task_multi_threaded(collect_result, callable, *args, **kwargs))
