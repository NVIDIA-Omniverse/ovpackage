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
from datetime import (
    datetime,
    timezone,
)
from types import SimpleNamespace
from typing import (
    Tuple,
)

import pytest

import wrapp
from wrapp.commands.build_manifest import _validate_manifest_stat
from wrapp.datastructures.catalog import CatalogItem
from wrapp.datastructures.package import (
    PackageInfo,
    wrapp_file_in_repo,
)
from wrapp.repo.repository import Repository
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
)

from .common import (
    create_unnamed_package,
    generate_named_package,
    join_paths,
    run_wrapp,
)

# Note: Fixtures from wrapp_test_fixtures are available via conftest.py plugin registration
# Do not import them directly to avoid duplicate module loading


def _manifest_files(manifest_data):
    if isinstance(manifest_data, wrapp.Manifest):
        manifest_data = manifest_data.model_dump(mode="json")
    assert isinstance(manifest_data, dict)
    assert manifest_data["format_version"] == {"version_string": "1.0"}
    assert manifest_data["storage_api_version"] == "v1alpha"
    assert isinstance(manifest_data["files"], list)
    return manifest_data["files"]


def compare_manifest_with_catalog(manifest_data, expected: wrapp.Catalog, *, minimal_format: bool = False):
    files = _manifest_files(manifest_data)
    assert len(files) == len(expected.items)
    # Should have at least the files we created
    relative_paths = {entry["relative_path"] for entry in files}
    for item in expected.items:
        assert item.relative_path in relative_paths

    # Check that each item has the required fields
    for item in files:
        assert "relative_path" in item
        assert isinstance(item["relative_path"], str)
        if minimal_format:
            assert "resource_identity" in item
            assert "resource_info" not in item
            assert isinstance(item["resource_identity"], str)
        else:
            assert "resource_identity" not in item
            assert "resource_info" in item
            resource_info = item["resource_info"]
            assert isinstance(resource_info["resource_identity"], str)
            assert isinstance(resource_info["size"], int)
            assert resource_info["modified_time"].endswith("Z")
            datetime.fromisoformat(resource_info["modified_time"].replace("Z", "+00:00"))


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
async def test_build_manifest_api_returns_persistent_model(test_folder_url, storage_backend):
    """Test build_manifest returns the WRAPP persistent manifest model."""
    workspace_dir = join_paths(test_folder_url, "api_workspace")
    test_package = await generate_named_package(storage_backend, "api_package", "1.0.0", workspace_dir, prefix="api_", create_catalog=True)

    manifest_data = await wrapp.build_manifest(test_package.package_file)

    assert isinstance(manifest_data, wrapp.Manifest)
    expected = await wrapp.catalog(workspace_dir)
    compare_manifest_with_catalog(manifest_data, expected)


@pytest.mark.asyncio
async def test_build_manifest_minimal_format(local_tmp_folder, test_folder_url, cli_runner, storage_backend):
    """Test build-manifest --minimal-format keeps the top-level manifest object."""
    workspace_dir = join_paths(test_folder_url, "minimal_workspace")
    test_package = await generate_named_package(
        storage_backend, "minimal_package", "1.0.0", workspace_dir, prefix="minimal_", create_catalog=True
    )
    output_file = join_paths(local_tmp_folder, "minimal_manifest.json")

    result = run_wrapp(cli_runner, ["build-manifest", test_package.package_file, output_file, "--minimal-format"])
    assert result.exit_code == 0

    with open(output_file, "r") as f:
        manifest_data = json.load(f)

    expected = await wrapp.catalog(workspace_dir)
    compare_manifest_with_catalog(manifest_data, expected, minimal_format=True)


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
    assert isinstance(manifest_data, dict)
    assert isinstance(manifest_data["files"], list)


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


def _manifest_catalog_item() -> CatalogItem:
    """A minimal CatalogItem whose source_path appears in the error messages."""
    return CatalogItem(source_path="/repo/file.bin", relative_path="file.bin")


def _good_stat_item() -> SimpleNamespace:
    """A well-formed stat item mirroring the ListEntry attributes used by _validate_manifest_stat."""
    return SimpleNamespace(
        is_regular_file=True,
        size=42,
        modified_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def test_validate_manifest_stat_none_raises_storage_operation_error():
    """stat_item is None -> StorageOperationError."""
    item = _manifest_catalog_item()
    with pytest.raises(StorageOperationError, match=f"Failed to stat manifest item {item.source_path}"):
        _validate_manifest_stat(item, None)


def test_validate_manifest_stat_not_regular_file_raises_failed_command():
    """stat_item.is_regular_file is False -> FailedCommand."""
    item = _manifest_catalog_item()
    stat_item = _good_stat_item()
    stat_item.is_regular_file = False
    with pytest.raises(FailedCommand, match=f"Manifest item {item.source_path} is not a regular file"):
        _validate_manifest_stat(item, stat_item)


def test_validate_manifest_stat_missing_size_raises_failed_command():
    """stat_item.size is None -> FailedCommand."""
    item = _manifest_catalog_item()
    stat_item = _good_stat_item()
    stat_item.size = None
    with pytest.raises(FailedCommand, match=f"Manifest item {item.source_path} has no size metadata"):
        _validate_manifest_stat(item, stat_item)


def test_validate_manifest_stat_missing_modified_time_raises_failed_command():
    """stat_item.modified_time is None -> FailedCommand."""
    item = _manifest_catalog_item()
    stat_item = _good_stat_item()
    stat_item.modified_time = None
    with pytest.raises(FailedCommand, match=f"Manifest item {item.source_path} has no modified_time metadata"):
        _validate_manifest_stat(item, stat_item)


def test_validate_manifest_stat_well_formed_does_not_raise():
    """A well-formed stat item does not raise."""
    _validate_manifest_stat(_manifest_catalog_item(), _good_stat_item())
