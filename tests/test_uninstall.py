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

import pytest

import wrapp
from wrapp.datastructures.loaded_package import (
    LoadedPackage,
    collect_super_packages,
)
from wrapp.datastructures.package import (
    read_package_info,
    wrapp_file_path,
)
from wrapp.utils.scheduler_task_group import SchedulerTaskGroup
from wrapp.utils.storage import (
    Result,
    wrapp_delete_folder_tree,
    wrapp_delete_item,
    wrapp_supports_tags,
    wrapp_url_exists,
    wrapp_write_file_bytes,
)

from .common import (
    create_unnamed_package,
    generate_named_package,
    join_paths,
    run_wrapp,
)


@pytest.mark.asyncio
async def test_collect_super_packages(test_folder_url, authenticator, storage_backend):
    firstlevel = join_paths(test_folder_url, "firstlevel")
    secondlevel = join_paths(firstlevel, "secondlevel")
    top = await generate_named_package(storage_backend, "top", "1.0", test_folder_url)
    first = await generate_named_package(storage_backend, "first", "1.0", firstlevel)
    second = await generate_named_package(storage_backend, "second", "1.0", secondlevel)

    second_package = await read_package_info(second.package_file)
    super_packages = await collect_super_packages(LoadedPackage(second.package_file, second_package))

    assert len(super_packages) == 2


@pytest.mark.asyncio
async def test_uninstall(cli_runner, test_folder_url, authenticator, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source, "--repo", repo_folder])

    # Now install
    target_folder = join_paths(test_folder_url, "uninstall_test")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder])
    assert await wrapp_url_exists(target_folder)

    # And now uninstall it again
    run_wrapp(cli_runner, ["uninstall", package, target_folder])


@pytest.mark.asyncio
async def test_uninstall_large_package(cli_runner, test_folder_url, authenticator, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"), prefix="", large=True)
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source, "--repo", repo_folder])

    # Now install
    target_folder = join_paths(test_folder_url, "uninstall_test")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder])
    assert await wrapp_url_exists(target_folder)

    # And now uninstall it again
    run_wrapp(cli_runner, ["uninstall", package, target_folder])


@pytest.mark.asyncio
async def test_uninstall_with_dependency(cli_runner, test_folder_url, authenticator, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source, "--repo", repo_folder])

    # Now install with recording of the dependency
    target_folder = join_paths(test_folder_url, "uninstall_test")
    run_wrapp(cli_runner, ["new", "dependencies", "1", target_folder])
    target_toml = join_paths(target_folder, ".dependencies.wrapp")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder, "--package", target_toml])
    assert await wrapp_url_exists(target_folder)
    assert await wrapp_url_exists(target_toml)

    # And now uninstall it again
    run_wrapp(cli_runner, ["uninstall", package, target_folder, "--package", target_toml])


@pytest.mark.asyncio
async def test_uninstall_with_missing_dependency_OMPE48853(test_folder_url, authenticator, storage_backend):
    top_level_folder = join_paths(test_folder_url, "top_level")
    await create_unnamed_package(
        storage_backend,
        "top_level",
        "v1",
        folder=top_level_folder,
        repo_folder=test_folder_url,
        prefix="t",
    )
    await create_unnamed_package(
        storage_backend,
        "subpackage",
        "v1",
        folder=join_paths(test_folder_url, "subpackage"),
        repo_folder=test_folder_url,
        prefix="s",
    )

    # Install subpackage into top level
    await wrapp.install("subpackage", "v1", top_level_folder, repo=test_folder_url)

    # Create the new version with subpackage
    await wrapp.create("top_level", "v2", top_level_folder, repo=test_folder_url, catalog=False)

    # Install it somewhere else
    target_folder = join_paths(test_folder_url, "uninstall_test")
    await wrapp.install("top_level", "v2", target_folder, repo=test_folder_url)
    assert await wrapp_url_exists(target_folder)

    # Check that the dependency has been recorded
    top_level_package_path = wrapp_file_path(target_folder, "top_level")
    top_level_package_info = await read_package_info(top_level_package_path)
    assert top_level_package_info.dependencies[0].package == "subpackage"

    # Evil - remove the wrapp file of the subpackage
    sub_wrapp = wrapp_file_path(target_folder, "subpackage")
    assert await wrapp_url_exists(sub_wrapp)
    assert await wrapp_delete_item(wrapp.CommandParameters(), sub_wrapp) == Result.OK

    # And now uninstall the missing submodule from the top level package. This fails without force set
    with pytest.raises(wrapp.FailedCommand, match="package file not found"):
        await wrapp.uninstall("subpackage", target_folder, package=top_level_package_path, force=False)
    await wrapp.uninstall("subpackage", target_folder, package=top_level_package_path, force=True)

    # Check that the dependency is gone from the top level package
    top_level_package_info = await read_package_info(top_level_package_path)
    assert not top_level_package_info.dependencies


@pytest.mark.asyncio
async def test_uninstall_with_deleted_subpackage_OMPE63854(test_folder_url, authenticator, storage_backend):
    top_level_folder = join_paths(test_folder_url, "top_level")
    await create_unnamed_package(
        storage_backend,
        "top_level",
        "v1",
        folder=top_level_folder,
        repo_folder=test_folder_url,
        prefix="t",
    )
    await create_unnamed_package(
        storage_backend,
        "subpackage",
        "v1",
        folder=join_paths(test_folder_url, "subpackage"),
        repo_folder=test_folder_url,
        prefix="s",
    )

    # Install subpackage into top level
    await wrapp.install("subpackage", "v1", join_paths(top_level_folder, "subinstall"), repo=test_folder_url)

    # Create the new version with subpackage
    await wrapp.create("top_level", "v2", top_level_folder, repo=test_folder_url, catalog=False)

    # Install it somewhere else
    target_folder = join_paths(test_folder_url, "uninstall_test")
    await wrapp.install("top_level", "v2", target_folder, repo=test_folder_url)
    assert await wrapp_url_exists(target_folder)

    # Check that the dependency has been recorded
    top_level_package_path = wrapp_file_path(target_folder, "top_level")
    top_level_package_info = await read_package_info(top_level_package_path)
    assert top_level_package_info.dependencies[0].package == "subpackage"

    # Evil - remove installed sub-package manually
    sub_install_path = join_paths(target_folder, "subinstall")
    assert await wrapp_url_exists(sub_install_path)
    scheduler_context = wrapp.SchedulerContext(wrapp.SchedulingParameters(jobs=100))
    scheduler_context.start_scheduler()
    try:
        assert (
            await wrapp_delete_folder_tree(
                wrapp.CommandParameters(), sub_install_path, scheduler_context.parent_task_node().create_task_group_node()
            )
            == Result.OK
        )
    finally:
        await scheduler_context.stop_scheduler()

    # And now uninstall the missing submodule from the top level package. This fails without force set
    with pytest.raises(wrapp.FailedCommand, match="package file not found"):
        await wrapp.uninstall("subpackage", sub_install_path, package=top_level_package_path, force=False)
    await wrapp.uninstall("subpackage", sub_install_path, package=top_level_package_path, force=True)

    # Check that the dependency is gone from the top level package
    top_level_package_info = await read_package_info(top_level_package_path)
    assert not top_level_package_info.dependencies


@pytest.mark.asyncio
async def test_uninstall_with_tags(cli_runner, test_folder_url, authenticator, storage_backend):
    if not await wrapp_supports_tags(test_folder_url):
        pytest.skip("Tagging not supported on this storage backend")
    tags_supported = await wrapp_supports_tags(test_folder_url)
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"), tags=True)
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source, "--repo", repo_folder, "--tags"])

    # Now install
    target_folder = join_paths(test_folder_url, "uninstall_test")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder] + (["--tags"] if tags_supported else []))
    assert await wrapp_url_exists(target_folder)

    if tags_supported:
        # And now uninstall it again. This will fail because there is a tag and we do not specify to check them
        run_wrapp(cli_runner, ["uninstall", package, target_folder], 1)

        # Specify to check the tags makes sure we do not lose any meta-information on uninstall
        run_wrapp(cli_runner, ["uninstall", package, target_folder, "--tags"], 0)
    else:
        # Makes no difference if "--tags" is specified or not for the uninstall on a non-tag-supporting storage.
        run_wrapp(cli_runner, ["uninstall", package, target_folder, "--tags"], 0)


@pytest.mark.asyncio
async def test_failed_uninstall(cli_runner, test_folder_url, authenticator, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source, "--repo", repo_folder])

    # Now install
    target_folder = join_paths(test_folder_url, "uninstall_test")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder])
    assert await wrapp_url_exists(target_folder)

    # Now modify the installed file to create an uninstall conflict
    source_file = join_paths(target_folder, "testfile.txt")
    result = await wrapp_write_file_bytes(wrapp.CommandParameters(), source_file, b"90909090\n")
    assert result == Result.OK

    # And now uninstall it again, this will fail
    run_wrapp(cli_runner, ["uninstall", package, target_folder], 1)

    # Force uninstall should work
    run_wrapp(cli_runner, ["uninstall", package, target_folder, "--force"])


@pytest.mark.asyncio
async def test_uninstall_mandatory_parameters_OMPE_74147(test_folder_url, authenticator, storage_backend):
    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.uninstall("some_package", "")
    assert exc_info.value.parameter_name == "install_location"
    assert exc_info.value.value == ""

    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.uninstall("", "/some/location")
    assert exc_info.value.parameter_name == "package_name"
    assert exc_info.value.value == ""
