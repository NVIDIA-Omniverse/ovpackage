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

import io
import tarfile

from wrapp import (
    diff,
    export_package,
    import_package,
)
from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from wrapp.datastructures.package import PackageInfo
from wrapp.utils.storage import (
    Result,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    make_relative_url,
)

from .common import *


@pytest.mark.asyncio
async def test_import(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    source_package = await storage_backend.create_source_package(join_paths(test_folder_url, "package_source"))
    repo_folder = join_paths(test_folder_url, ".package_cache")
    export_file_name = os.path.join(local_tmp_folder, "import_test.1.tar")
    run_wrapp(cli_runner, ["create", "import_test", "1", source_package, "--repo", repo_folder])
    run_wrapp(cli_runner, ["export", "import_test", "1", "--repo", repo_folder, "--overwrite", "--output", export_file_name])

    # now import it into a different place
    import_repo = join_paths(test_folder_url, ".second_package")
    run_wrapp(cli_runner, ["import", export_file_name, "--repo", import_repo])

    assert await wrapp_url_exists(join_paths(import_repo, ".packages", "import_test", "1"))
    import_source_name = os.path.join(local_tmp_folder, "import_source.json")
    run_wrapp(cli_runner, ["catalog", join_paths(repo_folder, ".packages"), import_source_name, "--overwrite"])
    import_result_name = os.path.join(local_tmp_folder, "import_result.json")
    import_result_path = join_paths(import_repo, ".packages")
    run_wrapp(cli_runner, ["catalog", import_result_path, import_result_name, "--overwrite"])
    unique_to_a, unique_to_b, diffs = await diff(import_source_name, import_result_name, ignore_tags=True)
    assert len(unique_to_a) == 0
    assert len(unique_to_b) == 0
    assert len(diffs) == 1
    assert diffs[0][0]["relative_path"] == f"import_test/1/.import_test.wrapp"

    # Now check all source_paths in the imported package point into the imported package
    imported_catalog = await Catalog.load(import_result_name)
    for item in imported_catalog.items:
        assert make_relative_url(import_result_path, item.source_path) != item.source_path

    os.remove(export_file_name)
    os.remove(import_result_name)
    os.remove(import_source_name)


async def create_exported_package(storage_backend, test_folder, local_tmp_folder) -> str:
    source_package = await storage_backend.create_source_package(join_paths(test_folder, "package_source"))
    package_name = "import_test"
    version = "1"
    await create(package_name, version, source_package, catalog=False, repo=test_folder)
    package_file = join_paths(local_tmp_folder, "export_test.tar")
    await export_package(catalog=None, repo=test_folder, package_name=package_name, version=version, output=package_file)
    return package_file


@pytest.mark.skip(reason="This runs into the 2 minute timeout of client library, needs to be fixed")
@pytest.mark.asyncio
async def test_import_into_non_existing_server(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    package_file = await create_exported_package(storage_backend, test_folder_url, local_tmp_folder)
    # Now import into a non-existing server
    run_wrapp(cli_runner, ["import", package_file, "--repo", "omniverse://server_that_doesnt_exist.local"], expected_exit_code=2)


@pytest.mark.asyncio
async def test_import_into_lazily_created_repo(cli_runner, test_folder_url, authenticator, local_tmp_folder, storage_backend):
    package_file = await create_exported_package(storage_backend, test_folder_url, local_tmp_folder)
    # Now import into a non-existing repo
    run_wrapp(cli_runner, ["import", package_file, "--repo", join_paths(test_folder_url, "remote_doesnt_exist")])


@pytest.mark.asyncio
async def test_import_fails_with_empty_tar(test_folder_url, authenticator, local_tmp_folder):
    output = join_paths(local_tmp_folder, "empty.tar")
    with tarfile.open(name=output, mode="w") as tar:
        pass
    with pytest.raises(FailedCommand) as e:
        await import_package(output, test_folder_url)


@pytest.mark.asyncio
async def test_import_fails_with_more_than_one_wrapp_file_in_tar(test_folder_url, authenticator, local_tmp_folder):
    output = join_paths(local_tmp_folder, "too_many_wrapps.tar")
    testdata = join_paths(local_tmp_folder, "testdata.txt")
    with open(testdata, "wt") as test:
        test.write("{}\n")
    with tarfile.open(name=output, mode="w") as tar:
        tar.add(testdata, ".first.wrapp")
        tar.add(testdata, ".second.wrapp")
    with pytest.raises(FailedCommand) as e:
        await import_package(output, test_folder_url)


@pytest.mark.asyncio
async def test_import_fails_when_tar_entry_missing(test_folder_url, authenticator, local_tmp_folder):
    """Test that import raises an error when a catalog item references a file missing from the tar."""
    output = join_paths(local_tmp_folder, "bad_entry.tar")
    # Build a valid .wrapp package whose catalog references a file that does not exist in the tar
    package_info = PackageInfo(
        name="test_pkg",
        version="1",
        catalog=Catalog(
            root="file:/dummy",
            items=[CatalogItem(source_path="file:/dummy/missing.txt", relative_path="missing.txt", hash="abc123", size=0)],
        ),
    )
    wrapp_content = package_info.model_dump_json(indent=2).encode("utf-8")
    with tarfile.open(name=output, mode="w") as tar:
        info = tarfile.TarInfo(name=".test_pkg.wrapp")
        info.size = len(wrapp_content)
        tar.addfile(info, io.BytesIO(wrapp_content))
    with pytest.raises(StorageOperationError, match="Failed to extract"):
        await import_package(output, test_folder_url)
