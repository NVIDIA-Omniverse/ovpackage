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
import gc
import os
import signal
import sys
import threading
import time
import warnings
from contextlib import contextmanager
from typing import Optional

import pytest

from tests.scheduling_test_helper import scheduling_test_helper as sth
from tests.scheduling_test_helper import (
    scheduling_test_helper_with_unfinished_tasks as sth_with_unfinished_tasks,
)
from wrapp.commands.catalog import catalog
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    SchedulerContext,
    SchedulingParameters,
    ThrottlingParameters,
    WrappScheduler,
)
from wrapp.utils.coro import WrappEventLoopThread
from wrapp.utils.scheduler_task_group import (
    SchedulerTaskGroup,
    TaskGroupState,
)
from wrapp.utils.scheduler_task_node import (
    SchedulerTaskGroupNode,
    SchedulerTreeNode,
)
from wrapp.utils.utils import random_string


@contextmanager
def _assert_no_unawaited_coroutine_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        yield
        gc.collect()
    unawaited = [warning for warning in caught if "was never awaited" in str(warning.message)]
    assert unawaited == []


@pytest.mark.asyncio
async def test_scheduling_plain(sth):
    # This is more to test our test framework
    async with sth as s:
        pass

    async with sth as s:
        # Simple context handler
        async with s.root_task_node().create_task_group_node() as tn_1:
            tn_1.create_task(sth.task_func(0.1), name="1")

        assert len(sth.task_state) == 1
        sth.task_state["1"].assert_finished()
        sth.clear()

        # Running a non-nested set of functions with result
        task_results: dict[asyncio.Task, int] = {}
        async with s.root_task_node().create_task_group_node("Do stuff", task_results) as tn_2:
            t = tn_2.create_task(sth.task_func(0.1, return_value=42))
        assert len(task_results) == 1
        assert task_results[t] == 42

        # Running a non-nested set of functions with result with context manager (e.g. for loops)
        task_results: dict[asyncio.Task, int] = {}
        tn_3 = s.root_task_node().create_task_group_node("Do more stuff", task_results)
        await tn_3.__aenter__()
        t = tn_3.create_task(sth.task_func(0.1, return_value=42))
        await tn_3.wait_for_all_tasks()
        assert len(task_results) == 1
        await tn_3.__aexit__()
        assert task_results[t] == 42


@pytest.mark.asyncio
async def test_nested_scheduling(sth):
    # Test the pattern of task node child of task node A being created in a task which was also created in a task node A.
    async with sth as s:

        async def subjob(task_node: SchedulerTaskGroupNode, n: str, t: float):
            async with task_node.create_task_group_node(n) as tn_11:
                tn_11.create_task(sth.task_func(t), name=f"{n}_task_1")

        async with s.root_task_node().create_task_group_node("job_main") as tn_1:
            tn_1.create_task(sth.tracked_task(subjob(tn_1, "subjob_0.1", 0.1)), name="job_main_subjob_0.1")
            tn_1.create_task(sth.tracked_task(subjob(tn_1, "subjob_0.3", 0.3)), name="job_main_subjob_0.3")
            tn_1.create_task(sth.task_func(0.2), name="job_main_task_0.2")
            tn_1.create_task(sth.task_func(0.0), name="job_main_task_0.0")
        # This can be called successfully after the task node has completed
        await tn_1.wait_for_all_tasks()

    # All tasks are executed concurrently
    sth.verify_finish_order(
        [
            "job_main_task_0.0",
            "subjob_0.1_task_1",
            "job_main_subjob_0.1",
            "job_main_task_0.2",
            "subjob_0.3_task_1",
            "job_main_subjob_0.3",
        ]
    )


@pytest.mark.asyncio
async def test_scheduling_node_direct_nesting(sth):
    # Test the pattern of task node child being created without an intermediate task to group together tasks sequentially for
    # better progress reporting.
    async with sth as s:
        async with s.root_task_node().create_task_group_node("job_main") as tn_1:
            async with tn_1.create_task_group_node("subjob_1") as tn_11:
                tn_11.create_task(sth.task_func(0.4), name="subjob_1_task_0.4")
                tn_11.create_task(sth.task_func(0.3), name="subjob_1_task_0.3")
            async with tn_1.create_task_group_node("subjob_2") as tn_12:
                tn_12.create_task(sth.task_func(0.2), name="subjob_2_task_0.2")
                tn_12.create_task(sth.task_func(0.1), name="subjob_2_task_0.1")
    # All tasks are executed in the expected order
    sth.verify_finish_order(
        [
            "subjob_1_task_0.3",
            "subjob_1_task_0.4",
            "subjob_2_task_0.1",
            "subjob_2_task_0.2",
        ]
    )


@pytest.mark.asyncio
async def test_scheduling_rejected_tasks_close_coroutines(sth):
    async with sth as s:
        tn_initialized = s.root_task_node().create_task_group_node()
        with _assert_no_unawaited_coroutine_warning():
            with pytest.raises(RuntimeError, match=r"state=.*Initialized"):
                # Passing a freshly-created coroutine to create_task() in the Initialized
                # state must reject the task and close the coroutine, otherwise Python later
                # reports "coroutine was never awaited" when it is garbage-collected.
                tn_initialized.create_task(sth.task_func(0.1))

    async with sth as s:
        async with s.root_task_node().create_task_group_node() as tn_completed:
            tn_completed.create_task(sth.task_func(0.1))
        with _assert_no_unawaited_coroutine_warning():
            with pytest.raises(RuntimeError, match=r"state=.*Completed"):
                # The same cleanup is required after the task group has completed: even though
                # create_task() raises, ownership of the coroutine has already been handed to
                # the scheduler API, so the rejected coroutine must be closed here.
                tn_completed.create_task(sth.task_func(0.1))


@pytest.mark.asyncio
async def test_scheduling_task_cancelled_before_start_closes_coroutine(sth):
    # create_task() accepts the task, but the task is cancelled before the event loop ever steps it -
    # which is exactly what asyncio.TaskGroup does to the sibling tasks it has not started yet when
    # another task in the group fails. The scheduled coroutine must not be left un-awaited in that case,
    # otherwise it is garbage-collected with a spurious "coroutine was never awaited" RuntimeWarning,
    # which then flakily fails unrelated tests that assert no warnings are emitted.
    async def scenario():
        async with sth as s:
            async with s.root_task_node().create_task_group_node() as tn:
                # Cancel the task synchronously, before yielding to the event loop, so it has not been
                # stepped yet when it is cancelled.
                assert tn.create_task(sth.task_func(0.1), name="cancel_before_start").cancel()

    with _assert_no_unawaited_coroutine_warning():
        # Run the scenario in its own frame so the task group (and the cancelled task it holds) are
        # released before the warning check forces a garbage collection.
        await scenario()


@pytest.mark.asyncio
async def test_scheduling_invalid_usage(sth, sth_with_unfinished_tasks):
    async with sth as s:
        # No creation of task node or task group should be possible - they all need to be tracked under one root node
        with pytest.raises(RuntimeError):
            tn_1 = SchedulerTaskGroupNode()
        with pytest.raises(RuntimeError):
            tn_2 = SchedulerTaskGroup()

        with pytest.raises(AttributeError, match="'SchedulerRootNode' object has no attribute 'create_task'"):
            s.root_task_node().create_task(sth.task_func(0.1))
        with pytest.raises(AttributeError, match="'SchedulerTreeNode' object has no attribute 'create_task'"):
            s.root_task_node().create_tree_node().create_task(sth.task_func(0.1))

    async with sth as s:
        # All possible ways to call the functions the wrong way in "Initialized" state
        tn_initialized = s.root_task_node().create_task_group_node()
        with pytest.raises(RuntimeError, match="state=.*Initialized"):
            tn_initialized.create_task(sth.task_func(0.1))
        with pytest.raises(RuntimeError, match="state=.*Exception"):
            await tn_initialized.__aexit__()

    # Repeated attempts to enter a task group are invalid
    async with sth as s:
        async with s.root_task_node().create_task_group_node() as tn_entered:
            assert tn_entered._task_group._state == TaskGroupState.Entered
            with pytest.raises(RuntimeError, match="state=.*Entered"):
                await tn_entered.__aenter__()

    # Entering a sub task node without its own task and not exited it
    # before the parent exit is an error.
    async with sth_with_unfinished_tasks as s:
        with pytest.raises(
            RuntimeError, match="Child task node 'child' has not been awaited yet before the parent task node 'parent' was awaited"
        ):
            async with s.root_task_node().create_task_group_node("parent") as tn_parent:
                tn_child = tn_parent.create_task_group_node("child")
                # if we do not enter, it is fine - no tasks can be waiting.
                await tn_child.__aenter__()
                tn_child.create_task(sth.task_func(0.1))

    # It is hard to call a function in the "Waiting" state because this is called using "await", and changed once the "await" is finished.

    # All possible ways to call the functions the wrong way in "Completed" state
    async with sth as s:
        async with s.root_task_node().create_task_group_node() as tn_completed:
            tn_completed.create_task(sth.task_func(0.1))
        assert tn_completed._task_group._state == TaskGroupState.Completed
        with pytest.raises(RuntimeError, match="state=.*Completed"):
            await tn_completed.create_task(sth.task_func(0.1))
        with pytest.raises(RuntimeError, match="state=.*Exception"):
            await tn_completed.__aexit__(None, None, None)

    # All possible ways to call the functions the wrong way in "Exception" state
    async with sth as s:
        with pytest.raises(RuntimeError, match="Just an exception"):
            async with s.root_task_node().create_task_group_node() as tn_completed:
                tn_completed.create_task(sth.task_func(0.1, exception=RuntimeError("Just an exception")))
        assert tn_completed._task_group._state == TaskGroupState.Exception
        with pytest.raises(RuntimeError, match="state=.*Exception"):
            await tn_completed.create_task(sth.task_func(0.1))
        with pytest.raises(RuntimeError, match="state=.*Exception"):
            await tn_completed.__aexit__(None, None, None)


def test_scheduling_task_exception_captured(sth):
    # For exceptions raised by tasks, test handling of an exceptions outside of the execution of the "async with" block.
    captured_exception: Optional[RuntimeError] = None
    raised_exception = RuntimeError("Raise an exception!")

    async def run_me(s: SchedulerContext):
        try:
            async with s.parent_task_node().create_task_group_node() as tg:
                tg.create_task(sth.task_func(0.1), name="1")
                tg.create_task(sth.task_func(0.2, exception=raised_exception), name="2")
                tg.create_task(sth.task_func(0.3), name="3")
        except RuntimeError as e:
            # The exception can be caught outside the "async with" block
            nonlocal captured_exception
            captured_exception = e

    sth.run_with_task_tracking(run_me)
    assert captured_exception == raised_exception
    assert len(sth.task_state) == 3
    sth.task_state["1"].assert_finished()
    sth.task_state["2"].assert_raised_exception(raised_exception)
    sth.task_state["3"].assert_cancelled()


@pytest.mark.parametrize(
    "raised_exception",
    [pytest.param(RuntimeError("Raise an exception!!"), id="RuntimeError"), pytest.param(KeyboardInterrupt(), id="KeyboardInterrupt")],
)
def test_scheduling_task_exception_fallthrough(sth, raised_exception):
    # For exceptions raised by tasks, test that they fall through to the root caller if not handled
    async def run_me(scheduler: SchedulerContext):
        async with scheduler.parent_task_node().create_task_group_node() as tg:
            tg.create_task(sth.task_func(0.1), name="1")
            tg.create_task(sth.task_func(0.2, exception=raised_exception), name="2")
            tg.create_task(sth.task_func(0.3), name="3")

    captured_exception: Optional[BaseException] = None
    try:
        sth.run_with_task_tracking(run_me)
    except BaseException as e:
        captured_exception = e
    assert captured_exception == raised_exception
    assert len(sth.task_state) == 3
    sth.task_state["1"].assert_finished()
    sth.task_state["2"].assert_raised_exception(raised_exception)
    sth.task_state["3"].assert_cancelled()


@pytest.mark.parametrize(
    "raised_exception",
    [pytest.param(RuntimeError("Raise an exception!!"), id="RuntimeError"), pytest.param(KeyboardInterrupt(), id="KeyboardInterrupt")],
)
def test_scheduling_queue_exception_captured(sth, raised_exception):
    # For exceptions raised when adding tasks, test handling of an exceptions outside of the execution of the "async with" block.
    captured_exception: Optional[RuntimeError] = None

    async def run_me(scheduler: SchedulerContext):
        try:
            async with scheduler.parent_task_node().create_task_group_node() as tg:
                tg.create_task(sth.task_func(0.1), name="1")
                tg.create_task(sth.task_func(10), name="2")
                await asyncio.sleep(0.2)  # give the task a chance to start and complete
                raise raised_exception
                tg.create_task(sth.task_func(0.3))
        except BaseException as e:
            # The exception can be caught outside the "async with" block
            nonlocal captured_exception
            captured_exception = e

    sth.run_with_task_tracking(run_me)
    assert captured_exception == raised_exception
    assert len(sth.task_state) == 2  # The third task is never started
    sth.task_state["1"].assert_finished()
    sth.task_state["2"].assert_cancelled()


@pytest.mark.parametrize(
    "raised_exception",
    [pytest.param(RuntimeError("Raise an exception!!"), id="RuntimeError"), pytest.param(KeyboardInterrupt(), id="KeyboardInterrupt")],
)
def test_scheduling_queue_exception_fallthrough(sth, raised_exception):
    # For exceptions raised when adding tasks, test that they fall through to the root caller if not handled
    async def run_me(scheduler: SchedulerContext):
        async with scheduler.parent_task_node().create_task_group_node() as tg:
            tg.create_task(sth.task_func(0.1), name="1")
            tg.create_task(sth.task_func(10), name="2")
            await asyncio.sleep(0.2)  # give the task a chance to start and complete
            raise raised_exception
            tg.create_task(sth.task_func(0.3))

    captured_exception: Optional[BaseException] = None
    try:
        sth.run_with_task_tracking(run_me)
    except BaseException as e:
        captured_exception = e
    assert captured_exception == raised_exception
    assert len(sth.task_state) == 2  # The third task is never started
    sth.task_state["1"].assert_finished()
    sth.task_state["2"].assert_cancelled()


def test_scheduling_task_keyboard_interrupt_falls_through_despite_capture(sth):
    # KeyboardInterrupt in one task will cancel an entire task group, even if we try to handle it.
    inside_captured_exception: Optional[KeyboardInterrupt] = None
    raised_exception = KeyboardInterrupt()

    async def run_me(s: SchedulerContext):
        try:
            async with s.parent_task_node().create_task_group_node() as tg:
                tg.create_task(sth.task_func(0.1), name="1")
                tg.create_task(sth.task_func(0.2, exception=raised_exception), name="2")
                tg.create_task(sth.task_func(0.3), name="3")
        except KeyboardInterrupt as e:
            # The exception can be caught outside the "async with" block
            nonlocal inside_captured_exception
            inside_captured_exception = e

    outside_captured_exception: Optional[BaseException] = None
    try:
        sth.run_with_task_tracking(run_me)
    except BaseException as e:
        outside_captured_exception = e
    assert len(sth.task_state) == 3
    assert inside_captured_exception == raised_exception
    assert outside_captured_exception == raised_exception
    sth.task_state["1"].assert_finished()
    sth.task_state["2"].assert_raised_exception(raised_exception)
    sth.task_state["3"].assert_cancelled()


def test_task_exception_was_never_retrieved(caplog, capsys):
    # This test has nothing to do with WRAPP itself, but it helps to understand why the
    # WRAPP tests for the handling of the KeyboardInterrupt need a weird workaround:
    # This is the simplest example for demonstrating the issue described here:
    # https://github.com/python/cpython/issues/108549
    # For us this issue results in a spurious and confusing "Task exception never retrieved" being
    # logged at the end of the test run by asyncio.run if a KeyboardInterrupt is raised.
    # The test also shows how to avoid the issue by installing an event handler which
    # suppresses the spurious message, but the exception is still falling through to the
    # asyncio.run, as it should be.
    # I left the code in here just to keep that simple example to make it easier if the
    # behaviour in future Python versions changes.
    async def raise_keyboard_interrupt():
        raise KeyboardInterrupt()

    async def run_me():
        main_task_created_by_asyncio = asyncio.current_task()
        assert main_task_created_by_asyncio is not None

        def _squelch_main_kbdint(loop, ctx):
            # Apparently in Python 3.11, "task" is not reliably set, and we need to use "future".
            # The "if" tries to make sure that we are only suppressing the exception we really
            # want to suppress.
            # Note that the exception handler is only executed after the test itself has
            # terminated - not quite sure why - so it is impossible to test within this
            # test if there actually was an exception that was suppressed.
            t = ctx.get("task") or ctx.get("future")
            if (
                ctx.get("message") == "Task exception was never retrieved"
                and isinstance(ctx.get("exception"), KeyboardInterrupt)
                and t is main_task_created_by_asyncio
            ):
                return
            loop.default_exception_handler(ctx)

        # If this line is commented out, and the problem still exists, you should see a
        # "Task exception was never retrieved" error message after the test has been run
        # (tested with Python 3.11.
        asyncio.get_running_loop().set_exception_handler(_squelch_main_kbdint)
        t1 = asyncio.create_task(asyncio.sleep(3600.0), name="CancelledTask")
        await asyncio.sleep(0.1)  # give it time to generate all tasks
        t2 = asyncio.create_task(raise_keyboard_interrupt(), name="InterruptingTask")
        await asyncio.gather(t1, t2)

    outside_captured_exception: Optional[BaseException] = None
    try:
        asyncio.run(run_me())
    except BaseException as e:
        outside_captured_exception = e
    assert isinstance(outside_captured_exception, KeyboardInterrupt)


# The actual test is if we can complete this test reasonably quickly
@pytest.mark.timeout(10, func_only=True)
def test_scheduling_task_ctrl_c_with_aborted_shutdown(sth):
    # Test that a shutdown with many tasks caused by a KeyboardInterrupt
    # does not take ridiculously long. This is really important for the CLI:
    # if we take ages to terminate the process after "Ctrl+C", nobody is happy.
    # BEWARE: This test does run fine, but if a debugger is attached and a breakpoint is set, it might fail
    # Not sure why, I assume it is the large amount of async tasks which are generated.
    raised_keyboard_interrupt_exception = KeyboardInterrupt()

    total_num_tasks: int = 0

    async def generate_keyboard_interrupt():
        raise raised_keyboard_interrupt_exception

    interrupting_task_name: str = "interrupting_task"

    async def run_me(scheduler: SchedulerContext):
        nonlocal interrupting_task_name
        nonlocal total_num_tasks

        asyncio.get_event_loop().set_debug(False)
        async with scheduler.parent_task_node().create_task_group_node("root") as task_node:
            total_num_tasks = await sth.generate_large_task_set(task_node)
            task_node.create_task(sth.tracked_task(generate_keyboard_interrupt()), name=interrupting_task_name)

    captured_exception: Optional[BaseException] = None
    try:
        sth.run_with_task_tracking(run_me)
    except BaseException as e:
        captured_exception = e
    assert captured_exception == raised_keyboard_interrupt_exception
    # We expect all tasks to have been generated and be aborted through an exception.
    assert len(sth.task_state) == total_num_tasks + 1
    for task_state in sth.task_state.values():
        if task_state.task_name == interrupting_task_name:
            task_state.assert_raised_exception(raised_keyboard_interrupt_exception)
        else:
            task_state.assert_cancelled()


@pytest.mark.timeout(30, func_only=True)
@pytest.mark.skipif(
    sys.platform == "win32", reason="os.kill(SIGINT) sends CTRL_C_EVENT to all console processes on Windows, crashing xdist workers"
)
def test_wrapp_thread_ctrl_c_cancels_scheduler_tasks(wrapp_init):
    """Ctrl+C during a command on WrappEventLoopThread must cancel all scheduler tasks.

    Exercises the real CLI code path: a coroutine using the WRAPP scheduler
    (``WrappScheduler`` → ``SchedulerTaskGroupNode`` → ``SchedulerTaskGroup``) runs on
    ``WrappEventLoopThread`` while the main thread blocks on ``Future.result()``.
    A real SIGINT is delivered, and we assert that after the ``KeyboardInterrupt`` the
    WRAPP scheduler tree is fully torn down — no leaked tasks.

    Contrast with ``test_scheduling_task_ctrl_c_with_aborted_shutdown`` which proves
    the scheduler's *internal* cancellation works when ``KeyboardInterrupt`` originates
    **inside** the event loop.  This test covers the *external* path where the interrupt
    arrives on the main thread and must be bridged to the event loop.
    """
    from wrapp.utils.coro import async_typer

    tasks_started = threading.Event()
    root_node_holder: list = []
    num_tasks = 20

    @async_typer
    async def simulated_command():
        """Simulates a CLI command (like mirror) that uses the WRAPP scheduler."""
        async with WrappScheduler() as scheduler:
            root_node_holder.append(scheduler.root_task_node())
            async with scheduler.parent_task_node().create_task_group_node("ctrl-c-test") as task_node:
                for i in range(num_tasks):
                    task_node.create_task(asyncio.sleep(3600), name=f"ctrl-c-task-{i}")
                tasks_started.set()
                await asyncio.sleep(3600)

    threading.Thread(
        target=lambda: (tasks_started.wait(5), time.sleep(0.1), os.kill(os.getpid(), signal.SIGINT)),
        daemon=True,
    ).start()

    # Temporarily restore Python's default SIGINT handler so os.kill actually
    # raises KeyboardInterrupt.  Pytest plugins (coverage, xdist, timeout) may
    # replace the handler with one that merely sets a flag, which would cause
    # Future.result() to block indefinitely instead of raising.
    original_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            simulated_command()
    finally:
        signal.signal(signal.SIGINT, original_handler)

    root_node = root_node_holder[0]
    try:
        # After Ctrl+C the WRAPP scheduler tree must be fully torn down.
        open_tasks = root_node.all_open_tasks()
        assert not open_tasks, (
            f"{len(open_tasks)} of {num_tasks} scheduler tasks still running after Ctrl+C. "
            "async_typer must propagate cancellation to the event loop on KeyboardInterrupt."
        )
        assert root_node.done(), "WRAPP scheduler tree should be fully torn down after Ctrl+C."
    finally:
        # Cancel any leaked tasks so they don't interfere with later tests.
        thread = WrappEventLoopThread.me()
        future = thread.run_coroutine(root_node.cancel_all_tasks())
        try:
            future.result(timeout=5)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_scheduling_initialized_multiple_calls_OM_116094(wrapp_init, tmp_path):
    # See OM-116094
    # Make sure we can call the same function multiple times, despite CommandContext being the same object
    # for each call.
    test_data_path = tmp_path / "catalog-source"
    test_data_path.mkdir()
    (test_data_path / "asset.usd").write_text("#usda 1.0", encoding="utf-8")
    test_data_path = str(test_data_path)
    c = await catalog(test_data_path, local_hash=True)
    assert len(c.items) > 0
    c = await catalog(test_data_path, local_hash=True)
    assert len(c.items) > 0

    # Harder: what if both calls are done concurrently?
    async def run_catalog_with_delay(delay: float) -> Catalog:
        await asyncio.sleep(delay)
        return await catalog(test_data_path, local_hash=True)

    t1 = asyncio.create_task(run_catalog_with_delay(delay=0.1))
    t2 = asyncio.create_task(run_catalog_with_delay(delay=0.0))
    r = await asyncio.gather(t1, t2)
    assert len(r[0].items) > 0
    assert len(r[1].items) > 0


@pytest.mark.asyncio
@pytest.mark.timeout(3, func_only=True)
async def test_scheduling_recursive_task_groups_block_when_max_jobs_reached_OMPE40881(sth, wrapp_init):
    scheduler_context = SchedulerContext(SchedulingParameters(jobs=1))

    async def wait_for_task_group():
        async with scheduler_context.parent_task_node().create_task_group_node() as tg:
            tg.create_task(sth.task_func(0.1))

    async with WrappScheduler(context=scheduler_context) as scheduler:
        async with scheduler.root_task_node().create_task_group_node() as tg:
            tg.create_task(wait_for_task_group())


@pytest.mark.asyncio
async def test_loop_creating_task_groups(sth):
    # Tests the pattern Patch.execute uses to create task groups.
    async def simple_task(index: int) -> int:
        await asyncio.sleep(0.1)
        return index * 2

    async with sth as s:
        all_indices = [(i, j) for j in range(10) for i in range(10)]
        it = iter(all_indices)
        curr_index = all_indices[0]
        done = False
        while not done:
            task_results: dict[asyncio.Task, int] = {}
            async with s.root_task_node().create_task_group_node(description=f"{curr_index[0]}", task_results=task_results) as tn:
                while not done:
                    tn.create_task(sth.tracked_task(simple_task(curr_index[1])), name=f"{curr_index[0]}-{curr_index[1]}")
                    try:
                        next_index = next(it)
                    except StopIteration:
                        # The iteration completed normally
                        done = True
                        break
                    if next_index[0] != curr_index[0]:
                        # next task node with block of tasks to be executed concurrently.
                        curr_index = next_index
                        break
                    else:
                        # next task to be executed concurrently with the last one
                        curr_index = next_index
            assert tn._task_group.done()
            assert tn.done()
            for t, r in task_results.items():
                assert t.done()
                task_name = t.get_name()
                j = int(task_name[-1])
                assert r == j * 2
        sth.finalize_tasks()
        for i, j in all_indices:
            sth.task_state[f"{i}-{j}"].assert_finished()


@pytest.mark.timeout(10, func_only=True)
@pytest.mark.asyncio
async def test_scheduling_cancelling_tasks_with_node_func(sth):
    # Test that a user of the Python API can cancel a task
    total_num_tasks: int = 0

    asyncio.get_event_loop().set_debug(False)
    async with sth as s:
        async with s.parent_task_node().create_task_group_node("root") as task_node:
            total_num_tasks = await sth.generate_large_task_set(task_node)
            await asyncio.sleep(0.2)
            await task_node.cancel_all_tasks()

    # We expect all tasks to have been generated and be aborted through an exception.
    assert total_num_tasks > 0  # Without this, the test completes successfully even if the code raises an exception
    assert len(sth.task_state) == total_num_tasks
    for task_state in sth.task_state.values():
        task_state.assert_cancelled()


@pytest.mark.timeout(10, func_only=True)
@pytest.mark.asyncio
async def test_scheduling_cancelling_tasks_through_asyncio(sth):
    # Test that a user of the Python API can cancel a task
    total_num_tasks: int = 0
    tasks_created = asyncio.Event()

    asyncio.get_event_loop().set_debug(False)

    async def wrapp_func(sth, tree_node: SchedulerTreeNode):
        nonlocal total_num_tasks
        async with tree_node.create_task_group_node("root") as task_node:
            total_num_tasks = await sth.generate_large_task_set(task_node)
            tasks_created.set()

    async with sth as s:
        my_task = asyncio.create_task(wrapp_func(sth, s.parent_task_node()))
        await tasks_created.wait()
        my_task.cancel()
        while not my_task.cancelled():
            await asyncio.sleep(0.1)

    # We expect all tasks to have been generated and be aborted through an exception.
    assert total_num_tasks > 0  # Without this, the test completes successfully even if the code raises an exception
    assert len(sth.task_state) == total_num_tasks
    for task_state in sth.task_state.values():
        task_state.assert_cancelled()


class TestSchedulingParametersValidation:
    """Test that SchedulingParameters and ThrottlingParameters reject invalid values.

    OMPE-80486: These classes were accepting inf, nan, booleans, strings, etc.
    which would cause undefined behavior in the scheduler.
    """

    @pytest.mark.parametrize(
        "invalid_value,description",
        [
            pytest.param(float("inf"), "positive infinity", id="inf"),
            pytest.param(float("-inf"), "negative infinity", id="neg_inf"),
            pytest.param(float("nan"), "NaN", id="nan"),
            pytest.param("100", "string", id="string"),
            pytest.param(True, "boolean True (evaluates to 1)", id="bool_true"),
            pytest.param(False, "boolean False (evaluates to 0)", id="bool_false"),
            pytest.param(None, "None", id="none"),
            pytest.param([1, 2, 3], "list", id="list"),
            pytest.param({"value": 1}, "dict", id="dict"),
            pytest.param(3.14, "float", id="float"),
        ],
    )
    def test_scheduling_parameters_rejects_invalid_jobs(self, invalid_value, description):
        """SchedulingParameters.jobs must be a positive integer."""
        with pytest.raises((TypeError, ValueError)):
            SchedulingParameters(jobs=invalid_value)

    @pytest.mark.parametrize(
        "invalid_value,description",
        [
            pytest.param(float("inf"), "positive infinity", id="inf"),
            pytest.param(float("-inf"), "negative infinity", id="neg_inf"),
            pytest.param(float("nan"), "NaN", id="nan"),
            pytest.param("50", "string", id="string"),
            pytest.param(True, "boolean True", id="bool_true"),
            pytest.param(None, "None", id="none"),
        ],
    )
    def test_scheduling_parameters_rejects_invalid_tagging_jobs(self, invalid_value, description):
        """SchedulingParameters.tagging_jobs must be a positive integer."""
        with pytest.raises((TypeError, ValueError)):
            SchedulingParameters(tagging_jobs=invalid_value)

    @pytest.mark.parametrize(
        "invalid_value,description",
        [
            pytest.param(float("inf"), "positive infinity", id="inf"),
            pytest.param(float("-inf"), "negative infinity", id="neg_inf"),
            pytest.param(float("nan"), "NaN", id="nan"),
            pytest.param("10", "string", id="string"),
            pytest.param(True, "boolean True", id="bool_true"),
            pytest.param(None, "None", id="none"),
        ],
    )
    def test_scheduling_parameters_rejects_invalid_file_transfer_jobs(self, invalid_value, description):
        """SchedulingParameters.file_transfer_jobs must be a positive integer."""
        with pytest.raises((TypeError, ValueError)):
            SchedulingParameters(file_transfer_jobs=invalid_value)

    @pytest.mark.parametrize(
        "invalid_value,description",
        [
            pytest.param(float("inf"), "positive infinity", id="inf"),
            pytest.param(float("nan"), "NaN", id="nan"),
            pytest.param("100", "string", id="string"),
            pytest.param(True, "boolean True", id="bool_true"),
            pytest.param(None, "None", id="none"),
        ],
    )
    def test_throttling_parameters_rejects_invalid_jobs(self, invalid_value, description):
        """ThrottlingParameters.jobs must be a positive integer."""
        with pytest.raises((TypeError, ValueError)):
            ThrottlingParameters(jobs=invalid_value)

    @pytest.mark.parametrize(
        "invalid_value,description",
        [
            pytest.param(0, "zero", id="zero"),
            pytest.param(-1, "negative", id="negative"),
            pytest.param(-100, "large negative", id="large_negative"),
        ],
    )
    def test_scheduling_parameters_rejects_non_positive_jobs(self, invalid_value, description):
        """SchedulingParameters.jobs must be positive (> 0)."""
        with pytest.raises(ValueError):
            SchedulingParameters(jobs=invalid_value)

    def test_scheduling_parameters_accepts_valid_values(self):
        """SchedulingParameters should accept valid positive integers."""
        params = SchedulingParameters(jobs=100, tagging_jobs=25, file_transfer_jobs=5)
        assert params.jobs == 100
        assert params.tagging_jobs == 25
        assert params.file_transfer_jobs == 5

    def test_throttling_parameters_accepts_valid_values(self):
        """ThrottlingParameters should accept valid positive integers."""
        params = ThrottlingParameters(jobs=100, tagging_jobs=25, file_transfer_jobs=5)
        assert params.jobs == 100
        assert params.tagging_jobs == 25
        assert params.file_transfer_jobs == 5
