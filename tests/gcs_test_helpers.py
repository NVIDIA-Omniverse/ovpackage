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
import tempfile
from contextlib import contextmanager
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
    GCSAuthInfo,
    Result,
    StorageAuthInfo,
)
from wrapp.utils.utils import (
    join_paths,
    random_string,
)


class GCSTestHelper(StorageTestHelper):
    def __init__(self):
        # Save values at construction time so we can restore them even after
        # clear_credentials_for_external_process has removed them from os.environ.
        self._gcs_endpoint = os.environ.get("WRAPP_GCS_ENDPOINT", "")
        self._google_adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
        self._cloudsdk_config = os.environ.get("CLOUDSDK_CONFIG", "")

    def storage_type(self) -> StorageBackendForTests:
        return StorageBackendForTests.GCS

    def session_initialization(self, test_hosts: List[str]) -> Generator["StorageTestHelper", None, None]:
        created_buckets: List[str] = []
        for bucket_name in test_hosts:
            try:
                # Get the GCS client
                gcs_client = Router.me().gcs()

                call_async(gcs_client.create_bucket_if_not_exists(bucket_name))

                created_buckets.append(bucket_name)
                logging.info(f"Created test bucket: {bucket_name}")
            except Exception as e:
                logging.error(f"Error creating test bucket {bucket_name}: {e}")

        yield self

    @contextmanager
    def clear_credentials_for_external_process(self, url: str):
        # Save and remove GCS credential environment variables.
        # Note: WRAPP_GCS_ENDPOINT is NOT removed - it's config, not a credential.
        original_env = {}
        gcs_env_vars = [
            "WRAPP_GCS_PROJECT",
            "WRAPP_GCS_CREDENTIALS_FILE",
            "GOOGLE_APPLICATION_CREDENTIALS",
        ]

        for var in gcs_env_vars:
            if var in os.environ:
                original_env[var] = os.environ[var]
                del os.environ[var]

        # Redirect CLOUDSDK_CONFIG to an empty temp directory so that
        # google.auth.default() cannot discover the gcloud CLI ADC file
        # (~/.config/gcloud/application_default_credentials.json).
        had_cloudsdk = "CLOUDSDK_CONFIG" in os.environ
        old_cloudsdk = os.environ.get("CLOUDSDK_CONFIG")

        tmpdir = tempfile.mkdtemp(prefix="wrapp_test_no_gcloud_")
        os.environ["CLOUDSDK_CONFIG"] = tmpdir

        try:
            yield
        finally:
            for var, value in original_env.items():
                os.environ[var] = value

            if had_cloudsdk:
                os.environ["CLOUDSDK_CONFIG"] = old_cloudsdk
            else:
                os.environ.pop("CLOUDSDK_CONFIG", None)

            try:
                os.rmdir(tmpdir)
            except OSError:
                pass

    def cli_auth_param(self, auth_info: StorageAuthInfo) -> str:
        d = auth_info.auth_data
        assert isinstance(d, GCSAuthInfo)

        if d.credentials_file:
            return f"{auth_info.server_url},{d.credentials_file}"
        elif d.credentials_json:
            return f"{auth_info.server_url},{d.credentials_json}"
        else:
            return f"{auth_info.server_url},"

    def env_auth_dict(self, auth_info: StorageAuthInfo) -> dict[str, str]:
        d = auth_info.auth_data
        assert isinstance(d, GCSAuthInfo)

        r = {}
        if d.project_id:
            r["WRAPP_GCS_PROJECT"] = d.project_id
        if d.credentials_file:
            r["WRAPP_GCS_CREDENTIALS_FILE"] = d.credentials_file
        elif self._google_adc:
            r["GOOGLE_APPLICATION_CREDENTIALS"] = self._google_adc
        else:
            # Credentials come from gcloud CLI ADC file.  Restore
            # CLOUDSDK_CONFIG so the subprocess can discover the
            # well-known ADC file that clear_credentials_for_external_process
            # blocks via a temp-dir redirect.
            if self._cloudsdk_config:
                r["CLOUDSDK_CONFIG"] = self._cloudsdk_config
            else:
                r["CLOUDSDK_CONFIG"] = os.path.join(os.path.expanduser("~"), ".config", "gcloud")

        return r

    def _invalid_auth_infos(self, auth_info: StorageAuthInfo) -> list[StorageAuthInfo]:
        d = auth_info.auth_data
        assert isinstance(d, GCSAuthInfo)

        invalid_auth_infos = []

        # Invalid credentials file
        invalid_auth_infos.append(
            StorageAuthInfo(
                auth_info.server_url,
                GCSAuthInfo(credentials_file="/nonexistent/path/to/credentials.json"),
            )
        )

        return invalid_auth_infos

    def cli_invalid_auth_data_and_interactive_flag(self, auth_info: StorageAuthInfo) -> list[tuple[str, bool]]:
        d = auth_info.auth_data
        assert isinstance(d, GCSAuthInfo)

        invalid_auth_params: list[str] = [
            self.cli_auth_param(invalid_auth_info) for invalid_auth_info in self._invalid_auth_infos(auth_info)
        ]

        # Add some malformed parameters
        invalid_auth_params.extend([",", ",,"])

        # GCS provider does not do interactive login, but test with flag on and off
        invalid_auth_data = [(p, False) for p in invalid_auth_params] + [(p, True) for p in invalid_auth_params]
        return invalid_auth_data

    def env_invalid_auth_data(self, auth_info: StorageAuthInfo) -> list[tuple[dict[str, str], bool]]:
        d = auth_info.auth_data
        assert isinstance(d, GCSAuthInfo)

        invalid_auth_dicts: list[dict[str, str]] = [
            self.env_auth_dict(invalid_auth_info) for invalid_auth_info in self._invalid_auth_infos(auth_info)
        ]

        # Note: We don't add an empty dict case here because fake-gcs-server does not enforce authentication,
        # so "no credentials" would succeed rather than fail.

        # GCS provider does not do interactive login, but test with flag on and off
        invalid_auth_data = [(p, False) for p in invalid_auth_dicts] + [(p, True) for p in invalid_auth_dicts]
        return invalid_auth_data

    def skip_if_remote_server_down(self, auth: AuthManager, explicit_server_url: str):
        pass

    async def create_source_package(self, url, tags=False, prefix: str = "", large=False) -> str:
        return await self.create_source_package_for_gcs(url, tags, prefix, large)

    async def is_versioned(self, url) -> bool:
        return False

    @staticmethod
    async def create_source_package_for_gcs(url: str, tags=False, prefix: str = "", large=False) -> str:
        if large:
            pytest.skip("This takes too long on Google Cloud Storage.")

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
            for _ in range(100):
                folder = join_paths(source_package, random_string())
                for _ in range(random.randrange(1, 5)):
                    folder = join_paths(folder, random_string())
                path = join_paths(folder, "random_file.txt")
                ops.append((path, wrapp_write_file_bytes(CommandParameters(), path, random_bytes())))

        # Add a non-prefixed folder to test folder overlap
        overlapping = join_paths(url, "overlapping/file_in_overlapping.txt")
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

            logging.info(f"Writing data of size {file_size} for {url}")

            # Use the write_file method to upload the large file
            result = await Router.me().gcs().write_file(url, buffer, "")
            assert result == Result.OK, f"Failed to create large file '{url}'"

    @classmethod
    def env_default_credentials(cls) -> dict[str, str]:
        """Return environment variable dict for default GCS credentials.

        Note: WRAPP_GCS_ENDPOINT is NOT included - it's config, not a credential.
        It stays in the system env for all subprocess calls.
        """
        r = {}
        project_id = os.getenv("WRAPP_GCS_PROJECT")
        credentials_file = os.getenv("WRAPP_GCS_CREDENTIALS_FILE")
        google_adc = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

        if project_id:
            r["WRAPP_GCS_PROJECT"] = project_id
        if credentials_file:
            r["WRAPP_GCS_CREDENTIALS_FILE"] = credentials_file
        if google_adc:
            r["GOOGLE_APPLICATION_CREDENTIALS"] = google_adc

        return r

    @classmethod
    def default_credentials(cls, server_url: str) -> StorageAuthInfo:
        env_creds = cls.env_default_credentials()

        project_id = env_creds.get("WRAPP_GCS_PROJECT")
        credentials_file = env_creds.get("WRAPP_GCS_CREDENTIALS_FILE")

        return StorageAuthInfo(
            server_url=server_url,
            auth_data=GCSAuthInfo(project_id=project_id, credentials_file=credentials_file),
        )
