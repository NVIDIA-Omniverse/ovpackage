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
Tests for StorageAPI per-service version negotiation.

Verifies that _negotiate_versions() selects per-service defaults based on
what the server advertises via ListServices, supports explicit env overrides,
and that copy fallback works when on v1beta (no server-side Copy RPC).
"""

import logging
import os
from types import SimpleNamespace
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
)

import pytest

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.router import (
    g_nucleus_enabled,
    g_storage_api_enabled,
)
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    StorageInitializationError,
    StorageMissingDependenciesError,
    StorageOperationError,
)

try:
    import grpc
    from google.protobuf.timestamp_pb2 import Timestamp

    from wrapp.utils.config import WrappConfig
    from wrapp.utils.storage_impls.storageapi import (
        _CAP_PB2,
        _DISABLE_ENUMERATE_ENV_VAR,
        _FF_SVC_PB2,
        _FO_PB2,
        _FO_SVC_PB2,
        _MD_GRPC,
        _MD_PB2,
        _STORAGE_API_VERSION_ENV_VARS,
        NUCLEUS_HASH_METADATA_KEY,
        StorageAPIListEntry,
        StorageAPIResourceIdentity,
        StorageAPIStorageGRPC,
        cap_pb2_v1alpha,
        cap_pb2_v1beta,
        ff_svc_pb2_v1alpha,
        ff_svc_pb2_v1beta,
        fo_pb2_v1alpha,
        fo_pb2_v1beta,
        fo_svc_pb2_v1alpha,
        fo_svc_pb2_v1beta,
        md_grpc_v1alpha,
        md_pb2_v1alpha,
    )
except (ImportError, ModuleNotFoundError, StorageMissingDependenciesError) as e:
    pytest.skip(f"Missing dependencies for StorageAPI: {e}", allow_module_level=True)


@pytest.fixture(autouse=True)
def _deterministic_storageapi_retry_jitter(monkeypatch):
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.random.uniform", lambda _low, high: high)


def _make_list_services_response(services_dict, pb2_module):
    """Build a fake ListServicesResponse.

    services_dict: {"fileobject": ["v1beta", "v1alpha"], "filefolder": ["v1beta"]}
    pb2_module: capabilities_pb2 module (v1beta or v1alpha) to construct entries
    """
    entries = []
    for name, versions in services_dict.items():
        entry = SimpleNamespace(service_name=name, service_versions=versions)
        entries.append(entry)
    return SimpleNamespace(services=entries)


# wrapp_config defaults to an empty WrappConfig() rather than None to prevent
# the constructor from calling load_wrapp_config() and picking up the host's
# ~/.nvidia-omniverse/config/wrapp.toml. Tests that need config behavior pass
# their own wrapp_config explicitly.
def _make_backend(env_overrides=None, wrapp_config=WrappConfig(), **kwargs) -> StorageAPIStorageGRPC:
    # Ensure tests are deterministic even if caller environment has overrides.
    patched_env = {env_var: "" for env_var in _STORAGE_API_VERSION_ENV_VARS.values()}
    if env_overrides:
        patched_env.update(env_overrides)
    with patch.dict(os.environ, patched_env, clear=False):
        return StorageAPIStorageGRPC(
            wrapp_version="test",
            debug_level=False,
            throttling_params=ThrottlingParameters(),
            wrapp_config=wrapp_config,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# _negotiate_versions tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negotiate_uses_service_defaults_when_both_available(caplog):
    """When both versions exist, defaults are chosen per service."""
    backend = _make_backend()
    backend._channel = MagicMock()
    caplog.set_level(logging.DEBUG, logger="omni.wrapp")

    response = _make_list_services_response(
        {
            "fileobject": ["v1beta", "v1alpha", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._fileobject_version == "v1alpha"
    assert backend._filefolder_version == "v1beta"
    assert backend._capabilities_version == "v1beta"
    assert backend._fileobject_msgs is fo_svc_pb2_v1alpha
    assert backend._fileobject_types is fo_pb2_v1alpha
    assert backend._filefolder_msgs is ff_svc_pb2_v1beta
    assert backend._capabilities_msgs is cap_pb2_v1beta
    assert "StorageAPI capabilities ListServices response" in caplog.text
    assert '"service_name": "fileobject"' in caplog.text
    assert '"v1testwrite"' in caplog.text


@pytest.mark.asyncio
async def test_negotiate_falls_back_to_v1alpha_when_v1beta_unavailable():
    """When the server only advertises v1alpha, v1alpha is selected."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1testwrite"],
            "filefolder": ["v1alpha"],
        },
        cap_pb2_v1alpha,
    )

    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            error = grpc.RpcError()
            error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
            raise error
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._fileobject_version == "v1alpha"
    assert backend._filefolder_version == "v1alpha"
    assert backend._capabilities_version == "v1alpha"
    assert backend._fileobject_msgs is fo_svc_pb2_v1alpha
    assert backend._fileobject_types is fo_pb2_v1alpha
    assert backend._filefolder_msgs is ff_svc_pb2_v1alpha
    assert backend._capabilities_msgs is cap_pb2_v1alpha


@pytest.mark.asyncio
async def test_negotiate_mixed_versions():
    """fileobject on v1alpha (for Copy), filefolder on v1beta."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._fileobject_version == "v1alpha"
    assert backend._filefolder_version == "v1beta"
    assert backend._fileobject_msgs is fo_svc_pb2_v1alpha
    assert backend._filefolder_msgs is ff_svc_pb2_v1beta


@pytest.mark.asyncio
async def test_negotiate_honors_fileobject_version_override():
    """Pinned fileobject version is honored when advertised by server."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_FILEOBJECT": "v1beta"})
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1beta", "v1alpha", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._fileobject_version == "v1beta"
    assert backend._fileobject_msgs is fo_svc_pb2_v1beta
    assert backend._fileobject_types is fo_pb2_v1beta


@pytest.mark.asyncio
async def test_negotiate_fails_when_pinned_fileobject_version_unavailable():
    """Pinned fileobject version fails fast when server does not advertise it."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_FILEOBJECT": "v1beta"})
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        with pytest.raises(StorageInitializationError, match="WRAPP_STORAGE_API_FILEOBJECT"):
            await backend._negotiate_versions()


def test_backend_init_fails_on_invalid_version_override():
    """Invalid env override values are rejected during backend construction."""
    with pytest.raises(StorageInitializationError, match="WRAPP_STORAGE_API_FILEOBJECT"):
        _make_backend(env_overrides={"WRAPP_STORAGE_API_FILEOBJECT": "v2gamma"})


@pytest.mark.asyncio
async def test_negotiate_fails_when_pinned_capabilities_unavailable():
    """Pinned capabilities version fails fast instead of silently falling back."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_CAPABILITIES": "v1beta"})
    backend._channel = MagicMock()

    async def fake_run_grpc(throttler, method, request, **kwargs):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
        raise error

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        with pytest.raises(StorageInitializationError, match="WRAPP_STORAGE_API_CAPABILITIES"):
            await backend._negotiate_versions()


@pytest.mark.asyncio
async def test_negotiate_pinned_v1alpha_capabilities_merges_split_service_entries():
    """Pinned v1alpha capabilities should merge duplicate service-name entries."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_CAPABILITIES": "v1alpha"})
    backend._channel = MagicMock()

    response = SimpleNamespace(
        services=[
            SimpleNamespace(service_name="fileobject", service_versions=["v1alpha"]),
            SimpleNamespace(service_name="fileobject", service_versions=["v1testwrite"]),
            SimpleNamespace(service_name="filefolder", service_versions=["v1alpha"]),
            SimpleNamespace(service_name="metadata", service_versions=["v1alpha"]),
        ]
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._capabilities_version == "v1alpha"
    assert backend._fileobject_version == "v1alpha"
    assert backend._filefolder_version == "v1alpha"
    assert backend._metadata_version == "v1alpha"


@pytest.mark.asyncio
async def test_negotiate_fails_when_pinned_metadata_unavailable():
    """Pinned metadata version fails when metadata service is not advertised."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_METADATA": "v1alpha"})
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1beta", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        with pytest.raises(StorageInitializationError, match="WRAPP_STORAGE_API_METADATA"):
            await backend._negotiate_versions()


@pytest.mark.asyncio
async def test_negotiate_skips_metadata_when_disabled_via_env(caplog):
    """WRAPP_STORAGE_API_DISABLE_METADATA skips wiring up the Metadata stub
    even when the server advertises it, so subsequent writes don't trigger
    UpdateMetadata calls."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_DISABLE_METADATA": "True"})
    backend._channel = MagicMock()
    caplog.set_level(logging.INFO, logger="omni.wrapp")

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1beta"],
            "filefolder": ["v1beta", "v1alpha"],
            "metadata": ["v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._metadata_version is None
    assert backend._metadata_msgs is None
    assert backend._metadata_api is None
    assert "WRAPP_STORAGE_API_DISABLE_METADATA" in caplog.text


@pytest.mark.asyncio
async def test_negotiate_disable_metadata_overrides_pinned_version():
    """Disable flag wins over a pinned metadata version: even if the server
    doesn't advertise metadata, no StorageInitializationError is raised."""
    backend = _make_backend(
        env_overrides={
            "WRAPP_STORAGE_API_DISABLE_METADATA": "True",
            "WRAPP_STORAGE_API_METADATA": "v1alpha",
        }
    )
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1beta"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._metadata_api is None


# ---------------------------------------------------------------------------
# Metadata retry policy tests
# ---------------------------------------------------------------------------


def _make_grpc_error(code: grpc.StatusCode, details: str = "") -> grpc.RpcError:
    err = grpc.RpcError()
    err.code = lambda: code
    err.details = lambda: details
    return err


def _wire_metadata_stubs(backend) -> None:
    """Stub out enough of the metadata service for set/get/delete to be exercised."""
    backend._metadata_msgs = MagicMock()
    backend._metadata_msgs.UpdateMetadataRequest = MagicMock(return_value="update-req")
    backend._metadata_msgs.GetMetadataRequest = MagicMock(return_value="get-req")
    backend._metadata_msgs.DeleteMetadataRequest = MagicMock(return_value="delete-req")
    backend._metadata_api = MagicMock()
    backend._metadata_api.UpdateMetadata = AsyncMock()
    backend._metadata_api.GetMetadata = AsyncMock()
    backend._metadata_api.DeleteMetadata = AsyncMock()


@pytest.mark.asyncio
async def test_set_identity_nucleus_hash_retries_on_unavailable(caplog):
    """UNAVAILABLE on the metadata UpdateMetadata path should be retried with
    exponential backoff (1s, 2s, …), matching write_file_content."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)
    # Per-attempt retries log at INFO; only the final-give-up path is WARNING.
    caplog.set_level(logging.INFO, logger="omni.wrapp")

    call_count = 0
    sleeps: list[float] = []
    retry_delay_calls = []

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return SimpleNamespace()  # third attempt succeeds

    async def fake_sleep(d):
        sleeps.append(d)

    def fake_retry_delay(attempt):
        retry_delay_calls.append(attempt)
        return {1: 21.0, 2: 22.0}[attempt]

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", side_effect=fake_sleep),
        patch("wrapp.utils.storage_impls.storageapi._retry_delay", side_effect=fake_retry_delay),
    ):
        await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")

    assert call_count == 3
    assert retry_delay_calls == [1, 2]
    assert sleeps == [21.0, 22.0]
    # No final WARNING when retries eventually succeed
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("Failed to store nucleus hash metadata" in r.message for r in warning_records)
    # Per-attempt retry messages are present at INFO level
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any("attempt 1/3" in r.getMessage() for r in info_records)


@pytest.mark.asyncio
async def test_set_identity_nucleus_hash_warns_after_exhausting_retries(caplog):
    """If every attempt times out the user gets exactly one final warning
    (best-effort semantics preserved)."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)
    caplog.set_level(logging.WARNING, logger="omni.wrapp")

    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")

    assert call_count == 3  # default attempts
    assert "Failed to store nucleus hash metadata for 'identity-token'" in caplog.text


@pytest.mark.asyncio
async def test_set_identity_nucleus_hash_does_not_retry_non_retryable_codes(caplog):
    """Non-retryable codes (e.g. PERMISSION_DENIED) fail on the first attempt."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)
    caplog.set_level(logging.WARNING, logger="omni.wrapp")

    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_grpc_error(grpc.StatusCode.PERMISSION_DENIED, "no access")

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")

    assert call_count == 1
    assert "Failed to store nucleus hash metadata" in caplog.text


@pytest.mark.asyncio
async def test_metadata_retries_env_var_caps_attempts():
    """WRAPP_STORAGE_API_METADATA_RETRIES=1 disables retries entirely."""
    backend = _make_backend(env_overrides={"WRAPP_STORAGE_API_METADATA_RETRIES": "1"})
    _wire_metadata_stubs(backend)

    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")

    assert call_count == 1


@pytest.mark.asyncio
async def test_get_nucleus_hash_retries_on_unavailable():
    """GetMetadata path retries the same retryable codes as set."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)

    call_count = 0
    success_response = SimpleNamespace(user_metadata={})

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return success_response

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        result = await backend._get_nucleus_hash("https://example/blob")

    assert call_count == 2
    assert result is None  # empty user_metadata → no hash, but no error


@pytest.mark.asyncio
async def test_get_nucleus_hash_not_found_does_not_retry():
    """NOT_FOUND/UNIMPLEMENTED should short-circuit to None without retrying."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)

    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_grpc_error(grpc.StatusCode.NOT_FOUND, "no such resource")

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        result = await backend._get_nucleus_hash("https://example/blob")

    assert call_count == 1
    assert result is None


@pytest.mark.asyncio
async def test_delete_identity_nucleus_hash_uses_opaque_identity():
    """Identity metadata uses the opaque identity byte-for-byte, without URL encoding."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)

    captured = {}

    async def fake_run_grpc(throttler, method, request, **kwargs):
        captured["method"] = method
        captured["request"] = request
        return SimpleNamespace()

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._delete_nucleus_hash_for_identity("opaque identity token")

    assert captured["method"] is backend._metadata_api.DeleteMetadata
    assert captured["request"] == "delete-req"
    backend._metadata_msgs.DeleteMetadataRequest.assert_called_once_with(
        uri="opaque identity token",
        user_metadata_key=NUCLEUS_HASH_METADATA_KEY,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNIMPLEMENTED])
async def test_delete_nucleus_hash_tolerates_missing_key(caplog, code):
    """NOT_FOUND / UNIMPLEMENTED mean 'nothing to delete' and are silently accepted."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)
    caplog.set_level(logging.WARNING, logger="omni.wrapp")

    async def fake_run_grpc(throttler, method, request, **kwargs):
        raise _make_grpc_error(code, "no such key")

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._delete_nucleus_hash_for_identity("identity-token")

    assert "Failed to delete nucleus hash metadata" not in caplog.text


@pytest.mark.asyncio
async def test_delete_nucleus_hash_warns_on_other_errors(caplog):
    """Any other failure is best-effort: one warning, no exception."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)
    caplog.set_level(logging.WARNING, logger="omni.wrapp")

    async def fake_run_grpc(throttler, method, request, **kwargs):
        raise _make_grpc_error(grpc.StatusCode.PERMISSION_DENIED, "no access")

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._delete_nucleus_hash_for_identity("identity-token")

    assert "Failed to delete nucleus hash metadata for 'identity-token'" in caplog.text


@pytest.mark.asyncio
async def test_set_nucleus_hash_skips_when_metadata_disabled():
    """When the metadata stub is None (disabled or not advertised), no calls are issued."""
    backend = _make_backend()
    backend._metadata_api = None
    backend._metadata_msgs = None

    with patch.object(backend, "_run_grpc", side_effect=AssertionError("must not be called")):
        await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")
        assert await backend._get_nucleus_hash("https://example/blob") is None
        await backend._delete_nucleus_hash_for_identity("identity-token")


@pytest.mark.asyncio
async def test_metadata_calls_and_retries_are_counted_in_stats():
    """Successful metadata calls bump SET/GET_METADATA_ASYNC; retries bump
    GRPC_RETRY so users can see retry pressure separately from logical ops."""
    backend = _make_backend()
    _wire_metadata_stubs(backend)
    Stats.get().clear()

    set_calls = 0

    async def fake_set_run_grpc(throttler, method, request, **kwargs):
        nonlocal set_calls
        set_calls += 1
        if set_calls < 3:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return SimpleNamespace()

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_set_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")

    assert Stats.get().get_stat(StatType.SET_METADATA_ASYNC) == 1
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 2  # 2 sleeps, then success on 3rd

    # GET on a different code path also increments GET_METADATA_ASYNC and GRPC_RETRY independently.
    Stats.get().clear()
    get_calls = 0

    async def fake_get_run_grpc(throttler, method, request, **kwargs):
        nonlocal get_calls
        get_calls += 1
        if get_calls < 2:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return SimpleNamespace(user_metadata={})

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_get_run_grpc),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", new=AsyncMock()),
    ):
        await backend._get_nucleus_hash("https://example/blob")

    assert Stats.get().get_stat(StatType.GET_METADATA_ASYNC) == 1
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1
    assert Stats.get().get_stat(StatType.SET_METADATA_ASYNC) == 0

    # DELETE is counted independently as well.
    Stats.get().clear()

    async def fake_delete_run_grpc(throttler, method, request, **kwargs):
        return SimpleNamespace()

    with patch.object(backend, "_run_grpc", side_effect=fake_delete_run_grpc):
        await backend._delete_nucleus_hash_for_identity("identity-token")

    assert Stats.get().get_stat(StatType.DELETE_METADATA_ASYNC) == 1
    assert Stats.get().get_stat(StatType.SET_METADATA_ASYNC) == 0
    assert Stats.get().get_stat(StatType.GET_METADATA_ASYNC) == 0


@pytest.mark.asyncio
async def test_disabled_metadata_does_not_count_in_stats():
    """When the metadata stub is None, no SET/GET_METADATA_ASYNC stats accrue."""
    backend = _make_backend()
    backend._metadata_api = None
    backend._metadata_msgs = None
    Stats.get().clear()

    await backend._set_nucleus_hash_for_identity("identity-token", "deadbeef")
    assert await backend._get_nucleus_hash("https://example/blob") is None

    assert Stats.get().get_stat(StatType.SET_METADATA_ASYNC) == 0
    assert Stats.get().get_stat(StatType.GET_METADATA_ASYNC) == 0
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 0


def test_metadata_stats_contribute_to_total_storage_calls():
    """SET/GET_METADATA_ASYNC should be in g_CLIENTLIB_STATS so they fold
    into 'total calls to storage provider'. GRPC_RETRY stays separate so
    users can see retry counts standalone."""
    from wrapp.utils.stats import g_CLIENTLIB_STATS

    assert StatType.SET_METADATA_ASYNC in g_CLIENTLIB_STATS
    assert StatType.GET_METADATA_ASYNC in g_CLIENTLIB_STATS
    assert StatType.GRPC_RETRY not in g_CLIENTLIB_STATS


@pytest.mark.asyncio
async def test_negotiate_fails_when_service_missing():
    """Raises StorageInitializationError when a required service is not advertised."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {"fileobject": ["v1beta", "v1testwrite"]},
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        with pytest.raises(StorageInitializationError, match="filefolder"):
            await backend._negotiate_versions()


@pytest.mark.asyncio
async def test_negotiate_fails_when_no_known_version():
    """Raises StorageInitializationError when a service has no known version."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v2gamma"],
            "filefolder": ["v1beta"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        with pytest.raises(StorageInitializationError, match="fileobject"):
            await backend._negotiate_versions()


@pytest.mark.asyncio
async def test_negotiate_capabilities_fallback_from_v1beta_to_v1alpha():
    """When v1beta CapabilitiesService is UNIMPLEMENTED, fall back to v1alpha."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1alpha", "v1testwrite"],
            "filefolder": ["v1alpha"],
        },
        cap_pb2_v1alpha,
    )

    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            error = grpc.RpcError()
            error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
            raise error
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._capabilities_version == "v1alpha"
    assert call_count == 2


# ---------------------------------------------------------------------------
# Copy fallback tests
# ---------------------------------------------------------------------------


async def _passthrough(x):
    return x


@pytest.mark.asyncio
async def test_copy_uses_server_side_when_v1alpha():
    """On v1alpha fileobject, copy() attempts server-side Copy RPC."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1alpha"
    backend._fileobject_msgs = fo_svc_pb2_v1alpha
    backend._fileobject_types = fo_pb2_v1alpha
    backend._fileobject_api = MagicMock()

    server_copy_called = False

    async def fake_server_copy(src, dst):
        nonlocal server_copy_called
        server_copy_called = True
        return Result.OK

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "stat", new_callable=AsyncMock, return_value=(Result.ERROR_NOT_FOUND, None)),
        patch.object(backend, "_server_side_copy", side_effect=fake_server_copy),
    ):
        result = await backend.copy_address("src://a", "dst://b")
        assert result == Result.OK
        assert server_copy_called


@pytest.mark.asyncio
async def test_copy_uses_supplied_source_identity_without_restatting_address():
    """A catalog copy must use its recorded identity, not resolve the address again.

    The Storage API Copy RPC requires a source identity.  When WRAPP already
    has that identity, statting the mutable source address can select different
    bytes and must not occur.
    """
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1alpha"
    backend._fileobject_msgs = fo_svc_pb2_v1alpha
    backend._fileobject_types = fo_pb2_v1alpha
    backend._fileobject_api = MagicMock()
    copy_requests = []

    async def fake_run_grpc(throttler, method, request, **kwargs):
        if method is backend._fileobject_api.Stat:
            pytest.fail("A known catalog resource identity must not be replaced by stat(address)")
        if method is backend._fileobject_api.Copy:
            copy_requests.append(request)
            return SimpleNamespace(resource_identity=SimpleNamespace(encoded_identity="destination-identity"))
        raise AssertionError(f"Unexpected RPC method: {method!r}")

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "stat", new_callable=AsyncMock, return_value=(Result.ERROR_NOT_FOUND, None)),
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch.object(backend, "_get_nucleus_hash_for_identity", new_callable=AsyncMock, return_value=None),
    ):
        result = await backend.copy_resource_identity(
            StorageAPIResourceIdentity("catalog-identity").to_string(),
            "https://content.example/destination.usd",
        )

    assert result == Result.OK
    assert len(copy_requests) == 1
    assert copy_requests[0].source_resource_identity.encoded_identity == "catalog-identity"


@pytest.mark.asyncio
async def test_copy_raises_not_implemented_when_v1beta():
    """On v1beta fileobject, copy() raises NotImplementedError (no server-side Copy RPC)."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
    ):
        with pytest.raises(NotImplementedError):
            await backend.copy_address("src://a", "dst://b")


@pytest.mark.asyncio
async def test_copy_raises_not_implemented_on_unimplemented_even_on_v1alpha():
    """On v1alpha, if server-side Copy returns UNIMPLEMENTED, raise NotImplementedError."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1alpha"
    backend._fileobject_msgs = fo_svc_pb2_v1alpha
    backend._fileobject_types = fo_pb2_v1alpha
    backend._fileobject_api = MagicMock()

    async def fake_server_copy(src, dst):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
        raise error

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "stat", new_callable=AsyncMock, return_value=(Result.ERROR_NOT_FOUND, None)),
        patch.object(backend, "_server_side_copy", side_effect=fake_server_copy),
    ):
        with pytest.raises(NotImplementedError):
            await backend.copy_address("src://a", "dst://b")


# ---------------------------------------------------------------------------
# Metadata negotiation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negotiate_metadata_available():
    """When the server advertises metadata v1alpha, a MetadataServiceStub is created."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1beta", "v1alpha", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
            "metadata": ["v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._metadata_version == "v1alpha"
    assert backend._metadata_msgs is md_pb2_v1alpha
    assert backend._metadata_api is not None


@pytest.mark.asyncio
async def test_negotiate_metadata_unavailable():
    """When the server does not advertise metadata, negotiation succeeds with _metadata_api=None."""
    backend = _make_backend()
    backend._channel = MagicMock()

    response = _make_list_services_response(
        {
            "fileobject": ["v1beta", "v1alpha", "v1testwrite"],
            "filefolder": ["v1beta", "v1alpha"],
        },
        cap_pb2_v1beta,
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._negotiate_versions()

    assert backend._metadata_version is None
    assert backend._metadata_msgs is None
    assert backend._metadata_api is None
    assert backend._fileobject_version == "v1alpha"
    assert backend._filefolder_version == "v1beta"


# ---------------------------------------------------------------------------
# Metadata hash get/set tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_nucleus_hash_returns_hash():
    """_get_nucleus_hash returns the stored hash when the Metadata API has it."""
    from google.protobuf.struct_pb2 import Value

    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    fake_response = SimpleNamespace(
        user_metadata={
            NUCLEUS_HASH_METADATA_KEY: SimpleNamespace(
                value=Value(string_value="sha-256-flat;1048576;abc123"),
                etag="etag1",
            )
        }
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return fake_response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        result = await backend._get_nucleus_hash("https://example.com/file.usd")

    assert result == "sha-256-flat;1048576;abc123"


@pytest.mark.asyncio
async def test_get_nucleus_hash_for_identity_uses_identity_as_metadata_uri():
    """Identity metadata is queried using the exact Storage API read token."""
    from google.protobuf.struct_pb2 import Value

    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    fake_response = SimpleNamespace(
        user_metadata={
            NUCLEUS_HASH_METADATA_KEY: SimpleNamespace(
                value=Value(string_value="sha-256-flat;1048576;identity"),
                etag="etag1",
            )
        }
    )
    requests = []

    async def fake_run_grpc(throttler, method, request, **kwargs):
        requests.append(request)
        return fake_response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        result = await backend._get_nucleus_hash_for_identity("opaque-read-token")

    assert result == "sha-256-flat;1048576;identity"
    assert len(requests) == 1
    assert requests[0].uri == "opaque-read-token"


@pytest.mark.asyncio
async def test_get_nucleus_hash_returns_none_when_key_missing():
    """_get_nucleus_hash returns None when the metadata key does not exist."""
    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    fake_response = SimpleNamespace(user_metadata={})

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return fake_response

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        result = await backend._get_nucleus_hash("https://example.com/file.usd")

    assert result is None


@pytest.mark.asyncio
async def test_get_nucleus_hash_returns_none_when_not_found():
    """_get_nucleus_hash returns None on NOT_FOUND gRPC error."""
    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    async def fake_run_grpc(throttler, method, request, **kwargs):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.NOT_FOUND
        raise error

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        result = await backend._get_nucleus_hash("https://example.com/file.usd")

    assert result is None


@pytest.mark.asyncio
async def test_get_nucleus_hash_returns_none_when_no_metadata_api():
    """_get_nucleus_hash returns None when the Metadata API is not available."""
    backend = _make_backend()
    assert backend._metadata_api is None

    result = await backend._get_nucleus_hash("https://example.com/file.usd")
    assert result is None


@pytest.mark.asyncio
async def test_set_identity_nucleus_hash_calls_update_metadata():
    """Identity hashes are stored against the opaque identity token."""
    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    calls = []

    async def fake_run_grpc(throttler, method, request, **kwargs):
        calls.append(request)
        return SimpleNamespace(etag="etag1")

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._set_nucleus_hash_for_identity("opaque-identity", "sha-256-flat;1048576;abc123")

    assert len(calls) == 1
    req = calls[0]
    assert req.uri == "opaque-identity"
    assert req.user_metadata_key == NUCLEUS_HASH_METADATA_KEY
    assert req.user_metadata.string_value == "sha-256-flat;1048576;abc123"


@pytest.mark.asyncio
async def test_set_identity_nucleus_hash_noop_when_no_metadata_api():
    """_set_nucleus_hash_for_identity is a no-op when the Metadata API is not available."""
    backend = _make_backend()
    assert backend._metadata_api is None

    # Should not raise
    await backend._set_nucleus_hash_for_identity("opaque-identity", "sha-256-flat;1048576;abc123")


@pytest.mark.asyncio
async def test_set_identity_nucleus_hash_logs_on_failure():
    """_set_nucleus_hash_for_identity logs a warning but does not raise on gRPC errors."""
    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    async def fake_run_grpc(throttler, method, request, **kwargs):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.INTERNAL
        raise error

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        # Should not raise
        await backend._set_nucleus_hash_for_identity("opaque-identity", "sha-256-flat;1048576;abc123")


# ---------------------------------------------------------------------------
# stat() with metadata hash tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stat_returns_hash_when_metadata_available():
    """stat() populates nucleus_hash on the ListEntry when the Metadata API has a hash."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()
    backend._metadata_api = MagicMock()
    backend._metadata_msgs = md_pb2_v1alpha

    fake_stat_response = SimpleNamespace(
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity="rid1"),
            metadata=SimpleNamespace(data_object_size=100, last_modified_timestamp=None),
        )
    )

    expected_hash = "sha-256-flat;1048576;abc123"

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return fake_stat_response

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch.object(backend, "_get_nucleus_hash_for_identity", new_callable=AsyncMock, return_value=expected_hash) as get_hash,
    ):
        from wrapp.utils.storage_interface import Result

        result, entry = await backend.stat("https://example.com/file.usd")

    assert result == Result.OK
    assert entry is not None
    assert entry.nucleus_hash == expected_hash
    get_hash.assert_awaited_once_with("rid1")


@pytest.mark.asyncio
async def test_stat_returns_no_hash_when_metadata_unavailable():
    """stat() leaves nucleus_hash as None when the Metadata API is not available."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()

    fake_stat_response = SimpleNamespace(
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity="rid1"),
            metadata=SimpleNamespace(data_object_size=100, last_modified_timestamp=None),
        )
    )

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return fake_stat_response

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
    ):
        from wrapp.utils.storage_interface import Result

        result, entry = await backend.stat("https://example.com/file.usd")

    assert result == Result.OK
    assert entry is not None
    assert entry.nucleus_hash is None


@pytest.mark.asyncio
async def test_stat_retries_on_unavailable():
    """Transient upstream request timeouts from Stat should not fail catalog."""
    Stats.get().clear()
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()

    fake_stat_response = SimpleNamespace(
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity="rid1"),
            metadata=SimpleNamespace(data_object_size=100, last_modified_timestamp=None),
        )
    )

    call_count = 0
    sleeps: list[float] = []
    retry_delay_calls = []

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return fake_stat_response

    async def fake_sleep(delay):
        sleeps.append(delay)

    def fake_retry_delay(attempt):
        retry_delay_calls.append(attempt)
        return {1: 23.0, 2: 24.0}[attempt]

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch.object(backend, "_get_nucleus_hash_for_identity", new_callable=AsyncMock, return_value=None),
        patch("wrapp.utils.storage_impls.storageapi.asyncio.sleep", side_effect=fake_sleep),
        patch("wrapp.utils.storage_impls.storageapi._retry_delay", side_effect=fake_retry_delay),
    ):
        from wrapp.utils.storage_interface import Result

        result, entry = await backend.stat("https://example.com/file.usd")

    assert result == Result.OK
    assert entry is not None
    assert entry.version == "rid1"
    assert call_count == 3
    assert retry_delay_calls == [1, 2]
    assert sleeps == [23.0, 24.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 2


@pytest.mark.asyncio
async def test_server_side_address_copy_rejects_stat_without_resource_identity():
    backend = _make_backend()
    backend._fileobject_version = "v1alpha"
    backend._fileobject_msgs = fo_svc_pb2_v1alpha
    backend._fileobject_types = fo_pb2_v1alpha
    backend._fileobject_api = MagicMock()
    backend._fileobject_api.Stat = object()
    backend._fileobject_api.Copy = object()
    stat_response = SimpleNamespace(
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity=""),
            metadata=SimpleNamespace(data_object_size=100, last_modified_timestamp=None),
        )
    )
    backend._run_grpc = AsyncMock(return_value=stat_response)

    with pytest.raises(StorageOperationError, match="Stat returned no resource identity"):
        await backend._server_side_copy("https://example.com/source", "https://example.com/destination")

    backend._run_grpc.assert_awaited_once()


# ---------------------------------------------------------------------------
# _compile_route_pattern / wildcard matching tests
# ---------------------------------------------------------------------------


class TestRoutePatternMatching:
    """Tests for the custom wildcard pattern matching used by ListRoutes."""

    @staticmethod
    def _matches(pattern: str, url: str) -> bool:
        rx = StorageAPIStorageGRPC._compile_route_pattern(pattern)
        return StorageAPIStorageGRPC._url_matches_route(rx, url)

    def test_double_star_matches_any_substring(self):
        assert self._matches("https://example.com/**", "https://example.com/foo/bar/baz")

    def test_double_star_matches_root(self):
        assert self._matches("https://example.com/**", "https://example.com/")

    def test_double_star_matches_without_trailing_slash(self):
        assert self._matches("https://example.com/**", "https://example.com")

    def test_trailing_double_star_requires_separator_boundary_OMPE_104629(self):
        pattern = "https://prod.example.com/**"

        assert self._matches(pattern, "https://prod.example.com")
        assert self._matches(pattern, "https://prod.example.com/")
        assert self._matches(pattern, "https://prod.example.com/a/b")
        assert not self._matches(pattern, "https://prod.example.com.attacker.io/creds")
        assert not self._matches(pattern, "https://prod.example.commercial.com/x")
        assert not self._matches(pattern, "https://prod.example.comEVIL")
        assert not self._matches("https://host.com/teamA/**", "https://host.com/teamApex/secret")

    def test_single_star_matches_identifier(self):
        assert self._matches("https://*.example.com/**", "https://mybucket.example.com/path/file")

    def test_single_star_rejects_slash(self):
        assert not self._matches("https://*.example.com/**", "https://a/b.example.com/path")

    def test_single_star_rejects_empty(self):
        assert not self._matches("https://*.example.com/**", "https://.example.com/path")

    def test_combined_wildcards(self):
        pattern = "https://*.s3.*.amazonaws.com/**"
        assert self._matches(pattern, "https://mybucket.s3.us-east-1.amazonaws.com/key/object.usd")
        assert self._matches(pattern, "https://other-bucket.s3.eu-west-1.amazonaws.com/a/b/c")

    def test_combined_wildcards_reject_mismatch(self):
        pattern = "https://*.s3.*.amazonaws.com/**"
        assert not self._matches(pattern, "https://mybucket.s4.us-east-1.amazonaws.com/key")
        assert not self._matches(pattern, "https://mybucket.s3.us-east-1.azure.com/key")

    def test_exact_match_no_wildcards(self):
        assert self._matches("https://example.com/data", "https://example.com/data")
        assert not self._matches("https://example.com/data", "https://example.com/other")

    def test_pattern_with_dots_in_domain(self):
        pattern = "https://*.blob.core.windows.net/**"
        assert self._matches(pattern, "https://myaccount.blob.core.windows.net/container/blob")
        assert not self._matches(pattern, "https://myaccount.blob.core.windows.org/container/blob")

    def test_single_star_allows_hyphens_and_underscores(self):
        assert self._matches("https://*.example.com/**", "https://my-bucket_v2.example.com/path")

    def test_single_star_rejects_dots_OMPE_104654(self):
        assert not self._matches("https://*.example.com/**", "https://my-bucket_v2.0.example.com/path")


# ---------------------------------------------------------------------------
# _fetch_routes tests
# ---------------------------------------------------------------------------


def _make_list_routes_response(patterns):
    """Build a fake ListRoutesResponse with wildcard patterns."""
    items = [SimpleNamespace(wildcard_pattern=p) for p in patterns]
    return SimpleNamespace(items=items)


def _make_top_level_addresses_response(addresses):
    """Build a fake ListTopLevelAddressesResponse."""
    items = [SimpleNamespace(top_level_address=a) for a in addresses]
    return SimpleNamespace(items=items)


@pytest.mark.asyncio
async def test_fetch_routes_success_populates_routes():
    """When ListRoutes succeeds, _routes and _route_regexes are populated."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_version = "v1alpha"
    backend._capabilities_msgs = cap_pb2_v1alpha
    backend._capability_api = MagicMock()

    routes_response = _make_list_routes_response(["https://*.s3.*.amazonaws.com/**", "https://example.com/**"])

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return routes_response

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.router.g_list_routes_enabled", True),
    ):
        result = await backend._fetch_routes()

    assert result is True
    assert backend._routes == ["https://*.s3.*.amazonaws.com/**", "https://example.com/**"]
    assert backend._route_regexes is not None
    assert len(backend._route_regexes) == 2


@pytest.mark.asyncio
async def test_fetch_routes_retries_retryable_failure(monkeypatch):
    """Transient ListRoutes failures are retried before falling back to TLAs."""
    Stats.get().clear()
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_version = "v1alpha"
    backend._capabilities_msgs = cap_pb2_v1alpha
    backend._capability_api = MagicMock()
    routes_response = _make_list_routes_response(["https://example.com/**"])
    call_count = 0
    sleep_calls = []
    retry_delay_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    def fake_retry_delay(attempt):
        retry_delay_calls.append(attempt)
        return 25.0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return routes_response

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi._retry_delay", fake_retry_delay)
    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.router.g_list_routes_enabled", True),
    ):
        result = await backend._fetch_routes()

    assert result is True
    assert backend._routes == ["https://example.com/**"]
    assert call_count == 2
    assert retry_delay_calls == [1]
    assert sleep_calls == [25.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.asyncio
async def test_fetch_routes_falls_back_on_unimplemented():
    """When the server returns UNIMPLEMENTED, _fetch_routes returns False."""
    Stats.get().clear()
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_version = "v1alpha"
    backend._capabilities_msgs = cap_pb2_v1alpha
    backend._capability_api = MagicMock()
    call_count = 0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _make_grpc_error(grpc.StatusCode.UNIMPLEMENTED, "not implemented")

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.router.g_list_routes_enabled", True),
    ):
        result = await backend._fetch_routes()

    assert result is False
    assert backend._routes is None
    assert backend._route_regexes is None
    assert call_count == 1
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 0


@pytest.mark.asyncio
async def test_fetch_routes_skipped_when_disabled():
    """When WRAPP_DISABLE_LIST_ROUTES is set, _fetch_routes returns False immediately."""
    backend = _make_backend()
    backend._capabilities_version = "v1alpha"

    with patch("wrapp.utils.router.g_list_routes_enabled", False):
        result = await backend._fetch_routes()

    assert result is False
    assert backend._routes is None


@pytest.mark.asyncio
async def test_fetch_routes_uses_v1alpha_method_when_v1beta_was_negotiated():
    """ListRoutes is probed through v1alpha even when other capabilities calls use v1beta."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_version = "v1beta"
    route_api = MagicMock()
    routes_response = _make_list_routes_response(["storage-id://service/**"])

    with (
        patch("wrapp.utils.storage_impls.storageapi.cap_grpc_v1alpha.CapabilitiesServiceStub", return_value=route_api),
        patch.object(backend, "_run_control_grpc_with_retries", new=AsyncMock(return_value=routes_response)) as run_routes,
        patch("wrapp.utils.router.g_list_routes_enabled", True),
    ):
        result = await backend._fetch_routes()

    assert result is True
    assert backend._routes == ["storage-id://service/**"]
    run_routes.assert_awaited_once()
    assert run_routes.await_args.args[1] is route_api.ListRoutes


@pytest.mark.asyncio
async def test_fetch_routes_empty_response_falls_back():
    """When ListRoutes returns no patterns, _fetch_routes returns False."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_version = "v1alpha"
    backend._capabilities_msgs = cap_pb2_v1alpha
    backend._capability_api = MagicMock()

    async def fake_run_grpc(throttler, method, request, **kwargs):
        return _make_list_routes_response([])

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.router.g_list_routes_enabled", True),
    ):
        result = await backend._fetch_routes()

    assert result is False
    assert backend._routes is None


# ---------------------------------------------------------------------------
# is_url_in_scope with routes
# ---------------------------------------------------------------------------


class TestIsUrlInScopeWithRoutes:
    """Tests for is_url_in_scope when route patterns are active."""

    def test_non_trailing_double_star_requires_leading_separator_OMPE_104629(self):
        backend = _make_backend()
        backend._route_regexes = [StorageAPIStorageGRPC._compile_route_pattern("foo/**/bar")]

        assert backend.is_url_in_scope("foo/a/b/bar")
        assert not backend.is_url_in_scope("fooX/bar")

    def test_uses_routes_when_available(self):
        backend = _make_backend()
        patterns = ["https://*.s3.*.amazonaws.com/**"]
        backend._routes = patterns
        backend._route_regexes = [StorageAPIStorageGRPC._compile_route_pattern(p) for p in patterns]

        assert backend.is_url_in_scope("https://mybucket.s3.us-east-1.amazonaws.com/key/file.usd")
        assert not backend.is_url_in_scope("https://mybucket.blob.core.windows.net/container/file.usd")

    def test_falls_back_to_tla_when_no_routes(self):
        backend = _make_backend()
        backend._routes = None
        backend._route_regexes = None
        backend._top_level_addresses = ["https://example.com/storage"]

        assert backend.is_url_in_scope("https://example.com/storage/file.usd")
        assert not backend.is_url_in_scope("https://other.com/file.usd")

    def test_permissive_when_not_yet_connected(self):
        backend = _make_backend()
        backend._endpoint = "localhost:50051"
        backend._discovery_url = ""
        backend._routes = None
        backend._route_regexes = None
        backend._top_level_addresses = None
        # _channel is None → not yet connected → permissive (an endpoint is configured)
        assert backend.is_url_in_scope("https://anything.com/anywhere")

    def test_inert_when_neither_endpoint_nor_discovery_url_configured(self):
        # Backend is enabled by default, but if nothing's configured it must not
        # claim URLs from other backends (see router default-enabled flip).
        backend = _make_backend()
        backend._endpoint = ""
        backend._discovery_url = ""
        backend._routes = None
        backend._route_regexes = None
        backend._top_level_addresses = None
        assert not backend.is_url_in_scope("https://anything.com/anywhere")

    def test_pessimistic_when_connected_but_nothing_populated(self):
        backend = _make_backend()
        backend._endpoint = "localhost:50051"
        backend._discovery_url = ""
        backend._channel = MagicMock()
        backend._routes = None
        backend._route_regexes = None
        backend._top_level_addresses = None

        assert not backend.is_url_in_scope("https://anything.com/anywhere")

    def test_routes_take_priority_over_tla(self):
        backend = _make_backend()
        patterns = ["https://specific.example.com/**"]
        backend._routes = patterns
        backend._route_regexes = [StorageAPIStorageGRPC._compile_route_pattern(p) for p in patterns]
        backend._top_level_addresses = ["https://broad.example.com"]

        assert backend.is_url_in_scope("https://specific.example.com/file.usd")
        assert not backend.is_url_in_scope("https://broad.example.com/file.usd")


# ---------------------------------------------------------------------------
# _ensure_grpc_stubs integration: routes vs TLA
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_grpc_stubs_skips_tla_when_routes_succeed():
    """When _fetch_routes returns True, _fetch_top_level_addresses is not called."""
    backend = _make_backend()
    backend._channel = None
    backend._discovery_url = ""
    backend._endpoint = "localhost:50051"

    tla_called = False

    async def fake_fetch_tla():
        nonlocal tla_called
        tla_called = True

    with (
        patch.object(backend, "_create_grpc_channel", return_value=MagicMock()),
        patch.object(backend, "_negotiate_versions", new_callable=AsyncMock),
        patch.object(backend, "_fetch_routes", new_callable=AsyncMock, return_value=True),
        patch.object(backend, "_fetch_top_level_addresses", side_effect=fake_fetch_tla),
    ):
        await backend._ensure_grpc_stubs()

    assert not tla_called


@pytest.mark.asyncio
async def test_ensure_grpc_stubs_calls_tla_when_routes_fail():
    """When _fetch_routes returns False, _fetch_top_level_addresses is called."""
    backend = _make_backend()
    backend._channel = None
    backend._discovery_url = ""
    backend._endpoint = "localhost:50051"

    tla_called = False

    async def fake_fetch_tla():
        nonlocal tla_called
        tla_called = True

    with (
        patch.object(backend, "_create_grpc_channel", return_value=MagicMock()),
        patch.object(backend, "_negotiate_versions", new_callable=AsyncMock),
        patch.object(backend, "_fetch_routes", new_callable=AsyncMock, return_value=False),
        patch.object(backend, "_fetch_top_level_addresses", side_effect=fake_fetch_tla),
    ):
        await backend._ensure_grpc_stubs()

    assert tla_called


@pytest.mark.asyncio
async def test_ensure_grpc_stubs_applies_config_route_override_from_toml(tmp_path):
    """End-to-end: a real wrapp.toml on disk is loaded and the route override
    is installed -- both at __init__ time (so `has_known_scope()` returns True
    before any connection) and reasserted by `_ensure_grpc_stubs` after
    discovery (in case `_fetch_routes` overwrote `_routes` with what the
    server advertised)."""
    toml_file = tmp_path / "wrapp.toml"
    toml_file.write_text(
        "[storage_api.default]\n" 'discovery_url = "https://discovery.example.com"\n' 'routes = ["https://override.example.com/**"]\n'
    )

    backend = _make_backend(
        env_overrides={"WRAPP_CONFIG_FILE": str(toml_file)},
        wrapp_config=None,
    )
    backend._channel = None
    backend._discovery_url = ""
    backend._endpoint = "localhost:50051"

    # The override is installed at construction time so the Router can claim
    # the URLs it covers before any gRPC connection has happened.
    assert backend.has_known_scope() is True
    assert backend._routes == ["https://override.example.com/**"]
    assert backend.is_url_in_scope("https://override.example.com/file.usd") is True
    assert backend.is_url_in_scope("https://other.example.com/file.usd") is False

    # Simulate the server advertising a different route list; the override
    # must be reasserted on top of it.
    async def overwrite_routes_with_server_advertised():
        backend._routes = ["https://server-advertised.example.com/**"]
        backend._route_regexes = [backend._compile_route_pattern(p) for p in backend._routes]
        return True

    with (
        patch.object(backend, "_create_grpc_channel", return_value=MagicMock()),
        patch.object(backend, "_negotiate_versions", new_callable=AsyncMock),
        patch.object(backend, "_fetch_routes", side_effect=overwrite_routes_with_server_advertised),
        patch.object(backend, "_fetch_top_level_addresses", new_callable=AsyncMock),
    ):
        await backend._ensure_grpc_stubs()

    assert backend.has_known_scope() is True
    assert backend._routes == ["https://override.example.com/**"]
    assert backend.is_url_in_scope("https://override.example.com/file.usd") is True
    assert backend.is_url_in_scope("https://other.example.com/file.usd") is False
    assert backend.is_url_in_scope("https://server-advertised.example.com/file.usd") is False


@pytest.mark.asyncio
async def test_get_top_level_addresses_fetches_tla_when_missing():
    """get_top_level_addresses() lazily fetches TLAs when routes-only init skipped them."""
    backend = _make_backend()
    backend._top_level_addresses = None

    async def fake_fetch_tla():
        backend._top_level_addresses = ["https://example.com/storage"]

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_fetch_top_level_addresses", side_effect=fake_fetch_tla) as fetch_tla_mock,
    ):
        result = await backend.get_top_level_addresses()

    fetch_tla_mock.assert_awaited_once()
    assert result == ["https://example.com/storage"]


@pytest.mark.asyncio
async def test_fetch_top_level_addresses_retries_retryable_failure(monkeypatch):
    """Transient ListTopLevelAddresses failures are retried before catch-all fallback."""
    Stats.get().clear()
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_msgs = cap_pb2_v1alpha
    backend._capability_api = MagicMock()
    tla_response = _make_top_level_addresses_response(["https://example.com/storage/"])
    call_count = 0
    sleep_calls = []
    retry_delay_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    def fake_retry_delay(attempt):
        retry_delay_calls.append(attempt)
        return 26.0

    async def fake_run_grpc(throttler, method, request, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _make_grpc_error(grpc.StatusCode.UNAVAILABLE, "upstream request timeout")
        return tla_response

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi._retry_delay", fake_retry_delay)
    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._fetch_top_level_addresses()

    assert backend._top_level_addresses == ["https://example.com/storage"]
    assert call_count == 2
    assert retry_delay_calls == [1]
    assert sleep_calls == [26.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.asyncio
async def test_ensure_grpc_stubs_raises_storage_op_error_on_negotiate_grpc_failure():
    """When _negotiate_versions() raises grpc.RpcError, channel is closed and StorageOperationError is raised."""
    backend = _make_backend()
    backend._channel = None
    backend._discovery_url = ""
    backend._endpoint = "localhost:50051"

    mock_channel = AsyncMock()

    rpc_error = grpc.RpcError()
    rpc_error.details = lambda: "Connection refused"
    rpc_error.code = lambda: grpc.StatusCode.UNAVAILABLE

    async def failing_negotiate():
        raise rpc_error

    with (
        patch.object(backend, "_create_grpc_channel", return_value=mock_channel),
        patch.object(backend, "_negotiate_versions", side_effect=failing_negotiate),
    ):
        with pytest.raises(StorageOperationError, match="Failed to connect to Storage API gRPC endpoint"):
            await backend._ensure_grpc_stubs()

    mock_channel.close.assert_awaited_once()
    assert backend._channel is None


@pytest.mark.asyncio
async def test_ensure_grpc_stubs_raises_storage_op_error_on_fetch_routes_grpc_failure():
    """When _fetch_routes() raises grpc.RpcError, channel is closed and StorageOperationError is raised."""
    backend = _make_backend()
    backend._channel = None
    backend._discovery_url = ""
    backend._endpoint = "localhost:50051"

    mock_channel = AsyncMock()

    rpc_error = grpc.RpcError()
    rpc_error.details = lambda: "Server unavailable"
    rpc_error.code = lambda: grpc.StatusCode.UNAVAILABLE

    async def failing_fetch_routes():
        raise rpc_error

    with (
        patch.object(backend, "_create_grpc_channel", return_value=mock_channel),
        patch.object(backend, "_negotiate_versions", new_callable=AsyncMock),
        patch.object(backend, "_fetch_routes", side_effect=failing_fetch_routes),
    ):
        with pytest.raises(StorageOperationError, match="Failed to connect to Storage API gRPC endpoint"):
            await backend._ensure_grpc_stubs()

    mock_channel.close.assert_awaited_once()
    assert backend._channel is None


# ---------------------------------------------------------------------------
# write_file() with metadata hash tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_file_stores_hash():
    """write_file() computes and stores the nucleus hash after a successful write."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()
    backend._metadata_api = MagicMock()
    backend._metadata_msgs = md_pb2_v1alpha

    content = b"hello world"
    set_hash_calls = []

    async def fake_set_hash(resource_identity, nucleus_hash):
        set_hash_calls.append((resource_identity, nucleus_hash))

    async def fake_write_source_content(resource_address, source):
        from wrapp.utils.storage_interface import Result

        # No sent-bytes hash observed -> write_file hashes the content itself.
        return Result.OK, None, "written-identity"

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_write_source_content", side_effect=fake_write_source_content),
        patch.object(backend, "_set_nucleus_hash_for_identity", side_effect=fake_set_hash),
    ):
        from wrapp.utils.storage_interface import Result

        result = await backend.write_file("https://example.com/file.usd", content, "")

    assert result == Result.OK
    assert len(set_hash_calls) == 1
    identity, stored_hash = set_hash_calls[0]
    assert identity == "written-identity"
    assert stored_hash.startswith("sha-256-flat;1048576;")


@pytest.mark.asyncio
async def test_write_file_skips_hash_on_error():
    """write_file() does not store a hash when the write fails."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()
    backend._metadata_api = MagicMock()
    backend._metadata_msgs = md_pb2_v1alpha

    set_hash_calls = []

    async def fake_set_hash(resource_identity, nucleus_hash):
        set_hash_calls.append((resource_identity, nucleus_hash))

    async def fake_write_source_content(resource_address, source):
        from wrapp.utils.storage_interface import Result

        return Result.ERROR, None, None

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_write_source_content", side_effect=fake_write_source_content),
        patch.object(backend, "_set_nucleus_hash_for_identity", side_effect=fake_set_hash),
    ):
        from wrapp.utils.storage_interface import Result

        result = await backend.write_file("https://example.com/file.usd", b"data", "")

    assert result == Result.ERROR
    assert len(set_hash_calls) == 0


@pytest.mark.asyncio
async def test_write_file_no_metadata_service():
    """write_file() succeeds without storing hash when Metadata API is unavailable."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._fileobject_version = "v1beta"
    backend._fileobject_msgs = fo_svc_pb2_v1beta
    backend._fileobject_types = fo_pb2_v1beta
    backend._fileobject_api = MagicMock()

    async def fake_write_source_content(resource_address, source):
        from wrapp.utils.storage_interface import Result

        return Result.OK, None, "written-identity"

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_write_source_content", side_effect=fake_write_source_content),
    ):
        from wrapp.utils.storage_interface import Result

        result = await backend.write_file("https://example.com/file.usd", b"data", "")

    assert result == Result.OK


# ---------------------------------------------------------------------------
# Router omniverse:// routing (regression for the StorageAPI-claims-omniverse fix)
# ---------------------------------------------------------------------------


class TestHasKnownScope:
    """Tests for StorageAPIStorageGRPC.has_known_scope()."""

    def test_false_before_connection(self):
        backend = _make_backend()
        assert backend._route_regexes is None
        assert backend._top_level_addresses is None
        assert backend.has_known_scope() is False

    def test_true_when_routes_populated(self):
        backend = _make_backend()
        backend._route_regexes = [StorageAPIStorageGRPC._compile_route_pattern("omniverse://example.com/**")]
        assert backend.has_known_scope() is True

    def test_true_when_tlas_populated(self):
        backend = _make_backend()
        backend._top_level_addresses = ["omniverse://example.com"]
        assert backend.has_known_scope() is True

    def test_true_when_tlas_populated_but_empty(self):
        # An empty list still means "we asked the server" — be authoritative.
        backend = _make_backend()
        backend._top_level_addresses = []
        assert backend.has_known_scope() is True

    def test_true_when_routes_populated_but_empty(self):
        # An empty list still means "we asked the server via ListRoutes"
        # — be authoritative, claim nothing.
        backend = _make_backend()
        backend._route_regexes = []
        assert backend.has_known_scope() is True


@pytest.mark.xdist_incompatible
class TestRouterOmniverseRouting:
    """Regression tests for routing ``omniverse://`` URLs to the Storage API.

    Before the fix, ``Router.is_storage_api_url`` short-circuited to ``False``
    for any ``omniverse://`` (or ``file://``) URL before consulting the Storage
    API's advertised scope, so a Storage API server that explicitly served
    ``omniverse://...`` URLs (via ListRoutes wildcards or ListTopLevelAddresses)
    never had its claim honored — those URLs fell through to the Nucleus
    backend and failed when Nucleus was disabled.
    """

    @staticmethod
    def _build_router(*, nucleus_enabled: bool):
        """Build a minimal Router with no real backends. Storage API and Nucleus
        backends are injected as mocks afterwards by the individual tests so
        that we exercise routing, not backend initialization."""
        from wrapp.utils import router as router_mod

        prev_instance = router_mod.Router._instance
        router_mod.Router._instance = None
        try:
            with patch.multiple(
                router_mod,
                g_aws_enabled=False,
                g_azure_enabled=False,
                g_gcs_enabled=False,
                g_nucleus_enabled=nucleus_enabled,
                g_storage_api_enabled=False,  # backends are injected manually
            ):
                router = router_mod.Router("test", False, ThrottlingParameters())
        finally:
            router_mod.Router._instance = prev_instance
        return router

    @staticmethod
    def _backend_with_routes(patterns):
        backend = _make_backend()
        backend._channel = MagicMock()
        backend._routes = list(patterns)
        backend._route_regexes = [StorageAPIStorageGRPC._compile_route_pattern(p) for p in patterns]
        return backend

    @staticmethod
    def _backend_with_tlas(tlas):
        backend = _make_backend()
        backend._channel = MagicMock()
        backend._top_level_addresses = list(tlas)
        return backend

    def test_omniverse_url_in_route_scope_claimed_by_storage_api(self):
        router = self._build_router(nucleus_enabled=True)
        router._storage_api = self._backend_with_routes(["omniverse://content.example.com/**"])

        assert router.is_storage_api_url("omniverse://content.example.com/sample-project/") is True
        assert router.is_storage_api_url("omniverse://content.example.com/sample-project") is True

    def test_omniverse_url_in_tla_scope_claimed_by_storage_api(self):
        # Mirrors v1beta servers, where ListRoutes is unavailable and TLAs are
        # the only source of routing scope.
        router = self._build_router(nucleus_enabled=True)
        router._storage_api = self._backend_with_tlas(["omniverse://content.example.com"])

        assert router.is_storage_api_url("omniverse://content.example.com/sample-project/") is True

    def test_omniverse_url_outside_scope_not_claimed(self):
        router = self._build_router(nucleus_enabled=True)
        router._storage_api = self._backend_with_routes(["omniverse://content.example.com/**"])

        assert router.is_storage_api_url("omniverse://other.host/something") is False

    def test_file_url_in_scope_claimed_by_storage_api(self):
        # Storage API can legitimately advertise file:// URLs (e.g. local-test
        # servers); we honor that claim once scope is known.
        router = self._build_router(nucleus_enabled=False)
        router._storage_api = self._backend_with_routes(["file:///srv/data/**"])

        assert router.is_storage_api_url("file:///srv/data/x") is True

    def test_omniverse_pre_connection_not_stolen(self):
        # Before the first RPC, has_known_scope() is False. We must not steal
        # omniverse:// / file:// URLs from their dedicated backends.
        router = self._build_router(nucleus_enabled=True)
        backend = _make_backend()
        assert backend.has_known_scope() is False
        router._storage_api = backend

        assert router.is_storage_api_url("omniverse://content.example.com/x") is False
        assert router.is_storage_api_url("file:///tmp/x") is False
        assert router.is_storage_api_url("/tmp/x") is False

    def test_https_pre_connection_still_permissive(self):
        # The pre-connection permissive path must still allow https:// URLs
        # so that lazy gRPC initialization can happen on the first call —
        # provided the backend has somewhere to connect to (after the
        # default-enabled / no-localhost-fallback flip, an unconfigured
        # backend stays inert and refuses to claim URLs).
        router = self._build_router(nucleus_enabled=True)
        backend = _make_backend()
        backend._endpoint = "localhost:50051"
        backend._discovery_url = ""
        assert backend.has_known_scope() is False
        router._storage_api = backend

        assert router.is_storage_api_url("https://example.com/file.usd") is True

    def test_storage_api_disabled_returns_false(self):
        router = self._build_router(nucleus_enabled=True)
        router._storage_api = None

        assert router.is_storage_api_url("omniverse://content.example.com/x") is False

    def test_omniverse_in_scope_routes_to_storageapi_with_nucleus_disabled(self):
        """The exact user-reported scenario: ``wrapp ls omniverse://...`` with
        Nucleus disabled now resolves to Storage API instead of erroring out.
        """
        if not g_storage_api_enabled:
            pytest.skip("Storage API not enabled at import time; cannot test _get_storage routing")

        from wrapp.utils.router import StorageType

        router = self._build_router(nucleus_enabled=False)
        router._storage_api = self._backend_with_tlas(["omniverse://content.example.com"])

        storage, storage_type = router._get_storage("omniverse://content.example.com/sample-project/")
        assert storage_type == StorageType.StorageAPI
        assert storage is router._storage_api

    def test_omniverse_in_scope_routes_to_storageapi_even_when_nucleus_enabled(self):
        """When both backends are enabled and Storage API has explicitly claimed
        ``omniverse://...`` via its routes, Storage API wins (matches the
        documented Router priority order).
        """
        if not g_storage_api_enabled:
            pytest.skip("Storage API not enabled at import time; cannot test _get_storage routing")

        from wrapp.utils.router import StorageType

        router = self._build_router(nucleus_enabled=True)
        # Inject a Nucleus mock so the dedicated branch would also be available.
        router._clientlib = MagicMock(name="ClientLibStorage")
        router._storage_api = self._backend_with_routes(["omniverse://content.example.com/**"])

        _, storage_type = router._get_storage("omniverse://content.example.com/sample-project/")
        assert storage_type == StorageType.StorageAPI

    def test_omniverse_outside_scope_falls_through_to_nucleus(self):
        """Counterpart: an ``omniverse://`` URL that Storage API has NOT claimed
        is still served by the Nucleus backend (priority change is scoped to
        URLs Storage API explicitly advertises)."""
        if not g_storage_api_enabled:
            pytest.skip("Storage API not enabled at import time; cannot test _get_storage routing")
        if not g_nucleus_enabled:
            pytest.skip("Nucleus not enabled at import time; Router.clientlib() is not defined")

        from wrapp.utils.router import StorageType

        router = self._build_router(nucleus_enabled=True)
        nucleus_mock = MagicMock(name="ClientLibStorage")
        router._clientlib = nucleus_mock
        router._storage_api = self._backend_with_routes(["omniverse://content.example.com/**"])

        storage, storage_type = router._get_storage("omniverse://other.host/folder/")
        assert storage_type == StorageType.Nucleus
        assert storage is nucleus_mock


# ---------------------------------------------------------------------------
# Discovery-URL prefix matching boundary checks
# ---------------------------------------------------------------------------


@pytest.mark.xdist_incompatible
class TestDiscoveryUrlBoundaryMatching:
    """The Router's discovery-URL fast-path must match the discovery URL itself
    and its subpaths, but not sibling names that share a common prefix
    (e.g. ``/discovery2`` must not match ``/discovery``)."""

    @staticmethod
    def _build_router_with_discovery(discovery_url: str):
        from wrapp.utils import router as router_mod

        prev_instance = router_mod.Router._instance
        router_mod.Router._instance = None
        try:
            with patch.multiple(
                router_mod,
                g_aws_enabled=False,
                g_azure_enabled=False,
                g_gcs_enabled=False,
                g_nucleus_enabled=False,
                g_storage_api_enabled=False,
            ):
                router = router_mod.Router("test", False, ThrottlingParameters())
        finally:
            router_mod.Router._instance = prev_instance
        backend = _make_backend()
        backend._discovery_url = StorageAPIStorageGRPC._normalize_discovery_url(discovery_url)
        # Give the backend known (but empty) scope so the fall-through path
        # in is_storage_api_url rejects URLs not covered by the discovery
        # check, isolating the boundary logic under test.
        backend._top_level_addresses = []
        router._storage_api = backend
        return router

    def test_exact_discovery_url_matches(self):
        router = self._build_router_with_discovery("https://disc.example.com/api")
        assert router.is_storage_api_url("https://disc.example.com/api") is True

    def test_discovery_url_with_trailing_slash_matches(self):
        router = self._build_router_with_discovery("https://disc.example.com/api")
        assert router.is_storage_api_url("https://disc.example.com/api/") is True

    def test_discovery_subpath_matches(self):
        router = self._build_router_with_discovery("https://disc.example.com/api")
        assert router.is_storage_api_url("https://disc.example.com/api/v1/routes") is True

    def test_sibling_name_does_not_match(self):
        router = self._build_router_with_discovery("https://disc.example.com/api")
        assert router.is_storage_api_url("https://disc.example.com/api2") is False

    def test_sibling_with_shared_prefix_does_not_match(self):
        router = self._build_router_with_discovery("https://disc.example.com/discovery")
        assert router.is_storage_api_url("https://disc.example.com/discovery-v2") is False

    def test_omniverse_scheme_discovery_url_matches(self):
        """An ``omniverse://``-scheme discovery URL is owned by the Storage API
        backend. The discovery fast-path must run before the
        ``is_nucleus_url`` early-return, otherwise an ``omniverse://`` discovery
        URL is mis-routed to the Nucleus backend (returns False on master)."""
        router = self._build_router_with_discovery("omniverse://disc.example.com")
        assert router.is_storage_api_url("omniverse://disc.example.com") is True
        assert router.is_storage_api_url("omniverse://disc.example.com/foo") is True


# ---------------------------------------------------------------------------
# wrapp.toml integration: discovery_url + routes overrides
# ---------------------------------------------------------------------------


class TestWrappConfigDiscoveryUrlPrecedence:
    """Env vars > wrapp.toml > built-in defaults for the discovery URL."""

    @staticmethod
    def _make_config(discovery_url=None, routes=None, source_path=None):
        from wrapp.utils.config import (
            StorageAPIBackendConfig,
            WrappConfig,
        )

        backend = StorageAPIBackendConfig(
            name="default",
            discovery_url=discovery_url,
            routes=tuple(routes) if routes else None,
        )
        return WrappConfig(
            source_path=source_path,
            storage_api_backends=(backend,),
        )

    def test_toml_used_when_env_unset(self, tmp_path):
        cfg = self._make_config(discovery_url="https://from-toml.example.com", source_path=tmp_path / "wrapp.toml")
        with patch.dict(os.environ, {"WRAPP_STORAGE_API_DISCOVERY_URL": ""}):
            backend = _make_backend(wrapp_config=cfg)
        assert backend._discovery_url == "https://from-toml.example.com"

    def test_env_wins_when_both_set(self, tmp_path, caplog):
        cfg = self._make_config(discovery_url="https://from-toml.example.com", source_path=tmp_path / "wrapp.toml")
        with (
            patch.dict(os.environ, {"WRAPP_STORAGE_API_DISCOVERY_URL": "https://from-env.example.com"}),
            caplog.at_level(logging.WARNING, logger="omni.wrapp"),
        ):
            backend = _make_backend(wrapp_config=cfg)
        assert backend._discovery_url == "https://from-env.example.com"
        assert "the environment variable wins" in caplog.text
        assert "from-toml.example.com" in caplog.text

    def test_no_warning_when_env_and_toml_agree(self, tmp_path, caplog):
        url = "https://shared.example.com"
        cfg = self._make_config(discovery_url=url, source_path=tmp_path / "wrapp.toml")
        with (
            patch.dict(os.environ, {"WRAPP_STORAGE_API_DISCOVERY_URL": url}),
            caplog.at_level(logging.WARNING, logger="omni.wrapp"),
        ):
            backend = _make_backend(wrapp_config=cfg)
        assert backend._discovery_url == url
        assert "the environment variable wins" not in caplog.text

    def test_no_config_means_env_only(self):
        from wrapp.utils.config import WrappConfig

        with patch.dict(os.environ, {"WRAPP_STORAGE_API_DISCOVERY_URL": "https://env-only.example.com"}):
            backend = _make_backend(wrapp_config=WrappConfig())
        assert backend._discovery_url == "https://env-only.example.com"


@pytest.mark.xdist_incompatible
class TestWrappConfigRouteOverride:
    """Configured routes replace the server-advertised route table; the
    server's response is still fetched and logged for diagnostics."""

    @staticmethod
    def _make_config_with_routes(patterns):
        from wrapp.utils.config import (
            StorageAPIBackendConfig,
            WrappConfig,
        )

        return WrappConfig(
            source_path=None,
            storage_api_backends=(
                StorageAPIBackendConfig(
                    name="default",
                    discovery_url="https://discovery.example.com",
                    routes=tuple(patterns),
                ),
            ),
        )

    def test_init_compiles_config_route_regexes(self):
        cfg = self._make_config_with_routes(["omniverse://example.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        assert backend._config_route_overrides == ["omniverse://example.com/**"]
        assert backend._config_route_regexes is not None
        assert len(backend._config_route_regexes) == 1
        # The override is installed into the live route table at construction
        # time so the Router can claim the URLs it covers before any gRPC
        # connection has happened.
        assert backend.has_known_scope() is True
        assert backend._routes == ["omniverse://example.com/**"]
        assert backend.is_url_in_scope("omniverse://example.com/foo") is True
        assert backend.is_url_in_scope("omniverse://other.example.com/foo") is False

    def test_apply_override_replaces_routes(self, caplog):
        cfg = self._make_config_with_routes(["omniverse://configured.example.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        # Simulate _fetch_routes having set the server's response.
        backend._routes = ["https://server-says.example.com/**"]
        backend._route_regexes = [
            StorageAPIStorageGRPC._compile_route_pattern("https://server-says.example.com/**"),
        ]
        backend._identity_route_regexes = [
            StorageAPIStorageGRPC._compile_route_pattern("storage-id://server-says/**"),
        ]

        with caplog.at_level(logging.INFO, logger="omni.wrapp"):
            backend._apply_config_route_overrides_to_scope()

        assert backend._routes == ["omniverse://configured.example.com/**"]
        assert backend.has_known_scope() is True
        assert backend.is_url_in_scope("omniverse://configured.example.com/x") is True
        assert backend.is_url_in_scope("https://server-says.example.com/x") is False
        assert backend.can_handle_resource_identity("storage-id://server-says/opaque-version") is True
        # The server's claim is still visible in the logs.
        assert "https://server-says.example.com/**" in caplog.text
        assert "configured" in caplog.text or "configured.example.com" in caplog.text

    def test_apply_override_replaces_tlas(self, caplog):
        cfg = self._make_config_with_routes(["omniverse://configured.example.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        backend._top_level_addresses = ["https://server-says.example.com"]
        backend._identity_top_level_addresses = ["https://server-says.example.com"]

        with caplog.at_level(logging.INFO, logger="omni.wrapp"):
            backend._apply_config_route_overrides_to_scope()

        # Route override wins; TLAs are dropped so is_url_in_scope takes the
        # routes branch unambiguously.
        assert backend._top_level_addresses is None
        assert backend._routes == ["omniverse://configured.example.com/**"]
        assert backend.is_url_in_scope("omniverse://configured.example.com/x") is True
        assert backend.is_url_in_scope("https://server-says.example.com/x") is False
        assert backend.can_handle_resource_identity("https://server-says.example.com/x?checkpoint=7") is True
        assert "https://server-says.example.com" in caplog.text

    def test_apply_override_noop_when_no_config(self):
        from wrapp.utils.config import WrappConfig

        backend = _make_backend(wrapp_config=WrappConfig())
        backend._routes = ["https://server.example.com/**"]
        backend._route_regexes = [StorageAPIStorageGRPC._compile_route_pattern("https://server.example.com/**")]

        backend._apply_config_route_overrides_to_scope()

        # No config → server's response is left untouched.
        assert backend._routes == ["https://server.example.com/**"]
        assert backend.is_url_in_scope("https://server.example.com/x") is True

    def test_user_repro_omniverse_via_toml_override(self):
        """The user's exact bug: TOML routes override claims omniverse:// URLs
        and the Router routes them to Storage API even with Nucleus disabled."""
        if not g_storage_api_enabled:
            pytest.skip("Storage API not enabled at import time; cannot test _get_storage routing")

        from wrapp.utils.router import StorageType

        cfg = self._make_config_with_routes(["omniverse://content.example.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        # Simulate the post-_ensure_grpc_stubs state where the override has
        # been applied to scope.
        backend._channel = MagicMock()
        backend._apply_config_route_overrides_to_scope()
        assert backend.has_known_scope() is True

        router = TestRouterOmniverseRouting._build_router(nucleus_enabled=False)
        router._storage_api = backend

        _, storage_type = router._get_storage("omniverse://content.example.com/sample-project/")
        assert storage_type == StorageType.StorageAPI


# ---------------------------------------------------------------------------
# list_file_tree (Enumerate) tests
# ---------------------------------------------------------------------------


def _wire_enumerate_stubs(backend) -> None:
    """Stub fileobject API/messages enough for list_file_tree."""
    backend._fileobject_msgs = fo_svc_pb2_v1alpha
    backend._fileobject_types = fo_pb2_v1alpha
    backend._fileobject_api = MagicMock()


def _make_address_info(address: str, *, size=None, mtime_seconds=None):
    """Build an AddressInfo proto with optional size/timestamp set."""
    md = fo_pb2_v1alpha.Metadata()
    if size is not None:
        md.data_object_size = size
    if mtime_seconds is not None:
        md.last_modified_timestamp.CopyFrom(Timestamp(seconds=mtime_seconds))
    return fo_pb2_v1alpha.AddressInfo(resource_address=address, metadata=md)


def _make_list_stat_item(address: str, identity: str, *, size: int = 1, mtime_seconds: int | None = None):
    if mtime_seconds is None:
        timestamp = None
    else:
        timestamp = Timestamp(seconds=mtime_seconds)
    return SimpleNamespace(
        resource_address=address,
        resource_info=SimpleNamespace(
            resource_identity=SimpleNamespace(encoded_identity=identity),
            metadata=SimpleNamespace(data_object_size=size, last_modified_timestamp=timestamp),
        ),
    )


def _make_storageapi_file_entry(address: str, identity: str, *, size: int | None = 1, mtime_seconds: int | None = None):
    modification_time = Timestamp(seconds=mtime_seconds).ToDatetime() if mtime_seconds is not None else None
    return StorageAPIListEntry(
        resource_address=address,
        resource_identity=identity,
        size=size,
        modification_time=modification_time,
        is_folder=False,
    )


def _make_storageapi_folder_entry(address: str):
    return StorageAPIListEntry(
        resource_address=address,
        resource_identity=None,
        size=None,
        modification_time=None,
        is_folder=True,
    )


def test_use_enumerate_default_is_on():
    """No env var, no kwarg → enumerate is enabled (production default)."""
    backend = _make_backend()
    assert backend._use_enumerate is True
    assert backend.supports_list_file_tree() is True


def test_use_enumerate_disabled_by_env(caplog):
    """WRAPP_STORAGE_API_DISABLE_ENUMERATE flips production default off."""
    caplog.set_level(logging.INFO, logger="omni.wrapp")
    backend = _make_backend(env_overrides={_DISABLE_ENUMERATE_ENV_VAR: "True"})
    assert backend._use_enumerate is False
    assert backend.supports_list_file_tree() is False
    assert _DISABLE_ENUMERATE_ENV_VAR in caplog.text


def test_use_enumerate_explicit_kwarg_overrides_env():
    """A test passing use_enumerate=False/True wins over the env var."""
    backend = _make_backend(
        env_overrides={_DISABLE_ENUMERATE_ENV_VAR: "True"},
        use_enumerate=True,
    )
    assert backend._use_enumerate is True

    backend = _make_backend(use_enumerate=False)
    assert backend._use_enumerate is False


@pytest.mark.asyncio
async def test_list_file_tree_anchors_prefix_with_trailing_slash():
    """Enumerate's resource_address is a string prefix on the server, so a
    request for ".../foo" leaks ".../foo-2/...". list_file_tree must always
    encode the prefix with a trailing slash."""
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    captured = {}

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        captured["throttler"] = throttler
        captured["request"] = request
        if False:
            yield  # make this an async generator
        return

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(
            backend,
            "_sanitize_address_for_storage_service",
            new=AsyncMock(return_value="https://srv.example/bucket/1k"),
        ),
    ):
        items = [item async for item in backend.list_file_tree("https://srv.example/bucket/1k")]

    assert items == []
    assert captured["request"].resource_address == "https://srv.example/bucket/1k/"
    assert captured["throttler"] is backend._throttler_serverdata


@pytest.mark.asyncio
async def test_list_file_tree_input_already_has_trailing_slash():
    """Idempotent: adding a slash to a slash-terminated input must not double up."""
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    captured = {}

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        captured["request"] = request
        if False:
            yield
        return

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(
            backend,
            "_sanitize_address_for_storage_service",
            new=AsyncMock(return_value="https://srv.example/bucket/1k/"),
        ),
    ):
        async for _ in backend.list_file_tree("https://srv.example/bucket/1k/"):
            pass

    assert captured["request"].resource_address == "https://srv.example/bucket/1k/"


@pytest.mark.asyncio
async def test_list_file_tree_marks_folder_shaped_addresses():
    """Folder entries from Enumerate must not be treated as regular files."""
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    base = "https://srv.example/bucket/1k"

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        yield fo_svc_pb2_v1alpha.EnumerateResponse(
            items=[
                _make_address_info(f"{base}/file-1", size=1, mtime_seconds=1_700_000_000),
                _make_address_info(f"{base}/empty_dir/", size=None, mtime_seconds=None),
                _make_address_info(f"{base}/file-2", size=2, mtime_seconds=1_700_000_001),
            ]
        )

    async def fake_stat(address):
        if address.endswith("/file-1"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-file-1", size=1)
        if address.endswith("/file-2"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-file-2", size=2)
        raise AssertionError(f"Unexpected stat call for {address}")

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
        patch.object(backend, "stat", AsyncMock(side_effect=fake_stat)) as stat_mock,
    ):
        items = [item async for item in backend.list_file_tree(base)]

    assert [(item.relative_path, item.is_folder) for item in items] == [
        ("file-1", False),
        ("empty_dir", True),
        ("file-2", False),
    ]
    assert stat_mock.await_count == 2


@pytest.mark.asyncio
async def test_list_file_tree_marks_prefix_entries_as_folders():
    """Some Storage API deployments return directory prefixes without a
    trailing slash. Treat entries that are parents of other entries as folders
    so catalog does not try to create resource identities for them."""
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    base = "https://srv.example/bucket/1k"

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        yield fo_svc_pb2_v1alpha.EnumerateResponse(
            items=[
                _make_address_info(f"{base}/subfolder", size=0, mtime_seconds=1_700_000_000),
                _make_address_info(f"{base}/subfolder/file.txt", size=42, mtime_seconds=1_700_000_001),
                _make_address_info(f"{base}/root.txt", size=1, mtime_seconds=1_700_000_002),
            ]
        )

    async def fake_stat(address):
        if address.endswith("/subfolder"):
            return Result.OK, _make_storageapi_folder_entry(address)
        if address.endswith("/subfolder/file.txt"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-nested", size=42)
        if address.endswith("/root.txt"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-root", size=1)
        raise AssertionError(f"Unexpected stat call for {address}")

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
        patch.object(backend, "stat", AsyncMock(side_effect=fake_stat)) as stat_mock,
    ):
        items = [item async for item in backend.list_file_tree(base)]

    assert [(item.relative_path, item.is_folder, item.size) for item in items] == [
        ("subfolder", True, None),
        ("subfolder/file.txt", False, 42),
        ("root.txt", False, 1),
    ]
    assert stat_mock.await_count == 3


@pytest.mark.asyncio
async def test_list_file_tree_populates_size_and_mtime_from_metadata():
    """Enumerate already returns size + last_modified_timestamp on this
    server. Catalog and ls callers benefit from these fields landing on the
    yielded ListEntry instead of triggering a separate stat() per file."""
    from datetime import datetime

    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    base = "https://srv.example/bucket/1k"

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        yield fo_svc_pb2_v1alpha.EnumerateResponse(
            items=[
                _make_address_info(f"{base}/file-with-metadata", size=42, mtime_seconds=1_700_000_000),
                _make_address_info(f"{base}/file-no-metadata"),  # both fields unset
            ]
        )

    async def fake_stat(address):
        if address.endswith("/file-with-metadata"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-with-metadata", size=None)
        if address.endswith("/file-no-metadata"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-no-metadata", size=None)
        raise AssertionError(f"Unexpected stat call for {address}")

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
        patch.object(backend, "stat", AsyncMock(side_effect=fake_stat)),
    ):
        items = [item async for item in backend.list_file_tree(base)]

    assert len(items) == 2
    by_path = {item._resource_address: item for item in items}

    assert by_path["file-with-metadata"]._data_object_size == 42
    # Match existing convention in StorageAPIListEntry.from_resource_info:
    # protobuf Timestamp.ToDatetime() returns a naive UTC datetime.
    assert by_path["file-with-metadata"]._modification_time == datetime.utcfromtimestamp(1_700_000_000)

    # Absent metadata fields must not silently coerce to 0/epoch.
    assert by_path["file-no-metadata"]._data_object_size is None
    assert by_path["file-no-metadata"]._modification_time is None


@pytest.mark.asyncio
async def test_list_file_tree_retries_rst_stream_and_skips_duplicates(monkeypatch):
    Stats.get().clear()
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    base = "https://srv.example/bucket/1k"
    call_count = 0
    sleep_calls = []
    retry_delay_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    def fake_retry_delay(attempt):
        retry_delay_calls.append(attempt)
        return 27.0

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield fo_svc_pb2_v1alpha.EnumerateResponse(items=[_make_address_info(f"{base}/file-1", size=1, mtime_seconds=1_700_000_000)])
            raise _make_grpc_error(grpc.StatusCode.INTERNAL, "Received RST_STREAM with error code 0")

        yield fo_svc_pb2_v1alpha.EnumerateResponse(
            items=[
                _make_address_info(f"{base}/file-1", size=1, mtime_seconds=1_700_000_000),
                _make_address_info(f"{base}/file-2", size=2, mtime_seconds=1_700_000_001),
            ]
        )

    async def fake_stat(address):
        if address.endswith("/file-1"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-file-1", size=1)
        if address.endswith("/file-2"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-file-2", size=2)
        raise AssertionError(f"Unexpected stat call for {address}")

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi._retry_delay", fake_retry_delay)
    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
        patch.object(backend, "stat", AsyncMock(side_effect=fake_stat)),
    ):
        items = [item async for item in backend.list_file_tree(base)]

    assert [item.relative_path for item in items] == ["file-1", "file-2"]
    assert call_count == 2
    assert retry_delay_calls == [1]
    assert sleep_calls == [27.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grpc_code, expected_result",
    [
        # NOT_FOUND is the only Enumerate error that must map to ERROR_NOT_FOUND so
        # callers treat an absent .metadata folder as an empty listing.
        (grpc.StatusCode.NOT_FOUND, Result.ERROR_NOT_FOUND),
        # Any other non-retryable gRPC error must keep the default Result.ERROR;
        # this guards the remap against being widened to non-NOT_FOUND codes.
        (grpc.StatusCode.PERMISSION_DENIED, Result.ERROR),
    ],
)
async def test_list_file_tree_maps_enumerate_grpc_error_codes(grpc_code, expected_result):
    """list_file_tree surfaces an Enumerate NOT_FOUND as StorageOperationError with
    result == Result.ERROR_NOT_FOUND, and any other non-retryable gRPC error as a
    StorageOperationError with the default Result.ERROR (not ERROR_NOT_FOUND)."""
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    base = "https://srv.example/bucket/1k"

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        # NOT_FOUND and PERMISSION_DENIED are not in _RETRYABLE_GRPC_STATUS_CODES,
        # so the retry loop raises on the first attempt without sleeping.
        raise _make_grpc_error(grpc_code, "boom")
        yield  # pragma: no cover - unreachable; makes this an async generator

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
    ):
        with pytest.raises(StorageOperationError) as exc:
            [item async for item in backend.list_file_tree(base)]

    assert exc.value.result == expected_result


@pytest.mark.asyncio
async def test_list_directory_retries_rst_stream_and_skips_duplicates(monkeypatch):
    Stats.get().clear()
    backend = _make_backend()
    backend._filefolder_api = MagicMock()
    backend._filefolder_msgs = ff_svc_pb2_v1beta
    base = "https://srv.example/bucket/1k"
    call_count = 0
    sleep_calls = []
    retry_delay_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)

    def fake_retry_delay(attempt):
        retry_delay_calls.append(attempt)
        return 28.0

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            yield SimpleNamespace(
                subfolder_addresses=[],
                entries=[_make_list_stat_item(f"{base}/file-1", "identity-1")],
            )
            raise _make_grpc_error(grpc.StatusCode.INTERNAL, "Received RST_STREAM with error code 0")

        yield SimpleNamespace(
            subfolder_addresses=[],
            entries=[
                _make_list_stat_item(f"{base}/file-1", "identity-1"),
                _make_list_stat_item(f"{base}/file-2", "identity-2"),
            ],
        )

    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("wrapp.utils.storage_impls.storageapi._retry_delay", fake_retry_delay)
    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
    ):
        items = [item async for item in backend.list_directory(base)]

    assert [item.relative_path for item in items] == ["file-1", "file-2"]
    assert [item.version for item in items] == ["identity-1", "identity-2"]
    assert call_count == 2
    assert retry_delay_calls == [1]
    assert sleep_calls == [28.0]
    assert Stats.get().get_stat(StatType.GRPC_RETRY) == 1


@pytest.mark.asyncio
async def test_make_resource_identity_lazy_stats_when_missing():
    """Entries from Enumerate carry no resource_identity (AddressInfo proto
    has only address+metadata). make_resource_identity must fall back to a
    single Stat to fetch one so catalog operations don't crash, and must
    patch the identity back onto the entry so a follow-up call doesn't
    re-stat."""
    from wrapp.utils.storage_impls.storageapi import StorageAPIListEntry
    from wrapp.utils.storage_interface import Result

    Stats.get().clear()
    backend = _make_backend()
    enumerate_entry = StorageAPIListEntry(
        resource_address="file-1",
        resource_identity=None,
        size=1,
        modification_time=None,
        is_folder=False,
    )

    stat_entry = StorageAPIListEntry(
        resource_address="file-1",
        resource_identity="encoded-identity-from-stat",
        size=1,
        modification_time=None,
        is_folder=False,
    )
    stat_entry.nucleus_hash = "sha-256-flat;1048576;from-stat"

    stat_mock = AsyncMock(return_value=(Result.OK, stat_entry))

    with patch.object(backend, "stat", stat_mock):
        _u1, _r1, ident1 = await backend.make_resource_identity("https://srv.example/bucket/1k/file-1", enumerate_entry, "file-1")
        # Second call on the same entry should hit the cached identity and
        # not issue another Stat.
        _u2, _r2, ident2 = await backend.make_resource_identity("https://srv.example/bucket/1k/file-1", enumerate_entry, "file-1")

    assert ident1.to_string() == "encoded-identity-from-stat"
    assert ident2.to_string() == ident1.to_string()
    assert stat_mock.await_count == 1
    assert enumerate_entry._resource_identity == "encoded-identity-from-stat"
    assert enumerate_entry.nucleus_hash == "sha-256-flat;1048576;from-stat"
    assert Stats.get().get_stat(StatType.STAT) == 1


@pytest.mark.asyncio
async def test_make_resource_identity_with_existing_identity_reads_version_metadata_without_stat():
    """Identity metadata can supply the hash without resolving the address again."""
    from wrapp.utils.storage_impls.storageapi import StorageAPIListEntry

    backend = _make_backend()
    entry = StorageAPIListEntry(
        resource_address="file-1",
        resource_identity="already-have-it",
        size=1,
        modification_time=None,
        is_folder=False,
    )

    stat_mock = AsyncMock()
    identity_hash_mock = AsyncMock(return_value="sha-256-flat;1048576;from-identity")
    with patch.object(backend, "stat", stat_mock), patch.object(backend, "_get_nucleus_hash_for_identity", identity_hash_mock):
        _u, _r, ident = await backend.make_resource_identity("https://srv.example/bucket/1k/file-1", entry, "file-1")

    assert ident.to_string() == "already-have-it"
    assert entry.nucleus_hash == "sha-256-flat;1048576;from-identity"
    identity_hash_mock.assert_awaited_once_with("already-have-it")
    stat_mock.assert_not_called()


def test_storageapi_resource_identity_routes_by_its_own_scope():
    backend = _make_backend()
    backend._identity_route_regexes = [backend._compile_route_pattern("storage-id://srv/**")]
    identity = "storage-id://srv/opaque-version"

    assert backend.can_handle_resource_identity(identity)
    assert not backend.can_handle_resource_identity("opaque-version")


def test_storage_core_resource_identity_routes_by_top_level_address():
    backend = _make_backend()
    backend._identity_top_level_addresses = ["omniverse://storage-core.example"]

    assert backend.can_handle_resource_identity("omniverse://storage-core.example/project/asset.usd?&7")
    assert not backend.can_handle_resource_identity("omniverse://other.example/project/asset.usd?&7")


def test_storageapi_does_not_claim_resource_identity_before_scope_is_known():
    backend = _make_backend()
    backend._endpoint = "storage-api.example:443"

    assert not backend.can_handle_resource_identity("omniverse://nucleus.example/project/asset.usd?&7")


@pytest.mark.asyncio
async def test_make_resource_identity_raises_when_stat_cannot_recover():
    """If the fallback Stat itself fails to produce an identity (storage
    doesn't support it, or the URL no longer exists), surface a clear
    TypeError so the catalog code's NotImplementedError/TypeError fallback
    path takes over."""
    from wrapp.utils.storage_impls.storageapi import StorageAPIListEntry
    from wrapp.utils.storage_interface import Result

    backend = _make_backend()
    entry = StorageAPIListEntry(
        resource_address="file-1",
        resource_identity=None,
        size=1,
        modification_time=None,
        is_folder=False,
    )

    stat_mock = AsyncMock(return_value=(Result.ERROR_NOT_FOUND, None))
    with patch.object(backend, "stat", stat_mock):
        with pytest.raises(TypeError, match="Cannot generate ResourceIdentity"):
            await backend.make_resource_identity("https://srv.example/bucket/1k/file-1", entry, "file-1")


@pytest.mark.asyncio
async def test_list_file_tree_stats_ambiguous_enumerate_entries():
    """Enumerate returns AddressInfo, not ResourceInfo, so WRAPP must classify
    ambiguous addresses before yielding them as files."""
    backend = _make_backend()
    _wire_enumerate_stubs(backend)
    base = "https://srv.example/bucket/1k"

    async def fake_iterate_grpc(throttler, method, request, *a, **k):
        yield fo_svc_pb2_v1alpha.EnumerateResponse(
            items=[
                _make_address_info(f"{base}/leaf-folder", size=0, mtime_seconds=1_700_000_000),
                _make_address_info(f"{base}/file.txt", size=1, mtime_seconds=1_700_000_001),
            ]
        )

    async def fake_stat(address):
        if address.endswith("/leaf-folder"):
            return Result.OK, _make_storageapi_folder_entry(address)
        if address.endswith("/file.txt"):
            return Result.OK, _make_storageapi_file_entry(address, "identity-file", size=1)
        raise AssertionError(f"Unexpected stat call for {address}")

    with (
        patch.object(backend, "_iterate_grpc", side_effect=fake_iterate_grpc),
        patch.object(backend, "_ensure_grpc_stubs", new=AsyncMock()),
        patch.object(backend, "_sanitize_address_for_storage_service", new=AsyncMock(return_value=base)),
        patch.object(backend, "stat", AsyncMock(side_effect=fake_stat)) as stat_mock,
    ):
        items = [item async for item in backend.list_file_tree(base)]

    assert [(item.relative_path, item.is_folder, item.version) for item in items] == [
        ("leaf-folder", True, ""),
        ("file.txt", False, "identity-file"),
    ]
    assert stat_mock.await_count == 2
