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

import hashlib
import logging
import os
from dataclasses import (
    dataclass,
    field,
)
from datetime import datetime
from enum import Enum
from typing import (
    BinaryIO,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)
from urllib.parse import urlparse

import typer
from rich.table import Table

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
    Tag,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.hash_cache import HashCache
from wrapp.datastructures.ignore import (
    IgnoreEvaluator,
    create_ignore_eval_from_params,
    item_is_ignored,
)
from wrapp.datastructures.package_standard import is_package_standard_sidecar_path
from wrapp.options import (
    AuthOption,
    CheckpointsFlag,
    DebugLevelFlag,
    FileListFlag,
    FileTransferJobsNumber,
    HashCacheFileOption,
    HashLocalFlag,
    IgnoreFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    OverwriteFlag,
    ProgressReportOption,
    StatsFlag,
    TaggingJobsNumber,
    TagsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.tagging_service import tagging
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.nucleus_hash import (
    calc_nucleus_hash_from_stream,
    calc_package_standard_hashes_from_stream,
)
from wrapp.utils.scheduler_task_node import (
    SchedulerTaskGroupNode,
    SchedulerTreeNode,
)
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    ListEntry,
    LocalReadOnlyFile,
    Result,
    Router,
    wrapp_can_be_cached,
    wrapp_get_cached_file,
    wrapp_list_checkpoints,
    wrapp_list_directory,
    wrapp_list_file_tree,
    wrapp_list_file_tree_supported,
    wrapp_read_text_file,
    wrapp_stat,
    wrapp_supports_tags,
    wrapp_url_exists,
    wrapp_url_is_folder,
)
from wrapp.utils.task_throttler import CPUThrottler
from wrapp.utils.temp_file_manager import TempFileManager
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    format_size,
    has_checkpoint,
    is_file_url,
    is_folder,
    is_regular_file,
    is_relative_path,
    make_file_url_absolute,
    make_relative_url,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


class ContentHashAlgorithm(Enum):
    """Extra content hashes a caller can request to be computed during cataloging.

    ``PACKAGE_STANDARD`` yields the SimReady package-standard hashes (a plain
    ``sha256`` and, for files over 1 MiB, ``sha256-first1m``) computed in the same
    offloaded pass that produces the WRAPP nucleus hash, so the payload is read once.
    """

    PACKAGE_STANDARD = "package-standard"


@app.command(name="catalog")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def catalog_command(
    source_dir: str = typer.Argument(..., help="The source URL for the package."),
    catalog_file: str = typer.Argument(..., help="The file to write the catalog to. Will be overwritten. JSON format."),
    file_list: bool = FileListFlag,
    root: str = typer.Option(
        None,
        help="Optionally specify the root directory for the file list. This is required in case the file list is a tab separated file and not all entries have the same root.",
    ),
    overwrite: bool = OverwriteFlag,
    checkpoints: bool = CheckpointsFlag,
    local_hash: bool = HashLocalFlag,
    hash_cache_file: Optional[str] = HashCacheFileOption,
    ignore_file: str = IgnoreFileOption,
    tags: bool = TagsFlag.default_help(),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    verbose: bool = VerboseFlag,
    stats: bool = StatsFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,  # This needs to be declared for measure_time
) -> None:
    """
    Catalog a version of a package for later publishing or preparing a reproducible build
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            logger.info(f"Cataloging package in {source_dir}...")
            if await wrapp_url_exists(catalog_file) and not overwrite:
                raise FailedCommand(f"Catalog {catalog_file} already exists and --overwrite is not specified!")

            catalog_struct: Catalog
            if file_list:
                catalog_struct = await catalog(
                    root_dir=root,
                    file_list=source_dir,
                    tags=tags,
                    ignores=ignore_file,
                    checkpoints=checkpoints,
                    local_hash=local_hash,
                    context=context,
                    scheduler=scheduler,
                )
            else:
                catalog_struct = await catalog(
                    root_dir=source_dir,
                    tags=tags,
                    ignores=ignore_file,
                    checkpoints=checkpoints,
                    local_hash=local_hash,
                    context=context,
                    scheduler=scheduler,
                )

            await catalog_struct.save(context, catalog_file)

            logger.info(f"Wrote catalog to {catalog_file}")
    except FailedCommand as e:
        logger.error(f"Failed to catalog {source_dir} into {catalog_file}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while creating catalog of {source_dir} to {catalog_file}: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def catalog(
    root_dir: Optional[str] = None,
    file_list: Optional[Union[str, List[Tuple[str, str]]]] = None,
    tags: bool = False,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    local_hash: bool = False,
    checkpoints: bool = True,
    skip_missing: bool = False,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> Catalog:
    """
    Takes a root URL or a file list to build a catalog via either a file tree walk or a file list tsv file, returning a Catalog

    :param root_dir: URL of the root of the files to be cataloged
    :param file_list: Alternative to root_dir, specify the name of a tab separated file containing a list of files to include,
                        or a list explicitly listing all files as tuples base path, relative path.
    :param tags: Set this to additionally query the tagging service and make tags part of the catalog
    :param ignores: Specify the name of an ignore file containing rules for ignoring items, or an IgnoreEvaluator object
    :param local_hash: Flag to allow the catalog operation to calculate the hash locally, potentially after a download of the data
    :param checkpoints: Flag, default on, to list checkpoints of local items and add the latest checkpoint into the source URL if the source URL supports checkpoints
    :param skip_missing: When True and using file_list mode, silently skip files that are not found instead of raising an error.
                         This is useful for targeted cataloging where some files may not exist in the destination.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :return: Catalog created

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    resulting_catalog, _ = await _catalog_impl(
        root_dir=root_dir,
        file_list=file_list,
        tags=tags,
        ignores=ignores,
        local_hash=local_hash,
        checkpoints=checkpoints,
        skip_missing=skip_missing,
        content_hashes=None,
        context=context,
        scheduler=scheduler,
    )
    return resulting_catalog


@force_wrapp_thread
@ensure_scheduler
@log_args
async def catalog_with_content_hashes(
    root_dir: Optional[str] = None,
    file_list: Optional[Union[str, List[Tuple[str, str]]]] = None,
    tags: bool = False,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    local_hash: bool = False,
    checkpoints: bool = True,
    skip_missing: bool = False,
    content_hashes: Optional[List[ContentHashAlgorithm]] = None,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> Tuple[Catalog, Dict[str, Dict[str, str]]]:
    """Internal variant of :func:`catalog` that also returns precomputed content hashes.

    When ``content_hashes`` requests an algorithm (e.g. ``ContentHashAlgorithm.PACKAGE_STANDARD``),
    the per-file hashes are computed in the same offloaded pass that produces the WRAPP nucleus hash
    and returned as a ``{relative_path: {hash_name: value}}`` map, so callers need not re-read the payload.
    """
    return await _catalog_impl(
        root_dir=root_dir,
        file_list=file_list,
        tags=tags,
        ignores=ignores,
        local_hash=local_hash,
        checkpoints=checkpoints,
        skip_missing=skip_missing,
        content_hashes=content_hashes,
        context=context,
        scheduler=scheduler,
    )


async def _catalog_impl(
    root_dir: Optional[str],
    file_list: Optional[Union[str, List[Tuple[str, str]]]],
    tags: bool,
    ignores: Optional[Union[str, IgnoreEvaluator]],
    local_hash: bool,
    checkpoints: bool,
    skip_missing: bool,
    content_hashes: Optional[List[ContentHashAlgorithm]],
    context: CommandParameters,
    scheduler: Optional[SchedulerContext],
) -> Tuple[Catalog, Dict[str, Dict[str, str]]]:
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    catalog_items: List[CatalogItem] = []
    with CatalogBuilder(
        task_node=scheduler.parent_task_node(),
        local_hash=local_hash,
        checkpoints=checkpoints,
        command_params=context,
        error_raises_exception=not skip_missing,
        content_hashes=content_hashes,
    ) as builder:
        if isinstance(file_list, list):
            if not root_dir:
                raise FailedCommand("You must specify root_dir if the file_list is given explicitly")
            root = root_dir
            ignore_rules = await create_ignore_eval_from_params(ignores, root)
            catalog_items = await builder.catalog_file_list(file_list, ignore_rules=ignore_rules)
        elif file_list is not None:
            if not await wrapp_url_exists(file_list):
                raise FailedCommand(f"Cannot find file list: {file_list}")
            if await wrapp_url_is_folder(file_list):
                raise FailedCommand(f"As file list, please specify the URL to a file and not a folder: {file_list}")
            if context.verbose:
                logger.info(f"Starting catalog operation of file list {file_list}")
            root, catalog_items = await builder.catalog_file_list_from_file(file_list, ignores=ignores)
            if root_dir:
                root = root_dir
            if not root:
                raise FailedCommand("Root folder was not specified, and the root in the file list is ambiguous.")
        elif root_dir is not None:
            root = make_file_url_absolute(root_dir)
            if context.verbose:
                logger.info(f"Starting catalog operation of file tree at {root_dir}")
            ignore_rules = await create_ignore_eval_from_params(ignores, root)
            catalog_items = await builder.catalog_file_tree(root, ignore_rules=ignore_rules)
        else:
            raise FailedCommand("Specify either root_dir or file_list to catalog!")

    # Sort catalog items by path, then build json structure and write it to disk
    catalog_items.sort(key=lambda x: x.relative_path)
    resulting_catalog = Catalog(root=root, items=catalog_items)
    if tags:
        await CatalogBuilder.add_tags_to_catalog(context, scheduler, resulting_catalog)
    return resulting_catalog, builder.content_hashes_map


class ProgressStats_Catalog:
    def __init__(self, catalog_builder: "CatalogBuilder"):
        self._cb = catalog_builder
        self.files_found = 0
        self.num_bytes_found = 0
        self.files_downloaded = 0
        self.num_bytes_downloaded = 0
        self.hash_cache_hits = 0
        self.num_bytes_hash_cache_hits = 0
        self.directories_found = 0

    def header_lines(self) -> Table:
        # Calculate the stats we can calculate
        files_catalogued = 0
        num_bytes_catalogued = 0
        for r in self._cb.result_list:
            files_catalogued += 1
            num_bytes_catalogued += r.size or 0

        header_row = []
        file_table_row = []
        if self.directories_found > 0:
            header_row = ["Dirs found"]
            file_table_row += [
                f"{self.directories_found}",
            ]

        header_row += ["Files Found", "Catalogued"]
        file_table_row += [
            f"{self.files_found} ({format_size(self.num_bytes_found)})",
            f"{files_catalogued} ({format_size(num_bytes_catalogued)})",
        ]
        if self._cb.hash_cache:
            file_table_row.append(f"{self.hash_cache_hits} ({format_size(self.num_bytes_hash_cache_hits)})")
            header_row.append("Hash Cache Hits")
        if self.files_downloaded > 0:
            file_table_row.append(f"{self.files_downloaded} ({format_size(self.num_bytes_downloaded)})")
            header_row.append("Downloaded")

        progress_table = Table(*header_row, title="Cataloging...")
        progress_table.add_row(*file_table_row)

        return progress_table


class RelativeUrl:
    def __init__(self, relative_url):
        self.relative_url = relative_url.replace("\\", "/").lstrip("/").rstrip("/")

    def append(self, url: "RelativeUrl"):
        return RelativeUrl(relative_url=self.relative_url + "/" + url.relative_url)

    def __repr__(self):
        return self.relative_url


class AbsoluteUrl:
    def __init__(self, absolute_url):
        if isinstance(absolute_url, AbsoluteUrl):
            # Copy constructor
            self.absolute_url = absolute_url.absolute_url
        else:
            self.absolute_url = absolute_url.replace("\\", "/").rstrip("/")

    def append(self, url: RelativeUrl):
        return AbsoluteUrl(absolute_url=(self.absolute_url + "/" + url.relative_url))

    def make_relative(self, url: "AbsoluteUrl"):
        return RelativeUrl(relative_url=make_relative_url(self.absolute_url, url.absolute_url))

    def __repr__(self):
        return self.absolute_url


class CatalogBuilder:
    def __init__(
        self,
        task_node: SchedulerTreeNode,
        verbose: bool = False,
        checkpoints: bool = True,
        local_hash: bool = False,
        command_params: CommandParameters = CommandParameters(),
        error_raises_exception=True,
        content_hashes: Optional[List[ContentHashAlgorithm]] = None,
    ):
        self.verbose = command_params.verbose
        self.checkpoints = checkpoints
        self.local_hash = local_hash
        self.result_list: List[CatalogItem] = []
        # Extra content hashes (e.g. package-standard sha256) requested in addition to the WRAPP
        # nucleus hash, computed in the same offloaded pass and collected as {relative_path: {name: value}}.
        self._content_hashes: List[ContentHashAlgorithm] = content_hashes or []
        self.content_hashes_map: Dict[str, Dict[str, str]] = {}
        self.hash_cache: Optional[HashCache] = None
        self.progress_stats: ProgressStats_Catalog = ProgressStats_Catalog(self)
        if command_params.hash_cache_file is not None:
            if verbose:
                logger.info(f"loading hash cache from {command_params.hash_cache_file}")
            self.hash_cache = HashCache(command_params.hash_cache_file, verbose=command_params.verbose)
        self._error_raises_exception = error_raises_exception
        self._root_task_node = task_node

    def __enter__(self) -> "CatalogBuilder":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.hash_cache is not None:
            self.hash_cache.save()

    async def _catalog_file_item(
        self, task_node: SchedulerTaskGroupNode, item: ListEntry, base: AbsoluteUrl, relative: RelativeUrl
    ) -> None:
        assert item.modified_time is not None
        assert item.size is not None
        self.progress_stats.files_found += 1
        self.progress_stats.num_bytes_found += item.size
        file_url: AbsoluteUrl = base.append(relative)
        Stats.get().increment(StatType.FILES)
        Stats.get().increment(StatType.BYTES_IN_CATALOG, item.size)
        if self.checkpoints and has_checkpoint(item):
            # This case is for backends that allow checkpoints/resource_identities, and this item has one.
            # Use the make_resource_identity function to create the data and the code required to retrieve the object later.
            storage = Router.me().storage_for_read(file_url.absolute_url)
            new_absolute_url, new_relative_path, resource_identity = await storage.make_resource_identity(
                file_url.absolute_url, item, relative.relative_url
            )
            if not item.nucleus_hash:
                # This can happen when the storage backend does not provide a hash via metadata
                await self._check_download_required(task_node, base, file_url, resource_identity.to_string())
            else:
                self.result_list.append(
                    CatalogItem(
                        relative_path=new_relative_path,
                        source_path=new_absolute_url,
                        resource_identity=resource_identity.to_string(),
                        hash=item.nucleus_hash,
                        size=item.size,
                    )
                )
        elif item.nucleus_hash:
            # we use the Nucleus hash provided by the storage.
            self.result_list.append(
                CatalogItem(
                    relative_path=relative.relative_url,
                    source_path=file_url.absolute_url,
                    hash=item.nucleus_hash,
                    size=item.size,
                )
            )
        elif (
            is_file_url(file_url.absolute_url)
            or self.local_hash
            or Router.me().is_s3_url(file_url.absolute_url)
            or Router.me().is_azure_url(file_url.absolute_url)
            or Router.me().is_storage_api_url(file_url.absolute_url)
        ):
            # This is a local file URL, or an S3/Azure URL, or a storage API URL, or something not checkpointed, we can calculate the hash value locally.
            # _check_download_required will stat() the URL, which might return the metadata list() has not delivered.
            await self._check_download_required(task_node, base, file_url, None)
        else:
            # So this is an omniverse URL, it has no nucleus_hash, and is either on a non-checkpointing server
            # or the item has no checkpoints. If it is a mount, list_checkpoints will return NOT_SUPPORTED
            # If it returns checkpoints here, the data on the Nucleus server is corrupt or it is a buggy server.
            # Note this ignores the local_hash flag and will always create the local hash.
            result, checkpoints = await wrapp_list_checkpoints(file_url.absolute_url)
            if result == Result.ERROR_NOT_SUPPORTED:
                # Fallback - this server (S3?) does not support checkpoints without us knowing up front. Treat it with --local-hash
                task_node.create_task(self._download_item(task_node, base, file_url, item.modified_time, item.size, None))
            else:
                raise FailedCommand(
                    f"Can't catalog file: Found file {file_url} without checkpoint! List gave {result} and {checkpoints} though"
                )

    async def catalog_file_list(
        self, list_of_urls: List[Tuple[str, str]], *, ignore_rules: Optional[IgnoreEvaluator] = None
    ) -> List[CatalogItem]:
        async with self._root_task_node.create_task_group_node(description="Cataloging file list...") as task_node:
            for base, url in list_of_urls:
                if is_relative_path(url):
                    file_url = AbsoluteUrl(absolute_url=base).append(RelativeUrl(relative_url=url))
                    relative = url
                else:
                    file_url = AbsoluteUrl(absolute_url=url)
                    relative = make_relative_url(base, url)
                    assert is_relative_path(relative)
                if is_package_standard_sidecar_path(relative):
                    if self.verbose:
                        logger.info(f"Ignoring package-standard sidecar {relative}")
                    continue
                if ignore_rules is not None and ignore_rules.is_ignored(file_url.absolute_url):
                    if self.verbose:
                        logger.info(f"Ignoring {file_url.absolute_url}")
                    continue
                task_node.create_task(self._parallel_stat_and_catalog(task_node, base, relative), name="Checking file")
        return self.result_list

    async def _parallel_stat_and_catalog(self, task_node: SchedulerTaskGroupNode, base: str, relative: str):
        # Stat this file and if found catalog it adding it to the self.result_list
        file_url = AbsoluteUrl(absolute_url=base).append(RelativeUrl(relative_url=relative))
        result, item = await wrapp_stat(file_url.absolute_url)
        if result != Result.OK:
            if result != Result.ERROR_NOT_FOUND or self._error_raises_exception:
                raise StorageOperationError(
                    f"Failed to stat url found in file list, got result {result}, can't catalog: {file_url.absolute_url}"
                )
            # NOT_FOUND is ok when the _error_raises_exception flag is cleared: silently skip not found item
        elif is_regular_file(item):
            await self._catalog_file_item(task_node, item, AbsoluteUrl(absolute_url=base), RelativeUrl(relative_url=relative))

    async def catalog_file_list_from_file(
        self, source_file: str, *, ignores: Optional[Union[str, IgnoreEvaluator]] = None
    ) -> Tuple[str, List[CatalogItem]]:
        # Read the file list, which is a tab separated file with two columns: base url and relative url of a file to catalog
        list_of_urls: List[Tuple[str, str]] = []
        file_list = await wrapp_read_text_file(source_file)
        root: Optional[str] = None
        for file_entry in file_list.splitlines(keepends=False):
            try:
                entries = file_entry.split("\t")
                if len(entries) < 2:
                    logger.warning(f"Ignoring line as it does not contain at least two tab separated values: {file_entry}")
                else:
                    base = entries[0]
                    relative = entries[1]
                    list_of_urls.append((base, relative))
                    if root is None:
                        root = base
                    elif root != base:
                        root = ""
            except ValueError:
                raise FailedCommand(
                    f"Failed to parse input file list {source_file}, line does not contain a tab to separate base path from relative path: {file_entry}"
                )
        if ignores is not None:
            if not root:
                raise FailedCommand("Failed to generate common root from file list - cannot apply ignore rules without common root!")
            ignore_rules = await create_ignore_eval_from_params(ignores, root)
        else:
            ignore_rules = None
        return root if root else "", await self.catalog_file_list(list_of_urls, ignore_rules=ignore_rules)

    async def catalog_file_tree(self, base_url: str, *, ignore_rules: Optional[IgnoreEvaluator] = None) -> List[CatalogItem]:
        self.progress_stats.directories_found = 1
        full_path = AbsoluteUrl(absolute_url=base_url)
        result, item = await wrapp_stat(full_path.absolute_url)
        if result == result.ERROR_NOT_FOUND:
            is_folder_path = await wrapp_url_is_folder(full_path.absolute_url)
            if is_folder_path:
                raise FailedCommand(f"catalog root URL must be a folder, not a file: {full_path.absolute_url}")
            else:
                raise FailedCommand(f"catalog root URL cannot be found: {full_path.absolute_url}")
        elif result != result.OK:
            raise StorageOperationError(f"result {result} when entering {full_path.absolute_url}")
        elif is_regular_file(item):
            raise FailedCommand(f"catalog root URL must be a folder, not a file: {full_path.absolute_url}")
        async with self._root_task_node.create_task_group_node(f"Cataloging file tree '{base_url}'") as task_node:
            await self._catalog_file_tree(task_node, full_path, RelativeUrl(relative_url=""), ignore_rules=ignore_rules)
        return self.result_list

    async def _catalog_file_tree(
        self,
        task_node: SchedulerTaskGroupNode,
        base_url: AbsoluteUrl,
        source_dir: RelativeUrl,
        *,
        ignore_rules: Optional[IgnoreEvaluator] = None,
    ) -> None:
        source_url = base_url.append(source_dir)
        need_manual_recurse = False
        if wrapp_list_file_tree_supported(source_url.absolute_url):
            items_generator = wrapp_list_file_tree(source_url.absolute_url)
        else:
            items_generator = wrapp_list_directory(source_url.absolute_url)
            need_manual_recurse = True
        async for item in items_generator:
            new_source_url: RelativeUrl = source_dir.append(RelativeUrl(relative_url=item.relative_path))
            source_file = base_url.append(new_source_url)
            if not item_is_ignored(source_dir.relative_url, RelativeUrl(relative_url=item.relative_path).relative_url, ignore_rules):
                if is_folder(item):
                    Stats.get().increment(StatType.FOLDERS)
                    self.progress_stats.directories_found += 1
                    if need_manual_recurse:
                        if self.verbose:
                            logger.info(f"Entering {source_file}...")
                        task_node.create_task(
                            self._catalog_file_tree(task_node, base_url, new_source_url, ignore_rules=ignore_rules),
                            name="Catalog file tree",
                        )
                elif is_regular_file(item):
                    task_node.create_task(self._catalog_file_item(task_node, item, base_url, new_source_url), name="Catalog file")
            else:
                if self.verbose:
                    logger.info(f"Ignoring {source_file.absolute_url}")

    def _add_local_hash_result(
        self,
        base_url: AbsoluteUrl,
        url: AbsoluteUrl,
        local_hash: str,
        filesize: int,
        modified_time: datetime,
        resource_identity: Optional[str],
        extra_hashes: Optional[Dict[str, str]] = None,
    ):
        if modified_time is not None and self.hash_cache is not None:
            self.hash_cache.set(url.absolute_url, filesize, modified_time, local_hash)
        relative_url = base_url.make_relative(url)
        if extra_hashes:
            self.content_hashes_map[relative_url.relative_url] = extra_hashes
        self.result_list.append(
            CatalogItem(
                relative_path=relative_url.relative_url,
                source_path=url.absolute_url,
                hash=local_hash,
                size=filesize,
                resource_identity=resource_identity,
            )
        )

    async def _check_download_required(
        self, task_node: SchedulerTaskGroupNode, base_url: AbsoluteUrl, url: AbsoluteUrl, resource_identity: Optional[str]
    ) -> None:
        result, item = await wrapp_stat(url.absolute_url)
        if result != Result.OK:
            raise FailedCommand(f"Failed to stat file '{url.absolute_url}' for check if hash calculation is required: {result}")
        assert item.modified_time is not None
        assert item.size is not None
        modified_time = item.modified_time

        if item.nucleus_hash is not None:
            # Ah, the stat gave us a Nucleus hash what the list couldn't. This is the case when we find the Nucleus
            # hash stored in the S3 Metadata. No need to download now.
            self._add_local_hash_result(base_url, url, item.nucleus_hash, item.size, modified_time, resource_identity)
            return

        if self.hash_cache is not None:
            cached_hash = self.hash_cache.get(url.absolute_url, item.size, item.modified_time)
            if cached_hash is not None:
                self.progress_stats.hash_cache_hits += 1
                self.progress_stats.num_bytes_hash_cache_hits += item.size
                self._add_local_hash_result(base_url, url, cached_hash, item.size, modified_time, resource_identity)
                return

        file_in_cache: bool = False
        file_locally_available = wrapp_can_be_cached(url.absolute_url)
        if file_locally_available:
            result, cache_file = await wrapp_get_cached_file(url.absolute_url, False)
            if result == Result.OK and cache_file is not None:
                file_in_cache = True
                task_node.create_task(
                    CPUThrottler.me().process_local_task_multi_threaded(
                        self._collect_local_hash_result,
                        self._compute_local_hash,
                        base_url,
                        url,
                        cache_file,
                        modified_time,
                        resource_identity,
                    )
                )
        if not file_in_cache:
            if self.verbose and not is_file_url(url.absolute_url):
                logger.info(f"Downloading item {url.absolute_url} to calculate local hash...")
            task_node.create_task(
                self._download_item(task_node, base_url, url, modified_time, item.size, resource_identity), name="Download item"
            )

    async def _download_item(
        self,
        task_node: SchedulerTaskGroupNode,
        base_url: AbsoluteUrl,
        url: AbsoluteUrl,
        modified_time: datetime,
        size: int,
        resource_identity: Optional[str],
    ) -> None:
        # If the client cache is not available, client-library will revert to its unmanaged local folder to store the downloaded files indefinitely.
        # We want to avoid that, so only download the file if the cache is available.
        hub_runs = wrapp_can_be_cached(url.absolute_url)
        if hub_runs:
            cache_file = await self._download_file(url)
            self.progress_stats.files_downloaded += 1
            self.progress_stats.num_bytes_downloaded += size
            task_node.create_task(
                CPUThrottler.me().process_local_task_multi_threaded(
                    self._collect_local_hash_result,
                    self._compute_local_hash,
                    base_url,
                    url,
                    cache_file,
                    modified_time,
                    resource_identity,
                )
            )
        else:
            # Download to a temp file to avoid holding large files in memory
            async with TempFileManager.me().create_temp_file() as tmp:
                result = await Router.me().storage_for_read(url.absolute_url).download_file(url.absolute_url, tmp.url, overwrite=True)
                if result != Result.OK:
                    raise FailedCommand(f"Failed to download file '{url.absolute_url}' for hash calculation: {result}")
                tmp.write_completed()
                self.progress_stats.files_downloaded += 1
                self.progress_stats.num_bytes_downloaded += size
                Stats.get().increment(StatType.READ_FILE)
                local_hash, filesize = self._compute_hash(tmp.path)
                self._add_local_hash_result(
                    base_url=base_url,
                    url=url,
                    local_hash=local_hash,
                    filesize=filesize,
                    modified_time=modified_time,
                    resource_identity=resource_identity,
                    extra_hashes=self._compute_extra_hashes(tmp.path),
                )

    @dataclass
    class ComputeLocalHashResult:
        base_url: str
        url: str
        local_hash: str
        filesize: int
        modified_time: datetime
        resource_identity: Optional[str]
        extra_hashes: Dict[str, str] = field(default_factory=dict)

    def _collect_local_hash_result(self, r: ComputeLocalHashResult) -> None:
        if self.verbose:
            logger.info(f"Calculated local hash for item {r.url}...")
        self._add_local_hash_result(
            AbsoluteUrl(absolute_url=r.base_url),
            AbsoluteUrl(absolute_url=r.url),
            r.local_hash,
            r.filesize,
            r.modified_time,
            r.resource_identity,
            r.extra_hashes,
        )

    def _compute_local_hash(
        self,
        base_url: AbsoluteUrl,
        url: AbsoluteUrl,
        cache_file: LocalReadOnlyFile,
        modified_time: datetime,
        resource_identity: Optional[str],
    ) -> ComputeLocalHashResult:
        try:
            path = cache_file.local_path()
            local_hash, filesize = self._compute_hash(path)
            return CatalogBuilder.ComputeLocalHashResult(
                base_url=base_url.absolute_url,
                url=url.absolute_url,
                local_hash=local_hash,
                filesize=filesize,
                modified_time=modified_time,
                resource_identity=resource_identity,
                extra_hashes=self._compute_extra_hashes(path),
            )
        finally:
            cache_file.close()

    def _compute_extra_hashes(self, path: str) -> Dict[str, str]:
        """Compute any requested extra content hashes for a local file (single read per algorithm)."""
        hashes: Dict[str, str] = {}
        if ContentHashAlgorithm.PACKAGE_STANDARD in self._content_hashes:
            with open(path, "rb") as f:
                package_standard_hashes, _ = calc_package_standard_hashes_from_stream(f)
            hashes.update(package_standard_hashes)
        return hashes

    @staticmethod
    async def _download_file(url: AbsoluteUrl) -> LocalReadOnlyFile:
        try:
            result, cache_file = await wrapp_get_cached_file(url.absolute_url)
            if result != Result.OK or cache_file is None:
                raise FailedCommand(f"Failed to download file '{url.absolute_url}' for hash calculation: {result}")
        except Exception as e:
            raise FailedCommand(f"Failed to download file '{url.absolute_url}' for hash calculation: {e}")
        return cache_file

    def _compute_hash_from_stream(self, f: BinaryIO) -> Tuple[str, int]:
        # All Nucleus Servers use 1MB has a chunk size for hashing
        if True:
            return calc_nucleus_hash_from_stream(f)
        else:
            # Keep implementation of MD5 for now
            m = hashlib.md5()
            content = f.read()
            size = len(content)
            m.update(content)
            local_hash = m.hexdigest()
            return local_hash, size

    def _compute_hash(self, path: str) -> Tuple[str, int]:
        with open(path, "rb") as f:
            return self._compute_hash_from_stream(f)

    @staticmethod
    async def add_tags_to_catalog(context: CommandParameters, scheduler: SchedulerContext, catalog: Catalog) -> None:
        # We need to sort out items by server, should the catalog contain urls of more than one server
        nucleus_hosts: Set[str] = set()
        paths: Dict[str, Dict[str, CatalogItem]] = {}
        for item in catalog.items:
            if not await wrapp_supports_tags(item.source_path):
                continue
            source_path = item.source_path
            split_url = urlparse(source_path)
            host = str(split_url.netloc)
            nucleus_hosts.add(host)
            if split_url.netloc not in paths:
                paths[host] = {}
            tag_path = split_url.path
            paths[host][tag_path] = item

        async def get_tags(host: str, path: str):
            task_result = await ts.get_tags([path])
            if task_result.status != "OK":
                raise StorageOperationError(f"Error communicating with the tagging service: status {task_result.status} for path {path}")
            for path_result in task_result.path_result:
                if path_result.connection_status not in [0, 1]:
                    # TODO: Is this a fatal error
                    logger.error(
                        f"Error when querying the tagging server: path_result {path_result.connection_status} {path_result.connection_status_string} for path {path}"
                    )
                    continue
                for tag in path_result.tags:
                    Stats.get().increment(StatType.TAGS)
                    tag_name = tag.name
                    tag_ns = tag.tag_namespace
                    tag_value = tag.value
                    if paths[host][path].tags is None:
                        paths[host][path].tags = []
                    # Store tag in catalog
                    item = paths[host][path]
                    if item is not None and item.tags is not None:
                        item.tags.append(Tag(name=tag_name, ns=tag_ns, value=tag_value))

        # Loop over all Nucleus hosts in the file set
        tag_services = []
        try:
            async with scheduler.parent_task_node().create_task_group_node() as tg:
                for host in nucleus_hosts:
                    # Find and contact the tagging service of that host
                    connection_url = f"omniverse://{host}"
                    ts = tagging.Tagging(context, host)
                    tag_services.append(ts)
                    await ts.connect(connection_url)
                    for tag_path in paths[host]:
                        tg.create_task(get_tags(host, tag_path), name="Get tags")
        finally:
            for ts in tag_services:
                await ts.disconnect()
