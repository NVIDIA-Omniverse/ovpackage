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
Tests for Google Cloud Storage URL parsing functionality.

These tests verify that the parse_gcs_url and is_gcs_url functions correctly handle
various URL formats for Google Cloud Storage.
"""

import unittest

import pytest

from wrapp.utils.utils import StorageMissingDependenciesError

try:
    from wrapp.utils.storage_impls.gcs_storage import (
        is_gcs_url,
        parse_gcs_url,
    )
except StorageMissingDependenciesError:
    pytest.skip("Missing dependencies for gcs_storage", allow_module_level=True)


class TestGCSUrlParsing(unittest.TestCase):
    """Test the GCS URL parsing functionality."""

    def test_gs_scheme_url_parsing(self):
        """Test parsing URLs with the gs:// scheme."""
        # Basic bucket and key
        bucket_name, object_name = parse_gcs_url("gs://my-bucket/path/to/object")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "path/to/object")

        # Bucket only
        bucket_name, object_name = parse_gcs_url("gs://my-bucket")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "")

        # Bucket with trailing slash
        bucket_name, object_name = parse_gcs_url("gs://my-bucket/")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "")

        # Deep path
        bucket_name, object_name = parse_gcs_url("gs://my-bucket/a/b/c/d/e.txt")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "a/b/c/d/e.txt")

    def test_gs_scheme_trailing_slash(self):
        """Test that trailing slashes are preserved for directory-like paths."""
        bucket_name, object_name = parse_gcs_url("gs://my-bucket/folder/")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "folder/")

        bucket_name, object_name = parse_gcs_url("gs://my-bucket/folder/subfolder/")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "folder/subfolder/")

    def test_https_scheme_url_parsing(self):
        """Test parsing URLs with the https://storage.googleapis.com scheme."""
        # Basic bucket and key
        bucket_name, object_name = parse_gcs_url("https://storage.googleapis.com/my-bucket/path/to/object")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "path/to/object")

        # Bucket only
        bucket_name, object_name = parse_gcs_url("https://storage.googleapis.com/my-bucket")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "")

        # Bucket with trailing slash
        bucket_name, object_name = parse_gcs_url("https://storage.googleapis.com/my-bucket/")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "")

    def test_https_scheme_trailing_slash(self):
        """Test that trailing slashes are preserved for https URLs."""
        bucket_name, object_name = parse_gcs_url("https://storage.googleapis.com/my-bucket/folder/")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "folder/")

    def test_invalid_urls(self):
        """Test that invalid URLs raise ValueError."""
        with self.assertRaises(ValueError):
            parse_gcs_url("s3://bucket/key")

        with self.assertRaises(ValueError):
            parse_gcs_url("file:///path/to/file")

        with self.assertRaises(ValueError):
            parse_gcs_url("http://example.com")

        with self.assertRaises(ValueError):
            parse_gcs_url("azure://account/container/blob")

        with self.assertRaises(ValueError):
            parse_gcs_url("https://example.com/path")

    def test_is_gcs_url(self):
        """Test the is_gcs_url function."""
        # Valid GCS URLs
        self.assertTrue(is_gcs_url("gs://my-bucket/object"))
        self.assertTrue(is_gcs_url("gs://my-bucket"))
        self.assertTrue(is_gcs_url("gs://my-bucket/"))
        self.assertTrue(is_gcs_url("gs://my-bucket/path/to/object"))
        self.assertTrue(is_gcs_url("https://storage.googleapis.com/my-bucket/object"))
        self.assertTrue(is_gcs_url("https://storage.googleapis.com/my-bucket"))

        # Invalid GCS URLs
        self.assertFalse(is_gcs_url("s3://bucket/key"))
        self.assertFalse(is_gcs_url("file:///path/to/file"))
        self.assertFalse(is_gcs_url("http://example.com"))
        self.assertFalse(is_gcs_url("azure://account/container/blob"))
        self.assertFalse(is_gcs_url("https://example.com/path"))

    def test_object_name_case_preservation(self):
        """Test that object name case is preserved."""
        bucket_name, object_name = parse_gcs_url("gs://my-bucket/ObjectWithUpperCase")
        self.assertEqual(object_name, "ObjectWithUpperCase")

        bucket_name, object_name = parse_gcs_url("https://storage.googleapis.com/my-bucket/ObjectWithUpperCase")
        self.assertEqual(object_name, "ObjectWithUpperCase")

    def test_empty_object_name(self):
        """Test parsing URLs with empty object names."""
        bucket_name, object_name = parse_gcs_url("gs://my-bucket/")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "")

        bucket_name, object_name = parse_gcs_url("gs://my-bucket")
        self.assertEqual(bucket_name, "my-bucket")
        self.assertEqual(object_name, "")


if __name__ == "__main__":
    unittest.main()
