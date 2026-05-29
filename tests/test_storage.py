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
import logging
import os
from typing import Optional

import pytest

from tests.common import run_wrapp_in_separate_process
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
    wrapp_copy,
    wrapp_delete_item,
    wrapp_initialize,
    wrapp_list_directory,
    wrapp_stat,
    wrapp_url_exists,
    wrapp_write_file_bytes,
    wrapp_write_file_content,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_path_from_url,
    is_file_url,
    join_paths,
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
    # In Windows, Python needs SYSTEMROOT for random number variables (https://stackoverflow.com/a/64706392)
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
        assert await wrapp_copy(src_file_url, dst_file_url) == Result.OK
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
    assert await wrapp_copy(src_path, target_path)
    r, e = await wrapp_stat(target_path)
    assert r == Result.OK
    assert e.nucleus_hash == nucleus_hash


@pytest.mark.asyncio
async def test_wrapp_copy_cross_storage(
    wrapp_init, authenticator, test_host, test_folder_url, storage_backend, alternate_storage_source_package
):
    src_path = join_paths(alternate_storage_source_package, "testfile.txt")
    dst_path = join_paths(test_folder_url, "testfile2.txt")
    await wrapp_copy(src_path, dst_path)


@pytest.mark.asyncio
async def test_wrapp_copy_cross_host(wrapp_init, authenticator, test_folder_url, alternate_host_test_folder_url, source_package):
    # OMPE-17485
    src_path = join_paths(source_package, "testfile.txt")
    dst_path = join_paths(alternate_host_test_folder_url, "testfile2.txt")
    await wrapp_copy(src_path, dst_path)
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

    monkeypatch.setattr(router, "storage_types_for_copy", lambda *_: (StorageType.S3, StorageType.StorageAPI))
    monkeypatch.setattr(router, "storage_for_read", lambda *_: fake_read)
    monkeypatch.setattr(router, "storage_for_write", lambda *_: fake_write)

    Stats.get().clear()
    result = await wrapp_copy("s3://source-bucket/source.txt", "https://target.example.com/destination.txt")
    assert result == Result.OK
    assert Stats.get().get_stat(StatType.COPY_ASYNC) == 1
    assert Stats.get().get_stat(StatType.READ_FILE_ASYNC) == 1
    assert Stats.get().get_stat(StatType.WRITE_FILE_ASYNC) == 1


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
    # If this fires, there is a new storage backend which needs a test here (the three above + local + invalid)
    assert len(StorageType) == 7


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
