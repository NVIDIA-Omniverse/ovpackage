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

from enum import (
    Enum,
)
from typing import (
    Callable,
    Iterable,
    Optional,
)

from wrapp.utils.api_marker import wrapp_public_api


class ProgressReportStatus(Enum):
    Running = "Running"
    Finished = "Finished"
    Failed = "Failed"
    Cancelling = "Cancelling"
    Cancelled = "Cancelled"


@wrapp_public_api
class ProgressReport:
    """
    Progress report for WRAPP operations (or "jobs") running in the background.

    To group WRAPP operations under a recognizable name, use :meth:`SchedulerContext.sub_job`,
    e.g. ``wrapp.list_repo(url, scheduler=scheduler.sub_job("list repo"))``

    Also specify a progress report callback when the SchedulerContext is set up. If you are using the ContextManager,
    you can specify the callback in its constructor.

    Once this is done and the progress report verbosity in SchedulerParams is not "off",
    the progress report callback will be called regularly with the progress report
    for the root job containing all other jobs to allow the application to update any progress indicators.

    Each progress report represents the state of a job, including its sub-jobs.
    A single root job report (named "WRAPP" by default, customizable via SchedulingParameters.job_name)
    serves as the top-level container for all other job reports.

    Jobs which are siblings in the hierarchy are executed concurrently. A job is completed when all its directly
    spawned tasks are completed, and all its children jobs are completed. On the lowest level a job is an asyncio.task.

    For a simple example how to transform the progress report into user-readable output, please check the example_complex.py.
    """

    def __init__(self, job_name: str, num_tasks_executing: int, num_tasks_done: int, expected_num_sub_jobs: Optional[int]):
        self._job_name = job_name
        self._status = ProgressReportStatus.Running
        self._num_tasks_executing = num_tasks_executing
        self._num_tasks_done = num_tasks_done
        self._expected_num_sub_jobs = expected_num_sub_jobs
        self._throttled_resource_data: dict[str, tuple[int, int]] = {}
        self._total_num_tasks_executing = 0
        self._total_num_tasks_done = 0
        self._sub_jobs: list[ProgressReport] = []
        self._total_throttled_resource_data: dict[str, tuple[int, int]] = {}

    def __repr__(self):
        return f"{self._job_name} {self._status}"

    def add_throttled_data(self, resource_name: str, num_waiting: int, num_processing: int):
        self._throttled_resource_data[resource_name] = (num_waiting, num_processing)

    # This will only work if finalize is first called on the leafs in the tree, before it is called on the parents.
    def finalize(self, job_status_direct_tasks: ProgressReportStatus, sub_jobs: list[ProgressReport]):
        self._sub_jobs = sub_jobs
        self._status = job_status_direct_tasks
        self._total_num_tasks_executing = self.num_tasks_executing()
        self._total_num_tasks_done = self.num_tasks_done()
        self._total_throttled_resource_data = self._throttled_resource_data.copy()

        for j in self.sub_jobs():
            if j.status() == ProgressReportStatus.Running:
                # Any running sub job will mark its parent as still running.
                self._status = ProgressReportStatus.Running
            elif j.status() == ProgressReportStatus.Finished:
                # A successfully completed sub job does nothing to the state of its parent job:
                # If it is still running, it keeps running. If it finished, failed, cancelling or cancelled, it remains so.
                pass
            elif j.status() in [ProgressReportStatus.Failed, ProgressReportStatus.Cancelling, ProgressReportStatus.Cancelled]:
                # A failed or cancelled sub job will mark its parent as failed, if it is marked as finished.
                if self._status == ProgressReportStatus.Finished:
                    self._status = ProgressReportStatus.Failed
                # In all other cases (Running, Failed, Cancelling, Cancelled) the parent remains unchanged by a failed/cancelled sub job.

            self._total_num_tasks_executing += j.total_num_tasks_executing()
            self._total_num_tasks_done += j.total_num_tasks_done()

            subjob_nums = j._total_throttled_resource_data
            for k, n in subjob_nums.items():
                total = self._total_throttled_resource_data.get(k, (0, 0))
                self._total_throttled_resource_data[k] = (total[0] + n[0], total[1] + n[1])

        if self._expected_num_sub_jobs and len(sub_jobs) < self._expected_num_sub_jobs:
            self._status = ProgressReportStatus.Running

    def job_name(self) -> str:
        """
        Name of the job, used to identify jobs. The name of the root job can be customized via
        the ``job_name`` parameter of :class:`SchedulingParameters`, or use
        :meth:`SchedulerContext.sub_job` to group operations under a named sub-job.
        The root job defaults to "WRAPP" if no custom name is provided.

        Job names might not be unique.
        """
        return self._job_name

    def status(self) -> ProgressReportStatus:
        """
        The status of the job. A job can be running without having any tasks or sub jobs spawned and running - then it is just
        the job being currently executed, blocking its parent job (unless it has been spawned within a task).
        """
        return self._status

    def progress(self) -> float:
        """
        A rough measure of progress for this job between 0 and 1 if the job status is "Running", otherwise return 1.
        Note that the number might go up as well as down if additional tasks are created during the execution of the jobs.

        Progress is distributed equally: each sub-job and this job's own tasks each receive 1/n weight.
        This prevents progress from jumping when only a fraction of total work has been discovered.
        """

        if self.status() != ProgressReportStatus.Running:
            return 1.0

        # We assume that each subjob has the same weighting. This offers a simple way to limit the amount of progress
        # one sub job can contribute to the total: if the code pre-generated sub jobs for each phase of a job,
        # each individual sub job can only contribute 1/N to the progress.
        total_num_jobs = max(len(self._sub_jobs), self._expected_num_sub_jobs or 0, 1)
        weight = 1.0 / total_num_jobs
        # It can happen that all tasks in this job and the sub jobs have been finished, but the job is not yet done,
        # because the task groups are not done yet. This is particularly obvious when there are no tasks at all
        # in a task group (e.g. when a task group was opened and contains a for loop which would generate tasks but
        # happens to do nothing).
        executing = self._num_tasks_executing
        done = self._num_tasks_done
        if executing == 0 and done == 0:
            if self.status() == ProgressReportStatus.Running:
                executing += 1  # also prevents division by 0
            else:
                done += 1  # prevent division by 0, also avoid the total task count going down
        overall_progress = weight * done / (done + executing)
        for j in self.sub_jobs():
            overall_progress += weight * j.progress()

        # Hacky solution to avoid 101% in progress report
        return max(min(overall_progress, 1.0), 0.0)

    def sub_jobs(self) -> list[ProgressReport]:
        """All sub jobs running as part of this job."""
        return self._sub_jobs

    def num_tasks_executing(self) -> int:
        """Number of tasks spawned by this job directly which are currently executing."""
        return self._num_tasks_executing

    def num_tasks_done(self) -> int:
        """Number of tasks spawned by this job which are completed (this includes failed tasks)."""
        return self._num_tasks_done

    def total_num_tasks_executing(self) -> int:
        """Number of tasks spawned by this job or its sub jobs which are currently executing."""
        return self._total_num_tasks_executing

    def total_num_tasks_done(self) -> int:
        """Number of tasks spawned by this job or its sub jobs which are completed (this includes failed tasks)."""
        return self._total_num_tasks_done

    def total_num_tasks_waiting(self) -> int:
        """The number of tasks spawned by this job or its sub jobs waiting for one of the limited resources to become available."""
        return sum([n[0] for n in self._total_throttled_resource_data.values()])

    def total_num_tasks_processing(self) -> int:
        """The number of tasks spawned by this job or its sub jobs being executed and making use of one of the limited resources."""
        return sum([n[1] for n in self._total_throttled_resource_data.values()])

    def throttled_resource_names(self) -> Iterable[str]:
        """
        The names of all resources limiting concurrent execution of tasks spawned directly by this job.
        The name of a resource is given by WRAPP and user readable, examples are "nucleus.example.com (max requests)",
        "S3:my_bucket (max data transfers)", "CPU threads".
        """
        return self._throttled_resource_data.keys()

    def num_tasks_waiting(self, resource_name: str) -> int:
        """
        The number of tasks spawned directly by this job waiting for the limited resources with the given name to become available.
        Raises KeyError if the resource name is not part of the list returned by throttled_resource_names.
        """
        return self._throttled_resource_data[resource_name][0]

    def num_tasks_processing(self, resource_name: str) -> int:
        """
        The number of tasks spawned directly by this job being executed and making use of the limited resource with the given name.
        Raises KeyError if the resource name is not part of the list returned by throttled_resource_names.
        """
        return self._throttled_resource_data[resource_name][1]

    def total_throttled_resource_names(self) -> Iterable[str]:
        """The names of all resources limiting concurrent execution of tasks spawned by this job or its sub jobs"""
        return self._total_throttled_resource_data.keys()

    def total_num_tasks_waiting_for_resource(self, resource_name: str) -> int:
        """
        The number of tasks spawned by this job or its sub jobs waiting for the limited resources with the given name to become available.
        Raises KeyError if the resource name is not part of the list returned by throttled_resource_names.
        """
        return self._total_throttled_resource_data[resource_name][0]

    def total_num_tasks_processing_for_resource(self, resource_name: str) -> int:
        """
        The number of tasks spawned by this job or its sub jobs being executed and making use of the limited resource with the given name.
        Raises KeyError if the resource name is not part of the list returned by throttled_resource_names.
        """
        return self._total_throttled_resource_data[resource_name][1]


ProgressReportCallback = Callable[[ProgressReport], None]
