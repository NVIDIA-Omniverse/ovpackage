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

import random
import string
from abc import (
    ABC,
    abstractmethod,
)
from contextlib import contextmanager
from enum import Enum
from typing import (
    Any,
    Coroutine,
    Generator,
    List,
)

import pytest

from wrapp.datastructures.authentication import (
    AuthManager,
    StorageAuthInfo,
)
from wrapp.datastructures.command_context import CommandParameters
from wrapp.utils.coro import sync_calls_async_on_wrapp_thread
from wrapp.utils.storage import (
    Result,
    wrapp_write_file_bytes,
)
from wrapp.utils.utils import join_paths


# Helper routine now only used in tests, moved here from wrapp.utils
def call_async(async_call: Coroutine) -> Any:
    @sync_calls_async_on_wrapp_thread
    async def do_it():
        return await async_call

    return do_it()


class StorageBackendForTests(Enum):
    NUCLEUS = 1
    S3 = 2
    LOCAL_DIR = 3
    STORAGEAPI = 4
    STORAGEAPI_WITH_ENUMERATE = 5
    AZURE_BLOB_STORE = 6
    GCS = 7


def random_bytes(k: int = 8) -> bytes:
    return "".join(random.choices(string.ascii_lowercase, k=k)).encode("utf-8")


class StorageTestHelper(ABC):
    @abstractmethod
    def storage_type(self) -> StorageBackendForTests:
        ...

    # Function is a generator and must yield "self", see storage_backend fixture
    @abstractmethod
    def session_initialization(self, test_hosts: List[str]) -> Generator["StorageTestHelper", None, None]:
        ...

    # This needs to clear any credentials WRAPP might pick up when started as an external process
    # with auth credentials specified either on the CLI or via environment variable.
    @abstractmethod
    @contextmanager
    def clear_credentials_for_external_process(self, url: str):
        ...

    @abstractmethod
    def cli_auth_param(self, auth_info: StorageAuthInfo) -> str:
        ...

    @abstractmethod
    def env_auth_dict(self, auth_info: StorageAuthInfo) -> dict[str, str]:
        ...

    # Return a list of invalid values for testing for the "--auth" parameter and a flag if "--interactive-auth"
    # should be enabled for the test (to verify that it is not actually triggered when the credentials are invalid)
    @abstractmethod
    def cli_invalid_auth_data_and_interactive_flag(self, auth_info: StorageAuthInfo) -> list[tuple[str, bool]]:
        ...

    # Return a list of invalid key,value pairs for testing authentication with environment variables,
    # and a flag if "--interactive-auth" should be enabled for the test
    # (to verify that it is not actually triggered when the credentials are invalid)
    @abstractmethod
    def env_invalid_auth_data(self, auth_info: StorageAuthInfo) -> list[tuple[dict[str, str], bool]]:
        ...

    @abstractmethod
    def skip_if_remote_server_down(self, auth: AuthManager, explicit_server_url: str):
        ...

    # Ideally reimplement this with storage specific functions: this relies on functions which are yet to be tested.
    @abstractmethod
    async def create_source_package(self, url, tags=False, prefix: str = "", large=False) -> str:
        ...

    # Return True if this storage and the given url is within a versioned storage area
    @abstractmethod
    async def is_versioned(self, url) -> bool:
        ...
