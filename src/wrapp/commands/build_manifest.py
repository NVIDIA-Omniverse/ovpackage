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

import json
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
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.package import (
    load_package_setup,
    repository_from_wrapp_file_path,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    OverwriteFlag,
    ProgressReportOption,
    RecalculateResourceIdentitiesFlag,
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
    Result,
    wrapp_stat,
    wrapp_write_file_content,
)
from wrapp.utils.utils import (
    FailedCommand,
    FileParseException,
    StorageOperationError,
    get_base_directory_from_url,
    is_folder,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from .catalog import catalog

# Import the extract_catalog function for repository files
from .extract_catalog import extract_catalog

logger = logging.getLogger("omni.wrapp")


@app.command(name="build-manifest")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def build_manifest_command(
    wrapp_file: str = typer.Argument(..., help="URL to the wrapp file"),
    output: str = typer.Argument(..., help="Output JSON file path"),
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
    time: bool = TimeFlag,
) -> None:
    """
    Build a manifest file from a wrapp file URL. If the wrapp file is in a repository,
    it will run extract-catalog recursive to generate a full catalog. If it is not in
    a repository (workspace file), it will catalog the file tree with the wrapp file
    as the root. The output is a JSON file with relative paths and download URLs.
    """
    context, scheduler_params = context_from_params()
    try:
        # Check if output file exists
        result, item = await wrapp_stat(output)

        if result != Result.ERROR_NOT_FOUND:
            if result == Result.OK:
                if is_folder(item):
                    raise FailedCommand(f"output path {output} exists and is a folder, can't write")
                if not overwrite:
                    raise FailedCommand(f"output file exists and --overwrite is not specified: {output}")
            else:
                raise StorageOperationError(f"Error checking the output file {output}: {result}")

        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            manifest_data = await build_manifest(
                wrapp_file, recalculate_identities=recalculate_identities, context=context, scheduler=scheduler
            )

        # Write the manifest to the output file
        await _write_manifest_to_file(manifest_data, output, context)

    except FailedCommand as e:
        logging.error(f"Failure to build manifest for wrapp file {wrapp_file}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logging.error(f"Error while building manifest for wrapp file {wrapp_file}: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
async def build_manifest(
    wrapp_file: str,
    recalculate_identities: bool = False,
    *,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> List[dict]:
    """
    Build a manifest from a wrapp file URL. Returns a list of dictionaries with
    'relative_path' and 'download_url' keys.

    :param wrapp_file: URL to the wrapp file
    :param recalculate_identities: Flag indicating whether to recalculate resource identities for old catalogs
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext
    :returns: List of manifest entries with relative_path and download_url
    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    if context is None:
        context = CommandParameters()

    # Check if the wrapp file exists
    result, item = await wrapp_stat(wrapp_file)
    if result == Result.ERROR_NOT_FOUND:
        raise FailedCommand(f"Wrapp file {wrapp_file} cannot be found: {result}")
    elif result != Result.OK:
        raise StorageOperationError(f"Error reading input file {wrapp_file}: {result}")

    # Load the package info to get name and version
    try:
        package_info = await load_package_setup(wrapp_file)
    except FileParseException as e:
        raise FailedCommand(f"Failed to parse package info from {wrapp_file}") from e
    if package_info is None:
        raise FailedCommand(f"Failed to load package info from {wrapp_file}")

    # Check if the wrapp file is in a repository
    repository_url = repository_from_wrapp_file_path(wrapp_file)

    if repository_url is not None:
        # File is in a repository - use extract-catalog recursive
        logger.info(f"wrapp file is in repository {repository_url}, using extract-catalog recursive")
        catalog = await extract_catalog(
            package_info.name,
            package_info.version,
            repository_url,
            recursive=True,
            add_wrapp_files=True,
            recalculate_identities=recalculate_identities,
            context=context,
            scheduler=scheduler,
        )
    else:
        # File is in workspace - catalog the file tree
        logger.info("wrapp file is in workspace, cataloging file tree")
        catalog = await _catalog_workspace_file_tree(wrapp_file, context, scheduler)

    # Convert catalog to manifest format
    manifest_data = []
    for item in catalog.items:
        if item.resource_identity:
            manifest_data.append({"relative_path": item.relative_path, "resource_identity": item.resource_identity})
        else:
            manifest_data.append({"relative_path": item.relative_path, "resource_identity": item.source_path})
    return manifest_data


async def _catalog_workspace_file_tree(wrapp_file: str, context: CommandParameters, scheduler: SchedulerContext) -> Catalog:
    """
    Catalog a file tree with the wrapp file as the root.
    Reuses the existing catalog command's functionality.
    """
    # Get the directory containing the wrapp file
    wrapp_dir = get_base_directory_from_url(wrapp_file)

    # Use the existing catalog command to catalog the file tree
    return await catalog(root_dir=wrapp_dir, context=context, scheduler=scheduler)


async def _write_manifest_to_file(manifest_data: List[dict], output_file: str, context: CommandParameters) -> None:
    """
    Write the manifest data to the output file as JSON.
    """
    json_content = json.dumps(manifest_data, indent=2)
    result = await wrapp_write_file_content(context, output_file, json_content)
    if result != Result.OK:
        raise StorageOperationError(f"Failed to write manifest to {output_file}: {result}")
