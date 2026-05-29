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

import contextlib
import datetime
import functools
import http.server
import logging
import os
import random
import shutil
import string
import sys
import tempfile
import threading
import traceback
from typing import Generator

import pytest
import pytest_asyncio
from typer.testing import CliRunner

from tests.storage_test_helpers import (
    StorageBackendForTests,
    StorageTestHelper,
    join_paths,
)
from wrapp.datastructures.authentication import (
    AuthManager,
    StorageAuthInfo,
)
from wrapp.utils.router import (
    g_aws_enabled,
    g_azure_enabled,
    g_gcs_enabled,
    g_local_file_storage_enabled,
    g_nucleus_enabled,
    g_storage_api_enabled,
)
from wrapp.utils.storage import (
    wrapp_initialize,
    wrapp_shutdown,
)
from wrapp.utils.utils import random_string

_storage_initialized = False


# Check actual availability of storage backends (not just config flags)
def _check_nucleus_available():
    """Nucleus is not part of the public ovpackage test matrix."""
    return False


def _check_s3_available():
    """Check if S3/aioboto3 is actually available."""
    if not g_aws_enabled:
        return False
    try:
        import aioboto3

        return True
    except ImportError:
        return False


def _check_azure_available():
    """Check if Azure Blob Storage is actually available."""
    if not g_azure_enabled:
        return False
    try:
        import azure.storage.blob.aio

        return True
    except ImportError:
        return False


def _check_gcs_available():
    """Check if Google Cloud Storage is actually available.

    Requires the SDK to be installed AND either explicit credentials or a
    custom endpoint (e.g. fake-gcs-server) to be configured.  Without
    credentials the google-cloud-storage SDK tries to reach the GCE
    metadata server which hangs on non-GCE machines (especially Windows).
    """
    if not g_gcs_enabled:
        return False
    try:
        import google.cloud.storage
    except ImportError:
        return False
    # Only enable GCS tests when credentials or a local emulator are available
    has_credentials = bool(
        os.environ.get("WRAPP_GCS_CREDENTIALS_FILE")
        or os.environ.get("WRAPP_GCS_ENDPOINT")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )
    return has_credentials


# All session scoped fixtures must be in this file - otherwise they will be executed more than once even
# in the same session


# This must be an unparametrized fixture - otherwise the fixture is setup and torn down for each parameter value,
# which does not work: wrapp_shutdown can only be called once.
@pytest.fixture(scope="session")
def wrapp_init():
    global _storage_initialized
    if _storage_initialized:
        pytest.fail("Storage can only be initialized once - once client-library is shut down, it cannot be reinitialized!")
    _storage_initialized = True
    from wrapp._version import __version__

    try:
        logging.info("Initializing WRAPP....")
        wrapp_initialize(__version__, False)
        logging.info("....WRAPP initialized.")
        yield "wrapp initialized"
    except Exception:
        pytest.fail(f"Failed to initialize storage: {traceback.format_exc()}")
    finally:
        logging.info("Shutting down WRAPP....")
        try:
            wrapp_shutdown(*sys.exc_info(), stop_timeout=30.0)
        except RuntimeError as e:
            logging.warning("Error during shutdown: %s", e)
        logging.info("....WRAPP shut down.")


all_storage_backends = []

# Check actual availability, not just config flags
if _check_nucleus_available():
    all_storage_backends.append(StorageBackendForTests.NUCLEUS)

if g_local_file_storage_enabled and _check_nucleus_available():
    # LOCAL_DIR uses omni.client under the hood via ClientLibTestHelper
    all_storage_backends.append(StorageBackendForTests.LOCAL_DIR)

if _check_s3_available():
    all_storage_backends.append(StorageBackendForTests.S3)

if _check_azure_available():
    all_storage_backends.append(StorageBackendForTests.AZURE_BLOB_STORE)

if _check_gcs_available():
    all_storage_backends.append(StorageBackendForTests.GCS)


def _check_storageapi_available():
    """Check if Storage API is enabled, its deps are installed, and an endpoint
    or discovery URL is configured. Mirrors the production behavior: the
    backend stays inert (claims no URLs, never tries to connect) when neither
    WRAPP_STORAGE_API_GRPC_ADDRESS nor WRAPP_STORAGE_API_DISCOVERY_URL is set,
    so STORAGEAPI test variants would have nothing to talk to."""
    if not g_storage_api_enabled:
        return False
    try:
        import grpc  # noqa: F401
    except ImportError:
        return False
    return bool(os.environ.get("WRAPP_STORAGE_API_GRPC_ADDRESS") or os.environ.get("WRAPP_STORAGE_API_DISCOVERY_URL"))


if _check_storageapi_available():
    all_storage_backends.extend([StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE])


@pytest.fixture(scope="session", params=all_storage_backends)
def storage_backend_type(request) -> StorageBackendForTests:
    return request.param


@pytest.fixture(scope="session", params=all_storage_backends)
def alternate_storage_backend_type(request, storage_backend_type: StorageBackendForTests) -> StorageBackendForTests:
    if request.param.name == storage_backend_type.name:
        pytest.skip("Skipping test where alternate type would be equal storage type")
    # TODO - disable only while S3 and Storage API can not be activated at the same time
    if (
        storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]
        and request.param == StorageBackendForTests.S3
    ):
        pytest.skip("Can't test S3 and StorageAPI at the same time")
    if (
        request.param in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]
        and storage_backend_type == StorageBackendForTests.S3
    ):
        pytest.skip("Can't test S3 and StorageAPI at the same time")
    if (
        storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]
        and request.param == StorageBackendForTests.AZURE_BLOB_STORE
    ):
        pytest.skip("Can't test Azure Blob Store and StorageAPI at the same time")
    if (
        request.param in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]
        and storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE
    ):
        pytest.skip("Can't test Azure Blob Store and StorageAPI at the same time")
    if (
        storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]
        and request.param == StorageBackendForTests.GCS
    ):
        pytest.skip("Can't test GCS and StorageAPI at the same time")
    if (
        request.param in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]
        and storage_backend_type == StorageBackendForTests.GCS
    ):
        pytest.skip("Can't test GCS and StorageAPI at the same time")
    return request.param


@contextlib.contextmanager
def test_host_func(storage_backend_type):
    if storage_backend_type == StorageBackendForTests.NUCLEUS:
        test_host = os.environ.get("WRAPP_NUCLEUS_TEST_HOST", os.environ.get("TEST_HOST", "localhost/Tests/omniverse/OM-92309"))
        yield test_host
    elif storage_backend_type == StorageBackendForTests.S3:
        yield os.environ.get("WRAPP_S3_TEST_BUCKET", "wrapptest")
    elif storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
        yield os.environ.get("WRAPP_AZURE_TEST_CONTAINER", "wrapp-test-container")
    elif storage_backend_type == StorageBackendForTests.GCS:
        yield os.environ.get("WRAPP_GCS_TEST_BUCKET", "wrapp-test-bucket")
    elif storage_backend_type == StorageBackendForTests.LOCAL_DIR:
        path = os.environ.get("LOCALDIR_TEST_FOLDER", None)
        if path:
            yield path
        else:
            with tempfile.TemporaryDirectory() as tmp_dir:
                yield tmp_dir
    elif storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
        # For storage API, the test host is the GRPC endpoint of the storage service, which is different from the netloc of the resource address!
        yield os.environ.get("WRAPP_STORAGE_API_GRPC_ADDRESS", "localhost:50051")
    else:
        pytest.fail(f"Unknown storage type {storage_backend_type}")


@pytest.fixture(scope="session")
def test_host(storage_backend_type):
    with test_host_func(storage_backend_type) as hostname:
        yield hostname


@pytest.fixture(scope="session")
def alternate_storage_test_host(alternate_storage_backend_type):
    with test_host_func(alternate_storage_backend_type) as hostname:
        yield hostname


@pytest.fixture(scope="session")
def alternate_host_test_host_no_fail(storage_backend_type):
    # We need a fixture which does not fail the tests for the setup.
    if storage_backend_type == StorageBackendForTests.S3:
        return os.environ.get("WRAPP_S3_ALTERNATE_TEST_BUCKET", "wrapptest2")
    elif storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
        return os.environ.get("WRAPP_AZURE_ALTERNATE_TEST_CONTAINER", "wrapptest-alternate")
    elif storage_backend_type == StorageBackendForTests.GCS:
        return os.environ.get("WRAPP_GCS_ALTERNATE_TEST_BUCKET", "wrapp-test-bucket-alternate")
    else:
        # Only implemented for S3 right now
        return None


@pytest.fixture(scope="session")
def alternate_host_test_host(alternate_host_test_host_no_fail, storage_backend_type):
    # Use this fixture for individual tests.
    host = alternate_host_test_host_no_fail
    if not host:
        pytest.skip(f"Alternate test host has not been implemented yet for {storage_backend_type}")
    return host


def auth_info_func(test_host, storage_backend_type):
    if storage_backend_type == StorageBackendForTests.NUCLEUS:
        from tests.clientlib_test_helpers import ClientLibTestHelper

        test_host_netloc = str(test_host).split("/")[0]
        return ClientLibTestHelper.default_credentials(f"omniverse://{test_host_netloc}")
    elif storage_backend_type == StorageBackendForTests.S3:
        from tests.s3_test_helpers import S3TestHelper

        return S3TestHelper.default_credentials(f"s3://{test_host}")
    elif storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
        from tests.azure_blob_storage_test_helpers import AzureBlobStorageTestHelper

        account_name = os.environ.get("WRAPP_AZURE_ACCOUNT_NAME", "devstoreaccount1")
        return AzureBlobStorageTestHelper.default_credentials(f"azure://{account_name}")
    elif storage_backend_type == StorageBackendForTests.GCS:
        from tests.gcs_test_helpers import GCSTestHelper

        bucket_name = os.environ.get("WRAPP_GCS_TEST_BUCKET", "wrapp-test-bucket")
        return GCSTestHelper.default_credentials(f"gs://{bucket_name}")
    elif storage_backend_type == StorageBackendForTests.LOCAL_DIR:
        return None
    elif storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
        return None
    else:
        raise Exception(f"Unknown storage type {storage_backend_type}")


@pytest.fixture
def auth_info(test_host, storage_backend_type):
    yield auth_info_func(test_host, storage_backend_type)


@pytest.fixture
def alternate_storage_auth_info(alternate_storage_test_host, alternate_storage_backend_type):
    yield auth_info_func(alternate_storage_test_host, alternate_storage_backend_type)


@pytest.fixture(scope="session")
def test_region():
    yield os.environ.get("WRAPP_S3_TEST_REGION", "us-west-1")


@pytest.fixture(scope="session")
def storage_backend_test_helper(
    storage_backend_type, wrapp_init, test_host, alternate_host_test_host_no_fail
) -> Generator[StorageTestHelper, None, None]:
    from wrapp.utils.utils import StorageOperationError

    backend = None
    try:
        if storage_backend_type in [StorageBackendForTests.NUCLEUS, StorageBackendForTests.LOCAL_DIR]:
            from tests.clientlib_test_helpers import ClientLibTestHelper

            backend = ClientLibTestHelper(storage_backend_type)
        elif storage_backend_type == StorageBackendForTests.S3:
            from tests.s3_test_helpers import S3TestHelper

            backend = S3TestHelper()
        elif storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
            from tests.azure_blob_storage_test_helpers import AzureBlobStorageTestHelper

            backend = AzureBlobStorageTestHelper()
        elif storage_backend_type == StorageBackendForTests.GCS:
            from tests.gcs_test_helpers import GCSTestHelper

            backend = GCSTestHelper()
        elif storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
            from tests.storageapi_test_helpers import StorageAPITestHelper

            backend = StorageAPITestHelper(use_enumerate=StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE == storage_backend_type)
        else:
            pytest.fail(f"Unimplemented storage backend {storage_backend_type}")
    except ImportError as e:
        pytest.skip(f"Storage backend {storage_backend_type} not available: missing dependencies ({e})")

    try:
        # pytest will use this fixture as a generator - it will create the generator,
        # and then call next(...) on it twice:
        # 1. to get the value passed to the test (and execute anything before the yield)
        # 2. to execute anything following the yield.
        # We use "yield from" here to allow the storage_backend to yield, too -
        # and execute code before/after the yield.
        # This allows the storages to use context managers.
        test_hosts = [test_host]
        if alternate_host_test_host_no_fail:
            test_hosts.append(alternate_host_test_host_no_fail)
        yield from backend.session_initialization(test_hosts)
    except StorageOperationError as e:
        # Storage not available (e.g., dependencies not installed for this wheel variant)
        pytest.skip(f"Storage backend {storage_backend_type} not available: {e}")
    except Exception:
        pytest.fail(f"Failed to initialize storage: {traceback.format_exc()}")


@pytest.fixture(scope="session")
def remote_test_host():
    if os.environ.get("REMOTE_TEST_HOST") is not None:
        return os.environ.get("REMOTE_TEST_HOST")
    else:
        pytest.skip(
            "You need to specify a remote test host in order to run the remote tests! Specify REMOTE_TEST_HOST environment variable!"
        )


@pytest.fixture
def remote_auth_info(remote_test_host) -> StorageAuthInfo:
    access_token = os.environ.get("WRAPP_TEST_ACCESS_TOKEN")
    if access_token is None:
        pytest.skip(
            "To run the remote tests, specify an environment variable WRAPP_TEST_ACCESS_TOKEN to use for server specified in REMOTE_TEST_HOST!"
        )
    return StorageAuthInfo.nucleus(server_url=f"omniverse://{remote_test_host}", username="$omni-api-token", password_or_token=access_token)


@pytest.fixture
def authenticator(auth_info, storage_backend_test_helper):
    with AuthManager(auth_infos=[auth_info] if auth_info else [], interactive_fallback=False) as auth:
        yield auth


@pytest.fixture
def authenticator_with_alternate(auth_info, alternate_storage_auth_info):
    all_auth = []
    if auth_info:
        all_auth.append(auth_info)
    if alternate_storage_auth_info:
        all_auth.append(alternate_storage_auth_info)
    with AuthManager(auth_infos=all_auth, interactive_fallback=False) as auth:
        yield auth


# Return a fully initialized storage backend
@pytest.fixture
def storage_backend(storage_backend_test_helper, authenticator) -> Generator[StorageTestHelper, None, None]:
    yield storage_backend_test_helper


def session_test_folder_url_func(test_host, test_region, storage_backend_type):
    utcnow = datetime.datetime.now(datetime.timezone.utc)
    target = utcnow.strftime("%Y%m%d_%H%M%S_%f") + "_" + "".join(random.choices(string.ascii_lowercase, k=2))
    if storage_backend_type == StorageBackendForTests.NUCLEUS:
        address_root = f"omniverse://{test_host}/tests/"
    elif storage_backend_type == StorageBackendForTests.S3:
        # We don't want to always run all tests twice, but as we run the tests often enough flip a coin and
        # decide which style of URLs to use
        # For MINIO testing, the region is ignored as it just parses bucket and key from the URL
        address_root = random.Random().choice(
            [
                f"s3://{test_host}/wrapp_tests/",  # Global URL, no region mentioned
                f"https://{test_host}.s3.{test_region}.amazonaws.com/wrapp_tests/",  # Virtual host style URL, region bound
            ]
        )
    elif storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
        account_name = os.environ.get("WRAPP_AZURE_ACCOUNT_NAME", "devstoreaccount1")
        address_root = f"azure://{account_name}/{test_host}/wrapp_tests/"
    elif storage_backend_type == StorageBackendForTests.GCS:
        address_root = f"gs://{test_host}/wrapp_tests/"
    elif storage_backend_type == StorageBackendForTests.LOCAL_DIR:
        address_root = f"file://{test_host}/tests/"
    elif storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
        test_root = os.environ.get(
            "WRAPP_STORAGE_API_RESOURCE_ROOT",
            os.environ.get("WRAPP_STORAGEAPI_RESOURCE_ROOT", "https://wrapptest.s3.us-west-1.amazonaws.com"),
        ).rstrip("/")
        address_root = f"{test_root}/wrapp_tests"
    else:
        pytest.fail(f"Unknown storage type {storage_backend_type}")
    folder = join_paths(address_root, target)
    return folder
    # Leave the folder around - we would actually only like to keep it if the test failed, how to do that?


@pytest.fixture(scope="session")
def session_test_folder_url(test_host, test_region, storage_backend_type):
    yield session_test_folder_url_func(test_host, test_region, storage_backend_type)


@pytest.fixture(scope="session")
def alternate_storage_session_test_folder_url(alternate_storage_test_host, test_region, alternate_storage_backend_type):
    # test_region is only used for S3, so no need to make an alternate version of that.
    yield session_test_folder_url_func(alternate_storage_test_host, test_region, alternate_storage_backend_type)


@pytest.fixture(scope="session")
def alternate_host_session_test_folder_url(alternate_host_test_host, test_region, storage_backend_type):
    # test_region is only used for S3, so no need to make an alternate version of that.
    yield session_test_folder_url_func(alternate_host_test_host, test_region, storage_backend_type)


def test_folder_url_func(session_test_folder_url: str):
    target = "".join(random.choices(string.ascii_lowercase, k=8))
    folder = join_paths(session_test_folder_url, target)
    return folder


@pytest.fixture
def test_folder_url(session_test_folder_url):
    yield test_folder_url_func(session_test_folder_url)


@pytest.fixture
def alternate_host_test_folder_url(alternate_host_session_test_folder_url):
    yield test_folder_url_func(alternate_host_session_test_folder_url)


@pytest.fixture
def alternate_storage_test_folder_url(alternate_storage_session_test_folder_url):
    yield test_folder_url_func(alternate_storage_session_test_folder_url)


@pytest_asyncio.fixture
async def source_package(test_folder_url, storage_backend):
    yield await storage_backend.create_source_package(test_folder_url)


@pytest_asyncio.fixture
async def alternate_storage_source_package(alternate_storage_backend_type, alternate_storage_test_folder_url, alternate_storage_auth_info):
    """
    Creates the same content as source_package but on the "other" storage (if source_package is on S3,
    this returns the URL for Nucleus and vice versa).
    This allows to write tests using both storages.
    """
    try:
        if alternate_storage_backend_type == StorageBackendForTests.NUCLEUS:
            from tests.clientlib_test_helpers import ClientLibTestHelper

            with AuthManager(auth_infos=[alternate_storage_auth_info], interactive_fallback=False) as auth:
                yield await ClientLibTestHelper.create_source_package_for_clientlib(alternate_storage_test_folder_url)

        elif alternate_storage_backend_type == StorageBackendForTests.LOCAL_DIR:
            from tests.clientlib_test_helpers import ClientLibTestHelper

            yield await ClientLibTestHelper.create_source_package_for_clientlib(alternate_storage_test_folder_url)

        elif alternate_storage_backend_type == StorageBackendForTests.S3:
            from tests.s3_test_helpers import S3TestHelper

            yield await S3TestHelper.create_source_package_for_s3(alternate_storage_test_folder_url)

        elif alternate_storage_backend_type == StorageBackendForTests.AZURE_BLOB_STORE:
            from tests.azure_blob_storage_test_helpers import AzureBlobStorageTestHelper

            yield await AzureBlobStorageTestHelper.create_source_package_for_azure(alternate_storage_test_folder_url)

        elif alternate_storage_backend_type == StorageBackendForTests.GCS:
            from tests.gcs_test_helpers import GCSTestHelper

            yield await GCSTestHelper.create_source_package_for_gcs(alternate_storage_test_folder_url)

        elif alternate_storage_backend_type in [StorageBackendForTests.STORAGEAPI, StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE]:
            from tests.storageapi_test_helpers import StorageAPITestHelper

            helper = StorageAPITestHelper(use_enumerate=alternate_storage_backend_type == StorageBackendForTests.STORAGEAPI_WITH_ENUMERATE)
            hosts = list(helper.session_initialization([]))
            yield await hosts[0].create_source_package(alternate_storage_test_folder_url)

        else:
            pytest.fail(f"Unexpected cross storage backend type {alternate_storage_backend_type}")
    except ImportError as e:
        pytest.skip(f"Alternate storage backend {alternate_storage_backend_type} not available: missing dependencies ({e})")


@pytest_asyncio.fixture
async def file_list_list(authenticator, storage_backend, source_package):
    base = source_package
    yield [(base, "testfile.txt"), (join_paths(base, "subfolder"), "file_in_folder.txt")]


@pytest_asyncio.fixture
async def file_list_file(local_tmp_folder, file_list_list):
    tsv_path = os.path.join(local_tmp_folder, "file_list.tsv")
    with open(tsv_path, "wt") as file:
        for item in file_list_list:
            file.write(f"{item[0]}\t{item[1]}\n")
    yield tsv_path


@pytest.fixture(scope="module")
def cli_runner():
    return CliRunner()


@pytest.fixture
def local_tmp_folder():
    """Create a temporary folder under ``tests/tmp/`` and remove it after the test.

    Uses a subfolder of the tests directory (instead of the system temp dir)
    so leftover files are easy to find when debugging.
    """
    tmpfolder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp", random_string(8))
    os.makedirs(tmpfolder, exist_ok=True)
    yield tmpfolder
    shutil.rmtree(tmpfolder, ignore_errors=True)


@pytest.fixture(scope="session")
def cloudfront_test_url():
    """CloudFront URL for testing external URL functionality with real CDN.

    This fixture returns the CloudFront URL if WRAPP_TEST_CLOUDFRONT_URL is set,
    otherwise returns None. Tests should check for None and skip or adjust
    behavior accordingly.
    """
    return os.environ.get("WRAPP_TEST_CLOUDFRONT_URL")


class CloudFrontLikeHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP request handler that mimics CloudFront behavior.

    - Doesn't log to stderr
    - Returns 403 Forbidden for directory listing requests (CloudFront doesn't allow listing)
    - Returns 404 for stat requests on directories (no real folders in CDNs)
    """

    def log_message(self, format, *args):
        # Suppress logging
        pass

    def do_HEAD(self):
        """Handle HEAD requests - return 404 for directory paths to simulate CDN behavior."""
        # CDNs don't have real directories, so HEAD on a directory path should fail
        if self.path.endswith("/") or "." not in self.path.split("/")[-1]:
            # Likely a directory - return 404 (not found) like a real CDN would
            self.send_error(404, "Not Found")
            return
        super().do_HEAD()

    def list_directory(self, path):
        """Override to return 403 Forbidden instead of directory listing."""
        self.send_error(403, "Directory listing not allowed (CloudFront-like behavior)")
        return None


@pytest.fixture
def cloudfront_like_http_server(local_tmp_folder):
    """Start a local HTTP server that mimics CloudFront behavior.

    CloudFront (and most CDNs):
    - Don't support directory listing (returns 403)
    - Don't have real folders (HEAD on directory paths returns 404)
    - Only serve individual files

    This is useful for testing error handling when users try to use
    list-repo or other directory-based operations against CDN URLs.
    """
    handler = functools.partial(CloudFrontLikeHTTPRequestHandler, directory=local_tmp_folder)
    # Bind to port 0 to let the OS assign a free port - avoids TOCTOU race
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


@pytest.fixture
def client_cache_size_unchanged():
    def dir_size(path: str) -> int:
        total_size = 0
        if os.path.exists(path):
            for dirpath, dirnames, filenames in os.walk(path):
                total_size += sum([os.path.getsize(os.path.join(dirpath, fn)) for fn in filenames])
        return total_size

    # Check size of the client-lib cache
    client_lib_cache_path = join_paths(os.environ.get("OVPACKAGE_TEST_CACHE_DIR", tempfile.gettempdir()), "client")
    client_lib_cache_size = dir_size(client_lib_cache_path)
    yield
    new_client_lib_cache_size = dir_size(client_lib_cache_path)
    assert (
        client_lib_cache_size == new_client_lib_cache_size
    ), f"Client library cache changed size from {client_lib_cache_size} to {new_client_lib_cache_size}! Client library cache at {client_lib_cache_path} had size {client_lib_cache_size} before the test, and {new_client_lib_cache_size} after the test! Make sure all get_local_file or open_local_file calls are only executed if hub is enabled (search for is_hub_running for examples)."


@pytest.fixture
def fail_on_warnings_errors(caplog) -> Generator[pytest.LogCaptureFixture, None, None]:
    """
    Capture logs at WARNING level and verify no warnings/errors are logged.

    This fixture sets the log level to WARNING for the duration of the test,
    then checks that no WARNING or ERROR messages were logged.
    """
    with caplog.at_level(logging.WARNING):
        yield caplog

    warnings_and_errors = [record for record in caplog.records if record.levelno >= logging.WARNING]

    if warnings_and_errors:
        messages = "\n".join(f"  [{record.levelname}] {record.name}: {record.message}" for record in warnings_and_errors)
        pytest.fail(f"Unexpected warnings/errors logged during test:\n{messages}")
