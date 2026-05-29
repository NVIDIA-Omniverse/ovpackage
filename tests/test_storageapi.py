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
import logging
import os
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
from wrapp.utils.router import g_storage_api_enabled
from wrapp.utils.scheduler_task_node import SchedulerTreeNode
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

from .common import StorageBackendForTests

try:
    import grpc

    from wrapp.utils.storage_impls.storageapi import (
        StorageAPIListEntry,
        StorageAPIStorageGRPC,
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
    storage._fileobject_api = SimpleNamespace(ReadFromAddress=object())
    storage._fileobject_msgs = SimpleNamespace(
        ReadFromAddressRequest=lambda resource_address: SimpleNamespace(resource_address=resource_address)
    )
    storage._ensure_grpc_stubs = AsyncMock()
    storage._sanitize_address_for_storage_service = AsyncMock(return_value="omniverse://server.example/repo/file.bin")

    async def _fake_iterate_grpc(*_args, **_kwargs):
        yield _FakeReply("resource_info", size=11)
        yield _FakeReply("chunk", chunk=b"hello ")
        yield _FakeReply("chunk", chunk=b"world")

    storage._iterate_grpc = _fake_iterate_grpc

    dst_path = tmp_path / "downloaded.bin"
    result = await storage._download_to_file("omniverse://server.example/repo/file.bin", str(dst_path))

    assert result == Result.OK
    assert dst_path.read_bytes() == b"hello world"


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
    storage.is_folder = AsyncMock(return_value=(Result.OK, False))

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

    assert len(entries) == 1
    # Single decode: %2520 -> %20  (correct)
    assert entries[0].relative_path == "Mini%20map/file.usd"
    # Double decode would give "Mini map/file.usd" (wrong)
    assert entries[0].relative_path != "Mini map/file.usd"


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
    # is_folder returns False so the entry is yielded as a file
    storage.is_folder = AsyncMock(return_value=(Result.OK, False))

    entries = []
    async for entry in storage.list_file_tree("omniverse://server/Material Library"):
        entries.append(entry)

    # Outbound resource_address must be percent-encoded
    assert captured_request["resource_address"] == "omniverse://server/Material%20Library"

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
