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
import json
import logging
from collections import deque
from typing import (
    Deque,
    Optional,
)

from pydantic import BaseModel

from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
)
from wrapp.datastructures.package import (
    Dependency,
    PackageInfo,
    UniquePackageLocation,
    collect_wrapp_files_in_directory,
    find_dependency,
    load_package_setup,
    wrapp_file_in_repo,
    wrapp_file_path,
)
from wrapp.repo.repository import Repository
from wrapp.utils.scheduler_task_node import (
    SchedulerTaskGroupNode,
    SchedulerTreeNode,
)
from wrapp.utils.storage import (
    wrapp_capture_file_observation,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_base_directory_from_url,
    is_relative_path,
    is_wrapp_file_url,
    join_url_path,
    make_relative_url,
)

logger = logging.getLogger("omni.wrapp")


class FingerprintError(Exception):
    def __init__(self, package_file: str, package_missing: str) -> None:
        self.package_file = package_file
        self.package_missing = package_missing
        super().__init__(f"Missing dependency '{package_missing}' referenced from '{package_file}'")


class PackageRecord(BaseModel):
    relative_path: str
    package: PackageInfo
    absolute_path: str


# TODO This should be renamed to WrappFile or WrappPackage
class LoadedPackage:
    def __init__(self, wrapp_file_url: str, package: PackageInfo):
        if not is_wrapp_file_url(wrapp_file_url):
            raise ValueError(f"wrapp_file_url is expected to point at a wrapp file, but is {wrapp_file_url}")
        self.wrapp_file_url = wrapp_file_url
        self.install_base = get_base_directory_from_url(wrapp_file_url)
        self.package = package
        self._fingerprint: Optional[str] = None

    @staticmethod
    async def get_from_url(wrapp_file_url: str) -> "LoadedPackage":
        package_info = await load_package_setup(wrapp_file_url)
        if package_info is None:
            raise FailedCommand(f"cannot load package from {wrapp_file_url}")
        return LoadedPackage(wrapp_file_url, package_info)

    @staticmethod
    def from_package_record(wrapp_package: PackageRecord):
        return LoadedPackage(wrapp_file_url=wrapp_package.absolute_path, package=wrapp_package.package)

    def cached_fingerprint(self) -> str | None:
        return self._fingerprint

    def fingerprint(
        self,
        *,
        loaded_packages: dict[UniquePackageLocation, "LoadedPackage"],
        known_dependency_fingerprints: dict[str, str] | None = None,
        already_visited: set[str] | None = None,
    ) -> str:
        if not self._fingerprint:
            if already_visited is None:
                already_visited = set()

            if self.wrapp_file_url in already_visited:
                raise Exception("Circular dependency detected during fingerprint calculation")

            # Add this package to the visited set before processing dependencies
            already_visited = already_visited.union({self.wrapp_file_url})

            # We need to calculate the fingerprint for this package
            own_fingerprint = self._catalog_fingerprint()
            # If we have dependencies, we need the fingerprints of the dependencies as well...
            if self.package.dependencies:
                fingerprints: list[str] = [own_fingerprint]
                for dep in self.package.dependencies:
                    dep_repo = Repository(repo_url=dep.repository)
                    wrapp_file = wrapp_file_in_repo(dep_repo, dep.package, dep.version)
                    dep_fingerprint = known_dependency_fingerprints.get(wrapp_file, None) if known_dependency_fingerprints else None
                    if dep_fingerprint is None:
                        try:
                            dep_loc = UniquePackageLocation(dep.package, dep.version, dep_repo.base_url())
                            dep_package = loaded_packages[dep_loc]
                        except KeyError as e:
                            # All required packages must be preloaded in loaded_packages
                            raise FingerprintError(self.wrapp_file_url, wrapp_file) from e
                        dep_fingerprint = dep_package.fingerprint(
                            loaded_packages=loaded_packages,
                            known_dependency_fingerprints=known_dependency_fingerprints,
                            already_visited=already_visited,
                        )
                    fingerprints.append(dep_fingerprint)
                fingerprint_list_as_str = ",".join(fingerprints)
                own_fingerprint = hashlib.sha256(fingerprint_list_as_str.encode(), usedforsecurity=False).hexdigest()
            self._fingerprint = own_fingerprint
        return self._fingerprint

    def _catalog_fingerprint(self) -> str:
        if self.package.catalog:
            catalog_copy = self.package.catalog.model_copy(deep=True)
            catalog_copy.root = None
            catalog_copy.storage_map = None
            for item in catalog_copy.items:
                # Clear location-dependent fields that don't affect actual content
                item.source_path = ""
                item.resource_identity = None
            # Preserve the established v5 content projection. Neither new
            # observation fields nor serialization-version changes are content.
            fingerprint_data = catalog_copy.model_dump(exclude={"items": {"__all__": {"resource_identity_v2", "modified_time"}}})
            fingerprint_data["format_version"] = {"version_string": "5.0"}
            catalog_str = json.dumps(fingerprint_data, sort_keys=True)
            catalog_fingerprint = "wrappcatalog-fingerprint;" + hashlib.sha256(catalog_str.encode(), usedforsecurity=False).hexdigest()
            return catalog_fingerprint
        else:
            raise Exception("No catalog in package, can't calculate fingerprint")

    def package_fingerprint(self) -> tuple[str, int]:
        # This is a quick fix for OMPE-47578
        # For WRAPP files we cannot use the normal file hash - if we just use the file hash,
        # on a second install we get conflicts because the install will change the "repository" field
        # in the subpackage .wrapp files.
        # In general, the fields "repository" and "source" are not relevant to determine if there is a
        # difference in the catalog. As long as two packages have the same catalog, they have the same content.
        # The catalog does not need to be compared, as we are adding it explicitly to the result of this function.
        # We still compare package name and version and the dependencies (including the repository the
        # dependencies comes from) - not sure if that is actually what we want to do, but this is meant
        # to be a minimal change. To be clarified in OMPE-42598.

        hashed_package_info = self.package.model_copy(deep=True)
        hashed_package_info.catalog = None
        hashed_package_info.repository = None
        hashed_package_info.source = None
        if hashed_package_info.dependencies:
            for dependency in hashed_package_info.dependencies:
                dependency.repository = None
        fingerprint_data = hashed_package_info.model_dump()
        fingerprint_data["format_version"] = {"version_string": "5.2"}
        hashed_package_info_str = json.dumps(fingerprint_data, sort_keys=True)
        package_fingerprint = "wrappfile-fingerprint;" + hashlib.sha256(hashed_package_info_str.encode(), usedforsecurity=False).hexdigest()
        return package_fingerprint, len(hashed_package_info_str)

    def dependency_install_path(self, dependency: Dependency):
        found = find_dependency(self.package, dependency.package, dependency.relative_destination)
        if not found:
            raise Exception("Program error - could not locate dependency install path in package setup")
        if is_relative_path(dependency.relative_destination):
            return join_url_path(self.install_base, dependency.relative_destination)
        else:
            return dependency.relative_destination

    # Return a catalog with all files in the subpackages and optionally the .wrapp files for the subpackages.
    async def _internal_calculate_catalog_including_deps(
        self, use_repo_path: bool, add_subpackage_wrapp_files: bool, task_node: SchedulerTreeNode
    ) -> Catalog:
        if self.package.catalog is None:
            raise RuntimeError("can only calculate full catalog from package which has a catalog")
        result = self.package.catalog.model_copy(deep=True)

        # Add the dependencies. Load all directly dependent wrapp files and add their catalog to the result
        async def read_package_with_dependencies(tree_node: SchedulerTreeNode, dep: Dependency):
            dep_install_path = join_url_path(self.install_base, dep.relative_destination)
            if use_repo_path:
                dep_wrapp_file = wrapp_file_in_repo(Repository(repo_url=dep.repository), dep.package, dep.version)
            else:
                dep_wrapp_file = wrapp_file_path(dep_install_path, dep.package)

            dep_package = await LoadedPackage.get_from_url(dep_wrapp_file)

            sub_catalog = await dep_package._internal_calculate_catalog_including_deps(
                use_repo_path=use_repo_path, add_subpackage_wrapp_files=add_subpackage_wrapp_files, task_node=tree_node
            )
            if add_subpackage_wrapp_files:
                source_base = get_base_directory_from_url(dep_package.wrapp_file_url)
                relative_path = make_relative_url(source_base, dep_package.wrapp_file_url)
                observation = await wrapp_capture_file_observation(dep_package.wrapp_file_url, relative_path)

                wrapp_file_catalog = Catalog(
                    root=source_base,
                    items=[
                        CatalogItem(
                            source_path=dep_package.wrapp_file_url,
                            relative_path=relative_path,
                            hash=observation.hash,
                            size=observation.size,
                            resource_identity_v2=observation.resource_identity,
                            modified_time=observation.modified_time,
                        )
                    ],
                )
                sub_catalog._add_catalog(
                    base_path=dep_package.install_base, catalog_base=source_base, additional_catalog=wrapp_file_catalog
                )

            result._add_catalog(base_path=self.install_base, catalog_base=dep_install_path, additional_catalog=sub_catalog)

        if self.package.dependencies:
            async with task_node.create_task_group_node(f"Reading {len(self.package.dependencies)} dependencies") as sub_task_node:
                for dep in self.package.dependencies:
                    sub_task_node.create_task(read_package_with_dependencies(sub_task_node, dep), name="Reading package catalogs")

        return result

    async def calculate_catalog_including_deps(
        self, use_repo_path: bool, add_subpackage_wrapp_files: bool, task_node: SchedulerTreeNode
    ) -> Catalog:
        return await self._internal_calculate_catalog_including_deps(
            use_repo_path,
            add_subpackage_wrapp_files,
            task_node.create_tree_node(f"Calculate full catalog for '{self.package.name}' '{self.package.version}'"),
        )


def deps_not_loaded(
    package: LoadedPackage,
    loaded_packages: dict[UniquePackageLocation, LoadedPackage],
) -> list[UniquePackageLocation]:
    """Return list of dependencies that haven't been loaded yet.

    :param package: The package to check dependencies for
    :param loaded_packages: Dictionary of already loaded packages
    :return: List of UniquePackageLocation for dependencies not yet loaded
    """
    result: list[UniquePackageLocation] = []
    for dep in package.package.dependencies or []:
        dep_loc = UniquePackageLocation(package_name=dep.package, version=dep.version, repo_url=dep.repository)
        if dep_loc not in loaded_packages:
            result.append(dep_loc)
    return result


async def load_deps_for_fingerprinting(
    packages_to_load: list[UniquePackageLocation],
    loaded_packages: dict[UniquePackageLocation, LoadedPackage],
    task_node: SchedulerTaskGroupNode,
) -> None:
    """Recursively load all dependencies needed for fingerprint calculation.

    :param packages_to_load: List of package locations to load
    :param loaded_packages: Dictionary to store loaded packages (modified in place)
    :param task_node: Task node for parallel execution
    """

    async def _perform(sub_package: UniquePackageLocation, wrapp_file: str) -> None:
        logger.info(f"Loading {wrapp_file}")
        try:
            dep_package = await LoadedPackage.get_from_url(wrapp_file)
            loaded_packages[sub_package] = dep_package
            not_loaded = deps_not_loaded(dep_package, loaded_packages)
            if not_loaded:
                # Make sure all dependencies of the existing package are loaded, too, to enable fingerprints
                task_node.create_task(
                    load_deps_for_fingerprinting(not_loaded, loaded_packages, task_node),
                    name="Load deps for fingerprinting",
                )
        except FailedCommand as e:
            # If we can't load the package, it might be missing, corrupted or incomplete. This can happen for dependencies
            # if a mirror_with_dependencies is interrupted and then continued (and then some of the packages in the
            # target repo have missing dependencies).
            logger.debug(
                f"Failed to load package {sub_package.package_name} {sub_package.version} "
                f"from repository '{sub_package.repo_url}' for fingerprinting: {e}"
            )

    for dep_loc in packages_to_load:
        dep_wrapp_file = wrapp_file_in_repo(Repository(repo_url=dep_loc.repo_url), dep_loc.package_name, dep_loc.version)
        task_node.create_task(_perform(dep_loc, dep_wrapp_file), name="Load dependency")


async def collect_wrapp_files_in_tree(context: CommandParameters, task_node: SchedulerTreeNode, root: str) -> list[LoadedPackage]:
    """Given a root URL, find all wrapp files recursively in the tree and return the loaded packages."""

    async def _gather_wrapp_files(node: str, wrapp_files: list[str], to_inspect: Deque[str]):
        if context.verbose:
            logger.info(f"Searching wrapp files in {node}")
        # TODO - this is another tree walk which could be accelerated via enumerate, if the storage supports it
        new_wrapp_files, new_nodes = await collect_wrapp_files_in_directory(node)
        wrapp_files.extend(new_wrapp_files)
        to_inspect.extend(new_nodes)

    task_node = task_node.create_tree_node(f"Reading all WRAPP files in '{root}'")

    to_inspect_next_batch: Deque[str] = deque()
    to_inspect_next_batch.append(root)
    wrapp_files: list[str] = []

    async with task_node.create_task_group_node(f"Finding WRAPP files in {root}") as sub_task_node:
        while to_inspect_next_batch:
            to_inspect_this_batch = to_inspect_next_batch.copy()
            to_inspect_next_batch.clear()
            while to_inspect_this_batch:
                node = to_inspect_this_batch.popleft()
                sub_task_node.create_task(
                    _gather_wrapp_files(
                        node=node,
                        wrapp_files=wrapp_files,
                        to_inspect=to_inspect_next_batch,
                    ),
                    name="Folders searched for wrapp files",
                )
            if not to_inspect_next_batch:
                await sub_task_node.wait_for_all_tasks()

    async def _load_wrapp_file_and_append_to_dependency_list(wrapp_file_to_load: str):
        if context.verbose:
            logger.info(f"Reading wrapp file {wrapp_file_to_load}")
        package: Optional[PackageInfo] = await load_package_setup(wrapp_file_to_load)
        if package is None:
            raise FailedCommand(f"Failed to read wrapp file at {wrapp_file_to_load}")
        real_dependencies.append(LoadedPackage(wrapp_file_to_load, package))

    # Load all the found wrapp files
    real_dependencies: list[LoadedPackage] = []
    async with task_node.create_task_group_node(f"Loading {len(wrapp_files)} WRAPP files") as sub_task_node:
        for wrapp_file in wrapp_files:
            sub_task_node.create_task(
                _load_wrapp_file_and_append_to_dependency_list(
                    wrapp_file_to_load=wrapp_file,
                ),
                name="Wrapp files to read to gather dependencies",
            )

    return real_dependencies


async def collect_super_packages(named_package_setup: LoadedPackage) -> list[LoadedPackage]:
    wrapp_files: list[str] = []
    previous_root = named_package_setup.wrapp_file_url
    while (root := get_base_directory_from_url(previous_root)) != previous_root:
        try:
            new_wrapp_files, _ = await collect_wrapp_files_in_directory(root)
        except (ValueError, StorageOperationError):
            # ValueError: walked above the Azure container root.
            # StorageOperationError: walked outside the address space served
            # by a Storage API backend (INVALID_ARGUMENT from the server).
            break
        wrapp_files = wrapp_files + new_wrapp_files
        previous_root = root

    # Load all the found wrapp files
    super_packages: list[LoadedPackage] = []
    for wrapp in wrapp_files:
        if wrapp == named_package_setup.wrapp_file_url:
            # Skip the input file
            continue
        package: Optional[PackageInfo] = await load_package_setup(wrapp)
        if package is None:
            raise FailedCommand(f"Failed to read wrapp file at {wrapp}")
        super_packages.append(LoadedPackage(wrapp, package))
    return super_packages


async def _recalculate_catalog_resource_identities(catalog: Catalog, context: CommandParameters, scheduler: SchedulerContext) -> None:
    """Explicitly establish v2 for legacy entries, verifying their recorded bytes.

    Stage every observation before changing the catalog; failure must not leave
    a partially promoted catalog behind. Deprecated tokens are never consulted.
    """
    items_without_identities = [item for item in catalog.items if item.resource_identity_v2 is None]

    if not items_without_identities:
        logger.info("All catalog items already have resource identities")
        return

    logger.info(f"Recalculating resource_identity_v2 for {len(items_without_identities)} catalog items")
    observations = []

    async def recalculate_item_resource_identity(item):
        if not item.hash:
            raise FailedCommand(f"Cannot validate identity recalculation for {item.source_path}: no recorded content hash")
        observation = await wrapp_capture_file_observation(
            item.source_path, item.relative_path, expected_hash=item.hash, expected_size=item.size
        )
        observations.append((item, observation))

    # Process all items in parallel
    async with scheduler.parent_task_node().create_task_group_node(
        f"Recalculating {len(items_without_identities)} resource identities"
    ) as task_node:
        for item in items_without_identities:
            task_node.create_task(recalculate_item_resource_identity(item), name="Recalculate resource_identity")
    for item, observation in observations:
        item.resource_identity_v2 = observation.resource_identity
        item.size = observation.size
        item.modified_time = observation.modified_time
