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

import csv
import io
import logging
from typing import (
    List,
    Optional,
)

import rich
import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.dependency_graph import (
    DependencyGraph,
)
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    Dependency,
    DependencySet,
    PackageInfo,
    normalize_install_path_from_user_input,
    validate_package_and_version,
    wrapp_file_in_repo,
    write_package_info,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
    RepoRequiredOption,
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
    wrapp_read_text_file,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="create-metapackage")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def create_metapackage_command(
    ctx: typer.Context,
    package_name: str = typer.Argument(None, help="Specify the name of the package to create"),
    version: str = typer.Argument(None, help="Specify the version of the package [any string]"),
    repo: str = RepoRequiredOption,
    json: str = typer.Option(None, "--json", help="Specify the path of the json DependencySet file."),
    csv: str = typer.Option(None, "--csv", help="Specify the path of a CSV file with 4 named columns: name, version, path, repo."),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    stats: bool = StatsFlag,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,
) -> None:
    """
    The create-metapackage command creates a package that contains only dependencies to other packages, and contains no
    files on its own.
    """
    context, scheduler_params = context_from_params()
    try:
        dependencies = DependencySet(dependencies=[])  # mypy empty
        if not json and not csv:
            raise FailedCommand("You must specify the metapackage content either with a JSON file or a CSV file parameter!")
        if json and csv:
            raise FailedCommand("Please specify either a JSON file to load or a CSV file, not both!")
        if json:
            if not await wrapp_url_exists(json):
                raise FailedCommand(f"JSON file {json} not found!")
            dependencies = await DependencySet.load(json)
        if csv:
            if not await wrapp_url_exists(csv):
                raise FailedCommand(f"CSV file {csv} not found!")
            csv_content = await wrapp_read_text_file(csv)
            dependencies = _load_csv(csv_content)
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await create_metapackage(package_name, version, repo, dependencies, context=context, scheduler=scheduler)
    except FailedCommand as e:
        rich.print(ctx.get_usage() + "\n")
        logger.error(f"Failed running create-metapackage: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while running create-metapackage for package '{package_name}' version '{version}', operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


def _load_csv(csv_content) -> DependencySet:
    required_columns = {"name", "version", "path", "repo"}
    data: list[Dependency] = []

    # Define a custom CSV dialect
    csv.register_dialect("custom", skipinitialspace=True, quoting=csv.QUOTE_ALL, strict=True)

    # Use StringIO to simulate a file object
    csvfile = io.StringIO(csv_content)
    reader = csv.DictReader(csvfile, dialect="custom")

    # Check if the columns match exactly with the required columns
    if not reader.fieldnames:
        raise FailedCommand(f"CSV files contains no columns. Required column names are: {','.join(required_columns)}")
    if set(reader.fieldnames) != set(required_columns):
        raise FailedCommand(
            f"CSV content must contain exactly the following columns: {','.join(required_columns)}, but found {reader.fieldnames}"
        )

    # Process rows and build list of 4-tuples
    for row in reader:
        data.append(Dependency(package=row["name"], version=row["version"], relative_destination=row["path"], repository=row["repo"]))

    return DependencySet(dependencies=data)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def create_metapackage(
    package_name: str,
    version: str,
    repo: str,
    content: DependencySet,
    *,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Creates a metapackage containing only dependencies to other packages, with no files of its own.

    :param package_name: Name of the package to create
    :param version:  Version of the package to create, can be any string identifier
    :param repo: URL of the repository where to create the package
    :param content: DependencySet containing the dependencies that should become part of the metapackage
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    if context is None:
        context = CommandParameters()

    validate_package_and_version(package_name, version)
    repo_object = Repository(repo_url=repo)
    wrapp_file = wrapp_file_in_repo(repo_object, package_name, version)
    target_base_url = repo_object.package_url(package_name, version)

    # Validate the install path (we only need to check the install path, existence of the dependent packages is checked later)
    for dependency in content.dependencies:
        # This should be moved to the Dependency class itself? And the Dependency class should also validate package name and version?
        try:
            dependency.relative_destination = normalize_install_path_from_user_input(dependency.relative_destination)
        except FailedCommand as e:
            raise FailedCommand(
                f"The install path '{dependency.relative_destination}' for package '{dependency.package}' version '{dependency.version}' from repo '{dependency.repository}' is not a valid install path: {e}"
            ) from e

    # Create a new package from the given list of dependencies
    metapackage = PackageInfo(
        name=package_name, version=version, catalog=Catalog(root=target_base_url, items=[]), dependencies=content.dependencies
    )
    metapackage_info = LoadedPackage(wrapp_file, metapackage)

    # We need to check the dependency graph constructed is actually valid (no cycles, no version conflicts)
    async with scheduler.parent_task_node().create_task_group_node(f"Creating metapackage '{package_name}' '{version}'") as task_node:
        opened_dependency_files: list[str] = []
        # This will raise a FailedCommand in case the dependency graph has a version conflict, or if a dependency file
        # cannot be found.
        await DependencyGraph.build_dependency_graph(
            context=context, task_node=task_node, named_package_setup=metapackage_info, opened_dependency_files=opened_dependency_files
        )

    # Check - the package to be created does not exist already?
    if await wrapp_url_exists(wrapp_file):
        raise FailedCommand(f"package already exists, can't overwrite existing package {wrapp_file}")
    if await wrapp_url_exists(target_base_url):
        raise FailedCommand(
            f"target directory already exists, repository seems to be dirty: Please remove before creating: {target_base_url}"
        )

    # Now, write our new package
    metapackage.repository = repo_object.base_url()
    await write_package_info(wrapp_file, metapackage, context)

    if context.verbose:
        logger.info(f"Created package file at {wrapp_file}")
