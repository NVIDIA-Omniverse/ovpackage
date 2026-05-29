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
    Awaitable,
    Callable,
    List,
    Optional,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.mirror_impl.mirror_handler import _MirrorHandler
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.package import (
    UniquePackageLocation,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    DestinationRepoOption,
    FileTransferJobsNumber,
    HashCacheFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
    ResumeFlag,
    SourceRepoOption,
    StatsFlag,
    TaggingJobsNumber,
    TagsFlag,
    TemplateVersionOption,
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
    wrapp_normalize_url,
    wrapp_read_text_file,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


async def _parse_repo_mapping_file(
    repo_mapping: str, source_repo: str, dest_repo: str
) -> Callable[[UniquePackageLocation], Awaitable[str]]:
    try:
        mappings = json.loads(await wrapp_read_text_file(repo_mapping))
    except StorageOperationError:
        raise FailedCommand(f"Repository mapping file could not be read: {repo_mapping}")
    except json.JSONDecodeError as e:
        raise FailedCommand(f"Invalid JSON in repository mapping file: {e}")

    # Validate JSON structure
    if not isinstance(mappings, list):
        raise FailedCommand("Repository mapping file must contain a JSON array")

    repo_map: dict[str, str] = {(await wrapp_normalize_url(source_repo)): (await wrapp_normalize_url(dest_repo))}
    for i, mapping in enumerate(mappings):
        if not isinstance(mapping, dict):
            raise FailedCommand(f"Entry {i} in repository mapping must be a dictionary")
        if "source" not in mapping:
            raise FailedCommand(f"Entry {i} in repository mapping missing 'source' key")
        if "dest" not in mapping:
            raise FailedCommand(f"Entry {i} in repository mapping missing 'dest' key")
        if not isinstance(mapping["source"], str) or not mapping["source"]:
            raise FailedCommand(f"Entry {i} 'source' must be a non-empty string")
        if not isinstance(mapping["dest"], str) or not mapping["dest"]:
            raise FailedCommand(f"Entry {i} 'dest' must be a non-empty string")
        source_norm = await wrapp_normalize_url(mapping["source"])
        dest_norm = await wrapp_normalize_url(mapping["dest"])
        if repo_map.setdefault(source_norm, dest_norm) != dest_norm:
            raise FailedCommand(
                f"Entry {i} 'source' (normalized: '{source_norm}') was mapped before to '{repo_map[source_norm]}' instead of '{dest_norm}'"
            )

    # Create the callback function
    async def repo_mapping_callback(package: UniquePackageLocation) -> str:
        package_repo_url = package.repo_url

        # Try to find a matching mapping
        norm_url = await wrapp_normalize_url(package_repo_url)
        dest_url = repo_map.get(norm_url, None)
        if not dest_url:
            raise FailedCommand(
                f"No repository mapping found for package '{package.package_name}' "
                f"version '{package.version}' from repository '{package_repo_url}'"
            )
        return dest_url

    return repo_mapping_callback


@app.command(name="mirror")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def mirror_command(
    package_name: str = typer.Argument(..., help="Specify the name of the package"),
    version: str = typer.Argument(..., help="Specify the version of the package [any string]"),
    source_repo: str = SourceRepoOption,
    destination_repo: str = DestinationRepoOption,
    repo_mapping: Optional[str] = typer.Option(
        None,
        "--repo-mapping",
        help="JSON file containing repository mappings for subpackages (list of dicts with a 'source' and a 'dest' key). "
        "The source and destination repo of the main package are implicitly added to the list. Implies --recursive.",
    ),
    tags: bool = TagsFlag.default_help(),
    resume: bool = ResumeFlag,
    template_version: str = TemplateVersionOption,
    redirect_dependencies: bool = typer.Option(
        False,
        help="Rewrites all dependency repositories to the target, even if subpackages haven't been mirrored yet—may cause rewritten dependencies to point to missing packages.",
    ),
    recursive: bool = typer.Option(
        False,
        help="Automatically mirror all subpackages and rewrite all dependencies to the target repository. If True, redirect_dependencies and template_version are ignored.",
    ),
    max_concurrent_single_package_mirror_ops: int = typer.Option(
        20,
        "--mcspmo",
        help="The maximum number of concurrent single-package mirroring operations for recursive mirror operations. "
        "A lower number means more packages are completed if the operation is interrupted, and a follow-up 'mirror --resume' is faster. "
        "A higher number means the packages are transferred more optimal, particular if the mirrored packages are small, "
        "because more files can be transferred concurrently.",
    ),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    hash_cache_file: Optional[str] = HashCacheFileOption,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    This will copy a package version from one server's .packages directory into another server's .packages directory.
    """
    context, scheduler_params = context_from_params()
    try:
        # Create destination repo callback if repo_mapping is provided
        if repo_mapping:
            recursive = True
            destination_repo_or_callback: str | Callable[[UniquePackageLocation], Awaitable[str]] = await _parse_repo_mapping_file(
                repo_mapping, source_repo, destination_repo
            )
        else:
            destination_repo_or_callback = destination_repo

        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            if not recursive:
                await mirror(
                    package_name,
                    version,
                    source_repo,
                    destination_repo,
                    tags=tags,
                    template_version=template_version,
                    redirect_dependencies=redirect_dependencies,
                    resume=resume,
                    context=context,
                    scheduler=scheduler,
                )
            else:
                await mirror_with_dependencies(
                    package_name,
                    version,
                    source_repo,
                    destination_repo_or_callback,
                    tags=tags,
                    resume=resume,
                    max_concurrent_single_package_mirror_ops=max_concurrent_single_package_mirror_ops,
                    context=context,
                    scheduler=scheduler,
                )
    except FailedCommand as e:
        logger.error(f"Failure to mirror package '{package_name}' from '{source_repo}' to '{destination_repo}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(
            f"Error while mirroring package '{package_name}' from '{source_repo}' to '{destination_repo}', operation incomplete: {e}"
        )
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def mirror_with_dependencies(
    package_name: str,
    version: str,
    source_repo: str,
    destination_repo: str | Callable[[UniquePackageLocation], Awaitable[str]],
    tags: bool = False,
    resume: bool = False,
    max_concurrent_single_package_mirror_ops: int = 20,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Mirror a package with all its dependencies from a source to a destination repository (or several).

    :param package_name: Name of the package to copy
    :param version: Version of the package to copy
    :param source_repo: Specify the URL of the source repository, excluding the .packages directory
    :param destination_repo: Either the URL of the target repository for the source package and all its dependencies.
        Or an async callback function that returns the target repository URL for all mirrored packages and all their dependencies.
        The callback function must return for the mirrored package and all its recursive dependencies a valid repository URL. To not
        mirror a package, return its current repository URL from the UniquePackageLocation.
    :param tags: Set this to create the tags in the target repository.
    :param resume: Set this flag to make a differential copy by first cataloging the target repository package directory and then doing a differential copy
    :param max_concurrent_single_package_mirror_ops: The maximum number of concurrent single-package mirroring operations.
        A lower number means more packages are completed if the operation is interrupted, and a follow-up "mirror --resume" is faster.
        A higher number means the packages are transferred more optimal, particular if the mirrored packages are small,
        because more files can be transferred concurrently.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    # Task structure:
    #   Reading source packages (discover_packages_to_mirror)
    #   Checking existing packages
    #   Mirror missing packages
    async with scheduler.parent_task_node().create_task_group_node(
        f"Mirroring '{package_name}' '{version}' with deps", expected_num_sub_jobs=3
    ) as mirror_task_node:
        safe_package_to_repo_callback: Callable[[UniquePackageLocation], Awaitable[str]]
        if isinstance(destination_repo, str):
            if not destination_repo:
                raise FailedCommand("The destination_repo parameter cannot be empty")

            async def default_package_to_repo_callback(_: UniquePackageLocation) -> str:
                return destination_repo

            safe_package_to_repo_callback = default_package_to_repo_callback
        elif callable(destination_repo):

            # Make calling the user provided function safe - i.e. check the returned value for basic correctness.
            async def _safe_package_to_repo(package: UniquePackageLocation) -> str:
                destination_repo_url = await destination_repo(package)
                if not isinstance(destination_repo_url, str):
                    raise FailedCommand(
                        f"User callback returned invalid destination repo location for package '{package.package_name}' version '{package.version}' from repository '{package.repo_url}': returned value '{destination_repo_url}' is not a string, but of type {type(destination_repo_url).__name__}"
                    )
                if not destination_repo_url:
                    raise FailedCommand(
                        f"User callback returned invalid destination repo location for package '{package.package_name}' version '{package.version}' from repository '{package.repo_url}': empty string"
                    )
                return destination_repo_url

            safe_package_to_repo_callback = _safe_package_to_repo
        else:
            raise TypeError(
                f"The destination_repo parameter must be either a str or a Callable[[UniquePackageLocation], Awaitable[str]], but it is a {type(destination_repo).__name__}"
            )

        mirror_handler = _MirrorHandler(safe_package_to_repo_callback, tags, context, scheduler)

        # Phase 1: Discover all packages that need to be mirrored
        await mirror_handler.package_collection.discover_packages_to_mirror(
            mirror_task_node,
            package_name,
            version,
            source_repo,
            True,
        )

        # Phase 2: Check if there is more than one source packages with same name and version.
        # If there is, make sure if mirrored they would result in identical packages, and mark only one of them as necessary to mirror.
        # Packages in the context of the mirror operation are considered identical if the content files resulting from an install operation would be the same,
        # and if those files would come from the same repository.
        # If packages with the same name and version are not identical, raise a FailedCommand exception.
        await mirror_handler.package_collection.mark_overlapping_source_packages()

        # Phase 3: Check which packages already exist in the target repository.
        # The main purpose of this function is to check if a package already exists in the destination repo, and
        # if the existing package is identical with the package which would be the result of the mirror operation.
        # If a package already exists and is not identical, a FailedCommand exception is raised.
        await mirror_handler.package_collection.check_existing_packages(mirror_task_node)

        # Phase 4: Mirror the missing packages
        await mirror_handler.mirror_missing_packages(mirror_task_node, resume, max_concurrent_single_package_mirror_ops)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def mirror(
    package_name: str,
    version: str,
    source_repo: str,
    destination_repo: str,
    tags: bool = False,
    resume: bool = False,
    template_version: Optional[str] = None,
    *,
    redirect_dependencies: bool = False,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Take package name and version, and copy it from the source repository to the destination repository.

    :param package_name: Name of the package to copy
    :param version: Version of the package to copy
    :param source_repo: Specify the URL of the source repository, excluding the .packages directory
    :param destination_repo: Specify the URL of the target repository, again excluding the .packages directory name
    :param tags: Set this to create the tags in the target repository.
    :param resume: Set this flag to make a differential copy by first cataloging the target repository package directory and then doing a differential copy
    :param template_version: Optionally specify a directory to copy into the place first. This can accelerate creation if the template is on the same server and can be reused.
    :param redirect_dependencies: Set this to rewrite dependencies in the package mirrored to point to the destination repository (no check for existance is done!)
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    if not destination_repo:
        raise FailedCommand("The destination_repo parameter cannot be empty")

    source_repo_obj = Repository(repo_url=source_repo)

    # Initialize mirror_context with the one package we want to mirror
    async def _safe_package_to_repo_callback(p: UniquePackageLocation) -> str:
        return (
            destination_repo
            if redirect_dependencies or p == UniquePackageLocation(package_name, version, source_repo_obj.base_url())
            else p.repo_url
        )

    mirror_handler = _MirrorHandler(_safe_package_to_repo_callback, tags, context, scheduler)

    unique_location = UniquePackageLocation(package_name=package_name, version=version, repo_url=source_repo_obj.base_url())
    package_to_mirror = await mirror_handler.package_collection.add_source_package(unique_location, "")

    async with scheduler.parent_task_node().create_task_group_node(f"Mirror {package_name} {version}") as mirror_task_node:
        await mirror_handler.package_collection.check_existing_packages(mirror_task_node)

        package_to_mirror.template_version = template_version

        # Mirror the package
        await mirror_handler.mirror_single_package(mirror_task_node, package_to_mirror, resume)
