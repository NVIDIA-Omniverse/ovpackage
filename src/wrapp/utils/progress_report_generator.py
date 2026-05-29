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
import logging
import traceback
from typing import (
    NamedTuple,
    Optional,
    Self,
)

from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    sync_force_wrapp_thread,
)
from wrapp.utils.progress_report import (
    ProgressReport,
    ProgressReportCallback,
    ProgressReportStatus,
)
from wrapp.utils.router import Router
from wrapp.utils.scheduler_task_group import (
    SchedulerTaskGroup,
    TaskGroupState,
)
from wrapp.utils.scheduler_task_node import (
    SchedulerRootNode,
    SchedulerTreeNode,
)
from wrapp.utils.task_throttler import (
    CPUThrottler,
    TaskThrottler,
)


class _ProgressReportSnapshot(NamedTuple):
    status: ProgressReportStatus
    executing: int
    done: int
    waiting: int
    processing: int


class ProgressReportGenerator:
    def __init__(
        self,
        root_task_node: SchedulerRootNode,
        progress_report_verbosity: ProgressReportVerbosity,
        interval_in_secs: Optional[float] = None,
    ):
        self._progress_report_verbosity: ProgressReportVerbosity = progress_report_verbosity
        self._report_progress_task: Optional[asyncio.Task] = None
        self._root_task_node = root_task_node
        self._interval_in_secs = interval_in_secs if interval_in_secs is not None else 0.2
        self._progress_report_cb: Optional[ProgressReportCallback] = None
        self._num_tasks_previous_report = _ProgressReportSnapshot(ProgressReportStatus.Finished, 0, 0, 0, 0)

    def start_progress_report(self, progress_report_cb: ProgressReportCallback):
        if self._progress_report_verbosity != ProgressReportVerbosity.off:
            self._progress_report_cb = progress_report_cb
            self._report_progress_task = asyncio.create_task(self._report_progress())

    # the function is deliberately not static - this function needs to be run before the Scheduling object is destroyed
    async def shut_down(self: Self):
        if self._report_progress_task:
            self._report_progress_task.cancel()
            # Generate one final report
            if self._progress_report_cb is not None:
                final_report = self.generate_job_report(self._root_task_node)
                # At shutdown an empty root means "nothing to do", not "waiting for work".
                if not self._root_task_node._children and final_report.status() == ProgressReportStatus.Running:
                    final_report._status = ProgressReportStatus.Finished
                self._progress_report_cb(final_report)

    async def _report_progress(self):
        while True:
            try:
                progress_report_data = self.generate_job_report(self._root_task_node)
                total_num_tasks = _ProgressReportSnapshot(
                    status=progress_report_data.status(),
                    executing=progress_report_data.total_num_tasks_executing(),
                    done=progress_report_data.total_num_tasks_done(),
                    waiting=progress_report_data.total_num_tasks_waiting(),
                    processing=progress_report_data.total_num_tasks_processing(),
                )
                if total_num_tasks != self._num_tasks_previous_report:
                    self._progress_report_cb(progress_report_data)
                    self._num_tasks_previous_report = total_num_tasks
            except Exception:
                logging.exception(f"Exception during progress report generation: {traceback.format_exc()}")
                raise
            await asyncio.sleep(self._interval_in_secs)

    def _all_throttlers(self) -> list[TaskThrottler]:
        throttlers: list[TaskThrottler] = [CPUThrottler.me().throttler()]
        throttlers.extend(Router.me().all_backend_throttlers())
        return throttlers

    @sync_force_wrapp_thread
    def generate_job_report(self, root_node: SchedulerRootNode) -> ProgressReport:
        class _TaskNodeTaskCounts:
            def __init__(self):
                self.executing = 0
                self.done = 0

        class _ThrottlerTaskCounts:
            def __init__(self):
                self.waiting = 0
                self.processing = 0

        pending_tasks: dict[Optional[asyncio.Task], set[TaskThrottler]] = {}
        processing_tasks: dict[Optional[asyncio.Task], set[TaskThrottler]] = {}

        for task_throttler in self._all_throttlers():
            for t in task_throttler.pending_tasks:
                pending_tasks.setdefault(t, set()).add(task_throttler)
            for t in task_throttler.processing_tasks:
                processing_tasks.setdefault(t, set()).add(task_throttler)

        def _generate_node_report(s: SchedulerTreeNode) -> ProgressReport:
            # Count tasks from this node's task group (if any).
            # The life-time of one asyncio.Task:
            # 1. Created using SchedulerTaskGroup.create_task (executing)
            # 2. Start execution, including yielding control through "await" (executing)
            # 3. Being throttled by a throttler, waiting within "async with semaphore" (throttled)
            # 4. Passed "async with semaphore", waiting for throttled operation (processing)
            # 5. Finished "await" for throttled operation. Back to 2. (executing)
            # 6. Done
            throttler_task_counts: dict[TaskThrottler, _ThrottlerTaskCounts] = {}
            task_group_task_counts = _TaskNodeTaskCounts()

            tg: Optional[SchedulerTaskGroup] = s._associated_task_group()
            if tg is not None:
                for task_in_scheduler_group in tg.task_set():
                    if task_in_scheduler_group.done():
                        task_group_task_counts.done += 1
                    else:
                        task_group_task_counts.executing += 1
                        pending_throttler_set = pending_tasks.get(task_in_scheduler_group, None)
                        if pending_throttler_set:
                            for pending_throttler in pending_throttler_set:
                                throttler_task_counts.setdefault(pending_throttler, _ThrottlerTaskCounts()).waiting += 1
                        else:
                            processing_throttler_set = processing_tasks.get(task_in_scheduler_group, None)
                            if processing_throttler_set:
                                for processing_throttler in processing_throttler_set:
                                    throttler_task_counts.setdefault(processing_throttler, _ThrottlerTaskCounts()).processing += 1

            report = ProgressReport(
                s.description(), task_group_task_counts.executing, task_group_task_counts.done, s.expected_num_sub_jobs()
            )

            for throttler, tasks_counts in throttler_task_counts.items():
                report.add_throttled_data(throttler.description(), tasks_counts.waiting, tasks_counts.processing)

            # Determine this node's direct task status.
            if tg is None:
                job_status_direct_tasks = ProgressReportStatus.Finished
            elif tg.done():
                job_status_direct_tasks = (
                    ProgressReportStatus.Finished if tg.state() == TaskGroupState.Completed else ProgressReportStatus.Failed
                )
            else:
                job_status_direct_tasks = ProgressReportStatus.Running

            # Recurse into children.
            sub_job_reports = [_generate_node_report(sg) for sg in s._children]

            report.finalize(job_status_direct_tasks, sub_job_reports)
            return report

        # The root node has no direct tasks — only children.
        progress_report = ProgressReport(root_node.description(), 0, 0, root_node.expected_num_sub_jobs())

        if not root_node._children:
            # No jobs registered yet — report as Running so an empty ProgressReport at the start does not show "Finished".
            progress_report.finalize(ProgressReportStatus.Running, [])
            return progress_report

        sub_job_reports = [_generate_node_report(sg) for sg in root_node._children]
        progress_report.finalize(ProgressReportStatus.Finished, sub_job_reports)
        return progress_report
