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

import pytest

import wrapp.commands
from wrapp import FailedCommand
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    read_package_info,
    wrapp_file_path,
    write_package_info,
)
from wrapp.datastructures.previous_versions.catalog_old_versions import (
    Catalog_V3_0,
    CatalogItem_V2,
)
from wrapp.datastructures.previous_versions.package_info_old_versions import (
    PackageInfo_V4_0,
)

from .common import (
    create_unnamed_package,
    generate_named_package,
    join_paths,
)


@pytest.mark.asyncio
async def test_status_with_changed_format_OMPE60794(test_folder_url, storage_backend, local_tmp_folder, source_package):
    #
    # Note - this test does not reproduce the problem reported in OMPE60794, because in that scenario we had seen
    # wrapp files being part of the catalog, and being compared in via size and hash by the workspace_analysis
    # function.
    # Sadly, the project producing the error in question has been deleted. I want to keep this test anyway because it might
    # be interesting for other scenarios of failure.
    #
    super_package = await generate_named_package(storage_backend, "super_package", "1", join_paths(test_folder_url, "super"))
    sub_package = await create_unnamed_package(
        storage_backend, "sub_package", "1", join_paths(test_folder_url, "sub"), repo_folder=test_folder_url
    )
    _ = await create_unnamed_package(
        storage_backend, "subsub_package", "1", join_paths(test_folder_url, "subsub"), repo_folder=test_folder_url
    )
    # Install subsub package into sub package, nesting level 1
    await wrapp.install(
        "subsub_package", "1", join_paths(test_folder_url, "sub/subsub_location"), repo=test_folder_url, package=sub_package.package_file
    )
    # Create a new version of sub_package containing subsub
    await wrapp.create("sub_package", "2", sub_package.path, catalog=False, repo=test_folder_url)
    # Install sub package into super package, double level nesting
    await wrapp.install(
        "sub_package", "2", join_paths(test_folder_url, "super/sub_location"), repo=test_folder_url, package=super_package.package_file
    )
    # We can create the package
    await wrapp.create("super_package", "2", super_package.path, catalog=False, repo=test_folder_url)
    # We can install it
    test_install = join_paths(test_folder_url, "test_install")
    await wrapp.install("super_package", "2", test_install, repo=test_folder_url)
    test_install_wrapp = wrapp_file_path(test_install, "super_package")
    # Make sure we get a clean status on that
    dirty_packages, details = await wrapp.status(test_install_wrapp)
    assert len(dirty_packages) == 0

    # Now comes the trick - what if the package file of the subsub_package would have an old version?
    installed_sub_wrapp = wrapp_file_path(join_paths(test_install, "sub_location"), "sub_package")
    installed_subsub_wrapp = wrapp_file_path(join_paths(test_install, "sub_location/subsub_location"), "subsub_package")
    current_version = await LoadedPackage.get_from_url(installed_subsub_wrapp)
    assert current_version
    wrong_catalog = await wrapp.catalog(join_paths(test_install, "sub_location"))

    # Hack us an old package catalog
    old_items = [
        CatalogItem_V2(source_path=i.source_path, relative_path=i.relative_path, hash=i.hash, size=i.size, tags=i.tags)
        for i in current_version.package.catalog.items
    ]
    old_catalog = Catalog_V3_0(root=current_version.package.catalog.root, items=old_items)
    old_package = PackageInfo_V4_0(
        name=current_version.package.name,
        version=current_version.package.version,
        catalog=old_catalog,
        repository=current_version.package.repository,
    )
    await old_package.save(wrapp.CommandParameters(), installed_subsub_wrapp)

    # Check the status again
    dirty_packages, details = await wrapp.status(test_install_wrapp)
    assert len(dirty_packages) == 0

    # Now create a new version
    await wrapp.create("super_package", "3", test_install, catalog=False, repo=test_folder_url)

    # Second part of the trick - the super package somehow had the wrapp file in its catalog (wrapp files don't belong into the catalog, they belong into the dependencies)
    sub_package = await read_package_info(installed_sub_wrapp)
    sub_package.catalog.items.extend([item for item in wrong_catalog.items if item.relative_path.endswith("subsub_package.wrapp")])
    await write_package_info(installed_sub_wrapp, sub_package)

    # Check the stat us again, now incorrectly two packages would be reported
    dirty_packages, details = await wrapp.status(test_install_wrapp)
    assert len(dirty_packages) == 2

    # And create will fail
    with pytest.raises(FailedCommand, match="Detected dirty content"):
        await wrapp.create("super_package", "4", test_install, catalog=False, repo=test_folder_url)
