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

import json
import warnings
from typing import List

import pytest
import pytest_asyncio

from tests.common import run_wrapp_in_separate_process
from wrapp.commands.catalog import catalog
from wrapp.commands.create import create
from wrapp.commands.install import install
from wrapp.datastructures.command_context import CommandParameters
from wrapp.datastructures.ignore import (
    IgnoreEvaluator,
    item_is_ignored,
)
from wrapp.utils.storage import (
    wrapp_delete_item,
    wrapp_url_exists,
    wrapp_write_file_content,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    FailedCommand,
    get_base_directory_from_url,
    join_paths,
    random_string,
)


@pytest_asyncio.fixture
async def ignore_file(local_tmp_folder, test_folder_url, wrapp_init, authenticator):
    ignore_file = join_paths(test_folder_url, f"{random_string()}.ignore")
    yield ignore_file
    await wrapp_delete_item(CommandParameters(), ignore_file)


async def ignore_rules(filename: str, rules: List[str]):
    await wrapp_write_file_content(CommandParameters(), filename, "\n".join(rules))


def test_ignore_simple():
    ignore = IgnoreEvaluator(root_path=None, predicate=lambda x: x == "ignorethis")
    assert ignore.is_ignored("ignorethis")
    assert not ignore.is_ignored("ignorethis.")
    assert not ignore.is_ignored(".ignorethis")


@pytest.mark.asyncio
async def test_ignore_path(ignore_file):
    await ignore_rules(ignore_file, ["wrapp/*", "barf/**"])
    ignores = await IgnoreEvaluator.create_from_file(ignore_file, get_base_directory_from_url(ignore_file))

    assert ignores.is_ignored("wrapp/test.png")
    assert not ignores.is_ignored("wrapp/second/test.png")
    assert ignores.is_ignored("barf/test.png")
    assert ignores.is_ignored("barf/second/test.png")


@pytest.mark.asyncio
async def test_ignore_thumbnails(ignore_file):
    await ignore_rules(ignore_file, ["*.jpg"])
    root = get_base_directory_from_url(ignore_file)
    ignores = await IgnoreEvaluator.create_from_file(ignore_file, root)

    assert not ignores.is_ignored("test.png")
    assert ignores.is_ignored("test.jpg")
    # TODO discuss case sensitivity
    assert not ignores.is_ignored("test.JPG")
    assert ignores.is_ignored("test.auto.png")

    # Also works for absolute paths
    assert not ignores.is_ignored(join_paths(root, "test.png"))
    assert ignores.is_ignored(join_paths(root, "test.jpg"))
    # TODO discuss case sensitivity
    assert not ignores.is_ignored(join_paths(root, "test.JPG"))
    assert ignores.is_ignored(join_paths(root, "test.auto.png"))

    # Compare both ignore methods, they should give the same result
    for value in ["test.png", "test.jpg", "test.JPG", "test.auto.png"]:
        assert ignores.is_ignored(value) == item_is_ignored(parent_dir="", relative_path=value, ignore_rules=ignores)
        assert ignores.is_ignored(value) == item_is_ignored(parent_dir=root, relative_path=value, ignore_rules=ignores)


@pytest.mark.asyncio
async def test_ignore_for_files_in_root_folder_for_create_OMPE67988(test_folder_url, source_package):
    repo_path = join_paths(test_folder_url, "repo")
    await create("p", "v1", source_package, False, repo=repo_path)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/p/v1/testfile.txt"))
    assert await wrapp_write_file_content(CommandParameters(), join_paths(test_folder_url, ".wrappignore"), "testfile.txt") == Result.OK
    await create("p", "v2", source_package, False, repo=repo_path, ignores=join_paths(test_folder_url, ".wrappignore"))
    assert await wrapp_write_file_content(CommandParameters(), join_paths(source_package, ".wrappignore"), "testfile.txt") == Result.OK
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/p/v2/testfile.txt"))
    await create("p", "v3", source_package, False, repo=repo_path)
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/p/v3/testfile.txt"))
    with pytest.raises(FailedCommand):
        await create("p", "v2", source_package, False, repo=repo_path, ignores=join_paths(test_folder_url, ".wrappignore2"))


# =============================================================================
# Automatic .wrappignore Pickup Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_auto_picks_up_wrappignore_from_source_root(test_folder_url, source_package):
    """Test that create automatically picks up .wrappignore from the root of the source package folder"""
    repo_path = join_paths(test_folder_url, "repo_auto_ignore")

    # First create a package without any ignore file - all files should be present
    await create("pkg", "v1", source_package, False, repo=repo_path)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))

    # Now create .wrappignore in the source_package root (NOT specifying ignore_file parameter)
    wrappignore_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "subfolder/**")

    # Create package v2 WITHOUT specifying ignore_file - should auto-pickup .wrappignore
    await create("pkg", "v2", source_package, False, repo=repo_path)

    # Verify .wrappignore was automatically picked up
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/subfolder/subsubfolder/file_in_subsubfolder.txt"))


@pytest.mark.asyncio
async def test_install_auto_picks_up_wrappignore_from_destination_root(test_folder_url, source_package):
    """Test that install automatically picks up .wrappignore from the root of the destination folder"""
    repo_path = join_paths(test_folder_url, "repo_install_auto_ignore")
    destination = join_paths(test_folder_url, "install_dest_auto_ignore")

    # Create and install initial package
    await create("pkg", "v1", source_package, False, repo=repo_path)
    await install("pkg", "v1", destination, repo=repo_path)

    # Add an extra file to destination
    extra_file = join_paths(destination, "extra_local_file.dat")
    await wrapp_write_file_content(CommandParameters(), extra_file, "local content")
    assert await wrapp_url_exists(extra_file)

    # Create .wrappignore in destination root (NOT specifying ignore_file parameter)
    wrappignore_path = join_paths(destination, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "extra_local_file.dat")

    # Create v2 and install WITHOUT specifying ignore_file - should auto-pickup .wrappignore
    await create("pkg", "v2", source_package, False, repo=repo_path)
    await install("pkg", "v2", destination, repo=repo_path)

    # Verify package files are present and extra file still exists (was ignored via auto-pickup)
    assert await wrapp_url_exists(join_paths(destination, "testfile.txt"))
    assert await wrapp_url_exists(extra_file)


@pytest.mark.asyncio
async def test_catalog_auto_picks_up_wrappignore_from_source_root(test_folder_url, source_package):
    """Test that catalog automatically picks up .wrappignore from the root of the source folder"""
    # First catalog without ignore file - all files should be present
    catalog_result_before = await catalog(root_dir=source_package)
    catalog_paths_before = [item.relative_path for item in catalog_result_before.items]
    assert "testfile.txt" in catalog_paths_before
    assert "subfolder/file_in_folder.txt" in catalog_paths_before

    # Create .wrappignore in source_package root (NOT specifying ignores parameter)
    wrappignore_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "subfolder/**")

    # Run catalog WITHOUT specifying ignores - should auto-pickup .wrappignore
    catalog_result_after = await catalog(root_dir=source_package)
    catalog_paths_after = [item.relative_path for item in catalog_result_after.items]

    # Verify .wrappignore was automatically picked up
    assert "testfile.txt" in catalog_paths_after
    assert "subfolder/file_in_folder.txt" not in catalog_paths_after
    assert "subfolder/subsubfolder/file_in_subsubfolder.txt" not in catalog_paths_after


# =============================================================================
# Explicit Ignore File Takes Precedence Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_explicit_ignores_takes_precedence_over_wrappignore(test_folder_url, source_package):
    """Test that explicitly specified ignores takes precedence over default .wrappignore"""
    repo_path = join_paths(test_folder_url, "repo_precedence")

    # Create .wrappignore in source root that ignores subfolder/**
    wrappignore_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "subfolder/**")

    # Create explicit ignore file that ignores only testfile.txt (different from .wrappignore)
    explicit_ignore_path = join_paths(test_folder_url, "explicit_ignore.txt")
    await wrapp_write_file_content(CommandParameters(), explicit_ignore_path, "testfile.txt")

    # Create package with explicit ignores - should use explicit file, NOT .wrappignore
    await create("pkg", "v1", source_package, False, repo=repo_path, ignores=explicit_ignore_path)

    # Verify explicit ignores was used: testfile.txt should be ignored, subfolder should be present
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))


# =============================================================================
# IgnoreEvaluator Support Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_with_ignore_evaluator(test_folder_url, source_package):
    """Test that create command accepts IgnoreEvaluator directly"""
    repo_path = join_paths(test_folder_url, "repo_evaluator")

    # Create an IgnoreEvaluator that ignores subfolder/**
    ignore_evaluator = IgnoreEvaluator(root_path=source_package, predicate=lambda path: "subfolder/" in path)

    # Create package with IgnoreEvaluator
    await create("pkg", "v1", source_package, False, repo=repo_path, ignores=ignore_evaluator)

    # Verify IgnoreEvaluator was used: subfolder should be ignored
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))


@pytest.mark.asyncio
async def test_catalog_with_ignore_evaluator(test_folder_url, source_package):
    """Test that catalog command accepts IgnoreEvaluator directly"""
    # Create an IgnoreEvaluator that ignores all txt files
    ignore_evaluator = IgnoreEvaluator(root_path=source_package, predicate=lambda path: path.endswith(".txt"))

    # Run catalog with IgnoreEvaluator
    catalog_result = await catalog(root_dir=source_package, ignores=ignore_evaluator)

    # Get list of relative paths in catalog
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify IgnoreEvaluator was used: txt files should be ignored
    assert "testfile.txt" not in catalog_paths
    assert "subfolder/file_in_folder.txt" not in catalog_paths
    assert "subfolder/subsubfolder/file_in_subsubfolder.txt" not in catalog_paths


@pytest.mark.asyncio
async def test_install_with_ignore_evaluator(test_folder_url, source_package):
    """Test that install command accepts IgnoreEvaluator directly"""
    repo_path = join_paths(test_folder_url, "repo_install_evaluator")
    destination = join_paths(test_folder_url, "install_dest_evaluator")

    # Create and install initial package
    await create("pkg", "v1", source_package, False, repo=repo_path)
    await install("pkg", "v1", destination, repo=repo_path)

    # Add an extra file to destination
    extra_file = join_paths(destination, "extra_local.dat")
    await wrapp_write_file_content(CommandParameters(), extra_file, "local content")

    # Create an IgnoreEvaluator that ignores the extra file
    ignore_evaluator = IgnoreEvaluator(root_path=destination, predicate=lambda path: "extra_local.dat" in path)

    # Create v2 and install with IgnoreEvaluator
    await create("pkg", "v2", source_package, False, repo=repo_path)
    await install("pkg", "v2", destination, repo=repo_path, ignores=ignore_evaluator)

    # Verify package files are present and extra file still exists (was ignored)
    assert await wrapp_url_exists(join_paths(destination, "testfile.txt"))
    assert await wrapp_url_exists(extra_file)


# =============================================================================
# External Ignores Location Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_with_ignores_from_different_location(test_folder_url, source_package):
    """Test that ignores can be specified from a different directory.

    When an external ignore file is used, patterns are resolved relative to the
    source directory being processed, NOT relative to the ignore file location.
    """
    repo_path = join_paths(test_folder_url, "repo_cli_ignore")

    # Create ignore file in a completely different location (not in source_package)
    # The pattern "subfolder/**" will match source_package/subfolder/**, not test_folder_url/subfolder/**
    ignore_file_path = join_paths(test_folder_url, "external_ignore_file.txt")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "subfolder/**")

    # Verify source package has the files
    assert await wrapp_url_exists(join_paths(source_package, "testfile.txt"))
    assert await wrapp_url_exists(join_paths(source_package, "subfolder/file_in_folder.txt"))

    # Create package with ignores from different location
    await create("pkg", "v1", source_package, False, repo=repo_path, ignores=ignore_file_path)

    # Verify root files are present but subfolder files are ignored
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))


@pytest.mark.asyncio
async def test_install_with_ignores_from_different_location(test_folder_url, source_package):
    """Test that ignores can be specified on command line from a different directory for install"""
    repo_path = join_paths(test_folder_url, "repo_cli_install_ignore")
    destination = join_paths(test_folder_url, "install_cli_ignore_dest")

    # Create and install initial package
    await create("pkg", "v1", source_package, False, repo=repo_path)
    await install("pkg", "v1", destination, repo=repo_path)

    # Add extra file to destination
    extra_file = join_paths(destination, "local_extra.dat")
    await wrapp_write_file_content(CommandParameters(), extra_file, "local content")

    # Create ignore file in a completely different location (not in destination)
    ignore_file_path = join_paths(test_folder_url, "external_install_ignore.txt")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "local_extra.dat")

    # Create v2 and install with external ignores
    await create("pkg", "v2", source_package, False, repo=repo_path)
    await install("pkg", "v2", destination, repo=repo_path, ignores=ignore_file_path)

    # Verify package files are present and extra file still exists (was ignored)
    assert await wrapp_url_exists(join_paths(destination, "testfile.txt"))
    assert await wrapp_url_exists(extra_file)


@pytest.mark.asyncio
async def test_ignore_file_bare_filename_resolves_from_cwd(source_package, tmp_path):
    """Test that a bare filename for ignore file resolves relative to current working directory.

    This test documents an important (and potentially confusing) behavior: when a user
    specifies just a filename for --ignore-file (e.g., "myignore.txt"), the file is
    looked up in the current working directory, NOT in the source data directory.

    This behavior is kept for backwards compatibility, even though it could be ambiguous
    (one might expect "myignore.txt" to look in the source directory).

    This test uses subprocess to avoid changing CWD in the test process (which would
    be unsafe with parallel test execution).
    """
    # Create an ignore file with just a simple filename in a temp directory
    ignore_filename = "myignore.txt"
    ignore_file = tmp_path / ignore_filename
    ignore_file.write_text("subfolder/**")

    # Create a catalog output file
    catalog_output = tmp_path / "catalog.json"

    # Run wrapp catalog via subprocess with CWD set to tmp_path
    # This tests the real CLI behavior: does "myignore.txt" resolve from CWD?
    run_wrapp_in_separate_process(
        ["catalog", source_package, str(catalog_output), "--ignore-file", ignore_filename],
        expected_return_codes=0,
        cwd=str(tmp_path),  # Set CWD to where the ignore file is
    )

    # Load and verify the catalog
    catalog_data = json.loads(catalog_output.read_text())
    catalog_paths = [item["relative_path"] for item in catalog_data["items"]]

    # Verify the ignore file was found in CWD and patterns applied
    assert "testfile.txt" in catalog_paths
    assert "subfolder/file_in_folder.txt" not in catalog_paths
    assert "subfolder/subsubfolder/file_in_subsubfolder.txt" not in catalog_paths


@pytest.mark.asyncio
async def test_catalog_with_ignores_from_different_location(test_folder_url, source_package):
    """Test that ignores can be specified from a different directory for catalog.

    When an external ignore file is used, patterns are resolved relative to the
    source directory being processed, NOT relative to the ignore file location.
    """
    # Create ignore file in a completely different location (not in source_package)
    # The pattern "subfolder/**" will match source_package/subfolder/**, not test_folder_url/subfolder/**
    ignore_file_path = join_paths(test_folder_url, "external_catalog_ignore.txt")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "subfolder/**")

    # Run catalog with external ignores
    catalog_result = await catalog(root_dir=source_package, ignores=ignore_file_path)

    # Get list of relative paths in catalog
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify root files are in catalog but subfolder files are ignored
    assert "testfile.txt" in catalog_paths
    assert "subfolder/file_in_folder.txt" not in catalog_paths
    assert "subfolder/subsubfolder/file_in_subsubfolder.txt" not in catalog_paths


# =============================================================================
# Create Command Tests
# =============================================================================


@pytest.mark.asyncio
async def test_create_ignores_subfolder_with_wildcards(test_folder_url, source_package):
    """Test that create command ignores subfolders matching wildcard patterns like subfolder/**"""
    repo_path = join_paths(test_folder_url, "repo_subfolder")

    # First verify the source package has the subfolder content
    assert await wrapp_url_exists(join_paths(source_package, "subfolder/file_in_folder.txt"))
    assert await wrapp_url_exists(join_paths(source_package, "subfolder/subsubfolder/file_in_subsubfolder.txt"))
    assert await wrapp_url_exists(join_paths(source_package, "testfile.txt"))

    # Create package without ignore file - all files should be present
    await create("pkg", "v1", source_package, False, repo=repo_path)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))

    # Create ignore file with subfolder/** pattern to ignore all contents of subfolder
    ignore_file_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "subfolder/**")

    # Create package with ignore file - subfolder content should be ignored
    await create("pkg", "v2", source_package, False, repo=repo_path)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/subfolder/subsubfolder/file_in_subsubfolder.txt"))


@pytest.mark.asyncio
async def test_create_with_wildcard_file_patterns(test_folder_url, source_package):
    """Test that create command ignores files matching wildcard patterns like *.txt"""
    repo_path = join_paths(test_folder_url, "repo_wildcard")

    # Verify source package has txt files
    assert await wrapp_url_exists(join_paths(source_package, "testfile.txt"))
    assert await wrapp_url_exists(join_paths(source_package, "subfolder/file_in_folder.txt"))

    # Create ignore file with *.txt pattern to ignore all txt files
    ignore_file_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "*.txt")

    # Create package - all txt files should be ignored
    await create("pkg", "v1", source_package, False, repo=repo_path)
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))


@pytest.mark.asyncio
async def test_create_with_exclusion_negation_rule(test_folder_url, source_package):
    """Test that create command respects exclusion (negation) rules like !testfile.txt"""
    repo_path = join_paths(test_folder_url, "repo_exclusion")

    # Verify source package has txt files
    assert await wrapp_url_exists(join_paths(source_package, "testfile.txt"))
    assert await wrapp_url_exists(join_paths(source_package, "subfolder/file_in_folder.txt"))

    # Create ignore file with *.txt to ignore all txt files, but !testfile.txt to exclude testfile.txt from ignore
    ignore_file_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "*.txt\n!testfile.txt")

    # Create package - testfile.txt should be included, other txt files should be ignored
    await create("pkg", "v1", source_package, False, repo=repo_path)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))


# =============================================================================
# Install Command Tests
# =============================================================================


@pytest.mark.asyncio
async def test_install_ignores_extra_files_at_destination(test_folder_url, source_package):
    """Test that install ignores extra files at destination that match ignore patterns"""
    repo_path = join_paths(test_folder_url, "repo_install_extra")
    destination = join_paths(test_folder_url, "install_dest_extra")

    # Create package v1
    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Install package v1
    await install("pkg", "v1", destination, repo=repo_path)
    assert await wrapp_url_exists(join_paths(destination, "testfile.txt"))

    # Add an extra file to destination that will match ignore pattern
    extra_file = join_paths(destination, "extra_local_file.txt")
    await wrapp_write_file_content(CommandParameters(), extra_file, "extra content")
    assert await wrapp_url_exists(extra_file)

    # Create ignore file at destination to ignore *.txt files (extra files)
    ignore_file_path = join_paths(destination, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "extra_local_file.txt")

    # Create and install v2 with ignores
    await create("pkg", "v2", source_package, False, repo=repo_path)
    # Install v2 - the extra file should be ignored (not cause conflict)
    await install("pkg", "v2", destination, repo=repo_path, ignores=ignore_file_path)

    # Verify package files are still present
    assert await wrapp_url_exists(join_paths(destination, "testfile.txt"))
    # Extra file should still exist (it was ignored during install, not deleted)
    assert await wrapp_url_exists(extra_file)


@pytest.mark.asyncio
async def test_install_does_not_ignore_package_files(test_folder_url, source_package):
    """Test that install does not ignore files that are part of the package itself"""
    repo_path = join_paths(test_folder_url, "repo_install_pkg_files")
    destination = join_paths(test_folder_url, "install_dest_pkg_files")

    # Create package containing txt files
    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Create ignore file at destination that would match package files
    ignore_file_path = join_paths(test_folder_url, "pkg_ignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "*.txt")

    # Install package with ignores - package's txt files should still be installed
    await install("pkg", "v1", destination, repo=repo_path, ignores=ignore_file_path)

    # Package files should be present despite ignore pattern
    assert await wrapp_url_exists(join_paths(destination, "testfile.txt"))
    assert await wrapp_url_exists(join_paths(destination, "subfolder/file_in_folder.txt"))


# =============================================================================
# Catalog Command Tests
# =============================================================================


@pytest.mark.asyncio
async def test_catalog_ignores_files_according_to_ignore_file(test_folder_url, source_package):
    """Test that catalog command ignores files according to ignore file patterns"""
    # Create ignore file with patterns
    ignore_file_path = join_paths(test_folder_url, "catalog_ignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "subfolder/**\n*.txt")

    # Run catalog with ignore file
    catalog_result = await catalog(root_dir=source_package, ignores=ignore_file_path)

    # Get list of relative paths in catalog
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify ignored files are not in catalog
    assert "testfile.txt" not in catalog_paths
    assert "subfolder/file_in_folder.txt" not in catalog_paths
    assert "subfolder/subsubfolder/file_in_subsubfolder.txt" not in catalog_paths


@pytest.mark.asyncio
async def test_catalog_with_exclusion_rules(test_folder_url, storage_backend):
    """Test that catalog command respects exclusion rules"""
    # Create a source with multiple txt files for testing exclusion
    source_path = join_paths(test_folder_url, "catalog_exclusion_source")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "keep.txt"), "keep this")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "ignore.txt"), "ignore this")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "also_ignore.txt"), "also ignore")

    # Create ignore file: ignore all txt except keep.txt
    ignore_file_path = join_paths(test_folder_url, "catalog_exclusion_ignore")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "*.txt\n!keep.txt")

    # Run catalog with ignore file
    catalog_result = await catalog(root_dir=source_path, ignores=ignore_file_path)

    # Get list of relative paths in catalog
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify keep.txt is in catalog but other txt files are excluded
    assert "keep.txt" in catalog_paths
    assert "ignore.txt" not in catalog_paths
    assert "also_ignore.txt" not in catalog_paths


# =============================================================================
# Ignore File Format Tests (gitignore-compatible)
# =============================================================================


@pytest.mark.asyncio
async def test_wrappignore_can_ignore_itself(test_folder_url, source_package):
    """Test that .wrappignore file can be configured to ignore itself"""
    repo_path = join_paths(test_folder_url, "repo_ignore_self")

    # Create .wrappignore in source_package root that ignores itself
    wrappignore_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ".wrappignore")

    # Create package - .wrappignore should not be included
    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Verify .wrappignore is not in the package
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/.wrappignore"))
    # But other files should still be present
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))


@pytest.mark.asyncio
async def test_ignore_file_supports_comment_lines(test_folder_url, source_package):
    """Test that ignore files support comment lines starting with #"""
    repo_path = join_paths(test_folder_url, "repo_comments")

    # Create .wrappignore with comments interspersed between actual ignore rules
    wrappignore_path = join_paths(source_package, ".wrappignore")
    ignore_content = """# Header comment explaining the ignore file
subfolder/**
# This comment is between two rules
*.log
# Another mid-file comment
# with multiple lines
.wrappignore
# Trailing comment
"""
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ignore_content)

    # Add a .log file to verify that rule works too
    await wrapp_write_file_content(CommandParameters(), join_paths(source_package, "debug.log"), "log content")

    # Create package
    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Verify comments didn't interfere with any of the rules
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/debug.log"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/.wrappignore"))


@pytest.mark.asyncio
async def test_catalog_wrappignore_not_included_by_default(test_folder_url, source_package):
    """Test that .wrappignore file is not included in catalog by default (it ignores itself)"""
    # Create .wrappignore that ignores itself
    wrappignore_path = join_paths(source_package, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ".wrappignore\nsubfolder/**")

    # Run catalog without specifying ignores - should auto-pickup .wrappignore
    catalog_result = await catalog(root_dir=source_package)

    # Get list of relative paths in catalog
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify .wrappignore is not in catalog (it ignores itself)
    assert ".wrappignore" not in catalog_paths
    # Other files should be present (except ignored ones)
    assert "testfile.txt" in catalog_paths
    assert "subfolder/file_in_folder.txt" not in catalog_paths


@pytest.mark.asyncio
async def test_negation_works_with_doublestar_pattern(test_folder_url, source_package):
    """Test that negation rules work with ** patterns (matches contents, not directory).

    When using `subfolder/**` pattern, this matches files inside the folder but not the
    folder itself. Therefore wrapp still enters the directory and evaluates each file,
    allowing negation rules to re-include specific files.
    """
    repo_path = join_paths(test_folder_url, "repo_negation_doublestar")

    # Create .wrappignore that excludes subfolder/** but negates a specific file inside it
    wrappignore_path = join_paths(source_package, ".wrappignore")
    ignore_content = """subfolder/**
!subfolder/file_in_folder.txt
"""
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ignore_content)

    # Create package
    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Verify: With ** pattern, negation works - file_in_folder.txt is included
    # while other files in subfolder remain excluded
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    # But deeper nested files are still excluded
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))


@pytest.mark.asyncio
async def test_negation_does_not_work_with_directory_pattern(test_folder_url, source_package):
    """Test that negation rules cannot re-include files when the directory itself is excluded.

    When a directory is excluded with a trailing slash pattern like `subfolder/`,
    wrapp skips traversing that directory entirely for performance. This means
    negation rules cannot re-include files inside it - matching gitignore behavior.
    """
    repo_path = join_paths(test_folder_url, "repo_negation_directory")

    # Create .wrappignore that excludes subfolder/ (the directory itself) and tries to negate a file
    wrappignore_path = join_paths(source_package, ".wrappignore")
    ignore_content = """subfolder/
!subfolder/file_in_folder.txt
"""
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ignore_content)

    # Create package
    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Verify: When directory itself is excluded, negation cannot bring back files inside
    # because wrapp skips traversing the excluded directory (performance optimization)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))


@pytest.mark.asyncio
async def test_negation_subfolder_requires_re_including_parent(test_folder_url, source_package):
    """Test that re-including a file from a subfolder requires re-including the subfolder too.

    When using `subfolder/**` to exclude all contents, you can negate files directly
    in `subfolder/`. But to negate files in `subfolder/subsubfolder/`, you must also
    re-include the subsubfolder directory itself (without trailing slash), otherwise
    the directory traversal will skip the subfolder entirely.
    """
    repo_path = join_paths(test_folder_url, "repo_negation_subfolder")

    # First test: Try to re-include a file from subsubfolder WITHOUT re-including the subfolder
    wrappignore_path = join_paths(source_package, ".wrappignore")
    ignore_content_without_subfolder = """subfolder/**
!subfolder/subsubfolder/file_in_subsubfolder.txt
"""
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ignore_content_without_subfolder)

    await create("pkg", "v1", source_package, False, repo=repo_path)

    # Verify: The file is NOT included because subsubfolder/ itself is still excluded
    # (the directory traversal skips it before checking files inside)
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/subsubfolder/file_in_subsubfolder.txt"))

    # Second test: Re-include the subfolder (without trailing slash!) AND the file
    # Note: !subfolder/subsubfolder (no trailing slash) un-ignores the directory
    #       !subfolder/subsubfolder/ (with trailing slash) does NOT work
    ignore_content_with_subfolder = """subfolder/**
!subfolder/subsubfolder
!subfolder/subsubfolder/file_in_subsubfolder.txt
"""
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, ignore_content_with_subfolder)

    await create("pkg", "v2", source_package, False, repo=repo_path)

    # Verify: Now the file IS included because we re-included the subfolder too
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/subfolder/file_in_folder.txt"))
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v2/subfolder/subsubfolder/file_in_subsubfolder.txt"))


# =============================================================================
# Pattern Matching Scope Tests (gitignore path separator behavior)
# =============================================================================


@pytest.mark.asyncio
async def test_pattern_without_separator_matches_at_any_level(test_folder_url, storage_backend):
    """Test that patterns without path separator match at any directory level.

    Per gitignore docs: "If there is a separator at the beginning or middle (or both)
    of the pattern, then the pattern is relative to the directory level of the particular
    .gitignore file itself. Otherwise the pattern may also match at any level below."

    So `target/` (no separator at beginning/middle) matches directories named "target"
    at any depth in the tree.
    """
    # Create a source with nested directories that have the same name
    source_path = join_paths(test_folder_url, "source_any_level")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "root_file.txt"), "root")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "target/file.txt"), "in target at root")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "other/target/file.txt"), "in nested target")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "other/keep.txt"), "keep this")

    # Create .wrappignore with pattern that has NO separator at beginning/middle
    wrappignore_path = join_paths(source_path, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "target/")

    # Run catalog
    catalog_result = await catalog(root_dir=source_path)
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify: "target/" matches directories named "target" at ANY level
    assert "root_file.txt" in catalog_paths
    assert "other/keep.txt" in catalog_paths
    # Both "target" directories should be excluded (at root level AND nested)
    assert "target/file.txt" not in catalog_paths
    assert "other/target/file.txt" not in catalog_paths


@pytest.mark.asyncio
async def test_pattern_with_separator_matches_only_at_root_level(test_folder_url, storage_backend):
    """Test that patterns with path separator only match relative to .gitignore location.

    Per gitignore docs: "If there is a separator at the beginning or middle (or both)
    of the pattern, then the pattern is relative to the directory level of the particular
    .gitignore file itself."

    So `target/**` (has separator in middle) only matches a "target" directory at the
    root level, not nested ones.
    """
    # Create a source with nested directories that have the same name
    source_path = join_paths(test_folder_url, "source_root_only")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "root_file.txt"), "root")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "target/file.txt"), "in target at root")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "other/target/file.txt"), "in nested target")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "other/keep.txt"), "keep this")

    # Create .wrappignore with pattern that HAS separator in middle
    wrappignore_path = join_paths(source_path, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "target/**")

    # Run catalog
    catalog_result = await catalog(root_dir=source_path)
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify: "target/**" only matches at root level because of the separator
    assert "root_file.txt" in catalog_paths
    assert "other/keep.txt" in catalog_paths
    # Root-level "target" contents should be excluded
    assert "target/file.txt" not in catalog_paths
    # But nested "other/target" should NOT be excluded (pattern is relative to root)
    assert "other/target/file.txt" in catalog_paths


@pytest.mark.asyncio
async def test_pattern_with_leading_slash_matches_only_at_root(test_folder_url, storage_backend):
    """Test that patterns starting with / only match at the root level.

    Per gitignore docs: "A leading slash matches the beginning of the pathname."
    So `/target/` only matches a "target" directory at the root, not nested ones.
    """
    # Create a source with nested directories that have the same name
    source_path = join_paths(test_folder_url, "source_leading_slash")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "root_file.txt"), "root")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "target/file.txt"), "in target at root")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "other/target/file.txt"), "in nested target")
    await wrapp_write_file_content(CommandParameters(), join_paths(source_path, "other/keep.txt"), "keep this")

    # Create .wrappignore with leading slash pattern
    wrappignore_path = join_paths(source_path, ".wrappignore")
    await wrapp_write_file_content(CommandParameters(), wrappignore_path, "/target/")

    # Run catalog
    catalog_result = await catalog(root_dir=source_path)
    catalog_paths = [item.relative_path for item in catalog_result.items]

    # Verify: "/target/" only matches at root level
    assert "root_file.txt" in catalog_paths
    assert "other/keep.txt" in catalog_paths
    # Root-level "target" should be excluded
    assert "target/file.txt" not in catalog_paths
    # But nested "other/target" should NOT be excluded
    assert "other/target/file.txt" in catalog_paths


# =============================================================================
# Deprecated ignore_file Parameter Tests
# =============================================================================


@pytest.mark.asyncio
async def test_deprecated_ignore_file_parameter_emits_warning(test_folder_url, source_package):
    """Test that using the deprecated ignore_file parameter emits a deprecation warning."""
    repo_path = join_paths(test_folder_url, "repo_deprecated_warning")

    # Create an ignore file
    ignore_file_path = join_paths(test_folder_url, "deprecated_test_ignore.txt")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "subfolder/**")

    # Using ignore_file= should emit a DeprecationWarning
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        await create("pkg", "v1", source_package, False, repo=repo_path, ignore_file=ignore_file_path)

        # Filter for our specific deprecation warning (there may be other warnings)
        deprecation_warnings = [
            warning for warning in w if issubclass(warning.category, DeprecationWarning) and "ignore_file" in str(warning.message)
        ]

        # Verify exactly one deprecation warning about ignore_file was emitted
        assert len(deprecation_warnings) == 1
        assert "deprecated" in str(deprecation_warnings[0].message).lower()


@pytest.mark.asyncio
async def test_deprecated_ignore_file_parameter_still_works(test_folder_url, source_package):
    """Test that the deprecated ignore_file parameter still functions correctly."""
    repo_path = join_paths(test_folder_url, "repo_deprecated_works")

    # Create an ignore file that ignores subfolder
    ignore_file_path = join_paths(test_folder_url, "deprecated_working_ignore.txt")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "subfolder/**")

    # Suppress the deprecation warning for this test
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        await create("pkg", "v1", source_package, False, repo=repo_path, ignore_file=ignore_file_path)

    # Verify the ignore file was applied - subfolder should be ignored
    assert await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/testfile.txt"))
    assert not await wrapp_url_exists(join_paths(repo_path, ".packages/pkg/v1/subfolder/file_in_folder.txt"))


@pytest.mark.asyncio
async def test_cannot_specify_both_ignores_and_ignore_file(test_folder_url, source_package):
    """Test that specifying both ignores and ignore_file raises an error."""
    # Use unique repo and version to avoid "Package already exists" error from previous runs
    repo_path = join_paths(test_folder_url, f"repo_both_error_{random_string()}")

    # Create an ignore file
    ignore_file_path = join_paths(test_folder_url, "both_test_ignore.txt")
    await wrapp_write_file_content(CommandParameters(), ignore_file_path, "*.txt")

    # Suppress the deprecation warning to focus on the FailedCommand error
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        with pytest.raises(FailedCommand, match="Cannot specify both"):
            await create("pkg", "v1", source_package, False, repo=repo_path, ignores=ignore_file_path, ignore_file=ignore_file_path)


@pytest.mark.asyncio
async def test_create_from_file_handles_access_denied_for_default_wrappignore(test_folder_url):
    """
    Test that IgnoreEvaluator.create_from_file returns None when checking for the default
    .wrappignore file fails with a storage error (e.g., CloudFront returns 403 for stat
    operations on non-existent files).

    This is a regression test for OMPE-77850.
    """
    from unittest.mock import (
        AsyncMock,
        patch,
    )

    from wrapp.utils.utils import StorageOperationError

    # Mock wrapp_url_exists to raise StorageOperationError (simulating CloudFront behavior)
    with patch("wrapp.datastructures.ignore.wrapp_url_exists", new_callable=AsyncMock) as mock_exists:
        mock_exists.side_effect = StorageOperationError("storage error trying to stat: Result.ERROR_ACCESS_DENIED")

        # When ignore_file is None, create_from_file should check for .wrappignore
        # and return None if it can't determine whether it exists
        result = await IgnoreEvaluator.create_from_file(None, test_folder_url)

        assert result is None
        mock_exists.assert_called_once()


@pytest.mark.asyncio
async def test_create_from_file_raises_for_explicit_ignore_file_that_fails(test_folder_url):
    """
    Test that IgnoreEvaluator.create_from_file raises FailedCommand when an explicitly
    specified ignore file cannot be read, even if the storage error is access denied.

    This ensures we only suppress errors for the optional default .wrappignore check,
    not for explicitly specified ignore files.
    """
    # Try to read a non-existent ignore file that the user explicitly specified
    non_existent_ignore = join_paths(test_folder_url, "this_file_does_not_exist.ignore")

    with pytest.raises(FailedCommand, match="Failed to read ignore file"):
        await IgnoreEvaluator.create_from_file(non_existent_ignore, test_folder_url)
