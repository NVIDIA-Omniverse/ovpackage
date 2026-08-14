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
import sys
import time
from datetime import (
    datetime,
    timezone,
)
from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    patch,
)

import pytest

import wrapp
from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.coro import force_wrapp_thread
from wrapp.utils.nucleus_hash import (
    IncrementalNucleusHasher,
    calc_nucleus_hash_from_stream,
)
from wrapp.utils.router import g_storage_api_enabled
from wrapp.utils.scheduler_task_node import SchedulerTreeNode
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    Router,
    wrapp_read_file_content,
    wrapp_stat,
    wrapp_write_file_bytes,
)
from wrapp.utils.storage_impls.s3_helper import parse_s3_bucket_key
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    StorageMissingDependenciesError,
    StorageOperationError,
    join_paths,
    random_string,
)

from .common import (
    StorageBackendForTests,
    count_open_fds_under,
)

try:
    import grpc

    from wrapp.utils.storage_impls.storageapi import (
        StorageAPIListEntry,
        StorageAPIStorageGRPC,
        _WriteSource,
    )
except (ImportError, ModuleNotFoundError, StorageMissingDependenciesError) as e:
    pytest.skip(f"Missing dependencies for StorageAPI tests: {e}", allow_module_level=True)


@pytest.mark.parametrize("upload_method", ["body", "redirect", "multipart"])
@pytest.mark.asyncio
@force_wrapp_thread
async def test_upload_download_by_size(test_folder_url, upload_method, storage_backend):
    if not g_storage_api_enabled:
        pytest.skip("Storage API has been disabled!")
    if storage_backend.storage_type() not in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
        pytest.skip("test is only for storagapi")
    api = Router.me().storage_api()
    intervals = await api.get_write_type_info(test_folder_url)
    target_url = join_paths(test_folder_url, upload_method + ".txt")
    for interval in intervals:
        if interval["mode"] == upload_method:
            content = random_string(interval["min"]).encode("UTF-8")
            await wrapp_write_file_bytes(wrapp.CommandParameters(), target_url, content)
            result, downloaded_content = await wrapp_read_file_content(target_url)
            assert result == Result.OK
            assert downloaded_content == content
            return
    pytest.skip(f"test did not detect upload size for upload method {upload_method}")


@pytest.mark.asyncio
@force_wrapp_thread
async def test_resource_identity(test_folder_url, storage_backend):
    if not g_storage_api_enabled:
        pytest.skip("Storage API has been disabled!")
    if storage_backend.storage_type() not in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
        pytest.skip("test is only for storagapi")
    target_url = test_folder_url + "/upload.txt"
    for i in range(5):
        content = f"{i}".encode("UTF-8")
        await wrapp_write_file_bytes(wrapp.CommandParameters(), target_url, content)
    # Now stat head, and check the URL
    result, head = await wrapp_stat(target_url)
    assert result == Result.OK
    new_absolute_url, new_relative_path, resource_identity = (
        await Router.me().storage_api().make_resource_identity(target_url, head, "upload.txt")
    )
    assert new_absolute_url == target_url
    assert new_relative_path == "upload.txt"
    result, data = await resource_identity.read_data(Router.me().storage_api())
    assert result == Result.OK
    assert data == "4".encode("UTF-8")

    # Can catalog it as well
    catalog = await wrapp.catalog(test_folder_url)
    assert len(catalog.items) == 1
    assert catalog.items[0].relative_path == "upload.txt"
    assert catalog.items[0].resource_identity == head.version


@pytest.mark.asyncio
@force_wrapp_thread
async def test_s3_url_support(storage_backend, monkeypatch):
    if storage_backend.storage_type() != StorageBackendForTests.STORAGEAPI:
        pytest.skip("Test only valid for StorageAPI")
    monkeypatch.setenv("WRAPP_FIXED_REGION", "us-west-1")
    test_url = "https://wrapptest.s3.us-west-1.amazonaws.com"
    bucket, key, region = parse_s3_bucket_key(test_url)
    storageapi = Router.me().storage_api()
    # Test assumption - the global test data URL comes in the form with bucket name and region
    assert test_url == await storageapi._sanitize_address_for_storage_service(test_url)
    # Then we must be able to determine the
    assert await storageapi._determine_s3_bucket_region(bucket) == region


@pytest.mark.asyncio
async def test_determine_bucket_region_caches(storage_backend):
    if storage_backend.storage_type() != StorageBackendForTests.STORAGEAPI:
        pytest.skip("Test only valid for StorageAPI")

    bucket = "my-bucket"
    region = "eu-central-1"

    # mock aiohttp.ClientSession.head to return a fake response with the header
    fake_resp = AsyncMock()
    fake_resp.__aenter__.return_value.headers = {"x-amz-bucket-region": region}

    storageapi = Router.me().storage_api()

    env = os.environ
    before = dict(env)
    if "WRAPP_FIXED_REGION" in env:
        del env["WRAPP_FIXED_REGION"]
    try:
        with patch("aiohttp.ClientSession.head", return_value=fake_resp) as mock_head:
            # First call should go through aiohttp and fetch the region
            result1 = await storageapi._determine_s3_bucket_region(bucket)
            assert result1 == region
            assert mock_head.call_count == 1

            # Second call should hit the cache, not aiohttp
            result2 = await storageapi._determine_s3_bucket_region(bucket)
            assert result2 == region
            assert mock_head.call_count == 1  # still only one network call
    finally:
        env.clear()
        env.update(before)


@pytest.mark.parametrize(
    "base_resource_address,item_resource_address,expected_relative",
    [
        (
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0",
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0/SubUSDs/textures/a.png",
            "SubUSDs/textures/a.png",
        ),
        (
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0",
            "https://s3.us-west-1.amazonaws.com/wrapptest/metadata_test/.packages/pkg/1.0.0/SubUSDs/textures/a.png",
            "SubUSDs/textures/a.png",
        ),
        (
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0",
            "/metadata_test/.packages/pkg/1.0.0/SubUSDs/textures/a.png",
            "SubUSDs/textures/a.png",
        ),
        (
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0",
            "SubUSDs/textures/a.png",
            "SubUSDs/textures/a.png",
        ),
        (
            "https://sacsapiuswest2.blob.core.windows.net/main/Users/cruch/wrapp_benchmark/.packages/kion_gxo_racks/4.0.0",
            "https://sacsapiuswest2.blob.core.windows.net/main/Users/cruch/wrapp_benchmark/.packages/kion_gxo_racks/4.0.0/Banner/Banner_13/Media%20%281%29.jpg",
            "Banner/Banner_13/Media (1).jpg",
        ),
        (
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0",
            "https://wrapptest.s3.us-west-1.amazonaws.com/metadata_test/.packages/pkg/1.0.0/Mini%2520map/file%25name.txt",
            "Mini%20map/file%name.txt",
        ),
    ],
)
def test_STORAGEAPI_relative_resource_address_normalizes_canonical_forms(
    base_resource_address: str, item_resource_address: str, expected_relative: str
):
    assert StorageAPIListEntry.relative_resource_address(base_resource_address, item_resource_address) == expected_relative


def test_STORAGEAPI_list_entry_nucleus_hash_is_immutable_once_set():
    entry = StorageAPIListEntry("file.usd", None, 1, None, is_folder=False)
    entry.nucleus_hash = "sha-256-flat;1048576;abc123"
    assert entry.nucleus_hash == "sha-256-flat;1048576;abc123"

    with pytest.raises(AttributeError, match="Nucleus hash cannot be changed once set"):
        entry.nucleus_hash = "sha-256-flat;1048576;def456"


def test_STORAGEAPI_list_entry_from_resource_info_converts_timestamp():
    from google.protobuf.timestamp_pb2 import Timestamp

    modified_time = datetime(2026, 4, 27, 9, 25, 58, tzinfo=timezone.utc)
    timestamp = Timestamp()
    timestamp.FromDatetime(modified_time)
    resource_info = SimpleNamespace(
        resource_identity=SimpleNamespace(encoded_identity="identity"),
        metadata=SimpleNamespace(data_object_size=123, last_modified_timestamp=timestamp),
    )

    entry = StorageAPIListEntry.from_resource_info("omniverse://server.example/repo/file.usd", resource_info)

    assert entry.modified_time == modified_time.replace(tzinfo=None)


def test_STORAGEAPI_list_entry_from_resource_info_handles_none_timestamp():
    resource_info = SimpleNamespace(
        resource_identity=SimpleNamespace(encoded_identity="identity"),
        metadata=SimpleNamespace(data_object_size=123, last_modified_timestamp=None),
    )

    entry = StorageAPIListEntry.from_resource_info("omniverse://server.example/repo/file.usd", resource_info)

    assert entry.modified_time is None


def test_STORAGEAPI_list_entry_from_list_item_converts_timestamp():
    from google.protobuf.timestamp_pb2 import Timestamp

    modified_time = datetime(2026, 4, 27, 9, 25, 58, tzinfo=timezone.utc)
    timestamp = Timestamp()
    timestamp.FromDatetime(modified_time)
    item = SimpleNamespace(
        resource_address="omniverse://server.example/repo/file.usd",
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity="identity"),
            metadata=SimpleNamespace(data_object_size=123, last_modified_timestamp=timestamp),
        ),
    )

    entry = StorageAPIListEntry.from_list_item("omniverse://server.example/repo", item)

    assert entry.modified_time == modified_time.replace(tzinfo=None)


def test_STORAGEAPI_list_entry_from_list_item_handles_none_timestamp():
    item = SimpleNamespace(
        resource_address="omniverse://server.example/repo/file.usd",
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity="identity"),
            metadata=SimpleNamespace(data_object_size=123, last_modified_timestamp=None),
        ),
    )

    entry = StorageAPIListEntry.from_list_item("omniverse://server.example/repo", item)

    assert entry.modified_time is None


class _FakeGrpcError(grpc.RpcError):
    def __init__(self, code: grpc.StatusCode, details: str = ""):
        self._code = code
        self._details = details

    def code(self):
        return self._code

    def details(self):
        return self._details


class _FakeWriteResponse:
    def __init__(self, field_name: str):
        self._field_name = field_name

    def HasField(self, name: str):
        return name == self._field_name


class _FakeWriteParameters:
    def __init__(self, destination_resource_address: str, data_object_size: int):
        self.destination_resource_address = destination_resource_address
        self.data_object_size = data_object_size


class _FakeWriteRequest:
    def __init__(self, params=None, chunk=None):
        self.params = params
        self.chunk = chunk


class _FakeChunk:
    def __init__(self, chunk: bytes):
        self.chunk = chunk


def _storageapi_with_fake_write() -> StorageAPIStorageGRPC:
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._fileobject_api = SimpleNamespace(Write=object())
    storage._fileobject_msgs = SimpleNamespace(
        WriteParameters=_FakeWriteParameters,
        WriteRequest=_FakeWriteRequest,
    )
    storage._fileobject_types = SimpleNamespace(Chunk=_FakeChunk)
    return storage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion_error_code",
    [None, grpc.StatusCode.UNIMPLEMENTED, grpc.StatusCode.PERMISSION_DENIED],
    ids=["success", "unimplemented", "permission-denied"],
)
async def test_STORAGEAPI_redirect_upload_completes_with_requested_response_headers_OMPE101972(completion_error_code):
    storage = _storageapi_with_fake_write()
    completion_rpc = object()
    storage._fileobject_api.CompleteRedirectUpload = completion_rpc
    storage._fileobject_msgs.CompleteRedirectUploadRequest = lambda destination_resource_address, additional_headers: SimpleNamespace(
        destination_resource_address=destination_resource_address,
        additional_headers=additional_headers,
    )
    storage._fileobject_types.Header = lambda name, value: SimpleNamespace(name=name, value=value)

    redirect_parameters = SimpleNamespace(
        redirect_target_url="https://upload.example/file.usd",
        completion_header_names=["etag", "x-version-id", "absent"],
    )
    flow_control_message = _FakeWriteResponse("write_redirect")
    flow_control_message.write_redirect = redirect_parameters
    events = []
    completion_requests = []

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        initial_request = await anext(aiter(request_iterator))
        assert initial_request.params.destination_resource_address == "omniverse://server.example/Material%20Library/file.usd"
        yield flow_control_message

    async def _fake_write_via_redirect(parameters, source, sent_hasher):
        assert parameters is redirect_parameters
        with source.open_payload() as payload:
            assert payload == b"content"
        assert sent_hasher is not None
        events.append("http-upload-complete")
        return SimpleNamespace(
            status=204,
            headers={
                "etag": "etag-value",
                "x-version-id": "version-value",
                "unrequested": "ignored-value",
            },
        )

    async def _fake_run_grpc(throttler, grpc_method, request):
        assert throttler is storage._throttler_roundtrip
        assert grpc_method is completion_rpc
        events.append("complete-redirect-upload")
        completion_requests.append(request)
        if completion_error_code is not None:
            raise _FakeGrpcError(completion_error_code, "optional RPC unavailable")

    storage._iterate_grpc = _fake_iterate_grpc
    storage._write_via_redirect = _fake_write_via_redirect
    storage._run_grpc = _fake_run_grpc

    result = await storage.write_file_content("omniverse://server.example/Material Library/file.usd", b"content")

    assert result == Result.OK
    assert events == ["http-upload-complete", "complete-redirect-upload"]
    assert len(completion_requests) == 1
    assert completion_requests[0].destination_resource_address == "omniverse://server.example/Material%20Library/file.usd"
    assert [(header.name, header.value) for header in completion_requests[0].additional_headers] == [
        ("etag", "etag-value"),
        ("x-version-id", "version-value"),
    ]


@pytest.mark.asyncio
async def test_STORAGEAPI_write_file_content_retries_stream_timeout(monkeypatch):
    storage = _storageapi_with_fake_write()
    call_count = 0
    # Keep strong references to the iterators so the interpreter cannot
    # recycle the first iterator's id() once it goes out of scope. On
    # CPython for Windows we have observed id() being reused across
    # generations, which made the "fresh iterator on retry" assertion
    # below flaky.
    request_iterators = []
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        nonlocal call_count
        call_count += 1
        request_iterators.append(request_iterator)
        if call_count == 1:
            raise _FakeGrpcError(grpc.StatusCode.UNKNOWN, "stream timeout")

        initial_request = await anext(aiter(request_iterator))
        assert initial_request.params.destination_resource_address == "omniverse://server.example/Material%20Library/file.usd"
        assert initial_request.params.data_object_size == 3

        yield _FakeWriteResponse("write_chunks_accepted")

        chunks = []
        async for request in request_iterator:
            chunks.append(request.chunk.chunk)
        assert chunks == [b"abc"]
        yield _FakeWriteResponse("resource_info")

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)
    storage._iterate_grpc = _fake_iterate_grpc

    result = await storage.write_file_content("omniverse://server.example/Material Library/file.usd", b"abc")

    assert result == Result.OK
    assert call_count == 2
    assert len(request_iterators) == 2
    assert request_iterators[0] is not request_iterators[1]
    assert sleep_calls == [1.0]


@pytest.mark.asyncio
async def test_STORAGEAPI_write_file_content_encodes_resource_address():
    storage = _storageapi_with_fake_write()

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        initial_request = await anext(aiter(request_iterator))
        assert (
            initial_request.params.destination_resource_address
            == "omniverse://server.example/Material%20Library/Mini%2520map/Media%20%281%29.usd"
        )
        assert initial_request.params.data_object_size == 0
        yield _FakeWriteResponse("resource_info")

    storage._iterate_grpc = _fake_iterate_grpc

    result = await storage.write_file_content("omniverse://server.example/Material Library/Mini%20map/Media (1).usd", b"")

    assert result == Result.OK


@pytest.mark.asyncio
async def test_STORAGEAPI_read_file_content_encodes_resource_address():
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._fileobject_api = SimpleNamespace(ReadFromAddress=object())
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server/Material Library/Mini%20map/file.usd")

    captured_request = []

    async def _fake_iterate_grpc(_throttler, _grpc_method, request):
        captured_request.append(request)
        yield _FakeReply("resource_info", size=0)

    storage._iterate_grpc = _fake_iterate_grpc

    result, _ = await storage.read_file_content("omniverse://server/Material Library/Mini%20map/file.usd")

    assert len(captured_request) == 1
    assert captured_request[0].resource_address == "omniverse://server/Material%20Library/Mini%2520map/file.usd"
    assert result == Result.OK


@pytest.mark.asyncio
async def test_STORAGEAPI_write_file_content_does_not_retry_non_timeout_unknown():
    storage = _storageapi_with_fake_write()
    call_count = 0

    async def _fake_iterate_grpc(_throttler, _grpc_method, _request_iterator):
        nonlocal call_count
        call_count += 1
        raise _FakeGrpcError(grpc.StatusCode.UNKNOWN, "permission denied")
        yield

    storage._iterate_grpc = _fake_iterate_grpc

    with pytest.raises(StorageOperationError, match="Error calling Write"):
        await storage.write_file_content("omniverse://server.example/repo/file.usd", b"abc")

    assert call_count == 1


def _path_to_file_url(path: str) -> str:
    """Convert a filesystem path to a file:// URL, handling Windows correctly."""
    abs_path = os.path.abspath(path)
    if sys.platform == "win32":
        return f"file:///{abs_path.replace(os.sep, '/')}"
    return f"file://{abs_path}"


class _FakeFlowControlMessage:
    """Write flow-control response carrying an arbitrary transport-selection field."""

    def __init__(self, field_name: str, value=None):
        self._field_name = field_name
        if value is not None:
            setattr(self, field_name, value)

    def HasField(self, name: str):
        return name == self._field_name


class _FakeWriteRedirectParams:
    def __init__(self, target_url: str):
        self.redirect_target_url = target_url
        self.additional_headers = []
        self.method = 0
        self.completion_header_names = []


class _FakeUploadResponse:
    status = 200
    headers = {}

    def raise_for_status(self):
        pass

    async def read(self):
        return b""


def _storageapi_for_upload_tests(file_transfer_jobs: int = 2) -> StorageAPIStorageGRPC:
    """StorageAPI backend prepared for driving upload_file without a real server."""
    storage = StorageAPIStorageGRPC(
        "0.0.0-test", False, throttling_params=ThrottlingParameters(file_transfer_jobs=file_transfer_jobs, jobs=8)
    )
    storage._fileobject_api = SimpleNamespace(Write=object(), UploadPart=object(), CompleteMultipartUpload=object())
    storage._fileobject_msgs = SimpleNamespace(
        WriteParameters=_FakeWriteParameters,
        WriteRequest=_FakeWriteRequest,
        UploadPartRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        CompleteMultipartUploadRequest=lambda **kwargs: SimpleNamespace(**kwargs),
        CompletedUploadPart=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    storage._fileobject_types = SimpleNamespace(Chunk=_FakeChunk, Header=lambda **kwargs: SimpleNamespace(**kwargs))
    storage._ensure_grpc_stubs = AsyncMock()
    storage._sanitize_address_for_storage_service = AsyncMock(side_effect=lambda url: url)
    storage._set_nucleus_hash = AsyncMock()
    storage._delete_nucleus_hash = AsyncMock()
    storage._complete_redirect_upload = AsyncMock()
    return storage


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="FD sampling requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_storageapi_upload_grpc_chunks_bounds_open_source_files_OMPE_74369(tmp_path, monkeypatch):
    """gRPC-chunk uploads open the source and stream only under a held NetworkDataTransfer slot.

    Previously upload_file read the whole file into memory before any throttling and
    the chunk loop ran with no slot held. Now at most file_transfer_jobs source files
    are open at once and the streaming phase itself holds the slot.
    """
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi._STORAGEAPI_WRITE_CHUNK_SIZE", 4)
    n_uploads = 6
    file_transfer_jobs = 2
    storage = _storageapi_for_upload_tests(file_transfer_jobs)

    consume_may_proceed = asyncio.Event()
    received = {}

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        # Consume the request stream in a background task like the real gRPC machinery,
        # but gated: with chunk size 4 and 60-byte files the Queue(5) fills, so the
        # admitted producers block holding their slot with the source file open -- a
        # deterministic sampling point.
        initial_request = await anext(aiter(request_iterator))
        chunks = []
        drained = asyncio.Event()

        async def _drain():
            await consume_may_proceed.wait()
            async for request in request_iterator:
                chunks.append(request.chunk.chunk)
            drained.set()

        drain_task = asyncio.create_task(_drain())
        try:
            yield _FakeWriteResponse("write_chunks_accepted")
            await drained.wait()
            received[initial_request.params.destination_resource_address] = b"".join(chunks)
            yield _FakeWriteResponse("resource_info")
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

    storage._iterate_grpc = _fake_iterate_grpc

    content_by_address = {}
    uploads = []
    for i in range(n_uploads):
        src = tmp_path / f"src{i}.bin"
        data = bytes([i]) * 60  # 15 chunks of 4 bytes >> request queue capacity 5
        src.write_bytes(data)
        dst_url = f"storageapi://server/dst{i}.bin"
        content_by_address[dst_url] = data
        uploads.append(asyncio.create_task(storage.upload_file(_path_to_file_url(str(src)), dst_url, overwrite=True)))

    # Let the event loop run until the two slot-holders block on the full queue.
    for _ in range(2000):
        await asyncio.sleep(0)
        if count_open_fds_under(str(tmp_path), ".bin") >= file_transfer_jobs:
            break
    # Give any (incorrectly) unbounded extra tasks a chance to slip past the throttler.
    for _ in range(100):
        await asyncio.sleep(0)

    assert count_open_fds_under(str(tmp_path), ".bin") == file_transfer_jobs
    # The whole chunk-streaming phase runs under held slots.
    assert len(storage._throttler_networkdata.processing_tasks) == file_transfer_jobs

    consume_may_proceed.set()
    results = await asyncio.gather(*uploads)
    assert all(r == Result.OK for r in results)
    assert received == content_by_address
    # The stored hash is computed from the bytes each stream actually sent.
    hash_by_address = {call.args[0]: call.args[1] for call in storage._set_nucleus_hash.await_args_list}
    for address, data in content_by_address.items():
        expected_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(data))
        assert hash_by_address[address] == expected_hash
    storage._delete_nucleus_hash.assert_not_awaited()


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="FD sampling requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_storageapi_upload_redirect_streams_file_object_OMPE_74369(tmp_path):
    """Redirect uploads pass an open file object to aiohttp instead of the file's bytes.

    The file is opened only inside the throttled request (bounding FDs) and streamed
    by aiohttp, so the whole file is never materialized in memory.
    """
    n_uploads = 6
    file_transfer_jobs = 2
    storage = _storageapi_for_upload_tests(file_transfer_jobs)
    storage._upload_method_to_string = lambda _method: "PUT"

    uploads_may_proceed = asyncio.Event()
    recorded = []

    class _FakeRequestContext:
        def __init__(self, data):
            self._data = data

        async def __aenter__(self):
            recorded.append(
                {
                    "data": self._data,
                    "is_bytes": isinstance(self._data, (bytes, bytearray)),
                    "pos": self._data.tell() if hasattr(self._data, "tell") else None,
                }
            )
            await uploads_may_proceed.wait()
            return _FakeUploadResponse()

        async def __aexit__(self, *_exc):
            return False

    class _FakeSession:
        def request(self, method, url, headers, data):
            return _FakeRequestContext(data)

    storage._ensure_aiohttp_session = AsyncMock(return_value=_FakeSession())

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        await anext(aiter(request_iterator))
        yield _FakeFlowControlMessage("write_redirect", _FakeWriteRedirectParams("https://upload.example.com/obj"))

    storage._iterate_grpc = _fake_iterate_grpc

    uploads = []
    for i in range(n_uploads):
        src = tmp_path / f"src{i}.bin"
        src.write_bytes(b"payload-%d" % i)
        uploads.append(
            asyncio.create_task(storage.upload_file(_path_to_file_url(str(src)), f"storageapi://server/dst{i}.bin", overwrite=True))
        )

    for _ in range(2000):
        await asyncio.sleep(0)
        if len(recorded) >= file_transfer_jobs:
            break
    for _ in range(100):
        await asyncio.sleep(0)

    assert len(recorded) == file_transfer_jobs
    assert count_open_fds_under(str(tmp_path), ".bin") == file_transfer_jobs

    uploads_may_proceed.set()
    results = await asyncio.gather(*uploads)
    assert all(r == Result.OK for r in results)
    assert len(recorded) == n_uploads
    for entry in recorded:
        # An open file object at position 0, not bytes: aiohttp streams it and derives
        # Content-Length via fstat; the content was never read into memory by wrapp.
        assert not entry["is_bytes"]
        assert entry["pos"] == 0
        assert hasattr(entry["data"], "read")
        assert hasattr(entry["data"], "fileno")
        # The wrapper hides the local filename so aiohttp cannot derive MIME-type /
        # Content-Disposition headers from it (wire parity with a bytes body).
        assert not hasattr(entry["data"], "name")
    # The fake session never read the bodies, so the sent bytes were not observed:
    # no hash may be stored (never hash the mutable path after the fact), and any
    # previous hash on the overwritten destinations must be removed.
    storage._set_nucleus_hash.assert_not_awaited()
    deleted_addresses = {call.args[0] for call in storage._delete_nucleus_hash.await_args_list}
    assert deleted_addresses == {f"storageapi://server/dst{i}.bin" for i in range(n_uploads)}


@pytest.mark.asyncio
async def test_storageapi_upload_multipart_reads_parts_lazily_OMPE_74369(tmp_path):
    """Multipart uploads read each part from disk as bounded bytes, never the whole file."""
    storage = _storageapi_for_upload_tests()
    storage._upload_method_to_string = lambda _method: "PUT"

    content = b"0123456789"  # part size 4 -> parts of 4, 4, 2 bytes
    max_part_size = 4
    src = tmp_path / "src.bin"
    src.write_bytes(content)

    recorded = []

    class _FakeRequestContext:
        def __init__(self, data):
            self._data = data

        async def __aenter__(self):
            recorded.append(self._data)
            return _FakeUploadResponse()

        async def __aexit__(self, *_exc):
            return False

    class _FakeSession:
        def request(self, method, url, headers, data):
            return _FakeRequestContext(data)

    storage._ensure_aiohttp_session = AsyncMock(return_value=_FakeSession())

    class _FakeMultipartParams:
        first_part_write_redirect = _FakeWriteRedirectParams("https://upload.example.com/part0")
        maximum_size_per_part = max_part_size
        upload_id = "upload-id"

        def HasField(self, name: str):
            return name == "maximum_size_per_part"

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        await anext(aiter(request_iterator))
        yield _FakeFlowControlMessage("multipart_upload", _FakeMultipartParams())

    storage._iterate_grpc = _fake_iterate_grpc

    grpc_calls = []

    async def _fake_run_grpc(_throttler, grpc_method, request):
        grpc_calls.append((grpc_method, request))
        if grpc_method is storage._fileobject_api.UploadPart:
            return SimpleNamespace(
                part_write_redirects=[
                    _FakeWriteRedirectParams(f"https://upload.example.com/part{n}") for n in range(1, request.part_count + 1)
                ]
            )
        return SimpleNamespace()

    storage._run_grpc = _fake_run_grpc

    result = await storage.upload_file(_path_to_file_url(str(src)), "storageapi://server/dst.bin", overwrite=True)

    assert result == Result.OK
    # Three bounded parts, each read from disk only for its own throttled request.
    assert recorded == [content[0:4], content[4:8], content[8:10]]
    assert all(isinstance(part, bytes) and len(part) <= max_part_size for part in recorded)
    assert grpc_calls[-1][0] is storage._fileobject_api.CompleteMultipartUpload
    # The stored hash covers the sent parts, in order.
    expected_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(content))
    storage._set_nucleus_hash.assert_awaited_once_with("storageapi://server/dst.bin", expected_hash)


@pytest.mark.asyncio
async def test_storageapi_upload_file_does_not_materialize_content_OMPE_74369(tmp_path):
    """upload_file passes a path-backed source along; it never reads the file into memory."""
    storage = _storageapi_for_upload_tests()

    captured = []
    sent_hash = "sha-256-flat;1048576;fakehash"

    async def _fake_write_source_content(resource_address, source):
        captured.append((resource_address, source))
        return Result.OK, sent_hash

    storage._write_source_content = _fake_write_source_content

    async def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("upload_file must not route through the bytes-based write_file")

    storage.write_file = _must_not_be_called

    content = b"0123456789" * 100
    src = tmp_path / "src.bin"
    src.write_bytes(content)

    result = await storage.upload_file(_path_to_file_url(str(src)), "storageapi://server/dst.bin", overwrite=True)

    assert result == Result.OK
    assert len(captured) == 1
    address, source = captured[0]
    assert address == "storageapi://server/dst.bin"
    # Path-backed: the content stays on disk until a transfer slot is held.
    # get_path_from_url returns forward slashes on Windows; compare normalized.
    assert source._path is not None and os.path.normpath(source._path) == os.path.normpath(str(src))
    assert source._data is None
    assert source.size == len(content)
    # The stored hash is the one computed from the bytes the transfer sent.
    storage._set_nucleus_hash.assert_awaited_once_with("storageapi://server/dst.bin", sent_hash)


def test_storageapi_write_source_rejects_changed_file_OMPE_74369(tmp_path):
    """A path-backed source refuses to open a file that changed since it was hashed.

    Otherwise the upload would send bytes that no longer match the nucleus hash
    computed before throttling and the advertised data_object_size.
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"original")
    source = _WriteSource.from_path(str(src))
    part = source.slice(0, 4)

    src.write_bytes(b"changed content with a different size")

    with pytest.raises(StorageOperationError, match="changed while the upload was queued"):
        with source.open():
            pass
    with pytest.raises(StorageOperationError, match="changed while the upload was queued"):
        part.read_bytes()
    with pytest.raises(StorageOperationError, match="changed while the upload was queued"):
        with source.open_payload():
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="detection of a same-size rewrite relies on POSIX ctime semantics")
def test_storageapi_write_source_rejects_same_size_rewrite_with_restored_mtime_OMPE_74369(tmp_path):
    """Even a same-size rewrite that restores mtime is rejected via the ctime check."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"original")
    st = os.stat(src)
    source = _WriteSource.from_path(str(src))

    # ctime is set from a coarse kernel clock: a rewrite within the same tick
    # keeps the original ctime and is legitimately indistinguishable. Repeat
    # until the clock has advanced so the test exercises the detectable case.
    for _ in range(500):
        src.write_bytes(b"ORIGINAL")  # same size, different bytes
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
        if os.stat(src).st_ctime_ns != st.st_ctime_ns:
            break
        time.sleep(0.01)
    else:
        pytest.skip("filesystem ctime granularity too coarse to observe the rewrite")

    with pytest.raises(StorageOperationError, match="changed while the upload was queued"):
        with source.open():
            pass


def test_incremental_nucleus_hasher_matches_stream_hash_OMPE_74369():
    """Feeding arbitrary chunk sizes yields exactly calc_nucleus_hash_from_stream's result."""
    one_mib = 1024 * 1024
    pattern = b"0123456789abcdef" * 4096  # 64 KiB
    for size in (0, 1, one_mib - 1, one_mib, one_mib + 1, 2 * one_mib + 12345):
        data = (pattern * (size // len(pattern) + 1))[:size]
        expected = calc_nucleus_hash_from_stream(io.BytesIO(data))
        hasher = IncrementalNucleusHasher()
        step = 65537  # deliberately misaligned with the 1 MiB hash blocks
        for i in range(0, len(data), step):
            hasher.update(data[i : i + step])
        assert hasher.finalize() == expected
        # finalize() is idempotent
        assert hasher.finalize() == expected


@pytest.mark.asyncio
async def test_storageapi_upload_stores_hash_of_sent_bytes_even_when_change_is_undetectable_OMPE_74369(tmp_path):
    """A source change that evades stat detection cannot make the stored hash stale.

    Simulates the Windows corner (in-place same-size rewrite with restored
    mtime): the stat snapshot passes, the changed bytes are uploaded — and the
    stored hash is the hash of exactly those uploaded bytes.
    """
    storage = _storageapi_for_upload_tests()

    received = {}

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        initial_request = await anext(aiter(request_iterator))
        chunks = []
        drained = asyncio.Event()

        async def _drain():
            async for request in request_iterator:
                chunks.append(request.chunk.chunk)
            drained.set()

        drain_task = asyncio.create_task(_drain())
        try:
            yield _FakeWriteResponse("write_chunks_accepted")
            await drained.wait()
            received[initial_request.params.destination_resource_address] = b"".join(chunks)
            yield _FakeWriteResponse("resource_info")
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except asyncio.CancelledError:
                pass

    storage._iterate_grpc = _fake_iterate_grpc

    original = b"original"
    changed = b"CHANGED!"  # same size
    src = tmp_path / "src.bin"
    src.write_bytes(original)
    source = _WriteSource.from_path(str(src))
    src.write_bytes(changed)
    source._snapshot = None  # the rewrite is undetectable via stat on this platform

    result, sent_hash = await storage._write_source_content("storageapi://server/dst.bin", source)

    assert result == Result.OK
    assert received["storageapi://server/dst.bin"] == changed
    expected_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(changed))
    assert sent_hash == expected_hash


@pytest.mark.asyncio
async def test_storageapi_redirect_upload_hash_matches_streamed_bytes_OMPE_74369(tmp_path):
    """A redirect upload's stored hash is computed from the streamed request body."""
    storage = _storageapi_for_upload_tests()
    storage._upload_method_to_string = lambda _method: "PUT"

    bodies = []

    class _ReadingRequestContext:
        def __init__(self, data):
            self._data = data

        async def __aenter__(self):
            # Consume the body the way aiohttp does: sequential bounded reads.
            chunks = []
            while True:
                chunk = self._data.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            bodies.append(b"".join(chunks))
            return _FakeUploadResponse()

        async def __aexit__(self, *_exc):
            return False

    class _FakeSession:
        def request(self, method, url, headers, data):
            return _ReadingRequestContext(data)

    storage._ensure_aiohttp_session = AsyncMock(return_value=_FakeSession())

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        await anext(aiter(request_iterator))
        yield _FakeFlowControlMessage("write_redirect", _FakeWriteRedirectParams("https://upload.example.com/obj"))

    storage._iterate_grpc = _fake_iterate_grpc

    content = (b"0123456789abcdef" * 4096) * 32 + b"tail"  # > 2 MiB, crosses hash-block boundaries
    src = tmp_path / "src.bin"
    src.write_bytes(content)

    result = await storage.upload_file(_path_to_file_url(str(src)), "storageapi://server/dst.bin", overwrite=True)

    assert result == Result.OK
    assert bodies == [content]
    expected_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(content))
    storage._set_nucleus_hash.assert_awaited_once_with("storageapi://server/dst.bin", expected_hash)


@pytest.mark.asyncio
async def test_storageapi_zero_byte_upload_validates_changed_source_OMPE_74369(tmp_path):
    """A zero-byte upload completed server-side re-validates the source it never opened."""
    storage = _storageapi_for_upload_tests()

    server_may_respond = asyncio.Event()

    async def _fake_iterate_grpc(_throttler, _grpc_method, request_iterator):
        initial_request = await anext(aiter(request_iterator))
        assert initial_request.params.data_object_size == 0
        await server_may_respond.wait()
        yield _FakeWriteResponse("resource_info")

    storage._iterate_grpc = _fake_iterate_grpc

    src = tmp_path / "src.bin"
    src.write_bytes(b"")
    upload = asyncio.create_task(storage.upload_file(_path_to_file_url(str(src)), "storageapi://server/dst.bin", overwrite=True))
    # Let the upload snapshot the empty file and wait on the server response...
    for _ in range(100):
        await asyncio.sleep(0)
    # ...then grow the source before the server confirms the 0-byte write.
    src.write_bytes(b"no longer empty")
    server_may_respond.set()

    with pytest.raises(StorageOperationError, match="changed while the upload was queued"):
        await upload

    # An unchanged 0-byte source still uploads fine.
    src2 = tmp_path / "src2.bin"
    src2.write_bytes(b"")
    result = await storage.upload_file(_path_to_file_url(str(src2)), "storageapi://server/dst2.bin", overwrite=True)
    assert result == Result.OK


@pytest.mark.asyncio
async def test_storageapi_redirect_download_opens_destination_inside_slot_OMPE_74369(tmp_path):
    """The redirect download opens its destination only once it holds the transfer slot."""
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters(file_transfer_jobs=1, jobs=8))
    payload = b"redirect-payload"

    class _FakeContent:
        def iter_chunked(self, _n):
            async def _gen():
                yield payload

            return _gen()

    class _FakeGetResponse:
        content = _FakeContent()

        def raise_for_status(self):
            pass

    class _FakeGetContext:
        async def __aenter__(self):
            return _FakeGetResponse()

        async def __aexit__(self, *_exc):
            return False

    class _FakeSession:
        def get(self, _url, headers=None):
            return _FakeGetContext()

    slot_may_release = asyncio.Event()

    async def _occupy_slot():
        await slot_may_release.wait()

    holder = asyncio.create_task(storage._throttler_networkdata.run(_occupy_slot()))

    dst = tmp_path / "dst.bin"
    download = asyncio.create_task(
        storage._download_redirect_single_stream_to_file(
            _FakeSession(), "https://download.example.com/obj", {}, str(dst), expected_size=len(payload)
        )
    )
    for _ in range(200):
        await asyncio.sleep(0)
    # The single slot is occupied: the pending download must not have created the destination.
    assert not dst.exists()

    slot_may_release.set()
    await holder
    await download
    assert dst.read_bytes() == payload


@pytest.mark.asyncio
async def test_STORAGEAPI_delete_tree_resolves_relative_paths_before_delete():
    class _DummyStorageAPI:
        async def list_file_tree(self, _url):
            yield StorageAPIListEntry(".subpackage.wrapp", None, None, None, is_folder=False)
            yield StorageAPIListEntry("testfile.txt", None, None, None, is_folder=False)
            yield StorageAPIListEntry("omniverse://server.example/repo/already-absolute.txt", None, None, None, is_folder=False)

        async def delete_item(self, url):
            deleted.append(url)
            return Result.OK

    deleted = []
    storage = _DummyStorageAPI()
    root = SchedulerTreeNode.create_root("test")
    parent = root.create_task_group_node("test-group")

    result = await StorageAPIStorageGRPC.delete_tree(storage, "omniverse://server.example/repo", parent)

    assert result == Result.OK
    assert set(deleted) == {
        "omniverse://server.example/repo/.subpackage.wrapp",
        "omniverse://server.example/repo/testfile.txt",
        "omniverse://server.example/repo/already-absolute.txt",
    }


# @pytest.mark.asyncio
# async def test_storageapi_auth():
#    api = StorageAPIStorageGRPC(wrapp_version="0.0.0", debug_level=True)
#    result = await api.stat("//np-sjc6c-br2-063-a/hello.txt")
#    print(result)


class _FakeMetadata:
    def __init__(self, size: int):
        self.data_object_size = size

    def HasField(self, name: str) -> bool:
        return name == "data_object_size"


class _FakeReply:
    def __init__(
        self, field_name: str, *, size: int = 0, chunk: bytes = b"", redirect_url: str = "", headers: dict[str, str] | None = None
    ):
        self._field_name = field_name
        if field_name == "resource_info":
            self.resource_info = SimpleNamespace(metadata=_FakeMetadata(size))
        elif field_name == "metadata":
            self.metadata = _FakeMetadata(size)
        elif field_name == "chunk":
            self.chunk = SimpleNamespace(chunk=chunk)
        elif field_name == "redirect":
            self.redirect = SimpleNamespace(
                redirect_target_url=redirect_url,
                additional_headers=[SimpleNamespace(name=k, value=v) for k, v in (headers or {}).items()],
            )

    def HasField(self, name: str) -> bool:
        return self._field_name == name


class _FakeResponseContent:
    def __init__(self, body: bytes):
        self._body = body

    async def iter_chunked(self, chunk_size: int):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self._body = body
        self.content = _FakeResponseContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def read(self):
        return self._body


class _FakeRangeSession:
    def __init__(self, payload: bytes):
        self._payload = payload

    def get(self, _url: str, headers: dict[str, str]):
        range_header = headers["Range"]
        assert range_header.startswith("bytes=")
        start_s, end_s = range_header[len("bytes=") :].split("-")
        start = int(start_s)
        end = int(end_s)
        body = self._payload[start : end + 1]
        content_range = f"bytes {start}-{end}/{len(self._payload)}"
        return _FakeResponse(status=206, headers={"Content-Range": content_range}, body=body)


@pytest.mark.asyncio
async def test_storageapi_download_to_file_streams_grpc_chunks_to_disk(tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())

    async def _fake_read_from_address(_request, metadata=None):
        yield _FakeReply("resource_info", size=11)
        yield _FakeReply("chunk", chunk=b"hello ")
        yield _FakeReply("chunk", chunk=b"world")

    storage._fileobject_api = SimpleNamespace(ReadFromAddress=_fake_read_from_address)
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._grpc_metadata = AsyncMock(return_value=None)
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")

    dst_path = tmp_path / "downloaded.bin"
    result = await storage._download_to_file("omniverse://server.example/repo/file.bin", str(dst_path))

    assert result == Result.OK
    assert dst_path.read_bytes() == b"hello world"


@pytest.mark.asyncio
async def test_storageapi_download_to_file_retries_grpc_stream_reset(monkeypatch, tmp_path):
    Stats.get().clear()
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    call_count = 0
    sleep_calls = []
    dst_path = tmp_path / "downloaded.bin"
    dst_path.write_bytes(b"old")

    async def _fake_sleep(delay):
        assert dst_path.read_bytes() == b"old"
        sleep_calls.append(delay)

    async def _fake_read_from_address(_request, metadata=None):
        nonlocal call_count
        call_count += 1
        yield _FakeReply("resource_info", size=5)
        if call_count == 1:
            yield _FakeReply("chunk", chunk=b"bad")
            raise _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "recvmsg:Connection reset by peer")
        yield _FakeReply("chunk", chunk=b"hello")

    storage._fileobject_api = SimpleNamespace(ReadFromAddress=_fake_read_from_address)
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._grpc_metadata = AsyncMock(return_value=None)
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)

    result = await storage._download_to_file("omniverse://server.example/repo/file.bin", str(dst_path))

    assert result == Result.OK
    assert dst_path.read_bytes() == b"hello"
    assert list(tmp_path.glob("*.wrapp-download-*.tmp")) == []
    assert call_count == 2
    assert sleep_calls == [1.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="FD sampling requires /proc/self/fd (Linux)")
@pytest.mark.asyncio
async def test_storageapi_grpc_download_bounds_open_destination_files_OMPE_74369(tmp_path):
    """gRPC-chunk downloads hold one networkdata slot for the whole stream.

    The destination is opened only under that held slot, so at most
    file_transfer_jobs destination files are open at once. Previously the slot
    was acquired per reply and released in between, leaving the fd bounded only
    by admission rate.
    """
    n_downloads = 6
    file_transfer_jobs = 2
    payload = b"grpc-chunk-payload"

    reached_streams = 0
    streams_may_finish = asyncio.Event()  # never set until the assert -> streams hang holding their slot

    storage = StorageAPIStorageGRPC(
        "0.0.0-test", False, throttling_params=ThrottlingParameters(file_transfer_jobs=file_transfer_jobs, jobs=8)
    )

    async def _fake_read_from_address(_request, metadata=None):
        nonlocal reached_streams
        yield _FakeReply("resource_info", size=len(payload))
        yield _FakeReply("chunk", chunk=payload[:4])
        reached_streams += 1
        # Keep the stream alive cooperatively (empty chunks) instead of blocking:
        # a per-reply-throttled implementation would interleave all 6 downloads
        # here and open all 6 destinations, while the single-held-slot
        # implementation admits only file_transfer_jobs streams at all.
        while not streams_may_finish.is_set():
            await asyncio.sleep(0)
            yield _FakeReply("chunk", chunk=b"")
        yield _FakeReply("chunk", chunk=payload[4:])

    storage._fileobject_api = SimpleNamespace(ReadFromAddress=_fake_read_from_address)
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._grpc_metadata = AsyncMock(return_value=None)
    storage._sanitize_address_for_storage_service = AsyncMock(side_effect=lambda url: url)

    downloads = []
    for i in range(n_downloads):
        dst = tmp_path / f"dst{i}.bin"
        downloads.append(
            asyncio.create_task(storage.download_file(f"storageapi://server/file{i}.bin", _path_to_file_url(str(dst)), overwrite=True))
        )

    for _ in range(2000):
        await asyncio.sleep(0)
        if reached_streams >= file_transfer_jobs:
            break
    # Give any (incorrectly) unbounded extra tasks a chance to slip past the throttler.
    for _ in range(100):
        await asyncio.sleep(0)

    assert reached_streams == file_transfer_jobs
    # Downloads write to <dst>.wrapp-download-<token>.tmp before the final os.replace.
    assert count_open_fds_under(str(tmp_path), ".tmp") == file_transfer_jobs
    assert len(storage._throttler_networkdata.processing_tasks) == file_transfer_jobs

    streams_may_finish.set()
    results = await asyncio.gather(*downloads)
    assert all(r == Result.OK for r in results)
    for i in range(n_downloads):
        assert (tmp_path / f"dst{i}.bin").read_bytes() == payload


@pytest.mark.asyncio
async def test_storageapi_grpc_redirect_reply_is_handled_outside_the_slot_OMPE_74369(tmp_path):
    """A redirect reply ends the throttled stream before the redirect download runs.

    The redirect download acquires its own networkdata slots; with
    file_transfer_jobs=1 it would deadlock (or trip the same-throttler re-entry
    guard) if the stream's slot were still held.
    """
    payload = b"redirect payload"
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters(file_transfer_jobs=1, jobs=8))

    async def _fake_read_from_address(_request, metadata=None):
        yield _FakeReply("resource_info", size=len(payload))
        yield _FakeReply("redirect", redirect_url="https://download.example/file", headers={})

    storage._fileobject_api = SimpleNamespace(ReadFromAddress=_fake_read_from_address)
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._grpc_metadata = AsyncMock(return_value=None)
    storage._sanitize_address_for_storage_service = AsyncMock(side_effect=lambda url: url)
    storage._ensure_aiohttp_session = AsyncMock(return_value=object())

    observed_slots = []

    async def _fake_download_redirect_to_file(*, session, redirect_target_url, headers, file_path, expected_size):
        observed_slots.append(len(storage._throttler_networkdata.processing_tasks))
        with open(file_path, "wb") as f:
            f.write(payload)

    storage._download_redirect_to_file = _fake_download_redirect_to_file

    dst = tmp_path / "dst.bin"
    result = await storage.download_file("storageapi://server/file.bin", _path_to_file_url(str(dst)), overwrite=True)

    assert result == Result.OK
    assert dst.read_bytes() == payload
    # The stream's slot was released before the redirect download started.
    assert observed_slots == [0]


@pytest.mark.asyncio
async def test_storageapi_grpc_download_fails_on_size_mismatch_OMPE_74369(tmp_path):
    """A chunk stream that ends cleanly but short of the declared size fails the download."""
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())

    async def _fake_read_from_address(_request, metadata=None):
        yield _FakeReply("resource_info", size=10)
        yield _FakeReply("chunk", chunk=b"short")  # 5 of the declared 10 bytes

    storage._fileobject_api = SimpleNamespace(ReadFromAddress=_fake_read_from_address)
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._grpc_metadata = AsyncMock(return_value=None)
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")

    dst_path = tmp_path / "downloaded.bin"
    result = await storage._download_to_file("omniverse://server.example/repo/file.bin", str(dst_path))

    assert result == Result.ERROR
    # The partial temp file was removed and no destination was created.
    assert not dst_path.exists()
    assert list(tmp_path.glob("*.wrapp-download-*.tmp")) == []


@pytest.mark.asyncio
async def test_storageapi_read_file_content_uses_optimized_redirect_download(tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._fileobject_api = SimpleNamespace(ReadFromAddress=object())
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")
    storage._ensure_aiohttp_session = AsyncMock(return_value=object())
    payload = b"redirect payload"
    download_calls = []

    async def _fake_iterate_grpc(*_args, **_kwargs):
        yield _FakeReply("resource_info", size=len(payload))
        yield _FakeReply("redirect", redirect_url="https://download.example/file", headers={"authorization": "Bearer token"})

    async def _fake_download_redirect_to_file(*, session, redirect_target_url, headers, file_path, expected_size):
        download_calls.append((session, redirect_target_url, headers, expected_size))
        with open(file_path, "wb") as f:
            f.write(payload)

    storage._iterate_grpc = _fake_iterate_grpc
    storage._download_redirect_to_file = _fake_download_redirect_to_file

    result, content = await storage.read_file_content("omniverse://server.example/repo/file.bin")

    assert result == Result.OK
    assert content == payload
    assert download_calls == [
        (storage._ensure_aiohttp_session.return_value, "https://download.example/file", {"authorization": "Bearer token"}, len(payload))
    ]


@pytest.mark.asyncio
async def test_storageapi_read_file_content_retries_grpc_stream_reset(monkeypatch):
    Stats.get().clear()
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._fileobject_api = SimpleNamespace(ReadFromAddress=object())
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")
    call_count = 0
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    async def _fake_iterate_grpc(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "recvmsg:Connection reset by peer")
        yield _FakeReply("resource_info", size=2)
        yield _FakeReply("chunk", chunk=b"ok")

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)
    storage._iterate_grpc = _fake_iterate_grpc

    result, content = await storage.read_file_content("omniverse://server.example/repo/file.bin")

    assert result == Result.OK
    assert content == b"ok"
    assert call_count == 2
    assert sleep_calls == [1.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.asyncio
async def test_storageapi_read_file_content_respects_read_retries_env(monkeypatch):
    Stats.get().clear()
    monkeypatch.setenv("WRAPP_STORAGE_API_READ_RETRIES", "1")
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    assert storage._read_max_attempts == 1
    storage._fileobject_api = SimpleNamespace(ReadFromAddress=object())
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")
    call_count = 0
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    async def _fake_iterate_grpc(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "recvmsg:Connection reset by peer")
        yield _FakeReply("resource_info", size=0)

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)
    storage._iterate_grpc = _fake_iterate_grpc

    with pytest.raises(StorageOperationError, match="Error calling read"):
        await storage.read_file_content("omniverse://server.example/repo/file.bin")

    assert call_count == 1
    assert sleep_calls == []
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 0


@pytest.mark.asyncio
async def test_storageapi_read_resource_identity_uses_optimized_redirect_download():
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._fileobject_api = SimpleNamespace(Read=object())
    storage._fileobject_msgs = SimpleNamespace(ReadRequest=lambda resource_identity: SimpleNamespace(resource_identity=resource_identity))
    storage._fileobject_types = SimpleNamespace(
        ResourceIdentity=lambda encoded_identity: SimpleNamespace(encoded_identity=encoded_identity)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._ensure_aiohttp_session = AsyncMock(return_value=object())
    payload = b"identity payload"
    download_calls = []

    async def _fake_iterate_grpc(*_args, **_kwargs):
        yield _FakeReply("metadata", size=len(payload))
        yield _FakeReply("redirect", redirect_url="https://download.example/by-identity", headers={"authorization": "Bearer token"})

    async def _fake_download_redirect_to_file(*, session, redirect_target_url, headers, file_path, expected_size):
        download_calls.append((session, redirect_target_url, headers, expected_size))
        with open(file_path, "wb") as f:
            f.write(payload)

    storage._iterate_grpc = _fake_iterate_grpc
    storage._download_redirect_to_file = _fake_download_redirect_to_file

    result, content = await storage.read_resource_identity("encoded-identity")

    assert result == Result.OK
    assert content == payload
    assert download_calls == [
        (
            storage._ensure_aiohttp_session.return_value,
            "https://download.example/by-identity",
            {"authorization": "Bearer token"},
            len(payload),
        )
    ]


@pytest.mark.asyncio
async def test_storageapi_read_resource_identity_retries_grpc_stream_reset(monkeypatch):
    Stats.get().clear()
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._fileobject_api = SimpleNamespace(Read=object())
    storage._fileobject_msgs = SimpleNamespace(ReadRequest=lambda resource_identity: SimpleNamespace(resource_identity=resource_identity))
    storage._fileobject_types = SimpleNamespace(
        ResourceIdentity=lambda encoded_identity: SimpleNamespace(encoded_identity=encoded_identity)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    call_count = 0
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    async def _fake_iterate_grpc(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _FakeGrpcError(grpc.StatusCode.UNAVAILABLE, "recvmsg:Connection reset by peer")
        yield _FakeReply("metadata", size=8)
        yield _FakeReply("chunk", chunk=b"identity")

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)
    storage._iterate_grpc = _fake_iterate_grpc

    result, content = await storage.read_resource_identity("encoded-identity")

    assert result == Result.OK
    assert content == b"identity"
    assert call_count == 2
    assert sleep_calls == [1.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.asyncio
async def test_storageapi_redirect_range_download_writes_expected_file(tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._download_range_part_size = 4
    storage._download_range_max_concurrency = 2
    payload = b"abcdefghij"
    session = _FakeRangeSession(payload)
    dst_path = tmp_path / "range.bin"

    await storage._download_redirect_ranges_to_file(
        session=session,
        redirect_target_url="https://download.example/file",
        headers={"authorization": "Bearer token"},
        file_path=str(dst_path),
        total_size=len(payload),
    )

    assert dst_path.read_bytes() == payload


@pytest.mark.asyncio
async def test_storageapi_redirect_download_falls_back_to_single_stream(tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._enable_range_download = True
    storage._probe_redirect_range_download_size = AsyncMock(return_value=None)
    storage._download_redirect_ranges_to_file = AsyncMock()
    storage._download_redirect_single_stream_to_file = AsyncMock()

    await storage._download_redirect_to_file(
        session=object(),
        redirect_target_url="https://download.example/file",
        headers={"authorization": "Bearer token"},
        file_path=str(tmp_path / "fallback.bin"),
        expected_size=10,
    )

    storage._download_redirect_ranges_to_file.assert_not_awaited()
    storage._download_redirect_single_stream_to_file.assert_awaited_once()


@pytest.mark.asyncio
async def test_storageapi_redirect_download_retries_transient_failure(monkeypatch, tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._enable_range_download = True
    storage._probe_redirect_range_download_size = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
    storage._download_redirect_single_stream_to_file = AsyncMock()
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)

    await storage._download_redirect_to_file(
        session=object(),
        redirect_target_url="https://download.example/file",
        headers={"authorization": "Bearer token"},
        file_path=str(tmp_path / "retry.bin"),
        expected_size=10,
    )

    assert storage._probe_redirect_range_download_size.await_count == 2
    storage._download_redirect_single_stream_to_file.assert_awaited_once()
    assert sleep_calls == [1.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "retries_env_var",
    ["WRAPP_STORAGE_API_DOWNLOAD_RETRIES", "WRAPP_STORAGEAPI_DOWNLOAD_RETRIES"],
)
async def test_storageapi_redirect_download_retries_configurable(monkeypatch, tmp_path, retries_env_var):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._enable_range_download = True
    storage._probe_redirect_range_download_size = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
    storage._download_redirect_single_stream_to_file = AsyncMock()
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setenv(retries_env_var, "2")
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)

    await storage._download_redirect_to_file(
        session=object(),
        redirect_target_url="https://download.example/file",
        headers={"authorization": "Bearer token"},
        file_path=str(tmp_path / "retry-configurable.bin"),
        expected_size=10,
    )

    assert storage._probe_redirect_range_download_size.await_count == 2
    storage._download_redirect_single_stream_to_file.assert_awaited_once()
    assert sleep_calls == [1.0]


@pytest.mark.asyncio
async def test_storageapi_redirect_download_ignores_upload_retries(monkeypatch, tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._enable_range_download = True
    storage._probe_redirect_range_download_size = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
    storage._download_redirect_single_stream_to_file = AsyncMock()
    sleep_calls = []

    async def _fake_sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setenv("WRAPP_STORAGE_API_UPLOAD_RETRIES", "1")
    monkeypatch.setenv("WRAPP_STORAGE_API_DOWNLOAD_RETRIES", "2")
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", _fake_sleep)

    await storage._download_redirect_to_file(
        session=object(),
        redirect_target_url="https://download.example/file",
        headers={"authorization": "Bearer token"},
        file_path=str(tmp_path / "retry-download-not-upload.bin"),
        expected_size=10,
    )

    assert storage._probe_redirect_range_download_size.await_count == 2
    storage._download_redirect_single_stream_to_file.assert_awaited_once()
    assert sleep_calls == [1.0]


@pytest.mark.asyncio
async def test_storageapi_download_file_returns_already_exists_for_existing_target(tmp_path):
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    dst_path = tmp_path / "already.txt"
    dst_path.write_bytes(b"already")

    result = await storage.download_file(
        "omniverse://server.example/repo/file.bin",
        dst_path.as_uri(),
        overwrite=False,
    )

    assert result == Result.ERROR_ALREADY_EXISTS


@pytest.mark.asyncio
async def test_STORAGEAPI_list_directory_encodes_and_decodes_resource_addresses():
    """list_directory must percent-encode the outbound ListStatRequest folder URI and
    decode percent-encoded paths returned by the server into plain relative paths."""

    # Fake FolderAddress / ListStatRequest message constructors
    class _FakeFolderAddress:
        def __init__(self, uri: str):
            self.uri = uri

    class _FakeListStatRequest:
        def __init__(self, folder: _FakeFolderAddress):
            self.folder = folder

    # Fake file-entry (mirrors a ListStat entry with resource_address and resource_info)
    encoded_file_uri = "omniverse://server/Material%20Library/Banner%2FMedia%20%281%29.jpg"
    fake_file_entry = SimpleNamespace(
        resource_address=encoded_file_uri,
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity="id-abc"),
            metadata=SimpleNamespace(data_object_size=42, last_modified_timestamp=None),
        ),
    )

    # Fake subfolder entry (encoded URI returned from server)
    encoded_subfolder_uri = "omniverse://server/Material%20Library/Sub%20Folder"

    # Capture the request passed to ListStat so we can assert on it
    captured_request = {}

    async def _fake_iterate_grpc(_throttler, _grpc_method, request):
        captured_request["folder_uri"] = request.folder.uri
        # Yield one partial result with a subfolder and a file entry
        yield SimpleNamespace(
            subfolder_addresses=[SimpleNamespace(uri=encoded_subfolder_uri)],
            entries=[fake_file_entry],
        )

    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._filefolder_api = SimpleNamespace(ListStat=object())
    storage._filefolder_msgs = SimpleNamespace(
        FolderAddress=_FakeFolderAddress,
        ListStatRequest=_FakeListStatRequest,
    )
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server/Material Library")
    storage._ensure_grpc_stubs = AsyncMock()
    storage._iterate_grpc = _fake_iterate_grpc

    entries = []
    async for entry in storage.list_directory("omniverse://server/Material Library"):
        entries.append(entry)

    # Outbound URI must be percent-encoded
    assert captured_request["folder_uri"] == "omniverse://server/Material%20Library"

    # Two entries should be yielded: subfolder + file
    assert len(entries) == 2

    folder_entry = next(e for e in entries if e.is_folder)
    file_entry = next(e for e in entries if not e.is_folder)

    # Subfolder relative address should be decoded plain text
    assert folder_entry.relative_path == "Sub Folder"

    # File relative address should be decoded plain text (slash in percent-encoded name decoded too)
    assert file_entry.relative_path == "Banner/Media (1).jpg"


def test_STORAGEAPI_encode_resource_address_normalises_backslash():
    from wrapp.utils.storage_impls.storageapi import (
        _encode_resource_address_for_storage_service,
    )

    result = _encode_resource_address_for_storage_service("omniverse://server/Material Library/Mini%20map\\Media (1).usd")
    assert result == "omniverse://server/Material%20Library/Mini%2520map/Media%20%281%29.usd"


@pytest.mark.asyncio
async def test_STORAGEAPI_list_file_tree_does_not_double_decode():
    """list_file_tree must not decode resource addresses twice.

    The storage service returns addresses with a single level of
    percent-encoding.  A literal ``%20`` in a folder name is stored as
    ``%2520`` (``%25`` = literal ``%``, followed by ``20``).  After one
    decode pass the ``%2520`` becomes ``%20`` — which is the correct
    relative address to expose to callers.  A second decode would turn
    ``%20`` into a space, corrupting the path.
    """
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._ensure_grpc_stubs = AsyncMock()
    storage._fileobject_api = SimpleNamespace(Enumerate=object())
    storage._fileobject_msgs = SimpleNamespace(EnumerateRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address))
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server/repo")
    storage.stat = AsyncMock(
        return_value=(
            Result.OK,
            StorageAPIListEntry("Mini%20map/file.usd", "identity-1", None, None, is_folder=False),
        )
    )

    # The server returns ``%2520`` which is the double-encoded form of ``%20``.
    # After a single decode this should become ``%20`` (a literal percent-sign
    # followed by "20"), NOT a space character.
    fake_entry = SimpleNamespace(resource_address="omniverse://server/repo/Mini%2520map/file.usd")
    fake_result = SimpleNamespace(items=[fake_entry])

    async def _fake_iterate_grpc(_throttler, _grpc_method, _request):
        yield fake_result

    storage._iterate_grpc = _fake_iterate_grpc

    entries = []
    async for entry in storage.list_file_tree("omniverse://server/repo"):
        entries.append(entry)

    assert len(entries) == 2
    folder_entry = next(entry for entry in entries if entry.is_folder)
    assert folder_entry.relative_path == "Mini%20map"

    file_entry = next(entry for entry in entries if not entry.is_folder)
    # Single decode: %2520 -> %20  (correct)
    assert file_entry.relative_path == "Mini%20map/file.usd"
    # Double decode would give "Mini map/file.usd" (wrong)
    assert file_entry.relative_path != "Mini map/file.usd"


@pytest.mark.asyncio
async def test_STORAGEAPI_list_file_tree_encodes_outbound_and_decodes_returned_addresses():
    """list_file_tree must percent-encode the outbound EnumerateRequest resource_address
    and decode percent-encoded paths returned by the server into plain relative paths."""

    # Capture what EnumerateRequest receives so we can assert on it
    captured_request = {}

    class _FakeEnumerateRequest:
        def __init__(self, resource_address: str):
            captured_request["resource_address"] = resource_address
            self.resource_address = resource_address

    # The server returns a percent-encoded absolute URI for the file entry
    encoded_file_uri = "omniverse://server/Material%20Library/file.usd"
    fake_entry = SimpleNamespace(resource_address=encoded_file_uri)
    fake_result = SimpleNamespace(items=[fake_entry])

    async def _fake_iterate_grpc(_throttler, _grpc_method, _request):
        yield fake_result

    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    storage._ensure_grpc_stubs = AsyncMock()
    storage._fileobject_api = SimpleNamespace(Enumerate=object())
    storage._fileobject_msgs = SimpleNamespace(EnumerateRequest=_FakeEnumerateRequest)
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server/Material Library")
    storage._iterate_grpc = _fake_iterate_grpc
    storage.stat = AsyncMock(
        return_value=(
            Result.OK,
            StorageAPIListEntry("file.usd", "identity-1", None, None, is_folder=False),
        )
    )

    entries = []
    async for entry in storage.list_file_tree("omniverse://server/Material Library"):
        entries.append(entry)

    # Outbound resource_address must be percent-encoded
    assert captured_request["resource_address"] == "omniverse://server/Material%20Library/"

    # Exactly one file entry should be returned
    assert len(entries) == 1

    # The returned relative_path should be decoded plain text (space, not %20)
    assert entries[0].relative_path == "file.usd"


@pytest.mark.asyncio
async def test_storageapi_download_redirect_to_bytes_returns_content_and_cleans_up_temp_file():
    """Happy path: _download_redirect_to_bytes returns the bytes written by
    _download_redirect_to_file and removes the temp file afterwards."""
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    expected_payload = b"hello redirect bytes"
    seen_tmp_paths = []

    async def _fake_download_redirect_to_file(*, session, redirect_target_url, headers, file_path, expected_size):
        seen_tmp_paths.append(file_path)
        with open(file_path, "wb") as f:
            f.write(expected_payload)

    storage._download_redirect_to_file = _fake_download_redirect_to_file

    result_bytes = await storage._download_redirect_to_bytes(
        session=object(),
        redirect_target_url="https://download.example/file",
        headers={},
        expected_size=len(expected_payload),
    )

    assert result_bytes == expected_payload
    assert len(seen_tmp_paths) == 1
    assert not os.path.exists(seen_tmp_paths[0]), "temp file must be cleaned up after successful download"


@pytest.mark.asyncio
async def test_storageapi_download_redirect_to_bytes_cleans_up_temp_file_on_error():
    """Error path: when _download_redirect_to_file raises StorageOperationError the
    exception propagates and the temp file is still cleaned up."""
    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    seen_tmp_paths = []

    async def _fake_download_redirect_to_file(*, session, redirect_target_url, headers, file_path, expected_size):
        seen_tmp_paths.append(file_path)
        raise StorageOperationError("simulated download failure")

    storage._download_redirect_to_file = _fake_download_redirect_to_file

    with pytest.raises(StorageOperationError, match="simulated download failure"):
        await storage._download_redirect_to_bytes(
            session=object(),
            redirect_target_url="https://download.example/file",
            headers={},
            expected_size=100,
        )

    assert len(seen_tmp_paths) == 1
    assert not os.path.exists(seen_tmp_paths[0]), "temp file must be cleaned up even when download fails"


class _FakeSingleStreamSession:
    """Fake aiohttp.ClientSession that delivers a fixed payload for GET requests."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def get(self, _url: str, headers: dict):
        return _FakeResponse(status=200, headers={}, body=self._payload)


@pytest.mark.asyncio
async def test_storageapi_single_stream_size_mismatch_emits_warning(tmp_path, caplog):
    payload = b"short"  # 5 bytes
    expected_size = 100  # deliberately larger than payload

    storage = StorageAPIStorageGRPC("0.0.0-test", False, throttling_params=ThrottlingParameters())
    session = _FakeSingleStreamSession(payload)
    dst_path = tmp_path / "download.bin"

    with caplog.at_level(logging.WARNING, logger="wrapp.utils.storage_impls.storageapi"):
        await storage._download_redirect_single_stream_to_file(
            session=session,
            redirect_target_url="https://download.example/file",
            headers={},
            file_path=str(dst_path),
            expected_size=expected_size,
        )

    assert dst_path.read_bytes() == payload
    actual_size = len(payload)
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any(
        str(actual_size) in msg and str(expected_size) in msg for msg in warnings
    ), f"Expected size-mismatch warning mentioning {actual_size} and {expected_size}; got: {warnings}"
