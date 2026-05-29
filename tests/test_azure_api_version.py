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
Tests for Azure Storage API version configuration.

These tests verify that:
1. The default API version is correctly set
2. The API version can be overridden via WRAPP_AZURE_API_VERSION environment variable
3. The API version is correctly passed to the Azure Blob Service Client
"""

import os
import sys
import unittest
from unittest import mock

import pytest

from wrapp.utils.utils import StorageMissingDependenciesError


def _clear_azure_blob_storage_module():
    """Clear the azure_blob_storage module from cache to allow fresh reimport."""
    module_name = "wrapp.utils.storage_impls.azure_blob_storage"
    parent_name = "wrapp.utils.storage_impls"

    # Remove from sys.modules
    if module_name in sys.modules:
        del sys.modules[module_name]

    # Also clear from parent package namespace to force reimport
    parent_module = sys.modules.get(parent_name)
    if parent_module and hasattr(parent_module, "azure_blob_storage"):
        delattr(parent_module, "azure_blob_storage")


class TestAzureApiVersion(unittest.TestCase):
    """Test the Azure API version configuration."""

    def test_default_api_version(self):
        """Test that the default API version is set correctly."""
        # Clear any existing environment variable
        env = os.environ.copy()
        env.pop("WRAPP_AZURE_API_VERSION", None)

        with mock.patch.dict(os.environ, env, clear=True):
            # Clear module cache to force fresh import with new environment
            _clear_azure_blob_storage_module()

            try:
                # Fresh import reads the new environment
                from wrapp.utils.storage_impls import azure_blob_storage

                # Check the default value
                self.assertEqual(azure_blob_storage.AZURE_API_VERSION, None)
            except StorageMissingDependenciesError:
                pytest.skip("Missing dependencies for azure_blob_storage")
            finally:
                # Cleanup for other tests
                _clear_azure_blob_storage_module()

    def test_api_version_override_via_env(self):
        """Test that the API version can be overridden via environment variable."""
        custom_version = "2023-11-03"

        with mock.patch.dict(os.environ, {"WRAPP_AZURE_API_VERSION": custom_version}):
            # Clear module cache to force fresh import with new environment
            _clear_azure_blob_storage_module()

            try:
                # Fresh import reads the new environment
                from wrapp.utils.storage_impls import azure_blob_storage

                # Check that the custom version is used
                self.assertEqual(azure_blob_storage.AZURE_API_VERSION, custom_version)
            except StorageMissingDependenciesError:
                pytest.skip("Missing dependencies for azure_blob_storage")
            finally:
                # Cleanup for other tests
                _clear_azure_blob_storage_module()

    def test_api_version_azurite_compatible(self):
        """Test override with an Azurite-compatible older version."""
        # Azurite may not support the latest API versions
        azurite_version = "2021-08-06"

        with mock.patch.dict(os.environ, {"WRAPP_AZURE_API_VERSION": azurite_version}):
            # Clear module cache to force fresh import with new environment
            _clear_azure_blob_storage_module()

            try:
                # Fresh import reads the new environment
                from wrapp.utils.storage_impls import azure_blob_storage

                self.assertEqual(azure_blob_storage.AZURE_API_VERSION, azurite_version)
            except StorageMissingDependenciesError:
                pytest.skip("Missing dependencies for azure_blob_storage")
            finally:
                # Cleanup for other tests
                _clear_azure_blob_storage_module()


class TestAzureApiVersionClientIntegration:
    """Test that API version is correctly passed to Azure clients."""

    @pytest.mark.asyncio
    async def test_api_version_passed_to_connection_string_client(self):
        """Test that API version is passed when creating client from connection string."""
        try:
            from wrapp.utils.storage_impls.azure_blob_storage import (
                AZURE_API_VERSION,
                AzureBlobClientWrapper,
            )
            from wrapp.utils.storage_interface import (
                AzureBlobAuthInfo,
                StorageAuthInfo,
            )
        except StorageMissingDependenciesError:
            pytest.skip("Missing dependencies for azure_blob_storage")

        # Create a mock for AsyncBlobServiceClient.from_connection_string
        with mock.patch("wrapp.utils.storage_impls.azure_blob_storage.AsyncBlobServiceClient") as mock_client_class:
            mock_instance = mock.MagicMock()
            mock_client_class.from_connection_string.return_value = mock_instance

            # Create auth info with connection string
            auth_info = StorageAuthInfo(
                "azure://testaccount",
                AzureBlobAuthInfo(
                    connection_string="DefaultEndpointsProtocol=https;AccountName=test;AccountKey=test==;EndpointSuffix=core.windows.net"
                ),
            )

            wrapper = AzureBlobClientWrapper(auth_info)

            with mock.patch("wrapp.utils.storage_impls.azure_blob_storage.assert_is_wrapp_thread"):
                await wrapper.get_async_instance()

            # Verify that from_connection_string was called with api_version
            call_kwargs = mock_client_class.from_connection_string.call_args
            assert "api_version" in call_kwargs.kwargs
            assert call_kwargs.kwargs["api_version"] == AZURE_API_VERSION

    @pytest.mark.asyncio
    async def test_api_version_passed_to_account_key_client(self):
        """Test that API version is passed when creating client with account key."""
        try:
            from wrapp.utils.storage_impls.azure_blob_storage import (
                AZURE_API_VERSION,
                AzureBlobClientWrapper,
            )
            from wrapp.utils.storage_interface import (
                AzureBlobAuthInfo,
                StorageAuthInfo,
            )
        except StorageMissingDependenciesError:
            pytest.skip("Missing dependencies for azure_blob_storage")

        with mock.patch("wrapp.utils.storage_impls.azure_blob_storage.AsyncBlobServiceClient") as mock_client_class:
            mock_instance = mock.MagicMock()
            mock_client_class.return_value = mock_instance

            # Create auth info with account name and key
            auth_info = StorageAuthInfo(
                "azure://testaccount",
                AzureBlobAuthInfo(account_name="testaccount", account_key="testkey=="),
            )

            wrapper = AzureBlobClientWrapper(auth_info)

            with mock.patch("wrapp.utils.storage_impls.azure_blob_storage.assert_is_wrapp_thread"):
                await wrapper.get_async_instance()

            # Verify that the client constructor was called with api_version
            call_kwargs = mock_client_class.call_args
            assert "api_version" in call_kwargs.kwargs
            assert call_kwargs.kwargs["api_version"] == AZURE_API_VERSION

    @pytest.mark.asyncio
    async def test_api_version_passed_to_sas_token_client(self):
        """Test that API version is passed when creating client with SAS token."""
        try:
            from wrapp.utils.storage_impls.azure_blob_storage import (
                AZURE_API_VERSION,
                AzureBlobClientWrapper,
            )
            from wrapp.utils.storage_interface import (
                AzureBlobAuthInfo,
                StorageAuthInfo,
            )
        except StorageMissingDependenciesError:
            pytest.skip("Missing dependencies for azure_blob_storage")

        with mock.patch("wrapp.utils.storage_impls.azure_blob_storage.AsyncBlobServiceClient") as mock_client_class:
            mock_instance = mock.MagicMock()
            mock_client_class.return_value = mock_instance

            # Create auth info with account name and SAS token
            auth_info = StorageAuthInfo(
                "azure://testaccount",
                AzureBlobAuthInfo(account_name="testaccount", sas_token="?sv=2021-08-06&ss=b&srt=sco&sp=rwdlacyx"),
            )

            wrapper = AzureBlobClientWrapper(auth_info)

            with mock.patch("wrapp.utils.storage_impls.azure_blob_storage.assert_is_wrapp_thread"):
                await wrapper.get_async_instance()

            # Verify that the client constructor was called with api_version
            call_kwargs = mock_client_class.call_args
            assert "api_version" in call_kwargs.kwargs
            assert call_kwargs.kwargs["api_version"] == AZURE_API_VERSION


if __name__ == "__main__":
    unittest.main()
