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

import wrapp
from tests.common import run_wrapp
from wrapp.utils.storage import (
    wrapp_read_text_file,
    wrapp_url_exists,
)
from wrapp.utils.utils import join_paths


@pytest.mark.asyncio
async def test_new(cli_runner, authenticator, test_folder_url):
    package = "".join(random.choices(string.ascii_lowercase, k=32))
    version = "".join(random.choices(string.ascii_lowercase, k=8))

    # Run with "--tags" just to make sure the flag does not cause trouble
    # on storages not supporting tags. Like "create" and "catalog" we can
    # ignore the flag in that case.
    from wrapp.utils.storage import g_tagging_client_available

    if g_tagging_client_available:
        run_wrapp(cli_runner, ["new", package, version, test_folder_url, "--tags"], 0)
    else:
        run_wrapp(cli_runner, ["new", package, version, test_folder_url], 0)
    target_file = join_paths(test_folder_url, f".{package}.wrapp")
    assert await wrapp_url_exists(target_file)

    written_toml = await wrapp_read_text_file(target_file)
    package_created = json.loads(written_toml)
    assert package_created["name"] == package
    assert package_created["version"] == version

    # Make sure we cannot create it twice
    run_wrapp(cli_runner, ["new", package, version, test_folder_url], 1)

    # Make sure we cannot create invalid names
    run_wrapp(cli_runner, ["new", "this/is/not/allowed", version, target_file], 1)
    run_wrapp(cli_runner, ["new", "neither is this", version, target_file], 1)
    run_wrapp(cli_runner, ["new", "or\\this", version, target_file], 1)
    run_wrapp(cli_runner, ["new", "no*wildcards", version, target_file], 1)
    run_wrapp(cli_runner, ["new", "because?theydontmakegoodfilenames", version, target_file], 1)


@pytest.mark.asyncio
async def test_new_empty_parameters(test_folder_url, authenticator, storage_backend):
    """Test new() raises FailedCommandValueError for empty string parameters."""
    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.new(package_name="test_package", version="1.0.0", destination="", create_catalog=False)
    assert exc_info.value.parameter_name == "destination"
    assert exc_info.value.value == ""

    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.new(package_name="", version="1.0.0", destination=test_folder_url, create_catalog=False)
    assert exc_info.value.parameter_name == "package_name"
    assert exc_info.value.value == ""

    with pytest.raises(wrapp.FailedCommandValueError) as exc_info:
        await wrapp.new(package_name="test_package", version="", destination=test_folder_url, create_catalog=False)
    assert exc_info.value.parameter_name == "version"
    assert exc_info.value.value == ""
