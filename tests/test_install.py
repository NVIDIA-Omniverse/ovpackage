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
import random
import string

from pydantic import ValidationError

from tests.common import *
from tests.common import get_tags_async
from tests.custom_assert import assert_url_exists
from wrapp import (
    install,
    mirror_with_dependencies,
    new,
)
from wrapp.commands.install import _local_copy_ops_for_eager_hash_cache
from wrapp.datastructures.catalog import CatalogItem
from wrapp.datastructures.package import (
    PackageInfo,
    read_package_info,
    write_package_info,
)
from wrapp.datastructures.patch import (
    ItemType,
    PatchOpBase,
    PatchOpCopy,
)
from wrapp.repo.repository import Repository
from wrapp.utils.logging import allow_reinit_logging_for_test
from wrapp.utils.storage import (
    Result,
    wrapp_is_case_sensitive,
    wrapp_supports_tags,
    wrapp_url_exists,
    wrapp_write_file_bytes,
)
from wrapp.utils.utils import (
    FailedCommand,
    is_nucleus_url,
)


def test_local_copy_ops_for_eager_hash_cache_skips_remote_destinations():
    local_op = PatchOpCopy(
        src="https://example.invalid/source.txt",
        dst="/tmp/install-target/source.txt",
        hash="sha-256-flat;1048576;local",
        item_type=ItemType.file,
    )
    file_url_op = PatchOpCopy(
        src="https://example.invalid/file-url.txt",
        dst="file:///tmp/install-target/file-url.txt",
        hash="sha-256-flat;1048576;file-url",
        item_type=ItemType.file,
    )
    remote_op = PatchOpCopy(
        src="https://example.invalid/remote.txt",
        dst="https://storage.example.invalid/install-target/remote.txt",
        hash="sha-256-flat;1048576;remote",
        item_type=ItemType.file,
    )
    missing_hash_op = PatchOpCopy(
        src="https://example.invalid/no-hash.txt",
        dst="/tmp/install-target/no-hash.txt",
        item_type=ItemType.file,
    )

    eligible_ops = _local_copy_ops_for_eager_hash_cache(
        [
            ("local", local_op),
            ("file-url", file_url_op),
            ("remote", remote_op),
            ("missing-hash", missing_hash_op),
        ]
    )

    assert eligible_ops == [("local", local_op), ("file-url", file_url_op)]


@pytest.mark.asyncio
async def test_install(cli_runner, test_folder_url, authenticator, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    # Now install the just created package
    target_folder = join_paths(test_folder_url, "installed_package")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder])
    assert await wrapp_url_exists(target_folder)

    # Now install with recording of the dependency
    target_folder = join_paths(test_folder_url, "installed_package_with_dependency")
    run_wrapp(cli_runner, ["new", "toplevel", "1", target_folder])
    wrapp_file = f"{target_folder}/.toplevel.wrapp"
    assert await wrapp_url_exists(wrapp_file)
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder, "--package", wrapp_file])
    assert_url_exists(target_folder)

    assert await PackageInfo.load(wrapp_file)

    # Install again, which implicitly runs an update
    target_folder = join_paths(test_folder_url, "installed_package")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder])
    assert await wrapp_url_exists(target_folder)


@pytest.mark.asyncio
async def test_install_dry_run(cli_runner, test_folder_url, authenticator, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    # Now install the just created package, but with the dry run option set
    target_folder = join_paths(test_folder_url, "installed_package")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder, "--dry-run"])
    assert not await wrapp_url_exists(target_folder)


@pytest.mark.asyncio
async def test_install_fails_when_file_is_already_there(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    # Create the target folder before installation and place one copy of the source package into the destination
    target_folder = join_paths(test_folder_url, "installed_package")
    await wrapp_write_file_bytes(CommandParameters(), join_paths(target_folder, "testfile.txt"), b"conflicting content")

    # Now install the just created package. This fails, because it is not an update install but there is a conflict
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder], 1)

    # We can instruct install to create a patch file that will resolve that conflict though
    patch_file = join_paths(local_tmp_folder, "install_conflict.json")
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder, "--patch", patch_file], 0)
    assert await wrapp_url_exists(patch_file)

    # We can apply the patch
    run_wrapp(cli_runner, ["apply-patch", patch_file])

    # After that, another installation of the same version no longer produces an error, install is idempotent
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder], 0)


@pytest.mark.asyncio
async def test_install_with_force(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    # Create the target folder before installation and place one copy of the source package into the destination
    target_folder = join_paths(test_folder_url, "installed_package")
    await wrapp_write_file_bytes(CommandParameters(), join_paths(target_folder, "testfile.txt"), b"conflicting content")
    await wrapp_write_file_bytes(CommandParameters(), join_paths(target_folder, "additional_file.txt"), b"additional, ignored content")

    # Now install the just created package. This fails, because it is not an update install but there is a conflict
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder], 1)

    # Run again, but with force
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--force", "--repo", repo_folder], 0)

    # And now the normal install should also run again
    run_wrapp(cli_runner, ["install", package, version, target_folder, "--repo", repo_folder], 0)


@pytest.mark.asyncio
async def test_install_conflicts_extras(test_folder_url, authenticator, local_tmp_folder, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")

    context = wrapp.CommandParameters()
    async with wrapp.WrappScheduler(params=wrapp.SchedulingParameters()) as scheduler:
        await wrapp.create(package, version, source_package, catalog=False, repo=repo_folder, context=context, scheduler=scheduler)

        # Create the target folder before installation and place one copy of the source package into the destination
        target_folder = join_paths(test_folder_url, "installed_package")
        await wrapp_write_file_bytes(CommandParameters(), join_paths(target_folder, "testfile.txt"), b"conflicting content")
        await wrapp_write_file_bytes(CommandParameters(), join_paths(target_folder, "additional_file.txt"), b"additional, ignored content")

        # Now install the just created package. This fails, because it is not an update install but there is a conflict
        conflicts_without_force: list[tuple[str, PatchOpBase | None]] = [("foo", None)]
        extras_without_force: list[tuple[str, PatchOpBase | None]] = [("bar", None)]

        with pytest.raises(wrapp.FailedCommand, match="local copy is modified"):
            await wrapp.install(
                package,
                version,
                target_folder,
                conflicts=conflicts_without_force,
                extras=extras_without_force,
                repo=repo_folder,
                context=context,
                scheduler=scheduler,
            )
        assert ["foo", "testfile.txt"] == [p for p, _ in conflicts_without_force]
        assert ["bar", "additional_file.txt"] == [p for p, _ in extras_without_force]

        # Run again, but with force, it should succeed
        conflicts_with_force: list[tuple[str, PatchOpBase | None]] = [("foo", None)]
        extras_with_force: list[tuple[str, PatchOpBase | None]] = [("bar", None)]
        await wrapp.install(
            package,
            version,
            target_folder,
            force=True,
            conflicts=conflicts_with_force,
            extras=extras_with_force,
            repo=repo_folder,
            context=context,
            scheduler=scheduler,
        )
        assert ["foo", "testfile.txt"] == [p for p, _ in conflicts_with_force]
        assert ["bar", "additional_file.txt"] == [p for p, _ in extras_with_force]


@pytest.mark.asyncio
async def test_install_over_new_package(test_folder_url, authenticator, local_tmp_folder, storage_backend):
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    src_folder = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")

    context = wrapp.CommandParameters()
    async with wrapp.WrappScheduler(params=wrapp.SchedulingParameters()) as scheduler:
        await wrapp.new(package, "this_version_does_not_exist", src_folder, create_catalog=False, context=context, scheduler=scheduler)
        await wrapp.create(package, version, src_folder, catalog=False, repo=repo_folder, context=context, scheduler=scheduler)

        # Now run an install back into the package. This should just work even though the package recorded in the wrapp file
        # created by "new" is in no repo.
        await wrapp.install(package, version, src_folder, repo=repo_folder, context=context, scheduler=scheduler)


@pytest.mark.asyncio
async def test_install_logs_warning_when_run_on_copy_package(test_folder_url, authenticator, local_tmp_folder, storage_backend, caplog):
    package = "manually_copied"
    version = "1"
    src_folder = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    context = wrapp.CommandParameters()
    async with wrapp.WrappScheduler(params=wrapp.SchedulingParameters()) as scheduler:
        # Create the package
        await wrapp.create(package, version, src_folder, catalog=False, repo=test_folder_url, context=context, scheduler=scheduler)
        # Copy the package to a different location - this will break
        source_repo = Repository(repo_url=test_folder_url)
        target_repo = Repository(repo_url=join_paths(test_folder_url, "copy_destination"))
        await wrapp.freeze(source_repo.package_cache_url(), target_repo.package_cache_url(), scheduler=scheduler)
        # Now run an install from the copied package cache - this must fail, because the catalog still points to the original source
        install_target = join_paths(test_folder_url, "install_target")
        await wrapp.install(package, version, install_target, repo=target_repo.package_cache_url(), context=context, scheduler=scheduler)
        assert any(
            [log_record.levelname == "WARNING" and "package problem" in log_record.message for log_record in caplog.records]
        ), f"Did not find expected warning in log output: {caplog.records}"


@pytest.mark.asyncio
async def test_install_with_tags_to_second_server(cli_runner, test_folder_url, auth_info, authenticator, storage_backend):
    if not await wrapp_supports_tags(test_folder_url):
        pytest.skip("Tagging not supported on this storage backend")
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"), tags=True)
    repo_folder = join_paths(test_folder_url, ".package_cache")

    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder, "--tags"])

    # Now install the just created package
    target_folder = join_paths(test_folder_url, "installed_package")

    if await wrapp_supports_tags(target_folder):
        assert is_nucleus_url(
            target_folder
        )  # we also need special handling for Nucleus which currently is the only storage supporting tags
        # Use an alias to force client library into copy mode instead of server-side copy
        t = urlparse(target_folder)
        alias_target_server = t.scheme + "://alias1@" + t.netloc
        target_folder = alias_target_server + t.path
        target_file = join_paths(target_folder, "testfile.txt")

        run_wrapp(
            cli_runner,
            [
                "install",
                package,
                version,
                target_folder,
                "--repo",
                repo_folder,
                "--tags",
                "--auth",
                f"{alias_target_server},{auth_info.username},{auth_info.password_or_token}",
                "--auth",
                f"{auth_info.server},{auth_info.username},{auth_info.password_or_token}",
            ],
        )
        assert await wrapp_url_exists(target_folder)

        tags = await get_tags_async(target_file)
        assert len(tags["path_result"][0]["tags"]) == 1
    else:
        # If there is no tag support for the target_folder, the command must fail if "--tags" is specified.
        run_wrapp(
            cli_runner,
            [
                "install",
                package,
                version,
                target_folder,
                "--repo",
                repo_folder,
                "--tags",
            ],
            1,
        )


@pytest.mark.asyncio
async def test_install_recording_dependency_from_different_repo(test_folder_url, authenticator, storage_backend):
    # OM-117588
    dog_package_name = "d_package"
    cat_package_name = "c_package"
    dogs = await storage_backend.create_source_package(
        join_paths(test_folder_url, dog_package_name), prefix="dogs_"
    )  # Give a prefix because in this case we don't want overlap
    cats = await storage_backend.create_source_package(join_paths(test_folder_url, cat_package_name), prefix="cats_")
    repo1 = join_paths(test_folder_url, "remote1")
    repo2 = join_paths(test_folder_url, "remote2")
    await create(dog_package_name, "1.0", dogs, catalog=False, repo=repo1)
    await create(cat_package_name, "1.0", cats, catalog=False, repo=repo2)
    shelter = join_paths(test_folder_url, "shelter")
    await new("shelter", "1.0", shelter, create_catalog=False)
    await install(dog_package_name, "1.0", shelter, package=join_paths(shelter, ".shelter.wrapp"), tags=False, repo=repo1)

    # This would fail without the fix
    await install(cat_package_name, "1.0", shelter, package=join_paths(shelter, ".shelter.wrapp"), tags=False, repo=repo2)


@pytest.mark.asyncio
async def test_update_install(cli_runner, test_folder_url, authenticator, storage_backend):
    source_package_1 = await storage_backend.create_source_package(join_paths(test_folder_url, "source1"))
    target_package = join_paths(test_folder_url, "target")
    install_destination = join_paths(target_package, "worktree_source1")
    repo_folder = test_folder_url

    # Creating version one of source1
    run_wrapp(cli_runner, ["create", "source1", "1", source_package_1, "--repo", repo_folder])

    # Installing version 1 into our target
    run_wrapp(cli_runner, ["new", "dependencies", "1", target_package])
    dependency_file = join_paths(target_package, ".dependencies.wrapp")
    run_wrapp(cli_runner, ["install", "source1", "1", install_destination, "--repo", repo_folder, "--package", dependency_file])
    assert await wrapp_url_exists(dependency_file)
    dependencies = await PackageInfo.load(dependency_file)
    assert dependencies.dependencies[0].package == "source1"
    assert dependencies.dependencies[0].version == "1"

    # Modify package 1 by adding a file and creating version 2
    added_file = "added_file.txt"
    new_file = join_paths(source_package_1, added_file)
    result = await wrapp_write_file_bytes(CommandParameters(), new_file, b"New content", "file added after version 1")
    assert result == Result.OK
    run_wrapp(cli_runner, ["create", "source1", "2", source_package_1, "--repo", repo_folder])

    # Updating to new package version
    run_wrapp(cli_runner, ["install", "source1", "2", install_destination, "--repo", repo_folder, "--package", dependency_file])
    assert await wrapp_url_exists(dependency_file)
    dependencies = await PackageInfo.load(dependency_file)
    assert dependencies.dependencies[0].package == "source1"
    assert dependencies.dependencies[0].version == "2"


@pytest.mark.asyncio
async def test_install_alternate_storage(
    cli_runner,
    test_folder_url,
    authenticator,
    local_tmp_folder,
    storage_backend,
    alternate_storage_source_package,
    alternate_storage_test_folder_url,
):
    target_folder = join_paths(test_folder_url, "target_folder")
    params = ["create", "alternate_storage_package", "v1", alternate_storage_source_package, "--repo", alternate_storage_test_folder_url]
    run_wrapp(cli_runner, params)
    params = ["install", "alternate_storage_package", "v1", target_folder, "--repo", alternate_storage_test_folder_url]
    run_wrapp(cli_runner, params)


@pytest.mark.asyncio
async def test_install_fails_on_circular_dependency(cli_runner, test_folder_url, source_package, authenticator, storage_backend):
    repo = join_paths(test_folder_url, "remote")
    params = ["create", "package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    install_url = join_paths(test_folder_url, "install_circular")
    await install(package_name="package", version="v1", destination=install_url, repo=repo)
    installed_package = join_paths(install_url, ".package.wrapp")

    # Now try the evil installation with a dependency on itself
    with pytest.raises(FailedCommand, match="Circular dependency detected"):
        await install(package_name="package", version="v1", destination=install_url, repo=repo, package=installed_package)


@pytest.mark.asyncio
async def test_install_logs_warning_on_out_of_tree_dependency(
    cli_runner, test_folder_url, source_package, authenticator, storage_backend, local_tmp_folder
):
    repo = join_paths(test_folder_url, "remote")
    # Create two packages from the same source
    params = ["create", "package1", "1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)
    params = ["create", "package2", "1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    # Install one
    install_url = join_paths(test_folder_url, "install_path")
    await install(package_name="package1", version="1", destination=install_url, repo=repo)
    installed_package = join_paths(install_url, ".package1.wrapp")

    # Now install again into a different directory, but doing a cross-reference that logs a warning
    install_url = join_paths(test_folder_url, "parallel_path")
    log_file = join_paths(local_tmp_folder, "install.log")
    params = ["install", "package2", "1", install_url, "--repo", repo, "--package", installed_package, "--log-file", log_file]
    allow_reinit_logging_for_test()
    run_wrapp(cli_runner, params)
    if os.environ.get("PYTEST_XDIST_WORKER", "") != "":
        log_file = log_file + "." + os.environ.get("PYTEST_XDIST_WORKER", "")
    with open(log_file, "r") as f:
        assert any(["that is not a subfolder of the package file location" in line for line in f.readlines()])


# See also OMPE-53971
@pytest.mark.asyncio
async def test_install_with_backslash_in_package_path(
    cli_runner, test_folder_url, source_package, authenticator, storage_backend, local_tmp_folder, fail_on_warnings_errors
):
    if not (os.path.sep == "\\" and storage_backend.storage_type() == StorageBackendForTests.LOCAL_DIR):
        pytest.skip(f"Tests make only sense when the path separator on the system is a backslash (and not '{os.path.sep}')")
    repo = join_paths(test_folder_url, "repo")
    params = ["create", "outer_package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)
    params = ["create", "inner_package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    # Deliberately provoke paths with backslashes
    install_url = os.path.join(local_tmp_folder, "install_path")
    await install(package_name="outer_package", version="v1", destination=install_url, repo=repo)
    await install(
        package_name="inner_package",
        version="v1",
        destination=join_paths(install_url, "inner_package"),
        package=os.path.join(install_url, ".outer_package.wrapp"),
        repo=repo,
    )


@pytest.mark.asyncio
async def test_install_with_invalid_args(cli_runner, test_folder_url, source_package, authenticator, storage_backend, local_tmp_folder):
    repo = join_paths(test_folder_url, "remote")
    params = ["create", "package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    with pytest.raises(FailedCommand, match="cannot be empty"):
        await install(package_name="package", version="v1", destination="", repo=repo)

    install_url = join_paths(test_folder_url, "install_fail")
    with pytest.raises(FailedCommand, match="cannot be empty"):
        await install(package_name="", version="v1", destination=install_url, repo=repo)

    with pytest.raises(FailedCommand, match="cannot be empty"):
        await install(package_name="package", version="", destination=install_url, repo=repo)

    with pytest.raises(FailedCommand, match="cannot be empty"):
        await install(package_name="package", version="v1", destination="   ", repo=repo)

    with pytest.raises(FailedCommand, match="cannot be empty"):
        await install(package_name="   ", version="v1", destination=install_url, repo=repo)

    with pytest.raises(FailedCommand, match="cannot be empty"):
        await install(package_name="package", version="   ", destination=install_url, repo=repo)


@pytest.mark.asyncio
async def test_install_writing_correct_deps_OMPE62832(cli_runner, test_folder_url, source_package, authenticator, storage_backend):
    repo = join_paths(test_folder_url, "repo")
    params = ["create", "inner_package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    # We need a package with dependendencies in a package.
    source_url = join_paths(test_folder_url, "source_path")
    install_url = join_paths(test_folder_url, "install_path")
    mirror_repo = join_paths(test_folder_url, "mirror_repo")
    await install(
        package_name="inner_package", version="v1", destination=join_paths(source_url, "outer_package", "inner_package"), repo=repo
    )
    await create(package_name="outer_package", version="v1", source=join_paths(source_url, "outer_package"), catalog=False, repo=repo)
    await install(
        package_name="outer_package", version="v1", destination=join_paths(source_url, "nested_package", "outer_package"), repo=repo
    )
    await create(package_name="nested_package", version="v1", source=join_paths(source_url, "nested_package"), catalog=False, repo=repo)
    # We need to  mirror the package, so the repository pointer for the subpackages needs to be updated
    await mirror_with_dependencies(package_name="nested_package", version="v1", source_repo=repo, destination_repo=mirror_repo)
    # Now install the package - the packages will be loaded with dependencies, but for the calculation of the fingerprints, various
    # fields (among them the "repository" field for the dependencies) will be set to None. Later the cached version of the package info
    # is written out - and if the fingerprint reset the fields of the original cached version instead of a copy, things break.
    # We need to install twice so the destination package is cached for the fingerprint calculation (on the first install the
    # file does not exist yet), and we need to run it from two different repositories, so the subpackage repo URL needs to be updated
    # on the second run.
    # We also need to run the test on S3 (not Nucleus) to see the error: on Nucleus the URL for the loaded package is the checkpoint URL
    # which does not match the install path without checkpoint (i.e. on Nucleus the cache is broken for this last step).
    await install(package_name="nested_package", version="v1", destination=install_url, repo=repo)
    await install(package_name="nested_package", version="v1", destination=install_url, repo=mirror_repo)

    # Verify that we can still read the updated subpackage .wrapp file with its dependencies
    package_info = await wrapp.read_package_info(join_paths(install_url, "outer_package", ".outer_package.wrapp"))
    assert package_info is not None
    assert package_info.dependencies is not None and len(package_info.dependencies) == 1
    assert isinstance(package_info.dependencies[0].repository, str)
    assert len(package_info.dependencies[0].repository) > 0


@pytest.mark.asyncio
async def test_install_invalid_wrapp_file_OMPE62834(cli_runner, test_folder_url, source_package, authenticator, storage_backend):
    repo = join_paths(test_folder_url, "repo")
    params = ["create", "package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    # Install the package
    install_url = join_paths(test_folder_url, "install_path")
    await install(package_name="package", version="v1", destination=install_url, repo=repo)

    # Modify the .wrapp file so pydantic cannot read it anymore
    wrapp_file_path = join_paths(install_url, ".package.wrapp")
    p = await read_package_info(wrapp_file_path)
    assert p is not None
    p.version = None  # Invalid
    await write_package_info(wrapp_file_path, p)
    # Verify the wrapp file causes a parse error now
    try:
        p = await read_package_info(wrapp_file_path)
    except FailedCommand as e:
        assert isinstance(e.__cause__, ValidationError)

    # Try again to install - it should be able to write despite the invalid .wrapp file
    await install(package_name="package", version="v1", destination=install_url, force=True, repo=repo)

    # Verify that the invalid .wrapp file has been written
    p = await read_package_info(wrapp_file_path)
    assert p is not None


@pytest.mark.asyncio
async def test_install_invalid_subpackage_wrapp_file_OMPE62834(cli_runner, test_folder_url, source_package, authenticator, storage_backend):
    repo = join_paths(test_folder_url, "repo")
    params = ["create", "package", "v1", source_package, "--repo", repo]
    run_wrapp(cli_runner, params)

    # Install the subpackage
    source_url = join_paths(test_folder_url, "source_path")
    await install(package_name="package", version="v1", destination=join_paths(source_url, "sub"), repo=repo)
    # Make a nested package
    await create(package_name="nested", version="v1", source=source_url, repo=repo, catalog=False)

    # Install the nested package
    install_url = join_paths(test_folder_url, "install_path")
    await install(package_name="nested", version="v1", destination=install_url, repo=repo)

    # Modify the subpackage .wrapp file so pydantic cannot read it anymore
    wrapp_file_path = join_paths(install_url, "sub", ".package.wrapp")
    p = await read_package_info(wrapp_file_path)
    assert p is not None
    p.version = None  # Invalid
    await write_package_info(wrapp_file_path, p)
    # Verify the wrapp file causes a parse error now
    try:
        p = await read_package_info(wrapp_file_path)
    except FailedCommand as e:
        assert isinstance(e.__cause__, ValidationError)

    # Try again to install - it should be able to write despite the invalid .wrapp file
    await install(package_name="nested", version="v1", destination=install_url, force=True, repo=repo)

    # Verify that the invalid .wrapp file has been written
    p = await read_package_info(wrapp_file_path)
    assert p is not None


# The problem occurred when fixing OMPE-60794: when an install updates an old version WRAPP file,
# and the subpackage is used more than once in the installed package, the comparison could break.
@pytest.mark.asyncio
async def test_install_with_wrapp_files_with_old_version_deps(test_folder_url, authenticator, storage_backend):
    repo_url = join_paths(test_folder_url, "source_repo")

    # Create the dependency chain: A -> B -> D and A -> C -> D
    # Note that this is not a diamond structure in the working folder - B and C install D into different locations.
    await create_unnamed_package(
        storage_backend, "package_d", "v1", folder=join_paths(test_folder_url, "package_d_source"), repo_folder=repo_url, prefix="d"
    )

    # Create package B that depends on D
    package_b_source = join_paths(test_folder_url, "package_b_source")
    await install("package_d", "v1", package_b_source + "/dep_d", repo=repo_url)
    await create("package_b", "v1", package_b_source, False, repo=repo_url)

    # Create package C that depends on D
    package_c_source = join_paths(test_folder_url, "package_c_source")
    await install("package_d", "v1", package_c_source + "/dep_d", repo=repo_url)
    await create("package_c", "v1", package_c_source, False, repo=repo_url)

    # Create package A that depends on B and C
    package_a_source = join_paths(test_folder_url, "package_a_source")
    await install("package_b", "v1", package_a_source + "/dep_b", repo=repo_url)
    await install("package_c", "v1", package_a_source + "/dep_c", repo=repo_url)
    await create("package_a", "v1", package_a_source, False, repo=repo_url)

    # install A to target
    package_a_install = join_paths(test_folder_url, "package_a_install")
    await install("package_a", "v1", join_paths(package_a_install, "install_a"), repo=repo_url)
    # Again, should work and do nothing
    await install("package_a", "v1", join_paths(package_a_install, "install_a"), repo=repo_url)


# OMPE-96260: When a parent package is re-installed with a bumped subpackage version into a
# destination that already contains an older version of that subpackage, the install-time
# retarget step must rewrite the freshly-copied subpackage's `repository` field to the repo it
# was actually installed from. The bug: install consults a pre-patch snapshot of the destination
# subpackage (whose `repository` was already correct from the earlier install), sees "no change
# needed", and leaves the newly-copied subpackage pointing at the original source repo.
@pytest.mark.asyncio
async def test_install_retargets_repository_of_updated_subpackage_OMPE96260(test_folder_url, authenticator, storage_backend):
    # The "origin" repo: where the subpackage and parent are first published. Stands in for the
    # Nucleus repo in the real defect.
    repo_origin = join_paths(test_folder_url, "repo_origin")
    # The "mirror" repo: where the parent (and its subpackage copy) is mirrored to and installed
    # from. Stands in for the S3 mirror in the real defect. mirror_with_dependencies copies the
    # subpackage into this repo but leaves the subpackage `.wrapp`'s own `repository` field pointing
    # at the origin repo - exactly the stale field that install must retarget at install time.
    repo_mirror = join_paths(test_folder_url, "repo_mirror")

    dest = join_paths(test_folder_url, "dest")
    sub_wrapp_install_path = join_paths(dest, "dep_s", ".package_s.wrapp")

    async def publish_and_mirror_parent(version: str) -> None:
        # Publish the leaf subpackage and a parent that depends on it into the origin repo.
        await create_unnamed_package(
            storage_backend,
            "package_s",
            version,
            folder=join_paths(test_folder_url, f"package_s_source_{version}"),
            repo_folder=repo_origin,
            prefix="s",
        )
        parent_source = join_paths(test_folder_url, f"package_p_source_{version}")
        await install("package_s", version, join_paths(parent_source, "dep_s"), repo=repo_origin)
        await create("package_p", version, parent_source, False, repo=repo_origin)
        # Mirror the parent and its dependency into the mirror repo. The mirrored subpackage now
        # lives in repo_mirror but its `.wrapp` still records `repository == repo_origin`.
        await mirror_with_dependencies("package_p", version, source_repo=repo_origin, destination_repo=repo_mirror)

    # First install from the mirror repo: destination has no prior subpackage, so retarget reads the
    # freshly written file and correctly rewrites its `repository` to the mirror repo.
    await publish_and_mirror_parent("v1")
    await install("package_p", "v1", dest, repo=repo_mirror)
    info = await read_package_info(sub_wrapp_install_path)
    assert info is not None
    assert "repo_origin" not in (info.repository or ""), f"first install left stale origin repository: {info.repository}"
    assert "repo_mirror" in (info.repository or ""), f"first install did not retarget to mirror repository: {info.repository}"

    # Update over the existing install: bump the subpackage to v2, mirror the new parent, and
    # re-install into the same destination. The newly-copied subpackage `.wrapp` again carries
    # `repository == repo_origin` and must be retargeted to the mirror repo.
    await publish_and_mirror_parent("v2")
    await install("package_p", "v2", dest, repo=repo_mirror)
    info = await read_package_info(sub_wrapp_install_path)
    assert info is not None
    assert "repo_origin" not in (info.repository or ""), f"updated subpackage kept stale origin repository (OMPE-96260): {info.repository}"
    assert "repo_mirror" in (info.repository or ""), f"updated subpackage was not retargeted to mirror repository: {info.repository}"


@pytest.mark.asyncio
async def test_install_from_cross_storage_type_OMPE62817(
    cli_runner, test_folder_url, alternate_storage_test_folder_url, authenticator_with_alternate, storage_backend
):
    source_subpackage = await storage_backend.create_source_package(join_paths(test_folder_url, "subpackage_source"))
    repo_folder = join_paths(test_folder_url, "repo")
    await wrapp.create("subpackage", "v1", source_subpackage, False, repo=repo_folder)

    # We need a package with a subpackage - only .wrapp files have a different hash from normal Nucleus hashes
    source_package = join_paths(test_folder_url, "package_source")
    await wrapp.install("subpackage", "v1", source_package, repo=repo_folder)
    await wrapp.create("package", "1.0", source_package, False, repo=repo_folder)

    # Now install the just created package to a different storage to verify cross-storage installs do work
    target_folder = join_paths(alternate_storage_test_folder_url, "installed_package")
    await wrapp.install("package", "1.0", target_folder, repo=repo_folder)
    p = await PackageInfo.load(join_paths(target_folder, ".package.wrapp"))
    assert p.version == "1.0"

    # Install again, which implicitly runs an update
    target_folder = join_paths(alternate_storage_test_folder_url, "installed_package")
    await wrapp.install("package", "1.0", target_folder, repo=repo_folder)
    p = await PackageInfo.load(join_paths(target_folder, ".package.wrapp"))
    assert p.version == "1.0"


@pytest.mark.asyncio
async def test_install_fails_on_case_insensitive_path_conflicts_OMPE52420(cli_runner, test_folder_url, storage_backend):
    """Test that install detects case-insensitive path conflicts when the destination storage is case-insensitive"""
    package = "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")

    # Create the package normally
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    package_wrapp_file = join_paths(repo_folder, ".packages", package, version, f".{package}.wrapp")
    package_info = await read_package_info(package_wrapp_file)
    assert package_info is not None
    assert package_info.catalog is not None

    original_item = next(item for item in package_info.catalog.items if item.relative_path == "testfile.txt")
    conflicting_item = CatalogItem(
        source_path=original_item.source_path,
        relative_path="Testfile.txt",
        hash=original_item.hash,
        size=original_item.size,
        tags=original_item.tags,
    )
    package_info.catalog.items.append(conflicting_item)
    await write_package_info(package_wrapp_file, package_info)

    # Note: "testfile.txt" and "Testfile.txt" have the same content hash, but we still fail on
    # case-insensitive storage — installing would leave the catalog out of sync with actual files.
    target_folder = join_paths(test_folder_url, "installed_package")
    if wrapp_is_case_sensitive(target_folder):
        await install(package_name=package, version=version, destination=target_folder, repo=repo_folder)
    else:
        with pytest.raises(FailedCommand, match="Cross platform case insensitive path conflicts found"):
            await install(package_name=package, version=version, destination=target_folder, repo=repo_folder)


@pytest.mark.asyncio
async def test_install_fails_on_case_insensitive_dependency_path_conflicts_OMPE52420(cli_runner, test_folder_url, storage_backend):
    dependency_package = "dep" + "".join(random.choices(string.ascii_lowercase, k=8))
    package = "pkg" + "".join(random.choices(string.ascii_lowercase, k=8))
    version = "1.0.0-" + "".join(random.choices(string.ascii_lowercase, k=8))
    repo_folder = join_paths(test_folder_url, ".package_cache")

    dependency_source = await storage_backend.create_source_package(join_paths(test_folder_url, "dependency_source"), prefix="dep_")
    run_wrapp(cli_runner, ["create", dependency_package, version, dependency_source, "--repo", repo_folder])

    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"), prefix="top_")
    await install(dependency_package, version, join_paths(source_package, "dependency"), repo=repo_folder)
    run_wrapp(cli_runner, ["create", package, version, source_package, "--repo", repo_folder])

    dependency_wrapp_file = join_paths(repo_folder, ".packages", dependency_package, version, f".{dependency_package}.wrapp")
    dependency_package_info = await read_package_info(dependency_wrapp_file)
    assert dependency_package_info is not None
    assert dependency_package_info.catalog is not None

    original_item = next(item for item in dependency_package_info.catalog.items if item.relative_path.endswith("testfile.txt"))
    conflicting_relative_path = original_item.relative_path[0].swapcase() + original_item.relative_path[1:]
    conflicting_item = CatalogItem(
        source_path=original_item.source_path,
        relative_path=conflicting_relative_path,
        hash=original_item.hash,
        size=original_item.size,
        tags=original_item.tags,
    )
    dependency_package_info.catalog.items.append(conflicting_item)
    await write_package_info(dependency_wrapp_file, dependency_package_info)

    target_folder = join_paths(test_folder_url, "installed_package")
    dependency_target = join_paths(target_folder, "dependency")
    if wrapp_is_case_sensitive(target_folder):
        await install(package_name=package, version=version, destination=target_folder, repo=repo_folder)
        assert await wrapp_url_exists(join_paths(dependency_target, original_item.relative_path))
        assert await wrapp_url_exists(join_paths(dependency_target, conflicting_relative_path))
    else:
        with pytest.raises(FailedCommand, match="Cross platform case insensitive path conflicts found"):
            await install(package_name=package, version=version, destination=target_folder, repo=repo_folder)
        assert not await wrapp_url_exists(target_folder)


@pytest.mark.asyncio
async def test_install_with_relative_repo_path_after_mirror(wrapp_init, cli_runner, local_tmp_folder):
    """
    Regression test for OMPE-79544: relative --repo paths should work on Windows.

    The bug scenario is:
    1. A package is mirrored into a repository using a relative path as the destination
    2. This creates a package file that stores paths relative to that location
    3. When installing from that repository using a relative path, it fails on Windows

    The command `wrapp install --repo .wrapp_working package version destination` failed on Windows
    but worked on Linux. Using an absolute path like `--repo c:\\path\\.wrapp_working` worked on both.
    """
    # Save current working directory
    original_cwd = os.getcwd()

    try:
        # Change to the temp folder - this simulates the user running wrapp from their project directory
        os.chdir(local_tmp_folder)

        # Create a source package folder with some files
        source_folder = "source_package"
        os.makedirs(source_folder, exist_ok=True)
        with open(os.path.join(source_folder, "testfile.txt"), "w") as f:
            f.write("test content")
        with open(os.path.join(source_folder, "another_file.txt"), "w") as f:
            f.write("more content")

        # First, create a package in a "source" repository (using absolute path)
        source_repo = os.path.abspath(".source_repo")
        package_name = "test_package"
        version = "1.0.0"
        run_wrapp(cli_runner, ["create", package_name, version, source_folder, "--repo", source_repo])

        # Now mirror the package to a destination repository using a RELATIVE path
        # This is the key part of the bug - mirroring with a relative destination path
        relative_dest_repo = ".wrapp_working"
        run_wrapp(
            cli_runner,
            ["mirror", package_name, version, "--source-repo", source_repo, "--destination-repo", relative_dest_repo],
        )

        # Verify the package was mirrored to the relative repo location
        package_wrapp_file = os.path.join(relative_dest_repo, ".packages", package_name, version, f".{package_name}.wrapp")
        assert os.path.exists(package_wrapp_file), f"Mirrored package wrapp file should exist at {package_wrapp_file}"

        # Now install from the mirrored repo using the same relative path via CLI
        # This is the exact scenario from the bug report:
        # wrapp install --repo .wrapp_working cubebox_a01 1.10.0 props/cubebox_a01
        install_target = os.path.join("props", "test_package")
        run_wrapp(cli_runner, ["install", package_name, version, install_target, "--repo", relative_dest_repo])

        # Verify the install succeeded
        assert os.path.exists(install_target), f"Install target folder should exist at {install_target}"
        assert os.path.exists(os.path.join(install_target, "testfile.txt")), "testfile.txt should be installed"
        assert os.path.exists(os.path.join(install_target, "another_file.txt")), "another_file.txt should be installed"

        # Verify the installed package has the correct content
        with open(os.path.join(install_target, "testfile.txt"), "r") as f:
            assert f.read() == "test content"

    finally:
        # Always restore the original working directory
        os.chdir(original_cwd)
