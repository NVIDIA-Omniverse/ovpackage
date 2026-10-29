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
from asyncio import Task
from datetime import (
    datetime,
    timezone,
)
from typing import (
    Any,
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
from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.manifest import (
    Manifest,
    ManifestEntry,
    ManifestFileEntry,
    ManifestMinimalFileEntry,
    ManifestResourceInfo,
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
    LogFileOption,
    MinimalFormatFlag,
    OverwriteFlag,
    ProgressReportOption,
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
    wrapp_resource_identity_for_manifest,
    wrapp_stat,
)
from wrapp.utils.storage_interface import SerializedResourceIdentity
from wrapp.utils.utils import (
    FailedCommand,
    FileParseException,
    StorageOperationError,
    get_base_directory_from_url,
    is_file_url,
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
    minimal_format: bool = MinimalFormatFlag,
    recalculate_identities: bool = typer.Option(
        True,
        "--recalculate-identities/--no-recalculate-identities",
        help="Validate legacy catalog bytes and capture missing durable identities. Existing trusted identities are preserved. Does not enable manifests for local files.",
    ),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    log_file: str = LogFileOption,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Build a manifest file from a wrapp file URL. If the wrapp file is in a repository,
    it will run extract-catalog recursive to generate a full catalog. If it is not in
    a repository (workspace file), it will catalog the file tree with the wrapp file
    as the root. The output is a storage-core manifest JSON file.

    Manifests for local file content are not supported. The only current consumer
    is the storage service, which serves S3, Azure and GCS through Storage API.
    The manifest output can still be written to a local file.
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
                wrapp_file,
                minimal_format=minimal_format,
                recalculate_identities=recalculate_identities,
                context=context,
                scheduler=scheduler,
            )

        await manifest_data.save(context, output)

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
    *,
    minimal_format: bool = False,
    recalculate_identities: bool = True,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> Manifest:
    """
    Build a storage-core manifest from a wrapp file URL.

    Manifests for local file content are not supported: the only current consumer
    is the storage service, which serves S3, Azure and GCS through Storage API.
    Recalculating identities does not enable local manifests. This restriction
    concerns the content being described, not the location of the manifest output.

    :param wrapp_file: URL to the wrapp file
    :param minimal_format: If true, write only relative_path and resource_identity per file entry
    :param recalculate_identities: By default, validate legacy catalog bytes against their recorded hashes and capture missing
        durable identities and metadata. Existing v2 identities are never recalculated. Set false to require recorded identities only.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext
    :returns: Manifest model containing format_version, storage_api_version, and files
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

    return await _manifest_from_catalog(catalog, minimal_format=minimal_format, scheduler=scheduler)


async def _manifest_from_catalog(catalog: Catalog, *, minimal_format: bool, scheduler: SchedulerContext) -> Manifest:
    """Convert a WRAPP catalog into the storage-core build-manifest shape."""
    task_results: dict[Task[Any], ManifestEntry] = {}
    tasks = []
    async with scheduler.parent_task_node().create_task_group_node(
        f"Building manifest entries for {len(catalog.items)} files", task_results
    ) as task_node:
        for item in catalog.items:
            tasks.append(
                task_node.create_task(
                    _manifest_entry_from_catalog_item(item, minimal_format=minimal_format),
                    name="Build manifest entry",
                )
            )

    return Manifest(files=[task_results[task] for task in tasks])


async def _manifest_entry_from_catalog_item(item: CatalogItem, *, minimal_format: bool) -> ManifestEntry:
    # There is deliberately no Stat-by-identity. A mutable address cannot supply
    # metadata for an earlier observation, even if the token is still readable.
    resource_identity = await _resource_identity_for_manifest_item(item)

    if minimal_format:
        return ManifestMinimalFileEntry(relative_path=item.relative_path, resource_identity=resource_identity)

    missing = [name for name in ("size", "modified_time") if getattr(item, name) is None]
    if missing:
        raise FailedCommand(
            f"Manifest item {item.source_path} lacks recorded {', '.join(missing)}; "
            "a validated catalog rebuild is required for a full manifest (or use minimal format)."
        )
    assert item.size is not None and item.modified_time is not None
    return ManifestFileEntry(
        relative_path=item.relative_path,
        resource_info=ManifestResourceInfo(
            resource_identity=resource_identity,
            size=item.size,
            modified_time=_format_modified_time_utc(item.modified_time),
        ),
    )


async def _resource_identity_for_manifest_item(item: CatalogItem) -> SerializedResourceIdentity:
    if item.resource_identity_v2 is not None:
        return wrapp_resource_identity_for_manifest(
            SerializedResourceIdentity(item.resource_identity_v2),
            expected_hash=item.hash,
        )
    if is_file_url(item.source_path):
        raise FailedCommand(
            f"Manifest generation for local files is not supported: {item.source_path}. "
            "The only current manifest consumer is the storage service, which serves S3, Azure and GCS through Storage API; "
            "it does not consume local files."
        )
    raise FailedCommand(
        f"Manifest item {item.source_path} lacks resource_identity_v2; storage-core manifests require a durable identity for every file. "
        "Enable --recalculate-identities for legacy catalogs and use a Storage API backend that supplies identities."
    )


def _format_modified_time_utc(modified_time: datetime) -> str:
    if modified_time.tzinfo is None:
        utc_time = modified_time.replace(tzinfo=timezone.utc)
    else:
        utc_time = modified_time.astimezone(timezone.utc)
    return utc_time.isoformat(timespec="seconds").replace("+00:00", "Z")


async def _catalog_workspace_file_tree(wrapp_file: str, context: CommandParameters, scheduler: SchedulerContext) -> Catalog:
    """
    Catalog a file tree with the wrapp file as the root.
    Reuses the existing catalog command's functionality.
    """
    # Get the directory containing the wrapp file
    wrapp_dir = get_base_directory_from_url(wrapp_file)

    # Use the existing catalog command to catalog the file tree
    return await catalog(root_dir=wrapp_dir, context=context, scheduler=scheduler)
