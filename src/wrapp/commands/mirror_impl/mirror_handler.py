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
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from wrapp.commands.catalog import (
    Catalog,
    catalog,
)
from wrapp.commands.freeze import freeze_catalog
from wrapp.datastructures.catalog import CatalogItem
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
)
from wrapp.datastructures.ignore import IgnoreEvaluator
from wrapp.datastructures.package import (
    PackageInfo,
    UniquePackageLocation,
    load_package_setup,
    wrapp_file_name,
    write_package_info,
)
from wrapp.datastructures.patch import Patch
from wrapp.datastructures.three_way_diff import ThreeWayDiff
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage import (
    wrapp_url_exists,
    wrapp_url_is_folder,
)
from wrapp.utils.task_throttler import (
    TaskThrottler,
    ThrottlerType,
)
from wrapp.utils.utils import (
    FailedCommand,
    drop_parameters_from_url,
    join_paths,
)

from .mirror_helper_objs import (
    _MirrorContext,
    _PackageToMirror,
)
from .mirror_package_collection import _MirrorPackageCollection

logger = logging.getLogger("omni.wrapp")


class _MirrorHandler:
    def __init__(
        self,
        safe_package_to_repo_callback: Callable[[UniquePackageLocation], Awaitable[str]],
        mirror_tags: bool,
        context: CommandParameters,
        scheduler: SchedulerContext,
    ):
        self._mirror_context = _MirrorContext(safe_package_to_repo_callback, mirror_tags, context, scheduler)
        self.package_collection = _MirrorPackageCollection(self._mirror_context)

    @staticmethod
    def _build_catalog_match_indexes(catalog_items: List[CatalogItem]) -> Tuple[Set[str], Dict[str, List[str]], Dict[str, List[str]]]:
        relative_paths: Set[str] = set()
        by_hash: Dict[str, List[str]] = {}
        by_file_name: Dict[str, List[str]] = {}
        for item in catalog_items:
            relative_paths.add(item.relative_path)
            if item.hash:
                by_hash.setdefault(item.hash, []).append(item.relative_path)
            file_name = item.relative_path.rsplit("/", 1)[-1]
            by_file_name.setdefault(file_name, []).append(item.relative_path)
        return relative_paths, by_hash, by_file_name

    @staticmethod
    def _diagnose_missing_catalog_path_update(
        item: CatalogItem,
        destination_relative_paths: Set[str],
        destination_paths_by_hash: Dict[str, List[str]],
        destination_paths_by_file_name: Dict[str, List[str]],
    ) -> Tuple[str, List[str], str]:
        normalized_relative_path = drop_parameters_from_url(item.relative_path)

        if normalized_relative_path != item.relative_path and normalized_relative_path in destination_relative_paths:
            return "relative_path_contains_parameters", [normalized_relative_path], normalized_relative_path

        if item.hash is not None and item.hash in destination_paths_by_hash:
            return (
                "matching_hash_found_at_different_relative_path",
                sorted(destination_paths_by_hash[item.hash])[:5],
                normalized_relative_path,
            )

        file_name = item.relative_path.rsplit("/", 1)[-1]
        if file_name in destination_paths_by_file_name:
            return (
                "same_file_name_found_at_different_relative_path",
                sorted(destination_paths_by_file_name[file_name])[:5],
                normalized_relative_path,
            )

        return "no_destination_candidate_found", [], normalized_relative_path

    async def mirror_missing_packages(
        self, task_node: SchedulerTaskGroupNode, resume: bool, max_concurrent_single_package_mirror_ops: int
    ) -> None:
        """
        Third phase of the mirror process: Mirror the missing packages.

        :param resume: Whether to resume a previous mirror operation
        """
        packages_to_mirror = [p for p in self.package_collection.packages() if not p.has_been_mirrored and p.needs_mirroring]
        throttler = TaskThrottler(ThrottlerType.LongRunningServerOp, "LimitingMirrorOps", max_concurrent_single_package_mirror_ops)
        async with task_node.create_task_group_node(
            f"{len(packages_to_mirror)} package(s) need to be mirrored", expected_num_sub_jobs=len(packages_to_mirror)
        ) as mirror_task_node:
            for package in packages_to_mirror:
                mirror_task_node.create_task(
                    throttler.run(
                        self.mirror_single_package(
                            mirror_task_node,
                            package,
                            resume,
                        )
                    ),
                    name=f"Mirror package '{package.package_name}' '{package.version}'",
                )

    async def mirror_single_package(
        self,
        task_node: SchedulerTaskGroupNode,
        package: _PackageToMirror,
        resume: bool,
    ) -> None:
        """
        Mirror a single package from its source repository to the destination repository.

        :param package: Package to mirror
        :param resume: Whether to resume a previous mirror operation
        """
        if package.has_been_mirrored or not package.needs_mirroring:
            return

        async with task_node.create_task_group_node(f"Mirroring '{package.package_name}' '{package.version}'") as mirror_pkg_task_node:
            package_name = package.package_name
            version = package.version
            source_setup = package.loaded_package.package
            source_package_url = package.loaded_package.wrapp_file_url
            source_package_folder = package.loaded_package.install_base

            # Destination path for the package
            destination = package.destination_repo.package_url(package_name, version)

            # If a template was specified, use it
            if package.template_version is not None:
                template_folder = package.destination_repo.package_url(package_name, package.template_version)
                if not await package.destination_repo.find_package(package_name, package.template_version):
                    raise FailedCommand(
                        f"Specified invalid template version {package.template_version} - could not find version of package in {template_folder}"
                    )
                template_catalog_file = join_paths(template_folder, wrapp_file_name(package_name))
                template_catalog: Optional[PackageInfo] = await load_package_setup(template_catalog_file)
                if template_catalog is None:
                    raise FailedCommand(
                        f"No catalog file found for template version, incomplete package? Expected file '{template_catalog_file}'"
                    )
                elif template_catalog.catalog is None:
                    raise FailedCommand(f"Package file {template_catalog_file} has no catalog, nothing to copy")
                else:
                    await freeze_catalog(
                        template_catalog_file,
                        template_folder,
                        template_catalog.catalog.items,
                        dest_dir=destination,
                        copy_tags=self._mirror_context.mirror_tags,
                        context=self._mirror_context.context,
                        scheduler=self._mirror_context.scheduler.create_sub_context(
                            mirror_pkg_task_node.create_tree_node(f"Copying from template version '{package.template_version}'")
                        ),
                    )

            # Check if the destination exists
            if await wrapp_url_exists(destination):
                if not await wrapp_url_is_folder(destination):
                    raise FailedCommand(f"Can't create destination - item exists and is not a folder: {destination}")

                package_file = wrapp_file_name(package_name)
                target_wrapp_file = join_paths(destination, package_file)

                if await wrapp_url_exists(target_wrapp_file):
                    await self.package_collection.check_for_installed_package_conflict(package)
                else:
                    # Resume or template logic, there is a directory in the repo, but no wrapp file
                    if not resume and package.template_version is None:
                        raise FailedCommand("Target package already exists in destination and --resume is not specified")
                    target_as_exists: Catalog = await catalog(
                        root_dir=destination,
                        tags=self._mirror_context.mirror_tags,
                        ignores=None,
                        context=self._mirror_context.context,
                        scheduler=self._mirror_context.scheduler.create_sub_context(
                            mirror_pkg_task_node.create_tree_node(
                                f"Analysing incomplete '{package.package_name}' '{package.version}' in destination"
                            )
                        ),
                    )
                    if source_setup.catalog is None:
                        raise FailedCommand(f"Source package '{source_package_url}' has no catalog, nothing to do")
                    a_dict = source_setup.catalog._catalog_to_dict()
                    b_dict = target_as_exists._catalog_to_dict()
                    patch_list, conflict_list, extras_list = ThreeWayDiff.diff(
                        b_dict, a_dict, a_dict, target_as_exists.root, tags=self._mirror_context.mirror_tags
                    )

                    p = Patch()
                    try:
                        for op in patch_list:
                            p.add(op[1])
                        for op in conflict_list:
                            p.add(op[1])
                        for op in extras_list:
                            p.add(op[1])

                        if not await p.execute(
                            context=self._mirror_context.context,
                            scheduler=self._mirror_context.scheduler.create_sub_context(
                                mirror_pkg_task_node.create_tree_node(
                                    f"Resuming mirror of '{package.package_name}' '{package.version}' to destination"
                                )
                            ),
                        ):
                            raise FailedCommand(
                                f"Failed to apply patch for mirroring {package_name} {version} from {package.source_repo.base_url()} to {package.destination_repo.base_url()}. Please check log for details."
                            )
                    finally:
                        await p.disconnect_tagging_service()
            else:
                # Destination doesn't exist, create it from scratch
                await freeze_catalog(
                    source_package_url,
                    source_package_folder,
                    source_setup.catalog.items if source_setup.catalog is not None else [],
                    destination,
                    ignores=None,
                    copy_tags=self._mirror_context.mirror_tags,
                    context=self._mirror_context.context,
                    scheduler=self._mirror_context.scheduler.create_sub_context(
                        mirror_pkg_task_node.create_tree_node(f"Copy '{package.package_name}' '{package.version}' to destination")
                    ),
                )

            # Create or update the catalog
            if await wrapp_url_exists(destination):
                package_file = wrapp_file_name(package_name)
                mirror_catalog = await catalog(
                    root_dir=destination,
                    tags=self._mirror_context.mirror_tags,
                    ignores=IgnoreEvaluator(root_path="", predicate=lambda x: x == package_file),
                    context=self._mirror_context.context,
                    scheduler=self._mirror_context.scheduler.create_sub_context(
                        mirror_pkg_task_node.create_tree_node(f"Creating catalog file for '{package.package_name}' '{package.version}'")
                    ),
                )
            else:
                # Nothing has been created, the package is empty (OMPE-42648)
                mirror_catalog = Catalog(root=destination, items=[])

            # Create the mirrored package info
            mirror_package = deepcopy(source_setup)
            if mirror_package.catalog is not None:
                mirror_package.catalog.root = destination
                not_updated_files = mirror_package.catalog._update_source_paths(mirror_catalog)
                if not_updated_files:
                    (
                        destination_relative_paths,
                        destination_paths_by_hash,
                        destination_paths_by_file_name,
                    ) = self._build_catalog_match_indexes(mirror_catalog.items)
                    logger.warning(
                        f"Failed to rewrite {len(not_updated_files)} of {len(mirror_package.catalog.items)} source_path entries "
                        f"while mirroring package '{package_name}' '{version}': "
                        f"source_catalog_root='{source_setup.catalog.root if source_setup.catalog else None}' "
                        f"destination_catalog_root='{mirror_catalog.root}'"
                    )
                for item in not_updated_files:
                    reason, candidate_paths, normalized_relative_path = self._diagnose_missing_catalog_path_update(
                        item, destination_relative_paths, destination_paths_by_hash, destination_paths_by_file_name
                    )
                    logger.warning(
                        f"Could not update path of item imported into mirror: reason='{reason}' "
                        f"source_relative_path='{item.relative_path}' "
                        f"normalized_source_relative_path='{normalized_relative_path}' "
                        f"source_hash='{item.hash}' candidate_relative_paths={candidate_paths} "
                        f"source_path='{item.source_path}'"
                    )

            # Update dependencies if needed
            if mirror_package.dependencies:
                for dependency in mirror_package.dependencies:
                    # Look up the dependency in mirror_context to find its destination repository
                    dep_unique_loc = UniquePackageLocation(
                        package_name=dependency.package, version=dependency.version, repo_url=dependency.repository
                    )
                    dest_repo_obj = await self.package_collection.destination_repo(dep_unique_loc)
                    dependency.repository = dest_repo_obj.base_url()

            # Set the repository field to the source repository
            mirror_package.repository = package.source_repo.base_url()

            # Write the package info file
            package_file = wrapp_file_name(package_name)
            await write_package_info(join_paths(destination, package_file), mirror_package, self._mirror_context.context)

            # Mark the package as mirrored
            package.has_been_mirrored = True
            logger.info(f"Done mirroring {package_name} {version} referenced by {package.referenced_by}")
