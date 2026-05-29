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

import collections.abc
import contextlib
import os
import platform
import subprocess
import sys
import tempfile
from typing import (
    List,
    Optional,
)
from urllib.parse import urlparse

import pytest
from pydantic import BaseModel

import wrapp.commands
from tests.storage_test_helpers import (
    StorageBackendForTests,
    StorageTestHelper,
    join_paths,
)
from wrapp.app import app
from wrapp.commands.create import create
from wrapp.datastructures.command_context import (
    CommandParameters,
)
from wrapp.datastructures.package import (
    PackageInfo,
    write_package_info,
)
from wrapp.tagging_service import tagging
from wrapp.utils.cli_progress_printer import CLIProgressPrinter


# Get for all providers the default credentials as environment variables
def env_default_credentials() -> dict[str, str]:
    # Make sure that no additional providers have been added
    assert len(StorageBackendForTests) == 7

    default_credentials = {}

    # ClientLibTestHelper requires omni.client which may not be installed
    try:
        from tests.clientlib_test_helpers import ClientLibTestHelper

        default_credentials.update(ClientLibTestHelper.env_default_credentials())  # StorageBackendForTests.NUCLEUS
    except ImportError:
        # Nucleus not available, use default credentials directly
        default_credentials.update({"OMNI_USERNAME": "omniverse", "OMNI_PASSWORD": "omniverse"})

    # S3TestHelper requires aioboto3 which may not be installed
    try:
        from tests.s3_test_helpers import S3TestHelper

        default_credentials.update(S3TestHelper.env_default_credentials())  # StorageBackendForTests.S3
    except ImportError:
        pass

    # AzureBlobStorageTestHelper requires azure-storage-blob which may not be installed
    try:
        from tests.azure_blob_storage_test_helpers import AzureBlobStorageTestHelper

        default_credentials.update(AzureBlobStorageTestHelper.env_default_credentials())  # StorageBackendForTests.Azure
    except ImportError:
        pass

    # GCSTestHelper requires google-cloud-storage which may not be installed
    try:
        from tests.gcs_test_helpers import GCSTestHelper

        default_credentials.update(GCSTestHelper.env_default_credentials())  # StorageBackendForTests.GCS
    except ImportError:
        pass

    # StorageBackendForTests.LOCAL_DIR has no default credentials (and is handled by ClientLibTestHelper)
    # StorageBackendForTests.STORAGEAPI has no default credentials
    return default_credentials


def run_wrapp_in_separate_process(
    args: List[str],
    expected_return_codes: int | List[int],
    env: Optional[collections.abc.Mapping[str, str]] = None,
    add_default_credentials=True,
    use_system_env=True,
    cwd: Optional[str] = None,
) -> str:
    # We cannot use CliRunner here - it does not spawn a separate process,
    # so once omni.client is loaded into the process, OMNICLIENT_USE_HUB is ignored.
    cmd = [sys.executable, "-m", "wrapp.cli"] + args
    print(f"Running wrapp in separate process: 'wrapp {' '.join(args)}' using env={env}, cwd={cwd}")
    complete_env = os.environ.copy()
    if not use_system_env:
        complete_env.clear()

    # Make sure that the default credentials are either set in the environment, or removed from it.
    if add_default_credentials:
        complete_env.update(env_default_credentials())
    else:
        complete_env = {
            env_key: env_value for env_key, env_value in complete_env.items() if env_key not in env_default_credentials().keys()
        }

    if env:
        complete_env.update(env)
    result: subprocess.CompletedProcess = subprocess.run(
        cmd, capture_output=True, env={**complete_env, "PYTHONIOENCODING": "utf-8"}, cwd=cwd
    )
    output = result.stdout.decode("utf-8")
    list_of_expected_return_codes: List[int] = [expected_return_codes] if isinstance(expected_return_codes, int) else expected_return_codes
    if result.returncode not in list_of_expected_return_codes:
        raise Exception(
            f"Unexpected return code {result.returncode} in {result},\noutput was {output}\nand stderr:\n{result.stderr.decode('utf-8')}"
        )
    return output


def run_wrapp_without_hub(args: List[str], expected_return_code: int = 0):
    return run_wrapp_in_separate_process(args, expected_return_code, env={"OMNICLIENT_HUB_MODE": "disabled"})


def run_wrapp_with_hub(args: List[str], expected_return_code: int = 0):
    with tempfile.TemporaryDirectory() as cache_dir_path:
        hub_path = os.path.normpath(
            join_paths(__file__, "../../_deps/omni-hub/target/release/", "hub.exe" if platform.system() == "Windows" else "hub")
        )
        if not os.path.exists(hub_path):
            pytest.skip(f"Cannot run run_wrapp_with_hub - there is not hub at {hub_path}")
        env = {"OMNICLIENT_HUB_MODE": "exclusive", "OMNICLIENT_HUB_CACHE_DIR": str(cache_dir_path), "OMNICLIENT_HUB_EXE": hub_path}
        return run_wrapp_in_separate_process(args, expected_return_code, env=env)


def run_wrapp(cli_runner, params: list, expected_exit_code=0):
    print(f"Running wrapp {' '.join(params)}")
    # This is sneaky - the tests run several at once in the same process. Therefore the progress output of all
    # tests get mixed up in the same stdout. This messes with the test capturing the stdout (like test_ls).
    # TODO: We cannot use the progress report option, as that is oddly enough not universal (it should be).
    CLIProgressPrinter.disable_progress_output()
    result = cli_runner.invoke(app, params, catch_exceptions=False, standalone_mode=False)
    # Oddly, if not run in standalone_mode, the exit code will be returned in return_value if Typer.Exit(..) is raised.
    exit_code = result.return_value if result.return_value != None else result.exit_code
    if exit_code != expected_exit_code:
        raise AssertionError(f"run_wrapp received exit code {exit_code}. Expected was {expected_exit_code}!")
    return result


class PackageLocation(BaseModel):
    name: str
    version: Optional[str]
    path: str
    package_file: Optional[str] = None


class TarfileLocation(BaseModel):
    name: str
    version: str
    tar_file: str


async def generate_named_package(
    storage_backend: StorageTestHelper, package_name: str, package_version: str, package_path: str, prefix: str = "", create_catalog=False
) -> PackageLocation:
    """generate a user package without creating it, first new, then add some content"""
    package_file = join_paths(package_path, f".{package_name}.wrapp")
    inner_data_path = await storage_backend.create_source_package(
        package_path, False, prefix=prefix
    )  # This points to the source_package subdirectory. This probably should be renamed
    await wrapp.commands.new(package_name, package_version, package_path, create_catalog)
    return PackageLocation(name=package_name, version=package_version, path=package_path, package_file=package_file)


async def create_unnamed_package(
    storage_backend: StorageTestHelper, package_name: str, package_version: str, folder: str, repo_folder: str, prefix: str = ""
) -> PackageLocation:
    """This creates a classical package, which has no wrapp file on its own because it is a leaf in the tree"""
    source = await storage_backend.create_source_package(folder, False, prefix=prefix)
    await create(package_name, package_version, folder, catalog=False, repo=repo_folder)
    return PackageLocation(name=package_name, version=package_version, path=folder)


async def generate_catalog(folder: str, package_name: str, version: str) -> PackageLocation:
    package = PackageInfo(name=package_name, version=version)
    package_file = join_paths(folder, f".{package_name}.wrapp")
    await write_package_info(package_file, package)
    return PackageLocation(name=package_name, version=version, path=folder, package_file=package_file)


@contextlib.asynccontextmanager
async def connect_tagging_service(url):
    parsed = urlparse(url)
    ts = tagging.Tagging(CommandParameters(), parsed.netloc)
    await ts.connect(url)
    try:
        yield ts
    finally:
        await ts.disconnect()


async def add_tag_async(url, tag_name, tag_value, tag_namespace):
    async with connect_tagging_service(url) as ts:
        parsed = urlparse(url)
        await ts.set_tag(parsed.path, tag_name, tag_value, tag_namespace)


async def delete_tag_async(url, tag_name, tag_namespace):
    async with connect_tagging_service(url) as ts:
        parsed = urlparse(url)
        await ts.delete_tag(parsed.path, tag_name, tag_namespace)


async def get_tags_async(url):
    async with connect_tagging_service(url) as ts:
        parsed = urlparse(url)
        return await ts.get_tags([parsed.path])
