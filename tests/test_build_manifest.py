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

import importlib
import json
import os
from datetime import (
    datetime,
    timezone,
)
from typing import (
    Tuple,
)

import pytest

import wrapp
from wrapp.commands.build_manifest import (
    _manifest_entry_from_catalog_item,
    _resource_identity_for_manifest_item,
)
from wrapp.datastructures.catalog import CatalogItem
from wrapp.datastructures.package import (
    PackageInfo,
    wrapp_file_in_repo,
)
from wrapp.repo.repository import Repository
from wrapp.utils.utils import (
    FailedCommand,
)

from .common import (
    create_unnamed_package,
    generate_named_package,
    join_paths,
    run_wrapp,
)
from .storage_test_helpers import StorageBackendForTests

build_manifest_module = importlib.import_module("wrapp.commands.build_manifest")

# Note: Fixtures from wrapp_test_fixtures are available via conftest.py plugin registration
# Do not import them directly to avoid duplicate module loading


@pytest.fixture
def identity_backend(storage_backend_type):
    if storage_backend_type not in (StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE):
        pytest.skip("Storage-core manifests require Storage API identities")


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
            assert isinstance(item["resource_identity"], str) and item["resource_identity"]
        else:
            assert "resource_identity" not in item
            assert "resource_info" in item
            resource_info = item["resource_info"]
            assert isinstance(resource_info["resource_identity"], str) and resource_info["resource_identity"]
            assert isinstance(resource_info["size"], int)
            assert resource_info["modified_time"].endswith("Z")
            datetime.fromisoformat(resource_info["modified_time"].replace("Z", "+00:00"))


@pytest.mark.asyncio
async def test_manifest_preserves_catalog_resource_identity(monkeypatch):
    recorded_identity = "storage-identity://immutable/catalog-version"
    item = CatalogItem(
        relative_path="asset.usd",
        source_path="storage-address://mutable/asset.usd",
        resource_identity_v2=recorded_identity,
        hash="sha-256-flat;1048576;catalogued",
        size=10,
    )

    async def fail_if_reconstructed(*_args, **_kwargs):
        pytest.fail("A manifest must not reconstruct an existing catalog identity from source_path")

    monkeypatch.setattr(build_manifest_module, "wrapp_stat", fail_if_reconstructed)
    monkeypatch.setattr(build_manifest_module, "wrapp_resource_identity_for_manifest", lambda token, **kw: token)

    assert await _resource_identity_for_manifest_item(item) == recorded_identity


@pytest.mark.asyncio
async def test_manifest_does_not_treat_source_address_as_resource_identity(monkeypatch):
    item = CatalogItem(
        relative_path="asset.usd",
        source_path="storage-address://mutable/asset.usd",
    )

    async def unexpected_stat(*_args, **_kwargs):
        pytest.fail("Manifest generation must not stat a content address")

    monkeypatch.setattr(build_manifest_module, "wrapp_stat", unexpected_stat)

    with pytest.raises(FailedCommand, match="asset.usd.*resource_identity_v2"):
        await _resource_identity_for_manifest_item(item)


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
async def test_build_manifest_from_workspace_file(local_tmp_folder, test_folder_url, cli_runner, storage_backend, identity_backend):
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
async def test_build_manifest_api_returns_persistent_model(test_folder_url, storage_backend, identity_backend):
    """Test build_manifest returns the WRAPP persistent manifest model."""
    workspace_dir = join_paths(test_folder_url, "api_workspace")
    test_package = await generate_named_package(storage_backend, "api_package", "1.0.0", workspace_dir, prefix="api_", create_catalog=True)

    manifest_data = await wrapp.build_manifest(test_package.package_file)

    assert isinstance(manifest_data, wrapp.Manifest)
    expected = await wrapp.catalog(workspace_dir)
    compare_manifest_with_catalog(manifest_data, expected)


@pytest.mark.asyncio
async def test_build_manifest_minimal_format(local_tmp_folder, test_folder_url, cli_runner, storage_backend, identity_backend):
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
async def test_build_manifest_repository_file(local_tmp_folder, cli_runner, test_folder_url, storage_backend, identity_backend):
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
async def test_build_manifest_failure_preserves_output_even_with_overwrite(local_tmp_folder, cli_runner, wrapp_init):
    # Create a simple package info for testing
    test_wrapp_file = os.path.join(local_tmp_folder, "test_wrapp.wrapp")
    package_info = PackageInfo(name="test", version="1.0.0")
    await package_info.save(wrapp.CommandParameters(), test_wrapp_file)

    # Create an existing output file
    output_file = join_paths(local_tmp_folder, "manifest.json")
    with open(output_file, "w") as f:
        f.write("existing content")

    # Local files cannot provide durable identities; --overwrite must not truncate the output on failure.
    run_wrapp(cli_runner, ["build-manifest", test_wrapp_file, output_file, "--overwrite"], 1)

    with open(output_file, "r") as f:
        assert f.read() == "existing content"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["size", "modified_time"])
async def test_manifest_missing_recorded_metadata_requires_validated_rebuild(missing):
    item = CatalogItem(
        source_path="/repo/file.bin",
        relative_path="file.bin",
        size=42,
        modified_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        resource_identity_v2="storage-identity://recorded",
    )
    setattr(item, missing, None)
    with pytest.raises(FailedCommand, match=f"{missing}.*validated catalog rebuild"):
        await _manifest_entry_from_catalog_item(item, minimal_format=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("minimal", [False, True])
async def test_manifest_complete_recorded_metadata_still_requires_identity(minimal):
    item = CatalogItem(
        source_path="storage-address://repo/file.bin",
        relative_path="file.bin",
        size=42,
        modified_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(FailedCommand, match="file.bin.*resource_identity_v2"):
        await _manifest_entry_from_catalog_item(item, minimal_format=minimal)
