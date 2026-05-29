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
from typing import (
    List,
    Optional,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures import SchedulerContext
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    CommandParameters,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.package import (
    UniquePackageLocation,
    read_package_info,
    repository_from_wrapp_file_path,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    DryRunFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.repo.repository import Repository
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.storage import (
    wrapp_delete_folder_tree,
    wrapp_read_file_content,
    wrapp_stat,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="delete-packages")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def delete_packages_command(
    package: Optional[List[str]] = typer.Argument(
        None,
        help="URL(s) of package file(s) inside a repository which will be deleted.",
    ),
    package_list: List[str] = typer.Option(
        None,
        help="File containing what is to be deleted, containing a list of package file URLs pointing to wrapp files in a repository, one per line.",
    ),
    auth: Optional[List[str]] = AuthOption,
    dry_run: bool = DryRunFlag,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    stats: bool = StatsFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Given a list of packages via their package file URLs, delete the packages from the repository. Only works for package files with a .packages repository structure.
    This will break any packages which use the deleted packages as a dependency and should only be done with great care.
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            packages_to_delete: list[UniquePackageLocation] = []
            async with scheduler.root_task_node().create_task_group_node() as task_group:
                if package:
                    for p in package:
                        task_group.create_task(unique_package_location_from_wrapp_file(p, packages_to_delete), name="Validating input file")
                if package_list:
                    for input_file in package_list:
                        r, content = await wrapp_read_file_content(input_file)
                        if r == Result.OK:
                            for line in content.splitlines():
                                task_group.create_task(
                                    unique_package_location_from_wrapp_file(line.decode("UTF-8"), packages_to_delete),
                                    name="Validating input file",
                                )
                        else:
                            raise FailedCommand(f"Could not open input file {input_file}: {r}")

            if packages_to_delete:
                await delete_packages_from_repo(packages_to_delete, context=context, scheduler=scheduler)
            else:
                raise FailedCommand(
                    "No packages to delete specified. Please specify packages to delete either as parameters to the delete-packages command, or supply files with one package file URL per line via the --package-list parameter."
                )

    except FailedCommand as e:
        logger.error(f"Failure during delete-packages: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while running delete-packages: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def delete_packages_from_repo(
    to_delete: list[UniquePackageLocation],
    *,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
):
    """Given a list of package versions and their repository, run a parallel delete tree operation to remove the packages and their files from storage."""
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    task_results: dict[asyncio.Task, Result] = {}
    async with scheduler.parent_task_node().create_task_group_node(f"delete {len(to_delete)} packages", task_results) as task_node:
        for package_to_delete in to_delete:
            repo = Repository(repo_url=package_to_delete.repo_url)
            package_url = repo.package_url(package_to_delete.package_name, package_to_delete.version)
            task_node.create_task(
                wrapp_delete_folder_tree(context or CommandParameters(), package_url, task_node),
                name=f"Deleting file tree '{package_url}'",
            )

    # Check that deletion worked
    error_exit = False
    for task, result in task_results.items():
        if result != Result.OK:
            error_exit = True
            logger.error(f"Failed: {task.get_name()}")
    if error_exit:
        raise FailedCommand("Failed to purge one or more items, see log for details!")


async def unique_package_location_from_wrapp_file(package_wrapp_file: str, result_list: List[UniquePackageLocation]):
    package_repo = repository_from_wrapp_file_path(package_wrapp_file)
    if package_repo is not None:
        package_stat_result, package_item = await wrapp_stat(package_wrapp_file)
        if package_stat_result == Result.ERROR_NOT_FOUND:
            raise FailedCommand(f"The given input package {package_wrapp_file} does not exist")
        package_info = await read_package_info(package_wrapp_file)
        if package_info is None:
            raise FailedCommand(f"The given input package {package_wrapp_file} cannot be read.")
        result_list.append(UniquePackageLocation(package_name=package_info.name, version=package_info.version, repo_url=package_repo))
    else:
        raise FailedCommand(f"The input must be packages inside a repository, not inside a work tree: '{package_wrapp_file}'")
