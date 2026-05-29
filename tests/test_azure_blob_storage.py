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
Tests for the Azure Blob Storage implementation.

These tests verify that the Azure Blob Storage implementation works correctly.
They require valid Azure Blob Storage credentials to be set in environment variables:
- WRAPP_AZURE_CONNECTION_STRING or
- WRAPP_AZURE_ACCOUNT_NAME and (WRAPP_AZURE_ACCOUNT_KEY or WRAPP_AZURE_SAS_TOKEN)

The tests will be skipped if the credentials are not set.
"""

import os
import sys
import tempfile
import threading
import uuid
from unittest.mock import AsyncMock

import pytest


def path_to_file_url(path: str) -> str:
    """Convert a filesystem path to a file:// URL, handling Windows correctly."""
    abs_path = os.path.abspath(path)
    if sys.platform == "win32":
        return f"file:///{abs_path.replace(os.sep, '/')}"
    else:
        return f"file://{abs_path}"


from wrapp import __version__
from wrapp.datastructures.command_context import (
    CommandParameters,
    ThrottlingParameters,
)
from wrapp.utils.coro import (
    SINGLE_WRAPP_THREAD_NAME,
    sync_calls_async_on_wrapp_thread,
)
from wrapp.utils.router import g_azure_enabled
from wrapp.utils.storage import (
    Router,
    StorageType,
    wrapp_copy,
    wrapp_delete_item,
    wrapp_initialize,
    wrapp_list_directory,
    wrapp_read_file_content,
    wrapp_url_exists,
)
from wrapp.utils.storage_interface import (
    AzureBlobAuthInfo,
    Result,
    StorageAuthInfo,
)
from wrapp.utils.utils import StorageMissingDependenciesError

try:
    # isort: off
    from wrapp.utils.storage_impls.azure_blob_storage import (
        AzureBlobClientWrapper,
        AzureBlobStorage,
        WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY,
        WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY,
        _parse_positive_int_env,
        is_azure_url,
    )

    # Keep all direct imports from azure below the import of azure_blob_storage, to make sure we only need to capture
    # the exception StorageMissingDependenciesError, and not direct ImportError.
    from azure.core.exceptions import (
        ResourceExistsError,
        ResourceNotFoundError,
    )

    # isort: on
except StorageMissingDependenciesError:
    pytest.skip("Missing dependencies for azure_blob_storage", allow_module_level=True)


def test_parse_positive_int_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("WRAPP_AZURE_TEST_INT", "invalid")
    assert _parse_positive_int_env("WRAPP_AZURE_TEST_INT", 7) == 7


def test_parse_positive_int_env_non_positive_is_clamped(monkeypatch):
    monkeypatch.setenv("WRAPP_AZURE_TEST_INT", "0")
    assert _parse_positive_int_env("WRAPP_AZURE_TEST_INT", 7) == 1


@pytest.mark.asyncio
async def test_azure_client_wrapper_applies_transfer_client_kwargs(monkeypatch):
    import wrapp.utils.storage_impls.azure_blob_storage as azure_mod

    monkeypatch.setattr(azure_mod, "assert_is_wrapp_thread", lambda: None)
    monkeypatch.setattr(threading.current_thread(), "name", SINGLE_WRAPP_THREAD_NAME)
    monkeypatch.setattr(azure_mod, "WRAPP_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE", 111)
    monkeypatch.setattr(azure_mod, "WRAPP_AZURE_UPLOAD_MAX_BLOCK_SIZE", 222)
    monkeypatch.setattr(azure_mod, "WRAPP_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE", 333)
    monkeypatch.setattr(azure_mod, "WRAPP_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE", 444)
    monkeypatch.setattr(azure_mod, "AZURE_API_VERSION", "2024-11-04")

    captured = {}

    class _FakeBlobServiceClient:
        async def close(self):
            return None

    def _fake_from_connection_string(connection_string, **kwargs):
        captured["connection_string"] = connection_string
        captured["kwargs"] = kwargs
        return _FakeBlobServiceClient()

    monkeypatch.setattr(azure_mod.AsyncBlobServiceClient, "from_connection_string", _fake_from_connection_string)

    wrapper = azure_mod.AzureBlobClientWrapper(
        StorageAuthInfo(
            "https://account.blob.core.windows.net",
            AzureBlobAuthInfo(connection_string="UseDevelopmentStorage=true"),
        )
    )
    await wrapper.get_async_instance()

    assert captured["connection_string"] == "UseDevelopmentStorage=true"
    assert captured["kwargs"]["api_version"] == "2024-11-04"
    assert captured["kwargs"]["max_single_put_size"] == 111
    assert captured["kwargs"]["max_block_size"] == 222
    assert captured["kwargs"]["max_single_get_size"] == 333
    assert captured["kwargs"]["max_chunk_get_size"] == 444


@pytest.mark.asyncio
async def test_azure_transfer_concurrency_is_forwarded_to_upload_and_download():
    storage = AzureBlobStorage("0.0.0-test", False, ThrottlingParameters())

    fake_blob_client = AsyncMock()
    fake_blob_client.get_blob_properties = AsyncMock(side_effect=ResourceNotFoundError("not found"))
    fake_blob_client.upload_blob = AsyncMock(return_value=None)

    fake_download_stream = AsyncMock()
    fake_download_stream.readall = AsyncMock(return_value=b"content")
    fake_blob_client.download_blob = AsyncMock(return_value=fake_download_stream)

    class _FakeContainerClient:
        def get_blob_client(self, _blob_name):
            return fake_blob_client

    class _FakeAsyncClient:
        def get_container_client(self, _container_name):
            return _FakeContainerClient()

        def get_blob_client(self, *_args, **_kwargs):
            return fake_blob_client

    class _FakeWrapper:
        async def get_async_instance(self):
            return _FakeAsyncClient()

    fake_wrapper = _FakeWrapper()
    storage._azure_client = fake_wrapper

    result = await storage.write_file("azure://account/container/blob.txt", b"payload", "")
    assert result == Result.OK
    fake_blob_client.upload_blob.assert_awaited()
    assert fake_blob_client.upload_blob.await_args.kwargs["max_concurrency"] == WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY

    result, content = await storage.read_file_content("azure://account/container/blob.txt")
    assert result == Result.OK
    assert content == b"content"
    fake_blob_client.download_blob.assert_awaited()
    assert fake_blob_client.download_blob.await_args.kwargs["max_concurrency"] == WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY


def has_azure_credentials():
    """Check if Azure Blob Storage credentials are set in environment variables."""
    connection_string = os.environ.get("WRAPP_AZURE_CONNECTION_STRING")
    account_name = os.environ.get("WRAPP_AZURE_ACCOUNT_NAME")
    account_key = os.environ.get("WRAPP_AZURE_ACCOUNT_KEY")
    sas_token = os.environ.get("WRAPP_AZURE_SAS_TOKEN")

    return bool(connection_string) or bool(account_name and (account_key or sas_token))


@pytest.fixture
def test_container():
    """Set up the test environment."""
    # Initialize the WRAPP storage system
    if not Router.is_initialized():
        wrapp_initialize(__version__, debug_level=False)

    # Set up test variables
    account_name = os.environ.get("WRAPP_AZURE_ACCOUNT_NAME", "testaccount")
    container_name = f"test-container-{uuid.uuid4().hex}"  # Create unique ID to avoid conflicts
    container_url = f"azure://{account_name}/{container_name}"

    @sync_calls_async_on_wrapp_thread
    async def create():
        await _create_test_container(container_name)

    # Create a test container
    create()

    yield container_url

    # Delete the test container
    @sync_calls_async_on_wrapp_thread
    async def delete():
        await _delete_test_container(container_name)

    delete()


async def _create_test_container(container_name):
    """Create a test container in Azure Blob Storage."""
    # This is a helper method to create a container for testing
    # In a real test, we would use the Azure SDK directly to create the container
    # But for simplicity, we'll use the Router to get the Azure client
    if not hasattr(Router.me(), "azure"):
        pytest.skip("Azure Blob Storage disabled")
    try:
        # Get the Azure client
        azure_client = Router.me().azure().azure_client()
        instance = await azure_client.get_async_instance()

        # Create the container
        container_client = instance.get_container_client(container_name)

        # Use call_async to wait for the operation to complete
        # The create_container method returns container properties if successful
        result = await container_client.create_container()

        # Check if container was created successfully
        if result:
            print(f"Created test container: {container_name}")
        else:
            print(f"Failed to create test container: {container_name}")
            pytest.skip(f"Failed to create test container: {container_name}")

    except ResourceExistsError:
        # Container already exists, which is fine for our tests
        print(f"Test container already exists: {container_name}")
    except Exception as e:
        print(f"Error creating test container: {e}")
        pytest.fail(f"Error creating test container: {e}")


async def _delete_test_container(container_name):
    """Delete the test container from Azure Blob Storage."""
    try:
        # Get the Azure client
        azure_client = await Router.me().azure().azure_client().get_async_instance()

        # Delete the container
        container_client = azure_client.get_container_client(container_name)

        # Use call_async to wait for the operation to complete
        await container_client.delete_container()
    except ResourceNotFoundError:
        # Already gone – fine for idempotent teardown
        pass
    except Exception as e:
        print(f"Error deleting test container: {e}")


def test_is_azure_url():
    """Test the is_azure_url function."""
    # Valid Azure URLs
    assert is_azure_url("azure://account/container/blob")
    assert is_azure_url("https://account.blob.core.windows.net/container/blob")

    # Invalid Azure URLs
    assert not is_azure_url("s3://bucket/key")
    assert not is_azure_url("file:///path/to/file")
    assert not is_azure_url("http://example.com")


def test_get_storage():
    """Test that the Router returns the correct storage type for Azure URLs."""
    if not g_azure_enabled:
        pytest.skip("Azure has been disabled!")
    # Get the storage type for an Azure URL
    if not Router.is_initialized():
        Router.initialize("test_version", False)
    storage, storage_type = Router.me()._get_storage("azure://testaccount/test_container/blob")

    # Check that the storage type is Azure
    assert storage_type == StorageType.Azure

    # Check that the storage is an instance of AzureBlobStorage
    assert storage.__class__.__name__ == "AzureBlobStorage"


@pytest.mark.asyncio
async def test_upload_download_delete(test_container):
    """Test uploading, downloading, and deleting a file."""
    if not g_azure_enabled:
        pytest.skip("Azure has been disabled!")

    if not has_azure_credentials():
        pytest.skip("No Azure credentials provided in WRAPP_AZURE_CONNECTION_STRING etc")
    # Create a temporary file
    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
        temp_file.write(b"This is a test file for Azure Blob Storage.")
        temp_file_path = temp_file.name

    try:
        download_file_path = None

        # Create a file URL from the temporary file path
        local_file_url = path_to_file_url(temp_file_path)

        # Upload the file to Azure Blob Storage
        blob_url = f"{test_container}/test_blob.bl"
        result = await wrapp_copy(local_file_url, blob_url, overwrite=True)
        assert result == Result.OK

        # Check that the file exists
        exists = await wrapp_url_exists(blob_url)
        assert exists

        # Read the file content
        result, content = await wrapp_read_file_content(blob_url)
        assert result == Result.OK
        assert content == b"This is a test file for Azure Blob Storage."

        # Download the file to a new temporary file
        with tempfile.NamedTemporaryFile(delete=False) as download_file:
            download_file_path = download_file.name

        download_file_url = path_to_file_url(download_file_path)
        result = await wrapp_copy(blob_url, download_file_url, overwrite=True)
        assert result == Result.OK

        # Check that the downloaded file has the correct content
        with open(download_file_path, "rb") as f:
            downloaded_content = f.read()
        assert downloaded_content == b"This is a test file for Azure Blob Storage."

        # Delete the file from Azure Blob Storage
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
async def test_list_directory(test_container):
    """Test listing files in a container."""
    if not has_azure_credentials():
        pytest.skip("No Azure credentials provided in WRAPP_AZURE_CONNECTION_STRING etc")
    if not g_azure_enabled:
        pytest.skip("Azure has been disabled!")

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

            # Upload the file to Azure Blob Storage
            blob_url = f"{test_container}/{file_name}"
            result = await wrapp_copy(local_file_url, blob_url, overwrite=True)
            assert result == Result.OK

            # Clean up the temporary file
            os.remove(temp_file_path)

        # List files in the container
        files = []
        folders = []
        async for entry in wrapp_list_directory(test_container):
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
        async for entry in wrapp_list_directory(f"{test_container}/folder"):
            if not entry.is_folder:
                folder_files.append(entry.relative_path)

        # Check that we found the expected file in the folder
        assert "file3.txt" in folder_files
    finally:
        # Clean up test files
        context = CommandParameters()
        for file_name in test_files:
            blob_url = f"{test_container}/{file_name}"
            await wrapp_delete_item(context, blob_url)
