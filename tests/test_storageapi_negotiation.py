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

import asyncio
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
from wrapp.utils.utils import (
    StorageInitializationError,
    StorageMissingDependenciesError,
    StorageOperationError,
)

try:
    import grpc

    from wrapp.utils.config import WrappConfig
    from wrapp.utils.storage_impls.storageapi import (
        _CAP_PB2,
        _FF_SVC_PB2,
        _FO_PB2,
        _FO_SVC_PB2,
        _MD_GRPC,
        _MD_PB2,
        _STORAGE_API_VERSION_ENV_VARS,
        NUCLEUS_HASH_METADATA_KEY,
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
        from wrapp.utils.storage_interface import Result

        return Result.OK

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "_server_side_copy", side_effect=fake_server_copy),
    ):
        from wrapp.utils.storage_interface import Result

        result = await backend.copy("src://a", "dst://b")
        assert result == Result.OK
        assert server_copy_called


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
            await backend.copy("src://a", "dst://b")


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
        patch.object(backend, "_server_side_copy", side_effect=fake_server_copy),
    ):
        with pytest.raises(NotImplementedError):
            await backend.copy("src://a", "dst://b")


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
async def test_set_nucleus_hash_calls_update_metadata():
    """_set_nucleus_hash calls UpdateMetadata with the correct key and value."""
    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    calls = []

    async def fake_run_grpc(throttler, method, request, **kwargs):
        calls.append(request)
        return SimpleNamespace(etag="etag1")

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        await backend._set_nucleus_hash("https://example.com/file.usd", "sha-256-flat;1048576;abc123")

    assert len(calls) == 1
    req = calls[0]
    assert req.uri == "https://example.com/file.usd"
    assert req.user_metadata_key == NUCLEUS_HASH_METADATA_KEY
    assert req.user_metadata.string_value == "sha-256-flat;1048576;abc123"


@pytest.mark.asyncio
async def test_set_nucleus_hash_noop_when_no_metadata_api():
    """_set_nucleus_hash is a no-op when the Metadata API is not available."""
    backend = _make_backend()
    assert backend._metadata_api is None

    # Should not raise
    await backend._set_nucleus_hash("https://example.com/file.usd", "sha-256-flat;1048576;abc123")


@pytest.mark.asyncio
async def test_set_nucleus_hash_logs_on_failure():
    """_set_nucleus_hash logs a warning but does not raise on gRPC errors."""
    backend = _make_backend()
    backend._metadata_msgs = md_pb2_v1alpha
    backend._metadata_api = MagicMock()

    async def fake_run_grpc(throttler, method, request, **kwargs):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.INTERNAL
        raise error

    with patch.object(backend, "_run_grpc", side_effect=fake_run_grpc):
        # Should not raise
        await backend._set_nucleus_hash("https://example.com/file.usd", "sha-256-flat;1048576;abc123")


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
        patch.object(backend, "_get_nucleus_hash", new_callable=AsyncMock, return_value=expected_hash),
    ):
        from wrapp.utils.storage_interface import Result

        result, entry = await backend.stat("https://example.com/file.usd")

    assert result == Result.OK
    assert entry is not None
    assert entry.nucleus_hash == expected_hash


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

    def test_single_star_allows_hyphens_underscores_dots(self):
        assert self._matches("https://*.example.com/**", "https://my-bucket_v2.0.example.com/path")


# ---------------------------------------------------------------------------
# _fetch_routes tests
# ---------------------------------------------------------------------------


def _make_list_routes_response(patterns):
    """Build a fake ListRoutesResponse with wildcard patterns."""
    items = [SimpleNamespace(wildcard_pattern=p) for p in patterns]
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
async def test_fetch_routes_falls_back_on_unimplemented():
    """When the server returns UNIMPLEMENTED, _fetch_routes returns False."""
    backend = _make_backend()
    backend._channel = MagicMock()
    backend._capabilities_version = "v1alpha"
    backend._capabilities_msgs = cap_pb2_v1alpha
    backend._capability_api = MagicMock()

    async def fake_run_grpc(throttler, method, request, **kwargs):
        error = grpc.RpcError()
        error.code = lambda: grpc.StatusCode.UNIMPLEMENTED
        raise error

    with (
        patch.object(backend, "_run_grpc", side_effect=fake_run_grpc),
        patch("wrapp.utils.router.g_list_routes_enabled", True),
    ):
        result = await backend._fetch_routes()

    assert result is False
    assert backend._routes is None
    assert backend._route_regexes is None


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
async def test_fetch_routes_skipped_when_v1beta():
    """ListRoutes is only in v1alpha; _fetch_routes returns False for v1beta."""
    backend = _make_backend()
    backend._capabilities_version = "v1beta"

    with patch("wrapp.utils.router.g_list_routes_enabled", True):
        result = await backend._fetch_routes()

    assert result is False
    assert backend._routes is None


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
    is installed by _ensure_grpc_stubs after discovery."""
    toml_file = tmp_path / "wrapp.toml"
    toml_file.write_text('[storage_api.default]\nroutes = ["https://override.example.com/**"]\n')

    backend = _make_backend(
        env_overrides={"WRAPP_CONFIG_FILE": str(toml_file)},
        wrapp_config=None,
    )
    backend._channel = None
    backend._discovery_url = ""
    backend._endpoint = "localhost:50051"

    assert backend.has_known_scope() is False

    with (
        patch.object(backend, "_create_grpc_channel", return_value=MagicMock()),
        patch.object(backend, "_negotiate_versions", new_callable=AsyncMock),
        patch.object(backend, "_fetch_routes", new_callable=AsyncMock, return_value=True),
        patch.object(backend, "_fetch_top_level_addresses", new_callable=AsyncMock),
    ):
        await backend._ensure_grpc_stubs()

    assert backend.has_known_scope() is True
    assert backend._routes == ["https://override.example.com/**"]
    assert backend.is_url_in_scope("https://override.example.com/file.usd") is True
    assert backend.is_url_in_scope("https://other.example.com/file.usd") is False


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

    async def fake_set_hash(resource_address, nucleus_hash):
        set_hash_calls.append((resource_address, nucleus_hash))

    async def fake_write_content(resource_address, content):
        from wrapp.utils.storage_interface import Result

        return Result.OK

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "write_file_content", side_effect=fake_write_content),
        patch.object(backend, "_set_nucleus_hash", side_effect=fake_set_hash),
    ):
        from wrapp.utils.storage_interface import Result

        result = await backend.write_file("https://example.com/file.usd", content, "")

    assert result == Result.OK
    assert len(set_hash_calls) == 1
    addr, stored_hash = set_hash_calls[0]
    assert addr == "https://example.com/file.usd"
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

    async def fake_set_hash(resource_address, nucleus_hash):
        set_hash_calls.append((resource_address, nucleus_hash))

    async def fake_write_content(resource_address, content):
        from wrapp.utils.storage_interface import Result

        return Result.ERROR

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "write_file_content", side_effect=fake_write_content),
        patch.object(backend, "_set_nucleus_hash", side_effect=fake_set_hash),
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

    async def fake_write_content(resource_address, content):
        from wrapp.utils.storage_interface import Result

        return Result.OK

    with (
        patch.object(backend, "_ensure_grpc_stubs", new_callable=AsyncMock),
        patch.object(backend, "_sanitize_address_for_storage_service", side_effect=_passthrough),
        patch.object(backend, "write_file_content", side_effect=fake_write_content),
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
        router._storage_api = self._backend_with_routes(["omniverse://content.ov.nvidia.com/**"])

        assert router.is_storage_api_url("omniverse://content.ov.nvidia.com/eglanz-test3/") is True
        assert router.is_storage_api_url("omniverse://content.ov.nvidia.com/eglanz-test3") is True

    def test_omniverse_url_in_tla_scope_claimed_by_storage_api(self):
        # Mirrors v1beta servers, where ListRoutes is unavailable and TLAs are
        # the only source of routing scope.
        router = self._build_router(nucleus_enabled=True)
        router._storage_api = self._backend_with_tlas(["omniverse://content.ov.nvidia.com"])

        assert router.is_storage_api_url("omniverse://content.ov.nvidia.com/eglanz-test3/") is True

    def test_omniverse_url_outside_scope_not_claimed(self):
        router = self._build_router(nucleus_enabled=True)
        router._storage_api = self._backend_with_routes(["omniverse://content.ov.nvidia.com/**"])

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

        assert router.is_storage_api_url("omniverse://content.ov.nvidia.com/x") is False
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

        assert router.is_storage_api_url("omniverse://content.ov.nvidia.com/x") is False

    def test_omniverse_in_scope_routes_to_storageapi_with_nucleus_disabled(self):
        """The exact user-reported scenario: ``wrapp ls omniverse://...`` with
        Nucleus disabled now resolves to Storage API instead of erroring out.
        """
        if not g_storage_api_enabled:
            pytest.skip("Storage API not enabled at import time; cannot test _get_storage routing")

        from wrapp.utils.router import StorageType

        router = self._build_router(nucleus_enabled=False)
        router._storage_api = self._backend_with_tlas(["omniverse://content.ov.nvidia.com"])

        storage, storage_type = router._get_storage("omniverse://content.ov.nvidia.com/eglanz-test3/")
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
        router._storage_api = self._backend_with_routes(["omniverse://content.ov.nvidia.com/**"])

        _, storage_type = router._get_storage("omniverse://content.ov.nvidia.com/eglanz-test3/")
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
        router._storage_api = self._backend_with_routes(["omniverse://content.ov.nvidia.com/**"])

        storage, storage_type = router._get_storage("omniverse://other.host/folder/")
        assert storage_type == StorageType.Nucleus
        assert storage is nucleus_mock


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
                    discovery_url=None,
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
        # Pre-connection, scope is not yet "known" — override only takes effect
        # after _ensure_grpc_stubs runs the override hook.
        assert backend.has_known_scope() is False

    def test_apply_override_replaces_routes(self, caplog):
        cfg = self._make_config_with_routes(["omniverse://configured.example.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        # Simulate _fetch_routes having set the server's response.
        backend._routes = ["https://server-says.example.com/**"]
        backend._route_regexes = [
            StorageAPIStorageGRPC._compile_route_pattern("https://server-says.example.com/**"),
        ]

        with caplog.at_level(logging.INFO, logger="omni.wrapp"):
            backend._apply_config_route_overrides_to_scope()

        assert backend._routes == ["omniverse://configured.example.com/**"]
        assert backend.has_known_scope() is True
        assert backend.is_url_in_scope("omniverse://configured.example.com/x") is True
        assert backend.is_url_in_scope("https://server-says.example.com/x") is False
        # The server's claim is still visible in the logs.
        assert "https://server-says.example.com/**" in caplog.text
        assert "configured" in caplog.text or "configured.example.com" in caplog.text

    def test_apply_override_replaces_tlas(self, caplog):
        cfg = self._make_config_with_routes(["omniverse://configured.example.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        backend._top_level_addresses = ["https://server-says.example.com"]

        with caplog.at_level(logging.INFO, logger="omni.wrapp"):
            backend._apply_config_route_overrides_to_scope()

        # Route override wins; TLAs are dropped so is_url_in_scope takes the
        # routes branch unambiguously.
        assert backend._top_level_addresses is None
        assert backend._routes == ["omniverse://configured.example.com/**"]
        assert backend.is_url_in_scope("omniverse://configured.example.com/x") is True
        assert backend.is_url_in_scope("https://server-says.example.com/x") is False
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

        cfg = self._make_config_with_routes(["omniverse://content.ov.nvidia.com/**"])
        backend = _make_backend(wrapp_config=cfg)
        # Simulate the post-_ensure_grpc_stubs state where the override has
        # been applied to scope.
        backend._channel = MagicMock()
        backend._apply_config_route_overrides_to_scope()
        assert backend.has_known_scope() is True

        router = TestRouterOmniverseRouting._build_router(nucleus_enabled=False)
        router._storage_api = backend

        _, storage_type = router._get_storage("omniverse://content.ov.nvidia.com/eglanz-test3/")
        assert storage_type == StorageType.StorageAPI
