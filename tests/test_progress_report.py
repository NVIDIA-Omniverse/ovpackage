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
import copy
import os

import deepdiff
import pytest

import wrapp
from tests.scheduling_test_helper import (
    SchedulingTestHelpers,
)
from tests.scheduling_test_helper import (
    scheduling_test_helper_with_progress_report as sth,
)
from wrapp.utils.progress_report import (
    ProgressReport,
    ProgressReportStatus,
)
from wrapp.utils.scheduler_task_node import SchedulerTreeNode


def test_progress_report_no_children_reports_running():
    """
    When the root node has no children (no jobs registered yet), the
    generator should produce a Running / 0.0 report. This is the edge case
    that occurs when the progress reporter fires before any jobs have been
    registered on the root node.
    """
    report = ProgressReport("WRAPP", 0, 0, None)
    report.finalize(ProgressReportStatus.Running, [])

    assert report.status() == ProgressReportStatus.Running
    assert report.progress() == 0.0


@pytest.mark.asyncio
async def test_progress_report_no_jobs_registered_yet(sth):
    """
    When the scheduler starts and the progress reporter fires before any jobs
    have been registered, the first callback should receive a report with
    status=Running and progress=0.0 (not Finished/1.0).
    """
    async with sth as s:
        # Yield to the event loop so the progress reporter fires
        # BEFORE any task nodes are created on the root.
        await asyncio.sleep(0.05)

        # At this point the reporter should have fired at least once.
        reports_before_work = list(sth.progress_reports())

        # Now do some actual work so the scheduler can exit cleanly.
        async with s.parent_task_node().create_task_group_node("MainJob") as tn_1:
            tn_1.create_task(sth.task_func(0.1), name="SoleTask")

    assert len(reports_before_work) >= 1, "Expected at least one progress report before jobs were created"

    first_report = reports_before_work[0]
    assert first_report.job_name() == "WRAPP"
    assert len(first_report.sub_jobs()) == 0
    assert first_report.status() == ProgressReportStatus.Running
    assert first_report.progress() == 0.0


@pytest.mark.asyncio
async def test_progress_report_empty_scheduler(sth):
    """
    A scheduler that starts and stops without any work should report
    Finished in its final report — there's nothing left to do.
    """
    async with sth as s:
        pass

    reports = sth.progress_reports()
    assert len(reports) >= 1

    final_report = reports[-1]
    assert final_report.job_name() == "WRAPP"
    assert len(final_report.sub_jobs()) == 0
    assert final_report.status() == ProgressReportStatus.Finished
    assert final_report.progress() == 1.0


@pytest.mark.asyncio
async def test_progress_report_empty_tree_node_placeholder(sth):
    """
    An intentionally empty tree node (used as a placeholder to keep
    expected_num_sub_jobs accurate) should report Finished after all work
    completes, not stay stuck at Running forever.

    This mirrors the pattern in dependency_graph.py where a package with
    no dependencies creates an empty tree node:
        task_node.create_tree_node("Adding package with no dependencies")
    """
    async with sth as s:
        parent = s.parent_task_node().create_task_group_node("AnalyseDeps", expected_num_sub_jobs=2)
        async with parent as parent_tg:
            # One dependency has real work
            async with parent_tg.create_task_group_node("DepWithWork") as work_node:
                work_node.create_task(sth.task_func(0.1), name="RealTask")
            # Other dependency has no sub-deps — empty placeholder
            parent_tg.create_tree_node("DepWithNoWork")

    final_pr = sth.progress_reports()[-1]
    assert final_pr.status() == ProgressReportStatus.Finished
    assert final_pr.progress() == 1.0

    analyse_job = final_pr.sub_jobs()[0]
    assert analyse_job.job_name() == "AnalyseDeps"
    assert analyse_job.status() == ProgressReportStatus.Finished

    dep_with_work = analyse_job.sub_jobs()[0]
    assert dep_with_work.job_name() == "DepWithWork"
    assert dep_with_work.status() == ProgressReportStatus.Finished

    dep_placeholder = analyse_job.sub_jobs()[1]
    assert dep_placeholder.job_name() == "DepWithNoWork"
    assert dep_placeholder.status() == ProgressReportStatus.Finished


@pytest.mark.asyncio
async def test_progress_report_one_job(sth):
    async with sth as s:
        # Just one job
        async with s.parent_task_node().create_task_group_node("MainJob") as tn_1:
            tn_1.create_task(sth.task_func(0.1), name="SoleTask")
    assert len(sth.progress_reports()) == 3
    first_pr = sth.progress_reports()[0]
    middle_pr = sth.progress_reports()[1]
    final_pr = sth.progress_reports()[2]

    # Initially, there is one task executing, everything running.
    assert first_pr.job_name() == "WRAPP"
    assert first_pr.status() == ProgressReportStatus.Running
    assert first_pr.progress() == 0
    assert first_pr.num_tasks_executing() == 0
    assert first_pr.num_tasks_done() == 0
    assert first_pr.total_num_tasks_executing() == 1
    assert first_pr.total_num_tasks_done() == 0
    assert len(first_pr.sub_jobs()) == 1
    assert first_pr.sub_jobs()[0].job_name() == "MainJob"
    assert first_pr.sub_jobs()[0].status() == ProgressReportStatus.Running
    assert first_pr.sub_jobs()[0].num_tasks_executing() == 1
    assert first_pr.sub_jobs()[0].num_tasks_done() == 0
    assert first_pr.sub_jobs()[0].total_num_tasks_executing() == 1
    assert first_pr.sub_jobs()[0].total_num_tasks_done() == 0
    assert len(first_pr.sub_jobs()[0].sub_jobs()) == 0

    compare_pr: ProgressReport = copy.deepcopy(first_pr)
    assert deepdiff.DeepDiff(first_pr, compare_pr) == {}

    # The task has finished, but the TaskGroup has not yet wrapped up (i.e. asyncio.TaskGroup.done() == False)
    compare_pr._total_num_tasks_executing = 0
    compare_pr._total_num_tasks_done = 1
    compare_pr.sub_jobs()[0]._num_tasks_executing = 0
    compare_pr.sub_jobs()[0]._num_tasks_done = 1
    compare_pr.sub_jobs()[0]._total_num_tasks_executing = 0
    compare_pr.sub_jobs()[0]._total_num_tasks_done = 1
    assert deepdiff.DeepDiff(middle_pr, compare_pr) == {}
    assert middle_pr.progress() == 1.0

    # Everything finished
    compare_pr._status = ProgressReportStatus.Finished
    compare_pr.sub_jobs()[0]._status = ProgressReportStatus.Finished
    assert deepdiff.DeepDiff(final_pr, compare_pr) == {}
    assert final_pr.progress() == 1.0


@pytest.mark.asyncio
async def test_progress_report_few_jobs(sth):
    async with sth as s:
        # Just one job
        async with s.parent_task_node().create_task_group_node("MainJob") as tn_1:
            tn_1.create_task(sth.task_func(0.3), name="Task01")
            tn_1.create_task(sth.task_func(0.4), name="Task02")
            tn_1.create_task(sth.task_func(0.2), name="Task03")
            tn_1.create_task(sth.task_func(0.1), name="Task04")

    # The progress reporter polls at intervals and only reports when state changes.
    # Due to timing variations, multiple tasks may complete between polls, resulting in
    # fewer reports. We expect at minimum: initial report + at least one intermediate + final.
    # Maximum is 6 (initial + 4 task completions + final).
    reports = sth.progress_reports()
    assert 3 <= len(reports) <= 6, f"Expected 3-6 progress reports, got {len(reports)}"

    first_pr = reports[0]
    assert first_pr.job_name() == "WRAPP"
    assert first_pr.status() == ProgressReportStatus.Running
    assert first_pr.progress() == 0
    assert first_pr.num_tasks_executing() == 0
    assert first_pr.num_tasks_done() == 0
    assert first_pr.total_num_tasks_executing() == 4
    assert first_pr.total_num_tasks_done() == 0
    assert len(first_pr.sub_jobs()) == 1
    assert first_pr.sub_jobs()[0].job_name() == "MainJob"
    assert first_pr.sub_jobs()[0].status() == ProgressReportStatus.Running
    assert first_pr.sub_jobs()[0].num_tasks_executing() == 4
    assert first_pr.sub_jobs()[0].num_tasks_done() == 0
    assert first_pr.sub_jobs()[0].total_num_tasks_executing() == 4
    assert first_pr.sub_jobs()[0].total_num_tasks_done() == 0
    assert len(first_pr.sub_jobs()[0].sub_jobs()) == 0

    # Verify intermediate reports show monotonic progress (tasks completing)
    for i in range(1, len(reports) - 1):
        pr = reports[i]
        prev_pr = reports[i - 1]
        assert pr.total_num_tasks_done() >= prev_pr.total_num_tasks_done()
        assert pr.total_num_tasks_executing() <= prev_pr.total_num_tasks_executing()
        assert pr.total_num_tasks_done() + pr.total_num_tasks_executing() == 4

    # Verify final report shows completion
    final_pr = reports[-1]
    assert final_pr.status() == ProgressReportStatus.Finished
    assert final_pr.total_num_tasks_done() == 4
    assert final_pr.total_num_tasks_executing() == 0
    assert final_pr.sub_jobs()[0].status() == ProgressReportStatus.Finished
    assert final_pr.progress() == 1


@pytest.mark.asyncio
async def test_progress_report_example_usage(sth):
    async def some_complex_sub_job(sth, sub_node: SchedulerTreeNode):
        async with sub_node.create_task_group_node(f"WorkDoneIn{sub_node.description()}") as tn_2:
            task_1: asyncio.Task = tn_2.create_task(sth.task_func(0.3), name="{sub_node.description()}_Task1")
            task_2: asyncio.Task = tn_2.create_task(sth.task_func(0.4), name="{sub_node.description()}_Task2")
            task_3: asyncio.Task = tn_2.create_task(sth.task_func(0.2), name="{sub_node.description()}_Task3")
            task_4: asyncio.Task = tn_2.create_task(sth.task_func(0.1), name="{sub_node.description()}_Task4")

    # Demonstrate how the progress report and the task nodes work together
    async with sth as s:
        # A typical WRAPP operation will make first a SchedulerTreeNode for all tasks it executes.
        # It is important to use "parent_task_node()" and not "root_task_node()" - if the WRAPP operation
        # is used by another WRAPP operation, the root_task_node() is the node passed in from the
        # outside for the original WRAPP call, the parent_task_node() is what the outer WRAPP operation
        # passes to the inner one.
        main_node = s.parent_task_node().create_tree_node("MainJob")
        # The operation can then generate task group nodes to execute blocks of tasks concurrently.
        # After the block is done, the tasks are finished, just as they would be for ayncio.TaskGroup.
        async with main_node.create_task_group_node("WorkDoneInMainJob") as tn_1:
            task_1: asyncio.Task = tn_1.create_task(sth.task_func(0.3), name="MainJob_Task1")
            task_2: asyncio.Task = tn_1.create_task(sth.task_func(0.4), name="MainJob_Task2")
            task_3: asyncio.Task = tn_1.create_task(sth.task_func(0.2), name="MainJob_Task3")
            task_4: asyncio.Task = tn_1.create_task(sth.task_func(0.1), name="MainJob_Task4")
        assert task_1.done() and task_2.done() and task_3.done() and task_4.done()
        # If the WRAPP operation calls more complex functions or other WRAPP operations, it can be
        # useful to group those operations into one SchedulerTreeNode. Technically it is also possible
        # to use a SchedulerTaskGroupNode, but that essentially forces an "async with" which is not
        # always practical. SchedulerTreeNodes and SchedulerTaskGroupNode will appear in the progress report:
        sub_node_1 = main_node.create_tree_node("SubNode_1")
        await some_complex_sub_job(sth, sub_node_1)
        await some_complex_sub_job(sth, sub_node_1)
        sub_node_2 = main_node.create_tree_node("SubNode_2")
        await some_complex_sub_job(sth, sub_node_2)

    # The progress reports directly all SchedulerTreeNodes and SchedulerTaksGroupNodes created:
    pr = sth.progress_reports()[-1]
    assert pr.progress() == 1.0  # all done
    # The root of the progress report defaults to "WRAPP" (customizable via SchedulingParameters.job_name)
    assert pr.job_name() == "WRAPP"
    # The first children are the commands executed within the SchedulerContext.
    # This is due to each WRAPP command explicitly creating a top-level tree node.
    assert pr.sub_jobs()[0].job_name() == "MainJob"
    # And the jobs for each WRAPP command will then contain the SchedulerTreeNodes created
    # during the execution of that WRAPP command
    assert pr.sub_jobs()[0].sub_jobs()[0].job_name() == "WorkDoneInMainJob"
    assert pr.sub_jobs()[0].sub_jobs()[0].total_num_tasks_done() == 4
    assert pr.sub_jobs()[0].sub_jobs()[1].job_name() == "SubNode_1"
    assert pr.sub_jobs()[0].sub_jobs()[1].total_num_tasks_done() == 8
    assert len(pr.sub_jobs()[0].sub_jobs()[1].sub_jobs()) == 2
    assert pr.sub_jobs()[0].sub_jobs()[2].job_name() == "SubNode_2"
    assert pr.sub_jobs()[0].sub_jobs()[2].total_num_tasks_done() == 4
    assert len(pr.sub_jobs()[0].sub_jobs()[2].sub_jobs()) == 1
    assert pr.sub_jobs()[0].sub_jobs()[2].sub_jobs()[0].job_name() == "WorkDoneInSubNode_2"


@pytest.mark.asyncio
async def test_custom_job_name_in_progress_report_OMPE_83813(wrapp_init):
    """Custom job_name set via SchedulingParameters must appear in the root ProgressReport."""
    from wrapp import (
        ProgressReportVerbosity,
        SchedulerContext,
        SchedulingParameters,
        WrappScheduler,
    )

    helper = SchedulingTestHelpers(progress_report_interval=0)
    params = SchedulingParameters(
        progress_report=ProgressReportVerbosity.verbose,
        progress_report_callback=helper.store_progress_report,
        progress_report_interval=0,
        job_name="PACKAGING",
    )
    scheduler = WrappScheduler(context=SchedulerContext(params=params))
    async with scheduler as s:
        async with s.parent_task_node().create_task_group_node("PackageStep") as tn:
            tn.create_task(helper.task_func(0.1), name="Task1")
    helper.finalize_tasks()

    assert len(helper.progress_reports()) >= 1
    root_pr = helper.progress_reports()[0]
    assert root_pr.job_name() == "PACKAGING"
    assert root_pr.sub_jobs()[0].job_name() == "PackageStep"


@pytest.mark.asyncio
async def test_sub_job_OMPE_83813(wrapp_init, local_tmp_folder):
    """SchedulerContext.sub_job() groups operations under a named sub-job in the progress report."""
    test_file = os.path.join(local_tmp_folder, "sample.txt")
    with open(test_file, "w") as f:
        f.write("test content")

    progress_reports: list[ProgressReport] = []

    params = wrapp.SchedulingParameters(
        progress_report=wrapp.ProgressReportVerbosity.verbose,
        progress_report_callback=lambda pr: progress_reports.append(pr),
        progress_report_interval=0,
    )
    async with wrapp.WrappScheduler(params=params) as scheduler:
        await wrapp.catalog(root_dir=local_tmp_folder, scheduler=scheduler.sub_job("CatalogJob"))

    assert len(progress_reports) >= 1
    final_pr = progress_reports[-1]
    assert final_pr.job_name() == "WRAPP"
    catalog_job = final_pr.sub_jobs()[0]
    assert catalog_job.job_name() == "CatalogJob"
