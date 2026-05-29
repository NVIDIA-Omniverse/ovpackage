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
    Union,
)

import rich
import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.catalog import catalog as catalog_command
from wrapp.commands.freeze import (
    freeze_catalog,
    freeze_file_tree_as_subtask,
)
from wrapp.commands.publish_redirect import create_redirect_package_for_single
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.dependency_graph import (
    collect_dependencies_in_tree,
    collect_recursive_dependencies,
)
from wrapp.datastructures.ignore import (
    IgnoreEvaluator,
    create_ignore_eval_from_params,
)
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    Dependency,
    PackageInfo,
    load_package_setup,
    validate_package_and_version,
    wrapp_file_in_repo,
    wrapp_file_name,
    wrapp_file_path,
    write_package_info,
)
from wrapp.datastructures.workspace_analysis import (
    check_dirty_state,
    collect_workspace_info,
)
from wrapp.options import (
    AllowDirtySubpackagesFlag,
    AuthOption,
    CatalogOptionFlag,
    CheckSubpackages,
    DebugLevelFlag,
    FileTransferJobsNumber,
    IgnoreFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    NewVersionOption,
    PackageFileOption,
    ProgressReportOption,
    RepoOption,
    StatsFlag,
    TaggingJobsNumber,
    TagsFlag,
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
from wrapp.utils.storage import wrapp_url_exists
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_base_directory_from_url,
    join_paths,
    normalize_url,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="create")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def create_command(
    ctx: typer.Context,
    package_name: str = typer.Argument(None, help="Specify the name of the package to create"),
    version: str = typer.Argument(None, help="Specify the version of the package [any string]"),
    source: str = typer.Argument(None, help="A source URL or catalog to freeze"),
    package: str = PackageFileOption.better_help(
        "Alternatively specify the URL to a .wrapp file for the package for which to create a new version"
    ),
    new_version: str = NewVersionOption,
    catalog: bool = CatalogOptionFlag,
    tags: bool = TagsFlag.default_help(),
    ignore_file: str = IgnoreFileOption,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    check_subpackage: bool = CheckSubpackages,
    allow_dirty_subpackages: bool = AllowDirtySubpackagesFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    stats: bool = StatsFlag,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    repo: str = RepoOption,
    publish_redirect: Optional[str] = typer.Option(
        None,
        "--publish-redirect",
        "--pr-redirect",
        help="Also create a redirect package in the specified redirect repository URL",
    ),
    external_url: Optional[str] = typer.Option(
        None,
        "--external-url",
        "--eu",
        help="External base URL for redirect package storage_map (used with --publish-redirect)",
    ),
    time: bool = TimeFlag,
) -> None:
    """
    The create command catalogs a subtree and creates a frozen version. The destination directory for the freeze operation always is a package cache directory,
    which by default is in /.packages on the same network location as the source data. Change the location of this by specifying the repo option.
    """

    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            if package is not None:
                if package_name is not None:
                    raise FailedCommand(
                        f"Both package name was specified as '{package_name}' and a package URL was given as {package}: Please use either or"
                    )
                if catalog:
                    raise FailedCommand(f"A package URL was given as {package}: Don't specify --catalog")
                if version is not None:
                    raise FailedCommand(
                        f"Both package version was specified as '{version}' and a package URL was given as {package}: Please use either or"
                    )
                package_setup = await load_package_setup(package)
                if package_setup is None:
                    raise FailedCommand(f"Failed to load package file {package}")
                if new_version is not None:
                    package_setup.version = new_version
                source = get_base_directory_from_url(package)
                package_name = package_setup.name
                version = package_setup.version
            else:
                if new_version is not None:
                    raise FailedCommand(f"A new_version was specified, but this can only be used in conjunction with the --package switch")
                if package_name is None or version is None or source is None:
                    raise FailedCommand(
                        "Please specify package name, version, and source, or the --package parameter pointing at an existing package file."
                    )

            await create(
                package_name,
                version,
                source,
                catalog,
                tags,
                ignore_file,
                repo,
                check_subpackage=check_subpackage,
                skip_dirty_check=allow_dirty_subpackages,
                publish_redirect=publish_redirect,
                external_url=external_url,
                context=context,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        # Instead of the usage, we could also print the full help: But that would be strange in the shell mode
        # app(["create", "--help"])
        rich.print(ctx.get_usage() + "\n")
        logger.error(f"Failed creating package: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while creating package '{package_name}' version '{version}', operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


async def _remove_non_existing_top_level_dependencies(extended_setup: PackageInfo, source: str, scheduler_context: SchedulerContext):
    """Pre-pass: Make sure all top-level dependencies exist. Else remove them before continuing"""
    if extended_setup.dependencies:
        missing_deps: set[str] = set()
        async with scheduler_context.parent_task_node().create_task_group_node("Checking top level dependencies") as task_node:
            # Make sure extended_setup is not None before using it
            for dep in extended_setup.dependencies:
                task_node.create_task(_check_if_wrapp_file_exists(dep, source, missing_deps))
        cleaned_deps = [
            dep
            for dep in extended_setup.dependencies
            if wrapp_file_path(join_paths(source, dep.relative_destination), dep.package) not in missing_deps
        ]
        extended_setup.dependencies = cleaned_deps


async def _check_if_wrapp_file_exists(dep: Dependency, source, missing_deps: set[str]):
    sub_wrapp_file_path = wrapp_file_path(join_paths(source, dep.relative_destination), dep.package)
    if not await wrapp_url_exists(sub_wrapp_file_path):
        missing_deps.add(sub_wrapp_file_path)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def create(
    package_name: str,
    version: str,
    source: str,
    catalog: bool,
    tags: bool = False,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    repo: Optional[str] = None,
    check_subpackage: bool = False,
    skip_dirty_check: bool = False,
    publish_redirect: Optional[str] = None,
    external_url: Optional[str] = None,
    *,
    ignore_file: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Takes package name and version plus either a catalog or a root directory, creates a new package in the repository.

    The create command catalogs a subtree and creates a frozen version. The destination directory for the freeze operation
    always is a package cache directory, which by default is in /.packages on the same network location as the source data.
    Change the location of this by specifying the repo option.

    WRAPP does not capture folders in a package, only files. On creation, wrapp will scan the source tree for other wrapp files,
    and record all files found as subpackages, and store a dependency instead of the subpackage content itself. Those
    subpackages must not be "new" packages without a catalog, because then it would not be clear which files are part of the
    subpackage and which files are part of the top level package, as the top level package also will collect all files
    not already claimed by any subpackage regardless of their location in the source tree.

    :param package_name: Name of the package to create
    :param version:  Version of the package to create, can be any string identifier
    :param source: Root directory of source that will be copied
    :param catalog: Flag to indicate source points to a catalog file instead of a root directory
    :param tags: Set this to additionally query the tagging service and make tags part of the catalog
    :param ignores: Specify the name of an ignore file containing rules for ignoring items, or an IgnoreEvaluator object
    :param repo: Specify the URL of the repository where to create the package. If not specified, will use the root of the server of the source directory
    :param check_subpackage: Set this to true to add a final pass checking that all subpackages also exist in the target repository
    :param skip_dirty_check: Set this to true to skip checking if the package is in dirty state. The check is default when there is a top level wrapp file. This allows creating a package even if subpackages are dirty (modified or removed files).
    :param publish_redirect: Specify a redirect repository URL to also create a redirect package in that repository
    :param external_url: External base URL for redirect package storage_map (used with publish_redirect).
                        If specified, storage_map URLs will use this base instead of the source repo URL.
    :param ignore_file: Deprecated, use ignores instead. Specify the name of an ignore file containing rules for ignoring items
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("package_name", package_name)
    validate_non_empty_string("version", version)
    validate_non_empty_string("source", source)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    validate_package_and_version(package_name, version)
    repo_object = Repository(repo_url=repo, fallback_base=source)
    target_base_url = repo_object.package_url(package_name, version)

    package_file_name = f".{package_name}.wrapp"

    # First make room for the package to be created
    if await wrapp_url_exists(wrapp_file_in_repo(repo_object, package_name, version)):
        raise FailedCommand(f"Package already exists at: '{target_base_url}'")

    # Check that the source exists
    if not await wrapp_url_exists(source):
        raise FailedCommand(f"Cannot find source '{source}'")

    # Resolve ignores (handles deprecated ignore_file parameter)
    ignore_rules = await create_ignore_eval_from_params(ignores, source, ignore_file=ignore_file)

    # Task structure of catalog == True:
    #  freeze
    #  create catalog for new package
    # else
    #  analysing source folder
    #  freeze
    #  create catalog for new package
    expected_num_sub_jobs = 2 if catalog else 3
    if check_subpackage:
        expected_num_sub_jobs += 1
    scheduler = scheduler.create_sub_context_and_node(f"Create '{package_name}' '{version}'", expected_num_sub_jobs=expected_num_sub_jobs)

    # Now freeze the package
    extended_setup = None
    if catalog:
        catalog_content = await Catalog.load(source)
        await freeze_catalog(
            source_catalog=source,
            catalog_root=catalog_content.root,
            catalog_items=catalog_content.items,
            dest_dir=target_base_url,
            overwrite=False,
            copy_tags=tags,
            ignores=ignore_rules,
            context=context,
            scheduler=scheduler.create_sub_context_and_node(f"Copying {len(catalog_content.items)} files in catalog"),
        )
    else:
        # Check if the package is a named package, i.e. there is a .package.wrapp file that might contain dependencies
        named_package_file = wrapp_file_path(source, package_name)
        named_package_setup: LoadedPackage

        named_package_file_exists = await wrapp_url_exists(named_package_file)
        # Task structure if named_package_file_exists and skip_dirty_check:
        #  collect_workspace_info
        #  check_dirty_state
        # else
        #  collect_dependencies_in_tree
        expected_num_sub_jobs = 2 if named_package_file_exists and not skip_dirty_check else 1
        scheduler_analyse_source = scheduler.create_sub_context_and_node(
            "Analysing source folder", expected_num_sub_jobs=expected_num_sub_jobs
        )

        if named_package_file_exists:
            # Check that the package is not dirty
            if not skip_dirty_check:
                # Get workspace info (which we'll need later anyway)
                package, workspace_catalog, package_info, sub_package_list, extra_files = await collect_workspace_info(
                    named_package_file, ignore_rules, context=context, scheduler=scheduler_analyse_source
                )

                # Check for dirty state
                dirty_packages, dirty_map = await check_dirty_state(
                    package,
                    workspace_catalog,
                    sub_package_list,
                    extra_files,
                    get_base_directory_from_url(named_package_file),
                    ignore_rules,
                    scheduler=scheduler_analyse_source,
                )

                if len(dirty_packages) > 0:
                    all_errors = ""
                    for pkg in dirty_packages:
                        if pkg != named_package_file:
                            all_errors += "\n".join(str(error) for error in dirty_map[pkg])
                    if all_errors != "":
                        raise FailedCommand(f"Detected dirty content: {all_errors}")

                # Reuse the already loaded package and collected information
                named_package_setup = package
                extended_setup = package_info
            else:
                # This is a named package, load the dependency information
                named_package_loaded = await load_package_setup(named_package_file)
                if named_package_loaded is None:
                    raise FailedCommand(f"Could not load {named_package_file}")
                else:
                    named_package_setup = LoadedPackage(named_package_file, named_package_loaded)
        else:
            # Create an intermediate package, because we're working in an unnamed package
            named_package_setup = LoadedPackage(
                named_package_file,
                PackageInfo(name=package_name, version=version, catalog=None, dependencies=None, repository=repo_object.base_url()),
            )

        # If we didn't already collect dependencies in the dirty check, do it now
        if skip_dirty_check or not named_package_file_exists:
            # We have to traverse the source tree to find any non-declared dependencies aka .wrapp files
            extended_setup, _, _ = await collect_dependencies_in_tree(
                context,
                named_package_setup,
                source,
                task_node=scheduler_analyse_source.parent_task_node().create_tree_node("Analysing source folder for subpackages"),
            )

        # Pre-pass: Make sure all top-level dependencies exist. Else remove them before continuing
        # Make sure extended_setup is not None before using it
        if extended_setup is None:
            raise RuntimeError("Program error, extended_setup is None")
        await _remove_non_existing_top_level_dependencies(
            extended_setup, source, scheduler_analyse_source.create_sub_context_and_node("Remove no longer available top-level subpackages")
        )

        # Now load all files that should not be part of our created package because they are part of a dependency discovered
        skip_files: set[str] | None = None
        async with scheduler_analyse_source.parent_task_node().create_task_group_node("Collect all dependencies") as task_node:
            list_of_sets = await collect_recursive_dependencies(
                task_node,
                LoadedPackage(named_package_file, extended_setup),
                from_repo=False,
                dependency_upgrade_allowed=True,
                check_deps_exist_in_repo=repo_object if check_subpackage else None,
            )
        if len(list_of_sets) > 0:
            skip_files = set.union({named_package_file}, *list_of_sets)

        # Copy the non skipped files - those are part of the top level package
        await freeze_file_tree_as_subtask(
            scheduler.parent_task_node().create_tree_node("Copying files to repository"),
            source,
            target_base_url,
            ignore_rules,
            overwrite=False,
            copy_tags=tags,
            skip_files=skip_files,
            context=context,
        )

    # Catalog after freeze
    logger.info("Finished copying, now cataloging result...")

    scheduler_create_package = scheduler.create_sub_context_and_node("Creating catalog for new package")
    if await wrapp_url_exists(target_base_url):
        destination_catalog = await catalog_command(
            root_dir=target_base_url, tags=tags, ignores=ignore_rules, context=context, scheduler=scheduler_create_package
        )
    else:
        # Create an empty catalog
        destination_catalog = Catalog(root=target_base_url, items=[])

    # Remove the top level package file from the catalog, because it will be wrongly referenced in there as we generate a new version now
    for item in destination_catalog.items:
        if item.relative_path == package_file_name:
            destination_catalog.items.remove(item)
            break

    # In catalog mode, we need to update or create the wrapp file
    package_target_file = join_paths(target_base_url, package_file_name)
    if catalog:
        existing_package = await load_package_setup(package_target_file)
        if existing_package is not None:
            # Exists, update the package
            existing_package.catalog = destination_catalog
            existing_package.version = version
            existing_package.name = (
                package_name  # This could actually rename a package, as the source might have been called differently. Feature or bug?
            )
            existing_package.repository = repo_object.base_url()
            await write_package_info(package_target_file, existing_package, context)
        else:
            # This is a raw package without its own wrapp file, now it needs to get one
            package_source = PackageInfo(
                name=package_name, version=version, catalog=destination_catalog, dependencies=None, repository=repo_object.base_url()
            )
            await write_package_info(package_target_file, package_source, context)
    elif extended_setup is not None:
        # In dependency mode, we just write the extended package setup
        extended_setup.version = version
        extended_setup.name = (
            package_name  # This could actually rename a package, as the source might have been called differently. Feature or bug?
        )
        extended_setup.repository = repo_object.base_url()
        extended_setup.catalog = destination_catalog
        extended_setup.source = normalize_url(source)
        await write_package_info(package_target_file, extended_setup, context)
    else:
        raise RuntimeError("Program error, neither catalog nor extended_setup defined")

    if check_subpackage:
        created_package: Optional[PackageInfo] = await load_package_setup(package_target_file)
        if created_package:
            # Final verification - we must make sure that all packages referenced by the package_target_file exist
            # Run another verification step
            async with scheduler.parent_task_node().create_task_group_node("Verify subpackages exist") as task_node:
                await collect_recursive_dependencies(
                    task_node,
                    LoadedPackage(package_target_file, created_package),
                    from_repo=True,
                    dependency_upgrade_allowed=False,
                    check_deps_exist_in_repo=repo_object if check_subpackage else None,
                )
        else:
            raise FailedCommand(f"Cannot load package file written to target repository for final subpackage check: {package_target_file}")

    # Create redirect package if requested
    if publish_redirect:
        logger.info(f"Creating redirect package in {publish_redirect}...")
        created_package = await load_package_setup(package_target_file)
        if created_package:
            redirect_repo = Repository(repo_url=publish_redirect)
            redirect_package = create_redirect_package_for_single(
                source_package=created_package,
                source_repo_url=repo_object.base_url(),
                redirect_repo_url=publish_redirect,
                external_url=external_url,
            )
            redirect_wrapp_url = join_paths(redirect_repo.package_url(package_name, version), wrapp_file_name(package_name))
            await write_package_info(redirect_wrapp_url, redirect_package, context)
        else:
            raise FailedCommand(f"Cannot load package file written to target repository for redirect creation: {package_target_file}")
