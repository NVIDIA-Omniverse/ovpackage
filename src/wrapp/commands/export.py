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

import io
import logging
import os
import shutil
import tarfile
from copy import deepcopy
from typing import (
    List,
    Optional,
    Set,
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
from wrapp.datastructures.package import (
    PackageInfo,
    apply_source_policy,
    is_package_file_name,
    load_package_setup,
    wrapp_file_name,
)
from wrapp.datastructures.package_standard import (
    PACKAGE_STANDARD_DEFINITION_FILENAME,
    PACKAGE_STANDARD_METADATA_FOLDER,
    read_package_standard_definition_bytes,
    read_package_standard_metadata_files,
)
from wrapp.options import (
    AuthOption,
    CanonicalSourceOption,
    CatalogFileOption,
    DebugLevelFlag,
    DedupFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    LogFileOption,
    OutputOption,
    OverwriteFlag,
    ProgressReportOption,
    RepoOption,
    StatsFlag,
    StripSourceFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.repo.blob_store import BlobStore
from wrapp.repo.repository import Repository
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import wrapp_materialize
from wrapp.utils.storage_interface import (
    ResourceAddress,
    SerializedResourceIdentity,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    format_size,
    join_paths,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="export")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def export_command(
    package_name: str = typer.Argument(..., help="Specify the name of the package to install"),
    version: str = typer.Argument(..., help="Specify the version of the package [any string]"),
    repo: str = RepoOption,
    catalog: str = CatalogFileOption,
    output: str = OutputOption,
    overwrite: bool = OverwriteFlag,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    dedup: bool = DedupFlag,
    strip_source: bool = StripSourceFlag,
    canonical_source: Optional[str] = CanonicalSourceOption,
    stats: bool = StatsFlag,
    debug: bool = DebugLevelFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Create an uncompressed tar file of a package from the given package cache
    """
    context, scheduler_params = context_from_params()
    if output is None:
        output = join_paths(os.getcwd(), f"{package_name}.{version}.tar")
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await export_package(
                catalog=catalog,
                repo=repo,
                package_name=package_name,
                version=version,
                output=output,
                overwrite=overwrite,
                dedup=dedup,
                strip_source=strip_source,
                canonical_source=canonical_source,
                context=context,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        logger.error(f"Failure to export package '{package_name}' version {version} to '{output}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while exporting '{package_name}' to '{output}', operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def export_package(
    catalog: Optional[str],
    repo: Optional[str],
    package_name: str,
    version: str,
    output: str,
    overwrite: bool = False,
    dedup=False,
    *,
    strip_source: bool = False,
    canonical_source: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Take package information and repository location and create an uncompressed tar file of a package from the given package cache

    :param catalog: Name of the catalog file that describes the items to export
    :param repo: URL of the repository containing the package to be exported
    :param package_name: Name of the package
    :param version: Version of the package
    :param output: Name of tar file to create
    :param overwrite: Flag to allow overwriting the tar file in case it already exists
    :param dedup: Specify this to deduplicate content on export, i.e. don't store files by their name in the tar but by their hash
    :param strip_source: When True, clear the source and repository provenance fields in the exported .wrapp file so internal addresses are not published. Mutually exclusive with canonical_source.
    :param canonical_source: When set, record this canonical base URL as the source/repository provenance instead of the internal address. Mutually exclusive with strip_source.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("package_name", package_name)
    validate_non_empty_string("version", version)
    validate_non_empty_string("output", output)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    if strip_source and canonical_source:
        raise FailedCommand("Use either --strip-source or --canonical-source, not both.")
    if catalog and repo:
        raise FailedCommand(
            "Specify to export either from a repository with --repo or to create a package from a catalog with --catalog, but not both."
        )
    if catalog:
        scheduler = scheduler.create_sub_context_and_node("Export catalog", expected_num_sub_jobs=1)

        # Load the catalog
        catalog_content = await Catalog.load(catalog)
        # Create a package and then export it
        new_package = PackageInfo(name=package_name, version=version, source=catalog, catalog=catalog_content)
        await _export_package_internal(
            new_package,
            output,
            overwrite=overwrite,
            dedup=dedup,
            source_package_url=None,
            strip_source=strip_source,
            canonical_source=canonical_source,
            context=context,
            scheduler=scheduler,
        )
    elif repo:
        scheduler = scheduler.create_sub_context_and_node(f"Export {package_name} {version}", expected_num_sub_jobs=1)

        logger.info(f"Connecting to {repo}...")
        repo_obj = Repository(repo_url=repo)
        if not await repo_obj.find_package(package_name, version):
            raise FailedCommand(f"Source package {package_name} version {version} not found in repository {repo_obj.package_cache_url()}")

        # Load the package file of the source package
        source_package_url = join_paths(repo_obj.package_url(package_name, version), wrapp_file_name(package_name))
        logger.info(f"Loading catalog for {package_name} {version} from {repo}...")
        source_setup: Optional[PackageInfo] = await load_package_setup(source_package_url)
        if source_setup is None:
            raise FailedCommand(f"Failed to load the source's package .wrapp file at {source_package_url}")
        else:
            await _export_package_internal(
                source_setup,
                output,
                overwrite=overwrite,
                dedup=dedup,
                source_package_url=source_package_url,
                strip_source=strip_source,
                canonical_source=canonical_source,
                context=context,
                scheduler=scheduler,
            )
    else:
        raise FailedCommand("Specify a source to export from: either a repository with --repo or a catalog with --catalog.")


async def _export_package_internal(
    package_info: PackageInfo,
    output: str,
    overwrite: bool,
    dedup,
    source_package_url: Optional[str],
    *,
    strip_source: bool = False,
    canonical_source: Optional[str] = None,
    context: CommandParameters,
    scheduler: SchedulerContext,
):
    source_setup = package_info
    if source_setup.catalog is None:
        raise FailedCommand(f"Source package {source_setup.name} has no catalog defined, nothing to export.")
    else:
        export_size_required = source_setup.catalog.size_of_catalog_deduped() if dedup else source_setup.catalog.size_of_catalog()
        bytes_free = shutil.disk_usage(os.path.dirname(os.path.abspath(output)))
        if bytes_free.free < export_size_required:
            raise FailedCommand(f"Not enough diskspace {bytes_free.free} bytes free but {export_size_required} bytes required")
        logger.info(f"Package total size is {export_size_required} bytes, preparing tar file...")

        if not overwrite and os.path.exists(output):
            raise FailedCommand(f"Output file {output} already exists and --overwrite is not specified")

        wrapp_files_in_catalog = [i for i in source_setup.catalog.items if is_package_file_name(i.relative_path)]
        if wrapp_files_in_catalog:
            raise FailedCommand("The source contains a .wrapp file - export of nested packages is not yet supported!")

        with tarfile.open(name=output, mode="w") as tar:
            # First save the retargeted catalog
            if dedup:
                blob_store = BlobStore(source_setup.catalog)
                storage_map = blob_store.get_storage_map()
            else:
                storage_map = None
            blob_store_package = deepcopy(source_setup)
            if blob_store_package.catalog is None:
                raise RuntimeError("deepcopy lost catalog - this should never happen")
            blob_store_package.catalog.storage_map = storage_map
            # Keep internal source addresses out of the exported (published) package if requested
            apply_source_policy(blob_store_package, strip_source=strip_source, canonical_source=canonical_source)
            tarfile_package_str = blob_store_package.model_dump_json(indent=4)
            _write_into_tarfile(tar, wrapp_file_name(blob_store_package.name), tarfile_package_str.encode("UTF-8"))

            # Carry the source package's existing package-standard sidecars into the archive verbatim; the export
            # does not regenerate (and therefore does not re-hash) them. The root definition file and every file in
            # the .metadata folder are archived. A catalog-only export has no source package.
            if source_package_url is not None:
                definition_bytes = await read_package_standard_definition_bytes(source_package_url)
                if definition_bytes is not None:
                    _write_into_tarfile(tar, PACKAGE_STANDARD_DEFINITION_FILENAME, definition_bytes)
                for rel_path, metadata_bytes in (await read_package_standard_metadata_files(source_package_url)).items():
                    _write_into_tarfile(tar, f"{PACKAGE_STANDARD_METADATA_FOLDER}/{rel_path}", metadata_bytes)

            # Now copy the content of the package into the tarball
            # When we are de-duplicating, check that no hash is stored twice
            paths_stored: Set[str] = set()
            async with scheduler.parent_task_node().create_task_group_node(
                f"Copying {len(source_setup.catalog.items)} files to tarball"
            ) as task_node:
                for item in source_setup.catalog.items:
                    # Get the storage path within the TAR file (uses hash-based dedup if enabled)
                    storage_path = blob_store_package.catalog.get_storage_path_for_item(item)
                    if dedup:
                        if storage_path in paths_stored:
                            continue
                        paths_stored.add(storage_path)
                    task_node.create_task(
                        _materialize_item_to_tar(scheduler.get_parameters().progress_report, tar, item, storage_path),
                        name="Materialize file and add to tar",
                    )
        logger.info(f"Done writing {output}")


async def _materialize_item_to_tar(
    progress_report_verbosity: ProgressReportVerbosity,
    tar: tarfile.TarFile,
    item: CatalogItem,
    storage_path: str,
):
    async with wrapp_materialize(
        ResourceAddress(item.source_path),
        resource_identity=SerializedResourceIdentity(item.resource_identity_v2) if item.resource_identity_v2 is not None else None,
        expected_hash=item.hash,
    ) as local_file:
        if progress_report_verbosity == ProgressReportVerbosity.off:
            logger.info(f"Materialized {format_size(item.size)} for {item.source_path}")
        # tar.add is synchronous. Keep the read lease open until it finishes;
        # context exit also closes/releases it if archive writing fails.
        path = os.path.realpath(local_file.local_path())
        Stats.get().increment(StatType.BYTES_COPIED, os.path.getsize(path))
        tar.add(path, arcname=storage_path)


def _write_into_tarfile(tar: tarfile.TarFile, name: str, content: bytes):
    tarinfo = tarfile.TarInfo(name)
    tarinfo.size = len(content)
    Stats.get().increment(StatType.BYTES_COPIED, len(content))
    tar.addfile(tarinfo, fileobj=io.BytesIO(content))
