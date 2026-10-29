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
import base64
import binascii
import io
import logging
import os
import random
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)

import pytest
import pytest_asyncio

from tests.common import run_wrapp_in_separate_process
from wrapp import ThrottlingParameters
from wrapp.utils.coro import force_wrapp_thread
from wrapp.utils.router import g_aws_enabled
from wrapp.utils.storage import (
    Router,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    StorageMissingDependenciesError,
    StorageOperationError,
)

from .storage_test_helpers import StorageBackendForTests

try:
    import aiohttp
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
    )

    from wrapp.utils.storage_impls.s3_helper import parse_s3_bucket_key
    from wrapp.utils.storage_impls.s3storage import (
        S3ClientWrapper,
        S3ListEntry,
        S3Storage,
        _s3_identity_request_args,
    )
except (StorageMissingDependenciesError, ModuleNotFoundError) as e:
    pytest.skip(f"Missing dependencies for S3Storage: {e}", allow_module_level=True)


def _recording_sleep():
    """Return an (async sleep callable, delays list) pair for injecting into S3Storage.

    The callable records each requested backoff delay and returns immediately (no real
    wait, no yield). S3Storage takes it as its injected backoff sleep, so only genuine
    backoff sleeps land in the list; the download mock's own asyncio.sleep(0) yields go
    straight to the event loop.
    """
    delays: list = []

    async def sleep(delay):
        delays.append(delay)

    return sleep, delays


@pytest.mark.parametrize(
    "query",
    [
        "versionId=",
        "versionId",
        "etag=",
        "etag",
        "versionId=&etag=valid",
        "versionId=valid&etag=",
        "versionId=&versionId=valid",
        "versionId=valid&versionId=",
        "etag=&etag=valid",
        "etag=valid&etag=",
    ],
)
def test_s3_identity_parser_rejects_blank_selectors(query):
    with pytest.raises(ValueError, match="must not be empty"):
        _s3_identity_request_args(f"s3://test-bucket/file.txt?{query}")


@pytest.mark.parametrize(
    "query,expected",
    [
        ("", {}),
        ("unrelated=", {}),
        ("versionId=null", {"VersionId": "null"}),
        ("etag=%22abc123%22", {"IfMatch": '"abc123"'}),
        ("versionId=first&versionId=last&etag=old&etag=new", {"VersionId": "last", "IfMatch": "new"}),
    ],
)
def test_s3_identity_parser_preserves_valid_selector_semantics(query, expected):
    assert _s3_identity_request_args(f"s3://test-bucket/file.txt?{query}") == expected


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize(
    "query", ["versionId=", "versionId", "etag=", "etag", "versionId=valid&etag=", "versionId=&versionId=valid", "etag=valid&etag="]
)
async def test_s3_blank_identity_never_reaches_sdk(query, tmp_path):
    client = AsyncMock()
    provider = _FakeClientProvider(client)
    storage = S3Storage("test-version", False, ThrottlingParameters(), client_provider=provider)
    identity = f"s3://test-bucket/file.txt?{query}"
    destination = tmp_path / "downloaded.txt"

    result, item = await storage.stat(identity)
    assert result == Result.ERROR_NOT_FOUND and item is None
    result, content = await storage.read_resource_identity(identity)
    assert result == Result.ERROR_NOT_FOUND and content == b""
    result = await storage.download_resource_identity(identity, str(destination), overwrite=True)
    assert result == Result.ERROR_NOT_FOUND and not destination.exists()
    result = await storage.copy_resource_identity(identity, "s3://destination-bucket/file.txt", overwrite=True)
    assert result == Result.ERROR_NOT_FOUND
    result, local_file = await storage.materialize("s3://test-bucket/current.txt", str(destination), resource_identity=identity)
    assert result == Result.ERROR_NOT_FOUND and local_file is None
    assert not destination.exists()
    assert client.mock_calls == []


class _FakeClientProvider:
    """Strict stand-in for S3ClientWrapper (satisfies the S3ClientProvider Protocol).

    Returns the injected fake client from get_instance; close_instance and
    set_new_auth_info are no-ops. Injected into S3Storage via client_provider= so the
    lazy s3_client()/close/auth paths use it without patching get_instance.
    """

    def __init__(self, client):
        self._client = client

    async def get_instance(self):
        return self._client

    async def close_instance(self):
        return None

    def set_new_auth_info(self, auth_info):
        return None


class _FakeS3Client:
    """Strict fake for the aioboto3 client surface the read/download paths call.

    Exposes exactly get_object, head_object and list_objects_v2 as async methods. It is a
    plain class (NOT MagicMock) with no __getattr__, so any other attribute access raises
    AttributeError naturally: an unstubbed client call fails loudly instead of silently
    returning a fabricated mock. head_object/list_objects_v2 track their own call counts so
    the download tests can assert on them.
    """

    def __init__(self, *, get_object=None, head_object_result=None, head_exception=None, list_objects_v2_result=None):
        self.get_object_func = get_object
        self._head_object_result = head_object_result
        self._head_exception = head_exception
        self._list_objects_v2_result = {} if list_objects_v2_result is None else list_objects_v2_result
        self.head_object_call_count = 0
        self.head_object_calls = []
        self.list_objects_v2_call_count = 0

    async def get_object(self, *args, **kwargs):
        assert self.get_object_func is not None, "get_object called but no get_object_func configured"
        return await self.get_object_func(*args, **kwargs)

    async def head_object(self, *args, **kwargs):
        self.head_object_call_count += 1
        self.head_object_calls.append((args, kwargs))
        if self._head_exception is not None:
            raise self._head_exception
        return self._head_object_result

    async def list_objects_v2(self, *args, **kwargs):
        self.list_objects_v2_call_count += 1
        return self._list_objects_v2_result


class _FakeUploadS3Client:
    """Strict fake for the client surface the upload-retry path calls.

    Exposes only upload_fileobj (delegating to the test's upload mock) and delete_object;
    any other attribute access raises AttributeError. delete_object stays an AsyncMock so
    the upload tests can introspect its call count and Bucket/Key kwargs.
    """

    def __init__(self, upload_fileobj_func):
        self.upload_fileobj = upload_fileobj_func
        self.delete_object = AsyncMock(return_value={})


def generate_random_bytes(size, seed=None):
    if seed is not None:
        random.seed(seed)
    else:
        random.seed()
    return bytes(random.choice([b for b in range(32)]) for _ in range(size))


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_calculated_checksum(test_folder_url, storage_backend):
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    if storage_backend.storage_type().name != "S3":
        pytest.skip("Test only for s3")

    storage = Router.me().s3()

    url = test_folder_url + "/test_s3_calculated_checksum.txt"
    bucket, key, region = parse_s3_bucket_key(url)

    # Upload some data with checksum
    data = generate_random_bytes(1024)
    # sha32_checksum = hashlib.crc32(data).digest()
    # base64_checksum = base64.b64encode(sha32_checksum).decode()

    # Get the S3 client instance
    client = await storage.s3_client().get_instance()

    # Put the object with checksum
    response = await client.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        # ChecksumSHA32=base64_checksum
        ChecksumAlgorithm="CRC32",
    )

    assert "ChecksumCRC32" in response
    assert response["ChecksumCRC32"] is not None
    uploaded_checksum = response["ChecksumCRC32"]

    # Calculate CRC32 checksum
    crc32_checksum = binascii.crc32(data) & 0xFFFFFFFF
    # Encode the checksum in base64
    base64_crc32_checksum = base64.b64encode(crc32_checksum.to_bytes(4, byteorder="big")).decode("utf-8")

    assert base64_crc32_checksum == uploaded_checksum

    # response = await s3_client.head_object(Bucket=path.bucket, Key=path.key, ChecksumMode='ChecksumCRC32')
    # response = await s3_client.get_object(Bucket=path.bucket, Key=path.key, ChecksumMode='ChecksumCRC32')
    # sha32_checksum = response['Checksum']['ChecksumCRC32']

    # Get object attributes
    response = await client.get_object_attributes(Bucket=bucket, Key=key, ObjectAttributes=["Checksum"])

    # Extract the CRC-32 checksum from the response
    sha32_checksum = response["Checksum"]["ChecksumCRC32"]
    print("CRC-32 Checksum:", sha32_checksum)

    assert sha32_checksum == uploaded_checksum


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_multipart_download_roundtrip(test_folder_url, storage_backend):
    """End-to-end multipart download against the live S3 API (MinIO in CI).

    Exercises the real chunked ranged-GET download path -- Range headers, If-Match on the quoted
    ETag, chunk reassembly -- rather than only the mock. A small threshold/chunksize forces a
    modest object to download as several chunks, so no multi-MiB transfer is needed. See
    OMPE-98951 note about the previously mock-only multipart path.
    """
    import tempfile

    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    if storage_backend.storage_type().name != "S3":
        pytest.skip("Test only for s3")

    # A fresh S3Storage with a small threshold/chunksize so the DOWNLOAD uses the chunked path
    # (Router.me().s3() keeps the 5 MiB default). It reads the same endpoint/credentials from the
    # environment, so it talks to the same backend as the fixtures.
    with patch.dict(os.environ, {"WRAPP_S3_MULTIPART_THRESHOLD": "4096", "WRAPP_S3_MULTIPART_CHUNKSIZE": "4096"}):
        storage = S3Storage("test-version", False, ThrottlingParameters())

    url = test_folder_url + "/test_multipart_download_roundtrip_OMPE_98951.bin"
    data = generate_random_bytes(10240)  # > 4096 threshold -> 3 chunks (4096 / 4096 / 2048)
    assert len(data) > storage._multipart_threshold
    bucket, key, region = parse_s3_bucket_key(url)

    try:
        # Seed the object with a single put_object -- a multipart UPLOAD at 4096-byte parts would be
        # rejected (S3 requires parts >= 5 MiB); only the download needs the small chunk size.
        client = await storage.s3_client().get_instance()
        await client.put_object(Bucket=bucket, Key=key, Body=data)
        with tempfile.TemporaryDirectory() as tmp:
            dst_path = os.path.join(tmp, "out.bin")
            if sys.platform == "win32":
                dst_url = f"file:///{dst_path.replace(os.sep, '/')}"
            else:
                dst_url = f"file://{dst_path}"

            result = await storage.download_file(url, dst_url, overwrite=True)
            assert result == Result.OK
            with open(dst_path, "rb") as f:
                assert f.read() == data
    finally:
        await storage.delete_item(url)
        await storage.shutdown()


@pytest.mark.parametrize(
    "url",
    [
        # This is an AWS global URL
        "s3://mybucket/a/little/path",
        # Legacy hostname path style
        "https://s3.amazonaws.com/mybucket/a/little/path",
        # Transfer Acceleration endpoints
        "https://mybucket.s3-accelerate.amazonaws.com/a/little/path",
        # Transfer Acceleration dualstack (IP6) endpoint
        "https://mybucket.s3-accelerate.dualstack.amazonaws.com/a/little/path",
    ],
)
def test_s3_url_format_without_region(url, wrapp_init):
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    assert Router.me().storage_for_read(url) == Router.me().s3()

    bucket, key, region = parse_s3_bucket_key(url)
    assert bucket == "mybucket"
    assert key == "a/little/path"
    assert region is None


@pytest.mark.parametrize(
    "url",
    [
        # This is called path style. Deprecated but still functional https://aws.amazon.com/blogs/aws/amazon-s3-path-deprecation-plan-the-rest-of-the-story/
        "https://s3.us-wild-2.amazonaws.com/mybucket/a/little/path",
        # This is called virtual host style
        "https://mybucket.s3.us-wild-2.amazonaws.com/a/little/path",
        # Virtual host style dualstack
        "https://mybucket.s3.dualstack.us-wild-2.amazonaws.com/a/little/path",
        # Legacy hostname with virtual-hosted-style
        "https://mybucket.us-wild-2.s3.amazonaws.com/a/little/path",
        # Dash style legacy outlier
        "https://s3-us-wild-2.amazonaws.com/mybucket/a/little/path",
    ],
)
def test_s3_url_formats_with_region(url, wrapp_init):
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    assert Router.me().storage_for_read(url) == Router.me().s3()

    bucket, key, region = parse_s3_bucket_key(url)
    assert bucket == "mybucket"
    assert key == "a/little/path"
    assert region == "us-wild-2"


@pytest.mark.parametrize(
    "url",
    [
        # This is an AWS global URL
        "s3://mybucket/",
        "s3://mybucket",
        # This is called path style. Deprecated but still functional https://aws.amazon.com/blogs/aws/amazon-s3-path-deprecation-plan-the-rest-of-the-story/
        "https://s3.us-wild-2.amazonaws.com/mybucket/",
        "https://s3.us-wild-2.amazonaws.com/mybucket",
        # This is called virtual host style
        "https://mybucket.s3.us-wild-2.amazonaws.com/",
        "https://mybucket.s3.us-wild-2.amazonaws.com",
        # Legacy hostname path style
        "https://s3.amazonaws.com/mybucket/",
        "https://s3.amazonaws.com/mybucket",
        # Legacy hostname with virtual-hosted-style
        "https://mybucket.us-wild-2.s3.amazonaws.com/",
        "https://mybucket.us-wild-2.s3.amazonaws.com",
        # Dash style legacy outlier
        "https://s3-us-wild-2.amazonaws.com/mybucket/",
        "https://s3-us-wild-2.amazonaws.com/mybucket",
    ],
)
def test_empty_s3_key(url, wrapp_init):
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    assert Router.me().storage_for_read(url) == Router.me().s3()

    bucket, key, region = parse_s3_bucket_key(url)
    assert bucket == "mybucket"
    assert key == ""
    assert region is None or region == "us-wild-2"


@pytest.mark.asyncio
async def test_s3_storage_exception_due_to_missing_profile():
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    # boto3.Session might raise an exceptions if the configuration is invalid. Make sure this is handled gracefully.
    run_wrapp_in_separate_process(["list-repo", "s3://thereisnosuchbucket/andnopath"], 2, env={"AWS_PROFILE": "ThisProfileDoesNotExist"})


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_client_wrapper_invalid_profile():
    """Test that S3ClientWrapper raises StorageOperationError when AWS_PROFILE points to non-existing profile"""
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    # Save original environment variable if it exists
    original_profile = os.environ.get("AWS_PROFILE")

    try:
        # Set AWS_PROFILE to a non-existing profile
        os.environ["AWS_PROFILE"] = "NonExistentProfile123456789"

        # Create S3ClientWrapper and attempt to create a session
        client = S3ClientWrapper(None)

        # The exception should be raised when we try to create a session
        with pytest.raises(StorageOperationError) as excinfo:
            await client.session()

        # Verify the error message contains useful information
        assert "Failed to initialize aioboto3 session" in str(excinfo.value)

    finally:
        await client.close_instance()
        # Restore original environment variable or remove it if it wasn't set
        if original_profile is not None:
            os.environ["AWS_PROFILE"] = original_profile
        else:
            os.environ.pop("AWS_PROFILE", None)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_storage_invalid_region():
    """Test that S3Storage raises StorageOperationError when AWS_REGION is invalid"""
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    # Save original environment variables if they exist
    original_region = os.environ.get("AWS_REGION")

    storage: S3Storage | None = None
    try:
        # Set AWS_REGION to an invalid region
        os.environ["AWS_REGION"] = "invalid-region-123"

        # Create S3Storage instance
        storage = S3Storage("test-version", False, ThrottlingParameters())

        # The exception should be raised when we try to use the storage
        with pytest.raises(StorageOperationError) as excinfo:
            # Since list_directory returns an async generator, we need to start iterating it
            # to trigger the exception
            async for _ in storage.list_directory("s3://test-bucket/test-path"):
                break

        # Verify the error message contains useful information
        assert "aioboto3 returned" in str(excinfo.value)

    finally:
        if storage:
            await storage.shutdown()
        # Restore original environment variable or remove it if it wasn't set
        if original_region is not None:
            os.environ["AWS_REGION"] = original_region
        else:
            os.environ.pop("AWS_REGION", None)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_storage_invalid_credentials():
    """Test that S3Storage raises StorageOperationError when AWS credentials are invalid"""
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    # Save original environment variables if they exist
    original_key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    original_secret = os.environ.get("AWS_SECRET_ACCESS_KEY")

    storage: S3Storage | None = None
    try:
        # Set invalid AWS credentials
        os.environ["AWS_ACCESS_KEY_ID"] = "INVALIDACCESSKEY123"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "InvalidSecretKey12345678901234567890"

        # Create S3Storage instance
        storage = S3Storage("test-version", False, ThrottlingParameters())

        # The exception should be raised when we try to use the storage
        with pytest.raises(StorageOperationError) as excinfo:
            # Since list_directory returns an async generator, we need to start iterating it
            # to trigger the exception
            async for _ in storage.list_directory("s3://test-bucket/test-path"):
                break

        # Verify the error message contains useful information
        assert "aioboto3 returned" in str(excinfo.value)

    finally:
        if storage:
            await storage.shutdown()
        # Restore original environment variables or remove them if they weren't set
        if original_key_id is not None:
            os.environ["AWS_ACCESS_KEY_ID"] = original_key_id
        else:
            os.environ.pop("AWS_ACCESS_KEY_ID", None)

        if original_secret is not None:
            os.environ["AWS_SECRET_ACCESS_KEY"] = original_secret
        else:
            os.environ.pop("AWS_SECRET_ACCESS_KEY", None)


@pytest.mark.asyncio
async def test_normalize(storage_backend):
    if not g_aws_enabled:
        pytest.skip("AWS is disabled")
    if not storage_backend.storage_type() == StorageBackendForTests.S3:
        pytest.skip("Test is only for S3 storage")
    storage = Router.me().s3()

    assert await storage.normalize("s3://bucketname/path/file") == "s3://bucketname/path/file"
    assert await storage.normalize("https://bucketname.s3.region.amazonaws.com/path/file") == "s3://bucketname/path/file"

    with pytest.raises(ValueError):
        assert await storage.normalize("https://account.azure.com/blob/path/file") == "s3://bucketname/path/file"


# ===== Retry Logic Tests =====


def _create_client_error(error_code="InternalError", message="Test error", http_status=None):
    """Helper function to create a ClientError for testing."""
    error_response = {"Error": {"Code": error_code, "Message": message}}
    if http_status is not None:
        error_response["ResponseMetadata"] = {"HTTPStatusCode": http_status}
    operation_name = "upload_fileobj"
    return ClientError(error_response, operation_name)


def _create_s3_entry(key: str, size: int) -> S3ListEntry:
    """Helper to create an S3ListEntry for testing."""
    return S3ListEntry.file_from_head_object(key, {"ContentLength": size, "LastModified": datetime.now(), "ETag": '"abc123"'})


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize("operation", ["stat", "read", "download_head", "download_get", "copy", "materialize_head", "materialize_get"])
@pytest.mark.parametrize("query", ["", "?etag=%22abc123%22", "?versionId=version-1", "?versionId=null"])
@pytest.mark.parametrize("error_code", ["AccessDenied", "403"])
async def test_s3_version_access_denied_names_required_permission(operation, query, error_code, tmp_path, caplog):
    denied = _create_client_error(error_code=error_code, message="Access denied", http_status=403)
    client = AsyncMock()
    client.head_object.return_value = {"ContentLength": 7, "LastModified": datetime.now(), "ETag": '"abc123"'}
    client.list_objects_v2.return_value = {}
    if operation in ("stat", "download_head", "materialize_head"):
        client.head_object.side_effect = denied
    elif operation in ("read", "download_get", "materialize_get"):
        client.get_object.side_effect = denied
    else:
        client.copy_object.side_effect = denied
    sleep, delays = _recording_sleep()
    storage = S3Storage("test-version", False, ThrottlingParameters(), client_provider=_FakeClientProvider(client), sleep=sleep)
    source = "s3://source-bucket/file.txt" + query
    destination = tmp_path / "out.txt"

    with caplog.at_level(logging.ERROR, logger="omni.wrapp"):
        if operation == "stat":
            result, entry = await storage.stat(source)
            assert entry is None
        elif operation == "read":
            result, content = await storage.read_resource_identity(source)
            assert content == b""
        elif operation.startswith("download"):
            result = await storage.download_resource_identity(source, destination.as_uri(), overwrite=True)
            assert not destination.exists()
        elif operation.startswith("materialize"):
            result, local_file = await storage.materialize("s3://source-bucket/current.txt", str(destination), resource_identity=source)
            assert local_file is None
            assert not destination.exists()
        else:
            with patch.object(storage, "stat", new_callable=AsyncMock, return_value=(Result.ERROR_NOT_FOUND, None)):
                result = await storage.copy_resource_identity(source, "s3://destination-bucket/file.txt", overwrite=True)

    assert result == (Result.ERROR if operation in ("stat", "copy") else Result.ERROR_ACCESS_DENIED)
    assert delays == []
    assert ("s3:GetObjectVersion" in caplog.text) == ("versionId=" in query)
    assert "Access denied" in caplog.text
    if operation in ("read", "download_get", "materialize_get"):
        assert client.get_object.await_count == 1
    if operation == "copy":
        assert client.copy_object.await_count == 1


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_make_resource_identity_keeps_source_and_uses_object_version_OMPE_58312():
    client = AsyncMock()
    client.head_object.return_value = {
        "ContentLength": 7,
        "LastModified": datetime.now(),
        "ETag": '"abc123"',
        "VersionId": "version-1",
        "Metadata": {"nv-omniverse-nucleus-hash": "sha-256-flat;1048576;hash"},
    }
    client.list_objects_v2.return_value = {}
    storage = S3Storage(
        "test-version",
        False,
        ThrottlingParameters(),
        client_provider=_FakeClientProvider(client),
    )
    listed_item = S3ListEntry.file_from_list_objects(
        "",
        {"Key": "file.txt", "Size": 7, "LastModified": datetime.now(), "ETag": '"abc123"'},
    )
    source_url = "s3://test-bucket/file.txt"

    absolute_url, relative_url, identity = await storage.make_resource_identity(source_url, listed_item, "file.txt")

    assert absolute_url == source_url
    assert relative_url == "file.txt"
    assert identity.to_string() == "s3://test-bucket/file.txt?versionId=version-1&etag=%22abc123%22"
    assert listed_item.nucleus_hash == "sha-256-flat;1048576;hash"
    client.head_object.assert_awaited_once()
    client.list_objects_v2.assert_not_awaited()


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize("version_id", [None, "version-1", "null"])
async def test_s3_identity_reuses_head_metadata_without_another_request(version_id):
    """A missing VersionId on a HEAD response also describes a complete observation."""
    client = AsyncMock()
    storage = S3Storage("test-version", False, ThrottlingParameters(), client_provider=_FakeClientProvider(client))
    metadata = {"ContentLength": 7, "LastModified": datetime.now(), "ETag": '"abc123"', "Metadata": {}}
    if version_id is not None:
        metadata["VersionId"] = version_id
    entry = S3ListEntry.file_from_head_object("file.txt", metadata)
    _, _, identity = await storage.make_resource_identity("s3://test-bucket/file.txt", entry, "file.txt")
    expected = "s3://test-bucket/file.txt?" + (f"versionId={version_id}&" if version_id is not None else "") + "etag=%22abc123%22"
    assert identity.to_string() == expected
    client.head_object.assert_not_awaited()
    client.list_objects_v2.assert_not_awaited()


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize("changed_field", ["size", "etag"])
async def test_s3_identity_rejects_object_changed_after_listing(changed_field):
    client = AsyncMock()
    client.head_object.return_value = {"ContentLength": 7, "LastModified": datetime.now(), "ETag": '"abc123"', "Metadata": {}}
    storage = S3Storage("test-version", False, ThrottlingParameters(), client_provider=_FakeClientProvider(client))
    listed = S3ListEntry.file_from_list_objects(
        "",
        {
            "Key": "file.txt",
            "Size": 8 if changed_field == "size" else 7,
            "LastModified": datetime.now(),
            "ETag": '"changed"' if changed_field == "etag" else '"abc123"',
        },
    )
    with pytest.raises(StorageOperationError, match="changed"):
        await storage.make_resource_identity("s3://test-bucket/file.txt", listed, "file.txt")
    client.head_object.assert_awaited_once()
    client.list_objects_v2.assert_not_awaited()


@pytest.mark.xdist_incompatible
@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_observation_recalculation_does_not_stat_twice(wrapp_init, monkeypatch):
    from wrapp.utils import storage as storage_utils
    from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
    from wrapp.utils.utils import get_path_from_url

    payload = b"payload"
    expected_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(payload))
    client = AsyncMock()
    client.head_object.return_value = {"ContentLength": len(payload), "LastModified": datetime.now(), "ETag": '"abc123"', "Metadata": {}}
    client.list_objects_v2.return_value = {}
    backend = S3Storage("test-version", False, ThrottlingParameters(), client_provider=_FakeClientProvider(client))
    monkeypatch.setattr(storage_utils, "wrapp_stat", backend.stat)
    monkeypatch.setattr(storage_utils, "wrapp_make_resource_identity", backend.make_resource_identity)

    async def download(identity, target, **kwargs):
        assert identity == "s3://test-bucket/file.txt?etag=%22abc123%22"
        Path(get_path_from_url(target)).write_bytes(payload)
        return Result.OK

    monkeypatch.setattr(storage_utils, "wrapp_download_resource_identity", download)
    observation = await storage_utils.wrapp_capture_file_observation("s3://test-bucket/file.txt", "file.txt", expected_hash=expected_hash)
    assert observation.hash == expected_hash
    assert observation.size == len(payload)
    client.head_object.assert_awaited_once()
    # The initial generic stat still determines file versus folder; minting
    # from its HEAD result must add neither a HEAD nor a folder-prefix LIST.
    client.list_objects_v2.assert_awaited_once()


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_stat_identity_miss_does_not_fall_back_to_folder_OMPE_58312():
    client = _FakeS3Client(
        head_exception=_create_client_error(error_code="PreconditionFailed", http_status=412),
        list_objects_v2_result={"Contents": [{"Key": "file.txt/child.txt"}]},
    )
    storage = S3Storage(
        "test-version",
        False,
        ThrottlingParameters(),
        client_provider=_FakeClientProvider(client),
    )

    result, item = await storage.stat("s3://test-bucket/file.txt?etag=%22stale-etag%22")

    assert result == Result.ERROR_NOT_FOUND
    assert item is None


def _create_upload_mock(fail_count: int, exception_type=None):
    """Create a mock upload function that fails N times then succeeds."""
    call_count = [0]
    exception = exception_type() if exception_type else _create_client_error()

    async def mock_upload(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] <= fail_count:
            raise exception
        return None

    return mock_upload, call_count


class _RetryTestContext:
    """Context manager for common retry test setup.

    Builds the S3Storage under test with an injected strict upload client and a recording
    sleep (no more patching of the global asyncio.sleep or of get_instance). Only stat and
    boto3's TransferConfig are still patched, since neither is part of the client seam.
    """

    def __init__(self, mock_upload_func):
        self.sleep_func, self.sleep_delays = _recording_sleep()
        self.mock_client = _FakeUploadS3Client(mock_upload_func)
        self.storage = S3Storage(
            "test-version",
            False,
            ThrottlingParameters(),
            client_provider=_FakeClientProvider(self.mock_client),
            sleep=self.sleep_func,
        )
        self.mock_stat = None
        self._patches = []

    def __enter__(self):
        # stat and TransferConfig are not part of the client seam, so they stay patched.
        self._patches = [
            patch.object(self.storage, "stat", new_callable=AsyncMock),
            patch("boto3.s3.transfer.TransferConfig"),
        ]
        self.mock_stat = self._patches[0].__enter__()
        self._patches[1].__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for p in reversed(self._patches):
            p.__exit__(exc_type, exc_val, exc_tb)
        return False


@pytest_asyncio.fixture
async def retry_test_setup():
    """Common setup for retry tests. The storage is built by _RetryTestContext so the
    strict client provider and recording sleep can be injected at construction time."""
    test_url = "s3://test-bucket/test-file.txt"
    test_data = b"test content"
    file_obj = io.BytesIO(test_data)
    nucleus_hash = "test_hash_123"

    yield {
        "test_url": test_url,
        "test_data": test_data,
        "file_obj": file_obj,
        "nucleus_hash": nucleus_hash,
    }
    # Note: shutdown() not called as we're mocking all S3 operations


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_single_failure(retry_test_setup):
    """Test successful upload after a single failure with exponential backoff."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await ctx.storage._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        assert call_count[0] == 2
        assert len(ctx.sleep_delays) == 1
        assert 1.0 <= ctx.sleep_delays[0] <= 1.1


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_multiple_failures(retry_test_setup):
    """Test successful upload after multiple failures with increasing backoff."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=2)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await ctx.storage._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        assert call_count[0] == 3
        assert len(ctx.sleep_delays) == 2
        sleep_times = ctx.sleep_delays
        assert 1.0 <= sleep_times[0] <= 1.1
        assert 2.0 <= sleep_times[1] <= 2.2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_max_retries_exhausted(retry_test_setup):
    """Test that upload fails after exhausting all retry attempts."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=3, exception_type=BotoCoreError)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
        ]

        result = await ctx.storage._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.ERROR
        assert call_count[0] == 3
        assert len(ctx.sleep_delays) == 2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_zero_byte_cleanup(retry_test_setup):
    """Test that 0-byte files are cleaned up before retry."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", 0)),  # 0-byte file
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await ctx.storage._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        assert ctx.mock_client.delete_object.call_count == 1
        assert ctx.mock_client.delete_object.call_args[1]["Bucket"] == "test-bucket"
        assert ctx.mock_client.delete_object.call_args[1]["Key"] == "test-file.txt"
        assert call_count[0] == 2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_nonzero_stale_file(retry_test_setup):
    """Test that non-zero byte stale files are NOT deleted during retry."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", 100)),  # Non-zero byte file
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await ctx.storage._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        assert ctx.mock_client.delete_object.call_count == 0
        assert call_count[0] == 2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_file_seeking(retry_test_setup):
    """Test that file is rewound (seek(0)) before each retry attempt."""
    setup = retry_test_setup

    # Track seeking behavior
    seek_calls = []
    original_seek = setup["file_obj"].seek

    def tracked_seek(position, whence=0):
        seek_calls.append((position, whence))
        return original_seek(position, whence)

    setup["file_obj"].seek = tracked_seek
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await ctx.storage._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        seek_to_zero_calls = [call for call in seek_calls if call[0] == 0]
        assert len(seek_to_zero_calls) == 2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_post_retry_verification_failure(retry_test_setup):
    """Test that post-retry verification catches 0-byte files after upload."""
    setup = retry_test_setup
    mock_upload, _ = _create_upload_mock(fail_count=1)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", 0)),  # 0-byte file after upload!
        ]

        with pytest.raises(StorageOperationError, match=r"Upload retry failed.*0 byte file"):
            await ctx.storage._upload_fileobj(setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_access_denied_permanent_error(retry_test_setup):
    """Test that ACCESS_DENIED during retry stat causes immediate failure."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_ACCESS_DENIED, None),  # Permanent error during retry cleanup
        ]

        with pytest.raises(StorageOperationError, match="Access denied"):
            await ctx.storage._upload_fileobj(setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True)

        assert len(ctx.sleep_delays) == 1
        assert call_count[0] == 1


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_custom_env_variables():
    """Test that WRAPP_S3_EXTRA_RETRIES and WRAPP_S3_RETRY_WAIT_SECONDS can be overridden."""
    # Save original environment variables
    original_retries = os.environ.get("WRAPP_S3_EXTRA_RETRIES")
    original_delay = os.environ.get("WRAPP_S3_RETRY_WAIT_SECONDS")

    try:
        # Set custom values: 5 retries, 0.5 second base delay
        os.environ["WRAPP_S3_EXTRA_RETRIES"] = "5"
        os.environ["WRAPP_S3_RETRY_WAIT_SECONDS"] = "0.5"

        # Storage is built inside _RetryTestContext, so the custom env vars set above are
        # picked up at its construction time.
        test_url = "s3://test-bucket/test-file.txt"
        test_data = b"test content"
        file_obj = io.BytesIO(test_data)
        nucleus_hash = "test_hash_123"

        # Mock upload to fail 4 times, succeed on 5th (within custom retry limit)
        mock_upload, call_count = _create_upload_mock(fail_count=4)

        with _RetryTestContext(mock_upload) as ctx:
            ctx.mock_stat.side_effect = [
                (Result.ERROR_NOT_FOUND, None),  # Initial check
                (Result.ERROR_NOT_FOUND, None),  # Retry 1 cleanup
                (Result.ERROR_NOT_FOUND, None),  # Retry 2 cleanup
                (Result.ERROR_NOT_FOUND, None),  # Retry 3 cleanup
                (Result.ERROR_NOT_FOUND, None),  # Retry 4 cleanup
                (Result.OK, _create_s3_entry("test-file.txt", len(test_data))),  # Post-retry verification
            ]

            result = await ctx.storage._upload_fileobj(file_obj, len(test_data), nucleus_hash, test_url, True)

            # Verify success with custom retry count
            assert result == Result.OK
            assert call_count[0] == 5

            # Verify custom delay is used with exponential backoff
            assert len(ctx.sleep_delays) == 4
            sleep_times = ctx.sleep_delays

            # With base_delay=0.5, exponential backoff should be: 0.5, 1.0, 2.0, 4.0 (+ jitter up to 10%)
            assert 0.5 <= sleep_times[0] <= 0.55  # 0.5 * 2^0 + jitter
            assert 1.0 <= sleep_times[1] <= 1.1  # 0.5 * 2^1 + jitter
            assert 2.0 <= sleep_times[2] <= 2.2  # 0.5 * 2^2 + jitter
            assert 4.0 <= sleep_times[3] <= 4.4  # 0.5 * 2^3 + jitter

    finally:
        # Restore original environment variables
        if original_retries is not None:
            os.environ["WRAPP_S3_EXTRA_RETRIES"] = original_retries
        else:
            os.environ.pop("WRAPP_S3_EXTRA_RETRIES", None)

        if original_delay is not None:
            os.environ["WRAPP_S3_RETRY_WAIT_SECONDS"] = original_delay
        else:
            os.environ.pop("WRAPP_S3_RETRY_WAIT_SECONDS", None)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_makes_one_attempt_with_retries_disabled_OMPE_98951(retry_test_setup):
    """With WRAPP_S3_EXTRA_RETRIES=0 the upload is still attempted exactly once.

    Regression guard: _upload used the raw retry count as the attempt budget, so 0 (a
    documented-valid value) made the retry loop run zero times and every upload silently
    returned Result.ERROR. Mirrors test_read_makes_one_attempt_with_retries_disabled_OMPE_98588.
    """
    setup = retry_test_setup
    original_retries = os.environ.get("WRAPP_S3_EXTRA_RETRIES")
    try:
        os.environ["WRAPP_S3_EXTRA_RETRIES"] = "0"
        mock_upload, call_count = _create_upload_mock(fail_count=0)

        # Storage is built inside _RetryTestContext, so it picks up the env var set above.
        with _RetryTestContext(mock_upload) as ctx:
            ctx.mock_stat.side_effect = [
                (Result.ERROR_NOT_FOUND, None),  # Initial existence check only; no retry stats
            ]

            result = await ctx.storage._upload_fileobj(
                setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
            )

        assert result == Result.OK
        assert call_count[0] == 1
        assert len(ctx.sleep_delays) == 0
    finally:
        if original_retries is not None:
            os.environ["WRAPP_S3_EXTRA_RETRIES"] = original_retries
        else:
            os.environ.pop("WRAPP_S3_EXTRA_RETRIES", None)


# ===== read_file_content Retry Logic Tests (OMPE-98588) =====


class _FakeStreamingBody:
    """Stand-in for the get_object response Body stream."""

    def __init__(self, data: bytes, read_exc: Exception | None = None):
        self._data = data
        self._read_exc = read_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        if self._read_exc is not None:
            raise self._read_exc
        return self._data


def _create_read_mock(fail_count: int, exception=None, fail_in_stream: bool = False, data: bytes = b"hello world"):
    """Create a mock get_object that fails N times then succeeds.

    When fail_in_stream is True the failure happens while reading the response body
    (a fresh Body is handed out on every attempt), exercising the mid-stream path.
    """
    call_count = [0]
    exc = exception if exception is not None else _create_client_error()

    async def mock_get_object(*args, **kwargs):
        call_count[0] += 1
        should_fail = call_count[0] <= fail_count
        if should_fail and not fail_in_stream:
            raise exc
        read_exc = exc if (should_fail and fail_in_stream) else None
        return {"Body": _FakeStreamingBody(data, read_exc=read_exc)}

    return mock_get_object, call_count


class _ReadRetryTestContext:
    """Inject a strict fake client + recording sleep for read_file_content retry tests.

    The storage under test is built here so the client provider and backoff sleep are
    injected at construction time (no more patching of get_instance or asyncio.sleep).
    Any env vars a test sets before entering the context are honored at construction.
    """

    def __init__(self, get_object_func):
        self.sleep_func, self.sleep_delays = _recording_sleep()
        self.mock_client = _FakeS3Client(get_object=get_object_func)
        self.storage = S3Storage(
            "test-version",
            False,
            ThrottlingParameters(),
            client_provider=_FakeClientProvider(self.mock_client),
            sleep=self.sleep_func,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_retry_single_failure_OMPE_98588():
    """read_file_content succeeds after a single transient failure with backoff."""
    mock_get_object, call_count = _create_read_mock(fail_count=1)

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

    assert result == Result.OK
    assert content == b"hello world"
    assert call_count[0] == 2
    assert len(ctx.sleep_delays) == 1
    assert 1.0 <= ctx.sleep_delays[0] <= 1.1


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_read_resource_identity_honors_version_and_etag_OMPE_58312():
    get_object_calls = []

    async def mock_get_object(*args, **kwargs):
        get_object_calls.append((args, kwargs))
        return {"Body": _FakeStreamingBody(b"identity bytes")}

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_resource_identity("s3://test-bucket/file.txt?versionId=version-1&etag=%22abc123%22")

    assert result == Result.OK
    assert content == b"identity bytes"
    assert get_object_calls == [
        (
            (),
            {
                "Bucket": "test-bucket",
                "Key": "file.txt",
                "VersionId": "version-1",
                "IfMatch": '"abc123"',
            },
        )
    ]


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_read_resource_identity_mismatch_is_not_found_OMPE_58312():
    precondition_failed = _create_client_error(error_code="PreconditionFailed", http_status=412)
    mock_get_object, call_count = _create_read_mock(fail_count=1, exception=precondition_failed)

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_resource_identity("s3://test-bucket/file.txt?etag=%22catalog-etag%22")

    assert result == Result.ERROR_NOT_FOUND
    assert content == b""
    assert call_count[0] == 1
    assert ctx.sleep_delays == []


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_copy_resource_identity_honors_version_and_etag_OMPE_58312():
    client = AsyncMock()
    client.copy_object.return_value = {}
    storage = S3Storage(
        "test-version",
        False,
        ThrottlingParameters(),
        client_provider=_FakeClientProvider(client),
    )

    with patch.object(storage, "stat", new_callable=AsyncMock, return_value=(Result.ERROR_NOT_FOUND, None)):
        result = await storage.copy_resource_identity(
            "s3://source-bucket/source.txt?versionId=version-1&etag=%22abc123%22",
            "s3://dest-bucket/dest.txt",
            overwrite=False,
        )

    assert result == Result.OK
    client.copy_object.assert_awaited_once_with(
        CopySource={"Bucket": "source-bucket", "Key": "source.txt", "VersionId": "version-1"},
        Bucket="dest-bucket",
        Key="dest.txt",
        CopySourceIfMatch='"abc123"',
    )


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_large_copy_fallback_preserves_stale_identity_result_OMPE_58312():
    client = AsyncMock()
    client.copy_object.side_effect = _create_client_error(error_code="InvalidRequest")
    storage = S3Storage(
        "test-version",
        False,
        ThrottlingParameters(),
        client_provider=_FakeClientProvider(client),
    )

    with patch.object(
        storage,
        "stat",
        new_callable=AsyncMock,
        side_effect=[(Result.ERROR_NOT_FOUND, None), (Result.ERROR_NOT_FOUND, None)],
    ):
        result = await storage.copy_resource_identity(
            "s3://source-bucket/source.txt?versionId=stale-version",
            "s3://dest-bucket/dest.txt",
            overwrite=False,
        )

    assert result == Result.ERROR_NOT_FOUND
    client.copy.assert_not_awaited()


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_retry_multiple_failures_OMPE_98588():
    """read_file_content succeeds after multiple failures with increasing backoff."""
    mock_get_object, call_count = _create_read_mock(fail_count=2)

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

    assert result == Result.OK
    assert call_count[0] == 3
    assert len(ctx.sleep_delays) == 2
    sleep_times = ctx.sleep_delays
    assert 1.0 <= sleep_times[0] <= 1.1
    assert 2.0 <= sleep_times[1] <= 2.2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_retry_max_retries_exhausted_OMPE_98588(caplog):
    """read_file_content returns ERROR after exhausting all retry attempts."""
    mock_get_object, call_count = _create_read_mock(fail_count=3, exception=BotoCoreError())

    with caplog.at_level(logging.WARNING, logger="omni.wrapp"), _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

    assert result == Result.ERROR
    assert content == b""
    assert call_count[0] == 3
    assert len(ctx.sleep_delays) == 2
    # Each retry logs the actual error before sleeping.
    assert any("read attempt 1/3 failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_retry_mid_stream_failure_OMPE_98588():
    """A failure while reading the body re-issues get_object on each attempt (stream not resumed)."""
    mock_get_object, call_count = _create_read_mock(fail_count=2, exception=BotoCoreError(), fail_in_stream=True)

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

    assert result == Result.OK
    assert content == b"hello world"
    assert call_count[0] == 3  # fresh get_object every attempt


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_not_found_no_retry_OMPE_98588():
    """A 404 is terminal: ERROR_NOT_FOUND with no retries."""
    mock_get_object, call_count = _create_read_mock(fail_count=1, exception=_create_client_error(error_code="404"))

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

    assert result == Result.ERROR_NOT_FOUND
    assert content == b""
    assert call_count[0] == 1
    assert len(ctx.sleep_delays) == 0


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize(
    "exception",
    [
        _create_client_error(error_code="AccessDenied"),
        # A credential code carried in S3_ACCESS_DENIED_CODES (no HTTP status needed).
        _create_client_error(error_code="InvalidAccessKeyId"),
        # An *unknown* error code with HTTP 403 isolates the http_status == "403" fallback branch.
        _create_client_error(error_code="SomeUndocumentedAuthCode", http_status=403),
        NoCredentialsError(),
    ],
)
async def test_read_access_denied_no_retry_OMPE_98588(exception):
    """Auth/permission errors are terminal: ERROR_ACCESS_DENIED with no retries."""
    mock_get_object, call_count = _create_read_mock(fail_count=1, exception=exception)

    with _ReadRetryTestContext(mock_get_object) as ctx:
        result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

    assert result == Result.ERROR_ACCESS_DENIED
    assert content == b""
    assert call_count[0] == 1
    assert len(ctx.sleep_delays) == 0


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_retry_custom_env_variables_OMPE_98588():
    """read_file_content honors custom WRAPP_S3_EXTRA_RETRIES / WRAPP_S3_RETRY_WAIT_SECONDS."""
    original_retries = os.environ.get("WRAPP_S3_EXTRA_RETRIES")
    original_delay = os.environ.get("WRAPP_S3_RETRY_WAIT_SECONDS")
    try:
        os.environ["WRAPP_S3_EXTRA_RETRIES"] = "5"
        os.environ["WRAPP_S3_RETRY_WAIT_SECONDS"] = "0.5"
        mock_get_object, call_count = _create_read_mock(fail_count=4)

        with _ReadRetryTestContext(mock_get_object) as ctx:
            result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

        assert result == Result.OK
        assert call_count[0] == 5
        assert len(ctx.sleep_delays) == 4
        sleep_times = ctx.sleep_delays
        assert 0.5 <= sleep_times[0] <= 0.55
        assert 1.0 <= sleep_times[1] <= 1.1
        assert 2.0 <= sleep_times[2] <= 2.2
        assert 4.0 <= sleep_times[3] <= 4.4
    finally:
        if original_retries is not None:
            os.environ["WRAPP_S3_EXTRA_RETRIES"] = original_retries
        else:
            os.environ.pop("WRAPP_S3_EXTRA_RETRIES", None)
        if original_delay is not None:
            os.environ["WRAPP_S3_RETRY_WAIT_SECONDS"] = original_delay
        else:
            os.environ.pop("WRAPP_S3_RETRY_WAIT_SECONDS", None)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_read_makes_one_attempt_with_retries_disabled_OMPE_98588():
    """With WRAPP_S3_EXTRA_RETRIES=0 the read is still attempted exactly once."""
    original_retries = os.environ.get("WRAPP_S3_EXTRA_RETRIES")
    try:
        os.environ["WRAPP_S3_EXTRA_RETRIES"] = "0"
        mock_get_object, call_count = _create_read_mock(fail_count=0)

        with _ReadRetryTestContext(mock_get_object) as ctx:
            result, content = await ctx.storage.read_file_content("s3://test-bucket/test-file.txt")

        assert result == Result.OK
        assert content == b"hello world"
        assert call_count[0] == 1
        assert len(ctx.sleep_delays) == 0
    finally:
        if original_retries is not None:
            os.environ["WRAPP_S3_EXTRA_RETRIES"] = original_retries
        else:
            os.environ.pop("WRAPP_S3_EXTRA_RETRIES", None)


# ===== download_file Chunked Retry Logic Tests (OMPE-98951) =====


class _RangedGetMock:
    """A get_object mock that serves byte ranges of `data` and records every call's kwargs.

    fail_plan maps a chunk's range start offset (None for an unranged get) to a list of
    exceptions; each GET for that range consumes one exception until the list is empty.
    With fail_in_stream the exception is raised while reading the response body instead
    of by the get_object call itself, exercising the mid-stream failure path.
    Keying failures by range offset instead of call order keeps the tests deterministic
    under the concurrent chunk downloads.
    """

    def __init__(self, data: bytes, fail_plan: dict | None = None, fail_in_stream: bool = False, ignore_range: bool = False):
        self.data = data
        self.calls: list[dict] = []
        self.fail_plan = {key: list(excs) for key, excs in (fail_plan or {}).items()}
        self.fail_in_stream = fail_in_stream
        # Model an endpoint/proxy that ignores the Range header and returns the full 200 body.
        self.ignore_range = ignore_range

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        # Yield to the event loop like a real network call would; without a suspension
        # point, sibling-task cancellation could never be delivered mid-download and the
        # bounded chunk-worker pool would never interleave. Nothing here stubs asyncio.sleep
        # (S3Storage takes an injected backoff-sleep instead), so this plain sleep(0) really
        # yields to the loop.
        await asyncio.sleep(0)
        range_header = kwargs.get("Range")
        if range_header is None:
            plan_key = None
            payload = self.data
        else:
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", range_header)
            assert match, f"malformed Range header: {range_header}"
            start, end = int(match.group(1)), int(match.group(2))
            plan_key = start
            payload = self.data if self.ignore_range else self.data[start : end + 1]
        pending = self.fail_plan.get(plan_key)
        if pending:
            exc = pending.pop(0)
            if not self.fail_in_stream:
                raise exc
            return {"Body": _FakeStreamingBody(payload, read_exc=exc)}
        return {"Body": _FakeStreamingBody(payload)}


class _DownloadTestContext:
    """Inject a strict fake client + recording sleep for download_file tests.

    Builds the S3Storage under test so the client provider and backoff sleep are injected at
    construction time (no more patching of get_instance or asyncio.sleep). The strict fake
    client answers get_object and head_object (and list_objects_v2, unused on this path) so
    _head_object_entry works against it. Pass chunked=True (plus threshold/chunksize/extra_env/
    throttling) to build a multipart-sized storage; otherwise a real-default storage is built.
    """

    def __init__(
        self,
        get_object_func,
        size: int,
        head_exception: Exception | None = None,
        *,
        chunked: bool = False,
        threshold: str = "4",
        chunksize: str = "4",
        extra_env: dict | None = None,
        throttling: ThrottlingParameters | None = None,
    ):
        self.sleep_func, self.sleep_delays = _recording_sleep()
        head_result = None if head_exception is not None else {"ContentLength": size, "LastModified": datetime.now(), "ETag": '"test-etag"'}
        self.mock_client = _FakeS3Client(get_object=get_object_func, head_object_result=head_result, head_exception=head_exception)
        provider = _FakeClientProvider(self.mock_client)
        if chunked:
            self.storage = _make_chunked_storage(
                threshold=threshold,
                chunksize=chunksize,
                extra_env=extra_env,
                throttling=throttling,
                client_provider=provider,
                sleep=self.sleep_func,
            )
        else:
            self.storage = S3Storage(
                "test-version", False, throttling or ThrottlingParameters(), client_provider=provider, sleep=self.sleep_func
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def _make_chunked_storage(
    threshold: str = "4",
    chunksize: str = "4",
    extra_env: dict | None = None,
    throttling: ThrottlingParameters | None = None,
    *,
    client_provider=None,
    sleep=asyncio.sleep,
) -> S3Storage:
    """Create an S3Storage whose multipart threshold/chunksize are small enough for chunked tests.

    Pass throttling=ThrottlingParameters(file_transfer_jobs=..., jobs=...) to exercise the
    networkdata / roundtrip slot bounds. client_provider/sleep thread the DI seam through so
    tests can inject a strict fake client and a recording sleep.
    """
    env = {"WRAPP_S3_MULTIPART_THRESHOLD": threshold, "WRAPP_S3_MULTIPART_CHUNKSIZE": chunksize}
    env.update(extra_env or {})
    with patch.dict(os.environ, env):
        return S3Storage("test-version", False, throttling or ThrottlingParameters(), client_provider=client_provider, sleep=sleep)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_small_file_OMPE_98951(tmp_path):
    """A file at or below the multipart threshold is fetched with a single unranged, If-Match-pinned GET."""
    data = b"hello world!"
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data)

    with _DownloadTestContext(mock_get, size=len(data)) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data
    assert len(mock_get.calls) == 1
    # No Range (the whole small object is fetched at once) but the GET is pinned to the version
    # stat saw via If-Match, so a concurrent overwrite fails the GET instead of returning it.
    assert "Range" not in mock_get.calls[0]
    assert mock_get.calls[0]["IfMatch"] == '"test-etag"'


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_small_file_concurrent_replace_412_OMPE_98951(tmp_path):
    """A concurrent overwrite between stat and the small-file GET (If-Match 412) aborts cleanly.

    The small-file branch is unranged, so the mock's fail_plan is keyed by None. A 412
    PreconditionFailed means the object changed under us: the download must fail terminally
    (not retry, not buffer the replaced object into RAM) and leave no partial destination file.
    Mirrors the multipart test_download_chunk_precondition_failed_aborts_OMPE_98951.
    """
    data = b"hello world!"
    dst_path = tmp_path / "out.bin"
    precondition_failed = _create_client_error(error_code="PreconditionFailed", http_status=412)
    mock_get = _RangedGetMock(data, fail_plan={None: [precondition_failed]})

    with _DownloadTestContext(mock_get, size=len(data)) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    # 412 is classified as a permanent, non-retryable client error -> terminal Result.ERROR.
    assert result == Result.ERROR
    assert not dst_path.exists()
    # The unranged small-file GET is attempted exactly once (no retry of a version-mismatch) ...
    assert len(mock_get.calls) == 1
    assert "Range" not in mock_get.calls[0]
    # ... it carried the If-Match pin that produced the 412 ...
    assert mock_get.calls[0]["IfMatch"] == '"test-etag"'
    # ... and no backoff sleep is taken (the mock's own sleep(0) yields don't count).
    backoff_sleeps = [c for c in ctx.sleep_delays if c > 0]
    assert backoff_sleeps == []


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_metadata_uses_head_object_not_list_OMPE_98951(tmp_path):
    """The download metadata lookup issues exactly one head_object and NO list_objects_v2.

    The download path only needs the object's size/etag, so _download_file_content uses the
    head_object-only _head_object_entry helper rather than the general-purpose stat(), which
    also fires a list_objects_v2 folder-prefix probe -- a wasted LIST per file on large syncs.
    """
    data = b"hello world!"
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data)

    with _DownloadTestContext(mock_get, size=len(data)) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data
    assert ctx.mock_client.head_object_call_count == 1
    assert ctx.mock_client.list_objects_v2_call_count == 0


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize("identity_download", [False, True])
async def test_download_stale_resource_identity_is_not_found_OMPE_58312(tmp_path, identity_download):
    dst_path = tmp_path / "out.bin"
    precondition_failed = _create_client_error(error_code="PreconditionFailed", http_status=412)
    mock_get = _RangedGetMock(b"replacement")

    with _DownloadTestContext(mock_get, size=11, head_exception=precondition_failed) as ctx:
        download = ctx.storage.download_resource_identity if identity_download else ctx.storage.download_file
        result = await download(
            "s3://test-bucket/test-file.txt?etag=%22catalog-etag%22",
            str(dst_path),
            overwrite=True,
            **({"expected_hash": "catalog hash"} if identity_download else {}),
        )

    assert result == Result.ERROR_NOT_FOUND
    assert not dst_path.exists()
    assert ctx.mock_client.head_object_call_count == 1
    assert ctx.mock_client.head_object_calls == [((), {"Bucket": "test-bucket", "Key": "test-file.txt", "IfMatch": '"catalog-etag"'})]
    assert mock_get.calls == []


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_multi_chunk_ranges_and_assembly_OMPE_98951(tmp_path):
    """A file above the threshold is split into ranged GETs and assembled at the right offsets."""
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data)

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data
    assert {call["Range"] for call in mock_get.calls} == {"bytes=0-3", "bytes=4-7", "bytes=8-9"}
    # The object version seen by stat must be pinned on every chunk GET.
    assert all(call["IfMatch"] == '"test-etag"' for call in mock_get.calls)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_more_chunks_than_workers_each_claimed_once_OMPE_98951(tmp_path):
    """With a bounded pool smaller than the chunk count, every chunk is claimed exactly once.

    WRAPP_S3_MAX_CONCURRENCY caps the pool at 2 workers while the file splits into 5 chunks
    (num_workers < n_chunks), so each worker must claim several chunks off the shared
    next_chunk_index counter. A double-claim or skip bug in that counter would corrupt the
    reassembled file or duplicate a GET; asserting each Range appears *exactly once* (by count,
    not set membership) catches both, which a set-based assertion would hide.
    """
    data = bytes(range(18))  # 5 chunks of size 4: bytes=0-3, 4-7, 8-11, 12-15, 16-17
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data)

    with _DownloadTestContext(mock_get, size=len(data), chunked=True, extra_env={"WRAPP_S3_MAX_CONCURRENCY": "2"}) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    # The reassembled destination must equal the source byte-for-byte.
    assert dst_path.read_bytes() == data
    # Each expected Range must be fetched exactly once - no chunk double-claimed or skipped.
    range_counts = Counter(call["Range"] for call in mock_get.calls)
    assert range_counts == Counter({"bytes=0-3": 1, "bytes=4-7": 1, "bytes=8-11": 1, "bytes=12-15": 1, "bytes=16-17": 1})
    # Every chunk GET pins the version seen by stat.
    assert all(call["IfMatch"] == '"test-etag"' for call in mock_get.calls)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_chunk_wrong_length_fails_OMPE_98951(tmp_path):
    """A ranged GET that returns the wrong number of bytes fails instead of corrupting the file.

    Models an endpoint/proxy that ignores the Range header and returns the full 200 body: the
    chunk read validates its length against the requested range, so the download fails with
    Result.ERROR and leaves no partial file rather than writing misaligned data and reporting
    success. See OMPE-98951.
    """
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data, ignore_range=True)  # every ranged GET returns the full body

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.ERROR
    assert not dst_path.exists()


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_chunk_precondition_failed_aborts_OMPE_98951(tmp_path):
    """A concurrent overwrite between stat and a chunk GET (If-Match 412) aborts cleanly.

    Version pinning is the MR's headline safety guarantee: a 412 PreconditionFailed means the
    object changed under us, so the download must fail terminally rather than stitch together
    bytes from two versions. It must not be retried (nothing transient to recover) and must
    leave no partial destination file behind.
    """
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    precondition_failed = _create_client_error(error_code="PreconditionFailed", http_status=412)
    mock_get = _RangedGetMock(data, fail_plan={4: [precondition_failed]})

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    # 412 is classified as a permanent, non-retryable client error -> terminal Result.ERROR.
    assert result == Result.ERROR
    assert not dst_path.exists()
    # The failing chunk is attempted exactly once (no retry of a version-mismatch failure) ...
    assert len([c for c in mock_get.calls if c.get("Range") == "bytes=4-7"]) == 1
    # ... and no backoff sleep is taken (the mock's own sleep(0) yields don't count).
    backoff_sleeps = [c for c in ctx.sleep_delays if c > 0]
    assert backoff_sleeps == []


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_chunk_midstream_retry_OMPE_98951(tmp_path):
    """A transient mid-stream failure of one chunk is retried; the download still succeeds."""
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data, fail_plan={4: [BotoCoreError()]}, fail_in_stream=True)

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data
    # 3 chunks + 1 retry of the failed chunk, each attempt with a fresh GET.
    assert len(mock_get.calls) == 4
    # One backoff sleep (the mock's own sleep(0) yields don't count).
    backoff_sleeps = [c for c in ctx.sleep_delays if c > 0]
    assert len(backoff_sleeps) == 1


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_retry_exhausted_removes_partial_OMPE_98951(tmp_path):
    """A persistently failing chunk exhausts its retries; the partial file is removed."""
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    # Default WRAPP_S3_EXTRA_RETRIES is 3, so 3 failures on one chunk exhaust all attempts.
    mock_get = _RangedGetMock(data, fail_plan={4: [BotoCoreError(), BotoCoreError(), BotoCoreError()]})

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.ERROR
    assert not dst_path.exists()
    # Two backoff sleeps for three attempts (the mock's own sleep(0) yields don't count).
    backoff_sleeps = [c for c in ctx.sleep_delays if c > 0]
    assert len(backoff_sleeps) == 2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_unexpected_worker_exception_returns_error_OMPE_98951(tmp_path):
    """An unexpected (non-boto/aiohttp) chunk failure yields Result.ERROR and removes the partial file.

    An exception outside (BotoCoreError, ClientError, aiohttp.ClientError) -- e.g. an OSError/
    ENOSPC from f_dst.write on a full disk -- escapes _read_file_ranged_content unclassified, is
    NOT wrapped in _ChunkDownloadError by the chunk worker, and fails the TaskGroup as an
    ExceptionGroup that does not match except* _ChunkDownloadError. download_file's generic
    except-Exception branch is the only thing converting it into a per-file Result.ERROR plus
    partial-file cleanup; if that branch regressed, the ExceptionGroup would escape and abort the
    caller's whole task group instead of failing just this file. See OMPE-98951.
    """
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data, fail_plan={4: [RuntimeError("boom")]})

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        # Must return a Result, not propagate an ExceptionGroup out of download_file.
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.ERROR
    assert not dst_path.exists()
    # The unexpected exception bypasses the retry classifier: the failing chunk is attempted
    # exactly once and no backoff sleep is taken (the mock's own sleep(0) yields don't count).
    assert len([c for c in mock_get.calls if c.get("Range") == "bytes=4-7"]) == 1
    backoff_sleeps = [c for c in ctx.sleep_delays if c > 0]
    assert backoff_sleeps == []


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_partial_cleanup_permissionerror_is_swallowed_OMPE_98951(tmp_path):
    """A PermissionError while removing the partial file must not escape download_file.

    On Windows an AV/indexer can briefly hold the just-closed destination file, so os.remove on
    the failure path raises PermissionError. Partial-cleanup is best-effort: it must be swallowed
    so download_file still returns the true failure Result instead of letting an OSError propagate
    to the caller's task group (and it must not be misreported as ERROR_ACCESS_DENIED). See
    OMPE-98951.
    """
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    # Exhaust the chunk's retries so the download fails and partial-cleanup runs.
    mock_get = _RangedGetMock(data, fail_plan={4: [BotoCoreError(), BotoCoreError(), BotoCoreError()]})

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        with patch("os.remove", side_effect=PermissionError("locked by scanner")):
            # Must return a Result, not raise: pre-fix the re-raised PermissionError escaped here.
            result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    # The genuine failure Result is preserved (not masked as ERROR_ACCESS_DENIED).
    assert result == Result.ERROR
    # os.remove was attempted but its PermissionError was swallowed, so the partial file remains.
    assert dst_path.exists()


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_not_found_OMPE_98951(tmp_path):
    """A missing object yields ERROR_NOT_FOUND and leaves no destination file behind."""
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(b"")

    with _DownloadTestContext(mock_get, size=0, head_exception=_create_client_error(error_code="404")) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.ERROR_NOT_FOUND
    assert not dst_path.exists()
    assert len(mock_get.calls) == 0


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_already_exists_OMPE_98951(tmp_path):
    """overwrite=False with an existing destination fails before any S3 call."""
    data = b"new content"
    dst_path = tmp_path / "out.bin"
    dst_path.write_bytes(b"old content")
    mock_get = _RangedGetMock(data)

    with _DownloadTestContext(mock_get, size=len(data)) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=False)

    assert result == Result.ERROR_ALREADY_EXISTS
    assert dst_path.read_bytes() == b"old content"
    assert len(mock_get.calls) == 0
    assert ctx.mock_client.head_object_call_count == 0


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_overwrite(tmp_path):
    """overwrite=True replaces an existing destination file."""
    data = b"new content"
    dst_path = tmp_path / "out.bin"
    dst_path.write_bytes(b"old content that is longer")
    mock_get = _RangedGetMock(data)

    with _DownloadTestContext(mock_get, size=len(data)) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_zero_byte_file(tmp_path):
    """A zero-byte object is materialized as an empty file without any GET."""
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(b"")

    with _DownloadTestContext(mock_get, size=0) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.exists() and dst_path.stat().st_size == 0
    assert len(mock_get.calls) == 0


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_wrong_type_for_folder_key_OMPE_98951(tmp_path):
    """A source key ending in '/' is rejected with ERROR_WRONG_TYPE, like read_file_content."""
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(b"")

    with _DownloadTestContext(mock_get, size=0) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/folder/", str(dst_path), overwrite=True)

    assert result == Result.ERROR_WRONG_TYPE
    assert not dst_path.exists()


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_access_denied_cancels_other_chunks_OMPE_98951(tmp_path):
    """A terminal chunk failure aborts the download promptly and removes the partial file."""
    data = bytes(range(64))  # 16 chunks of 4 bytes
    dst_path = tmp_path / "out.bin"
    # Two workers make the abort observable: far fewer than 16 GETs must have happened.
    mock_get = _RangedGetMock(data, fail_plan={0: [_create_client_error(error_code="AccessDenied")]})

    # _RangedGetMock yields on every GET (a real sleep(0)), so once the offset-0 chunk fails
    # the sibling workers are cancelled before most of the 16 GETs run.
    with _DownloadTestContext(mock_get, size=len(data), chunked=True, extra_env={"WRAPP_S3_MAX_CONCURRENCY": "2"}) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.ERROR_ACCESS_DENIED
    assert not dst_path.exists()
    assert len(mock_get.calls) < 16


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_cancelled_removes_partial_OMPE_98951(tmp_path):
    """Cancelling a download propagates CancelledError and removes the partial file."""
    data = bytes(range(16))
    dst_path = tmp_path / "out.bin"
    reached_read = asyncio.Event()
    hang_forever = asyncio.Event()

    class _HangingBody(_FakeStreamingBody):
        async def read(self):
            reached_read.set()
            await hang_forever.wait()
            return b""

    calls = []

    async def mock_get_object(**kwargs):
        calls.append(kwargs)
        return {"Body": _HangingBody(b"")}

    with _DownloadTestContext(mock_get_object, size=len(data), chunked=True) as ctx:
        download = asyncio.create_task(ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True))
        await reached_read.wait()
        download.cancel()
        with pytest.raises(asyncio.CancelledError):
            await download

    assert not dst_path.exists()


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_chunk_midstream_client_payload_error_retry_OMPE_98951(tmp_path):
    """A mid-stream aiohttp.ClientPayloadError (S3 idle-reset of the body) is retried, not fatal.

    ClientPayloadError is neither a BotoCoreError nor a botocore ClientError, so before the fix
    it escaped the read retry loop and failed the whole download. It must now be classified
    retryable and the re-fetched chunk must reassemble byte-identically.
    """
    data = bytes(range(10))
    dst_path = tmp_path / "out.bin"
    mock_get = _RangedGetMock(data, fail_plan={4: [aiohttp.ClientPayloadError()]}, fail_in_stream=True)

    with _DownloadTestContext(mock_get, size=len(data), chunked=True) as ctx:
        result = await ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data
    # 3 chunks + 1 retry of the failed chunk, each attempt re-issuing a fresh GET.
    assert len(mock_get.calls) == 4
    # Exactly one backoff sleep (the mock's own sleep(0) yields don't count).
    backoff_sleeps = [c for c in ctx.sleep_delays if c > 0]
    assert len(backoff_sleeps) == 1


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="FD sampling requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_bounds_open_destination_files_OMPE_98951(tmp_path):
    """At most file_transfer_jobs destination files are open at once (bounds FDs, avoids EMFILE).

    The destination FD is opened inside the single NetworkDataTransfer slot, so firing many
    concurrent download_file tasks with file_transfer_jobs=2 can never open more than 2
    destination files simultaneously -- the rest wait on the throttler before their open().

    The bound is verified patch-free by sampling this process's real open FDs via /proc/self/fd
    at a deterministic point: the two slot-holding reads hang on a never-set Event and the settle
    loop gives any (incorrectly) unbounded extra task time to open its destination, so a single
    sample is equivalent to peak tracking.
    """
    # A tiny file uses the single-GET small-file path (one open, one read per download).
    data = b"xy"
    n_downloads = 6
    file_transfer_jobs = 2

    reached_reads = 0
    reads_may_proceed = asyncio.Event()  # never set -> reads hang, holding their slot open

    class _HangingBody(_FakeStreamingBody):
        async def read(self):
            nonlocal reached_reads
            reached_reads += 1
            await reads_may_proceed.wait()
            return self._data

    async def mock_get_object(**kwargs):
        return {"Body": _HangingBody(data)}

    def open_destination_fds() -> int:
        """Count open FDs resolving to .bin destination files under tmp_path."""
        dst_dir = os.path.realpath(str(tmp_path)) + os.sep
        count = 0
        for fd in os.listdir("/proc/self/fd"):
            try:
                target = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue  # fd vanished between listdir and readlink (e.g. the listdir FD itself)
            if target.startswith(dst_dir) and target.endswith(".bin"):
                count += 1
        return count

    downloads = []
    with _DownloadTestContext(
        mock_get_object,
        size=len(data),
        chunked=True,
        threshold="1024",
        throttling=ThrottlingParameters(file_transfer_jobs=file_transfer_jobs, jobs=8),
    ) as ctx:
        for i in range(n_downloads):
            dst = tmp_path / f"out{i}.bin"
            downloads.append(asyncio.create_task(ctx.storage.download_file("s3://test-bucket/test-file.txt", str(dst), overwrite=True)))
        # Let the event loop run until the two slots are occupied and their reads are hanging.
        for _ in range(2000):
            await asyncio.sleep(0)
            if reached_reads >= file_transfer_jobs:
                break
        # Give any (incorrectly) unbounded extra tasks a chance to slip past the throttler.
        for _ in range(100):
            await asyncio.sleep(0)

        assert reached_reads == file_transfer_jobs
        # Exactly the two slot-holders have their destination open; an implementation that
        # opened destinations before slot acquisition would show all 6 here.
        assert open_destination_fds() == file_transfer_jobs

        # Unblock and let every download finish so nothing is left dangling.
        reads_may_proceed.set()
        results = await asyncio.gather(*downloads)

    assert all(r == Result.OK for r in results)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_download_holds_single_networkdata_slot_OMPE_98951(tmp_path):
    """A chunked download never re-acquires NetworkDataTransfer: at most one slot is held.

    download_file holds exactly one networkdata slot for the whole file; the chunk workers read
    underneath it via the low-level fetch without re-acquiring networkdata. Sampling the throttler
    on every GET must therefore never see more than one processing task.
    """
    data = bytes(range(10))  # 3 chunks of 4 bytes
    dst_path = tmp_path / "out.bin"
    base_mock = _RangedGetMock(data)
    observed_networkdata = []

    # sampling_get references the storage's throttler, so the storage must exist before the
    # get_object mock. Build the strict fake client + recording sleep and inject them directly
    # (rather than via _DownloadTestContext, which would build the storage after the mock).
    recording_sleep, _sleep_delays = _recording_sleep()
    fake_client = _FakeS3Client(head_object_result={"ContentLength": len(data), "LastModified": datetime.now(), "ETag": '"test-etag"'})
    storage = _make_chunked_storage(client_provider=_FakeClientProvider(fake_client), sleep=recording_sleep)

    async def sampling_get(**kwargs):
        observed_networkdata.append(len(storage._throttler_networkdata.processing_tasks))
        return await base_mock(**kwargs)

    fake_client.get_object_func = sampling_get
    result = await storage.download_file("s3://test-bucket/test-file.txt", str(dst_path), overwrite=True)

    assert result == Result.OK
    assert dst_path.read_bytes() == data
    # Multiple chunks were fetched under the one held slot.
    assert len(base_mock.calls) == 3
    # The parent download holds exactly one networkdata slot throughout; workers add none.
    assert max(observed_networkdata) <= 1
    assert 1 in observed_networkdata


@pytest.mark.asyncio
@force_wrapp_thread
@pytest.mark.parametrize("env_value, expected_read, expected_connect", [(None, 60.0, 5.0), ("123.5", 123.5, 123.5), ("abc", 60.0, 5.0)])
async def test_s3_client_read_timeout_env(env_value, expected_read, expected_connect):
    """WRAPP_S3_READ_TIMEOUT_S/WRAPP_S3_CONNECT_TIMEOUT_S reach the botocore client config; unset or invalid
    values fall back to the defaults (botocore's own 60s read / 5s connect)."""
    env_vars = ("WRAPP_S3_READ_TIMEOUT_S", "WRAPP_S3_CONNECT_TIMEOUT_S")
    originals = {env_var: os.environ.get(env_var) for env_var in env_vars}
    try:
        for env_var in env_vars:
            if env_value is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = env_value
        storage = S3Storage("test-version", False, ThrottlingParameters())
        wrapper = storage.s3_client()
        mock_session = MagicMock()
        mock_session.client = MagicMock(return_value=AsyncMock())

        with patch.object(S3ClientWrapper, "session", new_callable=AsyncMock, return_value=mock_session):
            await wrapper.get_instance()

        config = mock_session.client.call_args.kwargs["config"]
        assert config.read_timeout == expected_read
        assert config.connect_timeout == expected_connect
    finally:
        for env_var, original in originals.items():
            if original is not None:
                os.environ[env_var] = original
            else:
                os.environ.pop(env_var, None)


@pytest.mark.parametrize("bad_value", ["-1", "0", "not-an-int"])
@pytest.mark.parametrize(
    "env_var, attr, default",
    [
        ("WRAPP_S3_MULTIPART_CHUNKSIZE", "_multipart_chunksize", 5 * 1024 * 1024),
        ("WRAPP_S3_MULTIPART_THRESHOLD", "_multipart_threshold", 5 * 1024 * 1024),
        ("WRAPP_S3_MAX_CONCURRENCY", "_max_concurrency", 50),
    ],
)
def test_nonpositive_s3_size_env_falls_back_to_default_OMPE_98951(env_var, attr, default, bad_value):
    """A non-positive/invalid size or count setting must not reach the download math.

    A negative WRAPP_S3_MULTIPART_CHUNKSIZE would make n_chunks negative, so the chunk-worker
    loop would never run and a multipart download would silently produce a zero-filled file of
    the correct size while reporting Result.OK. parse_env_int rejects non-integer / non-positive
    values and falls back to the positive default, guarding against that. See OMPE-98951.
    """
    with patch.dict(os.environ, {env_var: bad_value}):
        storage = S3Storage("test-version", False, ThrottlingParameters())
    assert getattr(storage, attr) == default
    assert getattr(storage, attr) > 0


def test_extra_retries_and_wait_env_parsing_OMPE_98951():
    """EXTRA_RETRIES allows 0 (disables retries) but rejects negatives; RETRY_WAIT_SECONDS parses.

    EXTRA_RETRIES goes through parse_env_int(minimum=0), so 0 is honored while a negative or
    non-integer value falls back to the default. RETRY_WAIT_SECONDS goes through parse_env_float,
    so a non-numeric value falls back to the default. See OMPE-98951.
    """
    # 0 is a valid retry count (disables extra retries) and must be preserved.
    with patch.dict(os.environ, {"WRAPP_S3_EXTRA_RETRIES": "0"}):
        assert S3Storage("test-version", False, ThrottlingParameters())._retry_count == 0
    # A negative or non-integer retry count falls back to the default (3).
    with patch.dict(os.environ, {"WRAPP_S3_EXTRA_RETRIES": "-2"}):
        assert S3Storage("test-version", False, ThrottlingParameters())._retry_count == 3
    with patch.dict(os.environ, {"WRAPP_S3_EXTRA_RETRIES": "notanint"}):
        assert S3Storage("test-version", False, ThrottlingParameters())._retry_count == 3
    # RETRY_WAIT_SECONDS parses a valid float; a non-numeric value falls back to the default (1.0).
    with patch.dict(os.environ, {"WRAPP_S3_RETRY_WAIT_SECONDS": "2.5"}):
        assert S3Storage("test-version", False, ThrottlingParameters())._retry_delay == 2.5
    with patch.dict(os.environ, {"WRAPP_S3_RETRY_WAIT_SECONDS": "notanumber"}):
        assert S3Storage("test-version", False, ThrottlingParameters())._retry_delay == 1.0


@pytest.mark.asyncio
@force_wrapp_thread
async def test_log_level_env_variables(caplog):
    # Save original environment variables
    log_level = os.environ.get("WRAPP_S3_LOG_LEVEL")

    try:
        assert logging.getLogger("aiobotocore").level == logging.NOTSET

        # Do not set the report level
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.NOTSET

        # Set report level to INFO
        os.environ["WRAPP_S3_LOG_LEVEL"] = "INFO"
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.INFO

        # Set report level to WARNING
        logging.getLogger("aiobotocore").setLevel(logging.NOTSET)
        os.environ["WRAPP_S3_LOG_LEVEL"] = "WARNING"
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.WARNING

        # Set report level to something invalid
        caplog.clear()
        logging.getLogger("aiobotocore").setLevel(logging.NOTSET)
        os.environ["WRAPP_S3_LOG_LEVEL"] = "FOO"
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.NOTSET
        assert "Invalid log level 'FOO'" in caplog.records[0].message

        # Do not overwrite log levels set explicitly in code (by the app)
        logging.getLogger("aiobotocore").setLevel(logging.INFO)
        os.environ["WRAPP_S3_LOG_LEVEL"] = "WARNING"
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.INFO

        logging.getLogger("aiobotocore").setLevel(logging.ERROR)
        os.environ["WRAPP_S3_LOG_LEVEL"] = "NOTSET"
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.ERROR

        # When the outer log level is DEBUG, set output from S3 to INFO, unless explicitly set
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("aiobotocore").setLevel(logging.NOTSET)
        assert logging.getLogger("aiobotocore").isEnabledFor(logging.DEBUG)
        os.environ.pop("WRAPP_S3_LOG_LEVEL", None)
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.INFO

        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("aiobotocore").setLevel(logging.DEBUG)
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.DEBUG

        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("aiobotocore").setLevel(logging.WARNING)
        _ = S3Storage("test-version", False, ThrottlingParameters())
        assert logging.getLogger("aiobotocore").level == logging.WARNING
    finally:
        # Restore original environment variables
        if log_level is not None:
            os.environ["WRAPP_S3_LOG_LEVEL"] = log_level
        else:
            os.environ.pop("WRAPP_S3_LOG_LEVEL", None)


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_file_nonexistent_source():
    """Test that upload_file returns ERROR_NOT_FOUND for non-existent source file."""
    import tempfile

    storage = S3Storage("test-version", False, ThrottlingParameters())
    # Use platform-appropriate temp directory to ensure an absolute path
    nonexistent_path = os.path.join(tempfile.gettempdir(), "this_file_does_not_exist_12345.txt")
    dst_url = "s3://test-bucket/test-file.txt"

    # Construct file URL manually for cross-platform compatibility
    if sys.platform == "win32":
        file_url = f"file:///{nonexistent_path.replace(os.sep, '/')}"
    else:
        file_url = f"file://{nonexistent_path}"

    result = await storage.upload_file(file_url, dst_url, overwrite=True)

    assert result == Result.ERROR_NOT_FOUND
