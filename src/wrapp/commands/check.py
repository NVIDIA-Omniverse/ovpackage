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
from dataclasses import dataclass
from typing import (
    List,
    Optional,
)
from urllib.parse import quote

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.catalog import CatalogBuilder
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import (
    Catalog,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.dependency_graph import (
    collect_recursive_dependencies,
)
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    PackageInfo,
    is_package_file_name,
    load_package_setup,
    read_package_info,
    wrapp_file_in_repo,
)
from wrapp.datastructures.two_way_diff import Diff
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    HashCacheFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
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
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage import (
    Result,
    wrapp_stat,
    wrapp_urls_are_equivalent,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_base_directory_from_url,
    get_base_from_absolute_url,
    make_relative_url,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@dataclass
@wrapp_public_api
class CheckResult:
    failed: bool
    description: str | None = None


@app.command(name="check")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def check_command(
    package: str = typer.Argument(..., help="package name"),
    version: str = typer.Argument(..., help="version"),
    repo: str = RepoRequiredOption,
    recursive: bool = RecursiveOption,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    hash_cache_file: Optional[str] = HashCacheFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    stats: bool = StatsFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Given a package file URL, check that all files referenced by the package and have the correct content.
    """
    context, scheduler_params = context_from_params()
    try:
        # Check if we can find the package
        repo_object = Repository(repo_url=repo)
        wrapp_file = wrapp_file_in_repo(repo_object, package, version)
        result, item = await wrapp_stat(wrapp_file)
        if result != Result.OK:
            raise FailedCommand(f"Package file {wrapp_file} can not be found!")

        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            results = await check_package_content(wrapp_file, recursive, context=context, scheduler=scheduler)
        if results:
            for item in results:
                print(item.description)
            raise typer.Exit(1)
    except typer.Exit as e:
        raise e
    except FailedCommand as e:
        logger.error(f"Failure during check of package {package} {version}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error during check of package {package} {version}: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def check_package_content(
    wrapp_file: str,
    recursive: bool = False,
    *,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> list[CheckResult]:
    """
    Given a package file address, check that the content referenced by the package is available and correct.

    :param wrapp_file
    :param recursive: Should we check for nested packages
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    if context is None:
        # Default parameter
        context = CommandParameters()

    # Load the package file
    try:
        top_level: Optional[PackageInfo] = await load_package_setup(wrapp_file)
    except Exception as e:
        raise FailedCommand(f"Failed to load the package at {wrapp_file}: {e}") from e
    if top_level is None:
        raise FailedCommand(f"Failed to load the package at {wrapp_file}")

    async with scheduler.parent_task_node().create_task_group_node(
        f"Checking content for package '{top_level.name}'", expected_num_sub_jobs=(2 if recursive else 1)
    ) as check_package_content_task_node:
        results: list[CheckResult] = []
        if recursive:
            loaded_package = LoadedPackage(wrapp_file, top_level)
            async with check_package_content_task_node.create_task_group_node("Collecting dependencies") as collect_task_node:
                dependencies = await collect_recursive_dependencies(
                    collect_task_node, loaded_package, from_repo=True, dependency_upgrade_allowed=False, check_deps_exist_in_repo=None
                )

            all_wrapp_files = set()
            for files in dependencies:
                for file in files:
                    if is_package_file_name(file):
                        all_wrapp_files.add(file)

            async with check_package_content_task_node.create_task_group_node(
                "Checking packages", expected_num_sub_jobs=len(all_wrapp_files)
            ) as check_task_node:
                for sub_wrapp in all_wrapp_files:
                    package = await read_package_info(sub_wrapp)
                    if not package:
                        raise FailedCommand(f"Failed to load the package at {sub_wrapp}")
                    if not package.catalog:
                        raise FailedCommand(f"Package {sub_wrapp} is an invalid repository package, it has no catalog.")
                    location = get_base_directory_from_url(sub_wrapp)
                    check_task_node.create_task(
                        _check_single_package(check_task_node, context, location, package, results), name="Checking dependency"
                    )
        else:
            if not top_level.catalog:
                raise FailedCommand(f"Package {wrapp_file} is an invalid repository package, it has no catalog.")
        top_level_location = get_base_directory_from_url(wrapp_file)
        await _check_single_package(check_package_content_task_node, context, top_level_location, top_level, results)
    return results


async def _check_single_package(
    task_node: SchedulerTaskGroupNode, context: CommandParameters, location: str, top_level: PackageInfo, results: list[CheckResult]
):
    if not top_level.catalog:
        # This should never happen, all calls are checked already above, but mypy is unhappy
        raise FailedCommand(f"Package {top_level.name} is an invalid repository package, it has no catalog.")
    # Stat the catalog content and ensure all is there
    files_found = []
    with CatalogBuilder(
        task_node=task_node, verbose=context.verbose, local_hash=True, command_params=context, error_raises_exception=False
    ) as builder:
        catalog_items: list[tuple[str, str]] = []
        trusted_identity_items = []
        for item in top_level.catalog.items:
            # source_path is the logical catalog input. Validate that it still
            # belongs to this package even when a trusted identity makes an
            # address-integrity check unnecessary.
            relative = make_relative_url(top_level.catalog.root, item.source_path)
            if relative == item.source_path:
                try:
                    real_base = get_base_from_absolute_url(item.source_path, quote(item.relative_path, safe="/"))
                    if not await wrapp_urls_are_equivalent(real_base, location):
                        raise FailedCommand(
                            f"Catalog problem for package {top_level.name}: location of package is {location}, catalog root is {top_level.catalog.root}, but item is at {item.source_path}"
                        )
                    else:
                        logger.debug(
                            f"Fixing catalog root during check_single_package. Expected {location} but was {top_level.catalog.root}"
                        )
                        catalog_base = location
                except ValueError as e:
                    raise FailedCommand(
                        f"Catalog problem for package {top_level.name}: {item.relative_path} is not part of {item.source_path}, corrupt catalog?"
                    ) from e
            else:
                catalog_base = top_level.catalog.root

            if item.resource_identity_v2 is not None:
                # A resource identity is a trusted token for the exact bytes
                # recorded by the catalog. Checking whether storage still
                # serves those bytes is a deep availability audit, not an
                # address-integrity check.
                trusted_identity_items.append(item)
            else:
                catalog_items.append((catalog_base, item.source_path))

        files_found = trusted_identity_items + await builder.catalog_file_list(catalog_items)

    catalog_of_storage = Catalog(root=location, items=files_found)
    unique_to_a, unique_to_b, differing_items = Diff.diff_catalogs_internal(
        top_level.catalog, catalog_of_storage, Diff.not_to_compare_fields
    )
    for item_a in unique_to_a:
        results.append(CheckResult(failed=True, description=f"{item_a['source_path']} not found, file is missing from storage."))
    for item_b in unique_to_b:
        results.append(CheckResult(failed=True, description=f"Extra file at {item_b['source_path']}, file is not part of catalog."))
    for item_a, item_b in differing_items:
        compare_fields = Diff._build_compare_field_list([item_a], [item_b], Diff.not_to_compare_fields)
        reason = Diff.reason_for_compare_catalog_entries(item_a, item_b, compare_fields, "'In catalog'", "'On storage'")
        results.append(CheckResult(failed=True, description=f"{item_a['relative_path']} is modified on storage: {reason}"))
