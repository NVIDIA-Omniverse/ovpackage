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

import logging
from queue import Queue
from typing import (
    List,
    Optional,
    Tuple,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    InteractiveAuthFlag,
    JobsNumber,
    LogFileOption,
    ProgressReportOptionOff,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.storage import (
    wrapp_list_directory,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    is_folder,
    join_paths,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="list-repo")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def list_repo_command(
    repository: str = typer.Argument(..., help="repository to list packages (URL without the .packages folder)"),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOptionOff,
    verbose: bool = VerboseFlag,
    debug: bool = DebugLevelFlag,
    jobs: int = JobsNumber,
    stats: bool = StatsFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    List all packages and all versions that are stored in the specified repository. Those are the candidates for a subsequent install
    """
    context, scheduling_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduling_params) as scheduler:
            versions = await list_repo(repository, scheduler=scheduler)
            for version in versions:
                print(f"{version[0]}: {', '.join(version[1])}")
    except FailedCommand as e:
        logger.error(f"Failure to list '{repository}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while listing '{repository}': {e}, operation incomplete.")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def list_repo(repository: str, *, scheduler: Optional[SchedulerContext] = None) -> List[Tuple[str, List[str]]]:
    """
    Takes a repository URL and returns a list all packages and all versions that are stored there

    :param repository: URL of a repository, excluding the .packages directory name
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :return: List of tuples consisting of a package name and a list of version strings that package is available in

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("repository", repository)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    packages_dir = join_paths(repository, ".packages")
    if not await wrapp_url_exists(packages_dir):
        raise FailedCommand("No .packages folder")

    versions: Queue = Queue()
    async with scheduler.parent_task_node().create_task_group_node(f"List repo '{repository}'") as task_group:
        try:
            logger.info(f"Listing {packages_dir}")
            async for item in wrapp_list_directory(packages_dir):
                if is_folder(item):
                    version_dir = join_paths(packages_dir, item.relative_path)
                    task_group.create_task(_list_versions(version_dir, versions), name="List package versions")
                else:
                    logger.warning(f"extra file in .packages directory: {item.relative_path}, ignoring it")
        except StorageOperationError as e:
            raise StorageOperationError(f"Failed to list package directory at {packages_dir}", e.result)

    version_list = sorted(list(versions.queue), key=lambda v: v[0])
    return version_list


async def _list_versions(version_dir: str, results: Queue):
    try:
        logger.info(f"Listing {version_dir}")
        result_list = []
        async for version in wrapp_list_directory(version_dir):
            if is_folder(version):
                result_list.append(version.relative_path.rstrip("/"))
            else:
                logger.warning(f"extra file in {version_dir}: {version.relative_path}, ignoring it")
        results.put((version_dir.rstrip("/"), result_list))
    except StorageOperationError as e:
        raise StorageOperationError(f"Failed to list package version directory at {version_dir}", e.result)
