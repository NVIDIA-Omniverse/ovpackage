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

import io
import json
import logging
import os
from pathlib import Path
from typing import Optional

import pytest

from tests.common import run_wrapp_in_separate_process
from tests.filename_transfer_helpers import (
    FILENAME_CONTENTS,
    assert_filename_tree,
    write_filename_tree,
)
from tests.s3_test_helpers import S3TestHelper
from tests.storage_test_helpers import StorageBackendForTests
from wrapp.commands.list_repo import list_repo
from wrapp.datastructures.command_context import CommandParameters
from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    Router,
    StorageType,
    wrapp_copy_address,
    wrapp_copy_resource_identity,
    wrapp_copy_resource_identity_optimized,
    wrapp_delete_item,
    wrapp_initialize,
    wrapp_list_directory,
    wrapp_read_file_content,
    wrapp_read_resource_identity,
    wrapp_stat,
    wrapp_url_exists,
    wrapp_write_file_bytes,
    wrapp_write_file_content,
)
from wrapp.utils.storage_interface import (
    Result,
    SerializedResourceIdentity,
    StorageInterface,
    URLResourceIdentity,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_path_from_url,
    is_file_url,
    join_paths,
    join_url_path,
)


@pytest.mark.parametrize("path", [r"C:\tmp\file.txt", "C:/tmp/file.txt"])
def test_url_resource_identity_rejects_windows_drive_path_OMPE_58312(path):
    with pytest.raises(ValueError, match="absolute URL"):
        URLResourceIdentity(path)


@pytest.mark.asyncio
async def test_storage_interface_refuses_resource_identity_reads_by_default():
    with pytest.raises(NotImplementedError):
        await StorageInterface.read_resource_identity(
            object(),
            SerializedResourceIdentity("storage-id://service/version"),
        )


@pytest.mark.parametrize(
    "url1,url2,expected",
    [
        # Same URL in different S3 formats should be considered equal
        ("s3://mybucket/path/to/file", "https://mybucket.s3.us-west-2.amazonaws.com/path/to/file", True),
        ("s3://mybucket/path", "https://s3.us-west-2.amazonaws.com/mybucket/path", True),
        ("s3://mybucket/path/to/file", "https://s3.amazonaws.com/mybucket/path/to/file", True),
        # Sub-URL relationship across S3 formats
        ("s3://mybucket/path", "https://mybucket.s3.us-west-2.amazonaws.com/path/to/file", True),
        # Different buckets should not match
        ("s3://mybucket/path", "s3://otherbucket/path", False),
        ("s3://mybucket/path", "https://otherbucket.s3.us-west-2.amazonaws.com/path", False),
        # Non-S3 URLs should work normally
        ("https://example.com/path", "https://example.com/path/to/file", True),
        ("https://example.com/path", "https://other.com/path/to/file", False),
    ],
)
@pytest.mark.asyncio
async def test_is_url_pointing_to_sub_url_s3_formats(wrapp_init, url1, url2, expected):
    """Test that is_url_pointing_to_sub_url correctly handles S3 URL format variations."""
    from wrapp.utils.storage import is_url_pointing_to_sub_url

    result = await is_url_pointing_to_sub_url(url1, url2)
    assert result == expected, f"Expected {url1} {'is' if expected else 'is not'} parent of {url2}"


@pytest.mark.asyncio
async def test_storage_delete_file(source_package):
    context = CommandParameters()
    source_file = source_package + "/" + f"testfile.txt"
    assert await wrapp_url_exists(source_file)
    r = await wrapp_delete_item(context, source_file)
    assert r == Result.OK
    # Delete something that doesn't exist also returns OK for Storage API
    assert not (await wrapp_url_exists(source_file))
    r = await wrapp_delete_item(context, source_file)
    assert r in [Result.ERROR_NOT_FOUND, Result.OK]


@pytest.mark.asyncio
async def test_wrapp_stat_file_exists(test_folder_url, authenticator, source_package):
    source_url = source_package
    result, entry = await wrapp_stat(join_paths(source_url, "testfile.txt"))
    assert result == Result.OK


@pytest.mark.asyncio
async def test_wrapp_write_to_invalid_bucket_or_host(authenticator, storage_backend_type):
    if storage_backend_type != StorageBackendForTests.S3:
        pytest.skip("Client-library takes too long to fail with an unknown host and for file directories the test makes no sense.")
    context = CommandParameters()
    result = await wrapp_write_file_bytes(context, "s3://nvidia_omniverse_wrapp_nonexistingbucket/somefile.bytes", b"123")
    assert result != Result.OK  # we want a return code, not an exception.


@pytest.mark.asyncio
async def test_storage_can_be_initialized_in_clean_env(test_folder_url, storage_backend_type):
    # We had the issue that the storage could not be initialized in a clean environment without the test variables.
    # So do the most simple command in without any authorization credentials we can, except for Azurite, which needs the
    # API version in the environment
    if storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
        pytest.skip(
            "Azurite can't be connected to without specifying the API version in the environment via WRAPP_AZURE_API_VERSION. Skipping test"
        )
    expected_return_code = (
        [1, 2]
        if storage_backend_type
        in [
            StorageBackendForTests.S3,
            StorageBackendForTests.STORAGEAPI,
            StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE,
            StorageBackendForTests.GCS,
        ]
        else 1
    )  # S3, StorageAPI and GCS may fail due to missing credentials or because the folder cannot be found
    # On Windows, Python needs SYSTEMROOT to initialize its random-number support.
    env = {}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    run_wrapp_in_separate_process(["list-repo", test_folder_url], expected_return_code, env=env, use_system_env=False)


@pytest.mark.asyncio
async def test_storage_exception_for_double_initialization(wrapp_init):
    with pytest.raises(RuntimeError):
        wrapp_initialize("wrapp version 2", True)


@pytest.mark.asyncio
async def test_copy_6GB_file_s3_to_s3(wrapp_init, authenticator, test_host, test_folder_url, storage_backend):
    if storage_backend.storage_type() != StorageBackendForTests.S3:
        pytest.skip(f"Test is only required for S3")
    assert isinstance(storage_backend, S3TestHelper)
    if Router.me().s3().s3_client().endpoint_looks_like_minio():
        pytest.skip("The test makes most sense on S3 proper - minio has no 5GB limit when copying files.")
    src_file_url = f"s3://{test_host}/wrapp_tests/src_data/src_huge_6GB_file.raw"
    dst_file_url = join_paths(test_folder_url, "dst_huge_6GB_file.raw")
    # Leave creation of the file up to the storage, in the hope that there are tricks how to do that fast.
    logging.info(f"Creating 6GB file '{src_file_url}'")
    file_size = 6 * 1024 * 1024 * 1024  # 6GB
    await storage_backend.create_large_file(src_file_url, file_size)
    try:
        logging.info(f"Copying 6GB file '{src_file_url}' to '{dst_file_url}'")
        assert await wrapp_copy_address(src_file_url, dst_file_url) == Result.OK
    finally:
        await wrapp_delete_item(CommandParameters(), dst_file_url)


@pytest.mark.asyncio
async def test_nucleus_hash_is_available(wrapp_init, authenticator, test_host, test_folder_url, storage_backend):
    if is_file_url(test_folder_url):
        # Local file storage is still a special case and not required to return a nucleus_hash. See also OMPE-14844
        pytest.skip(f"Local file storage does not need to provide a nucleus hash.")
    if storage_backend.storage_type() in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
        if not Router.me().storage_api().has_metadata_support:
            pytest.skip("StorageAPI server does not expose the Metadata service.")
    src_path = join_paths(test_folder_url, "testfile.txt")
    content = b"This is content"
    nucleus_hash, size = calc_nucleus_hash_from_stream(io.BytesIO(content))
    assert await wrapp_write_file_bytes(CommandParameters(), src_path, content, "This is comment") == Result.OK
    r, e = await wrapp_stat(src_path)
    assert r == Result.OK
    assert e.nucleus_hash == nucleus_hash

    target_path = join_paths(test_folder_url, "testfile2.txt")
    assert await wrapp_copy_address(src_path, target_path) == Result.OK
    r, e = await wrapp_stat(target_path)
    assert r == Result.OK
    assert e.nucleus_hash == nucleus_hash


@pytest.mark.asyncio
async def test_wrapp_copy_cross_storage(
    wrapp_init, authenticator, test_host, test_folder_url, storage_backend, alternate_storage_source_package
):
    src_path = join_paths(alternate_storage_source_package, "testfile.txt")
    dst_path = join_paths(test_folder_url, "testfile2.txt")
    await wrapp_copy_address(src_path, dst_path)


async def _filename_tree_contents(base):
    """Read a backend tree by logical name to verify both names and bytes."""
    contents = {}
    async for entry in wrapp_list_directory(base):
        address = join_url_path(base, entry.relative_path)
        if entry.is_folder:
            for relative_path, content in (await _filename_tree_contents(address)).items():
                contents[entry.relative_path + "/" + relative_path] = content
        else:
            result, content = await wrapp_read_file_content(address)
            assert result == Result.OK
            contents[entry.relative_path] = content
    return contents


@pytest.mark.asyncio
@pytest.mark.parametrize("address_style", ["native path", "file URL"])
async def test_local_backend_transfer_preserves_distinct_filenames_OMPE_117642(tmp_path, test_folder_url, storage_backend, address_style):
    """Exercise uploads, same-backend copies, and downloads with sibling names."""
    source = tmp_path / "source %20 parent" / "source tree"
    destination = tmp_path / "download %20 parent" / "download tree"
    write_filename_tree(source)
    uploaded = join_url_path(test_folder_url, "uploaded tree %20")
    copied = join_url_path(test_folder_url, "copied tree %20")

    def local_address(path: Path):
        """Select native-path or escaped-file-URL input without changing the filename."""
        return str(path) if address_style == "native path" else path.as_uri()

    for name in FILENAME_CONTENTS:
        remote_source = join_url_path(uploaded, name)
        remote_copy = join_url_path(copied, name)
        assert await wrapp_copy_address(local_address(source / name), remote_source) == Result.OK
        assert await wrapp_copy_address(remote_source, remote_copy) == Result.OK
        assert await wrapp_copy_address(remote_copy, local_address(destination / name)) == Result.OK

    assert await _filename_tree_contents(uploaded) == FILENAME_CONTENTS
    assert await _filename_tree_contents(copied) == FILENAME_CONTENTS
    assert_filename_tree(destination)


@pytest.mark.asyncio
async def test_cross_backend_copy_preserves_distinct_filenames_OMPE_117642(
    test_folder_url,
    alternate_storage_test_folder_url,
    alternate_storage_backend_test_helper,
    authenticator_with_alternate,
):
    """Cloud/cloud pairs exercise the download-to-temp/upload fallback."""
    source = join_url_path(test_folder_url, "source tree %20")
    destination = join_url_path(alternate_storage_test_folder_url, "destination tree %20")
    for name, content in FILENAME_CONTENTS.items():
        source_address = join_url_path(source, name)
        assert await wrapp_write_file_bytes(CommandParameters(), source_address, content) == Result.OK
        assert await wrapp_copy_address(source_address, join_url_path(destination, name)) == Result.OK
    assert await _filename_tree_contents(source) == FILENAME_CONTENTS
    assert await _filename_tree_contents(destination) == FILENAME_CONTENTS


@pytest.mark.asyncio
async def test_wrapp_copy_cross_host(wrapp_init, authenticator, test_folder_url, alternate_host_test_folder_url, source_package):
    # OMPE-17485
    src_path = join_paths(source_package, "testfile.txt")
    dst_path = join_paths(alternate_host_test_folder_url, "testfile2.txt")
    await wrapp_copy_address(src_path, dst_path)
    assert (await wrapp_stat(dst_path))[0] == Result.OK, f"Cannot find copied target file {dst_path} (copied from {src_path} across hosts)"


@pytest.mark.asyncio
async def test_wrapp_copy_fallback_counts_read_and_write_stats(wrapp_init, monkeypatch):
    class _FakeReadStorage:
        async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
            dst_path = get_path_from_url(dst_url)
            os.makedirs(os.path.dirname(dst_path), exist_ok=True)
            mode = "wb" if overwrite else "xb"
            with open(dst_path, mode) as f:
                f.write(b"test-data")
            return Result.OK

    class _FakeWriteStorage:
        async def stat(self, url: str):
            return Result.ERROR_NOT_FOUND, None

        async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
            src_path = get_path_from_url(src_url)
            assert os.path.exists(src_path)
            return Result.OK

    router = Router.me()
    fake_read = _FakeReadStorage()
    fake_write = _FakeWriteStorage()

    # wrapp_copy routes both URLs in one pass via storages_for_address_copy, returning
    # (read_storage, read_type, write_storage, write_type). S3 -> StorageAPI is a
    # cross-storage copy, exercising the download-to-temp + upload fallback.
    monkeypatch.setattr(
        router,
        "storages_for_address_copy",
        lambda *_: (fake_read, StorageType.S3, fake_write, StorageType.StorageAPI),
    )

    Stats.get().clear()
    result = await wrapp_copy_address("s3://source-bucket/source.txt", "https://target.example.com/destination.txt")
    assert result == Result.OK
    assert Stats.get().get_stat(StatType.COPY_ASYNC) == 1
    assert Stats.get().get_stat(StatType.READ_FILE_ASYNC) == 1
    assert Stats.get().get_stat(StatType.WRITE_FILE_ASYNC) == 1


class _FakeStorageApiService:
    """Minimal Storage API-like backend for exercising wrapp_copy dispatch."""

    def __init__(self):
        self.copy_called = False
        self.copied_resource_identity = None

    async def copy_address(
        self,
        src_url: str,
        dst_url: str,
        overwrite: bool,
        message: str = "",
    ) -> Result:
        self.copy_called = True
        return Result.OK

    async def copy_resource_identity(
        self,
        resource_identity: str,
        dst_url: str,
        overwrite: bool,
        message: str = "",
    ) -> Result:
        self.copy_called = True
        self.copied_resource_identity = resource_identity
        return Result.OK

    async def stat(self, url: str):
        return Result.ERROR_NOT_FOUND, None

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        dst_path = get_path_from_url(dst_url)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        with open(dst_path, "wb" if overwrite else "xb") as f:
            f.write(b"test-data")
        return Result.OK

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        assert os.path.exists(get_path_from_url(src_url))
        return Result.OK


@pytest.mark.asyncio
async def test_wrapp_copy_between_two_storage_api_services_uses_temp_file(wrapp_init, monkeypatch):
    """A copy between two *different* Storage API services must NOT invoke the
    source service's server-side copy() (which would resolve the destination
    against the wrong server); it must go through the download+upload path."""
    router = Router.me()
    service_a = _FakeStorageApiService()
    service_b = _FakeStorageApiService()
    monkeypatch.setattr(
        router,
        "storages_for_address_copy",
        lambda *_: (service_a, StorageType.StorageAPI, service_b, StorageType.StorageAPI),
    )

    Stats.get().clear()
    result = await wrapp_copy_address("https://a.example.com/src.txt", "https://b.example.com/dst.txt")
    assert result == Result.OK
    assert service_a.copy_called is False  # cross-instance: no server-side copy
    assert Stats.get().get_stat(StatType.READ_FILE_ASYNC) == 1
    assert Stats.get().get_stat(StatType.WRITE_FILE_ASYNC) == 1


@pytest.mark.asyncio
async def test_wrapp_copy_within_one_storage_api_service_uses_server_side_copy(wrapp_init, monkeypatch):
    """A copy within a single Storage API service keeps Case 1 and uses the
    backend's (server-side) copy()."""
    router = Router.me()
    service = _FakeStorageApiService()
    monkeypatch.setattr(
        router,
        "storages_for_address_copy",
        lambda *_: (service, StorageType.StorageAPI, service, StorageType.StorageAPI),
    )

    Stats.get().clear()
    result = await wrapp_copy_address("https://a.example.com/src.txt", "https://a.example.com/dst.txt")
    assert result == Result.OK
    assert service.copy_called is True  # same instance: server-side copy


@pytest.mark.asyncio
async def test_wrapp_copy_passes_identity_to_same_storage_backend(wrapp_init, monkeypatch):
    router = Router.me()
    service = _FakeStorageApiService()
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (service, StorageType.StorageAPI, service, StorageType.StorageAPI),
    )

    result = await wrapp_copy_resource_identity(
        "storage-id://service/original-version",
        "https://a.example.com/dst.txt",
        overwrite=True,
    )

    assert result == Result.OK
    assert service.copied_resource_identity == "storage-id://service/original-version"


@pytest.mark.asyncio
async def test_wrapp_copy_falls_back_when_same_backend_identity_copy_is_not_supported(wrapp_init, monkeypatch):
    writes = []

    class _IdentityStorage(_FakeStorageApiService):
        async def copy_resource_identity(self, *args, **kwargs):
            return Result.ERROR_NOT_SUPPORTED

        async def download_resource_identity(self, resource_identity: str, dst_url: str, overwrite: bool):
            with open(get_path_from_url(dst_url), "wb") as f:
                f.write(b"identity bytes")
            return Result.OK

        async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
            with open(get_path_from_url(src_url), "rb") as f:
                writes.append((dst_url, f.read()))
            return Result.OK

    router = Router.me()
    storage = _IdentityStorage()
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (storage, StorageType.StorageAPI, storage, StorageType.StorageAPI),
    )

    result = await wrapp_copy_resource_identity(
        "storage-id://service/original-version",
        "https://a.example.com/dst.txt",
        overwrite=True,
    )

    assert result == Result.OK
    assert writes == [("https://a.example.com/dst.txt", b"identity bytes")]


@pytest.mark.asyncio
async def test_wrapp_copy_resource_identity_optimized_creates_nucleus_cross_server_copy_by_hash(wrapp_init, monkeypatch):
    class _FakeNucleusStorage:
        def __init__(self):
            self.created_with_hash = []

        async def copy_resource_identity(self, *args, **kwargs):
            pytest.fail("cross-server Nucleus identity copies should use create_with_hash")

        async def read_resource_identity(self, *args, **kwargs):
            pytest.fail("cross-server Nucleus identity copies should not download bytes")

        async def write_file(self, *args, **kwargs):
            pytest.fail("cross-server Nucleus identity copies should not upload buffered bytes")

        def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
            return True

        async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool):
            self.created_with_hash.append((copy_target, storage_hash, overwrite))
            return Result.OK

    router = Router.me()
    storage = _FakeNucleusStorage()
    resource_identity = SerializedResourceIdentity(
        json.dumps(
            {
                "checkpoint_url": "omniverse://source.example/pkg/asset.usd?&7",
                "hash": "sha-256-flat;123;abc",
                "type": "nucleus",
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    copy_target = "omniverse://target.example/pkg/asset.usd"
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (storage, StorageType.Nucleus, storage, StorageType.Nucleus),
    )

    result = await wrapp_copy_resource_identity_optimized(
        CommandParameters(),
        resource_identity,
        copy_target,
        "sha-256-flat;123;abc",
        overwrite=True,
        force_upload=False,
    )

    assert result == Result.OK
    assert storage.created_with_hash == [(copy_target, "sha-256-flat;123;abc", True)]


@pytest.mark.asyncio
async def test_wrapp_copy_resource_identity_optimized_keeps_same_nucleus_server_copy_path(wrapp_init, monkeypatch):
    class _FakeNucleusStorage:
        def __init__(self):
            self.created_with_hash = []
            self.copied_resource_identity = None

        async def copy_resource_identity(self, resource_identity: str, dst_url: str, overwrite: bool, message: str = ""):
            self.copied_resource_identity = resource_identity
            return Result.OK

        def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
            return True

        async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool):
            self.created_with_hash.append((copy_target, storage_hash, overwrite))
            return Result.OK

    router = Router.me()
    storage = _FakeNucleusStorage()
    resource_identity = SerializedResourceIdentity(
        json.dumps(
            {
                "checkpoint_url": "omniverse://same.example/pkg/asset.usd?&7",
                "hash": "sha-256-flat;123;abc",
                "type": "nucleus",
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (storage, StorageType.Nucleus, storage, StorageType.Nucleus),
    )

    result = await wrapp_copy_resource_identity_optimized(
        CommandParameters(),
        resource_identity,
        "omniverse://same.example/pkg/copy.usd",
        "sha-256-flat;123;abc",
        overwrite=True,
        force_upload=False,
    )

    assert result == Result.OK
    assert storage.created_with_hash == []
    assert storage.copied_resource_identity == resource_identity


@pytest.mark.asyncio
async def test_wrapp_copy_does_not_fall_back_when_resource_identity_read_fails(wrapp_init, monkeypatch):
    """A failed immutable read must not silently copy from the source address."""

    class _IdentityReadStorage:
        async def download_resource_identity(self, resource_identity: str, dst_url: str, overwrite: bool):
            assert resource_identity == "storage-id://service/original-version"
            return Result.ERROR_NOT_FOUND

        async def download_file(self, src_url: str, dst_url: str, overwrite: bool):
            pytest.fail("The mutable source address must not be used as a fallback")

    class _WriteStorage:
        async def stat(self, dst_url: str):
            return Result.ERROR_NOT_FOUND, None

        async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: str):
            pytest.fail("No bytes should be uploaded after the identity read fails")

    router = Router.me()
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (_IdentityReadStorage(), StorageType.StorageAPI, _WriteStorage(), StorageType.Local),
    )

    result = await wrapp_copy_resource_identity(
        "storage-id://service/original-version",
        "file:///tmp/asset.usd",
    )

    assert result == Result.ERROR_NOT_FOUND


@pytest.mark.asyncio
async def test_read_resource_identity_routes_by_identity_when_backend_claims_it(wrapp_init, monkeypatch):
    resource_identity = "storage-id://west/original-version"

    class _IdentityStorage:
        async def read_resource_identity(self, identity: str):
            assert identity == resource_identity
            return Result.OK, b"original bytes"

    router = Router.me()
    identity_storage = _IdentityStorage()

    def fake_storage_for_resource_identity(identity):
        assert identity == resource_identity
        return identity_storage

    monkeypatch.setattr(router, "storage_for_resource_identity", fake_storage_for_resource_identity)

    result, content = await wrapp_read_resource_identity(resource_identity)

    assert result == Result.OK
    assert content == b"original bytes"


@pytest.mark.asyncio
async def test_wrapp_copy_propagates_identity_read_verification_failure(wrapp_init, monkeypatch):
    """Identity verification belongs to the backend that issued the identity."""

    class _SelfVerifyingIdentityStorage:
        async def copy_resource_identity(self, *args, **kwargs):
            raise NotImplementedError

        async def download_resource_identity(self, resource_identity: str, dst_url: str, overwrite: bool):
            return Result.ERROR_BAD_VERSION

        async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: str):
            pytest.fail("Unverified bytes must not be uploaded")

    router = Router.me()
    storage = _SelfVerifyingIdentityStorage()
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (storage, StorageType.Nucleus, storage, StorageType.Nucleus),
    )

    result = await wrapp_copy_resource_identity(
        "omniverse://content.example/asset.usd?&7",
        "omniverse://content.example/copy.usd",
        overwrite=True,
    )

    assert result == Result.ERROR_BAD_VERSION


@pytest.mark.asyncio
async def test_wrapp_copy_accepts_backend_verified_identity(wrapp_init, monkeypatch):
    content = b"bytes from the recorded checkpoint"
    writes = []

    class _SelfVerifyingIdentityStorage:
        async def copy_resource_identity(self, *args, **kwargs):
            raise NotImplementedError

        async def download_resource_identity(
            self, resource_identity: str, dst_url: str, overwrite: bool, *, expected_hash: Optional[str] = None
        ):
            assert expected_hash == "catalog-hash"
            with open(get_path_from_url(dst_url), "wb") as f:
                f.write(content)
            return Result.OK

        async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: str):
            with open(get_path_from_url(src_url), "rb") as f:
                writes.append((dst_url, f.read()))
            return Result.OK

    router = Router.me()
    storage = _SelfVerifyingIdentityStorage()
    monkeypatch.setattr(
        router,
        "storages_for_resource_identity_copy",
        lambda *_: (storage, StorageType.Nucleus, storage, StorageType.Nucleus),
    )

    result = await wrapp_copy_resource_identity(
        "omniverse://content.example/asset.usd?&7",
        "omniverse://content.example/copy.usd",
        overwrite=True,
        expected_hash="catalog-hash",
    )

    assert result == Result.OK
    assert writes == [("omniverse://content.example/copy.usd", content)]


# Make sure that WRAPP gracefully handles missing storage backends by unconditionally checking all of them.
# This will be a relevant test for the tests running in the minimal wheel environment.
@pytest.mark.asyncio
async def test_missing_backends(wrapp_init, local_tmp_folder):
    sapi_info = Router.me().get_storage_version_info(StorageType.StorageAPI)
    # Storage API is "available" only when actually configured. Since the
    # default flip, the backend is enabled by default but stays inert
    # (claims no URLs) when neither WRAPP_STORAGE_API_GRPC_ADDRESS nor
    # WRAPP_STORAGE_API_DISCOVERY_URL is set — in that state version_info
    # reports "ENABLED but not configured" and tests that expect the
    # backend to claim URLs must skip it.
    sapi_available = not sapi_info.startswith("DISABLED") and "ENABLED but not configured" not in sapi_info
    s3_available = not Router.me().get_storage_version_info(StorageType.S3).startswith("DISABLED")
    nucleus_available = not Router.me().get_storage_version_info(StorageType.Nucleus).startswith("DISABLED")
    local_available = not Router.me().get_storage_version_info(StorageType.Local).startswith("DISABLED")
    huggingface_available = not Router.me().get_storage_version_info(StorageType.HuggingFace).startswith("DISABLED")

    if not local_available and not nucleus_available:
        # With LocalFileStorage, local file operations work even without Nucleus. But it needs aiofiles installed, so
        # either that or Nucleus need to be active
        with pytest.raises(StorageOperationError, match=r"No storage backend available to handle file URL"):
            async for _ in wrapp_list_directory(local_tmp_folder):
                pass
    else:
        # Should be possible to write a file to the local tmp folder
        tmp_file = os.path.join(local_tmp_folder, "write_once.txt")
        assert await wrapp_write_file_content(CommandParameters(), tmp_file, "test data")

    if nucleus_available:
        # Accept both case: the local Nucleus server is running and will respond or it is not available.
        # If we are using an unknown host, client-lib needs a long time to time out.
        with pytest.raises((FailedCommand, StorageOperationError), match=r"(No .packages folder|Result.ERROR_CONNECTION)"):  # type: ignore
            await list_repo("omniverse://localhost/some_repo")
    else:
        # When Nucleus is disabled, omniverse:// URLs should fail
        with pytest.raises(
            StorageOperationError,
            match=r"No backend configured|Nucleus storage backend is not available|failed to import omni\.client|storage error trying to stat omniverse://",
        ):
            await list_repo("omniverse://localhost/some_repo")
    # TODO - currently, if both S3 and StorageAPI are enabled, this test will accidentally use Storage API
    # and the error will be different. Once we figure out how to split URLs between S3 and StorageAPI, this could be
    # reactivated.
    if not s3_available and not sapi_available:
        with pytest.raises(StorageOperationError, match="No backend configured|S3: DISABLED"):
            await list_repo("s3://some_bucket/some_repo")
    else:
        # Sanity test to avoid false positives
        with pytest.raises((FailedCommand, StorageOperationError), match=r"(No \.packages folder|Result.ERROR|doesn't exist, cannot determine region)"):  # type: ignore
            await list_repo("s3://some_bucket/some_repo")
    azure_available = not Router.me().get_storage_version_info(StorageType.Azure).startswith("DISABLED")
    if not azure_available and not sapi_available:
        with pytest.raises(
            StorageOperationError, match=r"No backend configured|Azure: DISABLED|cannot be handled by any of the enabled storage backends"
        ):
            await list_repo("azure://some_account/some_container")
    else:
        # Sanity test to avoid false positives
        # When StorageAPI is enabled, it may handle azure:// URLs even if the direct Azure backend is disabled.
        with pytest.raises((FailedCommand, StorageOperationError), match=r"(No \.packages folder|Result.ERROR|no authentication information)"):  # type: ignore
            await list_repo("azure://some_account/some_container")
    if not huggingface_available and not sapi_available:
        with pytest.raises(
            StorageOperationError,
            match=r"No backend configured|Hugging Face: DISABLED|Storage Hugging Face was not successfully initialized|cannot be handled",
        ):
            await list_repo("hf://datasets/some_org/some_repo")
    # If this fires, there is a new storage backend which needs a test here.
    assert len(StorageType) == 9


def test_file_url_without_local_backend_reports_backend_diagnostics_opme_95748(wrapp_init, monkeypatch):
    router = Router.me()
    monkeypatch.setattr(router, "_local_file_storage", None)
    monkeypatch.setattr(router, "_clientlib", None)
    monkeypatch.setitem(
        router._storage_info,
        StorageType.Local,
        "DISABLED: failed to import aiofiles. Required dependencies: aiofiles. "
        "Install with 'pip install aiofiles'. Exception: No module named 'aiofiles'",
    )

    with pytest.raises(StorageOperationError) as exc_info:
        router.storage_for_read("file:///tmp/wrapp-minimal-repro")

    message = str(exc_info.value)
    assert "cannot be handled by any of the enabled storage backends" in message
    assert "Local File System: DISABLED: failed to import aiofiles" in message
    assert "Install with 'pip install aiofiles'" in message


def test_http_falls_back_to_clientlib_when_python_backend_is_unavailable(wrapp_init, monkeypatch):
    router = Router.me()
    clientlib = object()
    monkeypatch.setattr(router, "_http", None)
    monkeypatch.setattr(router, "_clientlib", clientlib)
    monkeypatch.setattr(router, "_select_known_storage_api", lambda url: None)
    monkeypatch.setattr(router, "is_s3_url", lambda url: False)
    monkeypatch.setattr(router, "is_azure_url", lambda url: False)
    monkeypatch.setattr(router, "is_gcs_url", lambda url: False)

    storage, storage_type = router._get_storage("https://downloads.example.com/asset")

    assert storage is clientlib
    assert storage_type == StorageType.Nucleus


def test_read_only_error_uses_selected_storage_display_name(wrapp_init, monkeypatch):
    router = Router.me()
    read_only_storage = type("ReadOnlyStorage", (), {"is_read_only": True})()
    monkeypatch.setattr(router, "_get_storage", lambda url: (read_only_storage, StorageType.GCS))

    with pytest.raises(StorageOperationError, match="storage backend 'Google Cloud Storage' is read-only"):
        router.storage_for_write("example://destination")
    with pytest.raises(StorageOperationError, match="storage backend 'Google Cloud Storage' is read-only"):
        router.storages_for_address_copy("example://source", "example://destination")
    with pytest.raises(StorageOperationError, match="storage backend 'Google Cloud Storage' is read-only"):
        router.storages_for_resource_identity_copy("example://source", "example://destination")


# =============================================================================
# Tests for HTTP URL normalization (doesn't require storage backends)
# =============================================================================


class TestNormalizeHttpUrl:
    """Tests for _normalize_http_url function."""

    def test_basic_http_url(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("http://example.com/path") == "http://example.com/path"

    def test_removes_trailing_slash(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("http://example.com/path/") == "http://example.com/path"

    def test_resolves_parent_directory(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("http://example.com/path/../other") == "http://example.com/other"
        assert _normalize_http_url("https://cdn.example.com/repo/pkg/../data") == "https://cdn.example.com/repo/data"

    def test_lowercases_scheme_and_host(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("HTTP://Example.COM/Path") == "http://example.com/Path"
        assert _normalize_http_url("HTTPS://CDN.Example.Com/Path") == "https://cdn.example.com/Path"

    def test_removes_default_http_port(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("http://example.com:80/path") == "http://example.com/path"

    def test_removes_default_https_port(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("https://example.com:443/path") == "https://example.com/path"

    def test_preserves_non_default_port(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("http://example.com:8080/path") == "http://example.com:8080/path"
        assert _normalize_http_url("https://example.com:8443/path") == "https://example.com:8443/path"

    def test_preserves_query_and_fragment(self):
        from wrapp.utils.storage import _normalize_http_url

        assert _normalize_http_url("http://example.com/path?query=1") == "http://example.com/path?query=1"
        assert _normalize_http_url("http://example.com/path#fragment") == "http://example.com/path#fragment"
        assert _normalize_http_url("http://example.com/path?q=1#frag") == "http://example.com/path?q=1#frag"

    def test_cloudfront_like_url(self):
        from wrapp.utils.storage import _normalize_http_url

        # Real-world CloudFront URL patterns
        assert (
            _normalize_http_url("https://d24kyioasbugw4.cloudfront.net/repo/package/1.0.0/")
            == "https://d24kyioasbugw4.cloudfront.net/repo/package/1.0.0"
        )
        assert (
            _normalize_http_url("HTTPS://D24KYIOASBUGW4.CLOUDFRONT.NET/repo/package/1.0.0/../2.0.0")
            == "https://d24kyioasbugw4.cloudfront.net/repo/package/2.0.0"
        )


class TestIsUrlPointingToSubUrl:
    """Tests for is_url_pointing_to_sub_url with HTTP URLs.

    These tests verify that HTTP URL comparison works without requiring
    the Nucleus/clientlib storage backend.
    """

    @pytest.mark.asyncio
    async def test_http_url_is_sub_url(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Basic sub-URL check
        assert await is_url_pointing_to_sub_url("http://example.com/repo", "http://example.com/repo/package/file.txt")

    @pytest.mark.asyncio
    async def test_http_url_not_sub_url(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Different paths
        assert not await is_url_pointing_to_sub_url("http://example.com/repo1", "http://example.com/repo2/file.txt")
        # Different hosts
        assert not await is_url_pointing_to_sub_url("http://example.com/repo", "http://other.com/repo/file.txt")

    @pytest.mark.asyncio
    async def test_http_url_with_case_differences(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Host case should be normalized
        assert await is_url_pointing_to_sub_url("http://EXAMPLE.COM/repo", "http://example.com/repo/file.txt")

    @pytest.mark.asyncio
    async def test_http_url_with_trailing_slash(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Trailing slashes should be normalized
        assert await is_url_pointing_to_sub_url("http://example.com/repo/", "http://example.com/repo/file.txt")

    @pytest.mark.asyncio
    async def test_http_url_with_parent_directory(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Parent directory (..) should be resolved
        assert await is_url_pointing_to_sub_url("http://example.com/repo", "http://example.com/repo/sub/../file.txt")

    @pytest.mark.asyncio
    async def test_https_cloudfront_url(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Real-world CloudFront URL patterns used in redirect packages
        base = "https://d24kyioasbugw4.cloudfront.net/repo/package/1.0.0"
        full = "https://d24kyioasbugw4.cloudfront.net/repo/package/1.0.0/data/file.usd"
        assert await is_url_pointing_to_sub_url(base, full)

    @pytest.mark.asyncio
    async def test_http_url_with_default_port(self, wrapp_init):
        from wrapp.utils.storage import is_url_pointing_to_sub_url

        # Default port should be normalized away
        assert await is_url_pointing_to_sub_url("http://example.com:80/repo", "http://example.com/repo/file.txt")
        assert await is_url_pointing_to_sub_url("https://example.com:443/repo", "https://example.com/repo/file.txt")
