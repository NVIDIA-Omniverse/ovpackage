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
Tests for the Azure Blob Storage metadata sanitization.

These tests verify that the metadata sanitization function correctly handles
invalid characters in metadata keys and values.
"""

import unittest

import pytest

from wrapp.utils.utils import StorageMissingDependenciesError

try:
    from wrapp.utils.storage_impls.azure_blob_storage import (
        sanitize_azure_metadata,
    )
except StorageMissingDependenciesError:
    pytest.skip("Missing dependencies for azure_blob_storage", allow_module_level=True)


class TestAzureMetadataSanitization(unittest.TestCase):
    """Test the Azure Blob Storage metadata sanitization function."""

    def test_valid_metadata(self):
        """Test that valid metadata is not modified."""
        metadata = {"key1": "value1", "key2": "value2", "key_with_underscore": "value_with_underscore"}

        sanitized = sanitize_azure_metadata(metadata)

        self.assertEqual(sanitized, metadata)

    def test_invalid_key_characters(self):
        """Test that invalid characters in keys are replaced."""
        metadata = {
            "key-with-hyphens": "value1",
            "key.with.dots": "value2",
            "key with spaces": "value3",
            "key@with@special#chars": "value4",
        }

        sanitized = sanitize_azure_metadata(metadata)

        self.assertEqual(
            sanitized,
            {"key_with_hyphens": "value1", "key_with_dots": "value2", "key_with_spaces": "value3", "key_with_special_chars": "value4"},
        )

    def test_invalid_value_characters(self):
        """Test that invalid characters in values are replaced."""
        metadata = {
            "key1": "value with spaces",
            "key2": "value-with-hyphens",
            "key3": "value.with.dots",
            "key4": "value@with@special#chars",
            "key5": "value_with_unicode_\u00E9\u00F1\u00FC",  # é, ñ, ü
        }

        sanitized = sanitize_azure_metadata(metadata)

        self.assertEqual(
            sanitized,
            {
                "key1": "value with spaces",
                "key2": "value-with-hyphens",
                "key3": "value.with.dots",
                "key4": "value@with@special#chars",
                "key5": "value_with_unicode____",  # Unicode chars replaced with underscores
            },
        )

    def test_none_values(self):
        """Test that None values are handled correctly."""
        metadata = {"key1": None, "key2": "value2"}

        sanitized = sanitize_azure_metadata(metadata)

        self.assertEqual(sanitized, {"key1": "", "key2": "value2"})

    def test_non_string_values(self):
        """Test that non-string values are converted to strings."""
        metadata = {"key1": 123, "key2": 3.14, "key3": True}

        sanitized = sanitize_azure_metadata(metadata)

        self.assertEqual(sanitized, {"key1": "123", "key2": "3.14", "key3": "True"})


if __name__ == "__main__":
    unittest.main()
