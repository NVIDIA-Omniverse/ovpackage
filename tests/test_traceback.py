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

import wrapp.commands
from wrapp import (
    PackageLocationType,
    mirror,
)
from wrapp.datastructures.package import (
    read_package_info,
    wrapp_file_in_repo,
    wrapp_file_path,
)
from wrapp.repo.repository import Repository

from .common import *


@pytest.mark.asyncio
async def test_traceback_tree(test_folder_url, storage_backend, local_tmp_folder, source_package):
    """Test that the traceback function returns a tree structure with both repository and source paths."""

    # Create a test package structure
    # primary_source -> intermediate_source -> repo_root -> repo_mirror
    name = "test_package"
    version = "1.0"

    # Create the primary source package (no repository)
    primary_source = PackageInfo(name=name, version=version)
    primary_source_path = test_folder_url + "/primary_source"
    primary_source_file = wrapp_file_path(primary_source_path, primary_source.name)
    await write_package_info(primary_source_file, primary_source)

    # Create the root repository package
    await create(name, version, primary_source_path, False, repo=test_folder_url)
    repo_root = wrapp_file_in_repo(Repository(repo_url=test_folder_url), name, version)

    # Mirror repository package
    repo_mirror_url = test_folder_url + "/repo_mirror"
    await mirror(name, version, test_folder_url, repo_mirror_url)
    repo_mirror_file = wrapp_file_in_repo(Repository(repo_url=repo_mirror_url), name, version)
    can_read = await read_package_info(repo_mirror_file)

    # Test traceback from mirror repository
    trace_tree = await wrapp.commands.traceback(repo_mirror_file)

    # Verify the tree structure
    assert trace_tree.type == "Mirror"
    assert trace_tree.path == repo_mirror_file
    assert len(trace_tree.children) == 2

    # Check first child (repo_root)
    repo_root_node = trace_tree.children[0]
    assert repo_root_node.type == "Root"
    assert repo_root_node.path == repo_root
    assert len(repo_root_node.children) == 1

    # Check second child (source)
    source_node = repo_root_node.children[0]
    assert source_node.type == "Primary"
    assert source_node.path == primary_source_file
    assert len(source_node.children) == 0  # Primary source has no children

    # The root node also has a pointer to the source
    assert repo_root_node.children[0] == source_node


@pytest.mark.asyncio
async def test_traceback_circular_reference(test_folder_url, authenticator):
    """Test that the traceback function handles circular references correctly. Here, we have the same package in two locations,
    which reference each other as source."""

    # Create two packages that reference each other
    name = "package1"
    package1_path = test_folder_url + "/package1"
    package2_path = test_folder_url + "/package2"
    package1_file = wrapp_file_path(package1_path, name)
    package2_file = wrapp_file_path(package2_path, name)

    # Package 1 has Package 2 as source
    package1 = PackageInfo(name=name, version="1.0", source=package2_path, repository=test_folder_url)
    await write_package_info(package1_file, package1)

    # Package 2 references Package 1
    package2 = PackageInfo(name=name, version="1.0", source=package1_path, repository=test_folder_url)
    await write_package_info(package2_file, package1)

    # Test traceback from package1
    trace_tree = await wrapp.commands.traceback(package1_file)

    # Verify the tree structure handles the circular reference
    assert trace_tree.package_location_type == PackageLocationType.SOURCE
    assert trace_tree.path == package1_file

    # Check first child (package2)
    package2_node = trace_tree.source
    assert package2_node.package_location_type == PackageLocationType.SOURCE
    assert package2_node.path == package2_file
    assert package2_node.source
    assert package2_node.repository

    # Check circular reference detection
    circular_node = package2_node.source
    assert circular_node.package_location_type == PackageLocationType.ERROR
    assert circular_node.path == package2_file
    assert circular_node.source is None
    assert circular_node.repository is None


@pytest.mark.asyncio
async def test_traceback_multiple_paths(test_folder_url, authenticator):
    """Test that the traceback function follows both source and repository paths."""

    # Create a package with both source and repository fields
    source_path = test_folder_url + "/source"
    repo_path = test_folder_url + "/repo"

    # Create source package
    source_package = PackageInfo(name="test_package", version="1.0", repository=repo_path)
    source_file = await write_package_info(wrapp_file_path(source_path, source_package.name), source_package)

    # Create repo package
    repo_package = PackageInfo(name="test_package", version="1.0", source=source_path, repository=repo_path)
    repo = Repository(repo_url=repo_path)
    repo_file = wrapp_file_in_repo(repo, repo_package.name, "1.0")
    await write_package_info(repo_file, repo_package)

    # Create package with both source and repository
    dual_path = test_folder_url + "/dual"
    dual_package = PackageInfo(name="test_package", version="1.0", repository=repo_path, source=source_path)
    dual_file = await write_package_info(wrapp_file_path(dual_path, dual_package.name), dual_package)

    # Test traceback from dual package
    trace_tree = await wrapp.commands.traceback(dual_file)

    # Verify the tree structure follows both paths
    assert trace_tree.package_location_type == PackageLocationType.SOURCE
    assert trace_tree.path == dual_file
    assert trace_tree.source
    assert trace_tree.repository

    # Check that one child is from the repo and one is from the source
    repo_found = False
    source_found = False

    for child in [trace_tree.source, trace_tree.repository]:
        if child.path == repo_file:
            repo_found = True
            assert child.package_location_type == PackageLocationType.REPOSITORY
        elif child.path == source_file:
            source_found = True
            assert child.package_location_type == PackageLocationType.SOURCE

    assert repo_found and source_found, "Both repository and source paths should be followed"


@pytest.mark.asyncio
async def test_traceback_missing_in_repo(test_folder_url, storage_backend, local_tmp_folder, source_package):
    # Create a test package structure
    name = "test_package"
    version = "1.0"

    # Create the primary source package path in repo
    repo = Repository(repo_url=test_folder_url)
    primary_source_file = wrapp_file_in_repo(repo, name, version)

    # Test traceback non-existing file
    trace_tree = await wrapp.commands.traceback(primary_source_file)

    # Verify the tree structure
    assert trace_tree.package_location_type == PackageLocationType.ERROR
    assert trace_tree.path == primary_source_file


@pytest.mark.asyncio
async def test_traceback_missing(test_folder_url, storage_backend, local_tmp_folder, source_package):
    # Create a test package structure
    name = "test_package"

    # Create the primary source package path in repo
    folder = join_paths(test_folder_url, "test")
    primary_source_file = wrapp_file_path(folder, name)

    # Test traceback non-existing file
    trace_tree = await wrapp.commands.traceback(primary_source_file)

    # Verify the tree structure
    assert trace_tree.package_location_type == PackageLocationType.ERROR
    assert trace_tree.path == primary_source_file


@pytest.mark.asyncio
async def test_traceback_tree(cli_runner, test_folder_url, storage_backend, local_tmp_folder, source_package):
    """Test that the traceback function returns a tree structure with both repository and source paths."""

    # Create a test package structure
    # primary_source -> intermediate_source -> repo_root -> repo_mirror
    name = "test_package"
    version = "1.0"

    # Create the primary source package (no repository)
    primary_source = PackageInfo(name=name, version=version)
    primary_source_path = test_folder_url + "/primary_source"
    primary_source_file = wrapp_file_path(primary_source_path, primary_source.name)
    await write_package_info(primary_source_file, primary_source)

    # Create the intermediate source package (with repository pointing to repo_root)
    intermediate_source = PackageInfo(name=name, version=version, repository=test_folder_url, source=primary_source_path)
    intermediate_source_path = test_folder_url + "/intermediate_source"
    intermediate_source_file = await write_package_info(
        wrapp_file_path(intermediate_source_path, intermediate_source.name), intermediate_source
    )

    # Create the root repository package
    await create(name, version, primary_source_path, False, repo=test_folder_url)
    repo_root = wrapp_file_in_repo(Repository(repo_url=test_folder_url), name, version)

    # Mirror repository package
    repo_mirror_url = test_folder_url + "/repo_mirror"
    await mirror(name, version, test_folder_url, repo_mirror_url)
    repo_mirror_file = wrapp_file_in_repo(Repository(repo_url=repo_mirror_url), name, version)
    can_read = await read_package_info(repo_mirror_file)

    # Test traceback from mirror repository
    result = run_wrapp(cli_runner, ["traceback", repo_mirror_file], 0)

    # Verify some output is correct
    assert "Mirror" in result.stdout
    assert "Primary" in result.stdout
    assert primary_source_file in result.stdout
    assert repo_root in result.stdout
    assert repo_mirror_file in result.stdout
