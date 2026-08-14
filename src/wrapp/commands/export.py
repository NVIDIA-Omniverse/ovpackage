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
    Any,
    Callable,
    Coroutine,
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
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    LocalReadOnlyFile,
    Result,
    wrapp_can_be_cached,
    wrapp_copy_item_optimized,
    wrapp_get_cached_file,
)
from wrapp.utils.temp_file_manager import TempFileManager
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    format_size,
    is_nucleus_url,
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
                    # source_path points to the actual file location (works for both normal and redirect packages)
                    source_url = item.source_path
                    scheduled_task_name: str = "Download file"
                    hub_runs = wrapp_can_be_cached(source_url)
                    if not hub_runs:
                        download_func: Callable[..., Coroutine[Any, Any, Any]] = _download_item_to_tar
                        # TODO we should think about how to extract the SchedulingParameters from a Scheduling object again
                        download_args = [context, scheduler.get_parameters().progress_report, tar, item, storage_path]
                        scheduled_task_name += " without hub cache"
                    else:
                        download_func = _download_cached_item_to_tar
                        download_args = [task_node, tar, source_url, storage_path]

                    # The if currently makes no difference - but it seems a good idea to keep it anyway for now.
                    if is_nucleus_url(source_url) and (item.size if item.size is not None else 0) >= 256 * 1024:
                        # Large file transfer, rate limit is done by connection library
                        task_node.create_task(download_func(*download_args), name=scheduled_task_name)
                    else:
                        # Small file, needs to be rate limited as it does not hit LFT
                        task_node.create_task(download_func(*download_args), name=scheduled_task_name)
        logger.info(f"Done writing {output}")


async def _download_cached_item_to_tar(task_node: SchedulerTaskGroupNode, tar: tarfile.TarFile, url: str, real_filename: str):
    result, cache_file = await wrapp_get_cached_file(url)
    if result != Result.OK or cache_file is None:
        raise StorageOperationError(f"Failed to download file {url}: {result}")
    task_node.create_task(_add_file_to_tar(tar, real_filename, cache_file), name="Add file to tar")


async def _add_file_to_tar(tar: tarfile.TarFile, real_filename: str, cache_file: LocalReadOnlyFile):
    # This next block of code takes a while, but there is unfortunately not an async version of tar.add.
    # So we will block the scheduler for a bit. This is actually okish (it is not that long).
    # Beware though that if too many of those kind of functions are called in the same frame
    # in the async loop we block the Python thread for as long as that takes (which can be quite long),
    # not being able to give the user visual feedback (the async loop in Python 3.10 executes all
    # functions "ready" in the current frame - see https://github.com/python/cpython/blob/e9d1360c9a1072aa23950b321491dc542c3a19b8/Lib/asyncio/base_events.py#L1983).
    # So asyncio.wait(..., return_when=asyncio.FIRST_COMPLETED) actually executes all "ready" tasks
    # which in this case could be all the add_file_to_tar functions at once.
    try:
        size_downloaded = os.path.getsize(cache_file.local_path())
        Stats.get().increment(StatType.BYTES_COPIED, size_downloaded)
        tar.add(cache_file.local_path(), arcname=real_filename)
        return Result.OK
    finally:
        cache_file.close()


async def _download_item_to_tar(
    context: CommandParameters,
    progress_report_verbosity: ProgressReportVerbosity,
    tar: tarfile.TarFile,
    item: CatalogItem,
    storage_path: str,
):
    # TODO: once the storage backends support a stream-based download API (read_stream),
    # we can stream directly into the tar archive instead of materializing to a temp file first.
    async with TempFileManager.me().create_temp_file() as tmp:
        result = await wrapp_copy_item_optimized(context, item.source_path, tmp.url, item.hash, overwrite=True, force_upload=False)
        if result != Result.OK:
            raise StorageOperationError(f"Failed to download '{item.source_path}', error {result}. Aborting export.")
        if progress_report_verbosity == ProgressReportVerbosity.off:
            logger.info(f"Downloaded {format_size(item.size)} for {item.source_path}")
        tmp.write_completed()
        Stats.get().increment(StatType.BYTES_COPIED, os.path.getsize(tmp.path))
        tar.add(tmp.path, arcname=storage_path)


def _write_into_tarfile(tar: tarfile.TarFile, name: str, content: bytes):
    tarinfo = tarfile.TarInfo(name)
    tarinfo.size = len(content)
    Stats.get().increment(StatType.BYTES_COPIED, len(content))
    tar.addfile(tarinfo, fileobj=io.BytesIO(content))
