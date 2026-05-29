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
from typing import (
    List,
    Optional,
)

import typer
from rich.console import Console

from wrapp.utils.api_marker import wrapp_public_api

from ..app import (
    app,
    measure_time,
    print_stats,
)
from ..datastructures.authentication import capture_auth
from ..datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from ..datastructures.dependency_graph import collect_dependencies_in_tree
from ..datastructures.installed_package import InstalledPackage
from ..datastructures.loaded_package import LoadedPackage
from ..options import (
    AuthOption,
    DebugLevelFlag,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    LsOutputFormat,
    ProgressReportOption,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from ..utils.cli_progress_printer import ProgressReportVerbosity
from ..utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from ..utils.ensure_scheduler import ensure_scheduler
from ..utils.logging import log_args
from ..utils.utils import (
    FailedCommand,
    StorageOperationError,
)
from ..utils.wrapp_init_for_cmd import wrapp_init_for_cmd

logger = logging.getLogger("omni.wrapp")


@app.command(name="installed-package-list")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def installed_package_list_command(
    wrapp_file: str,
    output_format: LsOutputFormat = typer.Option(
        LsOutputFormat.normal,
        "--output-format",
        "--of",
        case_sensitive=False,
        help="Specify the output format of the command",
    ),
    direct_only: bool = typer.Option(False, "--direct-only", help="Set this to true to report direct dependencies only."),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    stats: bool = StatsFlag,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,
):
    """
    Outputs the list of all packages within one top level package. Recursively load all wrapp files and print package name, version,
    install location, source repository, and number of file items. TSV export adds a column with the full wrapp file URL.
    """

    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            packages = await installed_package_list(wrapp_file, direct_only=direct_only, context=context, scheduler=scheduler)
        console = Console()
        if output_format == LsOutputFormat.normal:
            console.print(
                "Name",
                "Version",
                "Install path",
                "Repository",
                "Num items",
                sep="\t",
                soft_wrap=True,
            )
        for package in packages:
            num_items = len(package.package.catalog.items) if package.package.catalog else 0
            if output_format == LsOutputFormat.TSV:
                print(
                    f"{package.name}\t{package.version}\t{package.wrapp_file_url}\t{package.install_location}\t{package.repository}\t{num_items}"
                )
            elif output_format == LsOutputFormat.short:
                print(package.wrapp_file_url)
            elif output_format == LsOutputFormat.normal:
                console.print(
                    package.name,
                    package.version,
                    package.install_location,
                    package.repository,
                    num_items,
                    sep="\t",
                    soft_wrap=True,
                )
            else:
                raise Exception(f"Invalid argument to installed-package-list: {output_format} format is unknown")
    except FailedCommand as e:
        logger.error(f"Failure to list subpackages for '{wrapp_file}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Storage error during listing of subpackages for '{wrapp_file}': {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def installed_package_list(
    wrapp_file: str,
    *,
    direct_only: bool = False,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> List[InstalledPackage]:
    """
    Given a top level wrapp file, outputs the list of all packages within one top level package.

    Recursively load all wrapp files and print package name, version, wrapp file url, install location, source repository, and number of file items.

    :param wrapp_file: URL of the top level wrapp file to analyze.
    :param direct_only: Return only direct dependencies of the top level package, not the full recursive tree.
    :param context: Optionally, global configuration parameters.
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :return: List of tuples consisting of a package name and a list of version strings that package is available in

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    scheduler = scheduler.create_sub_context_and_node(f"Finding installed packages for '{wrapp_file}'", expected_num_sub_jobs=1)
    package = await LoadedPackage.get_from_url(wrapp_file)
    if not package:
        raise FailedCommand(f"Cannot load package file '{wrapp_file}'")
    else:
        package_info, sub_package_list, direct_dependency_list = await collect_dependencies_in_tree(
            context,
            package,
            package.install_base,
            scheduler.parent_task_node().create_task_group_node(
                f"Collecting subpackages of '{package.package.name}' '{package.package.version}'"
            ),
        )

        result = []
        if direct_only:
            packages_found = direct_dependency_list
        else:
            packages_found = sub_package_list
        for sub_package in packages_found:
            result.append(
                InstalledPackage(
                    name=sub_package.package.name,
                    version=sub_package.package.version,
                    wrapp_file_url=sub_package.absolute_path,
                    install_location=sub_package.relative_path,
                    repository=sub_package.package.repository,
                    package=sub_package.package,
                )
            )
        return result
