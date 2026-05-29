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

import wrapp
from tests.common import *
from tests.custom_assert import assert_url_exists
from tests.wrapp_sync_funcs import wrapp_url_exists_sync
from wrapp.app import app
from wrapp.datastructures.command_context import (
    SchedulingParameters,
    WrappScheduler,
)
from wrapp.utils.storage import (
    wrapp_delete_item,
    wrapp_url_exists,
)
from wrapp.utils.utils import join_paths


def test_create(cli_runner, test_folder_url, authenticator, source_package):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))

    repo_folder = test_folder_url

    params = ["create", package, version, source_package, "--repo", repo_folder]
    run_wrapp(cli_runner, params)

    # Check that the package is there
    package_folder = join_paths(repo_folder, ".packages")  # The .packages is default and always appended by wrapp
    assert_url_exists(join_paths(package_folder, package))
    assert_url_exists(join_paths(package_folder, package, version))
    assert_url_exists(join_paths(package_folder, package, version, "testfile.txt"))
    assert_url_exists(join_paths(package_folder, package, version, f".{package}.wrapp"))

    # Check the same with the list command
    result = run_wrapp(cli_runner, ["list-repo", repo_folder])
    assert version in result.stdout

    # Check that we cannot run it again
    print(f"Running wrapp {' '.join(params)} again without overwrite")
    result = cli_runner.invoke(app, " ".join(params))
    assert result.exit_code != 0


def test_create_ignore(cli_runner, test_folder_url, authenticator, local_tmp_folder, source_package):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    repo_folder = test_folder_url
    params = ["create", package, version, source_package, "--repo", repo_folder]

    # Now add an ignore file and check that the file is not copied
    ignore_file_name = os.path.join(local_tmp_folder, ".testignore")
    with open(ignore_file_name, "w") as ignore_file:
        ignore_file.write("testfile.txt\n")
    params.extend(["--ignore-file", ignore_file_name])
    print(f"Running wrapp {' '.join(params)} with ignore")
    result = cli_runner.invoke(app, params)
    assert result.exit_code == 0
    ignored_file = os.path.join(".packages", package, version, "testfile.txt")
    assert not wrapp_url_exists_sync(ignored_file)
    os.remove(ignore_file_name)


def test_create_with_new(cli_runner, test_folder_url, authenticator, source_package):
    # Create a wrapp file for this package
    package = "test_package"
    version = "1"
    run_wrapp(cli_runner, ["new", package, version, test_folder_url])
    wrapp_file = join_paths(test_folder_url, f".{package}.wrapp")

    # Use the short form of create just specifying the wrapp file url
    params = ["create", "--package", wrapp_file, "--repo", test_folder_url]
    run_wrapp(cli_runner, params)

    # Check that the package is there
    package_folder = join_paths(test_folder_url, ".packages")  # The .packages is default and always appended by wrapp
    assert_url_exists(join_paths(package_folder, package, version, f".{package}.wrapp"))
    assert_url_exists(join_paths(package_folder, package, version, "source_package/testfile.txt"))

    # Check the same with the list command
    result = run_wrapp(cli_runner, ["list-repo", test_folder_url])
    assert version in result.stdout

    # Check that we cannot run it again
    print(f"Running wrapp {' '.join(params)} again without overwrite")
    result = cli_runner.invoke(app, " ".join(params))
    assert result.exit_code != 0

    # But we can generate a second version with the override version switch
    run_wrapp(cli_runner, ["create", "--package", wrapp_file, "--repo", test_folder_url, "--new-version", "20230725"])

    # Check that worked
    result = run_wrapp(cli_runner, ["list-repo", test_folder_url])
    assert "20230725" in result.stdout


@pytest.mark.asyncio
async def test_create_with_missing_dependency_OMPE48853(test_folder_url, storage_backend, authenticator, source_package):
    await create_unnamed_package(
        storage_backend,
        "subpackage",
        "v1",
        folder=join_paths(test_folder_url, "subpackage_source"),
        repo_folder=test_folder_url,
        prefix="c",
    )
    await wrapp.new("top_level", "dev", source_package, True)
    toplevel_wrapp = join_paths(source_package, ".top_level.wrapp")
    await wrapp.install("subpackage", "v1", join_paths(source_package, "subpath"), repo=test_folder_url, package=toplevel_wrapp)

    # Check that the sub wrapp file is there, and then remove it to create the missing dependency problem
    subwrapp = join_paths(source_package, "subpath", ".subpackage.wrapp")
    assert await wrapp_url_exists(subwrapp)
    await wrapp_delete_item(CommandParameters(), subwrapp)

    # Now, make sure we can create the top level package anyway
    await wrapp.create("top_level", "v1", source_package, catalog=False, repo=test_folder_url)

    # Install the package
    target_dir = join_paths(test_folder_url, "target")
    await wrapp.install("top_level", "v1", target_dir, repo=test_folder_url)

    # Check that the content exists, but not the subwrapp file (the files of the subpackage have been collected by the top level package)
    assert await wrapp_url_exists(join_paths(target_dir, "subpath", "csource_package", "ctestfile.txt"))
    assert not await wrapp_url_exists(join_paths(target_dir, "subpath", ".subpackage.wrapp"))


@pytest.mark.asyncio
async def test_create_alternate_storage(
    cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend, alternate_storage_source_package
):
    target_folder = join_paths(test_folder_url, "target_folder")
    params = ["create", "test_create_from_alternate_storage", "v1", alternate_storage_source_package, "--repo", target_folder]
    run_wrapp(cli_runner, params)


@pytest.mark.asyncio
async def test_create_of_dependency_only_package_OMPE38875(
    test_folder_url, authenticator, storage_backend, alternate_storage_source_package
):
    super_folder = join_paths(test_folder_url, "superpackage")
    await create_unnamed_package(
        storage_backend,
        "subpackage",
        "v1",
        folder=join_paths(test_folder_url, "subpackage_source"),
        repo_folder=test_folder_url,
        prefix="s",
    )
    await wrapp.install("subpackage", "v1", super_folder, repo=test_folder_url)

    # Try to create the super package, the original bug had this failing because the package was empty except for dependencies
    await wrapp.create("super", "s1", super_folder, False, repo=test_folder_url)


@pytest.mark.asyncio
@pytest.mark.timeout(180, func_only=True)
async def test_create_with_one_job_OMPE40881(test_folder_url, authenticator, storage_backend):
    repo_folder = join_paths(test_folder_url, "repo")
    subpackage_source = join_paths(test_folder_url, "subpackage_source")
    source = join_paths(test_folder_url, "source")
    await create_unnamed_package(
        storage_backend,
        "subsubpackage",
        "v1",
        folder=join_paths(test_folder_url, "subsubpackage_source"),
        repo_folder=repo_folder,
        prefix="s",
    )
    await wrapp.install("subsubpackage", "v1", join_paths(subpackage_source, "subsub_1"), repo=repo_folder)
    await wrapp.install("subsubpackage", "v1", join_paths(subpackage_source, "subsub_2"), repo=repo_folder)
    await wrapp.create("subpackage", "v1", subpackage_source, False, repo=repo_folder)

    await wrapp.install("subpackage", "v1", join_paths(source, "sub_1"), repo=repo_folder)

    # Try to create the super package, the original bug had this failing because the package was empty except for dependencies
    async with WrappScheduler(params=SchedulingParameters(jobs=1, file_transfer_jobs=1)) as scheduler:
        await wrapp.create("package", "v1", source, False, repo=repo_folder, scheduler=scheduler)


@pytest.mark.asyncio
async def test_create_empty_parameters(test_folder_url, authenticator, storage_backend):
    """Test create() raises FailedCommandValueError for empty string parameters."""
    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.create(package_name="test_package", version="1.0.0", source="", catalog=False)
    assert exc_info.value.parameter_name == "source"
    assert exc_info.value.value == ""

    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.create(package_name="", version="1.0.0", source=test_folder_url, catalog=False)
    assert exc_info.value.parameter_name == "package_name"
    assert exc_info.value.value == ""

    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.create(package_name="test_package", version="", source=test_folder_url, catalog=False)
    assert exc_info.value.parameter_name == "version"
    assert exc_info.value.value == ""
