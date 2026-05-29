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

import os
from pathlib import Path

import pytest

from wrapp.commands.install import modify_package_file_with_added_dependency
from wrapp.commands.uninstall import remove_dependency_from_package_file
from wrapp.datastructures.package import *


def make_tmpfile_name(tmp_path: Path) -> str:
    """Return a path string under *tmp_path* for a temporary wrapp file.

    Uses pytest's ``tmp_path`` fixture so cleanup is automatic —
    no orphaned temp files.
    """
    return str(tmp_path / "wrapp_test.tmp")


from .wrapp_sync_funcs import wrapp_url_exists_sync


def create_package_info(
    package_name: str,
    package_version: str,
    catalog: Optional[Catalog] = None,
    repo: Optional[str] = None,
) -> PackageInfo:
    return PackageInfo(name=package_name, version=package_version, catalog=catalog, dependencies=None, repository=repo)


@pytest.mark.asyncio
async def test_write_package(storage_backend, tmp_path):
    package_info = create_package_info("my_package", "1.0.1", None)
    wrapp_package_file = make_tmpfile_name(tmp_path)
    await write_package_info(wrapp_package_file, package_info)
    assert wrapp_url_exists_sync(wrapp_package_file)


@pytest.mark.asyncio
async def test_load_package(storage_backend, tmp_path):
    package_info = create_package_info("my_package", "1.0.1", None)
    wrapp_package_file = make_tmpfile_name(tmp_path)
    await write_package_info(wrapp_package_file, package_info)
    assert wrapp_url_exists_sync(wrapp_package_file)
    setup = await read_package_info(wrapp_package_file)
    assert setup.name == "my_package"
    assert setup.version == "1.0.1"


@pytest.mark.asyncio
async def test_dependencies(storage_backend, tmp_path):
    package_info = create_package_info("my_package", "1.0.1", None)
    wrapp_package_file = make_tmpfile_name(tmp_path)
    await write_package_info(wrapp_package_file, package_info)
    assert wrapp_url_exists_sync(wrapp_package_file)
    await modify_package_file_with_added_dependency(
        CommandParameters(),
        wrapp_package_file,
        "i_depend_on_this",
        "in_this_version",
        repo=Repository(repo_url="omniverse://remote"),
        relative_destination="installedhere",
    )

    setup = await read_package_info(wrapp_package_file)
    assert setup.name == "my_package"
    assert setup.version == "1.0.1"
    assert "source_url" not in setup  # This is a deprecated field that should no longer be set
    assert len(setup.dependencies) > 0
    assert setup.dependencies[0].package == "i_depend_on_this"
    assert setup.dependencies[0].version == "in_this_version"
    assert setup.dependencies[0].repository == "omniverse://remote"

    # One more, it should be a list!
    await modify_package_file_with_added_dependency(
        CommandParameters(),
        wrapp_package_file,
        "second dep",
        "0.0.1",
        repo=Repository(repo_url="omniverse://remote"),
        relative_destination="installedhere",
    )

    setup = await read_package_info(wrapp_package_file)
    assert len(setup.dependencies) > 1
    assert setup.dependencies[1].package == "second dep"
    assert setup.dependencies[1].version == "0.0.1"

    assert find_dependency(setup, "i_depend_on_this", "installedhere") is not None
    assert find_dependency(setup, "i_depend_on_this", "notinstalledhere") is None
    assert find_dependency(setup, "not available", "installedhere") is None
    assert find_dependency(setup, "second dep", "installedhere").version == "0.0.1"

    await remove_dependency_from_package_file(CommandParameters(), wrapp_package_file, "second dep", "installedhere")
    setup = await read_package_info(wrapp_package_file)
    assert len(setup.dependencies) == 1


@pytest.mark.asyncio
async def test_migrate_from_version_1(storage_backend):
    version1 = join_paths(os.path.dirname(__file__), "test_data", ".format_version_1.wrapp")
    loaded = await read_package_info(version1)
    assert loaded is not None
    assert loaded.name == "cats"
    assert loaded.version == "1.0"
    assert loaded.repository == "omniverse://localhost"


@pytest.mark.asyncio
async def test_migrate_dependencies_from_version_1(storage_backend):
    version1 = join_paths(os.path.dirname(__file__), "test_data", ".dep_format_version_1.wrapp")
    loaded = await read_package_info(version1)
    assert loaded is not None
    assert loaded.name == "shelter"
    assert loaded.version == "1.0"
    assert loaded.repository == "omniverse://localhost"
    assert all([dep.version == "1.0" for dep in loaded.dependencies])
    assert any([dep.repository == "omniverse://dog_host/" for dep in loaded.dependencies])
    assert any([dep.repository == "omniverse://cat_host/" for dep in loaded.dependencies])
    assert any([dep.relative_destination == "cat_folder" for dep in loaded.dependencies])
    assert any([dep.relative_destination == "dog_folder" for dep in loaded.dependencies])


def test_detect_common_catalog_root():
    from wrapp.datastructures.catalog import (
        Catalog,
        CatalogItem,
    )
    from wrapp.datastructures.package import _detect_common_catalog_root

    # Test with items sharing a common root
    catalog = Catalog(
        root="file:///wrong/root",
        items=[
            CatalogItem(source_path="file:///correct/root/file1.txt", relative_path="file1.txt"),
            CatalogItem(source_path="file:///correct/root/subdir/file2.txt", relative_path="subdir/file2.txt"),
            CatalogItem(source_path="file:///correct/root/subdir/nested/file3.txt", relative_path="subdir/nested/file3.txt"),
        ],
    )
    detected = _detect_common_catalog_root(catalog)
    assert detected == "file:///correct/root"

    # Test with items that need to go up multiple levels to find common root
    catalog = Catalog(
        root="file:///wrong/root",
        items=[
            CatalogItem(source_path="file:///common/branch1/file1.txt", relative_path="file1.txt"),
            CatalogItem(source_path="file:///common/branch2/file2.txt", relative_path="file2.txt"),
        ],
    )
    detected = _detect_common_catalog_root(catalog)
    assert detected == "file:///common"

    # Test empty catalog
    catalog = Catalog(root="file:///some/root", items=[])
    detected = _detect_common_catalog_root(catalog)
    assert detected is None

    # Test catalog with only empty folder placeholders (should return None)
    catalog = Catalog(
        root="file:///some/root",
        items=[
            CatalogItem(source_path="/tmp/.wrapp_preserve_empty_folder", relative_path="empty/.wrapp_preserve_empty_folder"),
        ],
    )
    detected = _detect_common_catalog_root(catalog)
    assert detected is None

    # Test with omniverse URLs
    catalog = Catalog(
        root="omniverse://wrong/host",
        items=[
            CatalogItem(source_path="omniverse://server/project/assets/file1.usd", relative_path="file1.usd"),
            CatalogItem(source_path="omniverse://server/project/assets/subdir/file2.usd", relative_path="subdir/file2.usd"),
        ],
    )
    detected = _detect_common_catalog_root(catalog)
    assert detected == "omniverse://server/project/assets"


def test_package_repo_detection(test_folder_url):
    assert not repository_from_wrapp_file_path(join_paths(test_folder_url, "1.0"))
    assert not repository_from_wrapp_file_path(join_paths(test_folder_url, "mypackage", "1.0"))
    assert not repository_from_wrapp_file_path(join_paths(test_folder_url, ".packages", "mypackage", "1.0"))
    assert not repository_from_wrapp_file_path(join_paths(test_folder_url, ".packages", "mypackage", "1.0", "not_a_wrapp_file"))
    assert not repository_from_wrapp_file_path(join_paths(test_folder_url, ".packages", "mypackage", "1.0", "wrong_package_name.wrapp"))
    assert (
        repository_from_wrapp_file_path(join_paths(test_folder_url, ".packages", "mypackage", "1.0", ".mypackage.wrapp")) == test_folder_url
    )

    # Test on too short URLs
    assert not repository_from_wrapp_file_path("omniverse://localhost/1.0/.wrapp_file.wrapp")
    assert not repository_from_wrapp_file_path("omniverse://localhost/wrapp_file/1.0/.wrapp_file.wrapp")
    assert not repository_from_wrapp_file_path("omniverse://.packages/wrapp_file/1.0/.wrapp_file.wrapp")

    # Some special cases
    assert repository_from_wrapp_file_path("omniverse:///.packages/wrapp_file/1.0/.wrapp_file.wrapp") == "omniverse:"
    assert repository_from_wrapp_file_path("/.packages/wrapp_file/1.0/.wrapp_file.wrapp") == "/"
    assert repository_from_wrapp_file_path(".packages/wrapp_file/1.0/.wrapp_file.wrapp") == ""


@pytest.mark.asyncio
async def test_normalize_install_path_from_user_input():
    invalid_paths_and_msgregexs = [
        (":", "component ':' .* is not a valid directory name"),
        ("/absolute_path", "not a relative path"),
        ("C:\\some_absolute_path", "not a relative path"),
        ("https://host/this_is_a_url", "not a relative path"),
        ("my_path//with_a_double_slash", "double slash"),
        ("my_path/*/with_invalid_character", "component '\\*' .* is not a valid directory name"),
        ("../invalid_install_path", "contains a '..'"),
        ("./invalid_install_path/subfolder/../with_two_dots", "contains a '..'"),
    ]
    for invalid_path_and_msgregex in invalid_paths_and_msgregexs:
        with pytest.raises(FailedCommand, match=invalid_path_and_msgregex[1]):
            normalize_install_path_from_user_input(invalid_path_and_msgregex[0])
    valid_paths = [
        ("", ""),
        (".", "."),
        ("./", "."),
        ("./valid_install_path/", "./valid_install_path"),
        (".\\valid_install_path\\", "./valid_install_path"),
        ("./valid_install_path/with_subfolder", "./valid_install_path/with_subfolder"),
    ]
    for valid_path in valid_paths:
        assert valid_path[1] == normalize_install_path_from_user_input(valid_path[0])
