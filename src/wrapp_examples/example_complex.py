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
import logging
import os
import sys

import omni.client
from rich.live import Live
from rich.logging import RichHandler
from rich.table import (
    Column,
    Table,
)

import wrapp.api
from wrapp import (
    CommandParameters,
    ProgressReport,
    ProgressReportVerbosity,
    SchedulerContext,
    SchedulingParameters,
    ThrottlingParameters,
)
from wrapp.utils.progress_report import ProgressReportStatus

from .create_new_version import create_new_version


# Setup rich live display
def setup_live_display() -> Live:
    # Lazy creation to make the redirect of logging work
    _live_display = Live()
    rich_handler = RichHandler()
    rich_handler.setLevel(logging.getLogger().level)
    formatter = logging.Formatter("%(message)s")
    rich_handler.setFormatter(formatter)
    logging.getLogger().handlers = [
        rich_handler,
        *filter(lambda x: isinstance(x, logging.FileHandler), logging.getLogger().handlers),
    ]
    _live_display.start()
    return _live_display


_live_display = setup_live_display()


def print_progress(progress_report: ProgressReport):
    """
    Example function handling the progress report output.
    """
    progress_table = Table(
        "Progress  ",
        Column("Name", overflow="fold", ratio=1),
        "Discovered",
        "Running   ",
        "Done      ",
        expand=True,
    )

    def add_rows(job: ProgressReport, indent: int = 0):
        if job.status() == ProgressReportStatus.Running:
            progress_str = f"{(job.progress()) * 100:.0f}%"
        else:
            progress_str = job.status().value

        progress_table.add_row(
            progress_str,
            (" " * indent) + job.job_name(),
            str(job.total_num_tasks_executing() + job.total_num_tasks_done()),
            str(job.total_num_tasks_executing() - job.total_num_tasks_waiting()),
            str(job.total_num_tasks_done()),
        )
        if indent < 3:
            for sub_job in job.sub_jobs():
                add_rows(sub_job, indent + 1)

    # Skip the main root node called "WRAPP"
    for j in progress_report.sub_jobs():
        add_rows(j)
    _live_display.update(progress_table, refresh=True)


def main():
    """
    Example main function that shows how to use the WRAPP commands in a larger application using the initialize() and shutdown() pattern
    """
    # First, initialize the wrapp API which will also initialize the omni.client library
    wrapp.api.initialize(throttling_params=ThrottlingParameters(file_transfer_jobs=1))
    try:
        # Setup authentication for subsequent calls
        auth = wrapp.AuthManager(interactive_fallback=False)
        auth.initialize()
        try:
            # Read credentials from environment variables, supplying test defaults
            auth_info = wrapp.AuthInfo(
                server=os.getenv("HOST_URL", "omniverse://localhost"),
                username=os.getenv("OMNI_USER", "omniverse"),
                password_or_token=os.getenv("OMNI_PASS", "omniverse"),
            )
            auth.add_auth(auth_info)

            # To make sure we're not using cached credentials, force a sign-out and reconnect to the server
            omni.client.sign_out(auth_info.server)
            omni.client.reconnect(auth_info.server)

            # Define common parameters for all subsequent commands
            command_params = CommandParameters(verbose=True)

            # Define test data location
            root_url = f"{auth_info.server}/Projects/wrapp_examples"

            async def async_main():
                scheduler_context = SchedulerContext(
                    SchedulingParameters(progress_report_callback=print_progress, progress_report=ProgressReportVerbosity.normal)
                )
                scheduler_context.start_scheduler()
                try:
                    tasks = []
                    tasks.append(
                        asyncio.create_task(
                            create_new_version(
                                root_url=root_url,
                                package_name="all_projects",
                                repository=f"{auth_info.server}",
                                scheduler=scheduler_context.sub_job("Creating sample project"),
                                context=command_params,
                            )
                        )
                    )
                    results = await asyncio.gather(*tasks)
                finally:
                    await scheduler_context.stop_scheduler()

            asyncio.run(async_main())
        finally:
            # Deregister the AuthManager
            auth.shutdown()
    except wrapp.FailedCommand as e:
        print(f"Could not create package, error message by wrapp is: '{e}'")
    finally:
        # Shutdown the WRAPP API
        wrapp.api.shutdown(*sys.exc_info())


if __name__ == "__main__":
    main()
