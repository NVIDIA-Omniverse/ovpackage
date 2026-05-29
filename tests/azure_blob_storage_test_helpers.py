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
import logging
import os
import random
import time
from contextlib import contextmanager
from typing import (
    Generator,
    List,
)

import pytest

from tests.storage_test_helpers import (
    StorageBackendForTests,
    StorageTestHelper,
    call_async,
    random_bytes,
)
from wrapp import (
    AuthManager,
    CommandParameters,
)
from wrapp.utils.storage import (
    Router,
    wrapp_stat,
    wrapp_write_file_bytes,
)
from wrapp.utils.storage_interface import (
    AzureBlobAuthInfo,
    Result,
    StorageAuthInfo,
)
from wrapp.utils.utils import (
    join_paths,
    random_string,
)


class AzureBlobStorageTestHelper(StorageTestHelper):
    def storage_type(self) -> StorageBackendForTests:
        return StorageBackendForTests.AZURE_BLOB_STORE

    def session_initialization(self, test_hosts: List[str]) -> Generator["StorageTestHelper", None, None]:
        created_containers: List[str] = []
        for container_name in test_hosts:
            try:
                # Get the Azure client
                azure_client = Router.me().azure()

                call_async(azure_client.create_container_if_not_exists(container_name))

                created_containers.append(container_name)
                logging.info(f"Created test container: {container_name}")
            except Exception as e:
                logging.error(f"Error creating test container {container_name}: {e}")

        yield self

    @contextmanager
    def clear_credentials_for_external_process(self, url: str):
        # Save original environment variables
        original_env = {}
        azure_env_vars = ["WRAPP_AZURE_CONNECTION_STRING", "WRAPP_AZURE_ACCOUNT_NAME", "WRAPP_AZURE_ACCOUNT_KEY", "WRAPP_AZURE_SAS_TOKEN"]

        for var in azure_env_vars:
            if var in os.environ:
                original_env[var] = os.environ[var]
                del os.environ[var]

        try:
            yield
        finally:
            # Restore original environment variables
            for var, value in original_env.items():
                os.environ[var] = value

    def cli_auth_param(self, auth_info: StorageAuthInfo) -> str:
        d = auth_info.auth_data
        assert isinstance(d, AzureBlobAuthInfo)

        if d.connection_string:
            return f"{auth_info.server_url},{d.connection_string}"
        elif d.account_name and d.account_key:
            return f"{auth_info.server_url},{d.account_name},{d.account_key}"
        elif d.account_name and d.sas_token:
            return f"{auth_info.server_url},{d.account_name},{d.sas_token}"
        else:
            raise ValueError("Invalid Azure Blob Storage authentication information")

    def env_auth_dict(self, auth_info: StorageAuthInfo) -> dict[str, str]:
        d = auth_info.auth_data
        assert isinstance(d, AzureBlobAuthInfo)

        r = {}
        if d.connection_string:
            r["WRAPP_AZURE_CONNECTION_STRING"] = d.connection_string
        if d.account_name:
            r["WRAPP_AZURE_ACCOUNT_NAME"] = d.account_name
        if d.account_key:
            r["WRAPP_AZURE_ACCOUNT_KEY"] = d.account_key
        if d.sas_token:
            r["WRAPP_AZURE_SAS_TOKEN"] = d.sas_token

        return r

    def _invalid_auth_infos(self, auth_info: StorageAuthInfo) -> list[StorageAuthInfo]:
        d = auth_info.auth_data
        assert isinstance(d, AzureBlobAuthInfo)

        invalid_auth_infos = []

        if d.connection_string:
            # Invalid connection string
            invalid_auth_infos.append(
                StorageAuthInfo(
                    auth_info.server_url,
                    AzureBlobAuthInfo(
                        connection_string="DefaultEndpointsProtocol=https;AccountName=invalid;AccountKey=invalid;EndpointSuffix=core.windows.net"
                    ),
                )
            )

        if d.account_name and d.account_key:
            # Invalid account key
            invalid_auth_infos.append(
                StorageAuthInfo(auth_info.server_url, AzureBlobAuthInfo(account_name=d.account_name, account_key="invalid_key"))
            )
            # Invalid account name
            invalid_auth_infos.append(
                StorageAuthInfo(auth_info.server_url, AzureBlobAuthInfo(account_name="invalid_account", account_key=d.account_key))
            )

        if d.account_name and d.sas_token:
            # Invalid SAS token
            invalid_auth_infos.append(
                StorageAuthInfo(auth_info.server_url, AzureBlobAuthInfo(account_name=d.account_name, sas_token="invalid_token"))
            )
            # Invalid account name
            invalid_auth_infos.append(
                StorageAuthInfo(auth_info.server_url, AzureBlobAuthInfo(account_name="invalid_account", sas_token=d.sas_token))
            )

        return invalid_auth_infos

    def cli_invalid_auth_data_and_interactive_flag(self, auth_info: StorageAuthInfo) -> list[tuple[str, bool]]:
        d = auth_info.auth_data
        assert isinstance(d, AzureBlobAuthInfo)

        invalid_auth_params: list[str] = [
            self.cli_auth_param(invalid_auth_info) for invalid_auth_info in self._invalid_auth_infos(auth_info)
        ]

        # Add some malformed parameters
        invalid_auth_params.extend([",", ",,", f"{auth_info.server_url},,", f",{d.connection_string if d.connection_string else ''},"])

        # Azure Blob Storage provider does not do interactive login, but test with flag on and off
        invalid_auth_data = [(p, False) for p in invalid_auth_params] + [(p, True) for p in invalid_auth_params]
        return invalid_auth_data

    def env_invalid_auth_data(self, auth_info: StorageAuthInfo) -> list[tuple[dict[str, str], bool]]:
        d = auth_info.auth_data
        assert isinstance(d, AzureBlobAuthInfo)

        invalid_auth_dicts: list[dict[str, str]] = [
            self.env_auth_dict(invalid_auth_info) for invalid_auth_info in self._invalid_auth_infos(auth_info)
        ]

        # Add some incomplete configurations
        if d.connection_string:
            invalid_auth_dicts.append({"WRAPP_AZURE_CONNECTION_STRING": ""})

        if d.account_name and d.account_key:
            invalid_auth_dicts.append({"WRAPP_AZURE_ACCOUNT_NAME": d.account_name})
            invalid_auth_dicts.append({"WRAPP_AZURE_ACCOUNT_KEY": d.account_key})

        if d.account_name and d.sas_token:
            invalid_auth_dicts.append({"WRAPP_AZURE_ACCOUNT_NAME": d.account_name})
            invalid_auth_dicts.append({"WRAPP_AZURE_SAS_TOKEN": d.sas_token})

        invalid_auth_dicts.append({})  # Empty dict

        # Azure Blob Storage provider does not do interactive login, but test with flag on and off
        invalid_auth_data = [(p, False) for p in invalid_auth_dicts] + [(p, True) for p in invalid_auth_dicts]
        return invalid_auth_data

    def skip_if_remote_server_down(self, auth: AuthManager, explicit_server_url: str):
        pass

    async def create_source_package(self, url, tags=False, prefix: str = "", large=False) -> str:
        return await self.create_source_package_for_azure(url, tags, prefix, large)

    async def is_versioned(self, url) -> bool:
        # TODO -  we need to query Azure and find out if that url is within a versioned storage account
        return False

    @staticmethod
    async def create_source_package_for_azure(url: str, tags=False, prefix: str = "", large=False) -> str:
        if large:
            pytest.skip("This takes too long on Azure Blob Storage.")

        # Create a copy source
        source_package = join_paths(url, f"{prefix}source_package")
        # Create a file
        source_file = join_paths(source_package, f"{prefix}testfile.txt")
        op_write_testfile_txt = (source_file, wrapp_write_file_bytes(CommandParameters(), source_file, random_bytes()))
        subfolder = join_paths(source_package, f"{prefix}subfolder")
        source_file_subfolder = join_paths(subfolder, f"{prefix}file_in_folder.txt")
        op_write_file_in_folder_txt = (
            source_file_subfolder,
            wrapp_write_file_bytes(CommandParameters(), source_file_subfolder, random_bytes()),
        )
        subsubfolder = join_paths(subfolder, f"{prefix}subsubfolder/file_in_subsubfolder.txt")
        op_create_subsubfolder = (subsubfolder, wrapp_write_file_bytes(CommandParameters(), subsubfolder, random_bytes()))
        ops = [op_write_testfile_txt, op_write_file_in_folder_txt, op_create_subsubfolder]

        if large:
            for _ in range(100):
                folder = join_paths(source_package, random_string())
                for _ in range(random.randrange(1, 5)):
                    folder = join_paths(folder, random_string())
                path = join_paths(folder, "random_file.txt")
                ops.append((path, wrapp_write_file_bytes(CommandParameters(), path, random_bytes())))

        # Add a non-prefixed folder to test folder overlap
        overlapping = join_paths(url, "overlapping/file_in_overlapping.txt")
        ops.append((overlapping, wrapp_write_file_bytes(CommandParameters(), overlapping, b"overlapping but same content")))

        results = await asyncio.gather(*[o for _, o in ops])
        for i in range(len(results)):
            r = results[i]
            n = ops[i][0]
            assert r == Result.OK, f"Operation for file {n} failed with {r}!"

        return source_package

    async def create_large_file(self, url: str, file_size: int):
        r, e = await wrapp_stat(url)
        if r == Result.OK:
            assert e.size == file_size
        else:
            assert r == Result.ERROR_NOT_FOUND
            logging.info(f"Creating data of size {file_size} for {url}")
            buffer = bytes(file_size)

            logging.info(f"Writing data of size {file_size} for {url}")

            # Use the write_file method to upload the large file
            result = await Router.me().azure().write_file(url, buffer, "")
            assert result == Result.OK, f"Failed to create large file '{url}'"

    @classmethod
    def env_default_credentials(cls) -> dict[str, str]:
        # Use environment variables if available, otherwise use dummy values
        connection_string = os.getenv("WRAPP_AZURE_CONNECTION_STRING")
        account_name = os.getenv("WRAPP_AZURE_ACCOUNT_NAME")
        account_key = os.getenv("WRAPP_AZURE_ACCOUNT_KEY")
        sas_token = os.getenv("WRAPP_AZURE_SAS_TOKEN")

        if connection_string:
            return {"WRAPP_AZURE_CONNECTION_STRING": connection_string}
        elif account_name and account_key:
            return {"WRAPP_AZURE_ACCOUNT_NAME": account_name, "WRAPP_AZURE_ACCOUNT_KEY": account_key}
        elif account_name and sas_token:
            return {"WRAPP_AZURE_ACCOUNT_NAME": account_name, "WRAPP_AZURE_SAS_TOKEN": sas_token}
        else:
            # Dummy values for testing
            return {
                "WRAPP_AZURE_ACCOUNT_NAME": "devstoreaccount1",
                # Azurite default key – placeholder only, _not_ a production secret.
                "WRAPP_AZURE_ACCOUNT_KEY": "LOCAL_AZURITE_DEFAULT_KEY",
            }

    @classmethod
    def default_credentials(cls, server_url: str) -> StorageAuthInfo:
        env_creds = cls.env_default_credentials()

        if "WRAPP_AZURE_CONNECTION_STRING" in env_creds:
            return StorageAuthInfo(
                server_url=server_url, auth_data=AzureBlobAuthInfo(connection_string=env_creds["WRAPP_AZURE_CONNECTION_STRING"])
            )
        elif "WRAPP_AZURE_ACCOUNT_NAME" in env_creds and "WRAPP_AZURE_ACCOUNT_KEY" in env_creds:
            return StorageAuthInfo(
                server_url=server_url,
                auth_data=AzureBlobAuthInfo(
                    account_name=env_creds["WRAPP_AZURE_ACCOUNT_NAME"], account_key=env_creds["WRAPP_AZURE_ACCOUNT_KEY"]
                ),
            )
        elif "WRAPP_AZURE_ACCOUNT_NAME" in env_creds and "WRAPP_AZURE_SAS_TOKEN" in env_creds:
            return StorageAuthInfo(
                server_url=server_url,
                auth_data=AzureBlobAuthInfo(
                    account_name=env_creds["WRAPP_AZURE_ACCOUNT_NAME"], sas_token=env_creds["WRAPP_AZURE_SAS_TOKEN"]
                ),
            )
        else:
            raise ValueError("No valid Azure Blob Storage credentials available")
