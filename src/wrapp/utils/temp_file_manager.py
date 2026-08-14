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

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from typing import (
    AsyncIterator,
    Optional,
)

from wrapp.utils.utils import (
    make_file_url_absolute,
    parse_env_int,
)

logger = logging.getLogger("omni.wrapp")

# Hardcoded defaults, overridable via environment variables
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024 * 1024  # 10 GB soft limit
_DEFAULT_MAX_FILES = 500  # hard file count limit


class TempFile:
    """
    Handle for a temporary file managed by TempFileManager.

    Provides ``path`` (local filesystem path) and ``url`` (file:// URL) for use with
    storage backends.  After writing content into the file, the caller must call
    ``write_completed()`` so the byte-accounting stays accurate.

    Used as an async context manager via ``TempFileManager.create_temp_file()``.
    Cleanup (file deletion, accounting update, waiter notification) happens
    automatically on context exit.
    """

    def __init__(self, path: str, manager: TempFileManager) -> None:
        self._path = path
        self._url = make_file_url_absolute(path)
        self._manager = manager
        self._reported_bytes: int = 0

    @property
    def path(self) -> str:
        """Local filesystem path of the temporary file."""
        return self._path

    @property
    def url(self) -> str:
        """``file://`` URL suitable for passing to storage backend download/upload methods."""
        return self._url

    def write_completed(self) -> None:
        """
        Signal that the temporary file has been completely written.

        Reads the file size from disk and updates the TempFileManager's byte
        accounting so the soft limit works correctly.  Call this after the
        download/write into the file is complete.
        """
        actual_bytes = os.path.getsize(self._path)
        self._manager._add_bytes(actual_bytes)
        self._reported_bytes = actual_bytes


class TempFileManager:
    """
    Centralized manager for temporary files used during cross-storage transfers.

    Provides a shared temporary directory and enforces capacity limits:

    * **Soft byte limit** (``max_bytes``, default 10 GB): ``create_temp_file()``
      blocks when the total reported bytes of currently-held temp files reaches
      or exceeds ``max_bytes``.  Once unblocked, any single file is allowed
      regardless of its eventual size.
    * **Hard file count limit** (``max_files``, default 500):
      ``create_temp_file()`` blocks when the number of currently-held temp files
      reaches ``max_files``.

    **Deadlock-prevention layering rule:**
    This class operates at the *command / utility* layer only.  Storage backend
    implementations (``StorageInterface`` subclasses) and any code running inside
    ``TaskThrottler`` semaphores **must NOT** use the TempFileManager.  Doing so
    risks deadlocks because the TempFileManager's internal ``asyncio.Condition``
    could interleave with the backend's throttling semaphore across concurrent
    async tasks.

    Typical usage::

        async with TempFileManager.me().create_temp_file() as tmp:
            await backend.download_file(src_url, tmp.url, overwrite=True)
            tmp.write_completed()
            await backend.upload_file(tmp.url, dst_url, overwrite=True)
        # tmp is deleted and accounting updated automatically
    """

    _instance: Optional[TempFileManager] = None

    @classmethod
    def initialize(cls) -> TempFileManager:
        """
        Create and store the singleton instance.

        Reads configuration from environment variables, falling back to
        hardcoded defaults:

        * ``WRAPP_MAX_TEMP_BYTES`` — soft byte limit (default 10 GB)
        * ``WRAPP_MAX_TEMP_FILES`` — hard file count limit (default 500)
        """
        max_bytes = parse_env_int("WRAPP_MAX_TEMP_BYTES", _DEFAULT_MAX_BYTES, minimum=1)
        max_files = parse_env_int("WRAPP_MAX_TEMP_FILES", _DEFAULT_MAX_FILES, minimum=1)
        cls._instance = TempFileManager(max_bytes=max_bytes, max_files=max_files)
        logger.debug(
            f"TempFileManager initialized: max_bytes={cls._instance._max_bytes}, "
            f"max_files={cls._instance._max_files}, "
            f"tmp_dir={cls._instance._tmp_dir}"
        )
        return cls._instance

    @classmethod
    def me(cls) -> TempFileManager:
        """Return the singleton instance.  Raises if ``initialize()`` has not been called."""
        if cls._instance is None:
            raise RuntimeError("TempFileManager has not been initialized. Call TempFileManager.initialize() first.")
        return cls._instance

    @classmethod
    def is_initialized(cls) -> bool:
        """Return whether the singleton has been initialized."""
        return cls._instance is not None

    def __init__(self, *, max_bytes: int = _DEFAULT_MAX_BYTES, max_files: int = _DEFAULT_MAX_FILES) -> None:
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._current_bytes: int = 0
        self._current_files: int = 0
        self._condition = asyncio.Condition()

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._tmp_dir_obj = tempfile.TemporaryDirectory(prefix=f"wrapp_{timestamp}_")
        self._tmp_dir: str = self._tmp_dir_obj.name

    @asynccontextmanager
    async def create_temp_file(self) -> AsyncIterator[TempFile]:
        """
        Async context manager that creates a capacity-managed temporary file.

        Blocks if the soft byte limit or the hard file count limit has been
        reached.  On exit, deletes the file, updates accounting, and wakes
        any waiters.
        """
        # Wait for capacity
        async with self._condition:
            await self._condition.wait_for(self._has_capacity)
            self._current_files += 1

        # Create the temp file (outside the lock — mkstemp is fast and non-blocking)
        fd, tmp_path = tempfile.mkstemp(prefix="wrapp_", dir=self._tmp_dir)
        os.close(fd)

        tmp = TempFile(tmp_path, self)
        try:
            yield tmp
        finally:
            # Clean up the file
            try:
                os.remove(tmp_path)
            except OSError:
                if os.path.exists(tmp_path):
                    logger.warning(f"Failed to remove temporary file: {tmp_path}")
            # Update accounting and notify waiters
            async with self._condition:
                self._current_bytes -= tmp._reported_bytes
                self._current_files -= 1
                self._condition.notify_all()

    def _has_capacity(self) -> bool:
        """Check whether a new temp file can be created without exceeding limits."""
        return self._current_bytes < self._max_bytes and self._current_files < self._max_files

    def _add_bytes(self, num_bytes: int) -> None:
        """Add to the byte counter (called by TempFile.write_completed)."""
        self._current_bytes += num_bytes

    def shutdown(self) -> None:
        """
        Clean up the shared temporary directory.

        Safe to call multiple times.  Also happens automatically via the
        ``TemporaryDirectory`` finalizer on GC / process exit.
        """
        try:
            self._tmp_dir_obj.cleanup()
        except OSError:
            if os.path.exists(self._tmp_dir):
                logger.warning(f"Failed to fully clean up temporary directory: {self._tmp_dir}")
        TempFileManager._instance = None
