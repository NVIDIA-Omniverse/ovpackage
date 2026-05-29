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

import json
import os
from typing import (
    Tuple,
)

import pytest

import wrapp
from wrapp.datastructures.package import (
    PackageInfo,
    wrapp_file_in_repo,
)
from wrapp.repo.repository import Repository

from .common import (
    create_unnamed_package,
    generate_named_package,
    join_paths,
    run_wrapp,
)

# Note: Fixtures from wrapp_test_fixtures are available via conftest.py plugin registration
# Do not import them directly to avoid duplicate module loading


def compare_manifest_with_catalog(manifest_data, expected: wrapp.Catalog):
    assert isinstance(manifest_data, list)
    assert len(manifest_data) == len(expected.items)
    # Should have at least the files we created
    for item in expected.items:
        assert item.relative_path in [item["relative_path"] for item in manifest_data]

    # Check that each item has the required fields
    for item in manifest_data:
        assert "relative_path" in item
        assert "resource_identity" in item
        assert isinstance(item["relative_path"], str)
        assert isinstance(item["resource_identity"], str)


async def create_example_repo_structure(test_folder_url, storage_backend) -> Tuple[str, wrapp.Catalog, str]:
    """Create a test repository structure with a wrapp file and a subpackage."""
    repo = test_folder_url
    await create_unnamed_package(storage_backend, "p1", "1", test_folder_url + "/p1", repo_folder=repo)
    await create_unnamed_package(storage_backend, "p2", "1", test_folder_url + "/p2", prefix="p2_", repo_folder=repo)

    # Build working tree with a subpackage
    workspace_dir = test_folder_url + "/workspace"
    await wrapp.install("p1", "1", workspace_dir, repo=repo)
    await wrapp.install("p2", "1", workspace_dir + "/p2", repo=repo)

    # Catalog the result
    catalog_obj = await wrapp.catalog(root_dir=workspace_dir)

    # Create package in repo
    await wrapp.create("p1", "2", source=workspace_dir, catalog=False, repo=test_folder_url)

    repo = Repository(repo_url=test_folder_url)

    return wrapp_file_in_repo(repo, "p1", "2"), catalog_obj, test_folder_url


@pytest.mark.asyncio
async def test_build_manifest_from_workspace_file(local_tmp_folder, test_folder_url, cli_runner, storage_backend):
    """Test build-manifest with a workspace file (not in repository)."""
    # Create a wrapp file in the workspace
    workspace_dir = join_paths(test_folder_url, "workspace")
    test_package = await generate_named_package(
        storage_backend, "test_package", "1.0.0", workspace_dir, prefix="work_", create_catalog=True
    )

    # Create output file
    output_file = join_paths(local_tmp_folder, "manifest.json")

    # Run the command
    result = run_wrapp(cli_runner, ["build-manifest", test_package.package_file, output_file])
    assert result.exit_code == 0

    # Check that output file was created
    assert os.path.exists(output_file)

    # Check the manifest content
    with open(output_file, "r") as f:
        manifest_data = json.load(f)

    #  Find out what we expect in the manifest
    expected = await wrapp.catalog(workspace_dir)
    compare_manifest_with_catalog(manifest_data, expected)


@pytest.mark.asyncio
async def test_build_manifest_repository_file(local_tmp_folder, cli_runner, test_folder_url, storage_backend):
    """Test build-manifest with a repository file."""
    wrapp_file, package_catalog, _repo_root = await create_example_repo_structure(test_folder_url, storage_backend)

    # Create output file
    output_file = join_paths(local_tmp_folder, "manifest.json")

    # Run the command
    run_wrapp(cli_runner, ["build-manifest", wrapp_file, output_file, "--overwrite"])

    # Check that output file was created
    assert os.path.exists(output_file)

    # Check the manifest content
    with open(output_file, "r") as f:
        manifest_data = json.load(f)
    compare_manifest_with_catalog(manifest_data, package_catalog)


@pytest.mark.asyncio
async def test_build_manifest_nonexistent_file(local_tmp_folder, cli_runner):
    """Test build-manifest with a non-existent wrapp file."""
    nonexistent_file = join_paths(local_tmp_folder, "nonexistent.wrapp")
    output_file = join_paths(local_tmp_folder, "manifest.json")

    run_wrapp(cli_runner, ["build-manifest", nonexistent_file, output_file], 1)


@pytest.mark.asyncio
async def test_build_manifest_output_file_exists(local_tmp_folder, cli_runner, wrapp_init):
    # Create a simple package info for testing
    test_wrapp_file = os.path.join(local_tmp_folder, "test_wrapp.wrapp")
    package_info = PackageInfo(name="test", version="1.0.0")
    await package_info.save(wrapp.CommandParameters(), test_wrapp_file)

    # Create an existing output file
    output_file = os.path.join(local_tmp_folder, "manifest.json")
    with open(output_file, "w") as f:
        f.write("existing content")

    # Run without --overwrite should fail
    run_wrapp(cli_runner, ["build-manifest", test_wrapp_file, output_file], 1)


@pytest.mark.asyncio
async def test_build_manifest_output_file_exists_with_overwrite(local_tmp_folder, cli_runner, wrapp_init):
    # Create a simple package info for testing
    test_wrapp_file = os.path.join(local_tmp_folder, "test_wrapp.wrapp")
    package_info = PackageInfo(name="test", version="1.0.0")
    await package_info.save(wrapp.CommandParameters(), test_wrapp_file)

    # Create an existing output file
    output_file = join_paths(local_tmp_folder, "manifest.json")
    with open(output_file, "w") as f:
        f.write("existing content")

    # Run with --overwrite should succeed
    run_wrapp(cli_runner, ["build-manifest", test_wrapp_file, output_file, "--overwrite"])

    # Check that the file was overwritten
    with open(output_file, "r") as f:
        manifest_data = json.load(f)
    assert isinstance(manifest_data, list)


@pytest.mark.asyncio
@pytest.mark.skip("logging capture does not work reliably")
async def test_build_manifest_verbose_output(local_tmp_folder, cli_runner, caplog, wrapp_init):
    # Create a simple package info for testing
    test_wrapp_file = os.path.join(local_tmp_folder, "test_wrapp.wrapp")
    package_info = PackageInfo(name="test", version="1.0.0")
    await package_info.save(wrapp.CommandParameters(), test_wrapp_file)

    output_file = join_paths(local_tmp_folder, "manifest.json")

    # Run with --verbose
    run_wrapp(cli_runner, ["build-manifest", test_wrapp_file, output_file, "--verbose"])

    # Check that verbose output was produced
    assert any(
        [
            "wrapp file is in workspace" in log_record.message or "wrapp file is in repository" in log_record.message
            for log_record in caplog.records
        ]
    ), f"Did not find expected verbose output in log output: {caplog.records}"


@pytest.mark.asyncio
async def test_build_manifest_invalid_wrapp_file(local_tmp_folder, cli_runner):
    """Test build-manifest with an invalid wrapp file."""
    # Create a file that's not a valid wrapp file
    invalid_file = join_paths(local_tmp_folder, "invalid.wrapp")
    with open(invalid_file, "w") as f:
        f.write("not a valid wrapp file")

    output_file = join_paths(local_tmp_folder, "manifest.json")

    run_wrapp(cli_runner, ["build-manifest", invalid_file, output_file], 1)
