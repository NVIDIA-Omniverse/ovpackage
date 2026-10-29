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
import errno
import logging
import os
import platform
import shutil
import sys
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import (
    List,
    Optional,
    Tuple,
)
from urllib.parse import quote

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.coro import assert_is_wrapp_thread
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage_interface import (
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
    file_url_to_path,
    is_file_url,
    is_windows_path,
)

logger = logging.getLogger("omni.wrapp")

# Try to import aiofiles - defer error to LocalFileStorage.__init__ so Router can
# import this module and gracefully mark the backend as DISABLED rather than crash.
_aiofiles_import_error: Optional[str] = None
try:
    import aiofiles
    import aiofiles.os
    import aiofiles.ospath
except ImportError as e:
    missing = getattr(e, "name", str(e))
    _aiofiles_import_error = (
        f"failed to import {missing}. Required dependencies: aiofiles. Install with 'pip install aiofiles'. Exception: {e}"
    )
    aiofiles = None  # type: ignore[assignment]


class LocalFileListEntry(ListEntry):
    """ListEntry implementation for local file system files and directories."""

    def __init__(
        self,
        path: str,
        *,
        relative_path: str,
        is_folder: bool,
        size: Optional[int] = None,
        modified_time: Optional[datetime] = None,
    ) -> None:
        """
        Initialize a LocalFileListEntry.

        Args:
            path: The full absolute path to the file/folder
            relative_path: The relative path (for display purposes)
            is_folder: True if this is a directory
            size: File size in bytes (None for folders)
            modified_time: Last modification time
        """
        self._path = path
        self._relative_path = relative_path.replace(os.sep, "/") if os.sep != "/" else relative_path
        self._is_folder = is_folder
        self._size = size
        self._modified_time = modified_time
        self._nucleus_hash: Optional[str] = None

    @property
    def is_folder(self) -> bool:
        return self._is_folder

    @property
    def is_regular_file(self) -> bool:
        return not self._is_folder

    @property
    def has_checkpoint(self) -> bool:
        # Local files don't have checkpoints
        return False

    @property
    def version(self) -> str:
        # Local files don't have versions
        return ""

    @property
    def relative_path(self) -> str:
        return self._relative_path

    @property
    def nucleus_hash(self) -> Optional[str]:
        return self._nucleus_hash

    @nucleus_hash.setter
    def nucleus_hash(self, value: str):
        if self._nucleus_hash is not None:
            raise ValueError(f"Cannot overwrite existing hash {self._nucleus_hash} with {value}")
        self._nucleus_hash = value

    @property
    def size(self) -> Optional[int]:
        return self._size

    @property
    def modified_time(self) -> Optional[datetime]:
        return self._modified_time

    def __repr__(self) -> str:
        return (
            f"LocalFileListEntry(path={self._path}, relative_path={self._relative_path}, "
            f"is_folder={self._is_folder}, size={self._size}, modified_time={self._modified_time})"
        )


class LocalFileServerInfo(ServerInfo):
    """ServerInfo implementation for local file system."""

    def supports_create_with_hash(self, target_url: str) -> bool:
        # Local files don't support creating from hash
        return False

    @property
    def auth_token(self) -> str:
        # No auth needed for local files
        return ""


class LocalReadOnlyFileImpl(LocalReadOnlyFile):
    """LocalReadOnlyFile implementation for local file system."""

    def __init__(self, file_path: str, url: str = ""):
        self._file_path = file_path
        # Add _path and _url for compatibility with ClientLibCacheFile interface
        self._path = file_path
        self._url = url

    def local_path(self) -> str:
        return self._file_path

    def close(self):
        # No-op for local files - they're already on disk
        pass


class LocalFileStorage(StorageInterface):
    """
    Storage implementation for local file system access using async file I/O.

    Handles file:// URLs independently of the Nucleus clientlib, enabling
    local file system access even when Nucleus is disabled.
    """

    @staticmethod
    def _to_extended_path(path: str) -> str:
        """
        Convert a path to Windows extended-length path format if needed.

        On Windows, paths longer than 260 characters can cause errors.
        This function prepends the '\\\\?\\' prefix for absolute paths on Windows
        to enable extended-length paths (up to ~32,767 characters).

        Args:
            path: The path to convert

        Returns:
            Extended-length path on Windows if applicable, original path otherwise
        """
        if platform.system() != "Windows":
            return path

        # Already an extended path
        if path.startswith("\\\\?\\"):
            return path

        # Convert to absolute path first
        abs_path = os.path.abspath(path)

        # UNC paths need special handling
        if abs_path.startswith("\\\\"):
            # UNC path: \\server\share -> \\?\UNC\server\share
            return "\\\\?\\UNC\\" + abs_path[2:]
        else:
            # Regular path: C:\path -> \\?\C:\path
            return "\\\\?\\" + abs_path

    @staticmethod
    def _strip_extended_prefix(path: str) -> str:
        """
        Strip Windows extended-length path prefix if present.

        This is used to normalize paths returned from os.walk() and os.scandir()
        which return extended paths when given extended input on Windows.

        Args:
            path: Path that may have extended prefix

        Returns:
            Path without extended prefix
        """
        extended_unc_prefix = "\\\\?\\UNC\\"
        extended_prefix = "\\\\?\\"

        if path.startswith(extended_unc_prefix):
            # \\?\UNC\server\share -> \\server\share
            return "\\\\" + path[len(extended_unc_prefix) :]
        elif path.startswith(extended_prefix):
            # \\?\C:\path -> C:\path
            return path[len(extended_prefix) :]
        else:
            return path

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_parameters: ThrottlingParameters):
        """
        Initialize LocalFileStorage.

        Args:
            wrapp_version: Version string for WRAPP
            debug_level: Enable debug level logging
            throttling_parameters: Throttling parameters for file operations

        Raises:
            StorageMissingDependenciesError: If aiofiles is not installed
        """
        if _aiofiles_import_error is not None:
            raise StorageMissingDependenciesError(_aiofiles_import_error)

        self._wrapp_version = wrapp_version
        self._debug_level = debug_level
        self._throttling_parameters = throttling_parameters

        # Create throttlers for file operations (matching S3Storage pattern)
        self._throttler_read = TaskThrottler(
            ThrottlerType.NetworkDataTransfer,
            "LocalFile",
            throttling_parameters.file_transfer_jobs,
        )
        self._throttler_write = TaskThrottler(
            ThrottlerType.NetworkDataTransfer,
            "LocalFile",
            throttling_parameters.file_transfer_jobs,
        )

        logger.info(f"LocalFileStorage initialized: version={wrapp_version}, debug={debug_level}")

    @staticmethod
    def version_info() -> str:
        """Return version information for this storage backend."""
        try:
            import aiofiles

            # aiofiles doesn't expose __version__, try to get it from metadata
            try:
                from importlib.metadata import version

                aiofiles_version = version("aiofiles")
                return f"LocalFileStorage with aiofiles {aiofiles_version}"
            except Exception:
                return "LocalFileStorage with aiofiles (version unknown)"
        except ImportError:
            return "LocalFileStorage (aiofiles not available)"

    def directory_semantics(self) -> DirectorySemantics:
        """Local file systems have proper directory semantics."""
        return DirectorySemantics.FILESYSTEM

    def supports_tags(self, url: str) -> bool:
        """Local files don't support tags."""
        return False

    def supports_comments(self) -> bool:
        """Local files don't support comments."""
        return False

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        """Local files don't support creating from hash."""
        return False

    def is_case_sensitive(self, url: str) -> bool:
        path = self._url_to_path(url)
        probe = path if os.path.isdir(path) else os.path.dirname(path)
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                return sys.platform == "linux"
            probe = parent
        if not probe:
            return sys.platform == "linux"
        swapped = probe.swapcase()
        if swapped == probe:
            return sys.platform == "linux"
        try:
            s_orig = os.stat(probe)
            s_swap = os.stat(swapped)
            # Same inode+device means the FS resolved both to the same entry → case-insensitive
            return not (s_orig.st_ino == s_swap.st_ino and s_orig.st_dev == s_swap.st_dev)
        except OSError:
            return True

    def can_be_cached(self, url: str) -> bool:
        """Local files are already on disk - they're always 'cached'."""
        return True

    def supports_list_file_tree(self) -> bool:
        """Local file system supports recursive tree listing."""
        return True

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, ResourceIdentity]:
        raise NotImplementedError("Resource identities not supported for local files")

    async def normalize(self, url: str) -> str:
        """
        Normalize a file:// URL to an absolute path.

        Args:
            url: The URL or path to normalize

        Returns:
            Normalized absolute path as a file:// URL
        """
        path = file_url_to_path(url)

        # Convert to absolute path
        abs_path = os.path.abspath(os.path.expanduser(path))

        # Normalize path separators for the platform
        abs_path = os.path.normpath(abs_path)

        # Convert back to file:// URL
        if is_windows_path(abs_path):
            # Windows paths need special handling
            return "file:///" + quote(abs_path.replace(os.sep, "/"), safe="/:")
        else:
            return "file://" + quote(abs_path, safe="/:")

    def _url_to_path(self, url: str) -> str:
        """Convert a URL to a local file system path, with Windows long path support."""
        path = os.path.normpath(file_url_to_path(url))

        # Apply Windows extended-length path prefix if needed
        return self._to_extended_path(path)

    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        """
        Get file/directory metadata.

        Args:
            url: The file:// URL to stat

        Returns:
            Tuple of (Result, ListEntry or None)
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(url)

            # Check if path exists
            exists = await aiofiles.ospath.exists(path)
            if not exists:
                return Result.ERROR_NOT_FOUND, None

            # Get file stats
            stat_result = await aiofiles.os.stat(path)

            is_folder = await aiofiles.ospath.isdir(path)
            size = None if is_folder else stat_result.st_size
            modified_time = datetime.fromtimestamp(stat_result.st_mtime)

            # Get relative path (just the filename for stat)
            relative_path = os.path.basename(path)

            entry = LocalFileListEntry(
                path=self._strip_extended_prefix(path),
                relative_path=relative_path,
                is_folder=is_folder,
                size=size,
                modified_time=modified_time,
            )

            return Result.OK, entry

        except PermissionError:
            return Result.ERROR_ACCESS_DENIED, None
        except Exception as e:
            logger.exception(f"Error stating {url}")
            return Result.ERROR, None

    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        """
        Check if URL points to a directory.

        Args:
            url: The file:// URL to check

        Returns:
            Tuple of (Result, is_folder boolean)
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(url)

            # Check if path exists
            exists = await aiofiles.ospath.exists(path)
            if not exists:
                return Result.ERROR_NOT_FOUND, False

            is_dir = await aiofiles.ospath.isdir(path)
            return Result.OK, is_dir

        except PermissionError:
            return Result.ERROR_ACCESS_DENIED, False
        except Exception as e:
            logger.exception(f"Error checking if {url} is folder")
            return Result.ERROR, False

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        """
        List immediate children of a directory.

        Args:
            url: The file:// URL of the directory to list

        Yields:
            ListEntry objects for each child
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(url)

            # Use os.scandir in executor for efficiency
            def _scandir():
                entries = []
                with os.scandir(path) as it:
                    for entry in it:
                        try:
                            stat_result = entry.stat(follow_symlinks=False)
                            is_dir = entry.is_dir(follow_symlinks=False)

                            list_entry = LocalFileListEntry(
                                path=LocalFileStorage._strip_extended_prefix(entry.path),
                                relative_path=entry.name,
                                is_folder=is_dir,
                                size=None if is_dir else stat_result.st_size,
                                modified_time=datetime.fromtimestamp(stat_result.st_mtime),
                            )
                            entries.append(list_entry)
                        except (OSError, PermissionError) as e:
                            logger.warning(f"Error accessing {entry.path}: {e}")
                            continue
                return entries

            entries = await asyncio.to_thread(_scandir)
            for entry in entries:
                yield entry

        except FileNotFoundError as e:
            raise StorageOperationError(f"Directory not found: {url}", Result.ERROR_NOT_FOUND) from e
        except PermissionError as e:
            raise StorageOperationError(f"Permission denied: {url}", Result.ERROR_ACCESS_DENIED) from e
        except NotADirectoryError as e:
            raise StorageOperationError(f"Not a directory: {url}", Result.ERROR_WRONG_TYPE) from e
        except Exception as e:
            logger.exception(f"Error listing directory {url}")
            raise StorageOperationError(f"Error listing directory: {e}", Result.ERROR) from e

    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        """
        Recursively list all files in a directory tree.

        Args:
            url: The file:// URL of the directory to list recursively

        Yields:
            ListEntry objects for all files in the tree
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(url)

            # Use os.walk in executor
            def _walk():
                entries = []
                for root, dirs, files in os.walk(path, followlinks=False):
                    # Add all files
                    for filename in files:
                        file_path = os.path.join(root, filename)
                        try:
                            stat_result = os.stat(file_path, follow_symlinks=False)
                            relative = os.path.relpath(file_path, path)

                            list_entry = LocalFileListEntry(
                                path=LocalFileStorage._strip_extended_prefix(file_path),
                                relative_path=relative,
                                is_folder=False,
                                size=stat_result.st_size,
                                modified_time=datetime.fromtimestamp(stat_result.st_mtime),
                            )
                            entries.append(list_entry)
                        except (OSError, PermissionError) as e:
                            logger.warning(f"Error accessing {file_path}: {e}")
                            continue

                    # Add all directories
                    for dirname in dirs:
                        dir_path = os.path.join(root, dirname)
                        try:
                            stat_result = os.stat(dir_path, follow_symlinks=False)
                            relative = os.path.relpath(dir_path, path)

                            list_entry = LocalFileListEntry(
                                path=LocalFileStorage._strip_extended_prefix(dir_path),
                                relative_path=relative,
                                is_folder=True,
                                size=None,
                                modified_time=datetime.fromtimestamp(stat_result.st_mtime),
                            )
                            entries.append(list_entry)
                        except (OSError, PermissionError) as e:
                            logger.warning(f"Error accessing {dir_path}: {e}")
                            continue

                return entries

            entries = await asyncio.to_thread(_walk)
            for entry in entries:
                yield entry

        except FileNotFoundError as e:
            raise StorageOperationError(f"Directory not found: {url}", Result.ERROR_NOT_FOUND) from e
        except PermissionError as e:
            raise StorageOperationError(f"Permission denied: {url}", Result.ERROR_ACCESS_DENIED) from e
        except NotADirectoryError as e:
            raise StorageOperationError(f"Not a directory: {url}", Result.ERROR_WRONG_TYPE) from e
        except Exception as e:
            logger.exception(f"Error listing file tree {url}")
            raise StorageOperationError(f"Error listing file tree: {e}", Result.ERROR) from e

    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        """Local files don't have checkpoints."""
        assert_is_wrapp_thread()
        return Result.OK, []

    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        """
        Read entire file content as bytes.

        Args:
            file_url: The file:// URL to read

        Returns:
            Tuple of (Result, file content as bytes)
        """
        assert_is_wrapp_thread()

        async def _read():
            try:
                path = self._url_to_path(file_url)

                async with aiofiles.open(path, "rb") as f:
                    content = await f.read()

                return Result.OK, content

            except FileNotFoundError:
                return Result.ERROR_NOT_FOUND, b""
            except PermissionError:
                return Result.ERROR_ACCESS_DENIED, b""
            except IsADirectoryError:
                return Result.ERROR_WRONG_TYPE, b""
            except Exception as e:
                logger.exception(f"Error reading file {file_url}")
                return Result.ERROR, b""

        return await self._throttler_read.run(_read())

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        """
        Write bytes to a file.

        Args:
            file_url: The file:// URL to write to
            content: The bytes to write
            comment: Comment (ignored for local files)

        Returns:
            Result status
        """
        assert_is_wrapp_thread()

        async def _write():
            try:
                path = self._url_to_path(file_url)

                # Create parent directories if needed
                parent = os.path.dirname(path)
                if parent:
                    await asyncio.to_thread(os.makedirs, parent, exist_ok=True)

                async with aiofiles.open(path, "wb") as f:
                    await f.write(content)

                return Result.OK

            except PermissionError:
                return Result.ERROR_ACCESS_DENIED
            except IsADirectoryError:
                return Result.ERROR_WRONG_TYPE
            except Exception as e:
                logger.exception(f"Error writing file {file_url}")
                return Result.ERROR

        return await self._throttler_write.run(_write())

    async def copy_address(
        self,
        src_url: str,
        dst_url: str,
        overwrite: bool = False,
        message: str = "",
    ) -> Result:
        """
        Copy a file.

        Args:
            src_url: Source file:// URL
            dst_url: Destination file:// URL
            overwrite: Whether to overwrite existing file
            message: Message/comment (ignored for local files)

        Returns:
            Result status
        """
        assert_is_wrapp_thread()

        async def _copy():
            try:
                src_path = self._url_to_path(src_url)
                dst_path = self._url_to_path(dst_url)

                # Check if source exists
                if not await aiofiles.ospath.exists(src_path):
                    return Result.ERROR_NOT_FOUND

                # Check if destination exists and overwrite flag
                if await aiofiles.ospath.exists(dst_path):
                    if not overwrite:
                        return Result.ERROR_ALREADY_EXISTS
                    # Prevent copying a file into a directory unintentionally
                    if await aiofiles.ospath.isdir(dst_path):
                        return Result.ERROR_ALREADY_EXISTS

                # Create parent directories if needed
                parent = os.path.dirname(dst_path)
                if parent:
                    await asyncio.to_thread(os.makedirs, parent, exist_ok=True)

                # Copy the file (preserving metadata)
                await asyncio.to_thread(shutil.copy2, src_path, dst_path)

                return Result.OK

            except FileNotFoundError:
                return Result.ERROR_NOT_FOUND
            except PermissionError:
                return Result.ERROR_ACCESS_DENIED
            except IsADirectoryError:
                return Result.ERROR_WRONG_TYPE
            except Exception as e:
                logger.exception(f"Error copying {src_url} to {dst_url}")
                return Result.ERROR

        return await self._throttler_write.run(_copy())

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def delete_item(self, url: str) -> Result:
        """
        Delete a file or empty directory.

        Args:
            url: The file:// URL to delete

        Returns:
            Result status
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(url)

            # Check if it's a directory
            if await aiofiles.ospath.isdir(path):
                # Try to remove directory (will fail if not empty)
                try:
                    await aiofiles.os.rmdir(path)
                    return Result.OK
                except OSError as e:
                    # Directory not empty
                    if e.errno == errno.ENOTEMPTY or "Directory not empty" in str(e):
                        return Result.ERROR_FOLDER_NOT_EMPTY
                    raise
            else:
                # Remove file
                await aiofiles.os.remove(path)
                return Result.OK

        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError:
            return Result.ERROR_ACCESS_DENIED
        except Exception as e:
            logger.exception(f"Error deleting {url}")
            return Result.ERROR

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        """
        Recursively delete a directory tree.

        Args:
            url: The file:// URL to delete
            task_node: Task node for progress tracking

        Returns:
            Result status
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(url)

            # Use shutil.rmtree in executor
            await asyncio.to_thread(shutil.rmtree, path)

            return Result.OK

        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError:
            return Result.ERROR_ACCESS_DENIED
        except Exception as e:
            logger.exception(f"Error deleting tree {url}")
            return Result.ERROR

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        """
        Get a cached file (local files are already cached).

        Args:
            file_url: The file:// URL
            download: Ignored (local files are always available)

        Returns:
            Tuple of (Result, LocalReadOnlyFile or None)
        """
        assert_is_wrapp_thread()

        try:
            path = self._url_to_path(file_url)

            # Check if file exists
            if not await aiofiles.ospath.exists(path):
                return Result.ERROR_NOT_FOUND, None

            # Check if it's a file (not a directory)
            if await aiofiles.ospath.isdir(path):
                return Result.ERROR_WRONG_TYPE, None

            # Preserve the extended-length prefix (\\?\) on Windows so that callers
            # can perform I/O operations (open, getsize, tar.add) on paths >260 chars.
            return Result.OK, LocalReadOnlyFileImpl(path, file_url)

        except PermissionError:
            return Result.ERROR_ACCESS_DENIED, None
        except Exception as e:
            logger.exception(f"Error getting cached file {file_url}")
            return Result.ERROR, None

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        """Local files don't support creating from hash."""
        assert_is_wrapp_thread()
        return Result.ERROR_NOT_SUPPORTED

    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        """Get server info for local file system."""
        assert_is_wrapp_thread()
        return Result.OK, LocalFileServerInfo()

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        """No-op: Local files don't need authentication."""
        pass

    def add_auth_info(self, auth_info: StorageAuthInfo):
        """No-op: Local files don't need authentication."""
        pass

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        """Not supported for local files."""
        raise NotImplementedError("Local file system does not support authentication parameters")

    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        """Local file system is always connected."""
        return ConnectionStatus.CONNECTED

    def backend_throttlers(self) -> list[TaskThrottler]:
        """Return list of throttlers for this backend."""
        return [self._throttler_read, self._throttler_write]

    def reconfigure_throttlers(self, params: ThrottlingParameters) -> bool:
        """
        Reconfigure throttlers with new parameters.

        Args:
            params: New throttling parameters

        Returns:
            True if successful
        """
        # Check if already configured with same params
        if (
            self._throttler_read.max_parallel_tasks == params.file_transfer_jobs
            and self._throttler_write.max_parallel_tasks == params.file_transfer_jobs
        ):
            return True

        # Check if there are open tasks
        for t in self.backend_throttlers():
            if t.pending_tasks or t.processing_tasks:
                logger.warning(
                    f"Cannot update throttling parameters for LocalFileStorage: Throttler {t.server_backend_name} has open tasks."
                )
                return False

        # Recreate throttlers with new parameters
        self._throttler_read = TaskThrottler(
            ThrottlerType.NetworkDataTransfer,
            "LocalFile",
            params.file_transfer_jobs,
        )
        self._throttler_write = TaskThrottler(
            ThrottlerType.NetworkDataTransfer,
            "LocalFile",
            params.file_transfer_jobs,
        )
        return True

    async def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        """Clean up resources."""
        # No cleanup needed for local file storage
        pass
