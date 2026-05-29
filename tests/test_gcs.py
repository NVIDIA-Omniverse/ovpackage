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

"""
Tests for the Google Cloud Storage implementation.

These tests verify that the Google Cloud Storage implementation works correctly.
They require a GCS backend to be available (either real GCS or fake-gcs-server).

Environment variables:
- WRAPP_GCS_ENDPOINT: Custom endpoint (e.g., http://localhost:4443 for fake-gcs-server)
- WRAPP_GCS_TEST_BUCKET: Test bucket name (default: wrapp-test-bucket)
- WRAPP_GCS_PROJECT: GCS project ID (optional)
- WRAPP_GCS_CREDENTIALS_FILE: Path to service account key file (optional)
"""
import os
import sys
import tempfile
import uuid

import pytest

from wrapp import __version__
from wrapp.datastructures.command_context import CommandParameters
from wrapp.utils.coro import sync_calls_async_on_wrapp_thread
from wrapp.utils.router import g_gcs_enabled
from wrapp.utils.storage import (
    Router,
    StorageType,
    wrapp_copy,
    wrapp_delete_item,
    wrapp_initialize,
    wrapp_list_directory,
    wrapp_read_file_content,
    wrapp_stat,
    wrapp_url_exists,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import StorageMissingDependenciesError


def path_to_file_url(path: str) -> str:
    """Convert a filesystem path to a file:// URL, handling Windows correctly."""
    abs_path = os.path.abspath(path)
    if sys.platform == "win32":
        return f"file:///{abs_path.replace(os.sep, '/')}"
    else:
        return f"file://{abs_path}"


try:
    # isort: off
    from wrapp.utils.storage_impls.gcs_storage import (
        is_gcs_url,
    )

    # isort: on
except StorageMissingDependenciesError:
    pytest.skip("Missing dependencies for gcs_storage", allow_module_level=True)


def test_is_gcs_url():
    """Test the is_gcs_url function."""
    # Valid GCS URLs
    assert is_gcs_url("gs://bucket/key")
    assert is_gcs_url("https://storage.googleapis.com/bucket/key")

    # Invalid GCS URLs
    assert not is_gcs_url("s3://bucket/key")
    assert not is_gcs_url("file:///path/to/file")
    assert not is_gcs_url("http://example.com")
    assert not is_gcs_url("azure://account/container/blob")


def test_get_storage():
    """Test that the Router returns the correct storage type for GCS URLs."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")
    # Get the storage type for a GCS URL
    if not Router.is_initialized():
        Router.initialize("test_version", False)
    storage, storage_type = Router.me()._get_storage("gs://test-bucket/blob")

    # Check that the storage type is GCS
    assert storage_type == StorageType.GCS

    # Check that the storage is an instance of GCSStorage
    assert storage.__class__.__name__ == "GCSStorage"


@pytest.fixture
def test_bucket():
    """Set up the test environment."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")

    # Initialize the WRAPP storage system
    if not Router.is_initialized():
        wrapp_initialize(__version__, debug_level=False)

    bucket_name = os.environ.get("WRAPP_GCS_TEST_BUCKET", "wrapp-test-bucket")
    # Use a unique sub-path to avoid conflicts
    test_prefix = f"test-{uuid.uuid4().hex}"
    bucket_url = f"gs://{bucket_name}/{test_prefix}"

    @sync_calls_async_on_wrapp_thread
    async def create():
        await Router.me().gcs().create_bucket_if_not_exists(bucket_name)

    # Create a test bucket
    create()

    yield bucket_url

    # Cleanup: delete test objects
    @sync_calls_async_on_wrapp_thread
    async def cleanup():
        from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode

        async with SchedulerTaskGroupNode() as node:
            await Router.me().gcs().delete_tree(bucket_url, node)

    try:
        cleanup()
    except Exception:
        pass  # Best-effort cleanup


@pytest.mark.asyncio
async def test_upload_download_delete(test_bucket):
    """Test uploading, downloading, and deleting a file."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")

    # Create a temporary file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file.write(b"This is a test file for Google Cloud Storage.")
        temp_file_path = temp_file.name

    try:
        download_file_path = None

        # Create a file URL from the temporary file path
        local_file_url = path_to_file_url(temp_file_path)

        # Upload the file to GCS
        blob_url = f"{test_bucket}/test_blob.txt"
        result = await wrapp_copy(local_file_url, blob_url, overwrite=True)
        assert result == Result.OK

        # Check that the file exists
        exists = await wrapp_url_exists(blob_url)
        assert exists

        # Read the file content
        result, content = await wrapp_read_file_content(blob_url)
        assert result == Result.OK
        assert content == b"This is a test file for Google Cloud Storage."

        # Download the file to a new temporary file
        with tempfile.NamedTemporaryFile(delete=False) as download_file:
            download_file_path = download_file.name

        download_file_url = path_to_file_url(download_file_path)
        result = await wrapp_copy(blob_url, download_file_url, overwrite=True)
        assert result == Result.OK

        # Check that the downloaded file has the correct content
        with open(download_file_path, "rb") as f:
            downloaded_content = f.read()
        assert downloaded_content == b"This is a test file for Google Cloud Storage."

        # Delete the file from GCS
        context = CommandParameters()
        result = await wrapp_delete_item(context, blob_url)
        assert result == Result.OK

        # Check that the file no longer exists
        exists = await wrapp_url_exists(blob_url)
        assert not exists
    finally:
        # Clean up local files
        for file_path in [temp_file_path, download_file_path]:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)


@pytest.mark.asyncio
async def test_list_directory(test_bucket):
    """Test listing files in a bucket prefix."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")

    # Create some test files
    test_files = ["file1.txt", "file2.txt", "folder/file3.txt"]

    try:
        # Upload test files
        for file_name in test_files:
            # Create a temporary file
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(f"Content of {file_name}".encode())
                temp_file_path = temp_file.name

            # Create a file URL from the temporary file path
            local_file_url = path_to_file_url(temp_file_path)

            # Upload the file to GCS
            blob_url = f"{test_bucket}/{file_name}"
            result = await wrapp_copy(local_file_url, blob_url, overwrite=True)
            assert result == Result.OK

            # Clean up the temporary file
            os.remove(temp_file_path)

        # List files in the bucket prefix
        files = []
        folders = []
        async for entry in wrapp_list_directory(test_bucket):
            if entry.is_folder:
                folders.append(entry.relative_path)
            else:
                files.append(entry.relative_path)

        # Check that we found the expected files and folders
        assert "file1.txt" in files
        assert "file2.txt" in files
        assert "folder" in folders

        # List files in the folder
        folder_files = []
        async for entry in wrapp_list_directory(f"{test_bucket}/folder"):
            if not entry.is_folder:
                folder_files.append(entry.relative_path)

        # Check that we found the expected file in the folder
        assert "file3.txt" in folder_files
    finally:
        # Clean up test files
        context = CommandParameters()
        for file_name in test_files:
            blob_url = f"{test_bucket}/{file_name}"
            await wrapp_delete_item(context, blob_url)


@pytest.mark.asyncio
async def test_stat_nonexistent_bucket():
    """stat on a bucket that does not exist returns ERROR_NOT_FOUND."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")

    if not Router.is_initialized():
        wrapp_initialize(__version__, debug_level=False)

    result, entry = await wrapp_stat(f"gs://wrapp-bucket-does-not-exist-{uuid.uuid4().hex}/file.txt")
    assert result == Result.ERROR_NOT_FOUND
    assert entry is None


@pytest.mark.asyncio
async def test_stat_nonexistent_file(test_bucket):
    """stat on a path that does not exist in a valid bucket returns ERROR_NOT_FOUND."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")

    result, entry = await wrapp_stat(f"{test_bucket}/no-such-file-{uuid.uuid4().hex}.txt")
    assert result == Result.ERROR_NOT_FOUND
    assert entry is None


@pytest.mark.asyncio
async def test_read_nonexistent_file(test_bucket):
    """read_file_content on a missing path returns ERROR_NOT_FOUND."""
    if not g_gcs_enabled:
        pytest.skip("GCS has been disabled!")

    result, content = await wrapp_read_file_content(f"{test_bucket}/no-such-file-{uuid.uuid4().hex}.txt")
    assert result == Result.ERROR_NOT_FOUND
    assert content == b""
