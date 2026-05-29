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

import logging
import os
import traceback
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
)

if TYPE_CHECKING:
    from wrapp.datastructures.command_context import ThrottlingParameters

from wrapp._compat import (
    is_ovpackage_invocation,
    public_env_prefix,
)
from wrapp.utils.coro import (
    assert_not_wrapp_thread,
    sync_calls_async_on_wrapp_thread,
)
from wrapp.utils.storage_impls.import_tagging_client import g_tagging_client_available
from wrapp.utils.storage_interface import (
    Result,
    StorageInterface,
)
from wrapp.utils.task_throttler import TaskThrottler
from wrapp.utils.utils import (
    StorageInitializationError,
    StorageMissingDependenciesError,
    StorageOperationError,
    is_file_url,
    is_nucleus_url,
)

logger = logging.getLogger("omni.wrapp")

g_storage_api_enabled = (
    os.getenv("WRAPP_DISABLE_STORAGE_API", "False") != "True" and os.getenv("WRAPP_ENABLE_STORAGE_API", "True") != "False"
)
g_azure_enabled = os.getenv("WRAPP_DISABLE_AZURE", "False") == "False"
g_aws_enabled = os.getenv("WRAPP_DISABLE_AWS", "False") == "False"
g_gcs_enabled = os.getenv("WRAPP_DISABLE_GCS", "False") == "False"
g_nucleus_enabled = os.getenv("WRAPP_DISABLE_NUCLEUS", "False") == "False"
g_list_routes_enabled = os.getenv("WRAPP_DISABLE_LIST_ROUTES", "False") == "False"
g_local_file_storage_enabled = True  # Always enabled, independent of Nucleus


class StorageType(Enum):
    S3 = "S3"
    Nucleus = "Nucleus"
    StorageAPI = "StorageAPI"
    Azure = "Azure Blob Storage"
    GCS = "Google Cloud Storage"
    Local = "Local File System"
    Invalid = "n/a"


class Router:
    """Singleton that routes storage URLs to the correct backend implementation.

    The Router owns one instance of every enabled storage backend and decides
    which one handles a given URL.  Routing priority (checked in order):

    1. **Storage API** — if enabled and the URL is in scope (see
       :meth:`StorageAPIStorageGRPC.is_url_in_scope`). Once Storage API has
       reported its authoritative scope (via ``ListRoutes`` or
       ``ListTopLevelAddresses``), it claims any URL within that scope —
       including ``omniverse://`` and ``file://`` URLs — ahead of the dedicated
       Nucleus / local backends. Before the first connection, Storage API does
       not claim ``omniverse://`` or ``file://`` URLs to avoid stealing them
       from those dedicated backends during lazy init.
    2. **S3** — ``s3://`` or ``https://...amazonaws.com`` URLs.
    3. **Azure** — ``https://<account>.blob.core.windows.net/`` URLs.
    4. **GCS** — ``gs://`` or ``https://storage.googleapis.com/`` URLs.
    5. **Nucleus / HTTP** — ``omniverse://`` or generic ``http(s)://`` URLs.
    6. **Local file system** — ``file://`` URLs or bare paths.

    Backends can be disabled via environment variables (e.g.
    ``WRAPP_DISABLE_AWS=True``, ``WRAPP_DISABLE_NUCLEUS=True``,
    ``WRAPP_DISABLE_STORAGE_API=True``, etc.). Storage API is enabled by
    default but stays inert until a discovery URL or gRPC address is
    configured. The legacy ``WRAPP_ENABLE_STORAGE_API=False`` knob is still
    honored for backward compatibility.
    """

    _instance: Optional["Router"] = None

    @classmethod
    def initialize(cls, wrapp_version: str, debug_level: bool, throttling_parameters: Optional["ThrottlingParameters"] = None):
        """
        Initialize Router. Any configuration issues in the storage backends should be
        reported via a RuntimeError exception.
        """
        from wrapp.datastructures.command_context import ThrottlingParameters

        if throttling_parameters is None:
            throttling_parameters = ThrottlingParameters()

        if not cls._instance:
            cls._instance = Router(wrapp_version, debug_level, throttling_parameters)
        else:
            cls._instance._verify_same_params(wrapp_version, debug_level, throttling_parameters)
        return cls._instance

    @classmethod
    def is_initialized(cls) -> bool:
        return cls._instance is not None

    @classmethod
    def me(cls):
        if cls._instance is not None:
            return cls._instance
        else:
            raise RuntimeError("Router not initialized")

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_parameters: "ThrottlingParameters"):
        logger.debug(
            f"Router initialized: {wrapp_version} debug={debug_level} "
            f"jobs={throttling_parameters.jobs} file_transfer_jobs={throttling_parameters.file_transfer_jobs} "
            f"tagging_jobs={throttling_parameters.tagging_jobs}"
        )

        self._is_s3_url_impl: Optional[Callable[[str], bool]] = None
        self._is_azure_url_impl: Optional[Callable[[str], bool]] = None
        self._is_gcs_url_impl: Optional[Callable[[str], bool]] = None

        # Store throttling params for later access
        self._throttling_params = throttling_parameters

        # Store initialization status and version info for each storage backend
        self._storage_info: Dict[StorageType, str] = {}

        self._clientlib: Optional["ClientLibStorage"] = None
        # Nucleus is a wrapp-internal backend. The public ovpackage CLI must not
        # surface it in `version`, error messages, or import attempts.
        if not is_ovpackage_invocation():
            try:
                if g_nucleus_enabled:
                    from wrapp.utils.storage_impls.clientlib import ClientLibStorage

                    self._clientlib = ClientLibStorage(wrapp_version, debug_level, throttling_parameters)
                    self._storage_info[
                        StorageType.Nucleus
                    ] = f"{ClientLibStorage.version_info()} Tagging Support: {'ENABLED' if g_tagging_client_available else 'DISABLED'}."
                else:
                    self._storage_info[StorageType.Nucleus] = f"DISABLED: Nucleus backend: {public_env_prefix()}DISABLE_NUCLEUS is set"
            except (StorageInitializationError, StorageMissingDependenciesError) as e:
                self._storage_info[StorageType.Nucleus] = f"DISABLED: {e}"
                logger.info(f"{StorageType.Nucleus}: {self._storage_info[StorageType.Nucleus]}")

        self._local_file_storage: Optional["LocalFileStorage"] = None
        try:
            if g_local_file_storage_enabled:
                from wrapp.utils.storage_impls.local_file_storage import (
                    LocalFileStorage,
                )

                self._local_file_storage = LocalFileStorage(wrapp_version, debug_level, throttling_parameters)
                self._storage_info[StorageType.Local] = LocalFileStorage.version_info()
        except (StorageInitializationError, StorageMissingDependenciesError) as e:
            self._storage_info[StorageType.Local] = f"DISABLED: {e}"
            logger.info(f"{StorageType.Local}: {self._storage_info[StorageType.Local]}")

        self._s3: Optional["S3Storage"] = None
        try:
            if g_aws_enabled:
                # Import S3Storage and is_s3_url locally to avoid import errors if dependencies are missing
                from wrapp.utils.storage_impls.s3storage import (
                    S3Storage,
                    is_s3_url,
                )

                self._s3 = S3Storage(wrapp_version, debug_level, throttling_parameters)
                self._is_s3_url_impl = is_s3_url
                self._storage_info[StorageType.S3] = S3Storage.version_info()
            else:
                self._storage_info[StorageType.S3] = f"DISABLED: AWS S3 backend: {public_env_prefix()}DISABLE_AWS is set"
        except StorageMissingDependenciesError as e:
            self._storage_info[StorageType.S3] = f"DISABLED: {e}"
            logger.info(f"{StorageType.S3}: {self._storage_info[StorageType.S3]}")
        except StorageInitializationError as e:
            self._storage_info[StorageType.S3] = f"Failed to initialize S3 storage backend: {e}"
            logger.error(f"{StorageType.S3}: {self._storage_info[StorageType.S3]}")

        self._azure: Optional["AzureBlobStorage"] = None
        try:
            if g_azure_enabled:
                # Import AzureBlobStorage and _url locally to avoid import errors if dependencies are missing
                from wrapp.utils.storage_impls.azure_blob_storage import (
                    AzureBlobStorage,
                    is_azure_url,
                )

                self._azure = AzureBlobStorage(wrapp_version, debug_level, throttling_parameters)
                self._is_azure_url_impl = is_azure_url
                self._storage_info[StorageType.Azure] = AzureBlobStorage.version_info()
            else:
                self._storage_info[StorageType.Azure] = f"DISABLED: Azure backend: {public_env_prefix()}DISABLE_AZURE is set"
        except StorageMissingDependenciesError as e:
            self._storage_info[StorageType.Azure] = f"DISABLED: {e}"
            logger.info(f"{StorageType.Azure}: {self._storage_info[StorageType.Azure]}")
        except StorageInitializationError as e:
            logger.error(f"Failed to initialize Azure Blob Storage backend: {e}")

        self._gcs: Optional["GCSStorage"] = None
        try:
            if g_gcs_enabled:
                # Import GCSStorage and is_gcs_url locally to avoid import errors if dependencies are missing
                from wrapp.utils.storage_impls.gcs_storage import (
                    GCSStorage,
                    is_gcs_url,
                )

                self._gcs = GCSStorage(wrapp_version, debug_level, throttling_parameters)
                self._is_gcs_url_impl = is_gcs_url
                self._storage_info[StorageType.GCS] = GCSStorage.version_info()
            else:
                self._storage_info[StorageType.GCS] = f"DISABLED: GCS backend: {public_env_prefix()}DISABLE_GCS is set"
        except StorageMissingDependenciesError as e:
            self._storage_info[StorageType.GCS] = f"DISABLED: {e}"
            logger.info(f"{StorageType.GCS}: {self._storage_info[StorageType.GCS]}")
        except StorageInitializationError as e:
            self._storage_info[StorageType.GCS] = f"DISABLED: {e}"
            logger.error(f"Failed to initialize Google Cloud Storage backend: {e}")

        self._storage_api = None
        try:
            if g_storage_api_enabled:
                # Import StorageAPIStorageGRPC locally to avoid import errors if dependencies are missing
                from wrapp.utils.storage_impls.storageapi import StorageAPIStorageGRPC

                self._storage_api = StorageAPIStorageGRPC(wrapp_version, debug_level, throttling_parameters)
                target = self._storage_api._discovery_url or self._storage_api._endpoint
                if target:
                    self._storage_info[StorageType.StorageAPI] = f"Storage API initialized ({target})"
                else:
                    self._storage_info[StorageType.StorageAPI] = (
                        f"ENABLED but not configured: set {public_env_prefix()}STORAGE_API_DISCOVERY_URL "
                        f"or {public_env_prefix()}STORAGE_API_GRPC_ADDRESS"
                    )
            else:
                self._storage_info[
                    StorageType.StorageAPI
                ] = f"DISABLED: Storage API backend: {public_env_prefix()}DISABLE_STORAGE_API is set"
        except StorageMissingDependenciesError as e:
            self._storage_info[StorageType.StorageAPI] = f"DISABLED: Storage API backend: {e}"
            logger.info(f"{StorageType.StorageAPI}: {self._storage_info[StorageType.StorageAPI]}")
        except StorageInitializationError as e:
            # This is expected if the Storage API endpoint has not been configured correctly
            if e.result != Result.OK:
                self._storage_info[StorageType.StorageAPI] = f"Failed to initialize Storage API backend: {e}"
                logger.error(f"{StorageType.StorageAPI}: {self._storage_info[StorageType.StorageAPI]}")
            else:
                self._storage_info[StorageType.StorageAPI] = f"Storage API backend not configured: {e}"
                logger.info(f"{StorageType.StorageAPI}: {self._storage_info[StorageType.StorageAPI]}")

        self._wrapp_version = wrapp_version
        self._debug_level = debug_level

    def get_throttling_params(self) -> "ThrottlingParameters":
        """Get the scheduling parameters that were used to initialize the Router."""
        return self._throttling_params

    def reconfigure_throttlers(self, params: "ThrottlingParameters") -> bool:
        success = True
        if self._clientlib is not None:
            if not self._clientlib.reconfigure_throttlers(params):
                success = False
        if self._local_file_storage is not None:
            if not self._local_file_storage.reconfigure_throttlers(params):
                success = False
        if self._s3 is not None and not self._s3.reconfigure_throttlers(params):
            success = False
        if self._azure is not None and not self._azure.reconfigure_throttlers(params):
            success = False
        if self._gcs is not None and not self._gcs.reconfigure_throttlers(params):
            success = False
        if self._storage_api is not None and not self._storage_api.reconfigure_throttlers(params):
            success = False
        return success

    def _verify_same_params(self, wrapp_version: str, debug_level: bool, throttling_params: "ThrottlingParameters"):
        if wrapp_version != self._wrapp_version or debug_level != self._debug_level:
            raise RuntimeError(
                f"Repeated initialization of Router with different parameters: {wrapp_version}!={self._wrapp_version} or {debug_level}!={self._debug_level}.\n{traceback.format_stack()}"
            )

        if (
            self._throttling_params.jobs != throttling_params.jobs
            or self._throttling_params.file_transfer_jobs != throttling_params.file_transfer_jobs
            or self._throttling_params.tagging_jobs != throttling_params.tagging_jobs
        ):
            logger.debug(
                f"Throttling parameters cannot be changed at runtime. "
                f"Requested: jobs={throttling_params.jobs}, file_transfer_jobs={throttling_params.file_transfer_jobs}, tagging_jobs={throttling_params.tagging_jobs}. "
                f"Using initialized values: jobs={self._throttling_params.jobs}, file_transfer_jobs={self._throttling_params.file_transfer_jobs}, tagging_jobs={self._throttling_params.tagging_jobs}."
            )

    @sync_calls_async_on_wrapp_thread
    async def _s3_shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._s3:
            await self._s3.shutdown(exc_type, exc_val, exc_tb)
            self._s3 = None

    @sync_calls_async_on_wrapp_thread
    async def _azure_shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._azure:
            await self._azure.shutdown(exc_type, exc_val, exc_tb)
            self._azure = None

    @sync_calls_async_on_wrapp_thread
    async def _gcs_shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._gcs:
            await self._gcs.shutdown(exc_type, exc_val, exc_tb)
            self._gcs = None

    @sync_calls_async_on_wrapp_thread
    async def _local_file_storage_shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._local_file_storage:
            await self._local_file_storage.shutdown(exc_type, exc_val, exc_tb)
            self._local_file_storage = None

    @sync_calls_async_on_wrapp_thread
    async def _storageapi_shutdown(self):
        if self._storage_api:
            await self._storage_api.close()
            self._storage_api = None

    @assert_not_wrapp_thread
    def shutdown(self, exc_type=None, exc_val=None, exc_tb=None, shutdown_client_lib=True):
        logger.info("Router shutting down...")
        self._s3_shutdown(exc_type, exc_val, exc_tb)
        self._azure_shutdown(exc_type, exc_val, exc_tb)
        self._gcs_shutdown(exc_type, exc_val, exc_tb)
        self._storageapi_shutdown()
        self._local_file_storage_shutdown(exc_type, exc_val, exc_tb)
        # Clientlib may not be initialized if disabled via WRAPP_DISABLE_NUCLEUS
        if self._clientlib and shutdown_client_lib:
            self._clientlib.shutdown(exc_type, exc_val, exc_tb)
            # we cannot revive the client-library after a shut-down. A call to the client-library
            # while shut-down will possibly block the process. So make sure we don't
            # accidentally use the router after the shutdown.
            self._clientlib = None
        # Avoid reusing a Router which is shutting down
        Router._instance = None

    def is_s3_url(self, url: str) -> bool:
        """Check if a URL is an S3 URL.

        Returns False if S3 support is not available.
        """
        if self._is_s3_url_impl is None:
            return False
        return self._is_s3_url_impl(url)

    def is_azure_url(self, url: str) -> bool:
        """Check if a URL is an Azure URL.

        Returns False if Azure support is not available.
        """
        if self._is_azure_url_impl is None:
            return False
        return self._is_azure_url_impl(url)

    def is_gcs_url(self, url: str) -> bool:
        """Check if a URL is a Google Cloud Storage URL.

        Returns False if GCS support is not available.
        """
        if self._is_gcs_url_impl is None:
            return False
        return self._is_gcs_url_impl(url)

    def is_storage_api_url(self, url: str) -> bool:
        if self._storage_api is None:
            return False
        # Once Storage API has reported authoritative scope (routes or TLAs),
        # trust it for any URL scheme — including omniverse:// and file:// —
        # so that a service that explicitly advertises those URLs as its own
        # is correctly preferred over the Nucleus/local backends.
        if self._storage_api.has_known_scope():
            return self._storage_api.is_url_in_scope(url)
        # Pre-connection (no scope yet known): be permissive to allow lazy
        # gRPC initialization, but do not steal omniverse:// / file:// URLs
        # from their dedicated backends before the server has had a chance
        # to tell us what it actually serves.
        if is_file_url(url) or is_nucleus_url(url):
            return False
        if self._storage_api.is_url_in_scope(url):
            return True
        discovery_url = self._storage_api._discovery_url
        if discovery_url and url.rstrip("/").startswith(discovery_url):
            return True
        return False

    def get_storage_version_info(self, storage_type: StorageType) -> str:
        return self._storage_info[storage_type]

    def get_version_info(self) -> List[str]:
        """Get version info for all available storage backends."""
        versions = []
        for storage_type, info in self._storage_info.items():
            versions.append(f"{storage_type.value}: {info}")
        return versions

    def can_handle_url(self, url: str) -> bool:
        """Return True if at least one enabled backend can handle *url*."""
        try:
            self._get_storage(url)
            return True
        except StorageOperationError:
            return False

    def _get_storage(self, url: Optional[str]) -> Tuple[StorageInterface, StorageType]:
        if not url or not url.strip():
            raise StorageOperationError("URL cannot be empty")
        elif self.is_storage_api_url(url):
            # Prefer StorageAPI if it is not disabled
            # Once Routing is available on StorageAPI, this can be much more precise than a catch-all
            return self.storage_api(), StorageType.StorageAPI
        elif self.is_s3_url(url):
            return self.s3(), StorageType.S3
        elif self.is_azure_url(url):
            return self.azure(), StorageType.Azure
        elif self.is_gcs_url(url):
            return self.gcs(), StorageType.GCS
        elif (
            is_nucleus_url(url) or url.startswith("http://") or url.startswith("https://")
        ):  # we still handle http:// links through clientlib - TODO
            if self._clientlib is None:
                if is_ovpackage_invocation():
                    raise StorageOperationError(f"No backend enabled to handle URL '{url}'")
                nucleus_info = self._storage_info.get(StorageType.Nucleus, "unknown reason")
                raise StorageOperationError(f"Nucleus storage backend is not available ({nucleus_info}), cannot handle URL: {url}")
            return self.clientlib(), StorageType.Nucleus
        elif is_file_url(url):
            # Prefer dedicated local file storage if available, fall back to clientlib for backward compat
            if self._local_file_storage is not None:
                return self._local_file_storage, StorageType.Local
            elif self._clientlib is not None:
                return self.clientlib(), StorageType.Local
            else:
                raise StorageOperationError(f"No storage backend available to handle file URL: {url}")
        else:
            backend_info = "\n\t".join(self.get_version_info())
            raise StorageOperationError(f"URL {url} cannot be handled by any of the enabled storage backends:\n\t{backend_info}\n")

    def all_backend_throttlers(self) -> list[TaskThrottler]:
        throttlers: list[TaskThrottler] = []
        if self._clientlib:
            throttlers.extend(self.clientlib().backend_throttlers())
        if self._local_file_storage:
            throttlers.extend(self._local_file_storage.backend_throttlers())
        if self._s3:
            throttlers.extend(self.s3().backend_throttlers())
        if self._azure:
            throttlers.extend(self.azure().backend_throttlers())
        if self._gcs:
            throttlers.extend(self.gcs().backend_throttlers())
        if self._storage_api:
            throttlers.extend(self.storage_api().backend_throttlers())
        return throttlers

    def storage_for_read(self, url: str) -> StorageInterface:
        return self._get_storage(url)[0]

    def storage_for_write(self, url: str) -> StorageInterface:
        # TODO - we have no variation yet, but at some point we might have a choice of faster providers?
        return self._get_storage(url)[0]

    def storage_types_for_copy(self, read_url: str, write_url: str) -> Tuple[StorageType, StorageType]:
        read_storage, read_storage_type = self._get_storage(read_url)
        write_storage, write_storage_type = self._get_storage(write_url)
        return read_storage_type, write_storage_type

    def enable_auth_interactive_fallback(self, enabled: bool):
        if self._clientlib is not None:
            self._clientlib.enable_auth_interactive_fallback(enabled)
        if self._storage_api is not None:
            self._storage_api.enable_auth_interactive_fallback(enabled)

    def s3(self) -> "S3Storage":  # type: ignore[name-defined]
        # S3 might have failed during the initialization. Given there are use cases in which the user will
        # run WRAPP without requiring the S3 storage, handle those errors on-the-fly.
        if not self._s3:
            raise StorageOperationError("Storage S3 was not successfully initialized")
        return self._s3

    if g_azure_enabled:

        def azure(self) -> "AzureBlobStorage":  # type: ignore[name-defined]
            # Azure might have failed during the initialization. Given there are use cases in which the user will
            # run WRAPP without requiring the Azure storage, handle those errors on-the-fly.
            if not self._azure:
                raise StorageOperationError("Storage Azure Blob Storage was not successfully initialized")
            return self._azure

    if g_gcs_enabled:

        def gcs(self) -> "GCSStorage":  # type: ignore[name-defined]
            # GCS might have failed during the initialization. Given there are use cases in which the user will
            # run WRAPP without requiring the GCS storage, handle those errors on-the-fly.
            if not self._gcs:
                raise StorageOperationError("Storage Google Cloud Storage was not successfully initialized")
            return self._gcs

    if g_nucleus_enabled:

        def clientlib(self) -> "ClientLibStorage":  # type: ignore[name-defined]
            if not self._clientlib:
                raise StorageOperationError("Nucleus storage backend was not successfully initialized or has been shut down")
            return self._clientlib

    def local_file_storage(self) -> "LocalFileStorage":  # type: ignore[name-defined]
        if not self._local_file_storage:
            raise StorageOperationError("Local file storage was not successfully initialized")
        return self._local_file_storage

    if g_storage_api_enabled:

        def storage_api(self) -> "StorageAPIStorageGRPC":  # type: ignore[name-defined]
            if not self._storage_api:
                raise StorageOperationError("Storage API was not successfully initialized")
            return self._storage_api
