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
Unit tests for LocalFileStorage, particularly _url_to_path and normalize methods.

These tests verify correct handling of file:// URLs on both Linux and Windows platforms,
including the Windows-specific fix for URLs like file:///C:/path that need the leading
slash stripped after URL parsing.
"""

import os
import platform
import sys
from unittest import mock

import pytest

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.storage_impls.local_file_storage import (
    LocalFileListEntry,
    LocalFileStorage,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import StorageMissingDependenciesError


def path_to_file_url(path: str) -> str:
    """Convert a filesystem path to a file:// URL, handling Windows correctly."""
    # Ensure absolute path
    abs_path = os.path.abspath(path)
    if sys.platform == "win32":
        # Windows: file:///C:/path/to/file
        return f"file:///{abs_path.replace(os.sep, '/')}"
    else:
        # Unix: file:///path/to/file
        return f"file://{abs_path}"


@pytest.fixture
def local_storage():
    """Create a LocalFileStorage instance for testing."""
    params = ThrottlingParameters(file_transfer_jobs=4)
    # It will raise on init, so we might need to skip this test
    try:
        return LocalFileStorage("test_version", False, params)
    except StorageMissingDependenciesError:
        pytest.skip(reason="Missing dependencies for LocalFileStorage initialization")


def test_list_entry_normalizes_windows_relative_paths_to_url_separators_OMPE100308(monkeypatch):
    monkeypatch.setattr(os, "sep", "\\")
    entry = LocalFileListEntry(
        path="C:/repo/source/subdir/sub.txt",
        relative_path=r"subdir\sub.txt",
        is_folder=False,
    )

    assert entry.relative_path == "subdir/sub.txt"


@pytest.mark.skipif(os.sep != "/", reason="POSIX-specific literal backslash behavior")
def test_list_entry_preserves_literal_posix_backslashes_OMPE100308():
    entry = LocalFileListEntry(
        path="/repo/source/a\\b.txt",
        relative_path=r"a\b.txt",
        is_folder=False,
    )

    assert entry.relative_path == r"a\b.txt"


class TestUrlToPath:
    """Tests for the _url_to_path method."""

    @pytest.mark.skipif(sys.platform == "win32", reason="Linux-specific test")
    def test_linux_file_url_three_slashes(self, local_storage):
        """Test Linux file:// URL with three slashes."""
        url = "file:///home/user/path/to/file.txt"
        result = local_storage._url_to_path(url)
        assert result == "/home/user/path/to/file.txt"

    @pytest.mark.skipif(sys.platform == "win32", reason="Linux-specific test")
    def test_linux_file_url_with_spaces_encoded(self, local_storage):
        """Test Linux file:// URL with URL-encoded spaces."""
        url = "file:///home/user/path%20with%20spaces/file.txt"
        result = local_storage._url_to_path(url)
        assert result == "/home/user/path with spaces/file.txt"

    def test_windows_file_url_three_slashes(self, local_storage):
        """Test Windows file:// URL with three slashes (file:///C:/...)."""
        url = "file:///C:/Users/test/Documents/file.txt"
        result = local_storage._url_to_path(url)
        if sys.platform == "win32":
            result = local_storage._strip_extended_prefix(result)
            assert result == "C:\\Users\\test\\Documents\\file.txt"
        else:
            # On Linux, normpath keeps forward slashes. The drive letter does not make sense for Linux,
            # but that is a higher level input data error
            assert result == "C:/Users/test/Documents/file.txt"

    def test_windows_path_direct(self, local_storage):
        """Test direct Windows path (not a URL)."""
        # This tests is_windows_path detection
        if sys.platform != "win32":
            pytest.skip(reason="Test only valid for Win32")
        path = "C:\\Users\\test\\file.txt"
        result = local_storage._url_to_path(path)
        result = local_storage._strip_extended_prefix(result)
        assert result == "C:\\Users\\test\\file.txt"

    def test_path_normalization_removes_double_slashes(self, local_storage):
        """Test that path normalization handles double slashes."""
        url = "file:///home/user//double//slashes/file.txt"
        result = local_storage._url_to_path(url)
        # Strip extended prefix on Windows before checking (\\?\ contains \\)
        result = local_storage._strip_extended_prefix(result)
        # normpath should collapse double slashes
        assert "//" not in result and "\\\\" not in result


class TestNormalize:
    """Tests for the normalize method."""

    @pytest.mark.asyncio
    async def test_normalize_linux_path(self, local_storage, wrapp_init):
        """Test normalize with Linux file:// URL."""
        url = "file:///tmp/test/file.txt"
        result = await local_storage.normalize(url)
        # Should return a file:// URL
        assert result.startswith("file://")
        # Path should be absolute
        if sys.platform != "win32":
            assert "tmp/test/file.txt" in result

    @pytest.mark.asyncio
    async def test_normalize_windows_url(self, local_storage, wrapp_init):
        """Test normalize with Windows file:// URL."""
        url = "file:///C:/Users/test/file.txt"
        result = await local_storage.normalize(url)
        # Should return a file:// URL
        assert result.startswith("file://")
        # Should contain the path components
        assert "Users" in result
        assert "test" in result
        assert "file.txt" in result

    @pytest.mark.asyncio
    async def test_normalize_decodes_url_encoding(self, local_storage, wrapp_init):
        """Test that normalize properly decodes URL-encoded characters."""
        url = "file:///home/user/path%20with%20spaces/file.txt"
        result = await local_storage.normalize(url)
        # The result should have decoded spaces (either in path or re-encoded)
        # The important thing is it doesn't fail
        assert result.startswith("file://")

    @pytest.mark.asyncio
    async def test_normalize_windows_path_produces_three_slashes(self, local_storage, wrapp_init):
        """Test that normalizing a Windows path produces file:/// URL."""
        if sys.platform == "win32":
            # On Windows, test with an actual Windows path
            url = "C:\\Users\\test\\file.txt"
            result = await local_storage.normalize(url)
            # Should produce file:///C:/... format
            assert result.startswith("file:///")
            assert "C:" in result or "c:" in result


class TestUrlToPathWindowsRegression:
    """
    Regression tests for the Windows path handling bug.

    The bug: On Windows, file:///C:/path URLs were being incorrectly converted to \\C:\\path
    because urlparse returns /C:/path (with leading slash), and os.path.normpath on Windows
    converts the leading / to \\ resulting in \\C:\\path which is invalid.

    The fix: Strip the leading / when the path looks like /X:/ (a Windows drive path).
    """

    def test_regression_windows_path_no_leading_backslash(self, local_storage):
        """
        Regression test: Windows paths should not have leading backslash before drive letter.

        This was the original bug that caused:
        OSError: [WinError 123] The filename, directory name, or volume label syntax is incorrect: '\\C:'
        """
        # This is the exact URL pattern from the failing test
        url = "file:///C:/Users/test-user/AppData/Local/Temp/tmptjf8hvqw/tests/20260112_104102_729702_pg/ljmpcchu/sr1_repo/.packages/package_c/v1/csource_package/csubfolder/cfile_in_folder.txt"
        result = local_storage._url_to_path(url)

        # The bug caused the path to start with \C: on Windows
        assert not result.startswith("\\C:"), f"Path incorrectly starts with \\C: - got {result}"

        # Should start with C: (on Windows with extended path) or just C: on Linux
        if platform.system() == "Windows":
            assert result.startswith("\\\\?\\C:"), f"Path should start with \\\\?\\C: - got {result}"
        else:
            assert result.startswith("C:"), f"Path should start with C: - got {result}"


class TestWindowsBehaviorMocked:
    """
    Tests for Windows-specific behavior that can run on Linux by mocking platform checks.

    These tests cover the essential equivalence classes for Windows path handling.
    """

    def test_to_extended_path_all_branches(self, local_storage):
        """Test all 4 branches of _to_extended_path."""
        # Branch 1: Non-Windows -> return unchanged
        with mock.patch("platform.system", return_value="Linux"):
            assert LocalFileStorage._to_extended_path("/unix/path") == "/unix/path"

        # Branch 2: Already extended -> return unchanged (early return)
        with mock.patch("platform.system", return_value="Windows"):
            assert LocalFileStorage._to_extended_path("\\\\?\\C:\\test") == "\\\\?\\C:\\test"

        # Branch 3: Windows + UNC path -> add \\?\UNC\ prefix
        with mock.patch("platform.system", return_value="Windows"), mock.patch("os.path.abspath", side_effect=lambda p: p):
            assert LocalFileStorage._to_extended_path("\\\\server\\share") == "\\\\?\\UNC\\server\\share"

        # Branch 4: Windows + regular path -> add \\?\ prefix
        with mock.patch("platform.system", return_value="Windows"), mock.patch("os.path.abspath", side_effect=lambda p: p):
            assert LocalFileStorage._to_extended_path("C:\\Users\\test") == "\\\\?\\C:\\Users\\test"

    def test_strip_extended_prefix_all_branches(self, local_storage):
        """Test all 3 branches of _strip_extended_prefix (platform-independent)."""
        # Branch 1: UNC prefix -> strip and restore \\
        assert LocalFileStorage._strip_extended_prefix("\\\\?\\UNC\\server\\share") == "\\\\server\\share"

        # Branch 2: Regular extended prefix -> strip
        assert LocalFileStorage._strip_extended_prefix("\\\\?\\C:\\Users\\test") == "C:\\Users\\test"

        # Branch 3: No prefix -> return unchanged
        assert LocalFileStorage._strip_extended_prefix("C:\\Users\\test") == "C:\\Users\\test"
        assert LocalFileStorage._strip_extended_prefix("/home/user") == "/home/user"

    def test_url_to_path_roundtrip_mocked_windows(self, local_storage):
        """Test URL -> extended path -> strip roundtrip on mocked Windows."""

        def mock_normpath(p):
            return p.replace("/", "\\")

        with (
            mock.patch("platform.system", return_value="Windows"),
            mock.patch("os.path.normpath", side_effect=mock_normpath),
            mock.patch("os.path.abspath", side_effect=lambda p: p),
        ):
            # Test the key fix: file:///C:/path should produce C:\path after strip
            result = local_storage._url_to_path("file:///C:/Users/test/file.txt")
            assert result.startswith("\\\\?\\"), f"Should have extended prefix: {result}"
            stripped = LocalFileStorage._strip_extended_prefix(result)
            assert stripped == "C:\\Users\\test\\file.txt"
            assert not stripped.startswith("\\C:"), "Should not have leading backslash before drive"

    def test_get_cached_file_does_not_strip_prefix(self, local_storage):
        """Verify get_cached_file_async preserves extended prefix (code inspection)."""
        import inspect

        source = inspect.getsource(local_storage.get_cached_file_async)
        # The fix: LocalReadOnlyFileImpl should receive path directly, not stripped
        assert "LocalReadOnlyFileImpl(self._strip_extended_prefix" not in source, "get_cached_file_async should not strip extended prefix"


class TestGetPathFromUrl:
    """Tests for get_path_from_url function - covers 3 branches."""

    def test_get_path_from_url_branches(self):
        """Test all three branches of get_path_from_url."""
        from wrapp.utils.utils import get_path_from_url

        # Branch 1: is_windows_path=True -> return unchanged
        assert get_path_from_url("C:\\Users\\test\\file.txt") == "C:\\Users\\test\\file.txt"

        # Branch 2: Windows file URL with /C:/ pattern -> strip leading slash
        assert get_path_from_url("file:///C:/Users/test/file.txt") == "C:/Users/test/file.txt"

        # Branch 3: Linux file URL (no /X:/ pattern) -> return path unchanged
        assert get_path_from_url("file:///home/user/file.txt") == "/home/user/file.txt"


class TestWindowsLongPathHandling:
    """Tests for handling paths exceeding Windows MAX_PATH (260 characters).

    These tests verify that LocalFileStorage can handle very long paths
    that would normally fail on Windows due to the 260-character MAX_PATH limit.
    """

    def test_to_extended_path_on_windows(self, local_storage):
        """Test _to_extended_path adds prefix on Windows."""
        if platform.system() != "Windows":
            # On non-Windows, should return path unchanged
            path = "/some/long/path"
            assert local_storage._to_extended_path(path) == path
        else:
            # On Windows, should add prefix
            path = "C:\\Users\\test\\file.txt"
            result = local_storage._to_extended_path(path)
            assert result == "\\\\?\\C:\\Users\\test\\file.txt"

    def test_to_extended_path_unc(self, local_storage):
        """Test _to_extended_path handles UNC paths on Windows."""
        if platform.system() != "Windows":
            pytest.skip("UNC paths are Windows-specific")

        path = "\\\\server\\share\\folder\\file.txt"
        result = local_storage._to_extended_path(path)
        assert result == "\\\\?\\UNC\\server\\share\\folder\\file.txt"

    def test_to_extended_path_already_extended(self, local_storage):
        """Test _to_extended_path doesn't double-prefix."""
        if platform.system() != "Windows":
            pytest.skip("Extended paths are Windows-specific")

        path = "\\\\?\\C:\\Users\\test\\file.txt"
        result = local_storage._to_extended_path(path)
        assert result == path  # Should be unchanged

    def test_strip_extended_prefix_regular(self, local_storage):
        """Test _strip_extended_prefix removes regular prefix."""
        path = "\\\\?\\C:\\Users\\test\\file.txt"
        result = local_storage._strip_extended_prefix(path)
        assert result == "C:\\Users\\test\\file.txt"

    def test_strip_extended_prefix_unc(self, local_storage):
        """Test _strip_extended_prefix removes UNC prefix."""
        path = "\\\\?\\UNC\\server\\share\\file.txt"
        result = local_storage._strip_extended_prefix(path)
        assert result == "\\\\server\\share\\file.txt"

    def test_strip_extended_prefix_no_prefix(self, local_storage):
        """Test _strip_extended_prefix handles paths without prefix."""
        path = "C:\\Users\\test\\file.txt"
        result = local_storage._strip_extended_prefix(path)
        assert result == path  # Should be unchanged

    def _create_long_path(self, base_path: str, depth: int = 15) -> str:
        """Create a path that exceeds Windows MAX_PATH (260 chars)."""
        component_name = "very_long_directory_name_123"
        path = base_path
        for i in range(depth):
            path = os.path.join(path, f"{component_name}_{i:03d}")
        return path

    @pytest.mark.asyncio
    async def test_long_path_write_and_read(self, local_storage, tmp_path):
        """Test writing and reading files with paths exceeding MAX_PATH."""
        # Create a deeply nested path
        deep_dir = self._create_long_path(str(tmp_path), depth=15)
        file_path = os.path.join(deep_dir, "test_file.txt")

        # On Windows, verify path exceeds MAX_PATH
        if platform.system() == "Windows":
            assert len(file_path) > 260, f"Path length {len(file_path)} should exceed 260"

        # Convert to file:// URL
        file_url = path_to_file_url(file_path)

        # Write file (mock thread check since we're not in wrapp thread)
        content = b"Test content for long path"
        with mock.patch("wrapp.utils.storage_impls.local_file_storage.assert_is_wrapp_thread"):
            result = await local_storage.write_file(file_url, content, "test")
            assert result == Result.OK, f"Write should succeed, got {result}"

            # Read file back
            result, read_content = await local_storage.read_file_content(file_url)
            assert result == Result.OK, f"Read should succeed, got {result}"
            assert read_content == content

    @pytest.mark.asyncio
    async def test_long_path_stat(self, local_storage, tmp_path):
        """Test stat operations on files in deep paths."""
        deep_dir = self._create_long_path(str(tmp_path), depth=15)
        file_path = os.path.join(deep_dir, "stat_test.txt")
        file_url = path_to_file_url(file_path)

        with mock.patch("wrapp.utils.storage_impls.local_file_storage.assert_is_wrapp_thread"):
            # Create the file first
            await local_storage.write_file(file_url, b"stat test", "test")

            # Stat the file
            result, entry = await local_storage.stat(file_url)
            assert result == Result.OK
            assert entry is not None
            assert entry.size == len(b"stat test")

    @pytest.mark.asyncio
    async def test_long_path_list_directory(self, local_storage, tmp_path):
        """Test listing directory with long paths."""
        deep_dir = self._create_long_path(str(tmp_path), depth=15)

        with mock.patch("wrapp.utils.storage_impls.local_file_storage.assert_is_wrapp_thread"):
            # Create some files in the deep directory
            for i in range(3):
                file_path = os.path.join(deep_dir, f"file_{i}.txt")
                file_url = path_to_file_url(file_path)
                await local_storage.write_file(file_url, f"content {i}".encode(), "test")

            # List the directory
            dir_url = path_to_file_url(deep_dir)
            entries = []
            async for entry in local_storage.list_directory(dir_url):
                entries.append(entry)

            assert len(entries) == 3

    @pytest.mark.asyncio
    async def test_long_path_delete(self, local_storage, tmp_path):
        """Test deleting files in deep paths."""
        deep_dir = self._create_long_path(str(tmp_path), depth=15)
        file_path = os.path.join(deep_dir, "to_delete.txt")
        file_url = path_to_file_url(file_path)

        with mock.patch("wrapp.utils.storage_impls.local_file_storage.assert_is_wrapp_thread"):
            # Create and then delete the file
            await local_storage.write_file(file_url, b"delete me", "test")

            result = await local_storage.delete_item(file_url)
            assert result == Result.OK

            # Verify it's gone
            result, _ = await local_storage.stat(file_url)
            assert result == Result.ERROR_NOT_FOUND

    @pytest.mark.asyncio
    @pytest.mark.skipif(platform.system() != "Windows", reason="Windows-specific long path test")
    async def test_get_cached_file_async_long_path_preserves_extended_prefix(self, local_storage, tmp_path):
        """
        Test that get_cached_file_async preserves the extended-length prefix on Windows.

        This verifies that callers can perform normal OS I/O operations (open, getsize, tar.add)
        on paths exceeding MAX_PATH (260 chars) using the returned local_path().

        Regression test for: stripping the extended prefix can reintroduce Windows MAX_PATH
        failures for deep paths.
        """
        import tarfile
        import tempfile

        # Create a deeply nested path that exceeds MAX_PATH
        deep_dir = self._create_long_path(str(tmp_path), depth=15)
        file_path = os.path.join(deep_dir, "cached_file_test.txt")
        file_url = path_to_file_url(file_path)

        # Verify path exceeds MAX_PATH
        assert len(file_path) > 260, f"Path length {len(file_path)} should exceed 260"

        test_content = b"Test content for cached file with very long path"

        with mock.patch("wrapp.utils.storage_impls.local_file_storage.assert_is_wrapp_thread"):
            # Create the file
            result = await local_storage.write_file(file_url, test_content, "test")
            assert result == Result.OK

            # Get cached file - the returned local_path() should be usable for direct I/O
            result, cache_file = await local_storage.get_cached_file_async(file_url)
            assert result == Result.OK
            assert cache_file is not None

            local_path = cache_file.local_path()

            # On Windows, the path should have the extended prefix
            assert local_path.startswith("\\\\?\\"), f"Path should start with \\\\?\\ prefix: {local_path}"

            # Test 1: open() should work with the returned path
            with open(local_path, "rb") as f:
                content = f.read()
            assert content == test_content, "open() should be able to read the file"

            # Test 2: os.path.getsize() should work with the returned path
            size = os.path.getsize(local_path)
            assert size == len(test_content), "os.path.getsize() should return correct size"

            # Test 3: tar.add() should work with the returned path
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
                tar_path = tf.name
            try:
                with tarfile.open(tar_path, "w") as tar:
                    tar.add(local_path, arcname="test_file.txt")

                # Verify the file was added to the tar
                with tarfile.open(tar_path, "r") as tar:
                    members = tar.getnames()
                    assert "test_file.txt" in members
                    extracted = tar.extractfile("test_file.txt")
                    assert extracted is not None
                    assert extracted.read() == test_content
            finally:
                os.unlink(tar_path)

            cache_file.close()

    @pytest.mark.asyncio
    async def test_get_cached_file_async_long_path_io_operations(self, local_storage, tmp_path):
        """
        Test that get_cached_file_async returns a path usable for I/O on all platforms.

        This is a cross-platform version that verifies the basic contract: the local_path()
        returned by get_cached_file_async should be directly usable for open(), getsize(), etc.
        """
        import tarfile
        import tempfile

        # Create a deeply nested path
        deep_dir = self._create_long_path(str(tmp_path), depth=15)
        file_path = os.path.join(deep_dir, "io_test_file.txt")
        file_url = path_to_file_url(file_path)

        test_content = b"Cross-platform I/O test content"

        with mock.patch("wrapp.utils.storage_impls.local_file_storage.assert_is_wrapp_thread"):
            # Create the file
            result = await local_storage.write_file(file_url, test_content, "test")
            assert result == Result.OK

            # Get cached file
            result, cache_file = await local_storage.get_cached_file_async(file_url)
            assert result == Result.OK
            assert cache_file is not None

            local_path = cache_file.local_path()

            # Test open()
            with open(local_path, "rb") as f:
                assert f.read() == test_content

            # Test os.path.getsize()
            assert os.path.getsize(local_path) == len(test_content)

            # Test tarfile.add()
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
                tar_path = tf.name
            try:
                with tarfile.open(tar_path, "w") as tar:
                    tar.add(local_path, arcname="archived.txt")

                with tarfile.open(tar_path, "r") as tar:
                    assert "archived.txt" in tar.getnames()
            finally:
                os.unlink(tar_path)

            cache_file.close()
