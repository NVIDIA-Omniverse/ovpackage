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

import asyncio
import io
import logging
import os
import re
import traceback
from asyncio import AbstractEventLoop
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
)
from urllib.parse import urlparse

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.coro import assert_is_wrapp_thread
from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage_interface import (
    AzureBlobAuthInfo,
    ConnectionStatus,
    DirectorySemantics,
    ListEntry,
    LocalReadOnlyFile,
    ResourceIdentity,
    Result,
    ServerInfo,
    StorageAuthInfo,
    StorageInterface,
)
from wrapp.utils.task_throttler import (
    TaskThrottler,
    ThrottlerType,
)
from wrapp.utils.utils import (
    StorageMissingDependenciesError,
    StorageOperationError,
    get_path_from_url,
)

logger = logging.getLogger("omni.wrapp")


def _parse_positive_int_env(env_var: str, default: int) -> int:
    raw = os.getenv(env_var, str(default))
    try:
        parsed = int(raw)
    except (ValueError, TypeError):
        parsed = default
        logger.warning("%s=%r is not a valid integer, falling back to default %d", env_var, raw, default)
    if parsed < 1:
        logger.warning("%s=%d is less than 1, clamping to 1", env_var, parsed)
    return max(1, parsed)


# Try to import required dependencies
try:
    from azure.core.exceptions import (
        AzureError,
        ClientAuthenticationError,
        ResourceNotFoundError,
    )
    from azure.storage.blob import (
        BlobProperties,
    )
    from azure.storage.blob.aio import BlobServiceClient as AsyncBlobServiceClient
except ImportError as e:
    # The list of dependencies should be the same as in pyproject.toml
    missing = getattr(e, "name", str(e))
    error_msg = f"failed to import {missing}. Required dependencies: azure-storage-blob. Install with 'pip install ovpackage[azure]'. Exception: {e}"
    raise StorageMissingDependenciesError(error_msg) from e


# Constants
NUCLEUS_HASH_METADATA_KEY = "nv_omniverse_nucleus_hash"
WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY = _parse_positive_int_env("WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY", 1)
WRAPP_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE = _parse_positive_int_env("WRAPP_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE", 64 * 1024 * 1024)
WRAPP_AZURE_UPLOAD_MAX_BLOCK_SIZE = _parse_positive_int_env("WRAPP_AZURE_UPLOAD_MAX_BLOCK_SIZE", 4 * 1024 * 1024)
WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY = _parse_positive_int_env("WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY", 1)
WRAPP_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE = _parse_positive_int_env("WRAPP_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE", 32 * 1024 * 1024)
WRAPP_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE = _parse_positive_int_env("WRAPP_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE", 4 * 1024 * 1024)

# Azure Storage API version to use. You can use the environment variable
# to pin to a specific version to ensure compatibility with local emulators like Azurite
# which might not support the latest SDK's API version.
AZURE_API_VERSION: str | None = os.getenv("WRAPP_AZURE_API_VERSION", None)


# Azure Blob Storage metadata restrictions:
# - Metadata keys must adhere to C# identifier naming rules
# - Keys and values can only contain ASCII characters
# - Non-alphanumeric characters are not allowed except for specific ones
def sanitize_azure_metadata(metadata: Dict[str, str]) -> Dict[str, str]:
    """Sanitize metadata for Azure Blob Storage.

    Azure Blob Storage has specific restrictions on metadata keys and values:
    - Keys must follow C# identifier naming rules
    - Keys and values must contain only ASCII characters
    - Non-alphanumeric characters are generally not allowed

    This function sanitizes metadata to ensure it meets these requirements.

    Args:
        metadata: Dictionary of metadata key-value pairs

    Returns:
        Sanitized metadata dictionary
    """
    sanitized_metadata = {}

    for key, value in metadata.items():
        # Ensure key is a valid C# identifier (alphanumeric and underscore)
        sanitized_key = re.sub(r"[^a-zA-Z0-9_]", "_", key)

        # Ensure value contains only valid characters
        # Azure allows only ASCII characters in metadata values
        if value is not None:
            # Convert to string if not already
            str_value = str(value)
            # Replace non-ASCII characters with underscores
            sanitized_value = "".join(c if ord(c) < 128 else "_" for c in str_value)
        else:
            sanitized_value = ""

        sanitized_metadata[sanitized_key] = sanitized_value

    return sanitized_metadata


# Extend StorageAuthInfo with Azure Blob Storage support
def azure_blob(cls, server_url: str, connection_string: str) -> "StorageAuthInfo":
    """Convenience function to set up StorageAuthInfo for accessing Azure Blob Storage."""
    return StorageAuthInfo(server_url, AzureBlobAuthInfo(connection_string=connection_string))


# Add the azure_blob method to StorageAuthInfo
setattr(StorageAuthInfo, "azure_blob", classmethod(azure_blob))


def parse_azure_url(url: str) -> Tuple[str, str, str]:
    """Parse an Azure Blob Storage URL into account name, container name, and blob name.

    Args:
        url: Azure Blob Storage URL

    Returns:
        Tuple of (account_name, container_name, blob_name)

    Raises:
        ValueError: If the URL is not a valid Azure Blob Storage URL
    """
    parsed = urlparse(url)

    # Handle different URL formats
    if parsed.scheme == "azure":
        # azure://account/container/blob
        path_parts = parsed.path.strip("/").split("/", 1)
        account_name = parsed.netloc
        container_name = (path_parts[0] if path_parts else "").lower()
        blob_name = path_parts[1] if len(path_parts) > 1 else ""
        if url.endswith("/") and blob_name != "":
            blob_name = blob_name + "/"
        return account_name, container_name, blob_name

    elif parsed.scheme == "https" and "blob.core.windows.net" in parsed.netloc:
        # https://account.blob.core.windows.net/container/blob
        account_name = parsed.netloc.split(".")[0]
        path_parts = parsed.path.strip("/").split("/", 1)
        container_name = (path_parts[0] if path_parts else "").lower()
        blob_name = path_parts[1] if len(path_parts) > 1 else ""
        if url.endswith("/") and blob_name != "":
            blob_name = blob_name + "/"
        return account_name, container_name, blob_name

    raise ValueError(f"Not an Azure Blob Storage URL: {url}")


def is_azure_url(url: str) -> bool:
    """Check if a URL is an Azure Blob Storage URL.

    Args:
        url: URL to check

    Returns:
        True if the URL is an Azure Blob Storage URL, False otherwise
    """
    try:
        account_name, container_name, blob_name = parse_azure_url(url)
        return True
    except ValueError:
        return False


class AzureBlobListEntry(ListEntry):
    """Implementation of ListEntry for Azure Blob Storage."""

    @classmethod
    def file_from_blob_properties(cls, blob_name: str, props: BlobProperties, parent_path: str = "") -> "AzureBlobListEntry":
        """Create a ListEntry from a BlobProperties object.

        Args:
            blob_name: Name of the blob
            props: BlobProperties object
            parent_path: Parent path for relative path calculation

        Returns:
            AzureBlobListEntry instance
        """
        size = props.size
        last_modified = props.last_modified
        etag = props.etag.strip('"') if props.etag else ""

        # Extract nucleus hash from metadata if available
        nucleus_hash = None
        if props.metadata and NUCLEUS_HASH_METADATA_KEY in props.metadata:
            nucleus_hash = props.metadata[NUCLEUS_HASH_METADATA_KEY]

        return AzureBlobListEntry(
            blob_name,
            etag=etag,
            is_folder=False,
            parent_path=parent_path,
            size=size,
            last_modified=last_modified,
            nucleus_hash=nucleus_hash,
        )

    @classmethod
    def folder(cls, folder_name: str, parent_path: str = "") -> "AzureBlobListEntry":
        """Create a folder ListEntry.

        Args:
            folder_name: Name of the folder
            parent_path: Parent path for relative path calculation

        Returns:
            AzureBlobListEntry instance representing a folder
        """
        # Ensure folder name ends with "/"
        if not folder_name.endswith("/") and folder_name != "":
            folder_name += "/"

        return AzureBlobListEntry(folder_name.rstrip("/"), etag="", is_folder=True, parent_path=parent_path)

    def __init__(
        self,
        blob_name: str,
        *,
        etag: str,
        is_folder: bool,
        parent_path: str = "",
        size: Optional[int] = None,
        last_modified: Optional[datetime] = None,
        nucleus_hash: Optional[str] = None,
    ) -> None:
        """Initialize an Azure Blob Storage ListEntry.

        Args:
            blob_name: Name of the blob
            etag: ETag of the blob
            is_folder: Whether this entry represents a folder
            parent_path: Parent path for relative path calculation
            size: Size of the blob in bytes
            last_modified: Last modified time of the blob
            nucleus_hash: Nucleus hash of the blob
        """
        if parent_path and not parent_path.endswith("/"):
            parent_path += "/"

        assert isinstance(blob_name, str)
        assert blob_name.startswith(parent_path)

        assert size is None or (isinstance(size, int) and size >= 0)
        assert last_modified is None or isinstance(last_modified, datetime)
        assert nucleus_hash is None or (isinstance(nucleus_hash, str) and nucleus_hash != "")

        self._etag = etag
        self._is_folder = is_folder
        self._relative_path = blob_name[len(parent_path) :]
        self._size = size
        self._last_modified = last_modified
        self._nucleus_hash = nucleus_hash

    @property
    def is_folder(self) -> bool:
        return self._is_folder

    @property
    def is_regular_file(self) -> bool:
        return self._relative_path != "" and not self.is_folder

    @property
    def has_checkpoint(self) -> bool:
        return False

    @property
    def version(self) -> str:
        return "1"

    @property
    def relative_path(self) -> str:
        return self._relative_path

    @property
    def nucleus_hash(self) -> Optional[str]:
        return self._nucleus_hash

    @nucleus_hash.setter
    def nucleus_hash(self, value: str):
        if self.nucleus_hash is not None:
            raise AttributeError("Nucleus hash cannot be changed once set")
        self._nucleus_hash = value

    @property
    def size(self) -> Optional[int]:
        return self._size

    @property
    def modified_time(self) -> Optional[datetime]:
        return self._last_modified


class AzureBlobServerInfo(ServerInfo):
    """Implementation of ServerInfo for Azure Blob Storage."""

    def supports_create_with_hash(self, target_url: str) -> bool:
        return False

    @property
    def auth_token(self) -> str:
        return "NOT_IMPLEMENTED_AUTH_TOKEN"


class AzureBlobClientWrapper:
    """Wrapper for Azure Blob Storage client."""

    def __init__(self, auth_info: Optional[StorageAuthInfo] = None):
        """Initialize Azure Blob Storage client wrapper.

        Args:
            auth_info: Authentication information for Azure Blob Storage
        """
        self._async_instance = None
        self._loop: Optional[AbstractEventLoop] = None
        self._auth_data: Optional[AzureBlobAuthInfo] = None
        self._auth_is_dirty = False
        self.set_new_auth_info(auth_info)

    def set_new_auth_info(self, auth_info: Optional[StorageAuthInfo]):
        auth_data: Optional[AzureBlobAuthInfo] = None
        if not auth_info:
            # Check for environment variables
            connection_string = os.environ.get("WRAPP_AZURE_CONNECTION_STRING", None)
            account_name = os.environ.get("WRAPP_AZURE_ACCOUNT_NAME", None)
            account_key = os.environ.get("WRAPP_AZURE_ACCOUNT_KEY", None)
            sas_token = os.environ.get("WRAPP_AZURE_SAS_TOKEN", None)

            if connection_string:
                auth_data = AzureBlobAuthInfo(connection_string=connection_string)
                logger.info(f"Connecting to Azure Blob Storage with connection string from environment variables")
            elif account_name and (account_key or sas_token):
                auth_data = AzureBlobAuthInfo(account_name=account_name, account_key=account_key, sas_token=sas_token)
                logger.info(f"Connecting to Azure Blob Storage with account credentials from environment variables")
            else:
                logger.info(f"No Azure Blob Storage credentials found in environment variables")
        else:
            assert isinstance(auth_info.auth_data, AzureBlobAuthInfo)
            auth_data = auth_info.auth_data
            logger.info(f"Connecting to Azure Blob Storage with credentials provided by the caller")

        self._auth_data = auth_data
        self._auth_is_dirty = True

    async def get_async_instance(self) -> AsyncBlobServiceClient:
        """Get or create an AsyncBlobServiceClient instance.

        Returns:
            AsyncBlobServiceClient instance
        """
        assert_is_wrapp_thread()
        if self._async_instance:
            # Check if we're in the same event loop as when we created the client the last time
            current_loop = asyncio.get_running_loop()
            # If we have an instance, but it's from a different loop, close it and create a new one
            # Also, if we auth information has changed, we need to create a new instance
            if self._loop is not current_loop or self._auth_is_dirty:
                await self.close_instance()

        if self._async_instance is None and self._auth_data:
            try:
                # Store the current event loop to ensure we're using the same one
                self._loop = asyncio.get_running_loop()
                client_kwargs = {
                    "api_version": AZURE_API_VERSION,
                    "max_single_put_size": WRAPP_AZURE_UPLOAD_MAX_SINGLE_PUT_SIZE,
                    "max_block_size": WRAPP_AZURE_UPLOAD_MAX_BLOCK_SIZE,
                    "max_single_get_size": WRAPP_AZURE_DOWNLOAD_MAX_SINGLE_GET_SIZE,
                    "max_chunk_get_size": WRAPP_AZURE_DOWNLOAD_MAX_CHUNK_GET_SIZE,
                }
                if self._auth_data.connection_string:
                    self._async_instance = AsyncBlobServiceClient.from_connection_string(self._auth_data.connection_string, **client_kwargs)
                elif self._auth_data.account_name:
                    if self._auth_data.account_key:
                        # Connect with account key
                        account_url = f"https://{self._auth_data.account_name}.blob.core.windows.net"
                        self._async_instance = AsyncBlobServiceClient(
                            account_url=account_url, credential=self._auth_data.account_key, **client_kwargs
                        )
                    elif self._auth_data.sas_token:
                        # Connect with SAS token
                        account_url = f"https://{self._auth_data.account_name}.blob.core.windows.net{self._auth_data.sas_token}"
                        self._async_instance = AsyncBlobServiceClient(account_url=account_url, **client_kwargs)
                self._auth_is_dirty = False
                logger.debug(
                    f"Created instance {id(self._async_instance)} with API version {AZURE_API_VERSION if AZURE_API_VERSION else 'default'}"
                )
            except Exception as e:
                # This needs to be a StorageOperationError because AzureBlobStorage does lazy initialization
                raise StorageOperationError(f"Failed to initialize Azure Blob Storage async client: {str(e)}.\n{traceback.format_exc()}")

        if not self._async_instance:
            raise StorageOperationError("Failed to initialize Azure Blob Storage async client - no authentication information?")
        return self._async_instance

    async def close_instance(self):
        assert_is_wrapp_thread()
        """Close the client instances."""
        if self._async_instance is not None:
            instance_to_shutdown = self._async_instance
            self._async_instance = None
            loop = asyncio.get_running_loop()
            if self._loop == loop:
                await loop.shutdown_asyncgens()
            logger.debug(f"Shutting down instance {id(instance_to_shutdown)}")
            await instance_to_shutdown.close()


class AzureBlobStorage(StorageInterface):
    """Implementation of StorageInterface for Azure Blob Storage."""

    @classmethod
    def version_info(cls) -> str:
        return f"Azure Blob Storage Backend"

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_params: ThrottlingParameters):
        """Initialize Azure Blob Storage interface.

        Args:
            wrapp_version: WRAPP version
            debug_level: Debug level flag
            throttling_params: Scheduling parameters for throttling control
        """
        self._azure_client: Optional[AzureBlobClientWrapper] = None
        self._auth_info: Optional[StorageAuthInfo] = None
        self._last_connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
        # Use scheduling_params for throttler initialization
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "Azure", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "Azure", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "Azure", throttling_params.file_transfer_jobs)

    def backend_throttlers(self) -> list[TaskThrottler]:
        return [self._throttler_roundtrip, self._throttler_networkdata, self._throttler_serverdata]

    def reconfigure_throttlers(self, throttling_params: ThrottlingParameters) -> bool:
        if (
            self._throttler_roundtrip.max_parallel_tasks == throttling_params.jobs
            and self._throttler_networkdata.max_parallel_tasks == throttling_params.file_transfer_jobs
            and self._throttler_serverdata.max_parallel_tasks == throttling_params.jobs
        ):
            return True
        for t in self.backend_throttlers():
            if t.pending_tasks or t.processing_tasks:
                logger.warning(f"Cannot update throttling parameters for Azure: Throttler {t.server_backend_name} has open tasks.")
                return False
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "Azure", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "Azure", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "Azure", throttling_params.jobs)
        return True

    def azure_client(self) -> AzureBlobClientWrapper:
        """Get or create an Azure Blob Storage client wrapper.

        Returns:
            AzureBlobClientWrapper instance
        """
        # Make this a lazy creation, so the user has time to set up the authentication credentials
        if not self._azure_client:
            self._azure_client = AzureBlobClientWrapper(self._auth_info)
        return self._azure_client

    def supports_tags(self, url: str) -> bool:
        """Check if tags are supported.

        Args:
            url: URL to check

        Returns:
            True if tags are supported, False otherwise
        """
        return False  # Not implemented yet, though Azure could do tags

    def directory_semantics(self) -> DirectorySemantics:
        """Get directory semantics.

        Returns:
            DirectorySemantics.PREFIX for Azure Blob Storage
        """
        return DirectorySemantics.PREFIX  # Azure Blob Storage uses a flat namespace with virtual directories

    def supports_comments(self) -> bool:
        """Check if comments are supported.

        Returns:
            True if comments are supported, False otherwise
        """
        return True  # Azure Blob Storage supports metadata which can be used for comments

    async def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        """Shutdown the client.

        Args:
            exc_type: Exception type
            exc_val: Exception value
            exc_tb: Exception traceback
        """
        if self._azure_client:
            await self._azure_client.close_instance()

    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        """Get server information.

        Args:
            url: URL to get server information for

        Returns:
            Tuple of (Result, ServerInfo)
        """
        return Result.OK, AzureBlobServerInfo()

    async def normalize(self, url: str) -> str:
        return url

    async def _list_1_if_dir(self, blob_prefix: str, container_client) -> list:
        blobs = []
        stat_iterator = container_client.list_blobs(name_starts_with=blob_prefix, include=["metadata"])
        async for blob in stat_iterator:
            blobs.append(blob)
            break
        return blobs

    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        """Get information about a blob or virtual directory.

        Args:
            url: URL to get information for

        Returns:
            Tuple of (Result, ListEntry or None)
        """
        try:
            account_name, container_name, blob_name = parse_azure_url(url)

            # Get container client
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Check if this is a directory-like path
            is_directory_path = blob_name.endswith("/") or blob_name == ""

            if is_directory_path:
                # For directory-like paths, check if there are any blobs with this prefix
                blobs = await self._throttler_roundtrip.run(self._list_1_if_dir(blob_name, container_client))
                if blobs:
                    # There are blobs with this prefix, so it's a valid directory
                    return Result.OK, AzureBlobListEntry.folder(blob_name)
                else:
                    # No blobs with this prefix, check if the container exists
                    try:
                        await self._throttler_roundtrip.run(container_client.get_container_properties())
                        if blob_name == "":
                            # Empty blob name means we're checking the container itself
                            return Result.OK, AzureBlobListEntry.folder("")
                        else:
                            # No blobs with this prefix, so it's not a valid directory
                            return Result.ERROR_NOT_FOUND, None
                    except ResourceNotFoundError:
                        # Container doesn't exist
                        return Result.ERROR_NOT_FOUND, None
            else:
                # For file-like paths, try to get the blob properties
                blob_client = container_client.get_blob_client(blob_name)
                try:
                    props = await self._throttler_roundtrip.run(blob_client.get_blob_properties())
                    return Result.OK, AzureBlobListEntry.file_from_blob_properties(blob_name, props)
                except ResourceNotFoundError:
                    # Check if there's a directory with this name
                    directory_path = blob_name + "/"
                    blobs = await self._throttler_roundtrip.run(self._list_1_if_dir(directory_path, container_client))
                    if blobs:
                        # There are blobs with this directory prefix
                        return Result.OK, AzureBlobListEntry.folder(directory_path)
                    else:
                        # Neither a blob nor a directory exists with this name
                        return Result.ERROR_NOT_FOUND, None

        except ClientAuthenticationError as e:
            logger.error(f"Azure stat {url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED, None
        except ResourceNotFoundError as e:
            return Result.ERROR_NOT_FOUND, None
        except AzureError as e:
            logger.error(f"Azure error during stat {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR, None

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, ResourceIdentity]:
        raise NotImplementedError("Resource identities not supported for S3 yet")

    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        """Check if a URL represents a folder.

        Args:
            url: URL to check

        Returns:
            Tuple of (Result, bool)
        """
        result, item = await self.stat(url)
        return result, item.is_folder if item else False

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        """List the contents of a directory.

        Args:
            url: URL to list

        Yields:
            ListEntry objects

        Raises:
            StorageOperationError: If an error occurs
        """
        try:
            account_name, container_name, blob_name = parse_azure_url(url)

            # Ensure blob_name ends with "/" if it's not empty
            if blob_name and not blob_name.endswith("/"):
                blob_name += "/"

            # Get container client
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Check if container exists
            try:
                await self._throttler_roundtrip.run(container_client.get_container_properties())
            except ResourceNotFoundError:
                raise StorageOperationError(f"Container {container_name} not found", Result.ERROR_NOT_FOUND)

            # List blobs with the given prefix
            empty_list = True

            # Use walk_blobs instead of list_blobs to properly handle the delimiter parameter
            # walk_blobs returns both blobs and prefixes directly in the iterator
            async for item in self._throttler_roundtrip.iterate(container_client.walk_blobs(name_starts_with=blob_name, delimiter="/")):
                # Process both blobs and prefixes from the walk_blobs result
                # Check if this is a blob or a prefix (virtual directory)
                if hasattr(item, "is_prefix") and item.is_prefix:
                    # This is a prefix (virtual directory)
                    empty_list = False
                    yield AzureBlobListEntry.folder(item.name, blob_name)
                elif hasattr(item, "name") and not isinstance(item, type("BlobPrefix", (), {})):
                    # This is a blob (not a prefix)
                    if item.name != blob_name:  # Skip the directory marker itself
                        empty_list = False
                        # Make sure the item has all the required properties before using file_from_blob_properties
                        if hasattr(item, "size") and hasattr(item, "last_modified") and hasattr(item, "etag"):
                            yield AzureBlobListEntry.file_from_blob_properties(item.name, item, blob_name)
                        else:
                            # If it doesn't have all required properties, create a basic folder entry
                            yield AzureBlobListEntry.folder(item.name, blob_name)

            if empty_list:
                # If there is a file in the same place, return ERROR_WRONG_TYPE
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    raise StorageOperationError(f"Trying to list a file {url}", Result.ERROR_WRONG_TYPE)

        except ClientAuthenticationError as e:
            logger.error(f"Azure list_directory {url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            raise StorageOperationError(f"Authentication error: {e}", Result.ERROR_ACCESS_DENIED)
        except ResourceNotFoundError as e:
            raise StorageOperationError(f"Resource not found: {e}", Result.ERROR_NOT_FOUND)
        except AzureError as e:
            logger.error(f"Azure error during list_directory {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            raise StorageOperationError(f"Azure error: {e}", Result.ERROR)

    def supports_list_file_tree(self) -> bool:
        """Check if list_file_tree is supported.

        Returns:
            True if list_file_tree is supported, False otherwise
        """
        return True

    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        """List all blobs under a prefix recursively.

        Args:
            url: URL to list

        Yields:
            ListEntry objects

        Raises:
            StorageOperationError: If an error occurs
        """
        try:
            account_name, container_name, blob_name = parse_azure_url(url)

            # Ensure blob_name ends with "/" if it's not empty
            if blob_name and not blob_name.endswith("/"):
                blob_name += "/"

            # Get container client
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Check if container exists
            try:
                await self._throttler_roundtrip.run(container_client.get_container_properties())
            except ResourceNotFoundError:
                raise StorageOperationError(f"Container {container_name} not found", Result.ERROR_NOT_FOUND)

            # Track virtual directories we've seen
            virtual_dirs = set()
            num_components_in_prefix = len(blob_name.rstrip("/").split("/"))

            # List all blobs with the given prefix (no delimiter for recursive listing)
            async for blob in self._throttler_roundtrip.iterate(
                container_client.list_blobs(name_starts_with=blob_name, include=["metadata"])
            ):
                # Process blobs and generate virtual directories

                # Skip directory markers
                if blob.name.endswith("/"):
                    continue

                # Generate virtual directories from blob paths
                path_components = blob.name.split("/")[num_components_in_prefix:-1]
                current_path = blob_name

                for component in path_components:
                    current_path += component + "/"
                    if current_path not in virtual_dirs:
                        virtual_dirs.add(current_path)
                        yield AzureBlobListEntry.folder(current_path, blob_name)

                # Yield the blob itself
                yield AzureBlobListEntry.file_from_blob_properties(blob.name, blob, blob_name)

        except ClientAuthenticationError as e:
            logger.error(f"Azure list_file_tree {url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            raise StorageOperationError(f"Authentication error: {e}", Result.ERROR_ACCESS_DENIED)
        except ResourceNotFoundError as e:
            raise StorageOperationError(f"Resource not found: {e}", Result.ERROR_NOT_FOUND)
        except AzureError as e:
            logger.error(f"Azure error during list_file_tree {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            raise StorageOperationError(f"Azure error: {e}", Result.ERROR)

    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        """List checkpoints for a blob.

        Args:
            url: URL to list checkpoints for

        Returns:
            Tuple of (Result, list of ListEntry objects)
        """
        # Azure Blob Storage doesn't have a direct equivalent to checkpoints
        return Result.ERROR_NOT_SUPPORTED, []

    async def copy(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        """Copy a blob from one location to another.

        Args:
            src_url: Source URL
            dst_url: Destination URL
            overwrite: Whether to overwrite the destination if it exists
            message: Optional message

        Returns:
            Result of the operation
        """
        if not is_azure_url(src_url):
            raise ValueError(f"'{src_url} is not supported by the Azure Blob Storage backend.")
        if not is_azure_url(dst_url):
            raise ValueError(f"'{dst_url} is not supported by the Azure Blob Storage backend.")

        return await self._copy_blob_azure_azure(src_url, dst_url, overwrite)

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        """Check if create_with_hash is supported.

        Args:
            target_url: Target URL

        Returns:
            False as Azure Blob Storage doesn't support this operation
        """
        return False

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        """Create a blob with a specific hash.

        Args:
            copy_target: Target URL
            storage_hash: Hash to use
            overwrite: Whether to overwrite existing blob

        Returns:
            Result of the operation

        Raises:
            NotImplementedError: This operation is not supported
        """
        raise NotImplementedError("create_with_hash is not supported by Azure Blob Storage")

    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        """Read the content of a blob.

        Args:
            file_url: URL of the blob to read

        Returns:
            Tuple of (Result, content as bytes)
        """
        try:
            account_name, container_name, blob_name = parse_azure_url(file_url)

            if blob_name.endswith("/"):
                return Result.ERROR_WRONG_TYPE, bytes()

            # Get blob client
            async_client = await self.azure_client().get_async_instance()
            blob_client = async_client.get_blob_client(container=container_name, blob=blob_name)

            # Download blob
            download_stream = await self._throttler_roundtrip.run(
                blob_client.download_blob(max_concurrency=WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY)
            )
            content = await self._throttler_networkdata.run(download_stream.readall())

            return Result.OK, content

        except ClientAuthenticationError as e:
            logger.error(f"Azure read_file_content {file_url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED, bytes()
        except ResourceNotFoundError:
            return Result.ERROR_NOT_FOUND, bytes()
        except AzureError as e:
            logger.error(f"Azure error during read_file_content {file_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR, bytes()

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        """Get a cached file.

        Args:
            file_url: URL of the blob to get
            download: Whether to download the blob if it's not cached

        Returns:
            Tuple of (Result, LocalReadOnlyFile or None)

        Raises:
            NotImplementedError: This operation is not supported
        """
        # Azure Blob Storage doesn't support local caching in this implementation
        raise NotImplementedError("get_cached_file_async is not supported by Azure Blob Storage")

    async def create_container_if_not_exists(self, container_name):
        # Get container client and create container if it doesn't exist
        async_client = await self.azure_client().get_async_instance()
        container_client = async_client.get_container_client(container_name)
        try:
            await container_client.get_container_properties()
        except ResourceNotFoundError:
            try:
                # Create container if it doesn't exist
                await self._throttler_roundtrip.run(container_client.create_container())
            except AzureError as e:
                logger.error(f"Azure error during create_container {container_name}: {e}")
                raise e

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        """Write content to a blob.

        Args:
            file_url: URL of the blob to write
            content: Content to write
            comment: Comment to add to the blob metadata

        Returns:
            Result of the operation
        """
        try:
            account_name, container_name, blob_name = parse_azure_url(file_url)

            if blob_name.endswith("/"):
                return Result.ERROR_WRONG_TYPE  # Cannot make blobs ending with a '/'

            # Calculate nucleus hash
            nucleus_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(content))

            # Get container client and create container if it doesn't exist
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Get blob client
            blob_client = container_client.get_blob_client(blob_name)

            # Check if blob exists and we're not allowed to overwrite
            try:
                await self._throttler_roundtrip.run(blob_client.get_blob_properties())
                # Blob exists
                if not True:  # Always overwrite for now, similar to S3 implementation
                    return Result.ERROR_ALREADY_EXISTS
            except ResourceNotFoundError:
                # Blob doesn't exist, we can proceed
                pass

            # Upload blob with metadata
            metadata = {NUCLEUS_HASH_METADATA_KEY: nucleus_hash}
            if comment:
                metadata["comment"] = comment

            # Sanitize metadata to ensure it meets Azure Blob Storage requirements
            sanitized_metadata = sanitize_azure_metadata(metadata)

            await self._throttler_networkdata.run(
                blob_client.upload_blob(
                    content,
                    overwrite=True,
                    metadata=sanitized_metadata,
                    max_concurrency=WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY,
                )
            )

            return Result.OK

        except ClientAuthenticationError as e:
            logger.error(f"Azure write_file {file_url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except AzureError as e:
            logger.error(f"Azure error during write_file {file_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    async def delete_item(self, url: str) -> Result:
        """Delete a blob. For deleting directories with their content, use delete_tree

        Args:
            url: URL of the blob to delete

        Returns:
            Result of the operation
        """
        try:
            account_name, container_name, blob_name = parse_azure_url(url)

            # Get container client
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Check if this is a directory-like path
            is_directory_path = blob_name.endswith("/") or blob_name == ""

            if is_directory_path:
                raise ValueError("delete_item will not delete virtual directories, use delete_tree for that!")
            else:
                # For file-like paths, delete the specific blob
                blob_client = container_client.get_blob_client(blob_name)
                try:
                    await self._throttler_roundtrip.run(blob_client.delete_blob())
                    return Result.OK
                except ResourceNotFoundError:
                    return Result.ERROR_NOT_FOUND

        except ClientAuthenticationError as e:
            logger.error(f"Azure delete_item {url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except AzureError as e:
            logger.error(f"Azure error during delete_item {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        """Delete a virtual directory.

        Args:
            url: URL of the directory to delete

        Returns:
            Result of the operation
        """
        try:
            # Make sure the url ends on a slash, it must be a virtual directory!
            url = url.rstrip("/") + "/"
            account_name, container_name, blob_name = parse_azure_url(url)

            # Get container client
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Check if this is a directory-like path, expected as we appended a slash
            is_directory_path = blob_name.endswith("/") or blob_name == ""
            if is_directory_path:

                async def _perform_delete(blob_name):
                    await self._throttler_roundtrip.run(container_client.delete_blob(blob_name))

                # List all blobs with the given prefix
                empty = True
                async with task_node.create_task_group_node() as sub_task_node:
                    async for blob in self._throttler_roundtrip.iterate(container_client.list_blobs(name_starts_with=blob_name)):
                        empty = False
                        sub_task_node.create_task(_perform_delete(blob.name))

                if empty:
                    # No blobs with this prefix
                    return Result.ERROR_NOT_FOUND
                return Result.OK
            else:
                raise RuntimeError(f"Expected a virtual directory path after parsing the object key: {blob_name}")

        except ClientAuthenticationError as e:
            logger.error(f"Azure delete_tree {url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except AzureError as e:
            logger.error(f"Azure error during delete_tree {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    def can_be_cached(self, url: str) -> bool:
        """Check if a URL can be cached.

        Args:
            url: URL to check

        Returns:
            False as Azure Blob Storage doesn't support caching in this implementation
        """
        return False

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        """Upload a local file to Azure Blob Storage.

        Args:
            src_url: Source URL (local file)
            dst_url: Destination URL (Azure Blob Storage)
            overwrite: Whether to overwrite the destination if it exists
            message: Optional message

        Returns:
            Result of the operation
        """
        try:
            # Get local file path
            src_path = get_path_from_url(src_url)

            # Calculate nucleus hash
            with open(src_path, "rb") as f_src:
                nucleus_hash, _ = calc_nucleus_hash_from_stream(f_src)

            # Parse destination URL
            account_name, container_name, blob_name = parse_azure_url(dst_url)

            # Get container client
            async_client = await self.azure_client().get_async_instance()
            container_client = async_client.get_container_client(container_name)

            # Get blob client
            blob_client = container_client.get_blob_client(blob_name)

            # Check if blob exists and we're not allowed to overwrite
            if not overwrite:
                try:
                    await self._throttler_roundtrip.run(blob_client.get_blob_properties())
                    # Blob exists and we're not allowed to overwrite
                    return Result.ERROR_ALREADY_EXISTS
                except ResourceNotFoundError:
                    # Blob doesn't exist, we can proceed
                    pass

            # Upload blob with metadata
            metadata = {NUCLEUS_HASH_METADATA_KEY: nucleus_hash}
            if message:
                metadata["comment"] = message

            # Sanitize metadata to ensure it meets Azure Blob Storage requirements
            sanitized_metadata = sanitize_azure_metadata(metadata)

            with open(src_path, "rb") as f_src:
                await self._throttler_networkdata.run(
                    blob_client.upload_blob(
                        f_src,
                        overwrite=True,
                        metadata=sanitized_metadata,
                        max_concurrency=WRAPP_AZURE_UPLOAD_MAX_CONCURRENCY,
                    )
                )

            return Result.OK

        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"Azure upload_file, permission error {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        except IOError as e:
            logger.error(f"Azure upload_file, IO error {src_url}: {e}")
            return Result.ERROR
        except ClientAuthenticationError as e:
            logger.error(f"Azure upload_file {dst_url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except AzureError as e:
            logger.error(f"Azure error during upload_file {dst_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        """Download a blob to a local file.

        Args:
            src_url: Source URL (Azure Blob Storage)
            dst_url: Destination URL (local file)
            overwrite: Whether to overwrite the destination if it exists

        Returns:
            Result of the operation
        """
        try:
            # Parse source URL
            account_name, container_name, blob_name = parse_azure_url(src_url)

            # Get blob client
            async_client = await self.azure_client().get_async_instance()
            blob_client = async_client.get_blob_client(container=container_name, blob=blob_name)

            # Get destination path
            dst_path = get_path_from_url(dst_url)

            # Create parent directories if they don't exist
            dst_dir = os.path.dirname(dst_path)
            os.makedirs(dst_dir, exist_ok=True)

            # Download blob
            with open(dst_path, "wb" if overwrite else "xb") as f_dst:
                download_stream = await self._throttler_roundtrip.run(
                    blob_client.download_blob(max_concurrency=WRAPP_AZURE_DOWNLOAD_MAX_CONCURRENCY)
                )
                blob_data = await self._throttler_networkdata.run(download_stream.readall())
                f_dst.write(blob_data)

            return Result.OK

        except FileExistsError:
            return Result.ERROR_ALREADY_EXISTS
        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"Azure download_file, permission error {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        except ResourceNotFoundError:
            return Result.ERROR_NOT_FOUND
        except ClientAuthenticationError as e:
            logger.error(f"Azure download_file {src_url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except AzureError as e:
            logger.error(f"Azure error during download_file {src_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR
        except BaseException as e:
            logger.error(f"Azure download_file, error {src_url}: {e}")
            return Result.ERROR

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        """Set authentication information.

        Args:
            auth_info: List of StorageAuthInfo objects
        """
        if not auth_info:
            if self._auth_info:
                logger.info(f"Clearing auth information for Azure Blob Storage for host {self._auth_info.server_url}")
            self._auth_info = None
        else:
            if len(auth_info) > 1:
                logger.error(
                    f"Azure Blob Storage only supports one set of credentials. Only using credentials for {auth_info[0].server_url}, ignore other credentials."
                )
            if not isinstance(auth_info[0].auth_data, AzureBlobAuthInfo):
                raise StorageOperationError(f"Unexpected type of credentials for Azure Blob Storage for {auth_info[0].server_url}.")
            logger.info(f"Setting auth information for Azure Blob Storage for host {auth_info[0].server_url}")
            self._auth_info = auth_info[0]
        if self._azure_client:
            self._azure_client.set_new_auth_info(self._auth_info)

    def add_auth_info(self, auth_info: StorageAuthInfo):
        """Add authentication information.

        Args:
            auth_info: StorageAuthInfo object

        Raises:
            StorageOperationError: If the authentication information is incompatible
        """
        if not isinstance(auth_info.auth_data, AzureBlobAuthInfo):
            raise StorageOperationError(
                f"Incompatible authentication information for server {auth_info.server_url}: expected AzureBlobAuthInfo, got {auth_info.auth_data.__class__}"
            )
        if self._auth_info and self._auth_info != auth_info:
            logger.error(
                f"Azure Blob Storage only supports one set of credentials. Overwriting credentials for {self._auth_info.server_url} with new credentials for {auth_info.server_url}."
            )
        self.set_auth_info([auth_info])

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        """Parse authentication parameters.

        Args:
            auth_param: Authentication parameters string

        Returns:
            StorageAuthInfo object

        Raises:
            StorageOperationError: If the authentication parameters are invalid
        """
        try:
            auth_params = auth_param.split(",")
            server_url = auth_params[0]
            connection_string = auth_params[1]
        except IndexError as e:
            raise StorageOperationError(
                f"Authentication parameter for Azure Blob Storage needs to be of the form server_url,connection_string!"
            )
        if not connection_string:
            raise StorageOperationError(f"Authentication parameter for Azure Blob Storage given contains empty connection string")
        return StorageAuthInfo(server_url, AzureBlobAuthInfo(connection_string=connection_string))

    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        """Get the last connection status.

        Args:
            server_url: Server URL

        Returns:
            ConnectionStatus or None
        """
        return self._last_connection_status

    # Azure Blob Storage specific methods

    async def _copy_blob_azure_azure(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        """Copy a blob from one Azure Blob Storage location to another.

        Args:
            src_url: Source URL
            dst_url: Destination URL
            overwrite: Whether to overwrite the destination if it exists

        Returns:
            Result of the operation
        """
        try:
            # Parse URLs
            src_account_name, src_container_name, src_blob_name = parse_azure_url(src_url)
            dst_account_name, dst_container_name, dst_blob_name = parse_azure_url(dst_url)

            # Check if source and destination are the same
            if src_container_name == dst_container_name and src_blob_name == dst_blob_name:
                return Result.ERROR_SOURCE_IS_DEST

            # Get source blob client
            async_client = await self.azure_client().get_async_instance()
            src_blob_client = async_client.get_blob_client(container=src_container_name, blob=src_blob_name)

            # Check if source exists
            try:
                src_properties = await self._throttler_roundtrip.run(src_blob_client.get_blob_properties())
            except ResourceNotFoundError:
                return Result.ERROR_NOT_FOUND

            # Get destination container client and create container if it doesn't exist
            dst_container_client = async_client.get_container_client(dst_container_name)

            # Check if destination container exists
            try:
                await self._throttler_roundtrip.run(dst_container_client.get_container_properties())
            except ResourceNotFoundError:
                raise StorageOperationError(f"Container {dst_container_name} not found", Result.ERROR_NOT_FOUND)

            # Get destination blob client
            dst_blob_client = dst_container_client.get_blob_client(dst_blob_name)

            # Check if destination exists and we're not allowed to overwrite
            if not overwrite:
                try:
                    await self._throttler_roundtrip.run(dst_blob_client.get_blob_properties())
                    # Destination exists and we're not allowed to overwrite
                    return Result.ERROR_ALREADY_EXISTS
                except ResourceNotFoundError:
                    # Destination doesn't exist, we can proceed
                    pass

            # Get source URL for copy operation
            source_url = src_blob_client.url

            # Sanitize metadata to ensure it meets Azure Blob Storage requirements
            sanitized_metadata = sanitize_azure_metadata(src_properties.metadata)

            # Copy blob
            copy_props = await self._throttler_serverdata.run(dst_blob_client.start_copy_from_url(source_url, metadata=sanitized_metadata))

            # Check copy status
            copy_status = copy_props["copy_status"]
            if copy_status == "success":
                return Result.OK
            elif copy_status == "pending":
                # Wait for copy to complete
                while copy_status == "pending":
                    props = await dst_blob_client.get_blob_properties()
                    copy_status = props["copy_status"]
                    if copy_status == "success":
                        return Result.OK
                    elif copy_status == "pending":
                        await asyncio.sleep(1)  # Wait before checking again
            logger.error(f"Azure copy operation failed with status: {copy_status}")
            return Result.ERROR

        except ClientAuthenticationError as e:
            logger.error(f"Azure copy {src_url} to {dst_url} failed because of authentication error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except AzureError as e:
            logger.error(f"Azure error during copy {src_url} to {dst_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR
