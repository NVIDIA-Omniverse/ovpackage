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

import copy
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from ..commands.catalog import catalog
from ..datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from ..datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
)
from ..datastructures.dependency_graph import collect_dependencies_in_tree
from ..datastructures.package import (
    PackageInfo,
    is_package_file_name,
    wrapp_file_path,
)
from ..datastructures.two_way_diff import Diff
from ..utils.api_marker import wrapp_public_api
from ..utils.utils import (
    FailedCommand,
    get_base_directory_from_url,
    join_url_path,
    make_relative_url,
)
from .ignore import (
    IgnoreEvaluator,
    filter_catalog_with_ignores,
)
from .loaded_package import (
    LoadedPackage,
    PackageRecord,
)


@wrapp_public_api
class PackageStatusItemType(str, Enum):
    """Enum for the different package error types identified by the wrapp status operations. Each key has its name as a string value."""

    PACKAGE_MODIFIED = "package_modified"  #: This package was modified, a top level error that will be amended by more specific errors
    SUBPACKAGE_MODIFIED = "subpackage_modified"  #: At least one subpackage is modified
    PACKAGE_VERSION_MISMATCH = "package_version_mismatch"  #: There is a version mismatch between the package in the dependency and the wrapp file of the subpackage
    PACKAGE_MISSING = "package_missing"  #: A package file is missing
    EXTRA_PACKAGE = "extra_package"  #: There is an extra package which is not listed as a dependency
    PACKAGE_NO_CATALOG = "package_no_catalog"  #: A package has no catalog, indicating it has never been installed to the location
    ITEM_DIFFERS = "item_differs"  #: An item differs in size or content
    ITEM_MISSING = "item_missing"  #: An item is missing which is listed in the package's catalog
    EXTRA_ITEM = "extra_item"  #: There is an extra item which was not produced by the install operation


@wrapp_public_api
class PackageStatusItem(BaseModel):
    """
    Provides details about a package error found during a wrapp status call.

    Attributes:
        package_name (str): The name of the package
        absolute_wrapp_file_path (str): Location of the package file
        status_message (str): A detailed human-readable description of the package error
        status_item_type (PackageStatusItemType): An enum indicating the class of package error
    """

    package_name: str
    absolute_wrapp_file_path: str
    status_message: str
    status_item_type: PackageStatusItemType


async def collect_workspace_info(
    wrapp_file: str,
    ignore_rules: Optional[IgnoreEvaluator] = None,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: SchedulerContext,
) -> tuple[LoadedPackage, Catalog, PackageInfo, list[PackageRecord], set[str]]:
    """
    Given a top level wrapp file URL, collects all necessary information about a workspace:
    - Loads the package
    - Catalogs the workspace
    - Collects dependencies and builds the dependency graph

    Returns a tuple containing:
    - The loaded package
    - The workspace catalog
    - The package info with enhanced dependencies added after workspace traversal
    - The list of all sub packages loaded
    - A set of extra files not referenced by any package

    :param wrapp_file: Full URL for the top level wrapp file
    :param ignore_rules: Optional IgnoreEvaluator for ignoring items during catalog
    :param context: Global configuration parameters
    :param scheduler: Pre-constructed SchedulerContext
    """
    package = await LoadedPackage.get_from_url(wrapp_file)
    if not package:
        raise FailedCommand(f"Cannot load package file '{wrapp_file}'")

    scheduler = scheduler.create_sub_context_and_node(f"Analysing workspace for '{wrapp_file}'", expected_num_sub_jobs=2)
    # Pass 1 - catalog the workspace, respecting the ignore rules
    root_dir = get_base_directory_from_url(wrapp_file)
    workspace_catalog: Catalog = await catalog(root_dir, checkpoints=False, ignores=ignore_rules, context=context, scheduler=scheduler)
    extra_files = set()
    for item in workspace_catalog.items:
        if not is_package_file_name(item.relative_path) and not item.relative_path.endswith(".wrapp_keep_empty_folder"):
            extra_files.add(item.relative_path)

    # Pass 2 - retrieve all sub packages within the workspace and load them into memory
    # TODO - this is unnecessarily slow, we already have discovered all wrapp files via the Catalog operation above.
    # There is no need to re-traverse the whole tree.
    package_info, sub_package_list, _ = await collect_dependencies_in_tree(
        context, package, package.install_base, scheduler.parent_task_node()
    )

    return package, workspace_catalog, package_info, sub_package_list, extra_files


async def check_dirty_state(
    package: LoadedPackage,
    workspace_catalog: Catalog,
    sub_package_list: list[PackageRecord],
    extra_files: set[str],
    root_dir: str,
    ignore_rules: Optional[IgnoreEvaluator] = None,
    *,
    scheduler: Optional[SchedulerContext] = None,
) -> tuple[list[str], dict[str, list[PackageStatusItem]]]:
    """
    Check if the workspace are dirty.
    Returns a tuple of (dirty_packages, error_messages)

    :param package: The loaded package to check
    :param workspace_catalog: The catalog of the workspace
    :param sub_package_list: List of all sub packages loaded
    :param extra_files: Set of extra files not referenced by any package
    :param root_dir: Root directory of the workspace
    :param ignore_rules: Optional IgnoreEvaluator for ignoring items
    :param scheduler: Pre-constructed SchedulerContext
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present, program error!")
    scheduler = scheduler.create_sub_context_and_node(f"Checking {len(sub_package_list)} subpackages for modifications")

    output_errors: dict[str, list[PackageStatusItem]] = {}

    # For faster access, build a map to find the wrapp info for a wrapp file path, and build a set of all wrapp
    # files to find out if all were referenced
    wrapp_file_map: dict[
        str, PackageRecord
    ] = {}  # This maps absolute wrapp file URL to the PackageRecord, which includes absolute path, relative path, and PackageInfo
    wrapp_files_not_referenced = set()
    # Loop over all files found in the work tree and build our lookup tables
    for sub_package in sub_package_list:
        wrapp_file_map[sub_package.absolute_path] = sub_package
        if sub_package.absolute_path != package.wrapp_file_url:
            # Do not take the root file as not referenced, but all others
            wrapp_files_not_referenced.add(sub_package.absolute_path)

    # Pass 3 - check if the files in the catalog match the workspace
    workspace_map = dict()
    for item in workspace_catalog.items:
        workspace_map[item.relative_path] = item

    dirty_packages_in_order: list[PackageRecord] = []
    dirty_packages_set = set()
    for sub_package in sub_package_list:
        output_errors[sub_package.absolute_path] = []
        package_errors: list[PackageStatusItem] = []
        workspace_file: LoadedPackage = LoadedPackage.from_package_record(sub_package)
        if workspace_file.package.catalog is None:
            package_errors.append(
                PackageStatusItem(
                    package_name=sub_package.package.name,
                    absolute_wrapp_file_path=sub_package.absolute_path,
                    status_item_type=PackageStatusItemType.PACKAGE_NO_CATALOG,
                    status_message="Package is draft package without a catalog. Either create this package or remove it",
                )
            )
        else:
            # Extract the subpackage catalog from the overall catalog
            filtered_sub_catalog = filter_catalog_with_ignores(workspace_file.package.catalog, ignore_rules)
            sub_set_of_items: list[CatalogItem] = []
            for item in filtered_sub_catalog.items:
                item_absolute_path = join_url_path(workspace_file.install_base, item.relative_path)
                item_relative_path = make_relative_url(root_dir, item_absolute_path)
                if item_relative_path in extra_files:
                    extra_files.remove(item_relative_path)
                if item_relative_path in workspace_map:
                    # Need to re-root the item from the workspace catalog to compare it with the subpackage catalog
                    workspace_item = copy.deepcopy(workspace_map[item_relative_path])
                    workspace_item.relative_path = item.relative_path
                    sub_set_of_items.append(workspace_item)

            sub_workspace_catalog = Catalog(root=root_dir, items=sub_set_of_items)

            # Now, run a 2 way diff to detect if there are modified items.
            unique_to_a, unique_to_b, differing_items = Diff.diff_catalogs_internal(
                sub_workspace_catalog, filtered_sub_catalog, Diff.not_to_compare_fields
            )
            assert len(unique_to_a) == 0
            if len(differing_items) > 0 or len(unique_to_b) > 0:
                for deleted_item in unique_to_b:
                    package_errors.append(
                        PackageStatusItem(
                            package_name=sub_package.package.name,
                            absolute_wrapp_file_path=sub_package.absolute_path,
                            status_item_type=PackageStatusItemType.ITEM_MISSING,
                            status_message=f"{deleted_item['relative_path']} is missing",
                        )
                    )
                for different_item_a, different_item_b in differing_items:
                    compare_fields = Diff._build_compare_field_list([different_item_a], [different_item_b], Diff.not_to_compare_fields)
                    error_details = Diff.reason_for_compare_catalog_entries(
                        different_item_a, different_item_b, compare_fields, "workspace", "catalog"
                    )
                    package_errors.append(
                        PackageStatusItem(
                            package_name=sub_package.package.name,
                            absolute_wrapp_file_path=sub_package.absolute_path,
                            status_item_type=PackageStatusItemType.ITEM_DIFFERS,
                            status_message=f"{different_item_a['relative_path']} is locally modified: {error_details}",
                        )
                    )

        if workspace_file.package.dependencies:
            # We need to check the dependency list of this workspace wrapp file
            for dependency in workspace_file.package.dependencies:
                dep_absolute_path = workspace_file.install_base.rstrip("/") + "/" + dependency.relative_destination.lstrip("/")
                sub_wrapp_file = wrapp_file_path(dep_absolute_path, dependency.package)
                if sub_wrapp_file in wrapp_files_not_referenced:
                    wrapp_files_not_referenced.remove(sub_wrapp_file)
                if sub_wrapp_file not in wrapp_file_map:
                    package_errors.append(
                        PackageStatusItem(
                            package_name=sub_package.package.name,
                            absolute_wrapp_file_path=sub_package.absolute_path,
                            status_item_type=PackageStatusItemType.PACKAGE_MISSING,
                            status_message=f"Wrapp file referenced by dependency for package {dependency.package} at {sub_wrapp_file} is missing",
                        )
                    )
                else:
                    if wrapp_file_map[sub_wrapp_file].package.version != dependency.version:
                        package_errors.append(
                            PackageStatusItem(
                                package_name=sub_package.package.name,
                                absolute_wrapp_file_path=sub_package.absolute_path,
                                status_item_type=PackageStatusItemType.PACKAGE_VERSION_MISMATCH,
                                status_message=f"Dependency for package {dependency.package} at {sub_wrapp_file} expects version {dependency.version}, but found version {wrapp_file_map[sub_wrapp_file].package.version}",
                            )
                        )
                    if sub_wrapp_file in dirty_packages_set:
                        package_errors.append(
                            PackageStatusItem(
                                package_name=sub_package.package.name,
                                absolute_wrapp_file_path=sub_package.absolute_path,
                                status_item_type=PackageStatusItemType.SUBPACKAGE_MODIFIED,
                                status_message=f"Subpackage {sub_wrapp_file} is modified",
                            )
                        )

        if sub_package.absolute_path == package.wrapp_file_url:
            # This is the top level package, do some extra checks

            # Pass 4: Check if there are any wrapp files that were not referenced by dependencies. Those would be extra
            # nodes collected by the next create command, hence the top level package is dirty
            if len(wrapp_files_not_referenced) > 0:
                for extra_wrapp in wrapp_files_not_referenced:
                    package_errors.append(
                        PackageStatusItem(
                            package_name=sub_package.package.name,
                            absolute_wrapp_file_path=sub_package.absolute_path,
                            status_item_type=PackageStatusItemType.EXTRA_PACKAGE,
                            status_message=f"wrapp file {make_relative_url(root_dir, extra_wrapp)} not referenced by any package",
                        )
                    )

            # Pass 5: There might be extra files not referenced by any catalog
            if len(extra_files) > 0:
                for extra_file in extra_files:
                    package_errors.append(
                        PackageStatusItem(
                            package_name=sub_package.package.name,
                            absolute_wrapp_file_path=sub_package.absolute_path,
                            status_item_type=PackageStatusItemType.EXTRA_ITEM,
                            status_message=f"extra file at {extra_file} not referenced by any package",
                        )
                    )

        if len(package_errors) > 0:
            status_message = (
                f"{sub_package.package.name} at {sub_package.relative_path} is modified"
                if sub_package.relative_path
                else f"{sub_package.package.name} is modified"
            )
            dirty_error = PackageStatusItem(
                package_name=sub_package.package.name,
                absolute_wrapp_file_path=sub_package.absolute_path,
                status_item_type=PackageStatusItemType.PACKAGE_MODIFIED,
                status_message=status_message,
            )
            dirty_packages_in_order.append(sub_package)
            dirty_packages_set.add(sub_package.absolute_path)
            output_errors[sub_package.absolute_path].append(dirty_error)
            output_errors[sub_package.absolute_path].extend(package_errors)

    return [p.absolute_path for p in dirty_packages_in_order], output_errors
