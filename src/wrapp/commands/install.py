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
from dataclasses import replace
from typing import (
    List,
    Optional,
    Tuple,
    Union,
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
from wrapp.datastructures.dependency_graph import DependencyGraph
from wrapp.datastructures.hash_cache import HashCache
from wrapp.datastructures.ignore import (
    IgnoreEvaluator,
    create_ignore_eval_from_params,
)
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    PackageInfo,
    _write_package_info,
    add_dependency_to_package,
    is_package_file_name,
    load_package_setup,
    repository_from_wrapp_file_path,
    wrapp_file_in_repo,
    wrapp_file_name,
    wrapp_file_path,
    write_package_info,
)
from wrapp.datastructures.package_standard import (
    is_package_standard_sidecar_path,
)
from wrapp.datastructures.patch import (
    Patch,
    PatchOpBase,
    PatchOpCopy,
    PatchOpDelete,
)
from wrapp.datastructures.three_way_diff import (
    ThreeWayDiff,
    ThreeWayDiffContext,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    DryRunFlag,
    FileTransferJobsNumber,
    ForceFlag,
    HashCacheFileOption,
    IgnoreFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    PackageFileOption,
    PatchOutputOption,
    ProgressReportOption,
    RepoOption,
    ResolveConflictsFlag,
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
from wrapp.utils.router import Router
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage import (
    Result,
    wrapp_is_case_sensitive,
    wrapp_list_directory,
    wrapp_list_file_tree,
    wrapp_list_file_tree_supported,
    wrapp_stat,
    wrapp_supports_tags,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    FileParseException,
    StorageOperationError,
    get_base_directory_from_url,
    is_file_url,
    is_folder,
    is_regular_file,
    join_paths,
    make_relative_url,
    normalize_url,
    random_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="install")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def install_command(
    package_name: str = typer.Argument(..., help="Specify the name of the package to install"),
    version: str = typer.Argument(..., help="Specify the version of the package [any string]"),
    destination: str = typer.Argument(..., help="The path to install the package to"),
    dry_run: bool = DryRunFlag,
    tags: bool = TagsFlag.default_help(),
    resolve_conflicts: bool = ResolveConflictsFlag,
    package: str = PackageFileOption.better_help("Specify the package file to record the dependency in. Default is to do no recording."),
    patch: str = PatchOutputOption.better_help(
        help="File name of patch to store the install operation when conflicts arise, to be used with the apply command",
    ),
    force: bool = ForceFlag.better_help(
        help="Overwrite any files on the destination with the version from the package, even if they were modified in the destination since the last install."
    ),
    ignore_file: str = IgnoreFileOption,
    hash_cache_file: Optional[str] = HashCacheFileOption,
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
    repo: str = RepoOption,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Installs or updates a package described by package name and version string from the package cache. This looks in the package cache /.packages on
    the same server as the destination, and copies the package version found into the destination directory. If a package file is specified, the new dependency
    is recorded in there. If the repo is specified, the operations looks in that package cache instead of the default cache.

    WRAPP detects package catalog paths that differ only by letter case. Installs to case-insensitive destinations fail with
    'Cross platform case insensitive path conflicts found' to prevent overwrites, while installs to case-sensitive destinations
    are allowed.
    """
    context, scheduler_params = context_from_params()
    try:
        conflicts: List[Tuple[str, PatchOpBase]] = []
        patch_file: str = patch if patch else ""
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await install(
                package_name,
                version,
                destination,
                tags=tags,
                package=package,
                patch=patch_file != "",
                conflicts=conflicts,
                repo=repo,
                ignore_version_conflicts=resolve_conflicts,
                ignore_file=ignore_file,
                force=force,
                context=context,
                scheduler=scheduler,
            )
            if patch_file != "":
                resolve_conflict_patch = Patch()
                try:
                    for conflict in conflicts:
                        resolve_conflict_patch.add(conflict[1])
                    await resolve_conflict_patch.save(context, patch)
                finally:
                    await resolve_conflict_patch.disconnect_tagging_service()
    except FailedCommand as e:
        logger.error(f"Failure to install package '{package_name}' to '{destination}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Storage error during installation of '{package_name}' to '{destination}', operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def install(
    package_name: str,
    version: str,
    destination: str,
    tags: bool = False,
    package: Optional[str] = None,
    patch: bool = False,
    conflicts: Optional[List[Tuple[str, PatchOpBase]]] = None,
    repo: Optional[str] = None,
    ignore_version_conflicts: bool = False,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    force: bool = False,
    extras: Optional[List[Tuple[str, PatchOpBase]]] = None,
    *,
    ignore_file: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Given a package name and version, installs or updates the package's file set at the given destination.

    Installs or updates a package described by package name and version string from the package cache. This looks in the package cache /.packages on
    the same server as the destination, and copies the package version found into the destination directory. If a package file is specified, the new dependency
    is recorded in there. If the repo is specified, the operations looks in that package cache instead of the default cache.

    :param package_name: Name of the package to install
    :param version: Version to install
    :param destination: URL to install the package to. Can be an omniverse://, a URL supported by S3 or a file: URL.
    :param tags: Whether to also create the tags in the target. Only available when destination is a Nucleus server with Tagging Service
    :param package: Optionally specify the URL of a package file to record the dependency created
    :param patch: Specify False to fail installation on conflict, or True to install all non-conflicting files.
    :param conflicts: Supply a list that gets extended by all conflicts install detected.
    :param repo: Optionally specify a repository URL. If not given, assume it is the root of the destination server (same server install).
    :param ignore_version_conflicts: Set this to ignore version inconsistencies should versions create a diamond pattern conflict of nested packages
    :param ignores: Specify the name of a file with ignore rules, or an IgnoreEvaluator object, used when cataloging the existing destination.
    :param force: Overwrite any files on the destination with the version from the package, even if they were modified in the destination since the last install.
    :param extras: Supply a list that gets extended by all extra files detected in the destination.
    :param ignore_file: Deprecated, use ignores instead. Specify the name of a file with ignore rules.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    if ignore_version_conflicts:
        raise FailedCommand("Ignoring version conflicts on install is not supported yet, this is a future feature!")

    if not destination.strip():
        raise FailedCommand("Destination URL cannot be empty!")

    if not package_name.strip():
        raise FailedCommand("Package name cannot be empty!")

    if not version.strip():
        raise FailedCommand("Version cannot be empty!")

    if tags and not await wrapp_supports_tags(destination):
        raise FailedCommand(f"Tags are not supported for target destination {destination}!")

    # Resolve ignores (handles deprecated ignore_file parameter)
    ignore_rules = await create_ignore_eval_from_params(ignores, destination, ignore_file=ignore_file)

    # Need to work with normalized input
    destination_normalized = normalize_url(destination)

    hash_cache_supported_for_destination = _install_hash_cache_enabled_for_destination(context, destination_normalized)
    destination_context = context
    if context.hash_cache_file is not None and not hash_cache_supported_for_destination:
        destination_context = replace(context, hash_cache_file=None)

    # Validate package parameter to prevent circular dependencies
    if package is not None:
        package_dir = normalize_url(get_base_directory_from_url(package))

        # Allow installing into the same directory where the package file is located
        # But prevent installing into a subdirectory of the destination
        # This would create a circular dependency
        if package_dir.startswith(destination_normalized) and package_dir != destination_normalized:
            raise FailedCommand(
                f"Cannot use a package file ({package}) that is inside the installation target ({destination}). This would create a circular dependency."
            )

        # Check that the destination is in a subfolder of the package location
        # This ensures we're only installing into child directories
        # But only log a warning, don't fail - this allows for test cases and advanced usage
        if not destination_normalized.startswith(package_dir) and package_dir != destination_normalized:
            logger.warning(
                f"Installing package {package_name} into a location ({destination}) that is not a subfolder of the package file location ({package_dir}). This may lead to unexpected behavior."
            )

    source_repository: Repository = Repository(repo_url=repo, fallback_base=destination_normalized)
    source_base_url = source_repository.package_url(package_name, version)

    # Task structure:
    #   if wrapp_url_exists(destination):
    #     Analyse target directory
    #   Analyse source package
    #   if wrapp_url_exists(destination):
    #     Analyse previously installed package
    #   Calculate fingerprints
    #   Patch
    expected_num_sub_jobs = 3
    if await wrapp_url_exists(destination):
        expected_num_sub_jobs += 2
    install_tree_node = scheduler.parent_task_node().create_tree_node(
        f"Install '{package_name}' '{version}'", expected_num_sub_jobs=expected_num_sub_jobs
    )
    scheduler = scheduler.create_sub_context(install_tree_node)

    # Load the package file of the source package
    installee_package_file_name = wrapp_file_name(package_name)
    source_package_file_url = wrapp_file_path(source_base_url, package_name)
    source_setup: Optional[PackageInfo] = await load_package_setup(source_package_file_url)
    if source_setup is None:
        raise FailedCommand(f"Failed to load the source's package at {source_package_file_url}")
    elif source_setup.catalog is None:
        raise FailedCommand(f"Cannot install packages which have no catalog")

    # Add to dependency file if specified
    destination_workspace_root: Optional[str] = None
    if package is not None:
        destination_workspace_root = get_base_directory_from_url(package)
        relative_destination = make_relative_url(destination_workspace_root, destination)
        workspace_setup: Optional[PackageInfo] = await load_package_setup(package)
        if workspace_setup is None:
            raise FailedCommand(f"Cannot record dependency, please run new to create the dependency file {package} first.")

        add_dependency_to_package(
            workspace_setup, package_name=package_name, version=version, repo=source_repository, relative_destination=relative_destination
        )

        # Verify it. Building the dependency graph will raise an Exception in case of version conflicts if no override is specified
        workspace_loaded_package = LoadedPackage(package, workspace_setup)
        dependency_graph = await DependencyGraph.build_dependency_graph(
            context,
            scheduler.parent_task_node(),
            workspace_loaded_package,
            ignore_version_conflicts=ignore_version_conflicts,
            fallback_repo=source_repository,
        )

    # Now add the package via a merge/apply combination
    destination_package_url = wrapp_file_path(destination, package_name)
    try:
        destination_package_setup: Optional[PackageInfo] = await load_package_setup(destination_package_url)
    except FileParseException as e:
        # If we want to do a force install, we ignore all parse errors in the destination files: an invalid .wrapp file
        # should not block a force install.
        logger.warning(f"Ignoring .wrapp file {destination_package_url} in the target folder which could not be parsed: {e}")
        destination_package_setup = None

    # OPTIMIZATION: Calculate source and ancestor catalogs FIRST, so we can do targeted destination cataloging
    # This avoids hashing all files in the destination (including user "extras" files) which is expensive.

    # Step 1: Calculate source catalog with dependencies
    source_loaded_package = LoadedPackage(source_package_file_url, source_setup)
    full_source_catalog: Catalog = await source_loaded_package.calculate_catalog_including_deps(
        use_repo_path=True,
        add_subpackage_wrapp_files=True,
        task_node=scheduler.parent_task_node().create_tree_node("Analysing source package"),
    )
    if not wrapp_is_case_sensitive(destination_normalized):
        has_conflicts, conflicting_paths = full_source_catalog.check_case_insensitive_path_conflicts()
        if has_conflicts:
            raise FailedCommand(
                f"Cross platform case insensitive path conflicts found: {conflicting_paths}. Please ensure that the paths are unique on all platforms (Windows, Linux, macOS)."
            )

    source_dict = full_source_catalog._catalog_to_dict()

    # Step 2: Calculate ancestor catalog (if available)
    if destination_package_setup is not None:
        if destination_package_setup.repository is not None:
            ancestor_repo: Optional[str] = destination_package_setup.repository
        else:
            # Now it gets interesting, the destination_package has never been installed from somewhere. Where do we look for the created package?
            # Let's try the specified remote
            ancestor_repo = repo

        ancestor_package_url = wrapp_file_in_repo(
            repo=Repository(repo_url=ancestor_repo, fallback_base=destination),
            package_name=destination_package_setup.name,
            version=destination_package_setup.version,
        )
        if await wrapp_url_exists(ancestor_package_url):
            # This is a three-way merge, as we have an ancestor dictionary. For that, we need to calculate the real ancestor file set
            ancestor_package = await LoadedPackage.get_from_url(ancestor_package_url)
            full_ancestor_catalog = await ancestor_package.calculate_catalog_including_deps(
                use_repo_path=True,
                add_subpackage_wrapp_files=True,
                task_node=scheduler.parent_task_node().create_tree_node("Analysing previously installed package"),
            )
            ancestor_dict = full_ancestor_catalog._catalog_to_dict()
        else:
            logger.warning(
                f"Could not find ancestor for package '{destination_package_setup.name}' for version '{destination_package_setup.version}', using two-way diff to resolve"
            )
            ancestor_dict = {}
    else:
        # Turn this into a two-way merge
        ancestor_dict = {}

    # Step 3: Catalog destination using TARGETED file list (only files from source ∪ ancestor)
    # This is a major optimization: we skip hashing "extra" files that exist only in the destination

    # Build ignore rules for the package files (needed for both cataloging and extras detection)
    ignore_packages = []
    if destination_workspace_root is not None and package is not None:
        workspace_package_relative_path = make_relative_url(destination_workspace_root, package)
    else:
        workspace_package_relative_path = make_relative_url(destination, destination_package_url)
    ignore_packages.append(workspace_package_relative_path)
    default_ignore = IgnoreEvaluator(None, lambda x: x in ignore_packages or is_package_standard_sidecar_path(x))
    # Add user-specified ignore rules if any
    if ignore_rules is not None:
        default_ignore.add_evaluator(ignore_rules.predicate)

    # Build targeted file list: only catalog files that exist in source OR ancestor
    # Files that exist ONLY in destination (extras) don't need hashing - they won't be modified
    paths_in_package = set(source_dict.keys()) | set(ancestor_dict.keys())

    destination_exists = await wrapp_url_exists(destination)
    if destination_exists:
        # Filter out ignored paths for cataloging
        paths_to_check = {p for p in paths_in_package if not default_ignore.is_ignored(p)}
        file_list = [(destination, path) for path in paths_to_check]

        destination_catalog: Catalog = await catalog(
            root_dir=destination,
            file_list=file_list,
            checkpoints=False,
            tags=tags,
            skip_missing=True,  # Files not in destination are fine - they're new files from source
            context=destination_context,
            scheduler=scheduler.create_sub_context(scheduler.parent_task_node().create_tree_node("Analysing target directory")),
        )
    else:
        # This is the case for dry-run, the target folder has not been created.
        destination_catalog = Catalog(root=destination, items=[])

    destination_dict = destination_catalog._catalog_to_dict()

    # Cache for loaded subpackage, so we don't load them more than once. There can be many.
    loaded_subpackages: dict[str, LoadedPackage] = {}
    fingerprints_subpackages: dict[str, tuple[asyncio.Event, str, int]] = {}

    # We patch the "repository" field for the installed .wrapp files below after copying the files to the working folder.
    # We need to make sure that we only compare the parts of the .wrapp file in the diff which are not changed by this
    # patch to avoid spurious conflicts.
    async with scheduler.parent_task_node().create_task_group_node("Calculating fingerprints for .wrapp files") as task_node:
        _replace_wrapp_files_with_fingerprint(
            destination_dict, loaded_subpackages, fingerprints_subpackages, task_node, ignore_unparseable_wrapp_files=force
        )
        _replace_wrapp_files_with_fingerprint(source_dict, loaded_subpackages, fingerprints_subpackages, task_node)
        _replace_wrapp_files_with_fingerprint(ancestor_dict, loaded_subpackages, fingerprints_subpackages, task_node)
    # Now we know the source, the destination and the original version. Run the merge to create a patch
    diff_context = ThreeWayDiffContext(
        destination_dict,
        source_dict,
        ancestor_dict,
        destination,
        tags=tags,
        name_a="install target",
        name_b="source package",
        name_base="previous install",
    )
    patch_list, conflict_list, extras_list = ThreeWayDiff.diff_with_context(diff_context)

    # With targeted cataloging optimization, extras_list from three-way diff may be incomplete
    # because we only catalog files from source ∪ ancestor, not the entire destination tree.
    # If caller wants extras, enumerate them with a lightweight tree walk (no hashing).
    if extras is not None and destination_exists:
        all_dest_paths = await _enumerate_destination_files(destination)
        for path in all_dest_paths:
            if path not in paths_in_package and not default_ignore.is_ignored(path):
                extras_list.append(
                    (
                        path,
                        PatchOpDelete(
                            url=join_paths(destination, path),
                            explanation=f"Extra: {path} in install target but not in source package",
                        ),
                    )
                )

    if conflicts is not None:
        conflicts.extend(conflict_list)
    if extras is not None:
        extras.extend(extras_list)

    if len(conflict_list) > 0 and not patch and not force:
        for conflict_key, conflict_patch_op in conflict_list[:100]:
            logger.error(f"{conflict_patch_op.explanation_txt()}\nDiff details: {diff_context.diff_explanation(conflict_key)}")
        if len(conflict_list) > 100:
            logger.error(f"... {len(conflict_list) - 100} conflicts not shown.")
        raise FailedCommand(f"Can't update, local copy is modified. {len(conflict_list)} conflicts detected!")
    else:
        if force:
            patch_list.extend(conflict_list)

        # Repo-backed subpackage .wrapp files need their "repository" field retargeted to the repo we are
        # installing from. We write them here, BEFORE applying the patch, and skip their copy ops below so
        # each is written exactly once instead of being copied by the patch and then rewritten. Running this
        # first is safe: nothing in the patch overwrites these files, and writing them up front means they
        # already exist when the patch applies any tag operations to them.
        #
        # changed_subpackage_paths are the subpackage .wrapp files the patch would have (re)written (their
        # content is new or modified); it tells the writer which ones to materialise from the source package
        # unconditionally versus which already sit unchanged on disk and only need a repository fix-up.
        changed_subpackage_paths = {key for key, op in patch_list if isinstance(op, PatchOpCopy) and is_package_file_name(key)}
        # written_subpackage_paths are the catalog-relative paths actually written here, so we can skip the
        # patch's now-redundant copy of each below.
        written_subpackage_paths = await _write_subpackage_wrapp_files(
            context, full_source_catalog, destination_normalized, loaded_subpackages, changed_subpackage_paths, scheduler
        )

        # Build patch and run it
        p = Patch()
        try:
            for key, op in patch_list:
                if isinstance(op, PatchOpCopy) and key in written_subpackage_paths:
                    # Already written with the retargeted repository above; skip the redundant copy.
                    continue
                p.add(op)
            if not await p.execute(context=context, scheduler=scheduler):
                raise FailedCommand(
                    f"Failed to apply patch to install {package_name} {version} to {destination}. Please check log for details."
                )
        finally:
            await p.disconnect_tagging_service()

        # Eager hash cache population: for files we just copied, we know their hash
        # Insert them into the cache so the next re-install gets immediate cache hits
        if context.hash_cache_file is not None and hash_cache_supported_for_destination:
            await _populate_hash_cache_from_copied_files(context, patch_list)

        # Write the package file of the new source package version - we cannot copy the file at this point
        # as the installing WRAPP might use a different format version from the one which created the package.
        # Update the repository to record that we have installed this package from that repo, else we might still be pointing
        # to a mirror source
        source_setup.repository = source_repository.base_url()
        destination_package_file_url = join_paths(destination, installee_package_file_name)
        await _write_package_info(
            destination_package_file_url,
            source_setup,
            context,
            package_standard_seed_url=source_package_file_url,
        )

        # Update dependency file if specified
        if package is not None:
            destination_workspace_root = get_base_directory_from_url(package)
            relative_destination = make_relative_url(destination_workspace_root, destination)
            await modify_package_file_with_added_dependency(
                context, package, package_name, version, source_repository, relative_destination=relative_destination
            )


async def _patch_wrapp_file_in_catalog_dict(
    item: dict,
    loaded_subpackages: dict[str, LoadedPackage],
    fingerprints_subpackages: dict[str, tuple[asyncio.Event, str, int]],
    ignore_unparseable_wrapp_files: bool = False,
):
    # Load the installed file for this package
    wrapp_file_url = item["source_path"]
    if wrapp_file_url not in fingerprints_subpackages:
        # Make sure no other async call tries to load the package at the same time
        event = asyncio.Event()
        fingerprints_subpackages[wrapp_file_url] = (event, "", -2)
        try:
            loaded_subpackages[wrapp_file_url] = await LoadedPackage.get_from_url(wrapp_file_url)
            item_hash, item_size = loaded_subpackages[wrapp_file_url].package_fingerprint()
            fingerprints_subpackages[wrapp_file_url] = (event, item_hash, item_size)
        except FileParseException as e:
            if not ignore_unparseable_wrapp_files:
                raise
            # Force unparseable files to be different
            logger.warning(f"Ignoring .wrapp file {wrapp_file_url} which could not be parsed: {e}")
            fingerprints_subpackages[wrapp_file_url] = (event, f"file_cannot_be_parsed_{random_string()}", -1)
        finally:
            event.set()
    else:
        # Wait for the load being done by the other async call
        await fingerprints_subpackages[wrapp_file_url][0].wait()
    item["hash"] = fingerprints_subpackages[wrapp_file_url][1]
    item["size"] = fingerprints_subpackages[wrapp_file_url][2]


def _replace_wrapp_files_with_fingerprint(
    catalog_dict: dict[str, dict],
    loaded_subpackages: dict[str, LoadedPackage],
    fingerprints_subpackages: dict[str, tuple[asyncio.Event, str, int]],
    task_node: SchedulerTaskGroupNode,
    ignore_unparseable_wrapp_files: bool = False,
):
    for path, item in catalog_dict.items():
        if is_package_file_name(path):
            task_node.create_task(
                _patch_wrapp_file_in_catalog_dict(
                    item,
                    loaded_subpackages,
                    fingerprints_subpackages,
                    ignore_unparseable_wrapp_files,
                ),
                name="Loading subpackage file",
            )


async def _write_subpackage_wrapp_files(
    context: CommandParameters,
    catalog: Catalog,
    destination_url: str,
    loaded_subpackages: dict[str, LoadedPackage],
    changed_subpackage_paths: set[str],
    scheduler: SchedulerContext,
) -> set[str]:
    """Write each repo-backed subpackage .wrapp file with its repository pointer retargeted to the repo we
    are installing from.

    Returns the set of catalog-relative paths that were handled here, i.e. the subpackage .wrapp files the
    caller must NOT also copy via the patch (we write them directly instead).
    """
    written_subpackage_paths: set[str] = set()
    async with scheduler.parent_task_node().create_task_group_node("Retarget dependencies") as task_node:
        for sub_wrapp in filter(lambda x: is_package_file_name(x.relative_path), catalog.items):
            sub_wrapp_repo_base = repository_from_wrapp_file_path(sub_wrapp.source_path)
            if sub_wrapp_repo_base:
                written_subpackage_paths.add(sub_wrapp.relative_path)
                task_node.create_task(
                    _write_wrapp_with_updated_repo_field(
                        context,
                        sub_wrapp,
                        destination_url,
                        sub_wrapp_repo_base,
                        loaded_subpackages,
                        changed_subpackage_paths,
                    ),
                    name="Updating repository tracking info",
                )
            else:
                logger.warning(f"Encountered wrapp file which is not in a repository as installation source: {sub_wrapp.source_path}")
    return written_subpackage_paths


async def _write_wrapp_with_updated_repo_field(
    context: CommandParameters,
    sub_wrapp: PackageInfo,
    destination_url: str,
    sub_wrapp_repo_base: str,
    loaded_subpackages: dict[str, LoadedPackage],
    changed_subpackage_paths: set[str],
):
    sub_wrapp_install_path = join_paths(destination_url, sub_wrapp.relative_path)
    # This runs BEFORE the patch is applied, and the patch skips the copy of every subpackage handled here
    # (see install()). We use the loaded_subpackages cache filled during the diff/fingerprint phase so we
    # never have to read the file back from disk.
    if sub_wrapp.relative_path in changed_subpackage_paths:
        # The patch would have (re)written this subpackage; instead we materialise it here from the SOURCE
        # package we would have copied, with the repository retargeted. The destination file may not exist
        # yet (fresh install) or still hold the previous version, so we always write it. Using the source
        # cache (not the pre-patch destination snapshot) avoids OMPE-96052, where the stale destination
        # entry would make us believe no rewrite is needed.
        cached_subpackage = loaded_subpackages.get(sub_wrapp.source_path)
        sub_wrapp_package: Optional[PackageInfo] = (
            cached_subpackage.package if cached_subpackage is not None else await load_package_setup(sub_wrapp.source_path)
        )
        if not sub_wrapp_package:
            raise FailedCommand(f"Failed to load source wrapp file to install: {sub_wrapp.source_path}")
        logger.info(f"Retargeting repository pointer of subpackage at {sub_wrapp_install_path} to point to {sub_wrapp_repo_base}")
        sub_wrapp_package.repository = sub_wrapp_repo_base
        await write_package_info(sub_wrapp_install_path, sub_wrapp_package, context)
    else:
        # The patch does not touch this subpackage (its content is unchanged). The pre-patch destination
        # snapshot still matches the file on disk, so we only rewrite when the repository pointer differs.
        cached_subpackage = loaded_subpackages.get(sub_wrapp_install_path)
        sub_wrapp_package = cached_subpackage.package if cached_subpackage is not None else await load_package_setup(sub_wrapp_install_path)
        if not sub_wrapp_package:
            raise FailedCommand(f"Failed to load wrapp file that was successfully cataloged: {sub_wrapp_install_path}")
        if sub_wrapp_package.repository != sub_wrapp_repo_base:
            logger.info(f"Retargeting repository pointer of subpackage at {sub_wrapp_install_path} to point to {sub_wrapp_repo_base}")
            sub_wrapp_package.repository = sub_wrapp_repo_base
            await write_package_info(sub_wrapp_install_path, sub_wrapp_package, context)


async def _populate_hash_cache_from_copied_files(
    context: CommandParameters,
    patch_list: List[Tuple[str, PatchOpBase]],
) -> None:
    """
    Eagerly populate the hash cache with entries for files we just copied.

    Since we know the hash of copied files (from the source catalog), we can insert
    them into the hash cache immediately. This way, the next re-install will get
    cache hits instead of having to compute hashes.

    Args:
        context: Command parameters containing hash_cache_file path
        patch_list: List of patch operations that were executed
    """
    if context.hash_cache_file is None:
        return

    copy_ops = _local_copy_ops_for_eager_hash_cache(patch_list)
    if not copy_ops:
        return

    hash_cache = HashCache(context.hash_cache_file, verbose=context.verbose)
    inserted_count = 0

    for key, op in copy_ops:
        try:
            # Stat the destination file to get modification time and size
            result, item = await wrapp_stat(op.dst)
            if (
                result == Result.OK
                and item is not None
                and item.modified_time is not None
                and item.size is not None
                and op.hash is not None
            ):
                hash_cache.set(op.dst, item.size, item.modified_time, op.hash)
                inserted_count += 1
        except Exception as e:
            # Don't fail install if cache population fails - it's just an optimization
            logger.debug(f"Failed to populate hash cache for {op.dst}: {e}")

    if inserted_count > 0:
        hash_cache.save()
        logger.info(f"Eagerly populated hash cache with {inserted_count} entries from copied files")


def _local_copy_ops_for_eager_hash_cache(patch_list: List[Tuple[str, PatchOpBase]]) -> List[Tuple[str, PatchOpCopy]]:
    # Eager cache population stats every copied destination. Keep that optimization
    # local-only so WAN installs do not perform thousands of serial remote stats.
    # As of now (May 2026) all remote backends used in real-world use cases support
    # storing the content hash as metadata, so the hash cache as currently designed
    # is only needed for local files.
    #
    # Skip subpackage .wrapp files: their op.hash is the package fingerprint (which intentionally
    # ignores the repository field), not the flat content hash the hash cache stores for files. The
    # patch also rewrites these files afterwards when their repository is retargeted, so caching the
    # post-copy stat would both record the wrong hash type and reference a stale version (OMPE-96052).
    return [
        (key, op)
        for key, op in patch_list
        if isinstance(op, PatchOpCopy) and op.hash is not None and is_file_url(op.dst) and not is_package_file_name(key)
    ]


def _install_hash_cache_enabled_for_destination(context: CommandParameters, destination: str) -> bool:
    if context.hash_cache_file is None or not Router.me().storage_stores_wrapp_hashes(destination):
        return True

    logger.warning(
        f"Ignoring --hash-cache-file for install destination {destination}: "
        "this storage backend stores WRAPP hashes on files or objects directly, "
        "so install does not need a separate hash cache file for this target."
    )
    return False


async def _enumerate_destination_files(destination: str) -> List[str]:
    """Enumerate all file paths under destination without computing hashes."""
    paths: List[str] = []

    async def walk(url: str, prefix: str):
        use_tree = wrapp_list_file_tree_supported(url)
        items = wrapp_list_file_tree(url) if use_tree else wrapp_list_directory(url)
        async for item in items:
            rel_path = join_paths(prefix, item.relative_path) if prefix else item.relative_path
            rel_path = rel_path.replace("\\", "/").lstrip("/")
            if is_regular_file(item):
                paths.append(rel_path)
            elif is_folder(item) and not use_tree:
                await walk(join_paths(url, item.relative_path), rel_path)

    await walk(destination, "")
    return paths


async def modify_package_file_with_added_dependency(
    context: CommandParameters, dependency_file: str, package_name: str, version: str, repo: Repository, relative_destination: str
) -> None:
    package_setup = await load_package_setup(dependency_file)
    if package_setup is None:
        package_setup = PackageInfo()

    # Now we should have a package setup if desired, and can update it
    if package_setup is not None:
        add_dependency_to_package(
            package_setup,
            package_name=package_name,
            version=version,
            repo=repo,
            relative_destination=relative_destination,
        )
        await write_package_info(dependency_file, package_setup, context)
