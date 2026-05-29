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
    Any,
    List,
    Optional,
    Set,
    Union,
)
from urllib.parse import urlparse

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.catalog import catalog
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
from wrapp.datastructures.ignore import (
    IgnoreEvaluator,
    create_ignore_eval_from_params,
    item_is_ignored,
)
from wrapp.datastructures.loaded_package import (
    LoadedPackage,
    collect_wrapp_files_in_tree,
)
from wrapp.options import (
    AuthOption,
    CatalogOptionFlag,
    DebugLevelFlag,
    FileTransferJobsNumber,
    ForceUploadFlag,
    IgnoreFileOption,
    IgnorePackagesFlag,
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
from wrapp.tagging_service.tagging import (
    Tag,
    Tagging,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.scheduler_task_node import (
    SchedulerTaskGroupNode,
    SchedulerTreeNode,
)
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    Result,
    wrapp_copy_item_optimized,
    wrapp_list_directory,
    wrapp_stat,
    wrapp_supports_tags,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    drop_parameters_from_url,
    is_folder,
    is_regular_file,
    join_paths,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="freeze")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def freeze_command(
    source: str = typer.Argument(..., help="source URL to copy, e.g. omniverse://localhost/assets/lib1"),
    dest_dir: str = typer.Argument(..., help="destination URL to copy to, another omniverse URL or file URL like file:///c:/tmp/build_dir"),
    catalog: bool = CatalogOptionFlag,
    overwrite: bool = OverwriteFlag,
    tags: bool = TagsFlag.default_help(),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    ignore_file: str = IgnoreFileOption,
    ignore_packages: bool = IgnorePackagesFlag,
    force_upload: bool = ForceUploadFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Freeze a version of a package for later publishing or preparing a reproducible build
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await freeze(
                source,
                dest_dir,
                catalog=catalog,
                overwrite=overwrite,
                tags=tags,
                ignores=ignore_file,
                ignore_packages=ignore_packages,
                force_upload=force_upload,
                context=context,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        logger.error(f"Failed to freeze {source} to {dest_dir}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while freezing {source} to {dest_dir}, operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def freeze(
    source: str,
    dest_dir: str,
    catalog: bool = False,
    overwrite: bool = False,
    tags: bool = False,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    ignore_packages: bool = False,
    force_upload: bool = False,
    context: CommandParameters = CommandParameters(),
    *,
    ignore_file: Optional[str] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Taking a source and a destination, freeze a version of a package for later publishing or preparing a reproducible build

    :param source: URL of the root of the files to freeze
    :param dest_dir: URL of the destination
    :param catalog: When set, the source parameter points a file with a catalog previously produced by catalog, else source is a source folder URL
    :param overwrite: Specify to allow overwriting files in the destination
    :param tags: Specify whether to freeze tags via the Tagging Service as well
    :param ignores: Specify the name of an ignore file containing rules for ignoring items, or an IgnoreEvaluator object
    :param ignore_packages: When specified, first find all package files in the source and load their file lists to exclude them from the freeze operation
    :param force_upload: Specify this to disable the create_with_hash optimization that can accelerate uploads when files might already be on the target
    :param context: Global configuration parameters
    :param ignore_file: Deprecated, use ignores instead. Specify the name of an ignore file containing rules for ignoring items
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    # Resolve ignores (handles deprecated ignore_file parameter)
    ignore_rules = await create_ignore_eval_from_params(ignores, source, ignore_file=ignore_file)

    if await wrapp_url_exists(dest_dir) and not overwrite:
        raise FailedCommand(f"Destination directory {dest_dir} already exists and overwrite is not specified!")

    if catalog:
        catalog_content = await Catalog.load(source)
        await freeze_catalog(
            source_catalog=source,
            catalog_root=catalog_content.root,
            catalog_items=catalog_content.items,
            dest_dir=dest_dir,
            overwrite=overwrite,
            copy_tags=tags,
            ignores=ignore_rules,
            force_upload=force_upload,
            context=context,
            scheduler=scheduler,
        )
    else:
        skip_files = []
        tn = scheduler.parent_task_node().create_tree_node(f"Freeze file tree '{source}'")
        if ignore_packages:
            real_dependencies: List[LoadedPackage] = await collect_wrapp_files_in_tree(context, tn, source)
            for dep in real_dependencies:
                if dep.package.catalog is not None:
                    skip_files.extend([join_paths(dep.install_base, item.relative_path) for item in dep.package.catalog.items])
                else:
                    logger.info(f"Not skipping files from dependency {dep.wrapp_file_url} because it has no catalog")

        await freeze_file_tree(
            source,
            dest_dir,
            ignore_rules,
            overwrite=overwrite,
            copy_tags=tags,
            skip_files=set(skip_files),
            force_upload=force_upload,
            context=context,
            scheduler=scheduler.create_sub_context(tn),
        )


class FreezeTaskResult:
    result: Result
    error_msg: Optional[str]

    def __init__(self, result: Result, op_desc: str):
        self.result = result
        if self.result != Result.OK:
            self.error_msg = f"Failed to {op_desc}: {self.result.name}"


def process_results(results: dict[asyncio.Task, Any], op_desc: str) -> None:
    error_results: List[FreezeTaskResult] = []
    for task, result in results.items():
        if not isinstance(result, FreezeTaskResult):
            raise TypeError(f"Task {task.get_coro()} returned not a result of type FreezeTaskResult but {result}, program error")
        r: FreezeTaskResult = result
        if r.result != Result.OK:
            error_results.append(r)
            logger.error(r.error_msg)
    if not error_results:
        return
    logger.error(f"Failed to {op_desc} due to the {len(error_results)} error(s) listed above.")
    raise StorageOperationError(f"{error_results[0].error_msg}. Occurred while running {op_desc}. See log for more details.")


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def freeze_file_tree(
    source: str,
    dest_dir: str,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    overwrite: bool = False,
    copy_tags: bool = False,
    skip_files: Optional[Set[str]] = None,
    force_upload: bool = False,
    *,
    ignore_file: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Taking a source and a destination, freeze a version of a package for later publishing or preparing a reproducible build

    :param ignores: Specify the name of a file containing ignore rules, or an IgnoreEvaluator object
    :param source: URL of the root of the files to freeze
    :param dest_dir: URL of the destination
    :param overwrite: Specify to allow overwriting files in the destination
    :param copy_tags: Specify whether to freeze tags via the Tagging Service as well
    :param skip_files: Specify a list of files with their relative paths within the source directory that should not be part of the destination
    :param force_upload: Specify this to disable the create_with_hash optimization that can accelerate uploads when files might already be on the target
    :param ignore_file: Deprecated, use ignores instead. Specify the name of a file containing ignore rules
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    # Resolve ignores (handles deprecated ignore_file parameter)
    ignore_rules = await create_ignore_eval_from_params(ignores, source, ignore_file=ignore_file)

    await freeze_file_tree_as_subtask(
        scheduler.parent_task_node(), source, dest_dir, ignore_rules, overwrite, copy_tags, skip_files, force_upload, context=context
    )


async def freeze_file_tree_as_subtask(
    task_node: SchedulerTreeNode,
    source: str,
    dest_dir: str,
    ignore_rules: Optional[IgnoreEvaluator] = None,
    overwrite: bool = False,
    copy_tags: bool = False,
    skip_files: Optional[Set[str]] = None,
    force_upload: bool = False,
    *,
    context: CommandParameters = CommandParameters(),
) -> None:
    # We need to check if it is a file or a directory
    result, item = await wrapp_stat(source)
    if result != Result.OK:
        raise FailedCommand(f"Error: Source {source} not found!")
    if not is_folder(item):
        raise FailedCommand("freeze only supports copying folders, not individual files")
    if copy_tags and not await wrapp_supports_tags(dest_dir) and await wrapp_supports_tags(source):
        raise FailedCommand(
            f"Freezing content from a storage supporting tags {source} to a storage not supporting tags {dest_dir} is only allowed without tags."
        )
    if copy_tags:
        logger.info("Pass 1: Copy files...")

    logger.info(f"Freezing package in {source} to {dest_dir}...")

    task_results: dict[asyncio.Task, FreezeTaskResult] = {}
    async with task_node.create_task_group_node(f"Freezing from {source}", task_results=task_results) as sub_task_node:
        sub_task_node.create_task(
            _copy_internal(
                sub_task_node,
                context,
                source,
                dest_dir,
                ignore_rules,
                overwrite,
                skip_files=skip_files,
                force_upload=force_upload,
                depth=0,
            ),
            name="Copy folder",
        )
    process_results(task_results, f"copy '{source}' to '{dest_dir}'")

    if copy_tags:
        # We are doing copy that is not on the same server. For this, we need to catalog the tags of the source and copy them over!
        logger.info("Pass 2: Catalog and copy tags...")
        source_catalog = await catalog(root_dir=source, tags=True, ignores=ignore_rules)
        await _copy_tags(context, task_node, source_catalog.items, dest_dir)


async def _copy_tags(context: CommandParameters, task_node: SchedulerTreeNode, catalog_items: List[CatalogItem], dest_dir: str) -> None:
    if not await wrapp_supports_tags(dest_dir):
        if any([item.tags is not None for item in catalog_items]):
            # Do not rely on this - ideally the _copy_tags function is not called for a target not supporting tags,
            # because it means that all data is copied and freeze only fails at the very end of the operation.
            raise FailedCommand(f"Tags are not available for '{dest_dir}'")
        # There is nothing to do.
        return

    target = urlparse(dest_dir)
    target_host = target.netloc
    ts = None
    try:
        # Create the tagging service client and connect
        ts = Tagging(context, target_host)
        await ts.connect(dest_dir)

        async with task_node.create_task_group_node() as sub_task_node:
            for item in catalog_items:
                if item.tags is not None:
                    # source_is_nucleus = is_nucleus_url(item['source_path'])
                    copy_target = dest_dir + "/" + item.relative_path
                    # TODO - surprisingly, this does not work as expected. The apply test fails on the second install step to automatically get the tags
                    # if source_is_nucleus and target_is_nucleus:
                    #    source = urlparse(item['source_path'])
                    #    source_host = source.netloc
                    #    if source_host == target_host:
                    #        # Nothing to do, as the tagging service will do the copy automatically
                    #        if verbose:
                    #            print(f"Skipping tag copy from {item['source_path']} to {copy_target} because it is the same server")
                    #        continue
                    # Need to write tags to destination using the tagging service of the target
                    dest = urlparse(copy_target)
                    tags = []
                    for tag in item.tags:
                        Stats.get().increment(StatType.TAGS)
                        tags.append(Tag(name=tag.name, tag_namespace=tag.ns, value=tag.value))
                    logger.info(f"Setting tags{tags} on {dest.path}")
                    sub_task_node.create_task(ts.set_tags(dest.path, tags), name="Set tags")
    finally:
        if ts:
            await ts.disconnect()


async def _copy_item_task(
    context: CommandParameters,
    copy_source: str,
    copy_target: str,
    hash: Union[str, None],
    overwrite: bool = False,
    force_upload: bool = False,
) -> FreezeTaskResult:
    return FreezeTaskResult(
        await wrapp_copy_item_optimized(context, copy_source, copy_target, hash, overwrite, force_upload),
        f"copy '{copy_source}' to '{copy_target}'",
    )


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def freeze_catalog(
    source_catalog: str,
    catalog_root: str,
    catalog_items: List[CatalogItem],
    dest_dir: str,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    overwrite: bool = False,
    copy_tags: bool = False,
    ignore_existing_folders: bool = False,
    force_upload: bool = False,
    *,
    ignore_file: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Taking a source and a destination, freeze a version of a package for later publishing or preparing a reproducible build

    :param source_catalog: File URL of the catalog file
    :param catalog_root: The root URL for all
    :param catalog_items: List of item objects that should become part of the frozen version
    :param dest_dir: URL of the destination
    :param ignores: Specify the name of an ignore file containing rules for ignoring items, or an IgnoreEvaluator object
    :param overwrite: Specify to allow overwriting files in the destination
    :param copy_tags: Specify whether to freeze tags via the Tagging Service as well
    :param ignore_existing_folders: Deprecated, no longer used. Previously used to ignore if folders in the destination already exist
    :param force_upload: Specify this to disable the create_with_hash optimization that can accelerate uploads when files might already be on the target
    :param ignore_file: Deprecated, use ignores instead. Specify the name of an ignore file containing rules for ignoring items
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    # Resolve ignores (handles deprecated ignore_file parameter)
    ignore_rules = await create_ignore_eval_from_params(ignores, catalog_root, ignore_file=ignore_file)

    if copy_tags and not await wrapp_supports_tags(dest_dir) and await wrapp_supports_tags(catalog_root):
        raise FailedCommand(
            f"Freezing content from a storage supporting tags {catalog_root} to a storage not supporting tags {dest_dir} is only allowed without tags."
        )

    logger.info(f"Freezing catalog {source_catalog} to {dest_dir}...")
    task_results: dict[asyncio.Task, FreezeTaskResult] = {}
    created_tasks = 0
    async with scheduler.parent_task_node().create_task_group_node(f"Freezing {len(catalog_items)} items", task_results) as task_node:
        for source_item in catalog_items:
            if not item_is_ignored(catalog_root, source_item.relative_path, ignore_rules):
                destination_path = join_paths(dest_dir, drop_parameters_from_url(source_item.relative_path))
                task_node.create_task(
                    _copy_item_task(
                        context,
                        source_item.source_path,
                        destination_path,
                        source_item.hash,
                        overwrite,
                        force_upload,
                    ),
                    name="Copy file",
                )
                if created_tasks % 50 == 0:
                    # This is to allow the progress report in the CLI to kick in from time to time.
                    # In the case of freezing from local to S3, the WRAPP thread gets blocked otherwise
                    # because this is tight loop, and for kion_warehouse the loop adds 27000 tasks
                    # which are executed immediately without breaking which blocks the progress report
                    # for a few minutes. Basically make this loop a well-behaved async function
                    # not hogging the thread for a long time.
                    await asyncio.sleep(0)
                created_tasks += 1
            else:
                logger.info(f"Ignoring {source_item.relative_path}")
    process_results(task_results, f"copy catalog '{source_catalog}' to '{dest_dir}'")

    if copy_tags:
        async with scheduler.parent_task_node().create_task_group_node("Copying tags") as task_node:
            await _copy_tags(context, task_node, catalog_items, dest_dir)


async def _copy_internal(
    task_node: SchedulerTaskGroupNode,
    context: CommandParameters,
    source_dir: str,
    dest_dir: str,
    ignore_rules: Optional[IgnoreEvaluator],
    overwrite: bool = False,
    skip_files: Optional[Set[str]] = None,
    force_upload: bool = False,
    depth: int = 0,
) -> FreezeTaskResult:
    # Find out which files are not ignored/skipped
    non_skipped_files = []
    folders_to_recurse_into = []
    try:
        async for item in wrapp_list_directory(source_dir):
            copy_source = join_paths(source_dir, item.relative_path)
            if skip_files is None or copy_source not in skip_files:
                non_skipped_files.append(item)
            elif is_folder(item):
                folders_to_recurse_into.append(item)
    except StorageOperationError as e:
        return FreezeTaskResult(e.result, f"access directory {source_dir}")

    # Now traverse over the non-ignored files
    for item in non_skipped_files + folders_to_recurse_into:
        if not item_is_ignored(source_dir, item.relative_path, ignore_rules):
            copy_source = join_paths(source_dir, item.relative_path)
            copy_target = join_paths(dest_dir, item.relative_path)
            if is_folder(item):
                # Recurse
                task_node.create_task(
                    _copy_internal(
                        task_node,
                        context,
                        copy_source,
                        copy_target,
                        ignore_rules,
                        overwrite,
                        skip_files=skip_files,
                        force_upload=force_upload,
                        depth=depth + 1,
                    ),
                    name="Copy folder",
                )
            elif is_regular_file(item):
                logger.info(f"Copying {copy_source} to {copy_target}")
                task_node.create_task(
                    _copy_item_task(
                        context,
                        copy_source,
                        copy_target,
                        item.nucleus_hash,
                        overwrite,
                        force_upload,
                    ),
                    name="Copy file",
                )
        else:
            logger.info(f"Skipping {item.relative_path} because of ignore rules specified")

    return FreezeTaskResult(Result.OK, f"copy folder '{source_dir}' to '{dest_dir}'")
