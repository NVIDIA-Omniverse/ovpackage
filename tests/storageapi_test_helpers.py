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
from typing import (
    Generator,
    List,
    Optional,
)

from tests.storage_test_helpers import (
    StorageBackendForTests,
    StorageTestHelper,
    random_bytes,
)
from wrapp import (
    AuthManager,
    StorageAuthInfo,
    ThrottlingParameters,
    __version__,
)
from wrapp.utils.coro import force_wrapp_thread
from wrapp.utils.storage_impls.storageapi import (
    StorageAPIStorage,
    StorageAPIStorageGRPC,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import random_string


def join_paths(a, b):
    return a + "/" + b.lstrip("/")


class StorageAPITestHelper(StorageTestHelper):
    def __init__(self, use_enumerate=False):
        self.storage: Optional[StorageAPIStorage] = None
        self._use_enumerate = use_enumerate

    def storage_type(self) -> StorageBackendForTests:
        return StorageBackendForTests.STORAGEAPI if not self._use_enumerate else StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE

    def session_initialization(self, test_hosts: List[str]) -> Generator["StorageTestHelper", None, None]:
        self.storage = StorageAPIStorageGRPC(__version__, True, throttling_params=ThrottlingParameters(), use_enumerate=self._use_enumerate)
        yield self

    def skip_if_remote_server_down(self, auth: AuthManager, explicit_server_url: str):
        pass

    async def is_versioned(self, url) -> bool:
        # TODO - currently, this cannot be determined via the Capabilities API or the Versioning API
        return False

    @force_wrapp_thread
    async def create_source_package(self, url, tags=False, prefix: str = "", large=False) -> str:
        # Create a copy source
        source_package = join_paths(url, f"{prefix}source_package")
        # Create a file
        source_file = join_paths(source_package, f"{prefix}testfile.txt")
        result = await self.storage.write_file(source_file, random_bytes(), "")
        assert result == Result.OK, f"result is not OK: {result}"
        # Add a subfolder with a file in it, and a subsubfolder
        subfolder = join_paths(source_package, f"{prefix}subfolder")
        source_file_subfolder = join_paths(subfolder, f"{prefix}file_in_folder.txt")
        result = await self.storage.write_file(source_file_subfolder, random_bytes(), "")
        assert result == Result.OK, f"result is not OK: {result}"
        subsubfolder = join_paths(subfolder, f"{prefix}subsubfolder/file_in_subsubfolder.txt")
        result = await self.storage.write_file(subsubfolder, random_bytes(), "")
        assert result == Result.OK, f"result is not OK: {result}"

        if large:
            for i in range(100):
                folder = join_paths(source_package, random_string())
                for r in range(random.randrange(1, 5)):
                    folder = join_paths(folder, random_string())
                result = await self.storage.write_file(join_paths(folder, "random_file.txt"), random_bytes(), "")
                assert result == Result.OK, f"result is not OK: {result}"

        # Add a non-prefixed folder to test folder overlap
        overlapping = join_paths(url, f"overlapping/file_in_overlapping.txt")
        result = await self.storage.write_file(overlapping, b"overlapping but same content", "")
        assert result == Result.OK, f"result is not OK: {result}"

        return source_package

    def clear_credentials_for_external_process(self, url: str):
        pass

    def cli_auth_param(self, auth_info: StorageAuthInfo) -> str:
        return ""

    def env_auth_dict(self, auth_info: StorageAuthInfo) -> dict[str, str]:
        return {}

    def cli_invalid_auth_data_and_interactive_flag(self, auth_info: StorageAuthInfo) -> list[tuple[str, bool]]:
        pass

    def env_invalid_auth_data(self, auth_info: StorageAuthInfo) -> list[tuple[dict[str, str], bool]]:
        pass
