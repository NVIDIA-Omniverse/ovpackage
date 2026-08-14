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

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.catalog import catalog
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.loaded_package import (
    LoadedPackage,
    _recalculate_catalog_resource_identities,
)
from wrapp.datastructures.package import (
    PackageInfo,
    load_package_setup,
    wrapp_file_in_repo,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    OverwriteFlag,
    ProgressReportOption,
    RecalculateResourceIdentitiesFlag,
    RecursiveOption,
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
    Result,
    wrapp_stat,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    is_folder,
    make_file_url_absolute,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="extract-catalog")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def extract_catalog_command(
    package: str = typer.Argument(..., help="package name"),
    version: str = typer.Argument(..., help="version"),
    catalog: str = typer.Argument(..., help="Catalog file to write to"),
    recursive: bool = RecursiveOption,
    repo: str = RepoRequiredOption,
    overwrite: bool = OverwriteFlag,
    recalculate_identities: bool = RecalculateResourceIdentitiesFlag,
    auth: Optional[List[str]] = AuthOption,
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
    Given a package name and a version plus a repository location, extract the catalog and write it into the given
    catalog file, e.g. for use by create_patch.
    """
    context, scheduler_params = context_from_params()
    try:
        result, item = await wrapp_stat(catalog)
        if result != Result.ERROR_NOT_FOUND:
            if is_folder(item):
                raise FailedCommand(f"output path {catalog} exists and is a folder, can't write")
            elif not overwrite:
                raise FailedCommand(f"output file exists and --overwrite is not specified: {catalog}")

        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            output_catalog = await extract_catalog(
                package, version, repo, recursive, recalculate_identities=recalculate_identities, context=context, scheduler=scheduler
            )
        await output_catalog.save(context, catalog)

    except FailedCommand as e:
        logger.error(f"Failure to extract catalog for package {package} {version}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while extracting catalog for package {package} {version}: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def extract_catalog(
    package: str,
    version: str,
    repo: str,
    recursive: bool,
    add_wrapp_files: bool = False,
    recalculate_identities: bool = False,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> Catalog:
    """
    Given a package name and a version plus a repository location, extract and return the Catalog object.

    :param package: Name of the package
    :param version: Version of the package
    :param repo: The URL of the repository where the package is located
    :param recursive: Flag indicating whether to recursively traverse the subpackage structure, and to include all subpackages' items
    :param add_wrapp_files: Flag indicating whether to add the wrapp files themselves. This is only used for manifest generation.
    :param recalculate_identities: Flag indicating whether to recalculate resource identities for catalog items that don't have them.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("package", package)
    validate_non_empty_string("version", version)
    validate_non_empty_string("repo", repo)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    repo_object: Repository = Repository(repo_url=repo)
    if not await repo_object.find_package(package, version):
        raise FailedCommand(f"Specified repository does not contain a package {package} version {version}")

    # Check if we can find the package
    wrapp_file = wrapp_file_in_repo(repo_object, package, version)
    result, item = await wrapp_stat(wrapp_file)
    if result != Result.OK:
        raise FailedCommand(f"Package file {wrapp_file} can not be found!")

    scheduler = scheduler.create_sub_context_and_node(
        f"Extract catalog for {package} {version}", expected_num_sub_jobs=1 if recursive else None
    )

    # Load the package file of the source package
    top_level: Optional[PackageInfo] = await load_package_setup(wrapp_file)
    if top_level is None:
        raise FailedCommand(f"Failed to load the source's package at {wrapp_file}")

    if recursive:
        loaded_package = LoadedPackage(wrapp_file, top_level)
        # Now load the recursive catalog
        full_source_catalog = await loaded_package.calculate_catalog_including_deps(
            use_repo_path=True, add_subpackage_wrapp_files=add_wrapp_files, task_node=scheduler.parent_task_node()
        )
        final_catalog = full_source_catalog
    else:
        if not top_level.catalog:
            raise FailedCommand(f"Package {package} has no catalog")
        final_catalog = top_level.catalog

    if add_wrapp_files:
        # Add the wrapp file itself to the catalog. Normalize the paths to the same
        # absolute URI form the sibling catalog entries use (the catalog() file-tree
        # walk normalizes its root via make_file_url_absolute, but the file-list path
        # used here does not). Without this, a bare filesystem wrapp_file argument
        # produces a scheme-less source_path/resource_identity for the .wrapp entry
        # while data files keep their file:// scheme, yielding a mixed-scheme manifest.
        base_path = make_file_url_absolute(repo_object.package_url(package, version))
        wrapp_file = make_file_url_absolute(wrapp_file)
        single_file_catalog = await catalog(base_path, [(base_path, wrapp_file)], context=context, scheduler=scheduler)
        if not single_file_catalog.items:
            raise FailedCommand(f"Failed to determine source_url for wrapp file {wrapp_file}")
        final_catalog.items.append(single_file_catalog.items[0])

    # Recalculate resource_identities if requested (needed when building manifests for old catalogs without resource_identity field)
    if recalculate_identities:
        await _recalculate_catalog_resource_identities(final_catalog, context, scheduler)

    return final_catalog
