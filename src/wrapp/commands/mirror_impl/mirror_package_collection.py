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
    Collection,
    Optional,
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
    load_package_setup,
    wrapp_file_in_repo,
)
from wrapp.repo.repository import Repository
from wrapp.utils.storage import (
    wrapp_supports_tags,
    wrapp_url_exists,
)
from wrapp.utils.utils import FailedCommand

from ...utils.scheduler_task_node import SchedulerTaskGroupNode
from .mirror_helper_objs import (
    _MirrorContext,
    _PackageToMirror,
    _UniqueDependencyID,
)

logger = logging.getLogger("omni.wrapp")


class _MirrorPackageCollection:
    def __init__(self, mirror_context: _MirrorContext):
        self._mirror_context = mirror_context
        self._source_location_lookup: dict[UniquePackageLocation, _PackageToMirror] = {}
        self._loaded_packages: dict[UniquePackageLocation, LoadedPackage] = {}

    def packages(self) -> Collection[_PackageToMirror]:
        return self._source_location_lookup.values()

    def get_source_locations(self) -> Collection[UniquePackageLocation]:
        """Return all source package locations that have been discovered for mirroring."""
        return self._source_location_lookup.keys()

    async def _setup_package_to_mirror(
        self,
        unique_location: UniquePackageLocation,
        referenced_by: str,
    ) -> _PackageToMirror:
        """
        Helper function to add a package to the set of packages which will be mirrored and do various checks and pre-load the .wrapp file.

        :param unique_location: Location of the package to be mirrored
        :param referenced_by: URL of the package file that references this package
        """
        # Load the package file
        source_repo = Repository(repo_url=unique_location.repo_url)
        source_package_url = wrapp_file_in_repo(source_repo, unique_location.package_name, unique_location.version)
        logger.info(f"Inspecting source package {source_package_url}")
        source_setup: Optional[PackageInfo] = await load_package_setup(source_package_url)

        if source_setup is None:
            raise FailedCommand(f"Failed to find the source's package file at {source_package_url}")
        elif source_setup.catalog is None:
            raise FailedCommand(f"Source package '{source_package_url}' has no catalog, invalid dependency detected.")

        # Create a LoadedPackage object
        loaded_package = LoadedPackage(source_package_url, source_setup)

        # Determine target repository for this package
        destination_repo_obj = await self.destination_repo(unique_location)
        # Add this package to the list of packages to mirror
        package_to_mirror = _PackageToMirror(
            loaded_package=loaded_package,
            source_repo=source_repo,
            referenced_by=referenced_by,
            destination_repo=destination_repo_obj,
        )

        if self._mirror_context.mirror_tags and not await wrapp_supports_tags(package_to_mirror.destination_repo.base_url()):
            raise FailedCommand(f"Tags are not supported for target destination {package_to_mirror.destination_repo.base_url()}!")

        return package_to_mirror

    async def discover_packages_to_mirror(
        self,
        task_node: SchedulerTaskGroupNode,
        package_name: str,
        version: str,
        source_repo: str,
        recursive: bool,
    ) -> None:
        """
        First phase of the mirror process: Discover all packages that need to be mirrored.

        :param package_name: Name of the top-level package
        :param version: Version of the top-level package
        :param source_repo: URL of the source repository
        :param recursive: Whether to recursively discover dependencies
        """
        # Check if the source repository exists
        repo_object = Repository(repo_url=source_repo)

        # Start with the top-level package
        async with task_node.create_task_group_node("Reading source packages") as discover_task_node:
            await self._discover_package_and_dependencies(
                discover_task_node,
                package_name,
                version,
                repo_object,
                "",  # Top-level package has no referrer
                recursive,
            )

        if len(self.packages()) > 1:
            logger.info(f"Discovered {len(self.packages())} sub-packages in source package")

    async def _discover_package_and_dependencies(
        self,
        task_node: SchedulerTaskGroupNode,
        package_name: str,
        version: str,
        source_repo: Repository,
        referenced_by: str,
        recursive: bool,
    ) -> None:
        """
        Helper function to recursively discover a package and its dependencies.

        :param package_name: Name of the package
        :param version: Version of the package
        :param source_repo: Repository object for the source repository
        :param referenced_by: URL of the package file that references this package
        :param recursive: Whether to recursively discover dependencies
        """
        # Was this source package already processed?
        unique_location = UniquePackageLocation(package_name=package_name, version=version, repo_url=source_repo.base_url())
        package_to_mirror = self.lookup_package_to_mirror_by_source_location(unique_location)
        if package_to_mirror is not None:
            return

        package_to_mirror = await self.add_source_package(unique_location, referenced_by)

        # If recursive is enabled and the package has dependencies, discover them too
        if recursive and package_to_mirror.loaded_package.package.dependencies:
            for dependency in package_to_mirror.loaded_package.package.dependencies:
                dep_repo = Repository(repo_url=dependency.repository)
                task_node.create_task(
                    self._discover_package_and_dependencies(
                        task_node,
                        dependency.package,
                        dependency.version,
                        dep_repo,
                        package_to_mirror.loaded_package.wrapp_file_url,
                        recursive,
                    ),
                    name="Discover package and dependencies",
                )

    async def _calc_all_content_fingerprints(self, task_node: SchedulerTaskGroupNode) -> None:
        """
        Calculate for all source packages and all target packages the fingerprints. This includes
        the not yet existing target packages which have the same fingerprint as the source which will be mirrored there.
        """
        # For fingerprinting we need to have loaded all dependencies of the loaded and existing packages in the mirrored set.
        async with task_node.create_task_group_node("Loading mirrored packages for fingerprinting") as load_mirrored_pkgs_task_node:
            for package in self._source_location_lookup.values():
                loaded_package_deps = deps_not_loaded(package.loaded_package, self._loaded_packages)
                if loaded_package_deps:
                    load_mirrored_pkgs_task_node.create_task(
                        load_deps_for_fingerprinting(loaded_package_deps, self._loaded_packages, load_mirrored_pkgs_task_node),
                        name="Load deps for fingerprinting",
                    )
                if package.existing_package is not None:
                    existing_package_deps = deps_not_loaded(package.existing_package, self._loaded_packages)
                    if existing_package_deps:
                        load_mirrored_pkgs_task_node.create_task(
                            load_deps_for_fingerprinting(existing_package_deps, self._loaded_packages, load_mirrored_pkgs_task_node),
                            name="Load deps for fingerprinting",
                        )

        # We need to make sure we can load the package files for all dependencies of all packages which do not have a
        # fingerprint stored (as of writing this code that is all of them, but we will hopefully eventually store fingerprints
        # in the .wrapp files).
        async with task_node.create_task_group_node(
            "Loading unmirrored package dependencies for fingerprinting"
        ) as load_unmirrored_pkgs_task_node:
            for loaded_package in list(self._loaded_packages.values()):
                deps_to_load = deps_not_loaded(loaded_package, self._loaded_packages)
                if deps_to_load:
                    # Make sure all dependencies of the existing package are loaded, too, to enable fingerprints
                    load_unmirrored_pkgs_task_node.create_task(
                        load_deps_for_fingerprinting(deps_to_load, self._loaded_packages, load_unmirrored_pkgs_task_node),
                        name="Load deps for fingerprinting",
                    )

        # Now calculate the fingerprints for all source packages which need to be compared to an existing package.
        # We want to avoid calculating fingerprints we do not need to avoid failing the command if a dependency is missing:
        # if a source package's unmirrored dependencies cannot be read we do not want to fail the command unless we need the fingerprint.
        for package in self._source_location_lookup.values():
            if package.existing_package is not None or package.is_marked_as_overlapped:
                package.loaded_package.fingerprint(loaded_packages=self._loaded_packages)

        # If we only have the _loaded_packages (i.e. the source packages and the ones which are already in the destination repo)
        # in loaded_packages_by_wrapp_file_url, the fingerprint function will encounter a FingerprintError in the following
        # use case:
        # A mirror operation is executed to mirror two packages A and B with A depending on B. The mirror operation is aborted
        # at the point where A has already been mirrored, but B has not. So A exists in the destination repo with a dependency on B
        # in the destination repo which does not exist.
        # If the same mirror operations is run again, this set up causes the fingerprint calculation to raise FingerprintError when
        # calculating the fingerprint for A because the fingerprint for B in the destination cannot be calculated.
        # In general, we cannot calculate fingerprints for packages with missing dependencies. However, in this particular case we do
        # know the fingerprint of the missing package, as we will mirror B into the destination repo
        # (assuming it will be mirrored successfully). So in anticipation, add set the fingerprint for all destination packages
        # which will be created by the mirror operation to the value of the source package.
        known_dependency_fingerprints: dict[str, str] = {}
        for package in self._source_location_lookup.values():
            if not package.existing_package:
                dest_wrapp_file = wrapp_file_in_repo(package.destination_repo, package.package_name, package.version)
                # We expect that the fingerprint has been pre-calculated for all loaded packages previously.
                cached_fingerprint = package.loaded_package.cached_fingerprint()
                if cached_fingerprint is not None:
                    known_dependency_fingerprints[dest_wrapp_file] = cached_fingerprint
                else:
                    # This is the case of a source package for which no corresponding target package exists, and which in the loop
                    # above was not fingerprinted as one of the dependencies of one of the packages which do need to be fingerprinted.
                    # Example: a mirror without dependencies of a package with a missing dependency and empty target repo.
                    # Those packages never need to be compared, and we do not need their fingerprints.
                    pass

        # Now calculate the fingerprints for all existing packages.
        for package in self._source_location_lookup.values():
            if package.existing_package is not None:
                package.existing_package.fingerprint(
                    loaded_packages=self._loaded_packages, known_dependency_fingerprints=known_dependency_fingerprints
                )

    async def check_existing_packages(self, task_node: SchedulerTaskGroupNode) -> None:
        """
        Second phase of the mirror process: Check which packages already exist in the target repository.
        """
        async with task_node.create_task_group_node("Checking existing packages") as check_existing_packages_task_node:
            async with check_existing_packages_task_node.create_task_group_node(
                "Loading packages available in target"
            ) as loading_task_node:
                for package in self._source_location_lookup.values():
                    if not package.has_been_checked_in_dest:
                        loading_task_node.create_task(self._load_existing_package(package), name="Load existing package")

            # Calculate the fingerprints for all packages
            async with check_existing_packages_task_node.create_task_group_node("Calculate fingerprints for packages") as calc_task_node:
                await self._calc_all_content_fingerprints(calc_task_node)

            async with check_existing_packages_task_node.create_task_group_node("Checking packages available in target") as check_task_node:
                for package in self._source_location_lookup.values():
                    if package.existing_package and not package.is_marked_as_overlapped and not package.has_been_mirrored:
                        check_task_node.create_task(
                            self.check_for_installed_package_conflict(package), name="Check for installed package conflict"
                        )

            # Count how many packages need to be mirrored
            packages_to_mirror_count = sum(1 for p in self._source_location_lookup.values() if p.needs_mirroring)
            logger.info(f"{packages_to_mirror_count} out of {len(self._source_location_lookup)} packages need to be mirrored")

    async def _load_existing_package(self, package_to_mirror: _PackageToMirror) -> None:
        # Check if the target package already exists, and if so load it.
        package_to_mirror.has_been_checked_in_dest = True
        if await package_to_mirror.destination_repo.find_package(package_to_mirror.package_name, package_to_mirror.version):
            target_wrapp_file = wrapp_file_in_repo(
                package_to_mirror.destination_repo, package_to_mirror.package_name, package_to_mirror.version
            )
            try:
                # Load the package from the destination repository. Avoid the warning if the file does not exist (which is
                # the normal case for "resume")
                if await wrapp_url_exists(target_wrapp_file):
                    existing_package = await LoadedPackage.get_from_url(target_wrapp_file)
                    package_to_mirror.existing_package = existing_package
            except FailedCommand as e:
                # If we can't load the package, it might be corrupted or incomplete
                logger.warning(
                    f"Failed to load package {package_to_mirror.package_name} {package_to_mirror.version} from target repository '{package_to_mirror.destination_repo.base_url()}': {e}"
                )

    async def check_for_installed_package_conflict(
        self,
        package_to_mirror_1: _PackageToMirror,
        package_to_mirror_2: _PackageToMirror | None = None,
    ):
        """
        Check if the installed package generated by either
        * a source package being installed to a target location with an existing package
        causes a conflict, i.e. the installed package is not unambiguously defined or
        * two source packages which are not identical are installed to the same location.
        """
        # Compare the packages - we compare the source package (package_to_mirror_1) with either the package in the target (if it exists
        # (for package_to_mirror_2 is None) or we compare it to the other source package. In all cases we need to make sure
        # that the package that would be created by the mirror is identical with either what is already there, or what both packages
        # would generate.
        package_1 = package_to_mirror_1.loaded_package
        if package_to_mirror_2 is None:
            if not package_to_mirror_1.has_been_checked_in_dest:
                await self._load_existing_package(package_to_mirror_1)
            if not package_to_mirror_1.existing_package:
                # Nothing to check - there is no package in the target location and no second source for the target location.
                return
            package_2 = package_to_mirror_1.existing_package
        else:
            if package_to_mirror_1.unique_dest_location != package_to_mirror_2.unique_dest_location:
                raise RuntimeError(
                    f"_check_installed_package_for_conflict was called with packages being installed to different locations: {package_to_mirror_1.unique_dest_location} != {package_to_mirror_2.unique_dest_location}!"
                )
            package_2 = package_to_mirror_2.loaded_package

        # Compare the content of both packages.
        try:
            if package_1.fingerprint(loaded_packages=self._loaded_packages) != package_2.fingerprint(loaded_packages=self._loaded_packages):
                raise FailedCommand(
                    f"There is already a package {package_2.package.name} {package_2.package.version} in the target repository (wrapp file '{package_2.wrapp_file_url}'), but it is different from the one in {package_to_mirror_1.source_repo.base_url()} referenced by {package_to_mirror_1.referenced_by}"
                    if package_to_mirror_2 is None
                    else f"Package {package_1.package.name} {package_1.package.version} is mirrored from repository {package_1.package.repository} and repository {package_2.package.repository}, but the two source packages have different content."
                )
        except FingerprintError as e:
            raise FailedCommand(
                f"Failed to calculate fingerprint for package '{e.package_file}' because it references missing package '{e.package_missing}'."
            ) from e

        # The packages not only need to have identical content, their dependencies also need to end up in the same target locations
        # after being mirrored.
        package_1_deps_after_mirror = await self._dependencies_after_mirror(package_1)
        if package_to_mirror_2 is None:
            package_2_deps_after_mirror = await self._dependencies_from_existing_package(package_2)
        else:
            package_2_deps_after_mirror = await self._dependencies_after_mirror(package_2)
        for dep_2 in package_2_deps_after_mirror.items():
            dep_2_id: _UniqueDependencyID = dep_2[0]
            dep_2_dest_loc: UniquePackageLocation = dep_2[1]
            dep_1_dest_loc = package_1_deps_after_mirror.get(dep_2_id, None)
            if dep_1_dest_loc is None:
                raise RuntimeError(
                    f"Dependency {dep_2_id} of package {package_2.wrapp_file_url} is not part of the dependencies of the compared package supposedly identical in content! ({dep_2_id} not in [{','.join([str(k) for k in package_1_deps_after_mirror.keys()])}])"
                )
            if dep_1_dest_loc != dep_2_dest_loc:
                raise FailedCommand(
                    f"Package '{package_1.package.name}' '{package_1.package.version}' already exists in its target repository '{package_to_mirror_1.destination_repo.base_url()}', but the dependency installed to path '{dep_2_id.install_path}' for package '{dep_2_id.package_name}' is different ('{dep_1_dest_loc}' != '{dep_2_dest_loc}')"
                    if package_to_mirror_2 is None
                    else f"Package {package_1.package.name} {package_1.package.version} is mirrored from repository {package_1.package.repository} and repository {package_2.package.repository}, but the dependency installed by both packages to path '{dep_2_id.install_path}' for package '{dep_2_id.package_name}' is different, ('{dep_1_dest_loc}' != '{dep_2_dest_loc}')"
                )
        logger.info(
            f"Package {package_1.wrapp_file_url} and {package_2.wrapp_file_url} are identical and both mapped to the same target location {package_to_mirror_1.unique_dest_location}. The package is mirrored from {package_1.wrapp_file_url}."
        )

    async def add_source_package(self, unique_source_location: UniquePackageLocation, referenced_by: str) -> _PackageToMirror:
        # Was the source package already processed?
        package = self.lookup_package_to_mirror_by_source_location(unique_source_location)
        if package is None:
            package = await self._setup_package_to_mirror(unique_source_location, referenced_by)
            self._loaded_packages[unique_source_location] = package.loaded_package
            self._source_location_lookup[unique_source_location] = package
        return package

    async def destination_repo(self, unique_source_location: UniquePackageLocation) -> Repository:
        _package_to_mirror = self.lookup_package_to_mirror_by_source_location(unique_source_location)
        if _package_to_mirror is not None:
            return _package_to_mirror.destination_repo
        repo_url = await self._mirror_context.safe_package_to_repo_callback(unique_source_location)
        return Repository(repo_url=repo_url)

    def lookup_package_to_mirror_by_source_location(self, loc: UniquePackageLocation) -> Optional[_PackageToMirror]:
        return self._source_location_lookup.get(loc, None)

    async def mark_overlapping_source_packages(self) -> None:
        dest_location_lookup: dict[UniquePackageLocation, _PackageToMirror] = {}
        for p in self._source_location_lookup.values():
            unique_dest_location = UniquePackageLocation(p.package_name, p.version, p.destination_repo.base_url())
            p_alt = dest_location_lookup.get(unique_dest_location, None)
            if p_alt is None:
                dest_location_lookup[unique_dest_location] = p
                continue
            # There is another source package mapping to the same destination location. Make sure it is identical.
            await self.check_for_installed_package_conflict(p, p_alt)
            # Only mirror one of the packages
            p.mark_package_as_overlapped_by(p_alt)

    async def _dependencies_after_mirror(self, source_package: LoadedPackage) -> dict[_UniqueDependencyID, UniquePackageLocation]:
        """
        Return the location for all dependencies for a package as they will be after the package has been mirrored.

        Args:
            source_package: The package which will be mirrored.

        Returns:
            A dictionary mapping dependency install path and package name to the source location of the dependency
            after the mirror (i.e. as the dependencies will appear in the installed package).
        """
        dependencies_after_mirror: dict[_UniqueDependencyID, UniquePackageLocation] = {}
        for dep in source_package.package.dependencies or []:
            dep_id = _UniqueDependencyID.from_dep(dep)
            dep_loc = UniquePackageLocation(dep.package, dep.version, dep.repository)
            # The dependencies must be all part of the mirrored set, too: two source packages will
            # only be mapped to the same target location if more than one package is mirrored, i.e. this can only be
            # mirror_with_dependencies, and cannot be a single package mirror (with or without redirect_dependencies).
            # So all dependencies must be in self._source_location_lookup.
            dep_to_mirror = self.lookup_package_to_mirror_by_source_location(dep_loc)
            if dep_to_mirror:
                dependencies_after_mirror[dep_id] = dep_to_mirror.unique_dest_location
            else:
                logger.debug(
                    f"Dependency {dep_loc} of package {source_package.wrapp_file_url} is not part of the mirrored set. Using callback to determine destination repo."
                )
                dep_dest_repo = await self.destination_repo(dep_loc)
                dependencies_after_mirror[dep_id] = UniquePackageLocation(dep.package, dep.version, dep_dest_repo.base_url())
        return dependencies_after_mirror

    async def _dependencies_from_existing_package(
        self, existing_package: LoadedPackage
    ) -> dict[_UniqueDependencyID, UniquePackageLocation]:
        """
        Return the dependencies for an existing package in the destination repo in the same format as _dependencies_after_mirror.

        Args:
            existing_package: The package which already is in the destination repository (i.e. does not need to be mirrored).

        Returns:
            A dictionary mapping dependency install path and package name to the source location of the dependency.
        """
        dependencies: dict[_UniqueDependencyID, UniquePackageLocation] = {}
        for dep in existing_package.package.dependencies or []:
            dep_id = _UniqueDependencyID.from_dep(dep)
            dependencies[dep_id] = UniquePackageLocation(dep.package, dep.version, dep.repository)
        return dependencies

    def determine_destination_repos(self) -> set[Repository]:
        dest_repos = set()
        for package_id, package in self._source_location_lookup.items():
            dest_repos.add(package.destination_repo)
        return dest_repos
