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
import json
import logging
import os
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
from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage_interface import (
    ConnectionStatus,
    DirectorySemantics,
    GCSAuthInfo,
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
    parse_env_float,
)

logger = logging.getLogger("omni.wrapp")

# Prevent google.auth.default() from probing the GCE metadata server, which
# hangs for ~9 s on non-GCE machines.  Must be set before the google.auth
# module caches the value at import time.  Users on Compute Engine can
# override with NO_GCE_CHECK=false.
if "NO_GCE_CHECK" not in os.environ:
    os.environ["NO_GCE_CHECK"] = "true"

# Try to import required dependencies
try:
    import requests
    from google.api_core.exceptions import (
        Forbidden,
        GoogleAPICallError,
        NotFound,
        RetryError,
    )
    from google.api_core.retry import Retry
    from google.auth.exceptions import (
        DefaultCredentialsError,
        GoogleAuthError,
    )
    from google.cloud import storage as gcs_storage
    from google.cloud.storage import Blob
    from google.cloud.storage.retry import DEFAULT_RETRY
    from google.oauth2 import service_account
    from requests.adapters import HTTPAdapter
except ImportError as e:
    # The list of dependencies should be the same as in pyproject.toml
    missing = getattr(e, "name", str(e))
    error_msg = f"failed to import {missing}. Required dependency: google-cloud-storage. Install with 'pip install ovpackage[gcs]'. Exception: {e}"
    raise StorageMissingDependenciesError(error_msg) from e


# Constants
NUCLEUS_HASH_METADATA_KEY = "nv_omniverse_nucleus_hash"


def _log_gcs_retry(exc: Exception) -> None:
    """``on_error`` hook for the GCS SDK retry: log each transient error that triggers a retry."""
    logger.warning(f"GCS transient error, retrying: {exc!r}")


def parse_gcs_url(url: str) -> Tuple[str, str]:
    """Parse a Google Cloud Storage URL into bucket name and object name.

    Args:
        url: Google Cloud Storage URL

    Returns:
        Tuple of (bucket_name, object_name)

    Raises:
        ValueError: If the URL is not a valid Google Cloud Storage URL
    """
    parsed = urlparse(url)

    if parsed.scheme == "gs":
        # gs://bucket/key
        bucket_name = parsed.netloc
        object_name = parsed.path.lstrip("/")
        if url.endswith("/") and object_name != "" and not object_name.endswith("/"):
            object_name = object_name + "/"
        return bucket_name, object_name

    elif parsed.scheme == "https" and "storage.googleapis.com" in parsed.netloc:
        # https://storage.googleapis.com/bucket/key
        path_parts = parsed.path.strip("/").split("/", 1)
        bucket_name = path_parts[0] if path_parts else ""
        object_name = path_parts[1] if len(path_parts) > 1 else ""
        if url.endswith("/") and object_name != "":
            object_name = object_name + "/"
        return bucket_name, object_name

    raise ValueError(f"Not a Google Cloud Storage URL: {url}")


def is_gcs_url(url: str) -> bool:
    """Check if a URL is a Google Cloud Storage URL.

    Args:
        url: URL to check

    Returns:
        True if the URL is a Google Cloud Storage URL, False otherwise
    """
    try:
        bucket_name, object_name = parse_gcs_url(url)
        return bool(bucket_name)
    except ValueError:
        return False


class GCSListEntry(ListEntry):
    """Implementation of ListEntry for Google Cloud Storage."""

    @classmethod
    def file_from_blob(cls, blob_name: str, blob: Blob, parent_path: str = "") -> "GCSListEntry":
        """Create a ListEntry from a GCS Blob object.

        Args:
            blob_name: Name of the blob
            blob: GCS Blob object
            parent_path: Parent path for relative path calculation

        Returns:
            GCSListEntry instance
        """
        size = blob.size
        last_modified = blob.updated
        etag = blob.etag or ""

        # Extract nucleus hash from metadata if available
        nucleus_hash = None
        if blob.metadata and NUCLEUS_HASH_METADATA_KEY in blob.metadata:
            nucleus_hash = blob.metadata[NUCLEUS_HASH_METADATA_KEY]

        return GCSListEntry(
            blob_name,
            etag=etag,
            is_folder=False,
            parent_path=parent_path,
            size=size,
            last_modified=last_modified,
            nucleus_hash=nucleus_hash,
        )

    @classmethod
    def file_from_properties(
        cls,
        blob_name: str,
        *,
        size: Optional[int],
        last_modified: Optional[datetime],
        etag: str = "",
        parent_path: str = "",
        nucleus_hash: Optional[str] = None,
    ) -> "GCSListEntry":
        """Create a ListEntry from blob properties."""
        return GCSListEntry(
            blob_name,
            etag=etag,
            is_folder=False,
            parent_path=parent_path,
            size=size,
            last_modified=last_modified,
            nucleus_hash=nucleus_hash,
        )

    @classmethod
    def folder(cls, folder_name: str, parent_path: str = "") -> "GCSListEntry":
        """Create a folder ListEntry.

        Args:
            folder_name: Name of the folder
            parent_path: Parent path for relative path calculation

        Returns:
            GCSListEntry instance representing a folder
        """
        # Ensure folder name ends with "/"
        if not folder_name.endswith("/") and folder_name != "":
            folder_name += "/"

        return GCSListEntry(folder_name.rstrip("/"), etag="", is_folder=True, parent_path=parent_path)

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


class GCSServerInfo(ServerInfo):
    """Implementation of ServerInfo for Google Cloud Storage."""

    def supports_create_with_hash(self, target_url: str) -> bool:
        return False

    @property
    def auth_token(self) -> str:
        return "NOT_IMPLEMENTED_AUTH_TOKEN"


class GCSClientWrapper:
    """Wrapper for Google Cloud Storage client."""

    def __init__(self, auth_info: Optional[StorageAuthInfo] = None, pool_size: int = 0):
        self._sync_client: Optional[gcs_storage.Client] = None
        self._loop: Optional[AbstractEventLoop] = None
        self._auth_data: Optional[GCSAuthInfo] = None
        self._auth_is_dirty = False
        self._pool_size = pool_size
        self._endpoint = os.environ.get("WRAPP_GCS_ENDPOINT", None)
        self.set_new_auth_info(auth_info)

    def set_new_auth_info(self, auth_info: Optional[StorageAuthInfo]):
        auth_data: Optional[GCSAuthInfo] = None
        if not auth_info:
            # Check for environment variables
            project_id = os.environ.get("WRAPP_GCS_PROJECT", None)
            credentials_file = os.environ.get("WRAPP_GCS_CREDENTIALS_FILE", None)

            if project_id or credentials_file:
                auth_data = GCSAuthInfo(project_id=project_id, credentials_file=credentials_file)
                logger.info("Connecting WRAPP to Google Cloud Storage with credentials from environment variables")
            else:
                # Will use application default credentials
                auth_data = GCSAuthInfo()
                logger.info("Connecting WRAPP to Google Cloud Storage with application default credentials")
        else:
            assert isinstance(auth_info.auth_data, GCSAuthInfo)
            auth_data = auth_info.auth_data
            logger.info("Connecting WRAPP to Google Cloud Storage with credentials provided by the caller")

        self._auth_data = auth_data
        self._auth_is_dirty = True

    def _create_sync_client(self) -> gcs_storage.Client:
        """Create a synchronous GCS client."""
        try:
            kwargs: Dict = {}

            if self._auth_data:
                if self._auth_data.project_id:
                    kwargs["project"] = self._auth_data.project_id
                if self._auth_data.credentials_file:
                    credentials = service_account.Credentials.from_service_account_file(self._auth_data.credentials_file)
                    kwargs["credentials"] = credentials
                elif self._auth_data.credentials_json:
                    info = json.loads(self._auth_data.credentials_json)
                    credentials = service_account.Credentials.from_service_account_info(info)
                    kwargs["credentials"] = credentials

            if self._endpoint:
                # For fake-gcs-server or custom endpoints
                if "credentials" not in kwargs:
                    from google.auth.credentials import AnonymousCredentials

                    kwargs["credentials"] = AnonymousCredentials()
                if "project" not in kwargs:
                    kwargs["project"] = "test-project"
                from google.api_core.client_options import ClientOptions

                kwargs["client_options"] = ClientOptions(api_endpoint=self._endpoint)
                client = gcs_storage.Client(**kwargs)
            elif "credentials" not in kwargs:
                # Let google.auth.default() discover credentials from all
                # standard sources: GOOGLE_APPLICATION_CREDENTIALS env var,
                # gcloud CLI ADC file, App Engine, etc.  The GCE metadata
                # server probe is disabled via NO_GCE_CHECK (set at module
                # level) so this won't hang on non-GCE machines.
                if "project" not in kwargs:
                    kwargs["project"] = "none"
                try:
                    client = gcs_storage.Client(**kwargs)
                except DefaultCredentialsError:
                    from google.auth.credentials import AnonymousCredentials

                    kwargs["credentials"] = AnonymousCredentials()
                    logger.warning(
                        "No GCS credentials found. Checked GOOGLE_APPLICATION_CREDENTIALS, "
                        "gcloud CLI default credentials, and WRAPP_GCS_CREDENTIALS_FILE. "
                        "Using anonymous credentials -- operations will fail until credentials are provided."
                    )
                    client = gcs_storage.Client(**kwargs)
            else:
                client = gcs_storage.Client(**kwargs)

            if self._pool_size > 0:
                self._remount_http_adapters(client._http, self._pool_size)

            self._auth_is_dirty = False
            return client
        except Exception as e:
            raise StorageOperationError(f"Failed to initialize Google Cloud Storage client: {str(e)}.\n{traceback.format_exc()}")

    def get_sync_client(self) -> gcs_storage.Client:
        """Get or create a synchronous GCS client."""
        if self._sync_client is None or self._auth_is_dirty:
            if self._sync_client is not None:
                self._sync_client.close()
            self._sync_client = self._create_sync_client()
        return self._sync_client

    def update_pool_size(self, new_pool_size: int) -> None:
        """Re-mount HTTP adapters with updated pool size on existing client."""
        self._pool_size = new_pool_size
        if self._sync_client is not None and new_pool_size > 0:
            self._remount_http_adapters(self._sync_client._http, new_pool_size)

    @staticmethod
    def _remount_http_adapters(http_session: requests.Session, pool_size: int) -> None:
        """Apply adapter pool sizing to the main and nested auth sessions."""

        def mount_pool(session: requests.Session) -> None:
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, pool_block=True)
            session.mount("https://", adapter)

        sessions: list[requests.Session] = [http_session]

        # google-auth currently stores a dedicated session for token refreshes.
        auth_request_session = getattr(http_session, "_auth_request_session", None)
        if isinstance(auth_request_session, requests.Session):
            sessions.append(auth_request_session)

        # Some versions expose the refresh request session via _auth_request.session.
        auth_request = getattr(http_session, "_auth_request", None)
        nested_auth_session = getattr(auth_request, "session", None)
        if isinstance(nested_auth_session, requests.Session) and nested_auth_session not in sessions:
            sessions.append(nested_auth_session)

        for session in sessions:
            mount_pool(session)

    async def close_instance(self):
        """Close the client instances."""
        if self._sync_client is not None:
            client_to_close = self._sync_client
            self._sync_client = None
            client_to_close.close()


class GCSStorage(StorageInterface):
    """Implementation of StorageInterface for Google Cloud Storage."""

    @classmethod
    def version_info(cls) -> str:
        try:
            from importlib.metadata import version

            gcs_version = version("google-cloud-storage")
            return f"Google Cloud Storage Backend (google-cloud-storage {gcs_version})"
        except Exception:
            return "Google Cloud Storage Backend (version unknown)"

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_params: ThrottlingParameters):
        self._gcs_client: Optional[GCSClientWrapper] = None
        self._auth_info: Optional[StorageAuthInfo] = None
        self._last_connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "GCS", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "GCS", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "GCS", throttling_params.jobs)
        # Configure the google-cloud-storage client's own retry/back-off. Each knob is only
        # overridden when its env var is set; otherwise the library's own default is used
        # (we do not impose WRAPP-specific defaults). WRAPP_GCS_TIMEOUT_SECONDS maps to the
        # retry deadline (the "Timeout of Ns exceeded" RetryError). Reuse DEFAULT_RETRY's
        # transient-error predicate and attach an on_error hook so the client's internal
        # retries are logged as warnings.
        retry_overrides: dict = {}
        for env_name, key in (
            ("WRAPP_GCS_RETRY_INITIAL_SECONDS", "initial"),
            ("WRAPP_GCS_RETRY_MAXIMUM_SECONDS", "maximum"),
            ("WRAPP_GCS_RETRY_MULTIPLIER", "multiplier"),
            ("WRAPP_GCS_TIMEOUT_SECONDS", "timeout"),
        ):
            value = parse_env_float(env_name)
            if value is None:
                # Unset or invalid (parse_env_float warned): do not pass the kwarg so the
                # google-cloud-storage default applies.
                continue
            retry_overrides[key] = value
        self._retry = Retry(predicate=DEFAULT_RETRY._predicate, on_error=_log_gcs_retry, **retry_overrides)
        # WRAPP_GCS_REQUEST_TIMEOUT_SECONDS configures the per-request transport timeout
        # (the "The write operation timed out" TimeoutError on connections too slow to
        # finish a single request). It is independent of the retry deadline above: raising
        # the deadline only retries the same request-timeout-bounded attempt for longer.
        # Same override-only-when-set contract as the retry knobs.
        self._request_timeout: Optional[float] = parse_env_float("WRAPP_GCS_REQUEST_TIMEOUT_SECONDS")
        # Keyword arguments applied to every google-cloud-storage call. timeout= is only
        # included when overridden so the library's own per-request default applies otherwise.
        self._sdk_call_kwargs: Dict = {"retry": self._retry}
        if self._request_timeout is not None:
            self._sdk_call_kwargs["timeout"] = self._request_timeout

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
                logger.warning(f"Cannot update throttling parameters for GCS: Throttler {t.server_backend_name} has open tasks.")
                return False
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "GCS", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "GCS", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "GCS", throttling_params.jobs)
        if self._gcs_client is not None:
            self._gcs_client.update_pool_size(self._compute_pool_size())
        return True

    def _compute_pool_size(self) -> int:
        """Pool size must cover all throttlers firing concurrently."""
        return (
            self._throttler_roundtrip.max_parallel_tasks
            + self._throttler_networkdata.max_parallel_tasks
            + self._throttler_serverdata.max_parallel_tasks
        )

    def gcs_client(self) -> GCSClientWrapper:
        """Get or create a GCS client wrapper."""
        # Make this a lazy creation, so the user has time to set up the authentication credentials
        if not self._gcs_client:
            self._gcs_client = GCSClientWrapper(self._auth_info, pool_size=self._compute_pool_size())
        return self._gcs_client

    def supports_tags(self, url: str) -> bool:
        return False

    def directory_semantics(self) -> DirectorySemantics:
        return DirectorySemantics.PREFIX

    def supports_comments(self) -> bool:
        return False

    async def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._gcs_client:
            await self._gcs_client.close_instance()

    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        return Result.OK, GCSServerInfo()

    async def normalize(self, url: str) -> str:
        return url

    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        try:
            bucket_name, object_name = parse_gcs_url(url)
            client = self.gcs_client().get_sync_client()
            bucket = client.bucket(bucket_name)

            is_directory_path = object_name.endswith("/") or object_name == ""

            if is_directory_path:
                # For directory-like paths, check if there are any blobs with this prefix
                blobs = list(
                    await self._throttler_roundtrip.run(
                        asyncio.get_running_loop().run_in_executor(
                            None, lambda: list(client.list_blobs(bucket_name, prefix=object_name, max_results=1, **self._sdk_call_kwargs))
                        )
                    )
                )
                if blobs:
                    return Result.OK, GCSListEntry.folder(object_name)
                else:
                    # Check if bucket exists and the prefix is the root
                    try:
                        await self._throttler_roundtrip.run(
                            asyncio.get_running_loop().run_in_executor(None, lambda: bucket.reload(**self._sdk_call_kwargs))
                        )
                        if object_name == "":
                            return Result.OK, GCSListEntry.folder("")
                        else:
                            return Result.ERROR_NOT_FOUND, None
                    except NotFound:
                        return Result.ERROR_NOT_FOUND, None
            else:
                # For file-like paths, try to get the blob properties
                blob = bucket.blob(object_name)
                try:
                    await self._throttler_roundtrip.run(
                        asyncio.get_running_loop().run_in_executor(None, lambda: blob.reload(**self._sdk_call_kwargs))
                    )
                    return Result.OK, GCSListEntry.file_from_blob(object_name, blob)
                except NotFound:
                    # Check if there's a directory with this name
                    directory_path = object_name + "/"
                    blobs = list(
                        await self._throttler_roundtrip.run(
                            asyncio.get_running_loop().run_in_executor(
                                None,
                                lambda: list(client.list_blobs(bucket_name, prefix=directory_path, max_results=1, **self._sdk_call_kwargs)),
                            )
                        )
                    )
                    if blobs:
                        return Result.OK, GCSListEntry.folder(directory_path)
                    else:
                        return Result.ERROR_NOT_FOUND, None

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during stat {url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED, None
        except Forbidden as e:
            logger.error(f"GCS stat {url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED, None
        except NotFound:
            return Result.ERROR_NOT_FOUND, None
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during stat {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR, None

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, ResourceIdentity]:
        raise NotImplementedError("Resource identities not supported for GCS yet")

    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        result, item = await self.stat(url)
        return result, item.is_folder if item else False

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        try:
            bucket_name, object_name = parse_gcs_url(url)

            # Ensure object_name ends with "/" if it's not empty
            if object_name and not object_name.endswith("/"):
                object_name += "/"

            client = self.gcs_client().get_sync_client()

            # Check if bucket exists
            bucket = client.bucket(bucket_name)
            try:
                await self._throttler_roundtrip.run(
                    asyncio.get_running_loop().run_in_executor(None, lambda: bucket.reload(**self._sdk_call_kwargs))
                )
            except NotFound:
                raise StorageOperationError(f"Bucket {bucket_name} not found", Result.ERROR_NOT_FOUND)

            # List blobs with the given prefix and delimiter
            empty_list = True
            loop = asyncio.get_running_loop()

            blobs_iterator = await self._throttler_roundtrip.run(
                loop.run_in_executor(
                    None, lambda: client.list_blobs(bucket_name, prefix=object_name, delimiter="/", **self._sdk_call_kwargs)
                )
            )

            # Process blobs (files)
            pages = await loop.run_in_executor(None, lambda: list(blobs_iterator.pages))
            for page in pages:
                for blob in page:
                    if blob.name != object_name:  # Skip the directory marker itself
                        empty_list = False
                        yield GCSListEntry.file_from_blob(blob.name, blob, object_name)

            # Process prefixes (virtual directories)
            for prefix in blobs_iterator.prefixes:
                empty_list = False
                yield GCSListEntry.folder(prefix, object_name)

            if empty_list:
                # If there is a file in the same place, return ERROR_WRONG_TYPE
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    raise StorageOperationError(f"Trying to list a file {url}", Result.ERROR_WRONG_TYPE)

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during list_directory {url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            raise StorageOperationError(f"Authentication error: {e}", Result.ERROR_ACCESS_DENIED)
        except Forbidden as e:
            logger.error(f"GCS list_directory {url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            raise StorageOperationError(f"Permission error: {e}", Result.ERROR_ACCESS_DENIED)
        except NotFound as e:
            raise StorageOperationError(f"Resource not found: {e}", Result.ERROR_NOT_FOUND)
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during list_directory {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            raise StorageOperationError(f"GCS error: {e}", Result.ERROR)

    def supports_list_file_tree(self) -> bool:
        return True

    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        try:
            bucket_name, object_name = parse_gcs_url(url)

            # Ensure object_name ends with "/" if it's not empty
            if object_name and not object_name.endswith("/"):
                object_name += "/"

            client = self.gcs_client().get_sync_client()

            # Check if bucket exists
            bucket = client.bucket(bucket_name)
            try:
                await self._throttler_roundtrip.run(
                    asyncio.get_running_loop().run_in_executor(None, lambda: bucket.reload(**self._sdk_call_kwargs))
                )
            except NotFound:
                raise StorageOperationError(f"Bucket {bucket_name} not found", Result.ERROR_NOT_FOUND)

            # Track virtual directories we've seen
            virtual_dirs = set()
            num_components_in_prefix = len(object_name.rstrip("/").split("/"))

            loop = asyncio.get_running_loop()
            # List all blobs with the given prefix (no delimiter for recursive listing)
            all_blobs = await self._throttler_roundtrip.run(
                loop.run_in_executor(None, lambda: list(client.list_blobs(bucket_name, prefix=object_name, **self._sdk_call_kwargs)))
            )

            for blob in all_blobs:
                # Skip directory markers
                if blob.name.endswith("/"):
                    continue

                # Generate virtual directories from blob paths
                path_components = blob.name.split("/")[num_components_in_prefix:-1]
                current_path = object_name

                for component in path_components:
                    current_path += component + "/"
                    if current_path not in virtual_dirs:
                        virtual_dirs.add(current_path)
                        yield GCSListEntry.folder(current_path, object_name)

                # Yield the blob itself
                yield GCSListEntry.file_from_blob(blob.name, blob, object_name)

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during list_file_tree {url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            raise StorageOperationError(f"Authentication error: {e}", Result.ERROR_ACCESS_DENIED)
        except Forbidden as e:
            logger.error(f"GCS list_file_tree {url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            raise StorageOperationError(f"Permission error: {e}", Result.ERROR_ACCESS_DENIED)
        except NotFound as e:
            raise StorageOperationError(f"Resource not found: {e}", Result.ERROR_NOT_FOUND)
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during list_file_tree {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            raise StorageOperationError(f"GCS error: {e}", Result.ERROR)

    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        # GCS doesn't have a direct equivalent to checkpoints
        return Result.ERROR_NOT_SUPPORTED, []

    async def copy(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        if not is_gcs_url(src_url):
            raise ValueError(f"'{src_url}' is not supported by the Google Cloud Storage backend.")
        if not is_gcs_url(dst_url):
            raise ValueError(f"'{dst_url}' is not supported by the Google Cloud Storage backend.")

        return await self._copy_blob_gcs_gcs(src_url, dst_url, overwrite)

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        return False

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        try:
            bucket_name, object_name = parse_gcs_url(file_url)

            if object_name.endswith("/"):
                return Result.ERROR_WRONG_TYPE, bytes()

            client = self.gcs_client().get_sync_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_name)

            loop = asyncio.get_running_loop()
            content = await self._throttler_networkdata.run(
                loop.run_in_executor(None, lambda: blob.download_as_bytes(**self._sdk_call_kwargs))
            )

            return Result.OK, content

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during read_file_content {file_url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED, bytes()
        except Forbidden as e:
            logger.error(f"GCS read_file_content {file_url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED, bytes()
        except NotFound:
            return Result.ERROR_NOT_FOUND, bytes()
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during read_file_content {file_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR, bytes()

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        # GCS doesn't support local caching in this implementation
        raise NotImplementedError("get_cached_file_async is not supported by Google Cloud Storage")

    async def create_bucket_if_not_exists(self, bucket_name):
        """Create a bucket if it doesn't exist. Used for testing."""
        client = self.gcs_client().get_sync_client()
        loop = asyncio.get_running_loop()
        try:
            bucket = client.bucket(bucket_name)
            await self._throttler_roundtrip.run(loop.run_in_executor(None, lambda: bucket.reload(**self._sdk_call_kwargs)))
        except GoogleAuthError as e:
            raise StorageOperationError(f"GCS authentication error during create_bucket {bucket_name}: {e}")
        except NotFound:
            try:
                await self._throttler_roundtrip.run(
                    loop.run_in_executor(None, lambda: client.create_bucket(bucket_name, **self._sdk_call_kwargs))
                )
            except (GoogleAPICallError, RetryError) as e:
                logger.error(f"GCS error during create_bucket {bucket_name}: {e}")
                raise e

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        try:
            bucket_name, object_name = parse_gcs_url(file_url)

            if object_name.endswith("/"):
                return Result.ERROR_WRONG_TYPE

            nucleus_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(content))

            client = self.gcs_client().get_sync_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_name)

            metadata = {NUCLEUS_HASH_METADATA_KEY: nucleus_hash}
            if comment:
                metadata["comment"] = comment
            blob.metadata = metadata

            loop = asyncio.get_running_loop()
            await self._throttler_networkdata.run(
                loop.run_in_executor(None, lambda: blob.upload_from_string(content, **self._sdk_call_kwargs))
            )
            return Result.OK

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during write_file {file_url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except Forbidden as e:
            logger.error(f"GCS write_file {file_url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during write_file {file_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    async def delete_item(self, url: str) -> Result:
        try:
            bucket_name, object_name = parse_gcs_url(url)

            is_directory_path = object_name.endswith("/") or object_name == ""

            if is_directory_path:
                raise ValueError("delete_item will not delete virtual directories, use delete_tree for that!")
            else:
                client = self.gcs_client().get_sync_client()
                bucket = client.bucket(bucket_name)
                blob = bucket.blob(object_name)

                loop = asyncio.get_running_loop()
                try:
                    await self._throttler_roundtrip.run(loop.run_in_executor(None, lambda: blob.delete(**self._sdk_call_kwargs)))
                    return Result.OK
                except NotFound:
                    return Result.ERROR_NOT_FOUND

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during delete_item {url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except Forbidden as e:
            logger.error(f"GCS delete_item {url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during delete_item {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        try:
            # Make sure the url ends on a slash, it must be a virtual directory!
            url = url.rstrip("/") + "/"
            bucket_name, object_name = parse_gcs_url(url)

            is_directory_path = object_name.endswith("/") or object_name == ""
            if is_directory_path:
                client = self.gcs_client().get_sync_client()
                loop = asyncio.get_running_loop()

                async def _perform_delete(blob_name):
                    bucket = client.bucket(bucket_name)
                    blob = bucket.blob(blob_name)
                    await self._throttler_roundtrip.run(loop.run_in_executor(None, lambda: blob.delete(**self._sdk_call_kwargs)))

                # List all blobs with the given prefix
                all_blobs = await self._throttler_roundtrip.run(
                    loop.run_in_executor(None, lambda: list(client.list_blobs(bucket_name, prefix=object_name, **self._sdk_call_kwargs)))
                )

                if not all_blobs:
                    return Result.ERROR_NOT_FOUND

                async with task_node.create_task_group_node() as sub_task_node:
                    for blob in all_blobs:
                        sub_task_node.create_task(_perform_delete(blob.name))

                return Result.OK
            else:
                raise RuntimeError(f"Expected a virtual directory path after parsing the object key: {object_name}")

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during delete_tree {url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except Forbidden as e:
            logger.error(f"GCS delete_tree {url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during delete_tree {url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    def can_be_cached(self, url: str) -> bool:
        return False

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        try:
            src_path = get_path_from_url(src_url)

            with open(src_path, "rb") as f_src:
                nucleus_hash, _ = calc_nucleus_hash_from_stream(f_src)

            bucket_name, object_name = parse_gcs_url(dst_url)

            client = self.gcs_client().get_sync_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_name)

            if not overwrite:
                loop = asyncio.get_running_loop()
                exists = await self._throttler_roundtrip.run(loop.run_in_executor(None, lambda: blob.exists(**self._sdk_call_kwargs)))
                if exists:
                    return Result.ERROR_ALREADY_EXISTS

            metadata = {NUCLEUS_HASH_METADATA_KEY: nucleus_hash}
            if message:
                metadata["comment"] = message
            blob.metadata = metadata

            loop = asyncio.get_running_loop()
            await self._throttler_networkdata.run(
                loop.run_in_executor(None, lambda: blob.upload_from_filename(src_path, **self._sdk_call_kwargs))
            )
            return Result.OK

        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"GCS upload_file, permission error {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        except IOError as e:
            logger.error(f"GCS upload_file, IO error {src_url}: {e}")
            return Result.ERROR
        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during upload_file {dst_url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except Forbidden as e:
            logger.error(f"GCS upload_file {dst_url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during upload_file {dst_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        try:
            bucket_name, object_name = parse_gcs_url(src_url)

            client = self.gcs_client().get_sync_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(object_name)

            dst_path = get_path_from_url(dst_url)
            dst_dir = os.path.dirname(dst_path)
            os.makedirs(dst_dir, exist_ok=True)

            if not overwrite and os.path.exists(dst_path):
                return Result.ERROR_ALREADY_EXISTS

            loop = asyncio.get_running_loop()
            await self._throttler_networkdata.run(
                loop.run_in_executor(None, lambda: blob.download_to_filename(dst_path, **self._sdk_call_kwargs))
            )
            return Result.OK

        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"GCS download_file, permission error {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        except NotFound:
            return Result.ERROR_NOT_FOUND
        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during download_file {src_url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except Forbidden as e:
            logger.error(f"GCS download_file {src_url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during download_file {src_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR
        except Exception as e:
            logger.error(f"GCS download_file, error {src_url}: {e}")
            return Result.ERROR

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        if not auth_info:
            if self._auth_info:
                logger.info(f"Clearing auth information for Google Cloud Storage for host {self._auth_info.server_url}")
            self._auth_info = None
        else:
            if len(auth_info) > 1:
                logger.error(
                    f"Google Cloud Storage only supports one set of credentials. Only using credentials for {auth_info[0].server_url}, ignore other credentials."
                )
            if not isinstance(auth_info[0].auth_data, GCSAuthInfo):
                raise StorageOperationError(f"Unexpected type of credentials for Google Cloud Storage for {auth_info[0].server_url}.")
            logger.info(f"Setting auth information for Google Cloud Storage for host {auth_info[0].server_url}")
            self._auth_info = auth_info[0]
        if self._gcs_client:
            self._gcs_client.set_new_auth_info(self._auth_info)

    def add_auth_info(self, auth_info: StorageAuthInfo):
        if not isinstance(auth_info.auth_data, GCSAuthInfo):
            raise StorageOperationError(
                f"Incompatible authentication information for server {auth_info.server_url}: expected GCSAuthInfo, got {auth_info.auth_data.__class__}"
            )
        if self._auth_info and self._auth_info != auth_info:
            logger.error(
                f"Google Cloud Storage only supports one set of credentials. Overwriting credentials for {self._auth_info.server_url} with new credentials for {auth_info.server_url}."
            )
        self.set_auth_info([auth_info])

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        try:
            auth_params = auth_param.split(",")
            server_url = auth_params[0]
            credentials_file_or_json = auth_params[1] if len(auth_params) > 1 else ""
        except IndexError as e:
            raise StorageOperationError(
                "Authentication parameter for Google Cloud Storage needs to be of the form server_url,credentials_file_or_json!"
            ) from e
        if not credentials_file_or_json:
            # No credentials provided, use application default credentials
            return StorageAuthInfo(server_url, GCSAuthInfo())

        # Check if it's a file path or inline JSON
        if credentials_file_or_json.strip().startswith("{"):
            return StorageAuthInfo(server_url, GCSAuthInfo(credentials_json=credentials_file_or_json))
        else:
            return StorageAuthInfo(server_url, GCSAuthInfo(credentials_file=credentials_file_or_json))

    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        return self._last_connection_status

    # GCS specific methods

    async def _copy_blob_gcs_gcs(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        try:
            src_bucket_name, src_object_name = parse_gcs_url(src_url)
            dst_bucket_name, dst_object_name = parse_gcs_url(dst_url)

            # Check if source and destination are the same
            if src_bucket_name == dst_bucket_name and src_object_name == dst_object_name:
                return Result.ERROR_SOURCE_IS_DEST

            client = self.gcs_client().get_sync_client()
            src_bucket = client.bucket(src_bucket_name)
            src_blob = src_bucket.blob(src_object_name)

            # Check if source exists
            loop = asyncio.get_running_loop()
            try:
                await self._throttler_roundtrip.run(loop.run_in_executor(None, lambda: src_blob.reload(**self._sdk_call_kwargs)))
            except NotFound:
                return Result.ERROR_NOT_FOUND

            dst_bucket = client.bucket(dst_bucket_name)

            # Check if destination exists and we're not allowed to overwrite
            if not overwrite:
                dst_blob = dst_bucket.blob(dst_object_name)
                exists = await self._throttler_roundtrip.run(loop.run_in_executor(None, lambda: dst_blob.exists(**self._sdk_call_kwargs)))
                if exists:
                    return Result.ERROR_ALREADY_EXISTS

            # Server-side copy
            await self._throttler_serverdata.run(
                loop.run_in_executor(None, lambda: src_bucket.copy_blob(src_blob, dst_bucket, dst_object_name, **self._sdk_call_kwargs))
            )

            return Result.OK

        except GoogleAuthError as e:
            logger.error(f"GCS authentication error during copy {src_url} to {dst_url}: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except Forbidden as e:
            logger.error(f"GCS copy {src_url} to {dst_url} failed because of permission error: {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
            return Result.ERROR_ACCESS_DENIED
        except (GoogleAPICallError, RetryError) as e:
            logger.error(f"GCS error during copy {src_url} to {dst_url}: {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
            return Result.ERROR
