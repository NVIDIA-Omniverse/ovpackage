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

import base64
import binascii
import io
import logging
import os
import random
import sys
from datetime import datetime
from unittest.mock import (
    AsyncMock,
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
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
    )

    from wrapp.utils.storage_impls.s3_helper import parse_s3_bucket_key
    from wrapp.utils.storage_impls.s3storage import (
        S3ClientWrapper,
        S3ListEntry,
        S3Storage,
    )
except (StorageMissingDependenciesError, ModuleNotFoundError) as e:
    pytest.skip(f"Missing dependencies for S3Storage: {e}", allow_module_level=True)


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
        os.environ["AWS_ACCESS_KEY_ID"] = "AKIAINVALID12345678"
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


def _create_client_error(error_code="InternalError", message="Test error"):
    """Helper function to create a ClientError for testing."""
    error_response = {"Error": {"Code": error_code, "Message": message}}
    operation_name = "upload_fileobj"
    return ClientError(error_response, operation_name)


def _create_s3_entry(key: str, size: int) -> S3ListEntry:
    """Helper to create an S3ListEntry for testing."""
    return S3ListEntry.file_from_head_object(key, {"ContentLength": size, "LastModified": datetime.now(), "ETag": '"abc123"'})


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
    """Context manager for common retry test mocking setup."""

    def __init__(self, storage, mock_upload_func):
        self.storage = storage
        self.mock_client = AsyncMock()
        self.mock_client.upload_fileobj = mock_upload_func
        self.mock_client.delete_object = AsyncMock(return_value={})
        self.mock_stat = None
        self.mock_sleep = None
        self._patches = []

    def __enter__(self):
        # Create and enter all patches
        self._patches = [
            patch.object(self.storage, "stat", new_callable=AsyncMock),
            patch.object(self.storage.s3_client(), "get_instance", new_callable=AsyncMock, return_value=self.mock_client),
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("boto3.s3.transfer.TransferConfig"),
        ]
        self.mock_stat = self._patches[0].__enter__()
        self._patches[1].__enter__()
        self.mock_sleep = self._patches[2].__enter__()
        self._patches[3].__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for p in reversed(self._patches):
            p.__exit__(exc_type, exc_val, exc_tb)
        return False


@pytest_asyncio.fixture
async def retry_test_setup():
    """Common setup for retry tests."""
    test_url = "s3://test-bucket/test-file.txt"
    test_data = b"test content"
    file_obj = io.BytesIO(test_data)
    nucleus_hash = "test_hash_123"
    storage = S3Storage("test-version", False, ThrottlingParameters())

    yield {
        "storage": storage,
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

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await setup["storage"]._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        assert call_count[0] == 2
        assert ctx.mock_sleep.call_count == 1
        assert 1.0 <= ctx.mock_sleep.call_args[0][0] <= 1.1


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_multiple_failures(retry_test_setup):
    """Test successful upload after multiple failures with increasing backoff."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=2)

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await setup["storage"]._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.OK
        assert call_count[0] == 3
        assert ctx.mock_sleep.call_count == 2
        sleep_times = [call_args[0][0] for call_args in ctx.mock_sleep.call_args_list]
        assert 1.0 <= sleep_times[0] <= 1.1
        assert 2.0 <= sleep_times[1] <= 2.2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_max_retries_exhausted(retry_test_setup):
    """Test that upload fails after exhausting all retry attempts."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=3, exception_type=BotoCoreError)

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
        ]

        result = await setup["storage"]._upload_fileobj(
            setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
        )

        assert result == Result.ERROR
        assert call_count[0] == 3
        assert ctx.mock_sleep.call_count == 2


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_zero_byte_cleanup(retry_test_setup):
    """Test that 0-byte files are cleaned up before retry."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", 0)),  # 0-byte file
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await setup["storage"]._upload_fileobj(
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

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", 100)),  # Non-zero byte file
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await setup["storage"]._upload_fileobj(
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

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", len(setup["test_data"]))),
        ]

        result = await setup["storage"]._upload_fileobj(
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

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_NOT_FOUND, None),
            (Result.OK, _create_s3_entry("test-file.txt", 0)),  # 0-byte file after upload!
        ]

        with pytest.raises(StorageOperationError, match=r"Upload retry failed.*0 byte file"):
            await setup["storage"]._upload_fileobj(
                setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
            )


@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_retry_access_denied_permanent_error(retry_test_setup):
    """Test that ACCESS_DENIED during retry stat causes immediate failure."""
    setup = retry_test_setup
    mock_upload, call_count = _create_upload_mock(fail_count=1)

    with _RetryTestContext(setup["storage"], mock_upload) as ctx:
        ctx.mock_stat.side_effect = [
            (Result.ERROR_NOT_FOUND, None),
            (Result.ERROR_ACCESS_DENIED, None),  # Permanent error during retry cleanup
        ]

        with pytest.raises(StorageOperationError, match="Access denied"):
            await setup["storage"]._upload_fileobj(
                setup["file_obj"], len(setup["test_data"]), setup["nucleus_hash"], setup["test_url"], True
            )

        assert ctx.mock_sleep.call_count == 1
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

        # Create storage instance with custom env vars
        storage = S3Storage("test-version", False, ThrottlingParameters())
        test_url = "s3://test-bucket/test-file.txt"
        test_data = b"test content"
        file_obj = io.BytesIO(test_data)
        nucleus_hash = "test_hash_123"

        # Mock upload to fail 4 times, succeed on 5th (within custom retry limit)
        mock_upload, call_count = _create_upload_mock(fail_count=4)

        with _RetryTestContext(storage, mock_upload) as ctx:
            ctx.mock_stat.side_effect = [
                (Result.ERROR_NOT_FOUND, None),  # Initial check
                (Result.ERROR_NOT_FOUND, None),  # Retry 1 cleanup
                (Result.ERROR_NOT_FOUND, None),  # Retry 2 cleanup
                (Result.ERROR_NOT_FOUND, None),  # Retry 3 cleanup
                (Result.ERROR_NOT_FOUND, None),  # Retry 4 cleanup
                (Result.OK, _create_s3_entry("test-file.txt", len(test_data))),  # Post-retry verification
            ]

            result = await storage._upload_fileobj(file_obj, len(test_data), nucleus_hash, test_url, True)

            # Verify success with custom retry count
            assert result == Result.OK
            assert call_count[0] == 5

            # Verify custom delay is used with exponential backoff
            assert ctx.mock_sleep.call_count == 4
            sleep_times = [call_args[0][0] for call_args in ctx.mock_sleep.call_args_list]

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
