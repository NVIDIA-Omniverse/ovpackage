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
from typing import (
    Dict,
    List,
)

import pytest

import wrapp
from wrapp.cli import *
from wrapp.utils.storage import Router
from wrapp.utils.storage_interface import DirectorySemantics
from wrapp.utils.utils import random_string

from .common import (
    join_paths,
    run_wrapp,
)


@pytest.fixture
def local_catalog_file_name(local_tmp_folder):
    return join_paths(local_tmp_folder, random_string(8) + ".json")


@pytest.fixture
def local_output_file(local_tmp_folder):
    return os.path.join(local_tmp_folder, random_string())


def insert_variables(command_line: List[str], values: Dict):
    new_command_line: List[str] = []
    for c in command_line:
        new_command_line.append(c.format(**values))
    assert not any(["{" in new_command_line])  # Make sure all variables were specified and no {} are left after format()
    return new_command_line


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["{created_source}"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (["{source_dir}"], 1),
    ],
)
def test_cat_parameters(command_line, expected_exit_code, authenticator, test_folder_url, local_tmp_folder, cli_runner, source_package):
    run_wrapp(
        cli_runner,
        ["cat"]
        + insert_variables(command_line, {"created_source": join_paths(source_package, "testfile.txt"), "source_dir": source_package}),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["{created_source}", "{catalog_file}"],
            0,
        ),
        (
            ["{file_list}", "{catalog_file}", "--file-list", "--root", "created_source"],
            0,
        ),  # No root given, file list is ambiguous
        (
            ["{file_list}", "{catalog_file}", "--file-list"],
            1,
        ),  # No root given, file list is ambiguous
        (["{file_list}", "{catalog_file}"], 1),  # Source directory must be folder, not a file
    ],
)
@pytest.mark.asyncio
async def test_catalog_failures(
    command_line: List[str], expected_exit_code: int, test_folder_url, file_list_file, local_tmp_folder, authenticator, cli_runner
):
    catalog_file = join_paths(local_tmp_folder, "catalog.json")
    run_wrapp(
        cli_runner,
        ["catalog"]
        + insert_variables(command_line, {"created_source": test_folder_url, "file_list": file_list_file, "catalog_file": catalog_file}),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["{created_source}", "{catalog_file}", "--verbose", "--overwrite"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (["--file-list", "{created_source}", "{catalog_file}"], 1),
    ],
)
@pytest.mark.asyncio
async def test_catalog_missing_parameters(
    command_line, expected_exit_code, authenticator, test_folder_url, local_tmp_folder, cli_runner, source_package
):
    run_wrapp(
        cli_runner,
        ["catalog"]
        + insert_variables(
            command_line, {"created_source": source_package, "catalog_file": join_paths(local_tmp_folder, "catalog_missing.json")}
        ),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["{catalog_file}", "--verbose"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (["{local_tmp_folder}"], 1),
    ],
)
@pytest.mark.asyncio
async def test_count_parameters(
    command_line, expected_exit_code, authenticator, test_folder_url, local_tmp_folder, local_catalog_file_name, cli_runner, source_package
):
    catalog_content = await wrapp.catalog(root_dir=source_package)
    await catalog_content.save(wrapp.CommandParameters(), local_catalog_file_name)
    run_wrapp(
        cli_runner,
        ["count"] + insert_variables(command_line, {"catalog_file": local_catalog_file_name, "local_tmp_folder": local_tmp_folder}),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["package_name", "version", "{created_source}", "--repo", "{remote_folder}", "--verbose"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        ([], 1),
        (["package_name"], 1),
        (["package_name", "version"], 1),
        (["package_name", "version", "{created_source}", "--catalog"], 1),
        (["package_name", "version", "--catalog", "{created_source}"], 1),
        (["package_name", "version", "--new-version", "1.0", "{created_source}"], 1),
        (["package_name", "--package", "omniverse", "1.0", "{created_source}"], 1),
        (["package_name", "--package", "omniverse", "1.0", "--catalog"], 1),
        (["--package", "omniverse", "--catalog"], 1),
        (["package_name", "version", "{session_test_folder_url}/source_not_found", "--repo", "{remote_folder}"], 1),
        (
            [
                "package_name",
                "version",
                "{created_source}",
                "--repo",
                "{remote_folder}",
                "--package",
                "{session_test_folder_url}/not_found",
            ],
            1,
        ),
    ],
)
def test_create_missing_parameters(
    command_line, expected_exit_code, authenticator, source_package, test_folder_url, session_test_folder_url, cli_runner
):
    run_wrapp(
        cli_runner,
        ["create", "--repo", test_folder_url]
        + insert_variables(
            command_line,
            {"created_source": source_package, "remote_folder": test_folder_url, "session_test_folder_url": session_test_folder_url},
        ),
        expected_exit_code,
    )


@pytest.fixture
def local_output_existing(local_tmp_folder):
    # Create a file that blocks commands from overwriting it
    file_exists = join_paths(local_tmp_folder, "existing_file")
    with open(file_exists, "wt") as file:
        file.write("I exist\n")
    return file_exists


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["mypackage", "myversion", "--repo", "{remote_folder}", "--verbose", "--output", "{local_output}", "--pr", "off"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (["mypackage", "myversion", "--repo", "{remote_folder}", "--verbose", "--output", "{local_output_existing}", "--pr", "off"], 1),
        (["package_not_existing", "myversion", "--repo", "{remote_folder}"], 1),
        (["mypackage", "version_not_existing", "--repo", "{remote_folder}"], 1),
        (["mypackage/myversion", "subfolder", "--repo", "{remote_folder}"], 1),  # That's a hack! Coax into .wrapp file not existing
        (["mypackage", "myversion", "--repo", "{local_folder}"], 1),  # Wrong repo
        (["mypackage", "myversion", "--catalog", "{local_folder}"], 1),  # Catalog file is a folder
        (
            ["mypackage", "myversion", "--catalog", "{local_folder}", "--repo", "{remote_folder}"],
            1,
        ),  # Don't specify both catalog and repo
    ],
)
@pytest.mark.asyncio
async def test_export_missing_parameters(
    command_line, expected_exit_code, authenticator, source_package, test_folder_url, cli_runner, local_tmp_folder, local_output_existing
):
    package_name = "mypackage"
    version = "myversion"
    await wrapp.create(package_name, version, source_package, repo=test_folder_url, catalog=False)
    run_wrapp(
        cli_runner,
        ["export"]
        + insert_variables(
            command_line,
            {
                "remote_folder": test_folder_url,
                "created_source": source_package,
                "local_output": join_paths(local_tmp_folder, "exported.tar"),
                "local_output_existing": local_output_existing,
                "local_folder": local_tmp_folder,
            },
        ),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["package", "1.0", "{catalog_file}", "--repo", "{test_folder}"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (["package", "1.0", "{local_tmp_folder}", "--repo", "{test_folder}"], 1),
    ],
)
@pytest.mark.asyncio
async def test_extract_catalog_parameters(
    command_line, expected_exit_code, authenticator, test_folder_url, local_tmp_folder, local_catalog_file_name, cli_runner, source_package
):
    await wrapp.create(package_name="package", version="1.0", source=source_package, catalog=False, repo=test_folder_url)
    run_wrapp(
        cli_runner,
        ["extract-catalog"]
        + insert_variables(
            command_line, {"catalog_file": local_catalog_file_name, "local_tmp_folder": local_tmp_folder, "test_folder": test_folder_url}
        ),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (["package", "1.0", "--sr", "{source_folder}", "--dr", "{destination_folder}"], 0),
        # First test that this command works, before all other variants should fail with exit code 1
        (
            ["package_not_found", "1.0", "--sr", "{source_folder}/no_packages", "--dr", "{destination_folder}"],
            1,
        ),  # .packages not found here
        (["package_not_found", "1.0", "--sr", "{source_folder}", "--dr", "{destination_folder}"], 1),  # package does not exist
        (
            [
                "package",
                "1.0",
                "--sr",
                "{source_folder}",
                "--dr",
                "{destination_folder}",
                "--template-version",
                "2.0",
            ],
            1,
        ),  # template version does not exist
        (
            ["package", "1.0", "--sr", "{source_folder}", "--dr", "{source_folder}/.packages/package/1.0/.package.wrapp"],
            2,
        ),  # Target exists and is not a folder. TODO: This is an interesting test as it throws a task exception was never retrieved
    ],
)
@pytest.mark.asyncio
async def test_mirror_parameters(command_line, expected_exit_code, authenticator, test_folder_url, cli_runner, caplog, storage_backend):
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "source"))
    await wrapp.create("package", "1.0", source_package, catalog=False, repo=test_folder_url)
    target_folder = join_paths(test_folder_url, "destination")
    # The very last example does work differently on a storage with prefix semantics.
    if Router.me().storage_for_read(test_folder_url).directory_semantics() == DirectorySemantics.PREFIX:
        pytest.skip(f"Cannot run this test on a storage wíth prefix directory semantics")
    run_wrapp(
        cli_runner,
        ["mirror"]
        + insert_variables(
            command_line,
            {
                "source_folder": test_folder_url,
                "destination_folder": target_folder,
            },
        ),
        expected_exit_code,
    )
    if expected_exit_code == 1:
        assert any(["Failure to mirror package" in message for message in caplog.messages])


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["package", "{install_location}"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (
            ["package", "{install_location}", "--package", "{install_location}/.package_not_found.wrapp"],
            1,
        ),  # Fails to find the package file
        (
            [
                "package",
                "{install_location}",
                "--package",
                "{install_location}/.package.wrapp",
            ],
            1,
        ),  # Fails to find the dependency inside the package file
        (
            [
                "package",
                "{install_location}/not_found",
            ],
            1,
        ),  # Fail because there is no package to uninstall
    ],
)
@pytest.mark.asyncio
async def test_uninstall_parameters(command_line, expected_exit_code, authenticator, test_folder_url, cli_runner, source_package):
    await wrapp.create(package_name="package", version="1.0", source=source_package, catalog=False, repo=test_folder_url)
    install_location = join_paths(test_folder_url, "install_location")
    await wrapp.install("package", "1.0", destination=install_location, tags=False, repo=test_folder_url)
    run_wrapp(
        cli_runner,
        ["uninstall"] + insert_variables(command_line, {"install_location": install_location, "remote": test_folder_url}),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["--package", "{parent_package}", "--repo", "{test_folder}"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        (
            ["--package", "{parent_package}", "--repo", "{test_folder}", "--output", "{output_file}", "--overwrite"],
            0,
        ),  # Test with output file
        (
            ["--package", "{parent_package}", "--repo", "{test_folder}", "--output", "{output_existing}"],
            1,
        ),  # Output file exists without --overwrite
        (
            ["--package", "{parent_package}", "--repo", "{test_folder}", "--output", "{local_tmp_folder}"],
            1,
        ),  # Output path is a folder
        ([], "missing_param"),  # Missing required parent package argument
        (["--package", "{parent_package}"], "missing_param"),  # Missing required --repo option
        (["--package", "{parent_package_not_found}", "--repo", "{test_folder}"], 1),  # Parent package doesn't exist
        (["--package", "{parent_package}", "--repo", "{repo_not_found}"], 1),  # Repository doesn't exist
        (["--package", "{parent_package}", "--repo", "{source_package}"], 1),  # Repository is not a folder
    ],
)
@pytest.mark.asyncio
async def test_find_orphans_parameters(
    command_line, expected_exit_code, authenticator, test_folder_url, cli_runner, source_package, local_tmp_folder, local_output_existing
):
    from wrapp.datastructures.package import wrapp_file_in_repo
    from wrapp.repo.repository import Repository

    # Create test packages
    await wrapp.create(package_name="parent_package", version="1.0", source=source_package, catalog=False, repo=test_folder_url)
    await wrapp.create(package_name="orphan_package", version="1.0", source=source_package, catalog=False, repo=test_folder_url)

    repo = Repository(repo_url=test_folder_url)
    parent_package_file = wrapp_file_in_repo(repo, "parent_package", "1.0")
    parent_package_not_found = wrapp_file_in_repo(repo, "not_found", "1.0")
    output_file = join_paths(local_tmp_folder, "orphans_output.txt")

    if expected_exit_code == "missing_param":
        with pytest.raises(Exception, match="Missing parameter:"):
            run_wrapp(
                cli_runner,
                ["find-orphans"]
                + insert_variables(
                    command_line,
                    {
                        "parent_package": parent_package_file,
                        "parent_package_not_found": parent_package_not_found,
                        "test_folder": test_folder_url,
                        "repo_not_found": join_paths(test_folder_url, "repo_not_found"),
                        "source_package": source_package,
                        "output_file": output_file,
                        "output_existing": local_output_existing,
                        "local_tmp_folder": local_tmp_folder,
                    },
                ),
                0,  # This doesn't matter since we expect an exception
            )
    else:
        run_wrapp(
            cli_runner,
            ["find-orphans"]
            + insert_variables(
                command_line,
                {
                    "parent_package": parent_package_file,
                    "parent_package_not_found": parent_package_not_found,
                    "test_folder": test_folder_url,
                    "repo_not_found": join_paths(test_folder_url, "repo_not_found"),
                    "source_package": source_package,
                    "output_file": output_file,
                    "output_existing": local_output_existing,
                    "local_tmp_folder": local_tmp_folder,
                },
            ),
            expected_exit_code,
        )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["{package_to_delete}"],
            0,
        ),  # First test that this command works, before all other variants should fail with exit code 1
        ([], 1),  # Missing required package argument
        (["{package_not_found}"], 1),  # Package doesn't exist
        (["{source_package}"], 1),  # Package is not in a repository (work tree package)
    ],
)
@pytest.mark.asyncio
async def test_delete_packages_parameters(command_line, expected_exit_code, authenticator, test_folder_url, cli_runner, source_package):
    from wrapp.datastructures.package import wrapp_file_in_repo
    from wrapp.repo.repository import Repository

    # Create test package
    await wrapp.create(package_name="test_package", version="1.0", source=source_package, catalog=False, repo=test_folder_url)

    repo = Repository(repo_url=test_folder_url)
    package_to_delete = wrapp_file_in_repo(repo, "test_package", "1.0")
    package_not_found = wrapp_file_in_repo(repo, "not_found", "1.0")

    run_wrapp(
        cli_runner,
        ["delete-packages"]
        + insert_variables(
            command_line,
            {
                "package_to_delete": package_to_delete,
                "package_not_found": package_not_found,
                "source_package": source_package,
            },
        ),
        expected_exit_code,
    )


@pytest.mark.parametrize(
    "command_line, expected_exit_code",
    [
        (
            ["{wrapp_file}", "{output_file}"],
            0,
        ),  # First test that this command works with valid parameters
        (
            [],
            "missing_param",
        ),  # Missing required arguments, this doesn't return 1 as it is caught by Typer before executing the command itself
        (["{wrapp_file}"], "missing_param"),  # Missing output argument
        (["{output_file}"], "missing_param"),  # Missing wrapp_file argument
        (["{nonexistent_file}", "{output_file}"], 1),  # Non-existent wrapp file
        (["{wrapp_file}", "{output_file}", "--overwrite"], 0),  # With overwrite flag
    ],
)
@pytest.mark.asyncio
async def test_build_manifest_parameters(
    command_line, expected_exit_code, authenticator, test_folder_url, local_tmp_folder, cli_runner, source_package
):
    # Create a test wrapp file
    test_wrapp_file = join_paths(local_tmp_folder, "test.wrapp")
    test_output_file = join_paths(local_tmp_folder, "manifest.json")
    nonexistent_file = join_paths(local_tmp_folder, "nonexistent.wrapp")

    # Create a simple package info for testing
    from wrapp.datastructures.package import PackageInfo

    package_info = PackageInfo(name="test", version="1.0.0")
    await package_info.save(wrapp.CommandParameters(), test_wrapp_file)

    if expected_exit_code == "missing_param":
        with pytest.raises(Exception, match="Missing parameter:"):
            run_wrapp(
                cli_runner,
                ["build-manifest"]
                + insert_variables(
                    command_line,
                    {
                        "wrapp_file": test_wrapp_file,
                        "output_file": test_output_file,
                        "nonexistent_file": nonexistent_file,
                    },
                ),
            )
    else:
        run_wrapp(
            cli_runner,
            ["build-manifest"]
            + insert_variables(
                command_line,
                {
                    "wrapp_file": test_wrapp_file,
                    "output_file": test_output_file,
                    "nonexistent_file": nonexistent_file,
                },
            ),
            expected_exit_code,
        )
