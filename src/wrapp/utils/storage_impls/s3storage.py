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
import traceback
import urllib.parse
from collections.abc import AsyncGenerator
from datetime import datetime
from random import uniform
from typing import (
    Any,
    BinaryIO,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.coro import assert_is_wrapp_thread
from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage_impls.s3_helper import parse_s3_bucket_key
from wrapp.utils.storage_interface import (
    Boto3AuthInfo,
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

# Try to import required dependencies
try:
    import aioboto3
    import boto3
    import botocore.config
    from botocore.exceptions import (
        BaseEndpointResolverError,
        BotoCoreError,
        ClientError,
        ConnectionClosedError,
        CredentialRetrievalError,
        NoAuthTokenError,
        NoCredentialsError,
        PartialCredentialsError,
        TokenRetrievalError,
        UnknownRegionError,
        UnknownServiceError,
        UnknownSignatureVersionError,
    )
except ImportError as e:
    # The list of dependencies should be the same as in pyproject.toml
    missing = getattr(e, "name", str(e))
    error_msg = f"failed to import {missing}. Required dependencies: aioboto3; recommended: botocore[crt]. Install with 'pip install ovpackage[s3]'. Exception: {e}"
    raise StorageMissingDependenciesError(error_msg) from e


AWS_METADATA_KEY = "Metadata"
NUCLEUS_HASH_METADATA_KEY = "nv-omniverse-nucleus-hash"
# NUCLEUS_HASH_FULL_METADATA_KEY = "x-amz-metadata-" + NUCLEUS_HASH_METADATA_KEY
CRC32_CHECKSUM_METADATA_KEY = "ChecksumCRC32"
SHA256_CHECKSUM_METADATA_KEY = "ChecksumSHA256"


def is_s3_url(url: str) -> bool:
    try:
        bucket, key, region = parse_s3_bucket_key(url)
        return bucket is not None and key is not None
    except ValueError:
        return False


class S3ListEntry(ListEntry):
    @classmethod
    def file_from_head_object(cls, key: str, data: Dict[str, Any]) -> "S3ListEntry":
        assert not key.endswith("/")
        size: int = data["ContentLength"]
        last_modified: datetime = data["LastModified"]
        etag: str = data["ETag"].strip('"')
        if AWS_METADATA_KEY in data and NUCLEUS_HASH_METADATA_KEY in data[AWS_METADATA_KEY]:
            nucleus_hash = data[AWS_METADATA_KEY][NUCLEUS_HASH_METADATA_KEY]
        else:
            nucleus_hash = None
        if SHA256_CHECKSUM_METADATA_KEY in data:
            checksum = "sha256;" + data[SHA256_CHECKSUM_METADATA_KEY]
        elif CRC32_CHECKSUM_METADATA_KEY in data:
            checksum = "crc32;" + data[CRC32_CHECKSUM_METADATA_KEY]
        else:
            checksum = None
        return S3ListEntry(
            key, etag=etag, is_folder=False, size=size, last_modified=last_modified, nucleus_hash=nucleus_hash, s3_hash=checksum
        )

    @classmethod
    def file_from_list_objects(cls, parent_path: str, data: Dict[str, Any]) -> "S3ListEntry":
        key = data["Key"]
        assert isinstance(key, str) and not key.endswith("/")
        size: int = data["Size"]
        last_modified: datetime = data["LastModified"]
        etag: str = data["ETag"].strip('"')
        # List does not give us metadata (nucleus-hash) or attributes (crc32/sha256), only etag
        return S3ListEntry(key, etag=etag, is_folder=False, parent_path=parent_path, size=size, last_modified=last_modified)

    @classmethod
    def folder(cls, key: str, parent_path: Optional[str] = None) -> "S3ListEntry":
        assert key.endswith("/") or key == ""
        return S3ListEntry(key.rstrip("/"), etag="", is_folder=True, parent_path=parent_path)

    def __init__(
        self,
        key: str,
        *,
        etag: str,
        is_folder: bool,
        parent_path: Optional[str] = None,
        size: Optional[int] = None,
        last_modified: Optional[datetime] = None,
        nucleus_hash: Optional[str] = None,
        s3_hash: Optional[str] = None,
    ) -> None:
        if parent_path is None:
            parent_path = ""
        assert not parent_path or parent_path.endswith("/")
        assert isinstance(key, str)
        assert key.startswith(parent_path)

        assert size is None or (isinstance(size, int) and size >= 0)
        assert last_modified is None or isinstance(last_modified, datetime)
        assert nucleus_hash is None or (isinstance(nucleus_hash, str) and (nucleus_hash != ""))
        assert s3_hash is None or (isinstance(s3_hash, str) and (s3_hash != ""))

        self._etag: str = etag
        self._is_folder = is_folder
        self._relative_path = key[len(parent_path) :]
        self._size: Optional[int] = size  # attribute.get('ContentLength', 0)
        self._last_modified: Optional[datetime] = last_modified  # attribute.get('LastModified', None)
        self._nucleus_hash: Optional[str] = nucleus_hash
        self._s3_hash: Optional[str] = s3_hash

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


class S3ServerInfo(ServerInfo):
    def supports_create_with_hash(self, target_url: str) -> bool:
        return False

    @property
    def auth_token(self) -> str:
        return "NOT_IMPLEMENTED_AUTH_TOKEN"


class S3ClientWrapper:
    def __init__(self, auth_info: StorageAuthInfo | None):
        self._s3_endpoint = os.environ.get("WRAPP_S3_ENDPOINT", None)
        if os.environ.get("WRAPP_ENABLE_MINIO_BOTO3_COMPATIBILITY", "False") not in ["False", "false", "0"]:
            os.environ[
                "BOTO_EXPERIMENTAL__NO_EMPTY_CONTINUE"
            ] = "true"  # Avoid issues with minio and boto3, see OMPE-16494 and https://github.com/boto/botocore/pull/3123
        elif self.endpoint_looks_like_minio():
            logger.warning(
                f"If you are running with a minio server, set the WRAPP_ENABLE_MINIO_BOTO3_COMPATIBILITY environment variable to 'True' to avoid error messages about 'MissingHeaderBodySeparatorDefect'"
            )
        self._instance = None
        self._aws_region = os.environ.get("WRAPP_TEST_S3_REGION", None)
        self._s3_session = None
        self._client_context_manager = None
        self._loop = None
        # Lock to prevent race conditions during concurrent client initialization.
        # This is necessary because aioboto3 uses contextvars internally, and concurrent
        # initialization from multiple async tasks can cause ContextVar errors.
        self._init_lock = asyncio.Lock()

        self._auth_data: Boto3AuthInfo | None = None
        self._auth_is_dirty = False
        self.set_new_auth_info(auth_info)

    def set_new_auth_info(self, auth_info: Optional[StorageAuthInfo]):
        if not auth_info:
            # In gitlab CI AWS_ACCESS_KEY_ID is set with the values to allow the docs to be uploaded.
            # The WRAPP_AWS_... environment variables allows to specifically set the AWS credentials for WRAPP.
            aws_access_key_id = os.environ.get("WRAPP_AWS_ACCESS_KEY_ID", None)
            aws_secret_access_key = os.environ.get("WRAPP_AWS_SECRET_ACCESS_KEY", None)
            if aws_access_key_id and aws_secret_access_key:
                self._auth_data = Boto3AuthInfo(aws_access_key_id, aws_secret_access_key)
                logger.info(f"Connecting WRAPP to S3 storage at '{self._s3_endpoint}' from WRAPP_AWS_... env variables")
            else:
                logger.info(f"Connecting WRAPP to S3 storage with default boto3 credentials")
        else:
            assert isinstance(auth_info.auth_data, Boto3AuthInfo)
            self._auth_data = auth_info.auth_data
            logger.info(f"Connecting WRAPP to S3 storage at '{self._s3_endpoint}' with credentials provided by the caller")
        self._auth_is_dirty = True

    def endpoint_looks_like_minio(self):
        return self._s3_endpoint and (
            "minio" in self._s3_endpoint
            or "localhost" in self._s3_endpoint
            or "127.0.0.1" in self._s3_endpoint
            or ":9000" in self._s3_endpoint
        )

    def endpoint(self):
        return self._s3_endpoint

    def _create_session(self):
        """Create a new aioboto3 session (non-async method)"""
        assert_is_wrapp_thread()
        try:
            if self._auth_data:
                s = aioboto3.Session(
                    aws_access_key_id=self._auth_data.aws_access_key_id,
                    aws_secret_access_key=self._auth_data.aws_secret_access_key,
                    aws_session_token=self._auth_data.aws_session_token,
                )
            else:
                s = aioboto3.Session()
            self._auth_is_dirty = False
            return s
        except BotoCoreError as e:
            # This needs to be a StorageOperationError because S3Storage does lazy initialization.
            raise StorageOperationError(f"Failed to initialize aioboto3 session: {str(e)}.\n{traceback.format_exc()}")

    async def session(self):
        """Get or create an aioboto3 session.

        Note: This method should only be called while holding _init_lock from get_instance()
        to ensure thread-safe session creation.
        """
        if self._s3_session is None:
            self._s3_session = self._create_session()
            # Store the current event loop to ensure we're using the same one
            self._loop = asyncio.get_running_loop()
        return self._s3_session

    async def get_instance(self):
        """Get or create an S3 client instance.

        This method is safe for concurrent access from multiple async tasks.
        Uses double-checked locking pattern to minimize lock contention while
        ensuring thread-safe initialization of the aioboto3 client.
        """
        assert_is_wrapp_thread()

        # Check if we're in the same event loop as when we created the session
        current_loop = asyncio.get_running_loop()

        # Fast path: if we already have a valid instance, return it without acquiring the lock
        needs_reinit = (self._instance is not None and self._loop is not current_loop) or self._auth_is_dirty
        if self._instance is not None and not needs_reinit:
            return self._instance

        # Slow path: acquire lock for initialization or re-initialization
        async with self._init_lock:
            # Double-check after acquiring lock (another task may have initialized while we waited)
            needs_reinit = (self._instance is not None and self._loop is not current_loop) or self._auth_is_dirty

            # If we have an instance but it's from a different loop or auth changed, close it
            if needs_reinit:
                await self.close_instance()
                self._s3_session = None

            if self._instance is None:
                session = await self.session()
                self._loop = current_loop  # Update the loop reference

                # https://botocore.amazonaws.com/v1/documentation/api/latest/reference/config.html
                WRAPP_S3_CONNECT_TIMEOUT_S = float(os.getenv("WRAPP_S3_CONNECT_TIMEOUT_S", "5.0"))
                # Configure connection pool size
                WRAPP_S3_CONCURRENT_CONNECTIONS = int(os.getenv("WRAPP_S3_CONCURRENT_CONNECTIONS", "128"))
                # Check if S3 Transfer Acceleration should be enabled
                WRAPP_ENABLE_S3_ACCELERATION = os.getenv("WRAPP_ENABLE_S3_ACCELERATION", "False").lower() in ["true", "1", "yes"]

                extra_config_args = {
                    "connect_timeout": WRAPP_S3_CONNECT_TIMEOUT_S,
                    "max_pool_connections": WRAPP_S3_CONCURRENT_CONNECTIONS,
                }

                # Only enable acceleration if explicitly requested via environment variable
                if WRAPP_ENABLE_S3_ACCELERATION:
                    extra_config_args["s3"] = {"use_accelerate_endpoint": True}
                    logger.info("AWS S3 Transfer Acceleration is enabled")

                extra_config = botocore.config.Config(**extra_config_args)

                # Store the context manager so we can properly exit it later
                self._client_context_manager = session.client(
                    "s3", region_name=self._aws_region, endpoint_url=self._s3_endpoint, config=extra_config
                )

                self._instance = await self._client_context_manager.__aenter__()

        return self._instance

    async def close_instance(self):
        """Close the S3 client instance properly.

        Note: This method is typically called while holding _init_lock from get_instance().
        When called from S3Storage.shutdown(), it should be safe since shutdown is expected
        to be called when no other operations are in progress.

        shutdown_asyncgens() cannot deadlock on _init_lock here: the only async generators
        that use the S3 client (list_directory, list_file_tree) capture the client instance
        at the start and never re-enter get_instance() during finalization.
        """
        assert_is_wrapp_thread()
        if self._instance is not None and self._client_context_manager is not None:
            try:
                loop = asyncio.get_running_loop()
                if self._loop == loop:
                    try:
                        await asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("Timed out waiting for async generators to shut down; proceeding with S3 client cleanup")
                    except Exception as e:
                        logger.warning(f"Error during async generator shutdown: {e}")
                instance = self._instance
                context_manager = self._client_context_manager
                self._instance = None
                self._client_context_manager = None
                await context_manager.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"Error closing S3 client: {e}")


class S3Storage(StorageInterface):
    @classmethod
    def version_info(cls) -> str:
        return f"S3 Storage Backend"

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_params: ThrottlingParameters):
        self._s3_client: S3ClientWrapper | None = None
        self._auth_info: Optional[StorageAuthInfo] = None
        self._last_connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
        # Initialize upload config from environment variables once
        self._multipart_threshold = int(os.getenv("WRAPP_S3_MULTIPART_THRESHOLD", str(5 * 1024 * 1024)))
        self._multipart_chunksize = int(os.getenv("WRAPP_S3_MULTIPART_CHUNKSIZE", str(5 * 1024 * 1024)))
        self._max_concurrency = int(os.getenv("WRAPP_S3_MAX_CONCURRENCY", "50"))
        self._retry_count = int(os.getenv("WRAPP_S3_EXTRA_RETRIES", "3"))
        self._retry_delay = float(os.getenv("WRAPP_S3_RETRY_WAIT_SECONDS", "1.0"))
        # Configure boto3 logging - overwrite the debug level unless it has been overwritten already explicitly.
        boto3_log_level_name = os.getenv("WRAPP_S3_LOG_LEVEL", "NOTSET")
        try:
            boto3_log_level = getattr(logging, boto3_log_level_name.upper())
        except AttributeError:
            logger.warning(f"Invalid log level '{boto3_log_level_name}', ignoring WRAPP_S3_LOG_LEVEL")
            boto3_log_level = logging.NOTSET
        for name in ("boto3", "botocore", "aiobotocore"):
            boto_logger = logging.getLogger(name)
            if boto_logger.level == logging.NOTSET:
                if boto3_log_level == logging.NOTSET:
                    if boto_logger.isEnabledFor(logging.DEBUG):
                        boto_logger.setLevel(logging.INFO)
                else:
                    boto_logger.setLevel(boto3_log_level)
        # Use scheduling_params for throttler initialization
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "S3", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "S3", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "S3", throttling_params.jobs)

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
                logger.warning(f"Cannot update throttling parameters for S3: Throttler {t.server_backend_name} has open tasks.")
                return False
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "S3", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "S3", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "S3", throttling_params.jobs)
        return True

    def s3_client(self):
        # Make this a lazy creation, so the user has time to set up the authentication credentials
        if not self._s3_client:
            self._s3_client = S3ClientWrapper(self._auth_info)
        return self._s3_client

    def supports_tags(self, url: str) -> bool:
        return False

    def directory_semantics(self) -> DirectorySemantics:
        return DirectorySemantics.PREFIX

    def supports_comments(self) -> bool:
        return True

    async def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._s3_client:
            await self._s3_client.close_instance()
            self._s3_client = None
            self._auth_info = None
            self._last_connection_status = ConnectionStatus.DISCONNECTED

    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        return Result.OK, S3ServerInfo()

    async def normalize(self, url: str) -> str:
        bucket_name, key, region = parse_s3_bucket_key(url)
        scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
        return str(urllib.parse.urlunsplit(("s3", bucket_name, path, query, fragment)))

    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        try:
            bucket_name, key, region = parse_s3_bucket_key(url)
            client = await self.s3_client().get_instance()

            if key.endswith("/") or key == "":
                file_result = None
                file_path = None
                folder_result = await self._throttler_roundtrip.run(client.list_objects_v2(Bucket=bucket_name, Prefix=key, MaxKeys=1))
                folder_path = key
            else:
                file_path = key
                folder_path = key + "/"
                ops = [
                    self._throttler_roundtrip.run(client.head_object(Bucket=bucket_name, Key=key)),
                    self._throttler_roundtrip.run(client.list_objects_v2(Bucket=bucket_name, Prefix=folder_path, MaxKeys=1)),
                ]
                result_list: List[Dict[str, Any] | BaseException] = list(await asyncio.gather(*ops, return_exceptions=True))
                file_result = result_list[0]
                folder_result = result_list[1]

            if file_result is not None and not isinstance(file_result, BaseException):
                assert file_path is not None
                return Result.OK, S3ListEntry.file_from_head_object(key=file_path, data=file_result)
            if folder_result and not isinstance(folder_result, BaseException):
                if "Contents" in folder_result and len(folder_result["Contents"]) > 0:
                    return Result.OK, S3ListEntry.folder(key=folder_path)
                else:
                    folder_result = None
            if isinstance(file_result, BaseException):
                raise file_result
            elif isinstance(folder_result, BaseException):
                raise folder_result
            else:
                # Can happen if a folder is not found - then list_objects returns nothing
                return Result.ERROR_NOT_FOUND, None
        except NoCredentialsError as e:
            logger.error(f"S3 stat {url} failed because the S3 credentials could not be found: {e}")
            return Result.ERROR_ACCESS_DENIED, None
        except (BotoCoreError, ClientError) as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                return Result.ERROR_NOT_FOUND, None
            logger.error(f"boto3 error during stat {url}: {e}")
            return Result.ERROR, None

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, ResourceIdentity]:
        raise NotImplementedError("Resource identities not supported for S3 yet")

    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        result, item = await self.stat(url)
        return result, item.is_folder if item else False

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        try:
            bucket_name, prefix, region = parse_s3_bucket_key(url)
            if not prefix.endswith("/") and prefix != "":
                prefix += "/"
            empty_list = True

            # Capture the client once upfront so that if this generator is abandoned
            # and finalized by shutdown_asyncgens(), the finalization does not re-enter
            # get_instance() (which may be holding _init_lock).
            client = await self.s3_client().get_instance()
            paginator = client.get_paginator("list_objects_v2")
            operation_parameters = {"Bucket": bucket_name, "Prefix": prefix, "Delimiter": "/"}

            async for page in self._throttler_roundtrip.iterate(paginator.paginate(**operation_parameters)):
                if "Contents" in page:
                    for file_data in page["Contents"]:
                        if not file_data["Key"].endswith("/"):
                            empty_list = False
                            yield S3ListEntry.file_from_list_objects(prefix, file_data)
                if "CommonPrefixes" in page:
                    for folder_data in page["CommonPrefixes"]:
                        empty_list = False
                        yield S3ListEntry.folder(folder_data["Prefix"], prefix)

            if empty_list:
                # If there is a file in the same place, return ERROR_WRONG_TYPE
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    raise StorageOperationError(f"Trying to list a file {url}", Result.ERROR_WRONG_TYPE)
        except (BotoCoreError, ClientError) as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                raise StorageOperationError(f"aioboto3 returned {e}", Result.ERROR_NOT_FOUND)
            raise StorageOperationError(f"aioboto3 returned {e}", Result.ERROR)

    def supports_list_file_tree(self) -> bool:
        return True

    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        try:
            bucket_name, prefix, region = parse_s3_bucket_key(url)
            if not prefix.endswith("/") and prefix != "":
                prefix += "/"

            # Capture the client once upfront -- see comment in list_directory().
            client = await self.s3_client().get_instance()
            paginator = client.get_paginator("list_objects_v2")
            operation_parameters = {"Bucket": bucket_name, "Prefix": prefix}

            num_components_in_prefix = len(prefix.rstrip("/").split("/"))
            found_folders: Set[str] = set()

            throttled_page_iterator = self._throttler_roundtrip.iterate(paginator.paginate(**operation_parameters))
            async for page in throttled_page_iterator:
                if "Contents" in page:
                    for file_data in page["Contents"]:
                        path = file_data["Key"]
                        assert path.startswith(prefix)
                        if path.endswith("/"):
                            path_components = path.rstrip("/").split("/")[num_components_in_prefix:]
                        else:
                            path_components = path.split("/")[num_components_in_prefix:-1]
                        folder_path = prefix
                        for parent_folder in path_components:
                            folder_path += parent_folder + "/"
                            if folder_path not in found_folders:
                                found_folders.add(folder_path)
                                yield S3ListEntry.folder(folder_path, prefix)
                        if not path.endswith("/"):  # ignore the actual folder objects
                            yield S3ListEntry.file_from_list_objects(prefix, file_data)
        except (BotoCoreError, ClientError) as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                raise StorageOperationError(f"aioboto3 returned {e}", Result.ERROR_NOT_FOUND)
            raise StorageOperationError(f"aioboto3 returned {e}", Result.ERROR)

    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        return Result.ERROR_NOT_SUPPORTED, []

    async def copy(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        if not is_s3_url(src_url):
            raise ValueError(f"'{src_url} is not supported by the S3 backend.")
        if not is_s3_url(dst_url):
            raise ValueError(f"'{dst_url} is not supported by the S3 backend.")

        return await self._copy_file_s3_s3(src_url, dst_url, overwrite)

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        return False

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        raise NotImplementedError()

    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        try:
            bucket_name, key, region = parse_s3_bucket_key(file_url)
            if key.endswith("/"):
                return Result.ERROR_WRONG_TYPE, bytes()

            client = await self.s3_client().get_instance()
            response = await self._throttler_roundtrip.run(client.get_object(Bucket=bucket_name, Key=key))
            async with response["Body"] as stream:
                file_content = await self._throttler_networkdata.run(stream.read())
            return Result.OK, file_content
        except (BotoCoreError, ClientError) as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                return Result.ERROR_NOT_FOUND, bytes()
            logger.error(f"aioboto3 error during read {file_url}: {e}")
            return Result.ERROR, bytes()

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        raise NotImplementedError()

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        try:
            bucket_name, key, region = parse_s3_bucket_key(file_url)
            if key.endswith("/"):
                return Result.ERROR_WRONG_TYPE  # cannot make files ending with a '/'
            # TODO This needs to be multithreaded and use the hash cache. See OMPE-14844
            nucleus_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(content))
            return await self._upload(content, len(content), nucleus_hash, file_url, True)
        except (BotoCoreError, ClientError) as e:
            logger.error(f"aioboto3 error during write {file_url}: {e}")
            return Result.ERROR

    async def delete_item(self, url: str) -> Result:
        try:
            bucket_name, key, region = parse_s3_bucket_key(url)
            client = await self.s3_client().get_instance()
            response = await self._throttler_roundtrip.run(client.delete_object(Bucket=bucket_name, Key=key))
            if "Errors" in response:
                logger.error(f"Failed to delete object {response['Errors']}")
                return Result.ERROR
            return Result.OK
        except (BotoCoreError, ClientError) as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                return Result.ERROR_NOT_FOUND
            logger.error(f"aioboto3 error during delete_object {url}: {e}")
            return Result.ERROR

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        try:
            bucket_name, key, region = parse_s3_bucket_key(url)

            client = await self.s3_client().get_instance()
            paginator = client.get_paginator("list_objects_v2")

            throttled_paginator = self._throttler_roundtrip.iterate(paginator.paginate(Bucket=bucket_name, Prefix=key))

            objects_to_delete = []
            async for page in throttled_paginator:
                if "Contents" in page:
                    for obj in page["Contents"]:
                        objects_to_delete.append({"Key": obj["Key"]})

            if objects_to_delete:
                not_deleted_objects: List[Any] = []

                async def _delete_objects(batch: list[Any]):
                    response = await self._throttler_roundtrip.run(client.delete_objects(Bucket=bucket_name, Delete={"Objects": batch}))
                    if "Errors" in response:
                        not_deleted_objects.extend(response["Errors"])

                # Batch delete objects (Max 1000 objects per delete_objects call)
                async with task_node.create_task_group_node() as sub_task_node:
                    for i in range(0, len(objects_to_delete), 1000):
                        batch = objects_to_delete[i : i + 1000]
                        sub_task_node.create_task(_delete_objects(batch), name=f"delete_objects {i} to {i+1000}")
                if not_deleted_objects:
                    logger.error(f"Failed to delete objects. The first few of {len(not_deleted_objects)} errors: {not_deleted_objects[:3]}")
                    return Result.ERROR
                return Result.OK
            return Result.ERROR_NOT_FOUND
        except (BotoCoreError, ClientError) as e:
            if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                return Result.ERROR_NOT_FOUND
            logger.error(f"aioboto3 error during delete_objects {url}: {e}")
            return Result.ERROR

    def can_be_cached(self, url: str) -> bool:
        return False

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        if not auth_info:
            if self._auth_info:
                logger.info(f"Clearing auth information for boto3 for host {self._auth_info.server_url}")
            self._auth_info = None
        else:
            if len(auth_info) > 1:
                logger.error(
                    f"S3 only supports one set of credentials. Only using credentials for {auth_info[0].server_url}, ignore other credentials."
                )
            if not isinstance(auth_info[0].auth_data, Boto3AuthInfo):
                raise StorageOperationError(f"Unexpected type of credentials for S3 storage for {auth_info[0].server_url}.")
            logger.info(f"Setting auth information for boto3 for host {auth_info[0].server_url}")
            self._auth_info = auth_info[0]
        if self._s3_client:
            self._s3_client.set_new_auth_info(self._auth_info)

    def add_auth_info(self, auth_info: StorageAuthInfo):
        if not isinstance(auth_info.auth_data, Boto3AuthInfo):
            raise StorageOperationError(
                f"Incompatible authentication information for server {auth_info.server_url}: expected Boto3AuthInfo, got {auth_info.auth_data.__class__}"
            )
        if self._auth_info and self._auth_info != auth_info:
            logger.error(
                f"S3 only supports one set of credentials. Overwriting credentials for {self._auth_info.server_url} with new credentials for {auth_info.server_url}."
            )
        self.set_auth_info([auth_info])

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        try:
            auth_params = auth_param.split(",")
            server_url = auth_params[0]
            aws_access_key_id = auth_params[1]
            aws_secret_access_key = auth_params[2]
            aws_session_token = auth_params[3] if len(auth_params) > 3 else None
        except IndexError:
            raise StorageOperationError(
                f"Authentication parameter for S3 storage via boto3 needs to be of the form server_url, aws_access_key_id, aws_secret_access_key[, aws_session_token]!"
            )
        return StorageAuthInfo.boto3(
            server_url=server_url,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key,
            aws_session_token=aws_session_token,
        )

    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        return self._last_connection_status

    # S3 specific functions
    async def create_bucket(self, bucket_name: str) -> bool:
        bucket_created: bool = False
        if self.s3_client().endpoint():
            # Only try to create a new test bucket on minio
            # TODO - I think this is mistaken, we could specify an endpoint via env variables which is not
            # minio?
            try:
                client = await self.s3_client().get_instance()
                await self._throttler_roundtrip.run(client.create_bucket(Bucket=bucket_name))
                logger.info(f"Bucket '{bucket_name}' created successfully.")
                bucket_created = True
            except ClientError as e:
                try:
                    error_code = e.response["Error"]["Code"]
                    if error_code == "BucketAlreadyExists" or error_code == "BucketAlreadyOwnedByYou":
                        logger.info(f"Bucket '{bucket_name}' already exists. Reusing the same bucket.")
                        return False
                except Exception:
                    pass
                logger.error(f"Error creating bucket '{bucket_name}': {e}")
                raise e

        try:
            logger.info(f"Checking if bucket '{bucket_name}' exists....")
            client = await self.s3_client().get_instance()
            await self._throttler_roundtrip.run(client.head_bucket(Bucket=bucket_name))
            return bucket_created
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code == "404":
                logger.info(
                    f"Bucket '{bucket_name}' does not exist on S3 for the given credentials, please make manually sure the bucket exists."
                )
            elif error_code == "403":
                print(f"Access to bucket {bucket_name} on S3 is forbidden with the given credentials.")
            raise e

    async def _copy_file_s3_s3(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        try:
            src_bucket_name, src_key, src_region = parse_s3_bucket_key(src_url)
            dst_bucket_name, dst_key, dst_region = parse_s3_bucket_key(dst_url)
        except ValueError:
            raise NotImplementedError(f"Can only copy from S3 to S3. {src_url=} {dst_url=}")
        try:
            # TODO: this is problematic - this should be an atomic operation.
            # If the file does not exist, it might still come into existence
            # before we start the upload.
            stat_result, _ = await self.stat(dst_url)
            if stat_result != Result.OK and stat_result != Result.ERROR_NOT_FOUND:
                return stat_result
            if stat_result == Result.OK and not overwrite:
                return Result.ERROR_ALREADY_EXISTS
            copy_source = {"Bucket": src_bucket_name, "Key": src_key}

            client = await self.s3_client().get_instance()
            try:
                # This will fail for files >5GB. We do not want to use "copy" directly here: it does
                # a head_object first to determine if the file is greater than 5GB and a multi-part upload
                # is required, or if the normal copy_object can be used. Obviously we would like to avoid
                # an additional head_object for any small object copy.
                await self._throttler_serverdata.run(client.copy_object(CopySource=copy_source, Bucket=dst_bucket_name, Key=dst_key))
            except ClientError as e:
                if e.response["Error"]["Code"] != "InvalidRequest":
                    raise
                # This might be a failure because the file is greater than 5GB
                r, e = await self.stat(src_url)

                five_gb_in_bytes = 5368709120
                if r != Result.OK or e.size <= five_gb_in_bytes:
                    raise
                # Try again with s3transfer's copy
                await self._throttler_serverdata.run(client.copy(CopySource=copy_source, Bucket=dst_bucket_name, Key=dst_key))
            return Result.OK
        except (BotoCoreError, ClientError) as e:
            logger.error(f"aioboto3 error during copy {src_url} {dst_url}: {e}")
            return Result.ERROR

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        src_bucket_name, src_key, src_region = parse_s3_bucket_key(src_url)
        try:
            # make sure any parent directories exist
            async def _throttled_download():
                dst_path = get_path_from_url(dst_url)
                dst_dir = os.path.dirname(dst_path)
                os.makedirs(dst_dir, exist_ok=True)
                with open(dst_path, "wb" if overwrite else "xb") as f_dst:
                    try:
                        client = await self.s3_client().get_instance()
                        await client.download_fileobj(src_bucket_name, src_key, f_dst)
                        return Result.OK
                    except (BotoCoreError, ClientError) as e:
                        if isinstance(e, ClientError) and e.response["Error"]["Code"] == "404":
                            return Result.ERROR_NOT_FOUND
                        logger.error(f"aioboto3 error during download from {src_url} to {dst_url}: {e}")
                        return Result.ERROR

            return await self._throttler_networkdata.run(_throttled_download())
        except FileExistsError:
            return Result.ERROR_ALREADY_EXISTS
        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"Downloading error, access denied to file {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        except BaseException as e:
            logger.error(f"Error while downloading file {src_url} to {dst_url}: {e}")
            return Result.ERROR

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        try:
            # TODO: We really should have a hash cache for all local files. See OMPE-14844
            src_path = get_path_from_url(src_url)
            with open(src_path, "rb") as f_src:
                nucleus_hash, _ = calc_nucleus_hash_from_stream(f_src)
            upload_size = os.path.getsize(src_path)
            return await self._upload(src_path, upload_size, nucleus_hash, dst_url, overwrite)
        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"Upload file, permission error {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        except IOError as e:
            logger.error(f"Upload file, IO error {src_url}: {e}")
            return Result.ERROR

    async def _upload_fileobj(
        self,
        src_fileobj: BinaryIO,
        upload_size: int,
        nucleus_hash: str,
        dst_url: str,
        overwrite: bool,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Result:
        """Upload a file-like object to S3. Backwards-compatible wrapper for _upload."""
        return await self._upload(src_fileobj, upload_size, nucleus_hash, dst_url, overwrite, progress_callback)

    async def _upload(
        self,
        src: Union[str, bytes, BinaryIO],
        upload_size: int,
        nucleus_hash: str,
        dst_url: str,
        overwrite: bool,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> Result:
        """Upload data to S3.

        Args:
            src: Source data - can be a file path (str), bytes, or a file-like object (BinaryIO).
                 Using a file path is preferred as it avoids keeping file handles open during throttling.
            upload_size: Size of the data to upload in bytes.
            nucleus_hash: The nucleus hash of the content.
            dst_url: Destination S3 URL.
            overwrite: Whether to overwrite existing files.
            progress_callback: Optional callback for progress updates.

        Returns:
            Result indicating success or failure.

        Raises:
            FileNotFoundError: If src is a file path and the file doesn't exist.
        """
        dst_bucket_name, dst_key, dst_region = parse_s3_bucket_key(dst_url)

        # Determine the upload mode based on src type
        if isinstance(src, str):
            # src is a file path - will use client.upload_file
            use_file_path = True
            file_path = src
            file_obj: Optional[BinaryIO] = None
        elif isinstance(src, bytes):
            # src is bytes - wrap in BytesIO and use client.upload_fileobj
            use_file_path = False
            file_path = None
            file_obj = io.BytesIO(src)
        else:
            # src is a file-like object - use client.upload_fileobj
            use_file_path = False
            file_path = None
            file_obj = src

        # TODO: this is problematic - this should be an atomic operation.
        # If the file does not exist, it might still come into existence
        # before we start the upload.
        stat_result, _ = await self.stat(dst_url)
        if stat_result != Result.OK and stat_result != Result.ERROR_NOT_FOUND:
            return stat_result
        if stat_result == Result.OK and not overwrite:
            return Result.ERROR_ALREADY_EXISTS

        client = await self.s3_client().get_instance()

        max_attempts = self._retry_count
        base_delay = self._retry_delay

        extra_args = {"Metadata": {NUCLEUS_HASH_METADATA_KEY: nucleus_hash}}
        transfer_config = boto3.s3.transfer.TransferConfig(
            multipart_threshold=self._multipart_threshold,
            multipart_chunksize=self._multipart_chunksize,
            max_concurrency=self._max_concurrency,
        )

        needed_retry = False
        for attempt in range(1, max_attempts + 1):
            try:
                # If retry needed, stat the object and delete it if it has 0 bytes length
                if needed_retry:
                    stat_result, stat_entry = await self.stat(dst_url)
                    if stat_result == Result.ERROR_NOT_FOUND:
                        # That's ok, the prior tries did not leave a stale file
                        pass
                    elif stat_result == Result.ERROR_ACCESS_DENIED:
                        # That's probably a permanent error, we should not hit this object with more retries
                        raise StorageOperationError(f"Access denied on write destination: {dst_url}")
                    elif stat_result == Result.ERROR:
                        # Unknown error during stat. Let's log a warning and keep going, we do not know if it is permanent
                        logger.warning(
                            f"Got error checking write destination {dst_url} before retry, might not be permanent, retrying: {stat_result}"
                        )
                    elif stat_result == Result.OK:
                        if upload_size > 0:
                            if stat_entry and stat_entry.size == 0:
                                logger.warning(f"Removing 0 byte file before retrying upload to {dst_url}")
                                await self._throttler_roundtrip.run(client.delete_object(Bucket=dst_bucket_name, Key=dst_key))
                            else:
                                # There is a non-zero byte file at the destination address. It might be ok, like a file we want to overwrite, or it could be
                                # left over from the previous upload attempt. Don't touch it, we're trying to overwrite it.
                                # This will potentially leave a broken version from a previous attempt in the version history, but that is ok for now.
                                pass
                    else:
                        logger.warning(f"Program error, got unexpected return code from stat() operation on {dst_url}")

                if use_file_path:
                    # Use upload_file for file paths - boto3 handles opening/closing the file.
                    # If the calling functions would also use a stream at this point, we would have to keep the file open during throttling.
                    await self._throttler_networkdata.run(
                        client.upload_file(
                            file_path,
                            dst_bucket_name,
                            dst_key,
                            ExtraArgs=extra_args,
                            Callback=progress_callback,
                            Config=transfer_config,
                        )
                    )
                else:
                    # Use upload_fileobj for bytes or file-like objects
                    # Rewind before every upload attempt
                    assert file_obj is not None
                    file_obj.seek(0)
                    await self._throttler_networkdata.run(
                        client.upload_fileobj(
                            file_obj,
                            dst_bucket_name,
                            dst_key,
                            ExtraArgs=extra_args,
                            Callback=progress_callback,
                            Config=transfer_config,
                        )
                    )

                if needed_retry:
                    # Check if that upload really worked and did not leave a 0 byte file
                    stat_result, stat_entry = await self.stat(dst_url)
                    if stat_result != Result.OK:
                        raise StorageOperationError(f"Upload retry failed, stat on {dst_url} returned with {stat_result}")
                    elif stat_entry and stat_entry.size == 0 and upload_size > 0:
                        raise StorageOperationError(f"Upload retry failed, S3 left 0 byte file at {dst_url}")
                return Result.OK

            except (BotoCoreError, ClientError) as e:
                # If we've exhausted retries, break out
                if attempt == max_attempts:
                    logger.error(f"[{dst_url}] upload failed on attempt {attempt}/{max_attempts}: {e}. No more retries left.")
                    break

                # compute exponential backoff + jitter
                delay = base_delay * (2 ** (attempt - 1))
                jitter = uniform(0, delay * 0.1)
                total_sleep = delay + jitter

                logger.warning(f"[{dst_url}] upload attempt {attempt}/{max_attempts} failed: {e}. Retrying in {total_sleep:.1f}s…")
                await asyncio.sleep(total_sleep)
                needed_retry = True

        return Result.ERROR

    # Update connection status based on exceptions
    def _update_connection_status_from_exception(self, e: Exception) -> None:
        if isinstance(
            e,
            (
                UnknownSignatureVersionError,
                CredentialRetrievalError,
                TokenRetrievalError,
                NoAuthTokenError,
                NoCredentialsError,
                PartialCredentialsError,
            ),
        ):
            logger.error(f"S3 call failed with authentication exception {e}")
            self._last_connection_status = ConnectionStatus.AUTH_ERROR
        elif isinstance(
            e,
            (
                BaseEndpointResolverError,
                UnknownServiceError,
                UnknownRegionError,
                botocore.exceptions.ConnectionError,
                ConnectionClosedError,
            ),
        ):
            logger.error(f"S3 call failed with connection exception {e}")
            self._last_connection_status = ConnectionStatus.CONNECT_ERROR
        else:
            logger.error(f"S3 call failed with exception {e}")
