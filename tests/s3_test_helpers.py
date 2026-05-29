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

import asyncio
import logging
import os
import random
import time
from contextlib import contextmanager
from io import BytesIO
from typing import (
    Generator,
    List,
)

import pytest

from tests.storage_test_helpers import (
    StorageBackendForTests,
    StorageTestHelper,
    call_async,
    random_bytes,
)
from wrapp import (
    AuthManager,
    CommandParameters,
)
from wrapp.utils.storage import (
    Router,
    wrapp_stat,
    wrapp_write_file_bytes,
)
from wrapp.utils.storage_interface import (
    Boto3AuthInfo,
    Result,
    StorageAuthInfo,
)
from wrapp.utils.utils import (
    join_paths,
    random_string,
)


class S3TestHelper(StorageTestHelper):
    def storage_type(self) -> StorageBackendForTests:
        return StorageBackendForTests.S3

    def session_initialization(self, test_hosts: List[str]) -> Generator["StorageTestHelper", None, None]:
        created_buckets: List[str] = []
        for bucket_name in test_hosts:
            if call_async(Router.me().s3().create_bucket(bucket_name)):
                created_buckets.append(bucket_name)
        yield self
        # This should work, but I am just doing a bugfix right now. Uncomment at your leisure.
        # if Router.me().s3().s3_client().endpoint():  # Do not try to delete buckets right now on AWS S3, only minio
        #    for bucket_name in created_buckets:
        #        call_async(Router.me().s3().delete_bucket(bucket_name))

    @contextmanager
    def clear_credentials_for_external_process(self, url: str):
        # Nothing needs to be done here - if authentication is provided for an external
        # process via CLI or environment variable, boto3 will ignore any other credentials.
        yield

    def cli_auth_param(self, auth_info: StorageAuthInfo) -> str:
        d = auth_info.auth_data
        assert isinstance(d, Boto3AuthInfo)
        p = f"{auth_info.server_url},{d.aws_access_key_id},{d.aws_secret_access_key}"
        if d.aws_session_token:
            p += f",{d.aws_session_token}"
        return p

    def env_auth_dict(self, auth_info: StorageAuthInfo) -> dict[str, str]:
        d = auth_info.auth_data
        assert isinstance(d, Boto3AuthInfo)
        r = {}
        if d.aws_secret_access_key is not None:
            r["AWS_SECRET_ACCESS_KEY"] = d.aws_secret_access_key
        if d.aws_access_key_id is not None:
            r["AWS_ACCESS_KEY_ID"] = d.aws_access_key_id
        if d.aws_session_token is not None:
            r["AWS_SESSION_TOKEN"] = d.aws_session_token
        return r

    def _invalid_auth_infos(self, auth_info: StorageAuthInfo) -> list[StorageAuthInfo]:
        d = auth_info.auth_data
        assert isinstance(d, Boto3AuthInfo)
        # TODO: how to test for an invalid (or valid for that matter) session token?
        return [
            StorageAuthInfo.boto3(auth_info.server_url, "wrong_aws_access_key_id", d.aws_secret_access_key),
            StorageAuthInfo.boto3(auth_info.server_url, d.aws_access_key_id, "wrong_aws_secret_access_key"),
        ]

    def cli_invalid_auth_data_and_interactive_flag(self, auth_info: StorageAuthInfo) -> list[tuple[str, bool]]:
        d = auth_info.auth_data
        assert isinstance(d, Boto3AuthInfo)
        invalid_auth_params: list[str] = [
            self.cli_auth_param(invalid_auth_info) for invalid_auth_info in self._invalid_auth_infos(auth_info)
        ]
        invalid_auth_params.extend([",", ",,", f",{d.aws_access_key_id},", f",,{d.aws_secret_access_key}", f",,,{d.aws_session_token}"])
        # The boto3 provider does never do interactive login, but nevertheless test it with flag on and off
        invalid_auth_data = [(p, False) for p in invalid_auth_params] + [(p, True) for p in invalid_auth_params]
        return invalid_auth_data

    def env_invalid_auth_data(self, auth_info: StorageAuthInfo) -> list[tuple[dict[str, str], bool]]:
        d = auth_info.auth_data
        assert isinstance(d, Boto3AuthInfo)
        invalid_auth_dicts: list[dict[str, str]] = [
            self.env_auth_dict(invalid_auth_info) for invalid_auth_info in self._invalid_auth_infos(auth_info)
        ]
        invalid_auth_dicts.extend([{"AWS_ACCESS_KEY_ID": d.aws_access_key_id}, {"AWS_SECRET_ACCESS_KEY": d.aws_secret_access_key}, {}])
        # The boto3 provider does never do interactive login, but nevertheless test it with flag on and off
        invalid_auth_data = [(p, False) for p in invalid_auth_dicts] + [(p, True) for p in invalid_auth_dicts]
        return invalid_auth_data

    def skip_if_remote_server_down(self, auth: AuthManager, explicit_server_url: str):
        pass

    async def is_versioned(self, url) -> bool:
        # TODO - we need to actually use boto3 and check the bucket settings
        return False

    async def create_source_package(self, url, tags=False, prefix: str = "", large=False) -> str:
        return await self.create_source_package_for_s3(url, tags, prefix, large)

    @staticmethod
    async def create_source_package_for_s3(url: str, tags=False, prefix: str = "", large=False) -> str:
        if large:
            pytest.skip("This takes waaaay too long on S3.")

        # Create a copy source
        source_package = join_paths(url, f"{prefix}source_package")
        # Create a file
        source_file = join_paths(source_package, f"{prefix}testfile.txt")
        op_write_testfile_txt = (source_file, wrapp_write_file_bytes(CommandParameters(), source_file, random_bytes()))
        subfolder = join_paths(source_package, f"{prefix}subfolder")
        source_file_subfolder = join_paths(subfolder, f"{prefix}file_in_folder.txt")
        op_write_file_in_folder_txt = (
            source_file_subfolder,
            wrapp_write_file_bytes(CommandParameters(), source_file_subfolder, random_bytes()),
        )
        subsubfolder = join_paths(subfolder, f"{prefix}subsubfolder/file_in_subsubfolder.txt")
        op_create_subsubfolder = (subsubfolder, wrapp_write_file_bytes(CommandParameters(), subsubfolder, random_bytes()))
        ops = [op_write_testfile_txt, op_write_file_in_folder_txt, op_create_subsubfolder]

        if large:
            for i in range(100):
                folder = join_paths(source_package, random_string())
                for r in range(random.randrange(1, 5)):
                    folder = join_paths(folder, random_string())
                path = join_paths(folder, "random_file.txt")
                ops.append((path, wrapp_write_file_bytes(CommandParameters(), path, random_bytes())))

        # Add a non-prefixed folder to test folder overlap
        overlapping = join_paths(url, f"overlapping/file_in_overlapping.txt")
        ops.append((overlapping, wrapp_write_file_bytes(CommandParameters(), overlapping, b"overlapping but same content")))

        results = await asyncio.gather(*[o for _, o in ops])
        for i in range(len(results)):
            r = results[i]
            n = ops[i][0]
            assert r == Result.OK, f"Operation for file {n} failed with {r}!"

        return source_package

    async def create_large_file(self, url: str, file_size: int):
        r, e = await wrapp_stat(url)
        if r == Result.OK:
            assert e.size == file_size
        else:
            assert r == Result.ERROR_NOT_FOUND
            logging.info(f"Creating data of size {file_size} for {url}")
            buffer = bytes(file_size)
            # We have to write the file first, we do not
            logging.info(f"Writing data of size {file_size} for {url}")

            total_uploaded = 0
            last_logged_uploaded = 0
            last_time = time.monotonic()
            total_time = 0

            def progress_callback(bytes_copied: int):
                nonlocal total_uploaded
                nonlocal last_time
                nonlocal total_time
                nonlocal last_logged_uploaded
                total_uploaded += bytes_copied
                if total_uploaded // (4 * 1024 * 1024) != last_logged_uploaded // (4 * 1024 * 1024):
                    new_time = time.monotonic()
                    dt = new_time - last_time
                    last_time = new_time
                    total_time += dt
                    logging.info(
                        f"create_large_file: {total_uploaded/(1024*1024)} MB in {total_time:.1f} secs: {total_uploaded/(1024*1024*total_time):.2f} MB/sec. Last interval {(total_uploaded - last_logged_uploaded)/(1024*1024*dt):.2f} MB/s"
                    )
                    last_logged_uploaded = total_uploaded

            assert (
                await Router.me()
                .s3()
                ._upload_fileobj(BytesIO(buffer), len(buffer), "notcalculated", url, False, progress_callback=progress_callback)
                == Result.OK
            ), f"Failed to create 6GB file '{url}'"

    @classmethod
    def env_default_credentials(cls) -> dict[str, str]:
        return {
            "WRAPP_AWS_ACCESS_KEY_ID": os.getenv("WRAPP_AWS_ACCESS_KEY_ID", "dummy_user"),
            "WRAPP_AWS_SECRET_ACCESS_KEY": os.getenv("WRAPP_AWS_SECRET_ACCESS_KEY", "dummy_key"),
        }

    @classmethod
    def default_credentials(cls, server_url: str) -> StorageAuthInfo:
        return StorageAuthInfo.boto3(
            server_url=server_url,
            aws_access_key_id=cls.env_default_credentials()["WRAPP_AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=cls.env_default_credentials()["WRAPP_AWS_SECRET_ACCESS_KEY"],
        )
