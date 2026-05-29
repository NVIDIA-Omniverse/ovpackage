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

import asyncio
import concurrent.futures
import contextlib
import inspect
import logging
import threading
from functools import wraps
from typing import Optional

logger = logging.getLogger("omni.wrapp")

SINGLE_WRAPP_THREAD_NAME = "wrapp-thread"


def assert_is_not_wrapp_thread():
    assert threading.current_thread().name != SINGLE_WRAPP_THREAD_NAME


def assert_is_wrapp_thread():
    assert threading.current_thread().name == SINGLE_WRAPP_THREAD_NAME


class WrappEventLoopThread:
    _instance: Optional["WrappEventLoopThread"] = None

    @classmethod
    def initialize(cls, *args, **kwargs):
        assert_is_not_wrapp_thread()
        if not cls._instance:
            cls._instance = WrappEventLoopThread(*args, **kwargs)
        return cls._instance

    @classmethod
    def is_initialized(cls) -> bool:
        return cls._instance is not None

    @classmethod
    def me(cls):
        if cls._instance is not None:
            return cls._instance
        else:
            raise RuntimeError("WrappEventLoopThread not initialized")

    @classmethod
    def is_running(cls):
        return cls._instance and cls._instance._thread.is_alive()

    def __init__(self):
        assert_is_not_wrapp_thread()
        self._thread = threading.Thread(target=self._run_loop, name="wrapp-thread", daemon=True)
        self._loop_ready = threading.Event()
        self._loop = None
        self._thread.start()
        self._loop_ready.wait()

    def _run_loop(self):
        assert_is_wrapp_thread()
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        try:
            self._loop.run_forever()
        finally:
            # Ensure async generators/executors are finalized before closing the loop.
            # Timeouts prevent hanging if a generator or executor task doesn't cooperate.
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(asyncio.wait_for(self._loop.shutdown_asyncgens(), timeout=5))
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(asyncio.wait_for(self._loop.shutdown_default_executor(), timeout=5))
            self._loop.close()
            asyncio.set_event_loop(None)

    def run_coroutine(self, coro):
        assert_is_not_wrapp_thread()
        assert self._loop.is_running()
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    @classmethod
    def stop(cls, timeout: Optional[float]):
        assert_is_not_wrapp_thread()
        if cls._instance:
            loop = cls._instance._loop
            thread = cls._instance._thread
            if loop and loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=timeout)
            if thread.is_alive():
                raise RuntimeError(f"Wrapp event loop thread did not stop within {timeout}s")
            cls._instance._loop = None
            cls._instance = None


def async_typer(f):
    """Use this to annotate a typer command to turn it async"""

    assert inspect.iscoroutinefunction(f), f"Function {f.__name__} must be an async function to allow async_typer annotation."

    @wraps(f)
    def wrapper(*args, **kwargs):
        assert_is_not_wrapp_thread()
        WrappEventLoopThread.initialize()
        thread = WrappEventLoopThread.me()

        # Capture the asyncio.Task so we can cancel exactly this command on
        # KeyboardInterrupt, without disturbing other event-loop tasks (storage
        # backends, heartbeats, etc.).  run_coroutine_threadsafe does not expose
        # the Task it creates, so we grab it from inside the coroutine.
        command_task: Optional[asyncio.Task] = None

        async def _tracked_coro():
            nonlocal command_task
            command_task = asyncio.current_task()
            return await f(*args, **kwargs)

        future_result = thread.run_coroutine(_tracked_coro())
        try:
            return future_result.result()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt received, cancelling command task…")

            # Cancel only *our* command task — TaskGroup cascades the
            # CancelledError to all child tasks automatically.
            if command_task is not None:

                async def _cancel_and_wait():
                    command_task.cancel()
                    await asyncio.wait([command_task], timeout=10)

                try:
                    thread.run_coroutine(_cancel_and_wait()).result(timeout=15)
                except (KeyboardInterrupt, concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
                    pass
            raise

    return wrapper


def sync_calls_async_on_wrapp_thread(f):
    """This function assert the current thread is not the wrapp thread and calls the coroutine using the wrapp thread and waits for the result.

    If we wanted to call an async function from a sync function, and are already running in the wrapp thread, we are stuck.
    """

    assert inspect.iscoroutinefunction(
        f
    ), f"Function {f.__name__} must be an async function to allow sync_calls_async_on_wrapp_thread annotation."

    @wraps(f)
    def wrapper(*args, **kwargs):
        assert_is_not_wrapp_thread()
        WrappEventLoopThread.initialize()
        thread = WrappEventLoopThread.me()
        future_result = thread.run_coroutine(f(*args, **kwargs))
        result = future_result.result()
        return result

    return wrapper


def assert_not_wrapp_thread(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        assert_is_not_wrapp_thread()
        return f(*args, **kwargs)

    return wrapper


def force_wrapp_thread(f):
    assert inspect.iscoroutinefunction(f), f"Function {f.__name__} must be an async function to allow force_wrapp_thread annotation."

    @wraps(f)
    async def wrapper(*args, **kwargs):
        if threading.current_thread().name == SINGLE_WRAPP_THREAD_NAME:
            return await f(*args, **kwargs)
        else:
            WrappEventLoopThread.initialize()
            thread = WrappEventLoopThread.me()
            future_result = thread.run_coroutine(f(*args, **kwargs))
            result = await asyncio.wrap_future(future_result)
            return result

    return wrapper


def async_for_in_wrapp_thread(f):
    assert inspect.isasyncgenfunction(
        f
    ), f"Function {f.__name__} must be an async generator function (i.e., 'async def' with 'yield') to allow async_for_in_wrapp_thread annotation."

    @wraps(f)
    async def async_for_wrapper(*args, **kwargs):
        if threading.current_thread().name == SINGLE_WRAPP_THREAD_NAME:
            async for i in f(*args, **kwargs):
                yield i
        else:
            # Consumer runs in the current thread's event loop (caller_loop)
            try:
                caller_loop = asyncio.get_running_loop()
            except RuntimeError:
                # This means the main thread isn't running an event loop to begin with.
                # The caller needs to ensure this async generator is run in an event loop.
                raise RuntimeError("async_for_in_wrapp_thread_fixed must be consumed within an active asyncio event loop.")

            local_q = asyncio.Queue()  # Queue belongs to caller_loop

            WrappEventLoopThread.initialize()
            thread_handler = WrappEventLoopThread.me()

            # Producer coroutine (will run in wrapp-thread's loop)
            async def producer_coro():
                try:
                    async for item in f(*args, **kwargs):
                        # Safely put item into the consumer's (caller's) queue
                        caller_loop.call_soon_threadsafe(local_q.put_nowait, item)
                except Exception as e:
                    # Propagate exception to the consumer
                    caller_loop.call_soon_threadsafe(local_q.put_nowait, e)
                finally:
                    # Signal end of production
                    caller_loop.call_soon_threadsafe(local_q.put_nowait, None)  # Sentinel

            # Start the producer in the wrapp-thread. Store future for error checking.
            producer_future = thread_handler.run_coroutine(producer_coro())

            # Consumer part (runs in caller_loop)
            try:
                while True:
                    item = await local_q.get()
                    if isinstance(item, Exception):
                        # An exception was put by the producer
                        raise item  # Re-raise in consumer's context
                    if item is None:  # End of items sentinel
                        break
                    yield item
            finally:
                # Cleanup: Ensure producer future is checked for unhandled exceptions
                # that might not have resulted in an item/None being put on the queue.
                if producer_future and not producer_future.done():
                    # If producer is still running (e.g. consumer broke early), cancel it.
                    producer_future.cancel()
                elif producer_future and producer_future.done():
                    # If producer is done, check its result for an exception
                    try:
                        producer_future.result(timeout=0)  # Raise if exception occurred
                    except asyncio.CancelledError:
                        pass  # Expected if cancelled
                    except Exception as e:
                        # This means producer crashed without putting an exception or None on queue
                        # This error might be missed by the consumer if it already exited.
                        # Consider how to best propagate this. For now, print.
                        logger.error(f"Unhandled error in producer_coro: {e}")
                        # If consumer loop exited normally (got None), this is an additional error.
                        # If consumer loop exited due to its own error, this might be related or independent.

    return async_for_wrapper


def sync_force_wrapp_thread(f):
    assert not inspect.iscoroutinefunction(
        f
    ), f"Function {f.__name__} must be a synchronous (non-async) function to allow sync_force_wrapp_thread annotation."

    @wraps(f)
    def wrapper(*args, **kwargs):
        if threading.current_thread().name == SINGLE_WRAPP_THREAD_NAME:
            return f(*args, **kwargs)
        else:
            WrappEventLoopThread.initialize()
            thread = WrappEventLoopThread.me()

            async def make_coro():
                return f(*args, **kwargs)

            if not thread.is_running():
                raise Exception("Wrapp thread died")
            future_result = thread.run_coroutine(make_coro())
            result = future_result.result()
            return result

    return wrapper
