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

import os
import re

import pytest

import wrapp.commands
from tests.assert_backwards_forwards_compatibility import (
    assert_backwards_forwards_compatibility,
    previous_version_class_name_regex_str,
)
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.hash_cache import HashCacheFile
from wrapp.datastructures.package import (
    Dependency,
    DependencySet,
    PackageInfo,
)
from wrapp.datastructures.patch import PatchOpList
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    all_old_wrapp_file_model_classes,
)


@pytest.mark.parametrize(
    "test_class",
    [
        Catalog,
        Dependency,
        DependencySet,
        HashCacheFile,
        PatchOpList,
        PackageInfo,
        # CollectedReferences, This has not been in the hands of users yet - and right now it is hard to produce data for it (would have to re-enable the verify/collect functionality)
    ],
)
@pytest.mark.asyncio
async def test_file_format_compatibility(wrapp_init, local_tmp_folder, test_class):
    # Make sure all classes for old versions have names matching the expected regex in _get_model_schema
    old_version_class_name_regex = re.compile(previous_version_class_name_regex_str)
    for k, old_version_class in all_old_wrapp_file_model_classes.items():
        name, version = k
        old_version_class_name = old_version_class.__name__
        # classes for importing data before WRAPPFileModel
        if not issubclass(old_version_class, WRAPPFileModel):
            continue
        assert old_version_class_name_regex.match(
            old_version_class_name
        ), f"Class for backwards compatibility {old_version_class_name} must follow the naming scheme Class_vMajor_Hash. In this case it should be {name}_v{version.major()}_{version.minor()}"
    await assert_backwards_forwards_compatibility(test_class, local_tmp_folder)


@pytest.mark.asyncio
async def test_empty_folder_handling(wrapp_init, local_tmp_folder):
    # WRAPP 1.x supported empty folders in the catalog.
    # WRAPP 2.0.0-pre1 created .empty dummy files for each folder in the catalog, but had a bug when installing the
    #   WRAPP 1.x packages which lead to the .empty dummy files turning into no-longer folders (the source folder was copied
    #   into the ".empty" path).
    # WRAPP 2.0.0-a1 creates .wrapp_keep_empty_folder files in empty folders only for WRAPP 1.x packages.
    # This tests that this all works as expected.
    test_data_path = os.path.join(os.path.dirname(__file__), "test_data/empty_folder_compatibility")
    source_folder = os.path.join(test_data_path, "source")
    # We cannot add the expected empty folders in gitlab, so re-add them here
    os.makedirs(os.path.join(source_folder, "empty_1"), exist_ok=True)
    os.makedirs(os.path.join(source_folder, "empty_2", "empty_2_1"), exist_ok=True)
    os.makedirs(os.path.join(source_folder, "f", "f_empty"), exist_ok=True)

    src_repo_path = os.path.join(test_data_path, "repo")
    dst_repo_path = os.path.join(local_tmp_folder, "dstrepo")

    def verify_installed_content(folder: str, empty_folders_expected: bool, dummy_files_expected: bool):
        assert os.path.isdir(folder)
        if empty_folders_expected:
            expected_folders = {"empty_1", "empty_2", "f", "f_2", "empty_2/empty_2_1", "f/f_empty", "f_2/f_2_1"}
        else:
            expected_folders = {"f", "f_2", "f_2/f_2_1"}

        if dummy_files_expected:
            expected_files = {
                "X.txt",
                "empty_1/.wrapp_preserve_empty_folder",
                "f/f_X.txt",
                "empty_2/empty_2_1/.wrapp_preserve_empty_folder",
                "f/f_empty/.wrapp_preserve_empty_folder",
                "f_2/f_2_1/f_2_1_X.txt",
            }
        else:
            expected_files = {"X.txt", "f/f_X.txt", "f_2/f_2_1/f_2_1_X.txt"}

        # Get actual folders and files
        actual_folders = set()
        actual_files = set()
        for root, dirs, files in os.walk(folder):
            rel_path = os.path.relpath(root, folder)
            if rel_path == ".":
                rel_path = ""
            for d in dirs:
                actual_folders.add(os.path.join(rel_path, d).replace("\\", "/"))
            for f in files:
                # Ignore the .wrapp file
                if not f.endswith(".wrapp"):
                    actual_files.add(os.path.join(rel_path, f).replace("\\", "/"))

        # Verify folders match expected
        assert actual_folders == expected_folders, f"Folders don't match. Expected: {expected_folders}, Got: {actual_folders}"

        # Verify files match expected
        assert actual_files == expected_files, f"Files don't match. Expected: {expected_files}, Got: {actual_files}"

    verify_installed_content(source_folder, True, False)

    # Check creation/installation from source - no empty folders are taken into account
    installed_folder_from_source = os.path.join(local_tmp_folder, "install_from_source")
    await wrapp.commands.create("empty_folders", "2.0.0-a1_from_source", source_folder, False, repo=dst_repo_path)
    await wrapp.commands.install("empty_folders", "2.0.0-a1_from_source", installed_folder_from_source, repo=dst_repo_path)
    await wrapp.commands.install("empty_folders", "2.0.0-a1_from_source", installed_folder_from_source, repo=dst_repo_path)
    verify_installed_content(installed_folder_from_source, False, False)

    # Check installation/creation/installation from WRAPP 1.1.2 package - empty folders stored in the package are recreated with dummy files
    installed_folder_from_1_1_2 = os.path.join(local_tmp_folder, "install_from_1.1.2")
    await wrapp.commands.install("empty_folders", "1.1.2", installed_folder_from_1_1_2, repo=src_repo_path)
    verify_installed_content(installed_folder_from_1_1_2, True, True)
    await wrapp.commands.create("empty_folders", "2.0.0-a1_from_1.1.2", installed_folder_from_1_1_2, False, repo=dst_repo_path)
    await wrapp.commands.install("empty_folders", "2.0.0-a1_from_1.1.2", installed_folder_from_1_1_2, repo=dst_repo_path)
    verify_installed_content(installed_folder_from_1_1_2, True, True)
    # Repeated install should work without conflicts.
    await wrapp.commands.install("empty_folders", "2.0.0-a1_from_1.1.2", installed_folder_from_1_1_2, repo=dst_repo_path)
    verify_installed_content(installed_folder_from_1_1_2, True, True)

    # Check installation/creation/installation from WRAPP 2.0.0-pre1 package created from 1.1.2 package.
    # WRAPP 2.0.0-pre1 lost the useful .empty files, but instead created plenty of .empty folders, with the contents
    # of the original folder duplicated.
    # Those extraneous .empty folders are removed, and the original empty folders are lost.
    installed_folder_from_2_0_0_pre1 = os.path.join(local_tmp_folder, "install_from_2.0.0-pre1")
    await wrapp.commands.install("empty_folders", "2.0.0-pre1", installed_folder_from_2_0_0_pre1, repo=src_repo_path)
    verify_installed_content(installed_folder_from_2_0_0_pre1, False, False)
    await wrapp.commands.create("empty_folders", "2.0.0-a1_from_2.0.0-pre1", installed_folder_from_2_0_0_pre1, False, repo=dst_repo_path)
    await wrapp.commands.install("empty_folders", "2.0.0-a1_from_2.0.0-pre1", installed_folder_from_2_0_0_pre1, repo=dst_repo_path)
    verify_installed_content(installed_folder_from_2_0_0_pre1, False, False)
    # Repeated install should work without conflicts.
    await wrapp.commands.install("empty_folders", "2.0.0-a1_from_1.1.2", installed_folder_from_1_1_2, repo=dst_repo_path)
    verify_installed_content(installed_folder_from_1_1_2, True, True)
