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
from copy import deepcopy
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
from wrapp.commands.list_repo import list_repo
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.loaded_package import (
    FingerprintError,
    LoadedPackage,
    deps_not_loaded,
    load_deps_for_fingerprinting,
)
from wrapp.datastructures.package import (
    PackageInfo,
    UniquePackageLocation,
    _write_package_info,
    load_package_setup,
    wrapp_file_in_repo,
    wrapp_file_name,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
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
from wrapp.utils.storage import normalize_repo_url_for_comparison
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_filename_from_url,
    join_paths,
    normalize_url,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="publish-redirect")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def publish_redirect_command(
    source_repos: List[str] = typer.Option(
        ...,
        "--source-repo",
        "--sr",
        help="Source repository URL(s) containing packages to create redirects for. Can be specified multiple times.",
    ),
    redirect_repo: str = typer.Option(
        ..., "--redirect-repo", "--rr", help="Redirect repository URL where redirect packages will be created"
    ),
    external_url: Optional[str] = typer.Option(
        None,
        "--external-url",
        "--eu",
        help="External base URL for catalog source_path entries (e.g., https://cdn.example.com). If not specified, source_path is preserved.",
    ),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Create redirect packages in the redirect repository for all packages in the source repositories.

    Redirect packages contain only .wrapp files (no data files) with source_path entries
    pointing to the original source locations (or external URLs if specified). This enables
    CloudFront/CDN URL redirection while preserving data access to the original storage locations.

    Multiple source repositories can be specified to consolidate packages from different
    sources into a single redirect repository.
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await publish_redirect(
                source_repos=source_repos,
                redirect_repo=redirect_repo,
                external_url=external_url,
                context=context,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        logger.error(f"Failed to create redirect packages from {source_repos} to {redirect_repo}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while creating redirect packages from {source_repos} to {redirect_repo}, operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def publish_redirect(
    source_repos: Union[str, List[str]],
    redirect_repo: str,
    external_url: Optional[str] = None,
    *,
    context: Optional[CommandParameters] = None,
    scheduler: Optional[SchedulerContext] = None,
) -> List[str]:
    """
    Create redirect packages in the redirect repository for all packages in the source repositories.

    Redirect packages contain only .wrapp files (no data files) with source_path entries
    pointing to the original source locations (or external URLs if specified). This enables
    CloudFront/CDN URL redirection while preserving data access to the original storage locations.

    Multiple source repositories can be specified to consolidate packages from different
    sources into a single redirect repository.

    :param source_repos: URL(s) of the source repository/repositories containing packages to create redirects for.
                        Can be a single URL string or a list of URL strings.
    :param redirect_repo: URL of the redirect repository where redirect packages will be created
    :param external_url: External base URL for source_path entries (e.g., https://cdn.example.com).
                        If specified, source_path URLs will use this base instead of the source repo URL.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext

    :return: List of URLs of created redirect wrapp files

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if context is None:
        context = CommandParameters()

    # Normalize source_repos to a list
    if isinstance(source_repos, str):
        source_repos_list = [source_repos]
    else:
        source_repos_list = list(source_repos)

    if not source_repos_list:
        raise FailedCommand("At least one source repository must be specified")

    for repo_url in source_repos_list:
        validate_non_empty_string("source_repo", repo_url)
    validate_non_empty_string("redirect_repo", redirect_repo)

    # Check that no source repo is the same as the redirect repo
    # This would cause catastrophic overwriting of source data
    redirect_repo_normalized = normalize_repo_url_for_comparison(redirect_repo)
    for source_repo in source_repos_list:
        source_repo_normalized = normalize_repo_url_for_comparison(source_repo)
        if source_repo_normalized == redirect_repo_normalized:
            raise FailedCommand(
                f"Source repository and redirect repository cannot be the same: {source_repo}\n"
                "This would overwrite the source package data with redirect-only packages."
            )

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    redirect_repo_obj = Repository(repo_url=redirect_repo)
    created_files: List[str] = []
    all_packages: List[Tuple[Repository, str, str]] = []

    if len(source_repos_list) == 1:
        # Single source repo - no need for duplicate check
        source_repo = source_repos_list[0]
        source_repo_obj = Repository(repo_url=source_repo)

        logger.info(f"Listing packages in source repository {source_repo}...")
        packages = await list_repo(source_repo, scheduler=scheduler)

        for package_path, versions in packages:
            package_name = get_filename_from_url(package_path.rstrip("/"))
            for version in versions:
                all_packages.append((source_repo_obj, package_name, version))
    else:
        # Multiple source repos - pre-flight check for duplicates
        # Structure: dict of source_repo -> list of (package_name, version)
        repo_packages: dict[str, List[Tuple[str, str]]] = {}

        async def list_source_repo(source_repo: str) -> None:
            """List packages from a single source repo."""
            packages = await list_repo(source_repo, scheduler=scheduler)
            pkg_list: List[Tuple[str, str]] = []
            for package_path, versions in packages:
                package_name = get_filename_from_url(package_path.rstrip("/"))
                for version in versions:
                    pkg_list.append((package_name, version))
            repo_packages[source_repo] = pkg_list

        logger.info(f"Pre-flight check: listing packages from {len(source_repos_list)} source repo(s)...")
        async with scheduler.parent_task_node().create_task_group_node(
            f"Listing packages from {len(source_repos_list)} source repo(s)"
        ) as task_node:
            for source_repo in source_repos_list:
                task_node.create_task(list_source_repo(source_repo), name=f"List {source_repo}")

        # Check for duplicates across all repos
        # First pass: collect all occurrences of each package
        package_occurrences: dict[Tuple[str, str], List[str]] = {}  # (package_name, version) -> list of source_repos

        for source_repo in source_repos_list:
            for package_name, version in repo_packages.get(source_repo, []):
                pkg_key = (package_name, version)
                if pkg_key not in package_occurrences:
                    package_occurrences[pkg_key] = []
                package_occurrences[pkg_key].append(source_repo)

        # Find packages that appear in multiple repos
        duplicates = {pkg_key: repos for pkg_key, repos in package_occurrences.items() if len(repos) > 1}

        conflicts: List[str] = []

        if duplicates:
            # We have duplicates - need to compare fingerprints to see if they're actually the same
            logger.info(f"Found {len(duplicates)} package(s) in multiple source repos, comparing fingerprints...")

            # Load all duplicate packages and their dependencies for fingerprinting
            loaded_packages: dict[UniquePackageLocation, LoadedPackage] = {}

            async with scheduler.parent_task_node().create_task_group_node(
                f"Loading {len(duplicates)} duplicate package(s) for fingerprint comparison"
            ) as load_task_node:
                for (package_name, version), repos in duplicates.items():
                    for repo_url in repos:
                        repo = Repository(repo_url=repo_url)
                        wrapp_url = wrapp_file_in_repo(repo, package_name, version)
                        loc = UniquePackageLocation(package_name, version, repo.base_url())

                        async def load_package_and_deps(
                            loc: UniquePackageLocation, wrapp_url: str, task_node: SchedulerTaskGroupNode
                        ) -> None:
                            pkg = await LoadedPackage.get_from_url(wrapp_url)
                            loaded_packages[loc] = pkg
                            # Load dependencies for fingerprinting
                            not_loaded = deps_not_loaded(pkg, loaded_packages)
                            if not_loaded:
                                await load_deps_for_fingerprinting(not_loaded, loaded_packages, task_node)

                        load_task_node.create_task(
                            load_package_and_deps(loc, wrapp_url, load_task_node),
                            name=f"Load {package_name}@{version} from {repo_url}",
                        )

            # Wait for all dependency loading to complete
            # (The task group ensures this, but dependencies spawn more tasks)
            # We need to ensure all recursive loads are done before comparing fingerprints

            # Compare fingerprints for each duplicate set
            for (package_name, version), repos in duplicates.items():
                try:
                    fingerprints: dict[str, str] = {}  # repo_url -> fingerprint
                    for repo_url in repos:
                        repo = Repository(repo_url=repo_url)
                        loc = UniquePackageLocation(package_name, version, repo.base_url())
                        pkg = loaded_packages.get(loc)
                        if pkg:
                            fp = pkg.fingerprint(loaded_packages=loaded_packages)
                            fingerprints[repo_url] = fp

                    # Check if all fingerprints are the same
                    unique_fingerprints = set(fingerprints.values())
                    if len(unique_fingerprints) == 1:
                        # All copies are identical - use the first one
                        logger.info(
                            f"Package {package_name}@{version} exists in {len(repos)} repos with identical content, using {repos[0]}"
                        )
                    else:
                        # Different content - this is a real conflict
                        conflict_details = [f"  - {repo} (fingerprint: {fp})" for repo, fp in fingerprints.items()]
                        conflicts.append(
                            f"Package {package_name}@{version} exists in multiple source repos with different content:\n"
                            + "\n".join(conflict_details)
                        )
                except FingerprintError as e:
                    conflicts.append(
                        f"Package {package_name}@{version}: Failed to calculate fingerprint - "
                        f"missing dependency '{e.package_missing}' referenced from '{e.package_file}'"
                    )

        if conflicts:
            error_msg = "Cannot merge repositories with conflicting packages:\n" + "\n".join(conflicts)
            raise FailedCommand(error_msg)

        # Build the final package list, using first repo for duplicates
        for (package_name, version), repos in package_occurrences.items():
            source_repo_obj = Repository(repo_url=repos[0])  # Use first repo (duplicates have same content)
            all_packages.append((source_repo_obj, package_name, version))

        duplicate_count = len(duplicates)
        if duplicate_count > 0:
            logger.info(
                f"Pre-flight check passed: {len(all_packages)} package(s) to redirect, "
                f"{duplicate_count} duplicate(s) resolved (identical content)."
            )
        else:
            logger.info(f"Pre-flight check passed: {len(all_packages)} package(s) to redirect, no duplicates found.")

    # Now create all redirect packages
    async with scheduler.parent_task_node().create_task_group_node(f"Creating {len(all_packages)} redirect packages") as task_node:
        for source_repo_obj, package_name, version in all_packages:
            task_node.create_task(
                _create_redirect_for_package(
                    source_repo_obj,
                    redirect_repo_obj,
                    package_name,
                    version,
                    external_url,
                    context,
                    created_files,
                    all_source_repos=source_repos_list,
                ),
                name=f"Redirect {package_name}@{version}",
            )

    logger.info(f"Created {len(created_files)} redirect package(s) from {len(source_repos_list)} source repo(s)")
    return created_files


async def _create_redirect_for_package(
    source_repo: Repository,
    redirect_repo: Repository,
    package_name: str,
    version: str,
    external_url: Optional[str],
    context: CommandParameters,
    created_files: List[str],
    all_source_repos: Optional[List[str]] = None,
) -> None:
    """Create a redirect package for a single package version."""
    source_wrapp_url = join_paths(source_repo.package_url(package_name, version), wrapp_file_name(package_name))
    redirect_wrapp_url = join_paths(redirect_repo.package_url(package_name, version), wrapp_file_name(package_name))

    logger.info(f"Creating redirect for {package_name} {version}...")

    # Load the source package
    source_package = await load_package_setup(source_wrapp_url)
    if source_package is None:
        logger.warning(f"Could not load package file at {source_wrapp_url}, skipping")
        return

    # Create the redirect package
    redirect_package = create_redirect_package(
        source_package=source_package,
        source_repo=source_repo,
        redirect_repo=redirect_repo,
        package_name=package_name,
        version=version,
        external_url=external_url,
        all_source_repos=all_source_repos,
    )

    # Write the redirect package to the redirect repo location, carrying any
    # package-standard sidecars from the source package verbatim (like mirror/install).
    await _write_package_info(
        redirect_wrapp_url,
        redirect_package,
        context,
        package_standard_seed_url=source_wrapp_url,
    )
    created_files.append(redirect_wrapp_url)
    logger.info(f"Created redirect package at {redirect_wrapp_url}")


def create_redirect_package(
    source_package: PackageInfo,
    source_repo: Repository,
    redirect_repo: Repository,
    package_name: str,
    version: str,
    external_url: Optional[str] = None,
    all_source_repos: Optional[List[str]] = None,
) -> PackageInfo:
    """
    Create a redirect package from a source package.

    The redirect package has:
    - Updated catalog.root to point to the target location (external_url or source location)
    - Updated catalog item source_path values to point to the target location
    - Dependencies rewritten with repository changed from source to redirect repo (or external_url)

    :param source_package: The source PackageInfo to create a redirect for
    :param source_repo: The source repository
    :param redirect_repo: The redirect repository
    :param package_name: Name of the package
    :param version: Version of the package
    :param external_url: External base URL for source_path entries. If specified, the source repo
                        base URL will be replaced with this URL in source_path entries.
    :param all_source_repos: List of all source repository URLs being redirected. Used to rewrite
                            dependencies that point to any of the source repos.
    :return: A new PackageInfo configured as a redirect package
    """
    redirect_package = deepcopy(source_package)

    # Update repository to redirect repo
    redirect_package.repository = redirect_repo.base_url()

    # Compute external URL base (needed for both catalog and dependency rewriting)
    external_base = external_url.rstrip("/") if external_url else None

    # Update catalog source_path and root to use external URL if specified
    if redirect_package.catalog is not None:
        # Normalize the source base URL to ensure consistent path separators (important for Windows)
        source_base = normalize_url(source_repo.base_url())

        if external_base:
            # Update catalog.root to use external URL
            redirect_package.catalog.root = normalize_url(redirect_package.catalog.root).replace(source_base, external_base)

            # Update each item's source_path to use external URL
            for item in redirect_package.catalog.items:
                item.source_path = normalize_url(item.source_path).replace(source_base, external_base)
        # If no external_url, source_path values are preserved as-is (they already point to the right location)

    # Rewrite dependencies to use redirect repo (or external URL if specified)
    if redirect_package.dependencies is not None:
        # Determine the target repository for dependencies
        if external_base:
            # Use external URL so dependencies are also fetched via CDN
            target_dep_repo = external_base
        else:
            target_dep_repo = redirect_repo.base_url()

        # Build set of all source repo bases to check against
        # This handles the case where multiple source repos are being merged
        # Use normalization that handles URL variations (trailing slashes, S3 URL formats, etc.)
        source_bases_normalized: set[str] = set()
        source_bases_normalized.add(normalize_repo_url_for_comparison(source_repo.base_url()))
        if all_source_repos:
            for repo_url in all_source_repos:
                source_bases_normalized.add(normalize_repo_url_for_comparison(repo_url))

        for dep in redirect_package.dependencies:
            # Check if the dependency's repository matches ANY of the source repos
            # Use the same normalization to handle URL variations
            dep_repo_normalized = normalize_repo_url_for_comparison(dep.repository)
            if dep_repo_normalized in source_bases_normalized:
                dep.repository = target_dep_repo

    return redirect_package


def create_redirect_package_for_single(
    source_package: PackageInfo,
    source_repo_url: str,
    redirect_repo_url: str,
    external_url: Optional[str] = None,
) -> PackageInfo:
    """
    Helper function to create a redirect package for a single package.

    This is a convenience wrapper for create_redirect_package that accepts
    repository URLs instead of Repository objects.

    :param source_package: The source PackageInfo to create a redirect for
    :param source_repo_url: URL of the source repository
    :param redirect_repo_url: URL of the redirect repository
    :param external_url: External base URL for source_path entries. If specified, the source repo
                        base URL will be replaced with this URL in source_path entries.
    :return: A new PackageInfo configured as a redirect package
    """
    source_repo = Repository(repo_url=source_repo_url)
    redirect_repo = Repository(repo_url=redirect_repo_url)

    return create_redirect_package(
        source_package=source_package,
        source_repo=source_repo,
        redirect_repo=redirect_repo,
        package_name=source_package.name,
        version=source_package.version,
        external_url=external_url,
    )
