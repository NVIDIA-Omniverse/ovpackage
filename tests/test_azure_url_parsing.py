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

"""
Tests for Azure URL parsing functionality.

These tests verify that the parse_azure_url function correctly handles
URLs with uppercase container names by converting them to lowercase.
"""

import unittest

import pytest

from wrapp.utils.utils import StorageMissingDependenciesError

try:
    from wrapp.utils.storage_impls.azure_blob_storage import (
        parse_azure_url,
    )
except StorageMissingDependenciesError:
    pytest.skip("Missing dependencies for azure_blob_storage", allow_module_level=True)


class TestAzureUrlParsing(unittest.TestCase):
    """Test the Azure URL parsing functionality."""

    def test_azure_scheme_url_parsing(self):
        """Test parsing URLs with the azure:// scheme."""
        # Test with lowercase container name
        account_name, container_name, blob_name = parse_azure_url("azure://account/container/blob")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")
        self.assertEqual(blob_name, "blob")

        # Test with uppercase container name - should be converted to lowercase
        account_name, container_name, blob_name = parse_azure_url("azure://account/CONTAINER/blob")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")  # Should be lowercase
        self.assertEqual(blob_name, "blob")

        # Test with mixed case container name - should be converted to lowercase
        account_name, container_name, blob_name = parse_azure_url("azure://account/ConTaiNer/blob")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")  # Should be lowercase
        self.assertEqual(blob_name, "blob")

        # Make sure it keeps a trailing slash - important for deleting folders
        account_name, container_name, blob_name = parse_azure_url("azure://account/ConTaiNer/blobfolder/")
        self.assertEqual(blob_name, "blobfolder/")

    def test_https_scheme_url_parsing(self):
        """Test parsing URLs with the https:// scheme."""
        # Test with lowercase container name
        account_name, container_name, blob_name = parse_azure_url("https://account.blob.core.windows.net/container/blob")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")
        self.assertEqual(blob_name, "blob")

        # Test with uppercase container name - should be converted to lowercase
        account_name, container_name, blob_name = parse_azure_url("https://account.blob.core.windows.net/CONTAINER/blob")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")  # Should be lowercase
        self.assertEqual(blob_name, "blob")

        # Test with mixed case container name - should be converted to lowercase
        account_name, container_name, blob_name = parse_azure_url("https://account.blob.core.windows.net/ConTaiNer/blob")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")  # Should be lowercase
        self.assertEqual(blob_name, "blob")

        # Make sure it keeps a trailing slash - important for deleting folders
        account_name, container_name, blob_name = parse_azure_url("https://account.blob.core.windows.net/ConTaiNer/blobfolder/")
        self.assertEqual(blob_name, "blobfolder/")

    def test_blob_name_case_preservation(self):
        """Test that blob name case is preserved."""
        # Blob name should preserve its case
        account_name, container_name, blob_name = parse_azure_url("azure://account/container/BlobWithUpperCase")
        self.assertEqual(blob_name, "BlobWithUpperCase")

        account_name, container_name, blob_name = parse_azure_url("https://account.blob.core.windows.net/container/BlobWithUpperCase")
        self.assertEqual(blob_name, "BlobWithUpperCase")

    def test_empty_container_and_blob(self):
        """Test parsing URLs with empty container or blob names."""
        # Empty container name - Note: The current implementation treats this as container="blob"
        account_name, container_name, blob_name = parse_azure_url("azure://account//blob")
        self.assertEqual(account_name, "account")
        # In the current implementation, the container name is "blob" and blob_name is empty
        # This is because the path is split on the first "/" after stripping
        self.assertEqual(container_name, "blob")
        self.assertEqual(blob_name, "")

        # Empty blob name
        account_name, container_name, blob_name = parse_azure_url("azure://account/container/")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "container")
        self.assertEqual(blob_name, "")

        # Empty container and blob name
        account_name, container_name, blob_name = parse_azure_url("azure://account/")
        self.assertEqual(account_name, "account")
        self.assertEqual(container_name, "")
        self.assertEqual(blob_name, "")


if __name__ == "__main__":
    unittest.main()
