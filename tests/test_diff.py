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

# noinspection PyUnresolvedReferences
import wrapp
from wrapp.cli import *
from wrapp.utils.storage import (
    Result,
    wrapp_url_exists,
    wrapp_write_file_bytes,
)

from .common import *


@pytest.mark.asyncio
async def test_diff(cli_runner, test_folder_url, authenticator, local_tmp_folder, source_package):
    # Create the first catalog
    test_catalog_name = os.path.join(local_tmp_folder, "test-catalog.json")
    run_wrapp(cli_runner, ["catalog", test_folder_url, test_catalog_name, "--overwrite"])
    assert await wrapp_url_exists(test_catalog_name)

    # Now run diff on the same catalog, it should not differ
    run_wrapp(cli_runner, ["diff", test_catalog_name, test_catalog_name])

    # Add a file, and modify the existing one
    existing_file = join_paths(test_folder_url, "source_package", "testfile.txt")
    assert await wrapp_write_file_bytes(CommandParameters(), existing_file, b"999\n") == Result.OK
    new_file = join_paths(test_folder_url, "source_package", "added.txt")
    assert await wrapp_write_file_bytes(CommandParameters(), new_file, b"This is new\n") == Result.OK

    # Catalog again
    test_catalog2_name = os.path.join(local_tmp_folder, "test-catalog2.json")
    run_wrapp(cli_runner, ["catalog", test_folder_url, test_catalog2_name, "--overwrite"])
    assert await wrapp_url_exists(test_catalog2_name)

    # Now run diff on the two catalogs, and make sure they differ
    run_wrapp(cli_runner, ["diff", test_catalog_name, test_catalog2_name], 1)

    # Now run diff on the two catalogs, with show and check output
    result = run_wrapp(cli_runner, ["diff", test_catalog_name, test_catalog2_name, "--show"], 1)
    assert "Pair differs" in result.stdout
    assert "Item unique to" in result.stdout

    os.remove(test_catalog_name)
    os.remove(test_catalog2_name)


@pytest.mark.asyncio
async def test_diff_empty_parameters(test_folder_url, authenticator, storage_backend):
    """Test diff() raises FailedCommandValueError for empty string parameters."""
    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.diff(catalog_a="", catalog_b=f"{test_folder_url}/some_catalog.json", ignore_tags=False)
    assert exc_info.value.parameter_name == "catalog_a"
    assert exc_info.value.value == ""

    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.diff(catalog_a=f"{test_folder_url}/some_catalog.json", catalog_b="", ignore_tags=False)
    assert exc_info.value.parameter_name == "catalog_b"
    assert exc_info.value.value == ""
