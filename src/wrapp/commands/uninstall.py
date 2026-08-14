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
    Set,
)

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
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.dependency_graph import (
    DependencyGraph,
    DependencyNode,
    collect_dependencies_in_tree,
)
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    PackageInfo,
    find_dependency,
    load_package_setup,
    wrapp_file_path,
    write_package_info,
)
from wrapp.datastructures.package_standard import (
    list_package_standard_metadata_files,
    package_standard_metadata_folder_url,
    package_standard_sidecar_paths,
)
from wrapp.datastructures.two_way_diff import Diff
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    ForceFlag,
    HashCacheFileOption,
    IgnoreFileOption,
    IgnoreTagsFlag,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    PackageFileOption,
    ProgressReportOption,
    StatsFlag,
    TaggingJobsNumber,
    TagsFlag,
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
    wrapp_delete_item,
    wrapp_list_directory,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    all_parent_directories,
    get_base_directory_from_url,
    join_paths,
    make_relative_url,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="uninstall")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def uninstall_command(
    package_name: str = typer.Argument(..., help="Specify the name of the package to uninstall"),
    install_location: str = typer.Argument(..., help="Specify the directory in which the package had been installed"),
    package: str = PackageFileOption.better_help(
        help="Additionally, specify the package file from which to remove the package dependency."
    ),
    tags: bool = TagsFlag.better_help(
        help="Run additional check that no tags have been modified, else uninstall will fail when force not specified",
    ),
    ignore_file: str = IgnoreFileOption,
    ignore_tags: bool = IgnoreTagsFlag.better_help(
        help="Specify this to uninstall without checking tags in the installation directory, tagging info might be lost"
    ),
    force: bool = ForceFlag.better_help(help="Ignore local modifications, delete files anyway"),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    hash_cache_file: Optional[str] = HashCacheFileOption,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Any package that has been installed can be uninstalled again. There are two modes of uninstallation: Via the directory in which the package has been installed, or
    via pointing to the dependency file which had been used to record the install operation. Then uninstall will also remove the
    dependency information recorded in that file.
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await uninstall(
                package_name,
                install_location,
                package,
                tags=tags,
                ignore_file=ignore_file,
                ignore_tags=ignore_tags,
                force=force,
                context=context,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        logger.error(f"Failure to uninstall: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Server error, operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def uninstall(
    package_name: str,
    install_location: str,
    package: Optional[str] = None,
    tags: bool = False,
    ignore_file: Optional[str] = None,
    ignore_tags: bool = False,
    force: bool = False,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Given a package name and either an install_location or the URL of a wrapp file, uninstall that package after checking it has no local modifications that could be lost

    Any package that has been installed can be uninstalled again. There are two modes of uninstallation: Via the directory in which the package has been installed, or
    via pointing to the dependency file which had been used to record the install operation. Then uninstall will also remove the
    dependency information recorded in that file.

    :param package_name: The name of the package to uninstall
    :param install_location: If install_location is specified as the URL of the package to uninstall, the package will be removed from that install location
    :param package: If no install_location is given, specify the URL of a package file with a dependency to the package to uninstall
    :param tags: If set, check before uninstalling if any tags have been set and are different from the install source. Refuse to uninstall in case of modification in tags.
    :param ignore_file: Specify the name of an ignore file containing rules for ignoring items in the installed package
    :param ignore_tags: If specified, don't compare the package catalog and the installed location's tags before allowing to uninstall.
    :param force: Specify this to force uninstall - warning, this might potentially lose data!
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("package_name", package_name)
    validate_non_empty_string("install_location", install_location)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    package_info: Optional[PackageInfo] = None
    if package is not None:
        package_info = await load_package_setup(package)
        if package_info is None:
            raise FailedCommand(f"Failed to find package file at {package}")
        else:
            relative_install_location = make_relative_url(get_base_directory_from_url(package), install_location)
            dep = find_dependency(package_info, package_name, relative_install_location)
            if dep is None:
                raise FailedCommand(f"{package} contains no dependency to package '{package_name}', can't uninstall")

            # Load the catalog of the installed package
            installed_package_file = f".{dep.package}.wrapp"
            loaded_package = LoadedPackage(package, package_info)
            install_destination = loaded_package.dependency_install_path(dep)
            installed_package_file_url = join_paths(install_destination, installed_package_file)
    else:
        installed_package_file = f".{package_name}.wrapp"
        installed_package_file_url = join_paths(install_location, installed_package_file)

    installed_package_info: Optional[PackageInfo] = await load_package_setup(installed_package_file_url)
    # Task structure:
    # if installed_package_info is not None and installed_package_info.catalog is not None:
    #   Analysing install location
    # Uninstalling dependencies
    # Removing files
    if installed_package_info is not None and installed_package_info.catalog is not None:
        expected_num_sub_jobs = 3
    else:
        expected_num_sub_jobs = 2
    scheduler = scheduler.create_sub_context_and_node(f"Uninstalling {package_name}", expected_num_sub_jobs=expected_num_sub_jobs)

    if installed_package_info is None:
        if package and force:
            # The package is gone, not much we can do to uninstall this. But when force is specified, we need to at least remove the dependency
            relative_install_location = make_relative_url(get_base_directory_from_url(package), install_location)
            await remove_dependency_from_package_file(context, package, package_name, relative_destination=relative_install_location)
            return
        else:
            raise FailedCommand(f"Error: package file not found at {installed_package_file_url}")
    elif installed_package_info.catalog is None:
        raise FailedCommand(f"Error: Package {installed_package_file_url} does not contain catalog, can't determine items to uninstall")
    else:
        # Now, catalog the installed location to check that it is unmodified
        installed_catalog: Catalog = await catalog(
            root_dir=install_location,
            checkpoints=False,
            tags=tags,
            ignores=ignore_file,
            context=context,
            scheduler=scheduler.create_sub_context_and_node(f"Analysing install location for '{package_name}'"),
        )

    # Diff the two catalogs
    ignore_fields = Diff.not_to_compare_fields
    if ignore_tags:
        ignore_fields.append("tags")
    unique_to_a, unique_to_b, differing_items = Diff.diff_internal(
        [item.model_dump() for item in installed_package_info.catalog.items],
        [item.model_dump() for item in installed_catalog.items],
        fields_to_ignore=ignore_fields,
    )
    # Print out errors
    for ua in unique_to_a:
        logger.info(f"Item from package was removed manually, no need to uninstall file: {ua['relative_path']}")
    dont_delete = set()
    for ub in unique_to_b:
        if ub["relative_path"] != installed_package_file:
            dont_delete.add(ub["relative_path"])
            for parent in all_parent_directories(ub["relative_path"]):
                dont_delete.add(parent)
    for diff in differing_items:
        if not force:
            logger.error(f"File modified after installation, can't uninstall: {diff[1]['relative_path']}")
            if context.verbose:
                Diff.print_diff(diff, ignore_fields)
        else:
            logger.info(f"Forced uninstall of modified file: {diff[1]['relative_path']}")
    if len(differing_items) > 0 and not force:
        raise FailedCommand(f"Found {len(differing_items)} modified item(s), can't uninstall package {package_name}")

    scheduler_uninstall_deps = scheduler.create_sub_context_and_node(f"Uninstalling dependencies of '{package_name}'")
    if package is not None and package_info is not None:
        scheduler_analyse_package = scheduler_uninstall_deps.create_sub_context_and_node("Analysing package with dependency")
        # Make sure to not delete anything that is referenced by one of the other packages that remains after uninstalling this package
        # For this, build a new Dependency graph from the top level wrapp file but prune the tree to be removed
        top_level_root = get_base_directory_from_url(package)
        top_level_package = LoadedPackage(package, package_info)
        all_packages_before_uninstall, list_of_wrapp_files, _ = await collect_dependencies_in_tree(
            context, top_level_package, top_level_root, scheduler_analyse_package.parent_task_node()
        )
        top_level_package_plus_extras = LoadedPackage(package, all_packages_before_uninstall)
        # Ignore version conflicts as this might be a working folder without a finished package setup
        all_package_graph = await DependencyGraph.build_dependency_graph(
            context,
            scheduler_analyse_package.parent_task_node(),
            top_level_package_plus_extras,
            top_level_root,
            ignore_version_conflicts=True,
        )

        # Determine any extra roots, i.e. packages that are not referenced by the top level package yet
        conflicts, extras = all_package_graph.find_version_conflicts()

        # Now remove the package to be uninstalled from the tree, and check if we have more orphaned packages than before
        all_package_graph.remove_package(installed_package_info, install_location)
        new_conflicts, new_extras = all_package_graph.find_version_conflicts()
        extra_extras = set(new_extras) - set(extras)
        if len(extra_extras) > 0:
            for extra_node_id in extra_extras:
                dependency_to_uninstall = DependencyNode.from_id(extra_node_id)
                await uninstall(
                    dependency_to_uninstall.package,
                    install_location=dependency_to_uninstall.url,
                    package=None,
                    tags=tags,
                    ignore_file=ignore_file,
                    ignore_tags=ignore_tags,
                    force=force,
                    context=context,
                    scheduler=scheduler_uninstall_deps.create_sub_context_and_node(
                        f"Removing subpackage '{dependency_to_uninstall.package}'"
                    ),
                )

        # Add all files from the packages that should stay after uninstallation into don't delete list
        for node in all_package_graph.G.nodes:
            keeper_name, keeper_location = node.split("|")
            keeper_wrapp_file = wrapp_file_path(keeper_location, keeper_name)
            keeper = await load_package_setup(keeper_wrapp_file)
            if keeper is not None:
                if keeper.catalog is not None:
                    for item in keeper.catalog.items:
                        absolute_path = join_paths(keeper_location, item.relative_path)
                        relative_path = make_relative_url(install_location, absolute_path)
                        # TODO it could be that the path was outside the install_location, then we should not bother about the file anyway
                        dont_delete.add(relative_path)
            else:
                logger.warning(f"Could not load referenced wrapp file {keeper_wrapp_file}")
    elif installed_package_info.dependencies is not None:
        # This is a basic uninstallation without specifying a package file. Just do a recursive uninstall of all dependencies
        install_base = get_base_directory_from_url(installed_package_file_url)
        scheduler_uninstall_deps = scheduler_uninstall_deps.create_sub_context_and_node(
            f"Uninstalling {len(installed_package_info.dependencies)} dependencies"
        )
        for dependency in installed_package_info.dependencies:
            installed_in = join_paths(install_base, dependency.relative_destination)
            await uninstall(
                dependency.package,
                install_location=installed_in,
                package=None,
                tags=tags,
                ignore_file=ignore_file,
                ignore_tags=ignore_tags,
                force=force,
                context=context,
                scheduler=scheduler_uninstall_deps,
            )

    # Now, delete the files, and after that the folders if they are empty
    items_to_remove = set()

    async def uninstall_item(file_url: str):
        result = await wrapp_delete_item(context, file_url)
        if result != Result.OK and result != Result.ERROR_NOT_FOUND:
            raise StorageOperationError(f"Failed to remove file: {result}!")

    async with scheduler.parent_task_node().create_task_group_node(f"Removing files for '{package_name}'") as task_group:
        for delete_file in installed_catalog.items:
            if delete_file.relative_path not in dont_delete:
                file_url = join_paths(install_location, delete_file.relative_path)
                items_to_remove.add(file_url)
                task_group.create_task(uninstall_item(file_url), name=f"Remove item {file_url}")

        # Package-standard sidecars are companion metadata excluded from the catalog, so they
        # are not in installed_catalog.items. Install copies them into the install tree, so
        # remove them explicitly here: the root definition file plus every file in the .metadata
        # folder (no-op when the package has none - ERROR_NOT_FOUND is ignored).
        sidecar_paths = package_standard_sidecar_paths(installed_package_file_url)
        metadata_dir = package_standard_metadata_folder_url(installed_package_file_url)
        sidecar_urls = [sidecar_paths.definition_url]
        for rel_path, _ in await list_package_standard_metadata_files(installed_package_file_url):
            sidecar_urls.append(join_paths(metadata_dir, rel_path))
        for sidecar_url in sidecar_urls:
            items_to_remove.add(sidecar_url)
            task_group.create_task(uninstall_item(sidecar_url), name=f"Remove sidecar {sidecar_url}")

    # Then remove the dependency if package was specified
    if package is not None:
        await remove_dependency_from_package_file(context, package, package_name, relative_destination=relative_install_location)


async def delete_folder_if_empty(context: CommandParameters, folder: str, items_to_be_deleted: Set[str]) -> Result:
    try:
        async for item in wrapp_list_directory(folder):
            # Ignore items that still exist but are in the set of items to be deleted
            if join_paths(folder, item.relative_path) in items_to_be_deleted:
                continue
            logger.info(f"Keeping folder {folder} because it is not empty")
            return Result.OK
    except StorageOperationError as e:
        if e.result == Result.ERROR_NOT_FOUND:
            # This is already gone, I guess this is ok
            return Result.OK
        elif e.result == Result.ERROR_WRONG_TYPE:
            # This has by now morphed into a file. Should not be deleted
            logger.warning(f"Not deleting {folder} as it is not an empty folder as expected but a file, got {e}")
            return Result.OK
        elif e.result != Result.OK:
            raise StorageOperationError(f"Failed to list folder that is about to be deleted: {folder}, result is {e}")

    result = await wrapp_delete_item(context, folder)
    if result == Result.ERROR_NOT_FOUND:
        return Result.OK
    elif result != Result.OK:
        logger.error(f"Failed to delete folder: {folder}, result is {result}")
    return result


async def remove_dependency_from_package_file(
    context: CommandParameters, dependency_file: str, package_name: str, relative_destination: str
):
    package_setup = await load_package_setup(dependency_file)

    # Now we should have a package setup if desired, and can update it
    if package_setup is None:
        raise FailedCommand(f"Failed to load dependency file at {dependency_file}")

    # Remove this package
    if package_setup.dependencies is not None:
        package_setup.dependencies = list(
            filter(lambda x: x.package != package_name or x.relative_destination != relative_destination, package_setup.dependencies)
        )
    await write_package_info(dependency_file, package_setup, context)
