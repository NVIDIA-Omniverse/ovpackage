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
    List,
    Optional,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.delete_packages import unique_package_location_from_wrapp_file
from wrapp.commands.list_repo import list_repo
from wrapp.commands.mirror_impl.mirror_helper_objs import _MirrorContext
from wrapp.commands.mirror_impl.mirror_package_collection import (
    _MirrorPackageCollection,
)
from wrapp.datastructures import SchedulerContext
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    CommandParameters,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.package import (
    UniquePackageLocation,
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
    RequiredPackageFileOption,
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
    wrapp_stat,
    wrapp_urls_are_equivalent,
    wrapp_write_file_content,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_filename_from_url,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="find-orphans")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def find_orphans_command(
    seed_package: List[str] = RequiredPackageFileOption.better_help(
        help="URL of package file which will mark itself and its transitive dependencies as non-orphaned. Can be specified multiple times.",
    ),
    repo: List[str] = typer.Option(default=..., help="URL(s) of repositories to inspect for orphans."),
    output: str = typer.Option(None, help="If specified, don't print to stdout but write output into file given"),
    overwrite: bool = OverwriteFlag,
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
    In one or more repositories find orphaned packages not referenced directly or indirectly by a given list of top-level "seed" packages.
    """
    context, scheduler_params = context_from_params()
    try:
        if output:
            output_stat_result, output_item = await wrapp_stat(output)
            if output_stat_result != Result.ERROR_NOT_FOUND:
                if output_item.is_folder:
                    raise FailedCommand(f"output path {output} exists and is a folder, can't write")
                elif not overwrite:
                    raise FailedCommand(f"output file exists and --overwrite is not specified: {output}")

        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            parent_packages: list[UniquePackageLocation] = []
            async with scheduler.root_task_node().create_task_group_node(f"Validating {len(seed_package)} input packages") as task_node:
                for p in seed_package:
                    task_node.create_task(unique_package_location_from_wrapp_file(p, parent_packages), name="Validating input file")

            repos_to_inspect: list[Repository] = []
            for r in repo:
                repository = Repository(repo_url=r)
                repos_to_inspect.append(repository)

            orphan_list = await find_orphaned_package_versions(
                parent_packages, repos_to_inspect=[r.base_url() for r in repos_to_inspect], context=context, scheduler=scheduler
            )

        output_string = "\n".join(
            wrapp_file_in_repo(Repository(repo_url=orphan.repo_url), orphan.package_name, orphan.version) for orphan in orphan_list
        )
        if output:
            write_result = await wrapp_write_file_content(context, output, output_string)
            if write_result != Result.OK:
                raise StorageOperationError(f"can't write result file to {output}: {write_result}")
        else:
            print(output_string)

    except FailedCommand as e:
        logger.error(f"Failure to find orphans: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while running orphan search: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def find_orphaned_package_versions(
    locking_packages: list[UniquePackageLocation],
    repos_to_inspect: list[str],
    *,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> list[UniquePackageLocation]:
    """return all packages from all repos listed in the first parameter which are not needed to install the list of top-level package versions in the second parameter.
    The list of repos in the first parameter and the list of repos storing the packages in the second parameter are uncorrelated."""
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    # Step 1 - find out which packages are potentially orphaned by building a list of all packages in the repos we need to inspect
    all_packages = await _take_stock_of_existing_packages([Repository(repo_url=url) for url in repos_to_inspect], scheduler=scheduler)

    # Step 2 - Build the transitive list of all packages and their repos required by the given list of "locking" packages
    # We can use the mechanics implemented for the recursive mirror for this
    async def package_to_repo(package: UniquePackageLocation) -> str:
        # Mirror to self, we really do not neeed that
        return package.repo_url

    async with scheduler.parent_task_node().create_task_group_node(
        f"Find orphans in {len(repos_to_inspect)} repos"
    ) as find_orphans_task_node:
        collector = _MirrorPackageCollection(
            _MirrorContext(
                safe_package_to_repo_callback=package_to_repo,
                mirror_tags=False,
                context=context or CommandParameters(),
                scheduler=scheduler,
            )
        )

        async def _collect_packages_of_one_locking_package(task_node: SchedulerTaskGroupNode, package: UniquePackageLocation):
            await collector.discover_packages_to_mirror(task_node, package.package_name, package.version, package.repo_url, recursive=True)

        async with find_orphans_task_node.create_task_group_node("Collecting locked packages") as collect_task_node:
            for package in locking_packages:
                collect_task_node.create_task(_collect_packages_of_one_locking_package(collect_task_node, package))

    # Step 3 - Now we know all packages that are needed by the list of locking packages given, see if any of the
    # packages discovered in Step 1 are orphaned and not in use
    to_purge: list[UniquePackageLocation] = []
    for orphan_candidate in all_packages:
        # Check if the orphan candidate package is part of the mirror with dependencies set
        found = False
        for needed_package in collector.get_source_locations():
            if (
                orphan_candidate.package_name == needed_package.package_name
                and orphan_candidate.version == needed_package.version
                and await wrapp_urls_are_equivalent(orphan_candidate.repo_url, needed_package.repo_url)
            ):
                # This is needed, don't purge
                logger.info(
                    f"Keeping pre-existing package {orphan_candidate.package_name} version '{orphan_candidate.version}' in repo '{orphan_candidate.repo_url}', as it is referenced by the locking packages given."
                )
                found = True
        if not found:
            logger.info(
                f"Found orphaned package {orphan_candidate.package_name} version '{orphan_candidate.version}' in repo '{orphan_candidate.repo_url}'"
            )
            to_purge.append(orphan_candidate)
    return to_purge


async def _take_stock_of_existing_packages(
    destination_repos: list[Repository], *, scheduler: SchedulerContext
) -> list[UniquePackageLocation]:
    """Given a list of Repository items, find all versions of all packages stored in that repo and return it as a list of the RepositoryPackage objects."""

    async def _list_repo(url: str) -> tuple[str, list[tuple[str, list[str]]]]:
        r = await list_repo(url, scheduler=scheduler)
        return url, r

    task_results: dict[asyncio.Task, tuple[str, list[tuple[str, list[str]]]]] = {}
    async with scheduler.parent_task_node().create_task_group_node("Inspecting destination repositories", task_results) as task_node:
        for dest_repo in destination_repos:
            task_node.create_task(_list_repo(dest_repo.base_url()), name="Listing repository contents")
    existing_versions: list[UniquePackageLocation] = []
    for task, result in task_results.items():
        repo_url = result[0]
        list_repo_result = result[1]
        repo = Repository(repo_url=repo_url)
        for package_path, versions in list_repo_result:
            for version in versions:
                existing_versions.append(
                    UniquePackageLocation(package_name=get_filename_from_url(package_path), version=version, repo_url=repo.base_url())
                )
    return existing_versions
