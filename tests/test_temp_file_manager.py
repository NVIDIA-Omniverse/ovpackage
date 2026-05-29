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
import os
import random
import string

import pytest

from tests.common import run_wrapp
from wrapp.commands import catalog
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.two_way_diff import Diff
from wrapp.utils.temp_file_manager import (
    _DEFAULT_MAX_BYTES,
    _DEFAULT_MAX_FILES,
    TempFileManager,
)
from wrapp.utils.utils import join_paths


@pytest.fixture
def temp_file_manager():
    """Create a TempFileManager for testing, and shut it down after the test."""
    mgr = TempFileManager(max_bytes=1024, max_files=3)
    yield mgr
    mgr.shutdown()


@pytest.fixture
def singleton_manager():
    """Initialize the TempFileManager singleton for testing, and shut it down after the test."""
    mgr = TempFileManager.initialize()
    yield mgr
    mgr.shutdown()


@pytest.mark.asyncio
async def test_create_temp_file_basic(temp_file_manager):
    """A temp file is created on disk and cleaned up on context exit."""
    async with temp_file_manager.create_temp_file() as tmp:
        assert os.path.exists(tmp.path)
        assert tmp.url.startswith("file:")
        assert tmp.path.replace("\\", "/") in tmp.url
        # Write something
        with open(tmp.path, "wb") as f:
            f.write(b"hello")
        tmp.write_completed()
    # File should be deleted after context exit
    assert not os.path.exists(tmp.path)


@pytest.mark.asyncio
async def test_accounting_bytes(temp_file_manager):
    """write_completed updates current_bytes, and exiting the context resets it."""
    assert temp_file_manager._current_bytes == 0
    assert temp_file_manager._current_files == 0

    async with temp_file_manager.create_temp_file() as tmp:
        assert temp_file_manager._current_files == 1
        assert temp_file_manager._current_bytes == 0
        with open(tmp.path, "wb") as f:
            f.write(b"x" * 100)
        tmp.write_completed()
        assert temp_file_manager._current_bytes == 100

    assert temp_file_manager._current_bytes == 0
    assert temp_file_manager._current_files == 0


@pytest.mark.asyncio
async def test_file_count_limit_blocks(temp_file_manager):
    """When max_files is reached, create_temp_file blocks until a slot is freed."""
    # max_files=3, so fill all 3 slots
    acquired = []
    cm1 = temp_file_manager.create_temp_file()
    cm2 = temp_file_manager.create_temp_file()
    cm3 = temp_file_manager.create_temp_file()

    tmp1 = await cm1.__aenter__()
    tmp2 = await cm2.__aenter__()
    tmp3 = await cm3.__aenter__()
    acquired.extend([tmp1, tmp2, tmp3])
    assert temp_file_manager._current_files == 3

    # A 4th allocation should block
    blocked = asyncio.Event()
    unblocked = asyncio.Event()

    async def try_allocate():
        blocked.set()
        async with temp_file_manager.create_temp_file() as tmp4:
            unblocked.set()
            acquired.append(tmp4)

    task = asyncio.create_task(try_allocate())

    # Give the task a chance to start and block
    await blocked.wait()
    await asyncio.sleep(0.05)
    assert not unblocked.is_set(), "4th allocation should be blocked"
    assert temp_file_manager._current_files == 3

    # Release one slot
    await cm1.__aexit__(None, None, None)
    # The blocked task should now proceed
    await asyncio.wait_for(unblocked.wait(), timeout=2.0)

    # Clean up
    await cm2.__aexit__(None, None, None)
    await cm3.__aexit__(None, None, None)
    await task

    assert temp_file_manager._current_files == 0
    assert temp_file_manager._current_bytes == 0


@pytest.mark.asyncio
async def test_byte_limit_blocks(temp_file_manager):
    """When current_bytes >= max_bytes, create_temp_file blocks until bytes are freed."""
    # max_bytes=1024
    cm1 = temp_file_manager.create_temp_file()
    tmp1 = await cm1.__aenter__()
    with open(tmp1.path, "wb") as f:
        f.write(b"x" * 1024)
    tmp1.write_completed()  # Fill the byte budget
    assert temp_file_manager._current_bytes == 1024

    blocked = asyncio.Event()
    unblocked = asyncio.Event()

    async def try_allocate():
        blocked.set()
        async with temp_file_manager.create_temp_file():
            unblocked.set()

    task = asyncio.create_task(try_allocate())
    await blocked.wait()
    await asyncio.sleep(0.05)
    assert not unblocked.is_set(), "Allocation should block when bytes >= max_bytes"

    # Free the bytes
    await cm1.__aexit__(None, None, None)
    await asyncio.wait_for(unblocked.wait(), timeout=2.0)
    await task


@pytest.mark.asyncio
async def test_soft_limit_allows_any_single_file():
    """Once unblocked, any single file is allowed regardless of eventual size."""
    mgr = TempFileManager(max_bytes=100, max_files=10)
    try:
        async with mgr.create_temp_file() as tmp:
            # Write much more than max_bytes — that's fine for a single file
            with open(tmp.path, "wb") as f:
                f.write(b"x" * 5000)
            tmp.write_completed()
            assert mgr._current_bytes == 5000
        assert mgr._current_bytes == 0
    finally:
        mgr.shutdown()


@pytest.mark.asyncio
async def test_concurrent_allocations(temp_file_manager):
    """Multiple concurrent tasks can allocate and release temp files."""
    results = []

    async def worker(worker_id: int):
        async with temp_file_manager.create_temp_file() as tmp:
            with open(tmp.path, "wb") as f:
                data = bytes([worker_id]) * 10
                f.write(data)
            tmp.write_completed()
            # Simulate some async work
            await asyncio.sleep(0.01)
            results.append(worker_id)

    # Run 6 workers with max_files=3 — they should all complete
    tasks = [asyncio.create_task(worker(i)) for i in range(6)]
    await asyncio.gather(*tasks)

    assert sorted(results) == list(range(6))
    assert temp_file_manager._current_files == 0
    assert temp_file_manager._current_bytes == 0


@pytest.mark.asyncio
async def test_cleanup_on_exception(temp_file_manager):
    """The temp file is cleaned up even if an exception occurs inside the context."""
    tmp_path = None
    with pytest.raises(RuntimeError, match="test error"):
        async with temp_file_manager.create_temp_file() as tmp:
            tmp_path = tmp.path
            with open(tmp.path, "wb") as f:
                f.write(b"x" * 50)
            tmp.write_completed()
            assert os.path.exists(tmp_path)
            raise RuntimeError("test error")

    assert tmp_path is not None
    assert not os.path.exists(tmp_path)
    assert temp_file_manager._current_bytes == 0
    assert temp_file_manager._current_files == 0


@pytest.mark.asyncio
async def test_shutdown_cleans_directory(temp_file_manager):
    """shutdown() removes the shared temporary directory and all contents."""
    tmp_dir = temp_file_manager._tmp_dir
    assert os.path.isdir(tmp_dir)

    # Create a file inside via the manager
    async with temp_file_manager.create_temp_file() as tmp:
        with open(tmp.path, "wb") as f:
            f.write(b"x" * 100)

    # The individual file is gone, but the directory still exists
    assert os.path.isdir(tmp_dir)

    temp_file_manager.shutdown()
    assert not os.path.exists(tmp_dir)


@pytest.mark.asyncio
async def test_singleton_lifecycle(singleton_manager):
    """initialize() creates a singleton accessible via me(), shutdown() clears it."""
    assert TempFileManager.is_initialized()
    assert TempFileManager.me() is singleton_manager

    singleton_manager.shutdown()
    assert not TempFileManager.is_initialized()

    with pytest.raises(RuntimeError, match="has not been initialized"):
        TempFileManager.me()


def test_singleton_not_initialized():
    """me() raises RuntimeError if initialize() has not been called."""
    # Ensure clean state
    TempFileManager._instance = None
    with pytest.raises(RuntimeError, match="has not been initialized"):
        TempFileManager.me()


@pytest.mark.asyncio
async def test_env_var_override(monkeypatch):
    """Environment variables override hardcoded defaults."""
    monkeypatch.setenv("WRAPP_MAX_TEMP_BYTES", "2048")
    monkeypatch.setenv("WRAPP_MAX_TEMP_FILES", "7")

    mgr = TempFileManager.initialize()
    try:
        assert mgr._max_bytes == 2048
        assert mgr._max_files == 7
    finally:
        mgr.shutdown()


@pytest.mark.asyncio
async def test_temp_dir_has_timestamp_prefix(temp_file_manager):
    """The shared temp directory name contains a wrapp_ timestamp prefix."""
    dirname = os.path.basename(temp_file_manager._tmp_dir)
    assert dirname.startswith("wrapp_")
    # Should contain a date-like pattern: wrapp_YYYYMMDD_HHMMSS_
    parts = dirname.split("_")
    assert len(parts) >= 3
    # parts[1] should be the date (8 digits), parts[2] the time (6 digits)
    assert len(parts[1]) == 8 and parts[1].isdigit()
    assert len(parts[2]) == 6 and parts[2].isdigit()


@pytest.mark.asyncio
async def test_env_var_non_numeric_falls_back_to_default(monkeypatch):
    """Non-numeric environment variables fall back to hardcoded defaults."""
    monkeypatch.setenv("WRAPP_MAX_TEMP_BYTES", "not_a_number")
    monkeypatch.setenv("WRAPP_MAX_TEMP_FILES", "abc")

    mgr = TempFileManager.initialize()
    try:
        assert mgr._max_bytes == _DEFAULT_MAX_BYTES
        assert mgr._max_files == _DEFAULT_MAX_FILES
    finally:
        mgr.shutdown()


@pytest.mark.asyncio
async def test_env_var_zero_falls_back_to_default(monkeypatch):
    """Zero-valued environment variables fall back to hardcoded defaults."""
    monkeypatch.setenv("WRAPP_MAX_TEMP_BYTES", "0")
    monkeypatch.setenv("WRAPP_MAX_TEMP_FILES", "0")

    mgr = TempFileManager.initialize()
    try:
        assert mgr._max_bytes == _DEFAULT_MAX_BYTES
        assert mgr._max_files == _DEFAULT_MAX_FILES
    finally:
        mgr.shutdown()


@pytest.mark.asyncio
async def test_env_var_negative_falls_back_to_default(monkeypatch):
    """Negative environment variables fall back to hardcoded defaults."""
    monkeypatch.setenv("WRAPP_MAX_TEMP_BYTES", "-100")
    monkeypatch.setenv("WRAPP_MAX_TEMP_FILES", "-5")

    mgr = TempFileManager.initialize()
    try:
        assert mgr._max_bytes == _DEFAULT_MAX_BYTES
        assert mgr._max_files == _DEFAULT_MAX_FILES
    finally:
        mgr.shutdown()


@pytest.mark.asyncio
async def test_env_var_partial_override(monkeypatch):
    """Setting only one env var overrides just that one, the other keeps the default."""
    monkeypatch.setenv("WRAPP_MAX_TEMP_FILES", "42")
    # WRAPP_MAX_TEMP_BYTES is not set

    mgr = TempFileManager.initialize()
    try:
        assert mgr._max_bytes == _DEFAULT_MAX_BYTES
        assert mgr._max_files == 42
    finally:
        mgr.shutdown()


# ---------------------------------------------------------------------------
# Warning-on-failed-cleanup tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_failure_warns_when_file_still_exists(temp_file_manager, caplog):
    """If os.remove fails and the file still exists, a warning is logged."""
    async with temp_file_manager.create_temp_file() as tmp:
        with open(tmp.path, "wb") as f:
            f.write(b"data")
        tmp.write_completed()
        saved_path = tmp.path
        # Replace the file with a directory of the same name.
        # os.remove() on a directory always raises IsADirectoryError (even as root),
        # and os.path.exists() returns True, so the warning path is triggered.
        os.remove(saved_path)
        os.mkdir(saved_path)

    assert "Failed to remove temporary file" in caplog.text
    assert saved_path in caplog.text
    # Clean up the directory we created
    os.rmdir(saved_path)


@pytest.mark.asyncio
async def test_remove_failure_silent_when_file_already_gone(temp_file_manager, monkeypatch, caplog):
    """If os.remove fails but the file is already gone, no warning is logged."""
    async with temp_file_manager.create_temp_file() as tmp:
        with open(tmp.path, "wb") as f:
            f.write(b"data")
        tmp.write_completed()
        # Delete the file before context exit so os.remove will fail with FileNotFoundError
        os.remove(tmp.path)

    assert "Failed to remove temporary file" not in caplog.text


@pytest.mark.asyncio
async def test_shutdown_warns_when_directory_still_exists(monkeypatch, caplog):
    """If TemporaryDirectory.cleanup() fails and the directory still exists, a warning is logged."""
    mgr = TempFileManager(max_bytes=1024, max_files=3)
    tmp_dir = mgr._tmp_dir
    assert os.path.isdir(tmp_dir)

    # Make cleanup() raise while the directory still exists
    monkeypatch.setattr(mgr._tmp_dir_obj, "cleanup", _raise_oserror)

    mgr.shutdown()
    assert "Failed to fully clean up temporary directory" in caplog.text
    assert tmp_dir in caplog.text
    # Clean up manually since we prevented the real cleanup
    os.rmdir(tmp_dir)


@pytest.mark.asyncio
async def test_shutdown_silent_on_double_cleanup(temp_file_manager, caplog):
    """Calling shutdown() twice does not produce a warning (directory already gone)."""
    temp_file_manager.shutdown()
    # Second call — directory is already gone
    # Re-create a minimal manager to call shutdown on (since fixture will also call shutdown)
    mgr = TempFileManager(max_bytes=1024, max_files=3)
    mgr.shutdown()
    mgr.shutdown()  # Third call for good measure
    assert "Failed to fully clean up temporary directory" not in caplog.text


def _raise_oserror(*args, **kwargs):
    raise OSError("simulated failure")


# ---------------------------------------------------------------------------
# Stress tests — exercise mirror/freeze under extreme TempFileManager limits
# ---------------------------------------------------------------------------


@pytest.fixture
def constrained_temp_file_manager():
    """
    Reinitialize TempFileManager with extreme limits for stress testing.

    Restores the previous limits after the test.
    """
    mgr = TempFileManager.me()
    original_max_bytes = mgr._max_bytes
    original_max_files = mgr._max_files

    # Set extreme limits: only 1 file at a time, 1 byte soft limit
    mgr._max_bytes = 1
    mgr._max_files = 1
    yield mgr
    # Restore
    mgr._max_bytes = original_max_bytes
    mgr._max_files = original_max_files


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_mirror_with_single_temp_file_slot(
    cli_runner, test_folder_url, authenticator, storage_backend, constrained_temp_file_manager
):
    """Mirror completes without deadlock even when limited to a single temp file slot."""
    package = "stress_mirror_" + "".join(random.choices(string.ascii_lowercase, k=6))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=6))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "stress_mirror_src"))
    repo_folder = join_paths(test_folder_url, ".stress_mirror_repo")
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    second_repo_folder = join_paths(test_folder_url, ".stress_mirror_dest")
    run_wrapp(
        cli_runner,
        ["mirror", package, version, "--source-repo", repo_folder, "--destination-repo", second_repo_folder],
    )

    # Verify the mirror produced identical catalogs
    source_catalog: Catalog = await catalog(root_dir=repo_folder)
    dest_catalog: Catalog = await catalog(root_dir=second_repo_folder)
    unique_to_a, unique_to_b, _ = Diff.diff_internal(
        [item.model_dump() for item in source_catalog.items],
        [item.model_dump() for item in dest_catalog.items],
        Diff.not_to_compare_fields,
    )
    assert len(unique_to_a) == 0
    assert len(unique_to_b) == 0


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_freeze_with_single_temp_file_slot(
    cli_runner, test_folder_url, authenticator, storage_backend, constrained_temp_file_manager
):
    """Freeze completes without deadlock even when limited to a single temp file slot."""
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "stress_freeze_src"))
    target_folder = join_paths(test_folder_url, "stress_freeze_target")
    run_wrapp(cli_runner, ["freeze", source_package, target_folder])
