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
import random
import string

import pytest

from wrapp.commands.apply_patch import apply_patch
from wrapp.datastructures.command_context import WrappScheduler
from wrapp.datastructures.patch import (
    ItemType,
    Patch,
    PatchOpAddTag,
    PatchOpCopy,
    PatchOpCreateFolder,
    PatchOpDelete,
    PatchOpDeleteTag,
)
from wrapp.utils.storage import (
    Result,
    wrapp_stat,
    wrapp_supports_tags,
    wrapp_url_exists,
    wrapp_write_file_bytes,
)
from wrapp.utils.utils import FailedPatchCommand

from .common import *
from .custom_assert import assert_url_exists


def test_create_patch(cli_runner, test_folder_url, authenticator, local_tmp_folder):
    use1 = join_paths(os.path.dirname(__file__), "test_data", "use1.json")
    v1 = join_paths(os.path.dirname(__file__), "test_data", "v1.0.0.json")
    v2 = join_paths(os.path.dirname(__file__), "test_data", "v1.0.1.json")
    run_wrapp(cli_runner, ["create-patch", use1, v2, v1])

    patch_file_name = os.path.join(local_tmp_folder, "".join(random.choices(string.ascii_lowercase, k=8)) + ".json")
    assert not os.path.exists(patch_file_name)
    result = run_wrapp(cli_runner, ["create-patch", use1, v2, v1, f"--patch={patch_file_name}", "--show"])
    assert os.path.exists(patch_file_name)
    assert "Adding" in result.stdout
    os.remove(patch_file_name)

    # Run the inverse patch. This creates conflicts and extras. To get those into the patch file you need to specify --force
    patch_file_name = os.path.join(local_tmp_folder, "".join(random.choices(string.ascii_lowercase, k=8)) + ".json")
    assert not os.path.exists(patch_file_name)
    result = run_wrapp(cli_runner, ["create-patch", v2, use1, v1, f"--patch={patch_file_name}", "--show", "--force"])
    assert "Conflicts" in result.stdout
    assert "Copy" in result.stdout
    assert "Extra" in result.stdout
    os.remove(patch_file_name)


def test_create_patch_with_tags(cli_runner, local_tmp_folder):
    # Skip if tagging client is not available
    try:
        from wrapp.utils.storage_impls.import_tagging_client import (
            g_tagging_client_available,
        )

        if not g_tagging_client_available:
            pytest.skip("Tagging client not available")
    except ImportError:
        pytest.skip("Tagging client not available")

    use1 = join_paths(os.path.dirname(__file__), "test_data", "use1.json")
    v1 = join_paths(os.path.dirname(__file__), "test_data", "v1.0.0.json")
    v2 = join_paths(os.path.dirname(__file__), "test_data", "v1.0.1.json")

    patch_file_name = os.path.join(local_tmp_folder, "".join(random.choices(string.ascii_lowercase, k=8)) + ".json")
    assert not os.path.exists(patch_file_name)
    result = run_wrapp(
        cli_runner, ["create-patch", use1, v2, v1, f"--patch={patch_file_name}", "--show", "--tags", "--overwrite", "--ignore"]
    )
    assert os.path.exists(patch_file_name)
    os.remove(patch_file_name)
    assert "Adding" in result.stdout
    assert "Conflict: Tag" in result.stdout


@pytest.mark.asyncio
async def test_apply(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "source"))
    target_package = join_paths(test_folder_url, "target")
    install_destination = join_paths(target_package, "worktree_source")
    repo_folder = test_folder_url

    # Creating version one of source1
    run_wrapp(cli_runner, ["create", "source", "1", source_package, "--repo", repo_folder])

    # Cataloging it
    test_catalog1_name = os.path.join(local_tmp_folder, "test-catalog.json")
    run_wrapp(cli_runner, ["catalog", source_package, test_catalog1_name, "--overwrite"])

    # Installing version 1 into our target
    run_wrapp(cli_runner, ["install", "source", "1", install_destination, "--repo", repo_folder])

    # Modify package 1 by adding a file and creating version 2
    added_file = "added_file.txt"
    new_file = join_paths(source_package, added_file)
    result = await wrapp_write_file_bytes(CommandParameters(), new_file, b"New content", "file added after version 1")
    assert result == Result.OK
    run_wrapp(cli_runner, ["create", "source", "2", source_package, "--repo", repo_folder])

    # Now, create a patch file that will update target to version 2 when applied
    installed_catalog_name = os.path.join(local_tmp_folder, "installed.json")
    run_wrapp(cli_runner, ["catalog", install_destination, installed_catalog_name, "--overwrite"])
    version2_catalog_name = os.path.join(local_tmp_folder, "version2.json")
    run_wrapp(cli_runner, ["catalog", source_package, version2_catalog_name, "--overwrite"])
    patch_file = os.path.join(local_tmp_folder, "patch.file")
    # We need to ignore the source.toml conflict, so specify ignore
    run_wrapp(
        cli_runner,
        [
            "create-patch",
            installed_catalog_name,
            version2_catalog_name,
            test_catalog1_name,
            "--patch",
            patch_file,
            "--overwrite",
            "--ignore",
        ],
    )
    assert_url_exists(patch_file)

    # Now apply that patch
    run_wrapp(cli_runner, ["apply-patch", patch_file])
    assert_url_exists(join_paths(install_destination, added_file))

    # And run it with wrong patch file name to trigger the error handling code as well
    run_wrapp(cli_runner, ["apply-patch", patch_file + "non_existing"], 1)

    os.remove(installed_catalog_name)
    os.remove(patch_file)
    os.remove(version2_catalog_name)
    os.remove(test_catalog1_name)


@pytest.mark.asyncio
async def test_apply_with_tags(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "source"))
    target_package = join_paths(test_folder_url, "target")
    install_destination = join_paths(target_package, "worktree_source")
    repo_folder = test_folder_url
    src_supports_tags = await wrapp_supports_tags(source_package)
    dest_supports_tags = await wrapp_supports_tags(install_destination)
    # This test is currently only implemented for the case that both storages support tags.
    if not src_supports_tags and not dest_supports_tags:
        pytest.skip("This test makes no sense if neither source nor target support tags.")

    # Connect to tagging service, and tag testfile.txt
    testsource = join_paths(source_package, "testfile.txt")
    testtarget = join_paths(install_destination, "testfile.txt")
    await add_tag_async(testsource, "tagged", "with 1", "appearance")
    await add_tag_async(testsource, "tagged 2", "with 1", "appearance")

    # Creating version one of source1
    run_wrapp(cli_runner, ["create", "source", "1", source_package, "--repo", repo_folder, "--tags"])

    # Cataloging it
    test_catalog_name = os.path.join(local_tmp_folder, "version1-with-tags-catalog.json")
    run_wrapp(cli_runner, ["catalog", source_package, test_catalog_name, "--overwrite", "--tags"])

    # Installing version 1 into our target
    run_wrapp(cli_runner, ["install", "source", "1", install_destination, "--repo", repo_folder, "--tags"])

    # Modify package 1 by adding a tag, deleting another and creating version 2
    await add_tag_async(testsource, "tagged too", "with 2", "appearance")
    await delete_tag_async(testsource, "tagged 2", "appearance")
    run_wrapp(cli_runner, ["create", "source", "2", source_package, "--repo", repo_folder, "--tags"])

    # Now, create a patch file that will update target to version 2 when applied
    installed_catalog_name = os.path.join(local_tmp_folder, "installed-with-tags.json")
    run_wrapp(cli_runner, ["catalog", install_destination, installed_catalog_name, "--overwrite", "--tags"])
    version2_catalog_name = os.path.join(local_tmp_folder, "version2-with-tags.json")
    run_wrapp(cli_runner, ["catalog", source_package, version2_catalog_name, "--overwrite", "--tags"])
    patch_file = os.path.join(local_tmp_folder, "patch-with-tags.file")
    # We need to ignore the source.toml conflict, so specify ignore
    run_wrapp(
        cli_runner,
        [
            "create-patch",
            installed_catalog_name,
            version2_catalog_name,
            test_catalog_name,
            "--patch",
            patch_file,
            "--overwrite",
            "--ignore",
            "--tags",
        ],
    )
    assert await wrapp_url_exists(patch_file)

    # Now apply that patch
    run_wrapp(cli_runner, ["apply-patch", patch_file])
    tags = await get_tags_async(testtarget)
    assert len(tags["path_result"][0]["tags"]) == 2

    os.remove(installed_catalog_name)
    os.remove(patch_file)
    os.remove(test_catalog_name)
    os.remove(version2_catalog_name)


@pytest.mark.asyncio
async def test_patch_load_v0():
    patch_v0_file_path = join_paths(os.path.dirname(__file__), "test_data", "patch_v0.json")
    patch = Patch()
    await patch.load(patch_v0_file_path)
    ops_list = list(patch.ops.operations)
    # Make sure that the load operation does sort the patch operations as expected.
    ops_list.sort()
    assert ops_list == patch.ops.operations, f"Sort order jobs in loaded v0 patch differs from explicitly sorted job list."


@pytest.mark.asyncio
async def test_patch_error_handling(wrapp_init, local_tmp_folder):
    command_params = CommandParameters()

    invalid_src_path = join_paths(local_tmp_folder, "invalid_src")
    dst_path = join_paths(local_tmp_folder, "dst")
    broken_patch = Patch()
    tag_json = json.dumps({"name": "MyTag", "value": "MyTagValue", "ns": "namespace"})
    broken_ops = [
        PatchOpCopy(src=invalid_src_path, dst=dst_path, item_type=ItemType.file),
        PatchOpDelete(url=invalid_src_path),
        # No easy way to make PatchOpCreateFolder fail on Linux?
        PatchOpAddTag(url=invalid_src_path, tag=tag_json),
        PatchOpDeleteTag(url=invalid_src_path, tag=tag_json),
    ]
    # This always succeeds even if executed several times
    working_op = PatchOpCreateFolder(url=dst_path)
    for op in broken_ops:
        broken_patch.add(op)
    # Add one working op
    broken_patch.add(working_op)

    working_patch = Patch()
    working_patch.add(working_op)

    async with WrappScheduler() as scheduler:
        # Executing a patch with broken ops fails (even if one of the ops succeeds)
        assert not await broken_patch.execute(CommandParameters(), scheduler)
        # Successful patch execution succeeds
        assert await working_patch.execute(CommandParameters(), scheduler)
        # Empty patch execution succeeds
        assert await Patch().execute(CommandParameters(), scheduler)

    assert all([broken_patch.get_error_msg(op) for op in broken_ops])
    assert not broken_patch.get_error_msg(working_op)


@pytest.mark.asyncio
async def test_apply_patch_with_error(cli_runner, wrapp_init, local_tmp_folder):
    # Build a patch which will force an error, and then still have a valid operation
    invalid_src_path = join_paths(local_tmp_folder, "invalid_src")
    valid_src_path = join_paths(local_tmp_folder, "file.txt")
    with open(valid_src_path, "wt") as f:
        f.write("some file")
    valid_dst_path = join_paths(local_tmp_folder, "dst/file.txt")
    patch_path = join_paths(local_tmp_folder, "patch.json")
    patch_with_error = Patch()
    patch_with_error.add(PatchOpDelete(url=invalid_src_path))
    patch_with_error.add(PatchOpCopy(src=valid_src_path, dst=valid_dst_path, item_type=ItemType.file))
    await patch_with_error.save(CommandParameters(), patch_path)

    run_wrapp(cli_runner, ["apply-patch", patch_path], 1)
    with pytest.raises(FailedPatchCommand) as e:
        await apply_patch(patch_path)
    assert len(e.value.patch.get_errors()) == 1
    assert isinstance(e.value.patch.get_errors()[0][0], PatchOpDelete)
    assert e.value.patch.get_errors()[0][0].url == invalid_src_path
    assert e.value.patch.get_errors()[0][1], f"Expected an error message"
    assert (await wrapp_stat(valid_dst_path))[0] == Result.OK


@pytest.mark.asyncio
async def test_apply_patch_alternate_storage(
    cli_runner,
    test_folder_url,
    authenticator,
    local_tmp_folder,
    storage_backend,
    source_package,
    alternate_storage_source_package,
    alternate_storage_test_folder_url,
):
    # Cataloging on the alternate storage
    test_catalog1_name = os.path.join(local_tmp_folder, "test-catalog-alternate.json")
    run_wrapp(cli_runner, ["catalog", alternate_storage_source_package, test_catalog1_name, "--overwrite"])

    # Cataloging on the normal storage after modifying it
    test_catalog2_name = os.path.join(local_tmp_folder, "test-catalog.json")
    # Modify the source_package by adding a file
    added_file = "added_file.txt"
    new_file = join_paths(source_package, added_file)
    result = await wrapp_write_file_bytes(CommandParameters(), new_file, b"New content", "file added after version 1")
    assert result == Result.OK
    run_wrapp(cli_runner, ["catalog", source_package, test_catalog2_name, "--overwrite"])

    # Now, try to create a patch file from catalogs from two different storages
    patch_file = os.path.join(local_tmp_folder, "patch.file")
    # We need to ignore the source.toml conflict, so specify ignore
    run_wrapp(
        cli_runner,
        [
            "create-patch",
            test_catalog1_name,
            test_catalog2_name,
            test_catalog1_name,
            "--patch",
            patch_file,
            "--overwrite",
            "--ignore",
        ],
    )
    assert_url_exists(patch_file)

    # Now apply that patch
    run_wrapp(cli_runner, ["apply-patch", patch_file])
