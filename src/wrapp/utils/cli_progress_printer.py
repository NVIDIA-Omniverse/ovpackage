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

import dataclasses
import logging
from enum import Enum
from typing import (
    Optional,
    Self,
)

from rich.live import Live
from rich.logging import RichHandler
from rich.style import Style
from rich.table import (
    Column,
    Table,
)

from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.progress_report import (
    ProgressReport,
    ProgressReportStatus,
)


@wrapp_public_api
class ProgressReportVerbosity(str, Enum):
    """
    The amount of detail wrapps progress report prints out.

    Attributes:
        off: no progress is printed.
        normal: an overview of the progress of the top-level tasks is given.
        verbose: all scheduled jobs are listed.
    """

    off = "off"
    normal = "normal"
    verbose = "verbose"

    def __str__(self):
        return self.value


class CLIProgressPrinter:
    _live_display: Optional[Live] = None
    _disable_progress_report_for_tests: bool = False

    def __init__(self, progress_report: ProgressReportVerbosity):
        self._verbosity: ProgressReportVerbosity = progress_report

    @classmethod
    def disable_progress_output(cls):
        cls._disable_progress_report_for_tests = True

    def live_display(self) -> Live:
        if not self.__class__._live_display:
            # Lazy creation to make the redirect of logging work
            self.__class__._live_display = Live()
            if self._verbosity != ProgressReportVerbosity.off and not self._disable_progress_report_for_tests:
                rich_handler = RichHandler()
                rich_handler.setLevel(logging.getLogger().level)
                formatter = logging.Formatter("%(message)s")
                rich_handler.setFormatter(formatter)
                logging.getLogger().handlers = [
                    rich_handler,
                    *filter(lambda x: isinstance(x, logging.FileHandler), logging.getLogger().handlers),
                ]
        return self.__class__._live_display

    def start_progress_report(self):
        if self._verbosity != ProgressReportVerbosity.off and not self._disable_progress_report_for_tests:
            self.live_display().start(False)

    # the function is deliberately not static - this function needs to be run before the Scheduling object is destroyed
    async def shut_down(self: Self):
        if self._verbosity != ProgressReportVerbosity.off and not self._disable_progress_report_for_tests:
            self.live_display().stop()

    def print_progress(self, progress_report: ProgressReport):
        if self._verbosity == ProgressReportVerbosity.off:
            return

        @dataclasses.dataclass
        class _TaskNodeTaskCounts:
            progress: float = 0.0
            executing: int = 0
            throttled: int = 0
            processing: int = 0
            done: int = 0
            status: ProgressReportStatus = ProgressReportStatus.Running

            def finished(self) -> bool:
                return self.progress == 1.0 and self.status != ProgressReportStatus.Running

        @dataclasses.dataclass
        class _ThrottledResourceCounts:
            throttled: int = 0
            processing: int = 0

        @dataclasses.dataclass
        class ProgressRow:
            indent: int = 0
            description: str = ""
            task_counts: _TaskNodeTaskCounts | _ThrottledResourceCounts = dataclasses.field(default_factory=_TaskNodeTaskCounts)

        def _generate_scheduler_task_group_progress_rows(job: ProgressReport, indent=0) -> list[ProgressRow]:
            progress_table_rows: list[ProgressRow] = [
                ProgressRow(
                    indent,
                    job.job_name(),
                    _TaskNodeTaskCounts(
                        progress=job.progress(),
                        executing=job.total_num_tasks_executing(),
                        throttled=job.total_num_tasks_waiting(),
                        processing=job.total_num_tasks_processing(),
                        done=job.total_num_tasks_done(),
                        status=job.status(),
                    ),
                )
            ]
            if self._verbosity == ProgressReportVerbosity.verbose:
                for resource_name in job.throttled_resource_names():
                    progress_table_rows.append(
                        ProgressRow(
                            indent + 1,
                            resource_name,
                            _ThrottledResourceCounts(
                                job.total_num_tasks_waiting_for_resource(resource_name),
                                job.total_num_tasks_processing_for_resource(resource_name),
                            ),
                        )
                    )
            if (job.progress() < 1 or job.status() == ProgressReportStatus.Running) and (
                self._verbosity == ProgressReportVerbosity.verbose or indent < 2
            ):
                for sub_job in job.sub_jobs():
                    new_rows = _generate_scheduler_task_group_progress_rows(sub_job, indent + 1)
                    progress_table_rows.extend(new_rows)
            return progress_table_rows

        def _progress_row_sort_key(pr: ProgressRow):
            return (
                pr.indent,
                -(pr.task_counts.executing - pr.task_counts.throttled) if isinstance(pr.task_counts, _TaskNodeTaskCounts) else 0,
                -pr.task_counts.processing,
                -pr.task_counts.throttled,
            )

        progress_rows: list[ProgressRow] = []
        for sub_job in progress_report.sub_jobs():
            progress_rows.extend(_generate_scheduler_task_group_progress_rows(sub_job))

        if progress_rows:
            if self._verbosity == ProgressReportVerbosity.verbose:
                progress_table = Table(
                    "Progress  ",
                    Column("Name", overflow="fold", ratio=1),
                    "Discovered",
                    "Running   ",
                    "Waiting   ",
                    "Using jobs",
                    "Done      ",
                    expand=True,
                )
            else:
                progress_table = Table(
                    "Progress  ",
                    Column("Name", overflow="fold", ratio=1),
                    "Discovered",
                    "Running   ",
                    "Done      ",
                    expand=True,
                )

            in_progress_style = Style(bold=True)
            done_style = Style(dim=True)

            # TODO: make this number configurable? Must be smaller than some value though for things not to break
            max_num_displayed_rows = 10
            indices_to_be_displayed: set[int]
            if len(progress_rows) > max_num_displayed_rows:
                # Too many rows, find the top N rows which we want to display (i.e. the ones with the
                # most tasks running right now).
                progress_row_indices = range(len(progress_rows))
                sorted_progress_row_indices = sorted(
                    progress_row_indices,
                    key=lambda pr_index: _progress_row_sort_key(progress_rows[pr_index]),
                )
                # Only keep the max_num_displayed_rows most important tasks
                indices_to_be_displayed = set(sorted_progress_row_indices[:max_num_displayed_rows])
                # Find and add all parent rows to indices_to_be_displayed
                for index in sorted_progress_row_indices[:max_num_displayed_rows]:
                    if progress_rows[index].indent == 0:
                        # Keep the top rows in all cases
                        indices_to_be_displayed.add(index)
                        continue
                    # Find all ancestors of the row, and keep them, too
                    parent_indent = progress_rows[index].indent - 1
                    parent_index = index - 1
                    while parent_indent > 0 and parent_index >= 0:
                        # Skip backwards to the previous row with the parent_indent
                        while parent_index > 0 and progress_rows[parent_index].indent > parent_indent:
                            parent_index -= 1
                        indices_to_be_displayed.add(parent_index)
                        # Continue to a top-level row
                        parent_indent -= 1
            else:
                indices_to_be_displayed = set(range(len(progress_rows)))

            row_index_last_added = len(progress_rows)
            # True if any rows were skipped on the indent level of the last printed row.
            skipped_row_indent = -1
            for index in range(len(progress_rows)):
                pr = progress_rows[index]
                if index in indices_to_be_displayed:
                    if isinstance(pr.task_counts, _TaskNodeTaskCounts):
                        if not pr.task_counts.finished():
                            progress_str = f"{(pr.task_counts.progress) * 100:.0f}%"
                        else:
                            progress_str = pr.task_counts.status.value
                    else:
                        progress_str = ""

                    column_data = [
                        progress_str,
                        (" " * pr.indent) + pr.description,
                        str(pr.task_counts.executing + pr.task_counts.done) if isinstance(pr.task_counts, _TaskNodeTaskCounts) else "",
                        str(pr.task_counts.executing - pr.task_counts.throttled) if isinstance(pr.task_counts, _TaskNodeTaskCounts) else "",
                    ]
                    if self._verbosity == ProgressReportVerbosity.verbose:
                        column_data.extend([str(pr.task_counts.throttled), str(pr.task_counts.processing)])
                    column_data.append(str(pr.task_counts.done) if isinstance(pr.task_counts, _TaskNodeTaskCounts) else "")
                    progress_table.add_row(
                        *column_data,
                        style=in_progress_style
                        if (not isinstance(pr.task_counts, _TaskNodeTaskCounts)) or not pr.task_counts.finished()
                        else done_style,
                    )
                    row_index_last_added = index
                elif row_index_last_added < index - 1 and pr.indent == progress_rows[row_index_last_added].indent:
                    # Silently skip all rows which have a deeper indent than the last printed row.
                    skipped_row_indent = pr.indent
                if skipped_row_indent >= 0 and pr.indent < progress_rows[index].indent:
                    # We skipped rows in the indent level we are now leaving, add a "..."
                    progress_table.add_row("", " " * progress_rows[row_index_last_added].indent + "...")
                    # This nullifies any "..." on the levels lower than the current one. E.g. if we jump from indent 4 to 2 and we have skipped
                    # entries with indent 3 and 4 and 2, a "..." will cover all skipped entries on indent level 3 and 4, but not 2.
                    skipped_row_indent = -1
                    row_index_last_added = len(progress_rows)
            if skipped_row_indent >= 0:
                progress_table.add_row("", " " * progress_rows[row_index_last_added].indent + "...")

            self.live_display().update(progress_table, refresh=True)
        else:
            self.live_display().update("Starting...", refresh=True)
