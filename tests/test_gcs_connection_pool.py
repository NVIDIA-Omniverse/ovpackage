#!/usr/bin/env python3
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

import pytest

from wrapp.utils.utils import StorageMissingDependenciesError

try:
    import wrapp.utils.storage_impls.gcs_storage as gcs_module
except StorageMissingDependenciesError:
    pytest.skip("Missing dependencies for gcs_storage", allow_module_level=True)

requests = gcs_module.requests


class _FakeAuthRequest:
    def __init__(self) -> None:
        self.session = requests.Session()


class _FakeGCSClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self._http = requests.Session()
        self._http._auth_request_session = requests.Session()
        self._http._auth_request = _FakeAuthRequest()

    def close(self) -> None:
        return None


def _pool_adapter_settings(session: requests.Session) -> tuple[int, int, bool]:
    adapter = session.adapters["https://"]
    return adapter._pool_connections, adapter._pool_maxsize, adapter._pool_block


def test_gcs_client_wrapper_applies_pool_to_all_requests_sessions(monkeypatch):
    monkeypatch.setattr(gcs_module.gcs_storage, "Client", _FakeGCSClient)

    wrapper = gcs_module.GCSClientWrapper(pool_size=37)
    client = wrapper.get_sync_client()

    sessions = [client._http, client._http._auth_request_session, client._http._auth_request.session]
    for session in sessions:
        assert _pool_adapter_settings(session) == (37, 37, True)


def test_gcs_client_wrapper_updates_pool_for_existing_client(monkeypatch):
    monkeypatch.setattr(gcs_module.gcs_storage, "Client", _FakeGCSClient)

    wrapper = gcs_module.GCSClientWrapper(pool_size=11)
    client = wrapper.get_sync_client()

    wrapper.update_pool_size(53)

    sessions = [client._http, client._http._auth_request_session, client._http._auth_request.session]
    for session in sessions:
        assert _pool_adapter_settings(session) == (53, 53, True)
