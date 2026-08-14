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
import base64
import hashlib
import io
import json
import logging
import os
import re
import secrets
import tempfile
import urllib.parse
import webbrowser
from contextlib import (
    asynccontextmanager,
    contextmanager,
)
from datetime import datetime
from math import ceil
from typing import (
    Any,
    AsyncGenerator,
    Awaitable,
    BinaryIO,
    Callable,
    Dict,
    Generator,
    Iterator,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
)

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils import storage_interface
from wrapp.utils.config import (
    DEFAULT_STORAGE_API_BACKEND_NAME,
    StorageAPIBackendConfig,
    WrappConfig,
    load_wrapp_config,
)
from wrapp.utils.coro import sync_calls_async_on_wrapp_thread
from wrapp.utils.nucleus_hash import (
    IncrementalNucleusHasher,
    calc_nucleus_hash_from_stream,
)
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage_interface import (
    ConnectionStatus,
    DirectorySemantics,
    ListEntry,
    LocalReadOnlyFile,
    Result,
    ServerInfo,
    StorageAPIBearerAuthInfo,
    StorageAPIClientCredentialsAuthInfo,
    StorageAuthInfo,
    StorageInterface,
)
from wrapp.utils.task_throttler import (
    TaskThrottler,
    ThrottlerType,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageInitializationError,
    StorageMissingDependenciesError,
    StorageOperationError,
    get_path_from_url,
    is_file_url,
)

_CallableReturnType = TypeVar("_CallableReturnType")
_TaskReturnType = TypeVar("_TaskReturnType")

logger = logging.getLogger("omni.wrapp")

# Try to import required dependencies
try:
    import aiohttp
    import grpc
    import grpc.aio
    import keyring
    import yarl
    from aiohttp import web
    from google.protobuf.struct_pb2 import Value
    from nvidia.omniverse.storage.capabilities.v1alpha import (
        capabilities_pb2 as cap_pb2_v1alpha,
    )
    from nvidia.omniverse.storage.capabilities.v1alpha import (
        capabilities_pb2_grpc as cap_grpc_v1alpha,
    )
    from nvidia.omniverse.storage.capabilities.v1beta import (
        capabilities_pb2 as cap_pb2_v1beta,
    )
    from nvidia.omniverse.storage.capabilities.v1beta import (
        capabilities_pb2_grpc as cap_grpc_v1beta,
    )
    from nvidia.omniverse.storage.filefolder.v1alpha import (
        filefolder_service_pb2 as ff_svc_pb2_v1alpha,
    )
    from nvidia.omniverse.storage.filefolder.v1alpha import (
        filefolder_service_pb2_grpc as ff_grpc_v1alpha,
    )
    from nvidia.omniverse.storage.filefolder.v1beta import (
        filefolder_service_pb2 as ff_svc_pb2_v1beta,
    )
    from nvidia.omniverse.storage.filefolder.v1beta import (
        filefolder_service_pb2_grpc as ff_grpc_v1beta,
    )
    from nvidia.omniverse.storage.fileobject.v1alpha import (
        fileobject_pb2 as fo_pb2_v1alpha,
    )
    from nvidia.omniverse.storage.fileobject.v1alpha import (
        fileobject_service_pb2 as fo_svc_pb2_v1alpha,
    )
    from nvidia.omniverse.storage.fileobject.v1alpha import (
        fileobject_service_pb2_grpc as fo_grpc_v1alpha,
    )
    from nvidia.omniverse.storage.fileobject.v1beta import (
        fileobject_pb2 as fo_pb2_v1beta,
    )
    from nvidia.omniverse.storage.fileobject.v1beta import (
        fileobject_service_pb2 as fo_svc_pb2_v1beta,
    )
    from nvidia.omniverse.storage.fileobject.v1beta import (
        fileobject_service_pb2_grpc as fo_grpc_v1beta,
    )
    from nvidia.omniverse.storage.metadata.v1alpha import metadata_pb2 as md_pb2_v1alpha
    from nvidia.omniverse.storage.metadata.v1alpha import (
        metadata_pb2_grpc as md_grpc_v1alpha,
    )

    import requests  # isort: skip
except ImportError as e:
    missing = getattr(e, "name", str(e))
    error_msg = f"failed to import {missing}. Required dependencies: aiohttp>=3.11.18; yarl>=1.17.0; grpcio>=1.71.0; grpcio-tools>=1.71.0; requests>=2.32.3; keyring>=25.0.0. Install with 'pip install ovpackage[storage-api]'. Exception: {e}"
    raise StorageMissingDependenciesError(error_msg) from e

# Per-service version lookup tables for protocol negotiation.
# Keyed by version string ("v1beta" or "v1alpha"), each dict maps to the
# corresponding compiled protobuf module so that code can select the right
# one after negotiation with a simple dict lookup.
_CAP_PB2 = {"v1beta": cap_pb2_v1beta, "v1alpha": cap_pb2_v1alpha}
_CAP_GRPC = {"v1beta": cap_grpc_v1beta, "v1alpha": cap_grpc_v1alpha}
_FO_SVC_PB2 = {"v1beta": fo_svc_pb2_v1beta, "v1alpha": fo_svc_pb2_v1alpha}
_FO_PB2 = {"v1beta": fo_pb2_v1beta, "v1alpha": fo_pb2_v1alpha}
_FO_GRPC = {"v1beta": fo_grpc_v1beta, "v1alpha": fo_grpc_v1alpha}
_FF_SVC_PB2 = {"v1beta": ff_svc_pb2_v1beta, "v1alpha": ff_svc_pb2_v1alpha}
_FF_GRPC = {"v1beta": ff_grpc_v1beta, "v1alpha": ff_grpc_v1alpha}
_MD_PB2 = {"v1alpha": md_pb2_v1alpha}
_MD_GRPC = {"v1alpha": md_grpc_v1alpha}

NUCLEUS_HASH_METADATA_KEY = "nv-omniverse-nucleus-hash"

_OPENID_REDIRECT_URL = "http://127.0.0.1:35101/openid"
_OPENID_REDIRECT_HOST = "127.0.0.1"
_OPENID_REDIRECT_PORT = 35101
_OPENID_REDIRECT_PATH = "/openid"
_DEFAULT_AUTH_SCOPE = "openid"
_DEFAULT_AUTH_CLIENT_NAME = "client_library"
_FALLBACK_AUTH_CLIENT_NAMES = ("wrapp",)
_AUTH_CLIENT_NAME_ENV_VARS = ("WRAPP_STORAGE_API_AUTH_CLIENT_NAME", "OMNI_STORAGE_CLIENT_NAME")
_DEFAULT_KEYRING_SERVICE_NAME = "wrapp.storageapi.discovery"
_KEYRING_SERVICE_NAME_ENV_VAR = "WRAPP_STORAGE_API_KEYRING_SERVICE"
_AUTH_ATTEMPT_LIMIT = 2
_AUTH_REFRESH_MARGIN_SECONDS = 300
_STORAGE_API_VERSION_ENV_VARS = {
    "capabilities": "WRAPP_STORAGE_API_CAPABILITIES",
    "fileobject": "WRAPP_STORAGE_API_FILEOBJECT",
    "filefolder": "WRAPP_STORAGE_API_FILEFOLDER",
    "metadata": "WRAPP_STORAGE_API_METADATA",
}
_STORAGE_API_DEFAULT_VERSION_ORDER = {
    "capabilities": ("v1beta", "v1alpha"),
    "fileobject": ("v1alpha", "v1beta"),
    "filefolder": ("v1beta", "v1alpha"),
    "metadata": ("v1alpha",),
}
_STORAGE_API_ALLOWED_VERSIONS = {
    "capabilities": tuple(_CAP_PB2.keys()),
    "fileobject": tuple(_FO_SVC_PB2.keys()),
    "filefolder": tuple(_FF_SVC_PB2.keys()),
    "metadata": tuple(_MD_PB2.keys()),
}
_UPLOAD_RETRIES_ENV_VAR = "WRAPP_STORAGE_API_UPLOAD_RETRIES"
_LEGACY_UPLOAD_RETRIES_ENV_VAR = "WRAPP_STORAGEAPI_UPLOAD_RETRIES"
_DOWNLOAD_RETRIES_ENV_VAR = "WRAPP_STORAGE_API_DOWNLOAD_RETRIES"
_LEGACY_DOWNLOAD_RETRIES_ENV_VAR = "WRAPP_STORAGEAPI_DOWNLOAD_RETRIES"
_ENABLE_RANGE_DOWNLOAD_ENV_VAR = "WRAPP_STORAGE_API_ENABLE_RANGE_DOWNLOAD"
_LEGACY_ENABLE_RANGE_DOWNLOAD_ENV_VAR = "WRAPP_STORAGEAPI_ENABLE_RANGE_DOWNLOAD"
_DOWNLOAD_RANGE_PART_SIZE_ENV_VAR = "WRAPP_STORAGE_API_DOWNLOAD_RANGE_PART_SIZE"
_LEGACY_DOWNLOAD_RANGE_PART_SIZE_ENV_VAR = "WRAPP_STORAGEAPI_DOWNLOAD_RANGE_PART_SIZE"
_DOWNLOAD_RANGE_MAX_CONCURRENCY_ENV_VAR = "WRAPP_STORAGE_API_DOWNLOAD_RANGE_MAX_CONCURRENCY"
_LEGACY_DOWNLOAD_RANGE_MAX_CONCURRENCY_ENV_VAR = "WRAPP_STORAGEAPI_DOWNLOAD_RANGE_MAX_CONCURRENCY"
_DISABLE_METADATA_ENV_VAR = "WRAPP_STORAGE_API_DISABLE_METADATA"
_METADATA_RETRIES_ENV_VAR = "WRAPP_STORAGE_API_METADATA_RETRIES"
_READ_RETRIES_ENV_VAR = "WRAPP_STORAGE_API_READ_RETRIES"
_DISABLE_ENUMERATE_ENV_VAR = "WRAPP_STORAGE_API_DISABLE_ENUMERATE"
_DEFAULT_STORAGEAPI_DOWNLOAD_RANGE_PART_SIZE = 8 * 1024 * 1024
_DEFAULT_STORAGEAPI_DOWNLOAD_RANGE_MAX_CONCURRENCY = 8
_STORAGEAPI_DOWNLOAD_CHUNK_SIZE = 4 * 1024 * 1024
_STORAGEAPI_WRITE_CHUNK_SIZE = 1024 * 1024
_CONTENT_RANGE_PATTERN = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_RETRYABLE_GRPC_STATUS_CODES = {
    grpc.StatusCode.ABORTED,
    grpc.StatusCode.DEADLINE_EXCEEDED,
    grpc.StatusCode.RESOURCE_EXHAUSTED,
    grpc.StatusCode.UNAVAILABLE,
}


def _presigned_url(url: str) -> "yarl.URL":
    """Wrap a presigned/redirect URL so aiohttp uses it byte-exact.

    yarl re-normalizes percent-encoding by default (e.g. %2F -> / inside the
    X-Amz-Credential query value of S3/CloudFront presigned URLs), which
    breaks the signature and yields 403 AccessDenied. encoded=True disables
    the re-encoding.
    """
    return yarl.URL(url, encoded=True)


def _encode_resource_address_for_storage_service(resource_address: str) -> str:
    parsed = urllib.parse.urlsplit(resource_address.replace("\\", "/"))
    quoted_path = urllib.parse.quote(parsed.path, safe="/")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, quoted_path, parsed.query, parsed.fragment))


def _decode_resource_address_from_storage_service(resource_address: str) -> str:
    parsed = urllib.parse.urlsplit(resource_address.replace("\\", "/"))
    unquoted_path = urllib.parse.unquote(parsed.path)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, unquoted_path, parsed.query, parsed.fragment))


def _stat_snapshot(st: os.stat_result) -> Tuple[int, int, int, int]:
    # ctime_ns is included because a same-size rewrite can restore mtime (e.g.
    # touch -r) but cannot restore ctime on POSIX. On Windows st_ctime_ns is the
    # creation time, so an in-place same-size rewrite with restored mtime is not
    # detectable there without re-hashing.
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


class _WriteSource:
    """Lazy, re-openable content source for Storage API writes (OMPE-74369).

    Path-backed sources defer opening the local file until the caller holds a
    NetworkDataTransfer slot, so pending transfers hold neither a file handle
    nor the file's content in memory. Bytes-backed sources preserve the public
    write_file()/write_file_content() bytes contract. slice() creates a bounded
    view for multipart parts. Every open starts at the slice origin, so retries
    rewind for free by re-opening.

    Path-backed sources capture a stat snapshot at creation and validate it on
    every deferred open: a file that changed while the transfer was queued fails
    cleanly instead of uploading bytes that no longer match the advertised size
    and the content hash computed up front.
    """

    def __init__(
        self,
        *,
        path: Optional[str] = None,
        data: Optional[bytes] = None,
        offset: int = 0,
        size: Optional[int] = None,
        snapshot: Optional[Tuple[int, int, int, int]] = None,
    ):
        assert (path is None) != (data is None)
        self._path = path
        self._data = data
        self._offset = offset
        if snapshot is None and path is not None:
            snapshot = _stat_snapshot(os.stat(path))
        self._snapshot = snapshot
        # size=None means "the whole content"; slices always carry an explicit size.
        self._is_slice = size is not None
        if size is not None:
            self._size = size
        elif data is not None:
            self._size = len(data)
        else:
            assert snapshot is not None
            self._size = snapshot[0]

    @classmethod
    def from_path(cls, path: str) -> "_WriteSource":
        return cls(path=path)

    @classmethod
    def from_open_file(cls, path: str, f: BinaryIO) -> "_WriteSource":
        """Path-backed source whose snapshot is taken from an already-open handle.

        Lets a caller that just hashed the file bind the source to the exact
        state it hashed, with no stat-after-hash window.
        """
        return cls(path=path, snapshot=_stat_snapshot(os.fstat(f.fileno())))

    @classmethod
    def from_bytes(cls, data: bytes) -> "_WriteSource":
        return cls(data=data)

    @property
    def size(self) -> int:
        return self._size

    def slice(self, offset: int, length: int) -> "_WriteSource":
        length = max(0, min(length, self._size - offset))
        return _WriteSource(path=self._path, data=self._data, offset=self._offset + offset, size=length, snapshot=self._snapshot)

    def ensure_unchanged(self) -> None:
        """Re-stat a path-backed source and fail if it changed since the snapshot.

        For transfers that complete without ever opening the source (e.g. a
        server-side zero-byte write), this is the only change check that runs.
        """
        if self._path is None or self._snapshot is None:
            return
        if _stat_snapshot(os.stat(self._path)) != self._snapshot:
            raise StorageOperationError(
                f"Source file {self._path} changed while the upload was queued; "
                f"aborting so the stored content hash matches the uploaded bytes"
            )

    def _open_validated(self) -> BinaryIO:
        """Open the backing file, verify it still matches the snapshot, seek to the slice origin."""
        assert self._path is not None
        f = open(self._path, "rb")
        try:
            if self._snapshot is not None and _stat_snapshot(os.fstat(f.fileno())) != self._snapshot:
                raise StorageOperationError(
                    f"Source file {self._path} changed while the upload was queued; "
                    f"aborting so the stored content hash matches the uploaded bytes"
                )
            if self._offset:
                f.seek(self._offset)
        except BaseException:
            f.close()
            raise
        return f

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        """Open the content for reading, positioned at the slice origin.

        Path-backed sources open the local file — call only while holding a
        transfer slot. Reads on a sliced source are not bounded to the slice
        length; use read_bytes() for slices.
        """
        if self._data is not None:
            yield io.BytesIO(self._data[self._offset : self._offset + self._size])
            return
        f = self._open_validated()
        try:
            yield f
        finally:
            f.close()

    def read_bytes(self) -> bytes:
        """The (possibly sliced) content as bytes — call only while holding a transfer slot."""
        if self._data is not None:
            return self._data[self._offset : self._offset + self._size]
        f = self._open_validated()
        try:
            return f.read(self._size)
        finally:
            f.close()

    @contextmanager
    def open_payload(self) -> Iterator[Union[bytes, BinaryIO]]:
        """The content as an aiohttp request body — call only while holding a transfer slot.

        Bytes-backed sources yield the bytes (wire-identical to the pre-OMPE-74369
        behavior). A path-backed whole file yields the open file object, which
        aiohttp streams with a Content-Length derived via fstat (no chunked
        transfer-encoding, as presigned URLs require). A path-backed slice yields
        bytes bounded by the slice length, because fstat on the raw file object
        would report the wrong size.
        """
        if self._data is not None or self._is_slice:
            yield self.read_bytes()
            return
        f = self._open_validated()
        try:
            yield f
        finally:
            f.close()


class _HashingPayloadReader(io.BufferedIOBase):
    """File-object request body that hashes the bytes aiohttp actually reads.

    Delegates fileno()/seek()/tell() so aiohttp still derives Content-Length via
    fstat and sends a non-chunked body. The hash is only usable if the body was
    read sequentially from position 0; any other access pattern flags it invalid
    and the upload then stores no hash rather than a possibly-wrong one.

    Deliberately does NOT expose the wrapped file's .name: this keeps the
    request headers identical to the historical bytes body
    (application/octet-stream, no Content-Disposition) instead of a MIME type
    guessed from the local filename — headers a presigned signature may not
    cover.
    """

    def __init__(self, f: BinaryIO):
        self._f = f
        self.hasher = IncrementalNucleusHasher()
        self._expected_pos = f.tell()
        if self._expected_pos != 0:
            self.hasher.invalidate()

    def read(self, size: Optional[int] = -1) -> bytes:
        pos = self._f.tell()
        data = self._f.read(-1 if size is None else size)
        if pos != self._expected_pos:
            self.hasher.invalidate()
        elif data:
            self.hasher.update(data)
            self._expected_pos = pos + len(data)
        return data

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        result = self._f.seek(offset, whence)
        if result != self._expected_pos:
            self.hasher.invalidate()
        return result

    def tell(self) -> int:
        return self._f.tell()

    def fileno(self) -> int:
        return self._f.fileno()


class _AuthFailed(Exception):
    pass


class _AuthCancelled(Exception):
    pass


class _RangeDownloadNotSupported(Exception):
    pass


class _StorageAPIAuthState:
    def __init__(self):
        self.access_token: str = ""
        self.refresh_token: str = ""
        self.expires_at: Optional[float] = None

    def clear(self):
        self.access_token = ""
        self.refresh_token = ""
        self.expires_at = None


class StorageAPIBackendServerInfo(ServerInfo):
    def supports_create_with_hash(self, target_url: str) -> bool:
        return False

    @property
    def auth_token(self) -> str:
        return ""


class StorageAPIListEntry(ListEntry):
    def __init__(
        self,
        resource_address,
        resource_identity: Optional[str],
        size: Optional[int],
        modification_time: Optional[datetime],
        is_folder=False,
    ):
        self._resource_address = resource_address
        self._resource_identity = resource_identity
        self._data_object_size = size
        self._modification_time: Optional[datetime] = modification_time
        self._is_folder = is_folder
        self._nucleus_hash: Optional[str] = None

    @staticmethod
    def from_response_header(
        resource_address: str, resource_identity: Optional[str], meta_dict: Dict[str, str], is_folder: bool = False
    ) -> "StorageAPIListEntry":
        timestamp = None
        if "last_modified_timestamp" in meta_dict:
            stored_timestamp = meta_dict["last_modified_timestamp"]
            if stored_timestamp is not None:
                timestamp = datetime.fromisoformat(stored_timestamp)
        if "data_object_size" in meta_dict:
            size = int(meta_dict["data_object_size"])
        else:
            size = None
        return StorageAPIListEntry(
            resource_address,
            resource_identity,
            size,
            modification_time=timestamp,
            is_folder=is_folder,
        )

    @staticmethod
    def from_resource_info(resource_address: str, resource_info):
        modification_time = resource_info.metadata.last_modified_timestamp
        if modification_time is not None:
            modification_time = modification_time.ToDatetime()
        return StorageAPIListEntry(
            resource_address=resource_address,
            resource_identity=resource_info.resource_identity.encoded_identity,
            size=resource_info.metadata.data_object_size,
            modification_time=modification_time,
            is_folder=False,
        )

    @staticmethod
    def relative_resource_address(base_resource_address: str, item_resource_address: str) -> str:
        """
        Convert a backend-returned resource address into a path relative to the listed base.

        Storage API servers may return addresses in different canonical forms (absolute URL,
        path-only URL, or with an extra bucket segment). This method normalizes those variants
        so callers can consistently use catalog relative paths.
        """
        normalized_base = _decode_resource_address_from_storage_service(base_resource_address).rstrip("/")
        normalized_item = _decode_resource_address_from_storage_service(item_resource_address)

        # Fast path: exact URL/prefix style match.
        base_prefix = normalized_base + "/"
        if normalized_item.startswith(base_prefix):
            return normalized_item[len(base_prefix) :].lstrip("/\\")
        if normalized_item == normalized_base:
            return ""

        # Fallback: compare path components and tolerate canonicalization differences
        # (e.g. virtual-hosted vs path-style S3 addressing).
        base_path_parts = [p for p in get_path_from_url(normalized_base).strip("/").split("/") if p]
        item_path_parts = [p for p in get_path_from_url(normalized_item).strip("/").split("/") if p]

        if base_path_parts and len(item_path_parts) >= len(base_path_parts):
            window = len(base_path_parts)
            for i in range(len(item_path_parts) - window + 1):
                if item_path_parts[i : i + window] == base_path_parts:
                    return "/".join(item_path_parts[i + window :]).lstrip("/\\")

        # Last resort: preserve previous behavior for non-matching path forms.
        return normalized_item.lstrip("/\\")

    @staticmethod
    def from_list_item(base_resource_address: str, item):
        modification_time = item.resource_info.metadata.last_modified_timestamp
        if modification_time is not None:
            modification_time = modification_time.ToDatetime()
        return StorageAPIListEntry(
            resource_address=StorageAPIListEntry.relative_resource_address(base_resource_address, item.resource_address),
            resource_identity=item.resource_info.resource_identity.encoded_identity,
            size=item.resource_info.metadata.data_object_size,
            modification_time=modification_time,
            is_folder=False,
        )

    @property
    def is_folder(self) -> bool:
        return self._is_folder

    @property
    def is_regular_file(self) -> bool:
        return not self._is_folder

    @property
    def has_checkpoint(self) -> bool:
        # "Checkpoints" in StorageAPI are resource identities
        return True

    @property
    def version(self) -> str:
        return self._resource_identity if self._resource_identity else ""

    @property
    def relative_path(self) -> str:
        return self._resource_address

    @property
    def nucleus_hash(self) -> Optional[str]:
        return self._nucleus_hash

    @nucleus_hash.setter
    def nucleus_hash(self, value: str):
        if self.nucleus_hash is not None:
            raise AttributeError("Nucleus hash cannot be changed once set")
        self._nucleus_hash = value

    @property
    def size(self) -> Optional[int]:
        return self._data_object_size if self.is_regular_file else None

    @property
    def modified_time(self) -> Optional[datetime]:
        return self._modification_time


class StorageAPIResourceIdentity(storage_interface.ResourceIdentity):
    def __init__(self, resource_identity: str):
        self._resource_identity = resource_identity

    async def read_data(self, storage) -> Tuple[Result, bytes]:
        return await storage.read_resource_identity(self._resource_identity)

    def to_string(self) -> str:
        return self._resource_identity


class StorageAPIStorage(StorageInterface):
    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        raise NotImplementedError

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        return False

    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        return Result.OK, StorageAPIBackendServerInfo()

    async def normalize(self, url: str) -> str:
        return url

    def directory_semantics(self) -> DirectorySemantics:
        return DirectorySemantics.PREFIX

    def supports_tags(self, url: str) -> bool:
        return False

    def supports_comments(self) -> bool:
        return False

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        raise NotImplementedError

    def is_case_sensitive(self, url: str) -> bool:
        return False

    def can_be_cached(self, url: str) -> bool:
        return False

    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        return None

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        raise NotImplementedError

    def add_auth_info(self, auth_info: StorageAuthInfo):
        pass

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        pass


class StorageAPIStorageGRPC(StorageAPIStorage):
    """Storage backend that communicates with a Storage API service over gRPC.

    **Lifecycle**

    The constructor only stores configuration — no network I/O happens here.
    Connection, authentication, and protocol negotiation are performed by
    :meth:`eager_initialize` (called during ``wrapp_initialize``) or lazily
    by :meth:`_ensure_grpc_stubs` on the first storage operation.

    **Configuration** (environment variables)

    ``WRAPP_STORAGE_API_DISCOVERY_URL``
        Base URL of the REST discovery service. When set, the gRPC endpoint
        is resolved automatically via ``GET <url>/api/v1/services``.
    ``WRAPP_STORAGE_API_GRPC_ADDRESS``
        Direct ``host:port`` of the gRPC endpoint. Used only when the discovery
        URL is empty. No default — when neither this nor the discovery URL is
        set, the backend is inert: it does not claim URLs and the Router falls
        through to the dedicated backends (S3, Azure, GCS, ...).
    ``WRAPP_STORAGE_API_CAPABILITIES``, ``WRAPP_STORAGE_API_FILEOBJECT``,
    ``WRAPP_STORAGE_API_FILEFOLDER``, ``WRAPP_STORAGE_API_METADATA``
        Optional per-service API version pins (for example ``v1alpha`` or
        ``v1beta``). If set, WRAPP fails fast when that pinned version is not
        advertised by the server.
    ``WRAPP_STORAGE_API_DISABLE_ENUMERATE``
        Disable the Storage API ``Enumerate`` tree-walk path and fall back to
        recursive ``ListStat`` traversal.
    ``WRAPP_STORAGE_API_READ_RETRIES``, ``WRAPP_STORAGE_API_METADATA_RETRIES``
        Number of attempts for retryable read-side gRPC calls, including
        streams, and metadata gRPC calls (default ``3``).

    **URL routing**

    After connecting, the backend populates route data so that the Router can
    decide which URLs belong to this service:

    1. ``ListRoutes`` wildcard patterns (preferred), or
    2. ``ListTopLevelAddresses`` prefix list (fallback).

    Before the first connection, :meth:`is_url_in_scope` is permissive
    (returns ``True``) so that lazy initialization can trigger. After
    connecting, unrecognised URLs are rejected.
    """

    @classmethod
    def version_info(cls) -> str:
        return "Storage API gRPC Backend 0.0.1"

    def __init__(
        self,
        wrapp_version: str,
        debug_level: bool,
        throttling_params: ThrottlingParameters,
        use_enumerate: Optional[bool] = None,
        wrapp_config: Optional[WrappConfig] = None,
        backend_config: Optional[StorageAPIBackendConfig] = None,
    ):
        self._wrapp_version = wrapp_version
        self._debug_level = debug_level
        # ``default`` is the instance configured by the global WRAPP_STORAGE_API_*
        # env vars; a named instance (backend_config is not None) is configured
        # purely from its [storage_api.<name>] wrapp.toml table, with no env-var
        # fallback for its endpoint/discovery URL.
        self._name = backend_config.name if backend_config is not None else DEFAULT_STORAGE_API_BACKEND_NAME
        if backend_config is None:
            self._endpoint = os.getenv("WRAPP_STORAGE_API_GRPC_ADDRESS", "")
            discovery_env = os.getenv("WRAPP_STORAGE_API_DISCOVERY_URL", "")
        else:
            self._endpoint = ""
            discovery_env = ""
        self._discovery_url = self._normalize_discovery_url(discovery_env)
        self._interactive_fallback = False
        self._channel: Optional[grpc.aio.Channel] = None
        self._fileobject_api: Any = None
        self._filefolder_api: Any = None
        self._capability_api: Any = None
        self._metadata_api: Any = None
        # Production uses Enumerate by default; opt out per-process via env.
        # Tests pass an explicit bool to pin behavior regardless of env state.
        if use_enumerate is None:
            disable_enumerate = self._parse_bool_env(_DISABLE_ENUMERATE_ENV_VAR, False)
            self._use_enumerate = not disable_enumerate
            if disable_enumerate:
                logger.info(
                    "StorageAPI Enumerate disabled by %s; falling back to recursive ListStat traversal",
                    _DISABLE_ENUMERATE_ENV_VAR,
                )
        else:
            self._use_enumerate = use_enumerate
        self._top_level_addresses: Optional[List[str]] = None
        self._routes: Optional[List[str]] = None
        self._route_regexes: Optional[List[re.Pattern]] = None
        # Route override loaded from wrapp.toml. When set, _route_regexes is
        # populated with these patterns after _fetch_routes/_fetch_top_level_addresses
        # have run (the server's response is still logged for diagnostics).
        self._config_route_overrides: Optional[List[str]] = None
        self._config_route_regexes: Optional[List[re.Pattern]] = None
        # Negotiated proto modules — set by _negotiate_versions()
        self._fileobject_msgs: Any = None  # fileobject_service_pb2 (StatRequest, WriteRequest, …)
        self._fileobject_types: Any = None  # fileobject_pb2 (Chunk, Header, ResourceIdentity, …)
        self._filefolder_msgs: Any = None  # filefolder_service_pb2 (ListRequest, FolderAddress, …)
        self._capabilities_msgs: Any = None  # capabilities_pb2 (ListServicesRequest, …)
        self._metadata_msgs: Any = None  # metadata_pb2 (GetMetadataRequest, UpdateMetadataRequest, …)
        self._fileobject_version: Optional[str] = None
        self._filefolder_version: Optional[str] = None
        self._capabilities_version: Optional[str] = None
        self._metadata_version: Optional[str] = None
        self._aiohttp_session: Optional[aiohttp.ClientSession] = None
        self._bucket_region_cache: Dict[str, str] = {}
        self._manual_access_tokens: Dict[str, str] = {}
        self._client_credentials: Dict[str, StorageAPIClientCredentialsAuthInfo] = {}
        self._auth_state = _StorageAPIAuthState()
        self._auth_lock = asyncio.Lock()
        self._service_version_overrides = self._parse_service_version_overrides()
        self._enable_range_download = self._parse_bool_env(
            _ENABLE_RANGE_DOWNLOAD_ENV_VAR,
            True,
            legacy_env_var=_LEGACY_ENABLE_RANGE_DOWNLOAD_ENV_VAR,
        )
        self._download_range_part_size = self._parse_positive_int_env(
            _DOWNLOAD_RANGE_PART_SIZE_ENV_VAR,
            _DEFAULT_STORAGEAPI_DOWNLOAD_RANGE_PART_SIZE,
            legacy_env_var=_LEGACY_DOWNLOAD_RANGE_PART_SIZE_ENV_VAR,
        )
        self._download_range_max_concurrency = self._parse_positive_int_env(
            _DOWNLOAD_RANGE_MAX_CONCURRENCY_ENV_VAR,
            _DEFAULT_STORAGEAPI_DOWNLOAD_RANGE_MAX_CONCURRENCY,
            legacy_env_var=_LEGACY_DOWNLOAD_RANGE_MAX_CONCURRENCY_ENV_VAR,
        )
        self._metadata_disabled = self._parse_bool_env(_DISABLE_METADATA_ENV_VAR, False)
        self._metadata_max_attempts = self._parse_positive_int_env(_METADATA_RETRIES_ENV_VAR, 3)
        self._read_max_attempts = self._parse_positive_int_env(_READ_RETRIES_ENV_VAR, 3)
        # Use throttling_params for throttler initialization
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "StorageAPI", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "StorageAPI", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "StorageAPI", throttling_params.jobs)

        if backend_config is None:
            # Default instance: apply [storage_api.default] with env precedence.
            if wrapp_config is None:
                wrapp_config = load_wrapp_config()
            self._apply_backend_config(
                wrapp_config.default_storage_api,
                source_path=wrapp_config.source_path,
                discovery_env=discovery_env,
            )
        else:
            # Named instance: apply its own table (discovery_env is "" so the
            # table's discovery_url is adopted, not overridden by env).
            self._apply_backend_config(
                backend_config,
                source_path=wrapp_config.source_path if wrapp_config is not None else None,
                discovery_env=discovery_env,
            )

    def _apply_backend_config(
        self,
        backend: Optional[StorageAPIBackendConfig],
        *,
        source_path: Optional[object],
        discovery_env: str,
    ) -> None:
        """Apply a single ``[storage_api.<name>]`` table on top of any env-var
        configuration for this instance.

        Precedence (default instance only): env vars > wrapp.toml > built-in
        defaults. When both an env var and a TOML value are set for
        ``discovery_url`` and they disagree, the env var wins and a WARNING is
        emitted naming the TOML source path so that the override is never
        silent. Named instances pass ``discovery_env=""`` and therefore always
        adopt their table's ``discovery_url``.
        """
        if backend is None:
            return

        source = source_path
        label = f"[storage_api.{self._name}]"
        if backend.discovery_url:
            toml_url = self._normalize_discovery_url(backend.discovery_url)
            if discovery_env:
                if self._discovery_url != toml_url:
                    logger.warning(
                        "wrapp config %s sets %s.discovery_url=%r "
                        "but WRAPP_STORAGE_API_DISCOVERY_URL=%r is also set; "
                        "the environment variable wins.",
                        source,
                        label,
                        toml_url,
                        self._discovery_url,
                    )
            else:
                logger.info(
                    "wrapp config %s: using %s.discovery_url=%r",
                    source,
                    label,
                    toml_url,
                )
                self._discovery_url = toml_url

        if backend.routes:
            if not self._endpoint and not self._discovery_url:
                logger.error(
                    "wrapp config %s: %s.routes is set but no gRPC endpoint or "
                    "discovery URL is configured; routes will be ignored.",
                    source,
                    label,
                )
            else:
                patterns = list(backend.routes)
                self._config_route_overrides = patterns
                self._config_route_regexes = [self._compile_route_pattern(p) for p in patterns]
                logger.info(
                    "wrapp config %s: using %s.routes (%d pattern(s)) as the authoritative route table; "
                    "any server-advertised routes/TLAs will be logged for diagnostics only.",
                    source,
                    label,
                    len(patterns),
                )
                # The override is authoritative regardless of whether
                # discovery later succeeds, so seed the live route table
                # now.  This makes ``has_known_scope()`` return True from
                # construction time and lets the Router claim the
                # configured URLs on the first routing decision.  After a
                # successful connect,
                # ``_apply_config_route_overrides_to_scope()`` reasserts
                # the override on top of whatever ``_fetch_routes``
                # returned.
                self._routes = list(patterns)
                self._route_regexes = list(self._config_route_regexes)

    def _apply_config_route_overrides_to_scope(self) -> None:
        """If a route override was loaded from wrapp.toml, install it as the
        authoritative scope. Called after ``_fetch_routes`` /
        ``_fetch_top_level_addresses`` so the server's response is always
        fetched and logged before being shadowed.

        Note: the live route table is already seeded at construction time
        from the same override (see ``_apply_backend_config``).  This method
        exists to reassert the override in case ``_fetch_routes`` later
        overwrote ``_routes`` with what the server advertised.
        """
        if self._config_route_regexes is None or self._config_route_overrides is None:
            return
        if self._routes is not None and self._routes != self._config_route_overrides:
            logger.info(
                "wrapp config route override active; replacing server-advertised routes %s with configured routes %s.",
                self._routes,
                self._config_route_overrides,
            )
        elif self._top_level_addresses is not None:
            logger.info(
                "wrapp config route override active; replacing server-advertised top-level addresses %s with configured routes %s.",
                self._top_level_addresses,
                self._config_route_overrides,
            )
        self._routes = list(self._config_route_overrides)
        self._route_regexes = list(self._config_route_regexes)
        # Once routes are authoritative, stop using TLAs for matching so that
        # is_url_in_scope() takes the route-based branch unambiguously.
        self._top_level_addresses = None

    @staticmethod
    def _normalize_discovery_url(server_url: str) -> str:
        normalized = server_url.strip()
        if not normalized:
            return ""
        if "://" not in normalized:
            normalized = f"https://{normalized}"
        return normalized.rstrip("/")

    @staticmethod
    def _resolve_env_value(env_var: str, default: str, legacy_env_var: Optional[str] = None) -> Tuple[str, str]:
        if env_var in os.environ:
            return os.environ[env_var], env_var
        if legacy_env_var and legacy_env_var in os.environ:
            logger.warning("Environment variable %s is deprecated; use %s instead", legacy_env_var, env_var)
            return os.environ[legacy_env_var], legacy_env_var
        return default, env_var

    @staticmethod
    def _parse_bool_env(env_var: str, default: bool, legacy_env_var: Optional[str] = None) -> bool:
        value, _ = StorageAPIStorageGRPC._resolve_env_value(env_var, str(default), legacy_env_var)
        return value.lower() in ["true", "1", "yes"]

    @staticmethod
    def _parse_positive_int_env(env_var: str, default: int, legacy_env_var: Optional[str] = None) -> int:
        raw, configured_env_var = StorageAPIStorageGRPC._resolve_env_value(env_var, str(default), legacy_env_var)
        try:
            parsed = int(raw)
        except (ValueError, TypeError):
            parsed = default
            logger.warning("%s=%r is not a valid integer, falling back to default %d", configured_env_var, raw, default)
        if parsed < 1:
            logger.warning("%s=%d is less than 1, clamping to 1", configured_env_var, parsed)
        return max(1, parsed)

    @staticmethod
    def _is_retryable_grpc_error(error: grpc.RpcError) -> bool:
        code = error.code()
        if code in _RETRYABLE_GRPC_STATUS_CODES:
            return True
        details = ""
        details_method = getattr(error, "details", None)
        if callable(details_method):
            details = details_method() or ""
        details_lower = details.lower()
        if code == grpc.StatusCode.UNKNOWN:
            return "timeout" in details_lower
        if code == grpc.StatusCode.INTERNAL:
            return any(
                marker in details_lower
                for marker in (
                    "connection reset",
                    "recvmsg",
                    "rst_stream",
                    "timeout",
                )
            )
        return False

    def _parse_service_version_overrides(self) -> Dict[str, Optional[str]]:
        """Parse and validate per-service version override env vars."""
        overrides: Dict[str, Optional[str]] = {}
        for service_name, env_var in _STORAGE_API_VERSION_ENV_VARS.items():
            raw_value = os.getenv(env_var, "").strip()
            if not raw_value:
                overrides[service_name] = None
                continue

            normalized_value = raw_value.lower()
            allowed_versions = _STORAGE_API_ALLOWED_VERSIONS[service_name]
            if normalized_value not in allowed_versions:
                raise StorageInitializationError(
                    f"Invalid Storage API version override {env_var}={raw_value!r}. "
                    f"Supported values for {service_name}: {', '.join(allowed_versions)}."
                )
            overrides[service_name] = normalized_value
        return overrides

    def enable_auth_interactive_fallback(self, interactive_fallback: bool):
        self._interactive_fallback = interactive_fallback

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        auth_params = auth_param.split(",")
        if len(auth_params) == 2:
            server, token = auth_params
            if not server:
                raise StorageOperationError("Authentication parameter for Storage API must contain a server URL")
            if not token:
                raise StorageOperationError("Authentication parameter for Storage API must contain a bearer token")
            normalized_server = self._normalize_discovery_url(server)
            return StorageAuthInfo(
                normalized_server,
                StorageAPIBearerAuthInfo(server=normalized_server, bearer_token=token),
            )
        elif len(auth_params) == 3:
            server, client_id, client_secret = auth_params
            if not server:
                raise StorageOperationError("Authentication parameter for Storage API must contain a server URL")
            if not client_id:
                raise StorageOperationError("Authentication parameter for Storage API must contain a client_id")
            if not client_secret:
                raise StorageOperationError("Authentication parameter for Storage API must contain a client_secret")
            normalized_server = self._normalize_discovery_url(server)
            return StorageAuthInfo(
                normalized_server,
                StorageAPIClientCredentialsAuthInfo(server=normalized_server, client_id=client_id, client_secret=client_secret),
            )
        else:
            raise StorageOperationError(
                "Authentication parameter for Storage API needs to be of the form "
                "server_url,bearer_token or server_url,client_id,client_secret"
            )

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        self._manual_access_tokens.clear()
        self._client_credentials.clear()
        for auth in auth_info:
            self.add_auth_info(auth)

    def add_auth_info(self, auth_info: StorageAuthInfo):
        normalized_server = self._normalize_discovery_url(auth_info.server_url)
        if isinstance(auth_info.auth_data, StorageAPIBearerAuthInfo):
            self._manual_access_tokens[normalized_server] = auth_info.auth_data.bearer_token
        elif isinstance(auth_info.auth_data, StorageAPIClientCredentialsAuthInfo):
            self._client_credentials[normalized_server] = auth_info.auth_data
        else:
            raise StorageOperationError(
                f"Incompatible authentication information for server {auth_info.server_url}: "
                f"expected StorageAPIBearerAuthInfo or StorageAPIClientCredentialsAuthInfo, got {auth_info.auth_data.__class__}"
            )

    async def _ensure_aiohttp_session(self) -> aiohttp.ClientSession:
        """Ensure we have an aiohttp session for HTTP requests."""
        if self._aiohttp_session is None or self._aiohttp_session.closed:
            self._aiohttp_session = aiohttp.ClientSession()
        return self._aiohttp_session

    def _active_auth_token(self) -> str:
        if self._discovery_url:
            manual = self._manual_access_tokens.get(self._discovery_url, "")
            if manual:
                return manual
        return self._auth_state.access_token

    def _manual_auth_token_active(self) -> bool:
        return bool(self._discovery_url and self._manual_access_tokens.get(self._discovery_url, ""))

    async def _refresh_auth_if_expiring(self) -> None:
        if self._manual_auth_token_active() or not self._auth_state.access_token or self._auth_state.expires_at is None:
            return
        loop = asyncio.get_running_loop()
        if loop.time() + _AUTH_REFRESH_MARGIN_SECONDS < self._auth_state.expires_at:
            return
        async with self._auth_lock:
            if self._manual_auth_token_active() or not self._auth_state.access_token or self._auth_state.expires_at is None:
                return
            if loop.time() + _AUTH_REFRESH_MARGIN_SECONDS < self._auth_state.expires_at:
                return
            logger.debug("StorageAPI auth: access token for %s is nearing expiry; attempting refresh.", self._discovery_url)
            if not await self._refresh_auth_without_lock():
                logger.warning(
                    "StorageAPI auth: failed to refresh access token before expiry; the next request may fail with UNAUTHENTICATED."
                )

    async def _grpc_metadata(self) -> Optional[List[Tuple[str, str]]]:
        await self._refresh_auth_if_expiring()
        access_token = self._active_auth_token()
        if access_token:
            return [("authorization", f"Bearer {access_token}")]
        return None

    @staticmethod
    def _is_local_host(host: str) -> bool:
        return host in {"localhost", "127.0.0.1"}

    @classmethod
    def _grpc_target_and_secure(cls, endpoint: str) -> Tuple[str, bool]:
        if "://" not in endpoint:
            host, _, port = endpoint.partition(":")
            insecure = cls._is_local_host(host) or port in {"", "80"}
            return endpoint, not insecure

        parsed = urllib.parse.urlparse(endpoint)
        host = parsed.hostname or ""
        if not host:
            raise StorageInitializationError(f"Invalid gRPC endpoint '{endpoint}': no host configured.")
        if parsed.port:
            target = f"{host}:{parsed.port}"
        else:
            target = host

        if cls._is_local_host(host):
            return target, False
        if parsed.scheme in {"https", "grpcs"}:
            return target, True
        if parsed.scheme in {"http", "grpc"}:
            return target, False
        return target, True

    @classmethod
    def _create_grpc_channel(cls, endpoint: str) -> grpc.aio.Channel:
        target, secure = cls._grpc_target_and_secure(endpoint)
        if secure:
            credentials = grpc.ssl_channel_credentials()
            return grpc.aio.secure_channel(target, credentials)
        return grpc.aio.insecure_channel(target)

    @sync_calls_async_on_wrapp_thread
    async def eager_initialize(self):
        """Eagerly connect and fetch TLAs so is_url_in_scope() works correctly.

        Must be called after WrappEventLoopThread is initialized.
        """
        await self._ensure_grpc_stubs()

    async def _ensure_grpc_stubs(self):
        """Ensure gRPC channel and stubs are initialized within an async context."""
        if self._channel is None:
            if self._discovery_url:
                logger.debug(f"StorageAPI[{self._name}]: discovering gRPC endpoint via {self._discovery_url}")
                self._endpoint = await self._discover_storage_endpoint()
                logger.debug(f"StorageAPI[{self._name}]: discovered gRPC endpoint: {self._endpoint}")
            logger.debug(f"StorageAPI[{self._name}]: creating gRPC channel to {self._endpoint}")
            channel = self._create_grpc_channel(self._endpoint)
            self._channel = channel
            try:
                await self._negotiate_versions()
                if not await self._fetch_routes():
                    await self._fetch_top_level_addresses()
                # Apply wrapp.toml route override last so the server's
                # response is logged for diagnostics before being shadowed.
                self._apply_config_route_overrides_to_scope()
            except grpc.RpcError as e:
                await channel.close()
                self._channel = None
                raise StorageOperationError(f"Failed to connect to Storage API gRPC endpoint {self._endpoint}: {e.details()}") from e

    async def _negotiate_versions(self):
        """Discover which API versions the server supports and select the best per service.

        Uses per-service defaults and optional per-service env overrides:
        capabilities/filefolder prefer v1beta, fileobject prefers v1alpha.
        Any explicit override must be advertised by the server or negotiation
        fails fast.
        """

        def _selection_order(service_name: str) -> Tuple[str, ...]:
            pinned_version = self._service_version_overrides.get(service_name)
            if pinned_version:
                return (pinned_version,)
            return _STORAGE_API_DEFAULT_VERSION_ORDER[service_name]

        # Bootstrap CapabilitiesService by trying the selected order.
        cap_candidates = _selection_order("capabilities")
        cap_override = self._service_version_overrides.get("capabilities")
        cap_stub = None
        cap_msgs = None
        cap_version = None
        response = None
        for idx, ver in enumerate(cap_candidates):
            try:
                trial_stub = _CAP_GRPC[ver].CapabilitiesServiceStub(self._channel)
                trial_msgs = _CAP_PB2[ver]
                response = await self._run_grpc(
                    self._throttler_roundtrip,
                    trial_stub.ListServices,
                    trial_msgs.ListServicesRequest(),
                )
                cap_stub = trial_stub
                cap_msgs = trial_msgs
                cap_version = ver
                break
            except grpc.RpcError as e:
                has_next = idx < len(cap_candidates) - 1
                if e.code() == grpc.StatusCode.UNIMPLEMENTED and has_next:
                    logger.debug(f"{ver} CapabilitiesService not available, trying {cap_candidates[idx + 1]}")
                    continue
                if cap_override:
                    raise StorageInitializationError(
                        "Storage API capabilities version override "
                        f"{_STORAGE_API_VERSION_ENV_VARS['capabilities']}={cap_override!r} is not available."
                    ) from e
                raise

        if response is None or cap_stub is None or cap_msgs is None or cap_version is None:
            raise StorageInitializationError("Failed to negotiate Storage API capabilities service version.")

        logger.debug(
            "StorageAPI capabilities ListServices response from %s using %s: %s",
            self._endpoint,
            cap_version,
            json.dumps(self._list_services_response_for_log(response), sort_keys=True),
        )

        # Parse available versions per service from the ListServices response.
        available: Dict[str, List[str]] = {}
        for svc in response.services:
            available.setdefault(svc.service_name, []).extend(svc.service_versions)

        def _pick(service_name: str) -> str:
            versions = available.get(service_name, [])
            preferred_versions = _selection_order(service_name)
            for preferred in preferred_versions:
                if preferred in versions:
                    return preferred
            pinned_version = self._service_version_overrides.get(service_name)
            if pinned_version:
                raise StorageInitializationError(
                    "Storage API version override "
                    f"{_STORAGE_API_VERSION_ENV_VARS[service_name]}={pinned_version!r} is not available "
                    f"(server advertised {service_name}: {versions})."
                )
            raise StorageInitializationError(
                f"Server does not support {service_name} in {', '.join(preferred_versions)} (available: {versions})"
            )

        # Capabilities
        self._capabilities_version = cap_version
        self._capabilities_msgs = cap_msgs
        self._capability_api = cap_stub

        # FileObject
        fo_ver = _pick("fileobject")
        self._fileobject_version = fo_ver
        self._fileobject_msgs = _FO_SVC_PB2[fo_ver]
        self._fileobject_types = _FO_PB2[fo_ver]
        self._fileobject_api = _FO_GRPC[fo_ver].FileObjectServiceStub(self._channel)

        # FileFolder
        ff_ver = _pick("filefolder")
        self._filefolder_version = ff_ver
        self._filefolder_msgs = _FF_SVC_PB2[ff_ver]
        self._filefolder_api = _FF_GRPC[ff_ver].FileFolderServiceStub(self._channel)

        # Metadata (optional -- not all servers expose this service)
        md_ver: Optional[str] = None
        if self._metadata_disabled:
            # Client-side opt-out: skip stubbing the Metadata service even when
            # the server advertises it, so subsequent writes don't trigger
            # best-effort UpdateMetadata calls (useful when the metadata
            # service is unhealthy or for benchmarks where the side-channel
            # would dominate latency).
            logger.info(
                "StorageAPI metadata service disabled by %s",
                _DISABLE_METADATA_ENV_VAR,
            )
            self._metadata_version = None
            self._metadata_msgs = None
            self._metadata_api = None
        else:
            md_versions = available.get("metadata", [])
            md_candidates = _selection_order("metadata")
            md_ver = next((v for v in md_candidates if v in md_versions), None)
            if md_ver:
                self._metadata_version = md_ver
                self._metadata_msgs = _MD_PB2[md_ver]
                self._metadata_api = _MD_GRPC[md_ver].MetadataServiceStub(self._channel)
            else:
                md_override = self._service_version_overrides.get("metadata")
                if md_override:
                    raise StorageInitializationError(
                        "Storage API version override "
                        f"{_STORAGE_API_VERSION_ENV_VARS['metadata']}={md_override!r} is not available "
                        f"(server advertised metadata: {md_versions})."
                    )
                self._metadata_version = None
                self._metadata_msgs = None
                self._metadata_api = None

        if self._metadata_disabled:
            md_status = "disabled"
        else:
            md_status = md_ver or "unavailable"
        logger.info(
            f"StorageAPI negotiated versions: capabilities={cap_version}, "
            f"fileobject={fo_ver}, filefolder={ff_ver}, metadata={md_status}"
        )

    @staticmethod
    def _list_services_response_for_log(response) -> List[Dict[str, object]]:
        return [
            {
                "service_name": svc.service_name,
                "service_versions": list(svc.service_versions),
            }
            for svc in response.services
        ]

    async def _fetch_routes(self) -> bool:
        """Try to fetch route patterns via ListRoutes (v1alpha only).

        Returns True if routes were successfully fetched and should be used
        instead of top-level addresses.
        """
        from wrapp.utils.router import g_list_routes_enabled

        if not g_list_routes_enabled:
            logger.debug("ListRoutes disabled via WRAPP_DISABLE_LIST_ROUTES")
            return False
        if self._capabilities_version != "v1alpha":
            logger.debug("ListRoutes requires v1alpha CapabilitiesService, skipping")
            return False
        try:
            response = await self._run_control_grpc_with_retries(
                "ListRoutes",
                self._capability_api.ListRoutes,
                self._capabilities_msgs.ListRoutesRequest(),
            )
            patterns = [route.wildcard_pattern for route in response.items if route.wildcard_pattern]
            if not patterns:
                logger.debug("ListRoutes returned no wildcard patterns, falling back to ListTopLevelAddresses")
                return False
            self._routes = patterns
            self._route_regexes = [self._compile_route_pattern(p) for p in patterns]
            logger.info(f"StorageAPI[{self._name}] routes: {self._routes}")
            return True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                logger.debug("ListRoutes not implemented by server, falling back to ListTopLevelAddresses")
            else:
                logger.warning(f"ListRoutes call failed ({e.code()}), falling back to ListTopLevelAddresses: {e}")
            return False
        except Exception as e:
            logger.warning(f"Failed to fetch routes, falling back to ListTopLevelAddresses: {e}")
            return False

    @staticmethod
    def _compile_route_pattern(pattern: str) -> re.Pattern:
        """Compile a route wildcard pattern into a regex.

        Wildcard semantics (from the proto definition):
        - ``*``  matches a single identifier ``[a-zA-Z0-9_.-]+``
        - ``**`` matches any substring (including ``/``)

        A trailing ``/**`` also matches the base URL without the slash,
        e.g. ``https://example.com/**`` matches ``https://example.com``.
        """
        parts = pattern.split("**")
        regex_parts = []
        for i, part in enumerate(parts):
            segments = part.split("*")
            escaped = [re.escape(s) for s in segments]
            literal = "[a-zA-Z0-9_.\\-]+".join(escaped)
            if i < len(parts) - 1:
                if literal.endswith("/"):
                    literal = literal[:-1] + "/?"
                regex_parts.append(literal)
                regex_parts.append(".*")
            else:
                regex_parts.append(literal)
        return re.compile("^" + "".join(regex_parts) + "$")

    @staticmethod
    def _url_matches_route(compiled_pattern: re.Pattern, url: str) -> bool:
        return compiled_pattern.match(url) is not None

    async def _fetch_top_level_addresses(self):
        """Cache the address spaces this server serves (for URL routing)."""
        try:
            response = await self._run_control_grpc_with_retries(
                "ListTopLevelAddresses",
                self._capability_api.ListTopLevelAddresses,
                self._capabilities_msgs.ListTopLevelAddressesRequest(),
            )
            self._top_level_addresses = [entry.top_level_address.rstrip("/") for entry in response.items]
            logger.info(f"StorageAPI[{self._name}] top-level addresses: {self._top_level_addresses}")
        except Exception as e:
            logger.warning(f"Failed to fetch top-level addresses, URL routing will use catch-all: {e}")
            self._top_level_addresses = None

    async def _determine_s3_bucket_region(self, bucket: str) -> str | None:
        if bucket in self._bucket_region_cache:
            return self._bucket_region_cache[bucket]
        else:
            default_region = os.getenv("WRAPP_FIXED_REGION", "None")
            if default_region != "None":
                self._bucket_region_cache[bucket] = default_region
                return default_region
            else:
                # Query real AWS for the region of that bucket
                session = await self._ensure_aiohttp_session()
                url = f"https://s3.amazonaws.com/{bucket}"
                async with session.head(url) as resp:
                    region = resp.headers.get("x-amz-bucket-region")
                if region:
                    self._bucket_region_cache[bucket] = region
                    # Do not cache misses, we might want to create the bucket later
                return region

    async def _sanitize_address_for_storage_service(self, resource_address) -> str:
        scheme, netloc, path, query, fragment = urllib.parse.urlsplit(resource_address)
        if scheme.lower() == "s3":
            # Storage service refuses to work with s3 URLs, we need to determine the region of that bucket ourselves and
            # rewrite the URL
            bucket = netloc
            region = await self._determine_s3_bucket_region(bucket)
            if region:
                host = f"{bucket}.s3.{region}.amazonaws.com"
                return str(urllib.parse.urlunsplit(["https", host, path, query, fragment]))
            else:
                raise StorageOperationError(
                    f"Bucket {bucket} for address {resource_address} doesn't exist, cannot determine region. Aborting operation."
                )
        else:
            return resource_address

    async def _discover_storage_endpoint(self) -> str:
        if not self._discovery_url:
            return self._endpoint

        async with self._auth_lock:
            manual_token = self._manual_access_tokens.get(self._discovery_url, "")
            candidate_tokens = [manual_token] if manual_token else []
            if self._auth_state.access_token and self._auth_state.access_token not in candidate_tokens:
                candidate_tokens.append(self._auth_state.access_token)
            if not candidate_tokens:
                candidate_tokens = [""]

            token_sources = []
            if manual_token:
                token_sources.append("manual")
            if self._auth_state.access_token and self._auth_state.access_token != manual_token:
                token_sources.append("cached")
            if not token_sources:
                token_sources.append("anonymous")
            logger.debug(f"StorageAPI discovery: trying candidate tokens: {token_sources}")

            for token in candidate_tokens:
                try:
                    return await self._discover_with_token(token)
                except _AuthFailed:
                    logger.debug("StorageAPI discovery: candidate token rejected (401/403)")
                    if token and token == manual_token:
                        self._manual_access_tokens.pop(self._discovery_url, None)
                        raise FailedCommand(
                            f"The bearer token provided via --auth / WRAPP_AUTH for {self._discovery_url} "
                            f"was rejected by the discovery service (HTTP 401/403). "
                            f"The token may be expired, revoked, or invalid."
                        )

            client_creds = self._client_credentials.get(self._discovery_url)
            if client_creds:
                logger.debug(f"StorageAPI discovery: trying client credentials auth for {self._discovery_url}")
                try:
                    await self._run_client_credentials_auth(client_creds)
                    return await self._discover_with_token(self._auth_state.access_token)
                except (_AuthFailed, StorageInitializationError) as e:
                    raise FailedCommand(f"Storage API client credentials authentication failed for {self._discovery_url}: {e}") from e

            if not self._auth_state.refresh_token:
                logger.debug("StorageAPI auth: no in-memory refresh token for %s; checking keyring.", self._discovery_url)
                self._auth_state.refresh_token = self._load_refresh_token_from_keyring(self._discovery_url)

            if self._auth_state.refresh_token:
                logger.debug("StorageAPI auth: refreshing access token for %s using stored refresh token.", self._discovery_url)
                try:
                    await self._refresh_access_token()
                    logger.debug("StorageAPI auth: refresh token exchange succeeded for %s.", self._discovery_url)
                    return await self._discover_with_token(self._auth_state.access_token)
                except _AuthFailed as e:
                    logger.warning(
                        "StorageAPI auth: refresh token exchange failed for %s; deleting stored refresh token and clearing auth state: %s",
                        self._discovery_url,
                        e,
                    )
                    self._auth_state.clear()
                    self._delete_refresh_token_from_keyring(self._discovery_url)
            else:
                logger.warning(
                    "StorageAPI auth: no refresh token available for %s. Long-running interactive sessions cannot renew access tokens "
                    "past expiry unless browser auth returns and stores a refresh token.",
                    self._discovery_url,
                )

            if not self._interactive_fallback:
                raise FailedCommand(
                    f"Storage API discovery at {self._discovery_url} requires authentication. "
                    f"Provide '--auth {self._discovery_url},<token>' or WRAPP_AUTH, "
                    f"or re-run with '--interactive-auth'."
                )

            auth_errors: List[str] = []
            for _ in range(_AUTH_ATTEMPT_LIMIT):
                try:
                    logger.debug("StorageAPI auth: starting interactive browser authentication for %s.", self._discovery_url)
                    await self._run_browser_auth()
                    if self._auth_state.refresh_token:
                        logger.debug(
                            "StorageAPI auth: interactive browser authentication stored a refresh token for %s.", self._discovery_url
                        )
                    else:
                        logger.warning(
                            "StorageAPI auth: interactive browser authentication did not return a refresh token for %s; "
                            "long-running operations may fail when the access token expires.",
                            self._discovery_url,
                        )
                    return await self._discover_with_token(self._auth_state.access_token)
                except (_AuthFailed, _AuthCancelled, StorageInitializationError) as e:
                    logger.warning("StorageAPI auth: interactive browser authentication failed for %s: %s", self._discovery_url, e)
                    auth_errors.append(str(e))
            raise FailedCommand(
                f"Storage API authentication failed for discovery endpoint {self._discovery_url} after {_AUTH_ATTEMPT_LIMIT} attempts. "
                f"Last error: {auth_errors[-1] if auth_errors else 'unknown'}"
            )

    async def _discover_with_token(self, access_token: str) -> str:
        session = await self._ensure_aiohttp_session()
        headers: Dict[str, str] = {}
        if access_token:
            headers["authorization"] = f"Bearer {access_token}"
        url = f"{self._discovery_url}/api/v1/services"
        logger.debug(f"StorageAPI discovery: GET {url} (authenticated={bool(access_token)})")
        try:
            async with session.get(url, headers=headers) as response:
                logger.debug(f"StorageAPI discovery: GET {url} -> {response.status}")
                if response.status in [401, 403]:
                    raise _AuthFailed("Discovery endpoint rejected authentication.")
                if response.status >= 400:
                    body = await response.text()
                    raise StorageInitializationError(f"Storage API discovery request to {url} failed with status {response.status}: {body}")
                payload = await response.json(content_type=None)
                logger.debug("StorageAPI discovery: response payload from %s: %s", url, json.dumps(payload, sort_keys=True))
        except (_AuthFailed, StorageInitializationError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise StorageInitializationError(f"Failed to connect to Storage API discovery service at {url}: {e}") from e

        schema_version = payload.get("schema-version")
        if schema_version != 1:
            raise StorageInitializationError(
                f"Storage API discovery at {self._discovery_url} returned unsupported schema-version {schema_version!r}."
            )
        services = payload.get("services", [])
        if not isinstance(services, list):
            raise StorageInitializationError("Storage API discovery response is missing a valid services list.")

        for service in services:
            if not isinstance(service, dict):
                continue
            if service.get("type") == "storage":
                grpc_endpoint = service.get("grpc")
                if isinstance(grpc_endpoint, str) and grpc_endpoint:
                    return grpc_endpoint
        raise StorageInitializationError(f"Storage API discovery at {self._discovery_url} did not return a storage gRPC endpoint.")

    async def _run_browser_auth(self):
        auth_config = await self._get_auth_config()
        openid_config = await self._get_openid_config(auth_config["openid_configuration"])
        code_verifier = secrets.token_urlsafe(48)
        verifier_hash = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(verifier_hash).decode("utf-8").rstrip("=")
        nonce = secrets.token_urlsafe(12)
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": auth_config["client_id"],
                "redirect_uri": _OPENID_REDIRECT_URL,
                "scope": auth_config["scope"],
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        auth_url = f"{openid_config['authorization_endpoint']}?{query}"
        code = await self._open_browser_and_wait_for_code(auth_url)
        token_payload = await self._exchange_auth_code(
            token_endpoint=openid_config["token_endpoint"],
            client_id=auth_config["client_id"],
            client_secret=auth_config.get("client_secret", ""),
            code=code,
            code_verifier=code_verifier,
        )
        self._set_token_state(token_payload)

    async def _run_client_credentials_auth(self, credentials: StorageAPIClientCredentialsAuthInfo):
        auth_config = await self._get_auth_config()
        openid_config = await self._get_openid_config(auth_config["openid_configuration"])
        token_payload = await self._exchange_client_credentials(
            token_endpoint=openid_config["token_endpoint"],
            client_id=credentials.client_id,
            client_secret=credentials.client_secret,
            scope=auth_config["scope"],
        )
        self._set_token_state(token_payload)

    async def _exchange_client_credentials(self, token_endpoint: str, client_id: str, client_secret: str, scope: str) -> Dict[str, object]:
        session = await self._ensure_aiohttp_session()
        basic_auth = aiohttp.BasicAuth(client_id, client_secret)
        fields = {"grant_type": "client_credentials", "scope": scope}
        try:
            async with session.post(
                token_endpoint,
                data=fields,
                auth=basic_auth,
                headers={"content-type": "application/x-www-form-urlencoded"},
            ) as response:
                if response.status in [400, 401, 403]:
                    body = await response.text()
                    raise _AuthFailed(f"Token endpoint rejected client credentials with status {response.status}: {body}")
                if response.status >= 400:
                    body = await response.text()
                    raise StorageInitializationError(f"Token endpoint {token_endpoint} failed with status {response.status}: {body}")
                payload = await response.json(content_type=None)
        except (_AuthFailed, StorageInitializationError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise StorageInitializationError(f"Client credentials token exchange with {token_endpoint} failed: {e}") from e
        if "access_token" not in payload:
            raise StorageInitializationError("Token response does not include access_token.")
        return payload

    async def _open_browser_and_wait_for_code(self, auth_url: str) -> str:
        auth_code: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        auth_error: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def openid_handler(request: web.Request):
            if "code" in request.query:
                if not auth_code.done():
                    auth_code.set_result(request.query["code"])
                return web.Response(
                    text="You have successfully logged in. You can now return to wrapp.",
                    content_type="text/plain",
                )
            reason = request.query.get("error_description") or request.query.get("error") or "No authorization code was received."
            if not auth_error.done():
                auth_error.set_result(reason)
            return web.Response(status=400, text=reason, content_type="text/plain")

        app = web.Application()
        app.router.add_get(_OPENID_REDIRECT_PATH, openid_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, _OPENID_REDIRECT_HOST, _OPENID_REDIRECT_PORT)
            await site.start()
        except Exception as e:
            await runner.cleanup()
            raise StorageInitializationError(f"Failed to start local authentication callback server on {_OPENID_REDIRECT_URL}: {e}") from e

        opened = await asyncio.to_thread(webbrowser.open, auth_url)
        if not opened:
            logger.warning(f"Failed to open browser automatically. Open this URL manually to continue auth: {auth_url}")

        try:
            done, _ = await asyncio.wait({auth_code, auth_error}, timeout=300, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise _AuthCancelled("Timed out waiting for browser authentication callback.")
            if auth_error in done:
                raise _AuthFailed(auth_error.result())
            return auth_code.result()
        finally:
            await runner.cleanup()

    async def _get_auth_config(self) -> Dict[str, str]:
        session = await self._ensure_aiohttp_session()
        url = f"{self._discovery_url}/api/v1/auth-config"
        logger.debug(f"StorageAPI auth: GET {url}")
        try:
            async with session.get(url) as response:
                logger.debug(f"StorageAPI auth: GET {url} -> {response.status}")
                if response.status >= 400:
                    body = await response.text()
                    raise StorageInitializationError(
                        f"Failed to retrieve Storage API auth config from {url} " f"(status {response.status}): {body}"
                    )
                payload = await response.json(content_type=None)
        except StorageInitializationError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise StorageInitializationError(f"Failed to connect to Storage API auth config endpoint at {url}: {e}") from e
        openid_configuration = payload.get("openid_configuration")
        clients = payload.get("clients", {})
        if not isinstance(openid_configuration, str) or not openid_configuration:
            raise StorageInitializationError("Storage API auth config is missing a valid openid_configuration field.")
        if not isinstance(clients, dict):
            raise StorageInitializationError("Storage API auth config has invalid clients configuration.")
        client_name_candidates = self._auth_client_name_candidates()
        client_name = next((name for name in client_name_candidates if isinstance(clients.get(name), dict)), None)
        client_config = clients.get(client_name) if client_name else None
        if not isinstance(client_config, dict):
            expected_clients = ", ".join([f"clients['{name}']" for name in client_name_candidates])
            raise StorageInitializationError(
                f"Storage API auth config does not define {expected_clients}. "
                f"The discovery server must expose one of the configured OAuth client names."
            )
        client_id = client_config.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise StorageInitializationError(f"Storage API auth config for client '{client_name}' is missing client_id.")
        scope = client_config.get("scope", _DEFAULT_AUTH_SCOPE)
        if not isinstance(scope, str) or not scope:
            scope = _DEFAULT_AUTH_SCOPE
        result = {
            "openid_configuration": openid_configuration,
            "client_id": client_id,
            "scope": scope,
        }
        client_secret = client_config.get("client_secret")
        if isinstance(client_secret, str) and client_secret:
            result["client_secret"] = client_secret
        return result

    @staticmethod
    def _auth_client_name_candidates() -> List[str]:
        candidates: List[str] = []

        for env_name in _AUTH_CLIENT_NAME_ENV_VARS:
            value = os.getenv(env_name, "").strip()
            if value and value not in candidates:
                candidates.append(value)

        for default_name in (_DEFAULT_AUTH_CLIENT_NAME, *_FALLBACK_AUTH_CLIENT_NAMES):
            if default_name not in candidates:
                candidates.append(default_name)

        return candidates

    async def _get_openid_config(self, openid_config_url: str) -> Dict[str, str]:
        session = await self._ensure_aiohttp_session()
        logger.debug(f"StorageAPI auth: GET {openid_config_url}")
        try:
            async with session.get(openid_config_url) as response:
                logger.debug(f"StorageAPI auth: GET {openid_config_url} -> {response.status}")
                if response.status >= 400:
                    body = await response.text()
                    raise StorageInitializationError(
                        f"Failed to retrieve OpenID configuration from {openid_config_url} (status {response.status}): {body}"
                    )
                payload = await response.json(content_type=None)
        except StorageInitializationError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise StorageInitializationError(f"Failed to connect to OpenID configuration endpoint at {openid_config_url}: {e}") from e
        auth_endpoint = payload.get("authorization_endpoint")
        token_endpoint = payload.get("token_endpoint")
        if not isinstance(auth_endpoint, str) or not isinstance(token_endpoint, str):
            raise StorageInitializationError("OpenID configuration is missing authorization_endpoint or token_endpoint.")
        return {
            "authorization_endpoint": auth_endpoint,
            "token_endpoint": token_endpoint,
        }

    async def _exchange_auth_code(
        self, token_endpoint: str, client_id: str, client_secret: str, code: str, code_verifier: str
    ) -> Dict[str, object]:
        auth = aiohttp.BasicAuth(client_id, client_secret) if client_secret else None
        return await self._exchange_token(
            token_endpoint=token_endpoint,
            fields={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": _OPENID_REDIRECT_URL,
                "client_id": client_id,
            },
            auth=auth,
        )

    async def _refresh_access_token(self):
        if not self._auth_state.refresh_token:
            raise _AuthFailed("No refresh token is available.")
        auth_config = await self._get_auth_config()
        openid_config = await self._get_openid_config(auth_config["openid_configuration"])
        client_secret = auth_config.get("client_secret", "")
        auth = aiohttp.BasicAuth(auth_config["client_id"], client_secret) if client_secret else None
        token_payload = await self._exchange_token(
            token_endpoint=openid_config["token_endpoint"],
            fields={
                "grant_type": "refresh_token",
                "client_id": auth_config["client_id"],
                "refresh_token": self._auth_state.refresh_token,
            },
            auth=auth,
        )
        self._set_token_state(token_payload)

    async def _exchange_token(
        self, token_endpoint: str, fields: Dict[str, str], auth: Optional[aiohttp.BasicAuth] = None
    ) -> Dict[str, object]:
        session = await self._ensure_aiohttp_session()
        grant_type = fields.get("grant_type", "unknown")
        logger.debug(f"StorageAPI auth: POST {token_endpoint} (grant_type={grant_type})")
        try:
            async with session.post(
                token_endpoint, data=fields, auth=auth, headers={"content-type": "application/x-www-form-urlencoded"}
            ) as response:
                logger.debug(f"StorageAPI auth: POST {token_endpoint} -> {response.status}")
                if response.status in [400, 401, 403]:
                    body = await response.text()
                    raise _AuthFailed(f"Token endpoint rejected authentication with status {response.status}: {body}")
                if response.status >= 400:
                    body = await response.text()
                    raise StorageInitializationError(f"Token endpoint {token_endpoint} failed with status {response.status}: {body}")
                payload = await response.json(content_type=None)
        except (_AuthFailed, StorageInitializationError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise StorageInitializationError(f"Token exchange with {token_endpoint} failed: {e}") from e
        if "access_token" not in payload:
            raise StorageInitializationError("Token response does not include access_token.")
        return payload

    def _set_token_state(self, token_payload: Dict[str, object]):
        access_token = token_payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise StorageInitializationError("Token response access_token is invalid.")
        refresh_token = token_payload.get("refresh_token")
        expires_in_value = token_payload.get("expires_in", 3600)
        expires_in_seconds = 3600
        if isinstance(expires_in_value, int):
            expires_in_seconds = expires_in_value
        elif isinstance(expires_in_value, float):
            expires_in_seconds = int(expires_in_value)
        elif isinstance(expires_in_value, str):
            try:
                expires_in_seconds = int(expires_in_value)
            except ValueError:
                expires_in_seconds = 3600

        self._auth_state.access_token = access_token
        if isinstance(refresh_token, str) and refresh_token:
            self._auth_state.refresh_token = refresh_token
            self._store_refresh_token_to_keyring(self._discovery_url, refresh_token)
        self._auth_state.expires_at = asyncio.get_running_loop().time() + max(expires_in_seconds, 1)

    async def _try_refresh_auth(self) -> bool:
        """Attempt to refresh credentials reactively after an UNAUTHENTICATED gRPC error.

        Returns True if a new access token was obtained and the caller should retry.
        """
        async with self._auth_lock:
            logger.debug("StorageAPI auth: received UNAUTHENTICATED from gRPC; attempting credential refresh.")
            return await self._refresh_auth_without_lock()

    async def _refresh_auth_without_lock(self) -> bool:
        if self._auth_state.refresh_token:
            try:
                logger.debug("StorageAPI auth: refreshing access token for %s using refresh token.", self._discovery_url)
                await self._refresh_access_token()
                logger.debug("StorageAPI auth: access token refresh succeeded for %s.", self._discovery_url)
                return True
            except _AuthFailed as e:
                logger.warning(
                    "StorageAPI auth: access token refresh failed for %s; deleting stored refresh token and clearing auth state: %s",
                    self._discovery_url,
                    e,
                )
                self._auth_state.clear()
                self._delete_refresh_token_from_keyring(self._discovery_url)
                return False

        client_creds = self._client_credentials.get(self._discovery_url)
        if client_creds:
            try:
                logger.debug("StorageAPI auth: refreshing access token for %s using client credentials.", self._discovery_url)
                await self._run_client_credentials_auth(client_creds)
                logger.debug("StorageAPI auth: client credentials refresh succeeded for %s.", self._discovery_url)
                return True
            except (_AuthFailed, StorageInitializationError) as e:
                logger.warning("StorageAPI auth: client credentials refresh failed for %s: %s", self._discovery_url, e)
                return False

        logger.warning(
            "StorageAPI auth: no refresh token or client credentials available for %s; cannot renew expired access token.",
            self._discovery_url,
        )
        return False

    def _keyring_service_name(self) -> str:
        configured = os.getenv(_KEYRING_SERVICE_NAME_ENV_VAR, "").strip()
        return configured or _DEFAULT_KEYRING_SERVICE_NAME

    @classmethod
    def _keyring_username_for(cls, discovery_url: str) -> str:
        return cls._normalize_discovery_url(discovery_url)

    def _load_refresh_token_from_keyring(self, discovery_url: str) -> str:
        service = self._keyring_service_name()
        username = self._keyring_username_for(discovery_url)
        try:
            stored_value = keyring.get_password(service, username)
        except Exception as e:
            logger.warning(f"Failed to read Storage API auth token from keyring for {discovery_url}: {e}")
            return ""
        found = bool(stored_value and stored_value.strip())
        if found:
            logger.debug("StorageAPI auth: found refresh token in keyring for %s.", discovery_url)
        else:
            logger.debug("StorageAPI auth: no refresh token found in keyring for %s.", discovery_url)
        return stored_value.strip() if isinstance(stored_value, str) else ""

    def _store_refresh_token_to_keyring(self, discovery_url: str, refresh_token: str):
        service = self._keyring_service_name()
        username = self._keyring_username_for(discovery_url)
        try:
            keyring.set_password(service, username, refresh_token)
        except Exception as e:
            logger.warning(f"Failed to store Storage API auth token in keyring for {discovery_url}: {e}")

    def _delete_refresh_token_from_keyring(self, discovery_url: str):
        service = self._keyring_service_name()
        username = self._keyring_username_for(discovery_url)
        try:
            existing = keyring.get_password(service, username)
        except Exception as e:
            logger.warning(f"Failed to read Storage API auth token from keyring for {discovery_url}: {e}")
            return
        if not existing:
            return
        try:
            keyring.delete_password(service, username)
        except Exception as e:
            logger.warning(f"Failed to delete Storage API auth token from keyring for {discovery_url}: {e}")

    async def _initialize_capabilities(self):
        """Initialize and check capability compatibility.

        After _negotiate_versions() has already selected the best version per
        service, this method performs a secondary validation to ensure that the
        server also advertises the supplementary test-write capability.
        """
        await self._ensure_grpc_stubs()
        try:
            capabilities = await self._run_grpc(
                self._throttler_roundtrip,
                self._capability_api.ListServices,
                self._capabilities_msgs.ListServicesRequest(),
            )
        except Exception as e:
            if "WRAPP_STORAGE_API_GRPC_ADDRESS" in os.environ or self._discovery_url:
                raise StorageInitializationError(
                    f"Storage service failed to retrieve service capabilities from end point {self._endpoint}: {e}"
                )
            else:
                raise StorageInitializationError(f"Storage service failed to retrieve service capabilities.", result=Result.OK)

        logger.debug(
            "StorageAPI capabilities validation ListServices response from %s: %s",
            self._endpoint,
            json.dumps(self._list_services_response_for_log(capabilities), sort_keys=True),
        )

        required_capabilities = [
            ("fileobject", self._fileobject_version),
            ("fileobject", "v1testwrite"),
            ("filefolder", self._filefolder_version),
        ]
        for service_name, version in required_capabilities:
            if not any(svc.service_name == service_name and version in svc.service_versions for svc in capabilities.services):
                raise StorageInitializationError(
                    f"Storage service does not support the {service_name} API in version {version}, won't connect"
                )

    async def get_top_level_addresses(self) -> list[str]:
        """Return the address spaces served by this Storage API endpoint."""
        await self._ensure_grpc_stubs()
        if self._top_level_addresses is None:
            # Routes-first initialization can skip TLA fetching, but some callers
            # still explicitly query TLAs (for diagnostics/tests/root traversal).
            await self._fetch_top_level_addresses()
        return list(self._top_level_addresses) if self._top_level_addresses else []

    def has_known_scope(self) -> bool:
        """True iff routes or top-level addresses have been populated.

        When this returns True, :meth:`is_url_in_scope` reflects an authoritative
        answer from the server (or a configured override) and the Router can
        trust it for any URL scheme, including ``omniverse://`` and ``file://``.
        When False, the backend has not yet connected and ``is_url_in_scope`` is
        permissive to allow lazy initialization to proceed.
        """
        return self._route_regexes is not None or self._top_level_addresses is not None

    def is_url_in_scope(self, url: str) -> bool:
        """Check whether *url* falls within a known route or top-level address (sync).

        Prefers route-based wildcard matching when ListRoutes data is available;
        otherwise falls back to top-level address prefix matching.

        When the backend has not yet connected (``_channel is None``), returns
        ``True`` so that ``_ensure_grpc_stubs()`` can lazily initialise on the
        first real operation (e.g. after auth is configured via ``--auth``).
        Once connected, returns ``False`` if TLAs were not populated so that
        StorageAPI does not incorrectly claim URLs meant for other backends.

        If neither an explicit gRPC address nor a discovery URL is configured
        the backend has nothing to connect to and refuses to claim any URL, so
        the Router can fall through to the dedicated backends.
        """
        if self._route_regexes is not None:
            return any(self._url_matches_route(rx, url) for rx in self._route_regexes)
        if self._top_level_addresses is None:
            # Not yet connected — be permissive so _ensure_grpc_stubs()
            # can lazily populate TLAs on the first real operation
            # (e.g. when auth is configured after startup), but only if
            # we actually have an endpoint to connect to.
            if not self._endpoint and not self._discovery_url:
                return False
            # Once connected (_channel is set), be conservative.
            return self._channel is None
        u = url.rstrip("/")
        return any(u == tla or u.startswith(tla + "/") for tla in self._top_level_addresses)

    def backend_throttlers(self) -> list[TaskThrottler]:
        return [self._throttler_roundtrip, self._throttler_networkdata, self._throttler_serverdata]

    def reconfigure_throttlers(self, throttling_params: ThrottlingParameters) -> bool:
        if (
            self._throttler_roundtrip.max_parallel_tasks == throttling_params.jobs
            and self._throttler_networkdata.max_parallel_tasks == throttling_params.file_transfer_jobs
        ):
            return True
        for t in self.backend_throttlers():
            if t.pending_tasks or t.processing_tasks:
                logger.warning(f"Cannot update throttling parameters for Storage API: Throttler {t.server_backend_name} has open tasks.")
                return False
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "StorageAPI", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "StorageAPI", throttling_params.file_transfer_jobs)
        self._throttler_serverdata = TaskThrottler(ThrottlerType.LongRunningServerOp, "StorageAPI", throttling_params.jobs)
        return True

    async def _run_lazy(
        self,
        throttler: TaskThrottler,
        grpc_method: Callable[..., Awaitable[_TaskReturnType]],
        *args: object,
        add_auth_metadata: bool = False,
        **kwargs: object,
    ) -> _TaskReturnType:
        """
        Helper to defer unary gRPC call initiation until after throttler semaphore acquisition.

        Problem: gRPC-aio stub methods for unary calls may initiate RPC calls when the stub
        method is called (creating the coroutine), not when the coroutine is awaited. Passing
        such a coroutine directly to throttler.run() means the RPC starts before the semaphore
        is acquired, defeating the throttler's purpose.

        Solution: Wrap the gRPC method call in a lambda/inner async function. This defers the
        creation of the gRPC coroutine until after the throttler acquires its semaphore and
        executes the wrapper function.

        Example issue:
            # BAD: RPC may start immediately
            await throttler.run(stub.UnaryMethod(request))

            # GOOD: RPC starts only after semaphore acquired
            await self._run_lazy(throttler, stub.UnaryMethod, request)
        """

        async def _deferred_call():
            if add_auth_metadata and "metadata" not in kwargs:
                metadata = await self._grpc_metadata()
                if metadata:
                    return await grpc_method(*args, metadata=metadata, **kwargs)
            return await grpc_method(*args, **kwargs)

        return await throttler.run(_deferred_call())

    async def _run_grpc(
        self,
        throttler: TaskThrottler,
        grpc_method: Callable[..., Awaitable[_TaskReturnType]],
        *args: object,
        **kwargs: object,
    ) -> _TaskReturnType:
        try:
            return await self._run_lazy(throttler, grpc_method, *args, add_auth_metadata=True, **kwargs)
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNAUTHENTICATED or not await self._try_refresh_auth():
                raise
            return await self._run_lazy(throttler, grpc_method, *args, add_auth_metadata=True, **kwargs)

    async def _iterate_lazy(
        self,
        throttler: TaskThrottler,
        grpc_method: Callable[..., AsyncGenerator[_CallableReturnType, None]],
        *args: object,
        add_auth_metadata: bool = False,
        **kwargs: object,
    ) -> AsyncGenerator[_CallableReturnType, None]:
        """
        Wrapper for streaming gRPC calls to defer stream initiation until after throttler semaphore acquisition.

        Problem: Similar to unary calls, gRPC streaming calls are initiated when the stub method is
        called (creating the async generator), not when __anext__() is awaited. Passing such a generator
        directly to throttler.iterate() means the RPC stream starts before the semaphore is acquired.

        Solution: Wrap the gRPC method call in an inner async generator. This defers the creation of
        the gRPC stream until the first __anext__() call on the wrapper generator, which happens inside
        throttler.iterate() after acquiring the semaphore.

        Note that the server might run ahead during the operation and not wait for further __anext__()
        calls. That is fine - we just don't want the client to send thousands of gRPC messages concurrently.

        Example issue:
            # BAD: Stream may start immediately
            throttler.iterate(stub.StreamingMethod(request))

            # GOOD: Stream starts only after first semaphore acquisition
            self._iterate_lazy(throttler, stub.StreamingMethod, request)

        Reference for the gRPC aio implementation:
        https://github.com/grpc/grpc/blob/c4b87395ceb1a95aaf604f2d749af383a9d9e55f/src/python/grpcio/grpc/aio/_call.py#L366
        """

        async def _deferred_ainit():
            # This actually sends the first request to the server, but does not wait for the answer
            if add_auth_metadata and "metadata" not in kwargs:
                metadata = await self._grpc_metadata()
                if metadata:
                    stream = grpc_method(*args, metadata=metadata, **kwargs)
                else:
                    stream = grpc_method(*args, **kwargs)
            else:
                stream = grpc_method(*args, **kwargs)
            # This does only add syntactic sugar
            gen = stream.__aiter__()
            # Wait for the answer to the first request.
            r = await gen.__anext__()
            return gen, r

        # Wrap each yield with semaphore acquisition
        try:
            gen, first_item = await self._run_lazy(throttler, _deferred_ainit)
            yield first_item
            while True:
                # Acquire semaphore for each server operation
                item = await self._run_lazy(throttler, gen.__anext__)
                # Yield it to caller
                yield item
        except StopAsyncIteration:
            # Generator exhausted
            pass

    async def _iterate_grpc(
        self,
        throttler: TaskThrottler,
        grpc_method: Callable[..., AsyncGenerator[_CallableReturnType, None]],
        *args: object,
        **kwargs: object,
    ) -> AsyncGenerator[_CallableReturnType, None]:
        try:
            async for item in self._iterate_lazy(throttler, grpc_method, *args, add_auth_metadata=True, **kwargs):
                yield item
            return
        except grpc.RpcError as e:
            if e.code() != grpc.StatusCode.UNAUTHENTICATED or not await self._try_refresh_auth():
                raise
        async for item in self._iterate_lazy(throttler, grpc_method, *args, add_auth_metadata=True, **kwargs):
            yield item

    async def close(self):
        """Close the gRPC channel and aiohttp session."""
        if self._aiohttp_session and not self._aiohttp_session.closed:
            await self._aiohttp_session.close()
        if self._channel is not None:
            await self._channel.close()
            self._channel = None

    async def _run_metadata_grpc_with_retries(
        self,
        op_label: str,
        grpc_method: Callable[..., Awaitable[Any]],
        request: Any,
    ) -> Any:
        """Run a Metadata-service gRPC call with retries.

        Mirrors ``write_file_content``'s retry policy
        (``_RETRYABLE_GRPC_STATUS_CODES`` with 1s/2s/4s exponential
        backoff) so transient ``UNAVAILABLE`` / upstream-timeout responses
        don't leave the data write looking healthy while the metadata
        side-channel silently drops on the first try. Number of attempts is
        tunable via ``WRAPP_STORAGE_API_METADATA_RETRIES`` (default 3).

        Returns the gRPC response on success. Raises ``grpc.RpcError`` if all
        attempts are exhausted or a non-retryable code is received; callers
        decide whether to swallow or surface that.
        """
        max_attempts = self._metadata_max_attempts
        base_delay = 1.0
        method_name = getattr(grpc_method, "_method", None) or getattr(grpc_method, "__name__", "metadata")
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._run_grpc(self._throttler_roundtrip, grpc_method, request)
            except grpc.RpcError as e:
                if attempt == max_attempts or not self._is_retryable_grpc_error(e):
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                # Per-attempt retries are routine on a transient blip; only
                # the final-give-up path stays a WARNING. Use info so a
                # successful retry doesn't look like an error.
                logger.info(
                    "[%s] metadata %s attempt %d/%d failed: %s. Retrying in %.1fs...",
                    op_label,
                    method_name,
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)
        raise AssertionError("metadata retry loop exited without returning or raising")

    async def _run_read_grpc_with_retries(
        self,
        op_label: str,
        grpc_method: Callable[..., Awaitable[Any]],
        request: Any,
    ) -> Any:
        """Run a read-side gRPC call with retries for transient proxy/backend timeouts."""
        max_attempts = self._read_max_attempts
        base_delay = 1.0
        method_name = getattr(grpc_method, "_method", None) or getattr(grpc_method, "__name__", "read")
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._run_grpc(self._throttler_roundtrip, grpc_method, request)
            except grpc.RpcError as e:
                if attempt == max_attempts or not self._is_retryable_grpc_error(e):
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(
                    "[%s] read %s attempt %d/%d failed: %s. Retrying in %.1fs...",
                    op_label,
                    method_name,
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)
        raise AssertionError("read retry loop exited without returning or raising")

    async def _run_control_grpc_with_retries(
        self,
        op_label: str,
        grpc_method: Callable[..., Awaitable[Any]],
        request: Any,
    ) -> Any:
        """Run a read-only control-plane gRPC call with retries."""
        max_attempts = self._read_max_attempts
        base_delay = 1.0
        method_name = getattr(grpc_method, "_method", None) or getattr(grpc_method, "__name__", "control")
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._run_grpc(self._throttler_roundtrip, grpc_method, request)
            except grpc.RpcError as e:
                if attempt == max_attempts or not self._is_retryable_grpc_error(e):
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(
                    "[%s] control %s attempt %d/%d failed: %s. Retrying in %.1fs...",
                    op_label,
                    method_name,
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)
        raise AssertionError("control retry loop exited without returning or raising")

    async def _run_read_operation_with_retries(
        self,
        op_label: str,
        operation: Callable[[], Awaitable[_TaskReturnType]],
        *,
        on_retry: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> _TaskReturnType:
        """Run a read-side operation with retries for retryable gRPC stream failures."""
        max_attempts = self._read_max_attempts
        base_delay = 1.0
        for attempt in range(1, max_attempts + 1):
            try:
                return await operation()
            except grpc.RpcError as e:
                if attempt == max_attempts or not self._is_retryable_grpc_error(e):
                    raise
                if on_retry is not None:
                    await on_retry()
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(
                    "[%s] read stream attempt %d/%d failed: %s. Retrying in %.1fs...",
                    op_label,
                    attempt,
                    max_attempts,
                    e,
                    delay,
                )
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)
        raise AssertionError("read operation retry loop exited without returning or raising")

    async def _sleep_before_grpc_stream_retry(
        self,
        op_label: str,
        method_name: str,
        attempt: int,
        max_attempts: int,
        error: grpc.RpcError,
    ) -> bool:
        """Sleep before retrying a restartable gRPC stream.

        List-like Storage API streams do not expose resume tokens, so callers
        restart from the beginning and dedupe entries they already yielded.
        """
        if attempt == max_attempts or not self._is_retryable_grpc_error(error):
            return False
        delay = 1.0 * (2 ** (attempt - 1))
        logger.info(
            "[%s] %s stream attempt %d/%d failed: %s. Retrying in %.1fs...",
            op_label,
            method_name,
            attempt,
            max_attempts,
            error,
            delay,
        )
        Stats.get().increment(StatType.GRPC_RETRY)
        await asyncio.sleep(delay)
        return True

    async def _get_nucleus_hash(self, resource_address: str) -> Optional[str]:
        """Retrieve the stored nucleus hash for a resource via the Metadata API.

        Returns None when the Metadata service is unavailable, the key does not
        exist, or the call fails for any other reason.
        """
        if not self._metadata_api or not self._metadata_msgs:
            return None
        Stats.get().increment(StatType.GET_METADATA_ASYNC)
        try:
            response = await self._run_metadata_grpc_with_retries(
                resource_address,
                self._metadata_api.GetMetadata,
                self._metadata_msgs.GetMetadataRequest(
                    uri=_encode_resource_address_for_storage_service(resource_address),
                    user_metadata_keys=[NUCLEUS_HASH_METADATA_KEY],
                ),
            )
            entry = response.user_metadata.get(NUCLEUS_HASH_METADATA_KEY)
            if entry and entry.value.HasField("string_value"):
                return entry.value.string_value
            return None
        except grpc.RpcError as e:
            if e.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNIMPLEMENTED):
                return None
            logger.debug("Failed to retrieve nucleus hash metadata for %s: %s", resource_address, e)
            return None

    async def _set_nucleus_hash(self, resource_address: str, nucleus_hash: str) -> None:
        """Store the nucleus hash for a resource via the Metadata API (best-effort)."""
        if not self._metadata_api or not self._metadata_msgs:
            return
        Stats.get().increment(StatType.SET_METADATA_ASYNC)
        try:
            await self._run_metadata_grpc_with_retries(
                resource_address,
                self._metadata_api.UpdateMetadata,
                self._metadata_msgs.UpdateMetadataRequest(
                    uri=_encode_resource_address_for_storage_service(resource_address),
                    user_metadata_key=NUCLEUS_HASH_METADATA_KEY,
                    user_metadata=Value(string_value=nucleus_hash),
                ),
            )
        except grpc.RpcError as e:
            logger.warning("Failed to store nucleus hash metadata for %s: %s", resource_address, e)

    async def _delete_nucleus_hash(self, resource_address: str) -> None:
        """Remove any stored nucleus hash for a resource via the Metadata API (best-effort).

        Used when an upload cannot provide a trustworthy hash: on overwrite the
        destination may still carry the previous object's hash, which must not
        be left describing the new content.

        Benign race: a retried/delayed delete can remove a hash that a newer
        concurrent upload to the same address just wrote. That leaves the hash
        missing — re-derivable, never incorrect — which matches this method's
        semantics.
        """
        if not self._metadata_api or not self._metadata_msgs:
            return
        Stats.get().increment(StatType.DELETE_METADATA_ASYNC)
        try:
            await self._run_metadata_grpc_with_retries(
                resource_address,
                self._metadata_api.DeleteMetadata,
                self._metadata_msgs.DeleteMetadataRequest(
                    uri=_encode_resource_address_for_storage_service(resource_address),
                    user_metadata_key=NUCLEUS_HASH_METADATA_KEY,
                ),
            )
        except grpc.RpcError as e:
            if e.code() in (grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNIMPLEMENTED):
                return
            logger.warning("Failed to delete nucleus hash metadata for %s: %s", resource_address, e)

    @property
    def has_metadata_support(self) -> bool:
        """Whether the connected server exposes the Metadata service."""
        return self._metadata_api is not None

    def supports_list_file_tree(self) -> bool:
        return self._use_enumerate

    def _map_upload_method(self, upload_method) -> str:
        assert self._fileobject_msgs is not None
        pref = self._fileobject_msgs
        modes = {
            pref.UPLOAD_PREFERENCE_BODY: "body",
            pref.UPLOAD_PREFERENCE_REDIRECT: "redirect",
            pref.UPLOAD_PREFERENCE_MULTIPART: "multipart",
        }
        if upload_method not in modes:
            raise Exception("Invalid upload prefrence")
        return modes[upload_method]

    async def get_write_type_info(self, url: str):
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(url)
        self.write_type_info = await self._run_grpc(
            self._throttler_roundtrip,
            self._fileobject_api.FetchWriteTypeInfo,
            self._fileobject_msgs.FetchWriteTypeInfoRequest(
                destination_resource_address=_encode_resource_address_for_storage_service(resource_address)
            ),
        )
        result = []
        for interval in self.write_type_info.write_type_intervals:
            result.append(
                {
                    "min": interval.minimum_data_object_size,
                    "max": interval.maximum_data_object_size,
                    "mode": self._map_upload_method(interval.preferred_upload_method),
                }
            )
        return result

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, storage_interface.ResourceIdentity]:
        if not isinstance(item, StorageAPIListEntry):
            raise TypeError("Can only generate ResourceIdentity for StorageAPI list entries")
        if item._resource_identity:
            return file_url, relative_url, StorageAPIResourceIdentity(item._resource_identity)
        # AddressInfo protos from Enumerate do not carry identities. Most
        # list_file_tree callers receive entries already classified and
        # identity-patched, but keep this fallback for older direct callers
        # and tests that construct StorageAPIListEntry objects manually.
        Stats.get().increment(StatType.STAT)
        result, stat_item = await self.stat(file_url)
        if result != Result.OK or not isinstance(stat_item, StorageAPIListEntry) or not stat_item._resource_identity:
            raise TypeError(
                f"Cannot generate ResourceIdentity for {file_url}: stat returned result={result}, "
                f"resource_identity={getattr(stat_item, '_resource_identity', None)!r}"
            )
        item._resource_identity = stat_item._resource_identity
        if stat_item.nucleus_hash and not item.nucleus_hash:
            item.nucleus_hash = stat_item.nucleus_hash
        return file_url, relative_url, StorageAPIResourceIdentity(stat_item._resource_identity)

    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(url)
        try:
            stat_coro = self._run_read_grpc_with_retries(
                resource_address,
                self._fileobject_api.Stat,
                self._fileobject_msgs.StatRequest(resource_address=_encode_resource_address_for_storage_service(resource_address)),
            )
            hash_coro = self._get_nucleus_hash(resource_address)

            response, nucleus_hash = await asyncio.gather(stat_coro, hash_coro)

            entry = StorageAPIListEntry.from_resource_info(url, response.resource_info)
            if nucleus_hash:
                entry.nucleus_hash = nucleus_hash
            return Result.OK, entry
        except grpc.RpcError as e:
            # Stat may legitimately fail for directories (the server only
            # stats data-objects).  Always try a list() fallback before
            # giving up — a successful list proves the URL is a folder.
            try:
                _, is_real_folder = await self.is_folder(resource_address)
                if is_real_folder:
                    return Result.OK, StorageAPIListEntry(url, None, None, None, is_folder=True)
            except StorageOperationError:
                pass  # list also failed — fall through to the error handling below

            if e.code() in [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.INTERNAL, grpc.StatusCode.INVALID_ARGUMENT]:
                # NOT_FOUND / INTERNAL: resource does not exist.
                # INVALID_ARGUMENT: URL is outside the server's address space,
                # which is semantically "not found on this server".
                return Result.ERROR_NOT_FOUND, None
            else:
                logger.error(f"Failure to stat {url}: {str(e)}")
                return Result.ERROR, None

    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        await self._ensure_grpc_stubs()
        if not self._filefolder_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(url)
        # TODO: For now, list_directory to determine if it is a folder
        try:
            resource_address = resource_address.lstrip("/")
            async for _ in self._iterate_grpc(
                self._throttler_roundtrip,
                self._filefolder_api.List,
                self._filefolder_msgs.ListRequest(
                    folder=self._filefolder_msgs.FolderAddress(uri=_encode_resource_address_for_storage_service(resource_address))
                ),
            ):
                break
            return Result.OK, True
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return Result.OK, False
            else:
                raise StorageOperationError(f"Failure to check for folder by calling list on {url}: {str(e)}")

    async def _list_entry_from_enumerate_address(
        self,
        resource_address: str,
        relative_resource_address: str,
        metadata: Optional[Any],
    ) -> Optional[StorageAPIListEntry]:
        normalized_relative = relative_resource_address.rstrip("/\\")
        if not normalized_relative:
            return None
        if relative_resource_address.endswith(("/", "\\")):
            return StorageAPIListEntry(normalized_relative, None, None, None, is_folder=True)

        result, stat_item = await self.stat(resource_address)
        if result != Result.OK or not isinstance(stat_item, StorageAPIListEntry):
            raise StorageOperationError(
                f"Cannot classify Enumerate entry {resource_address}: stat returned result={result}, item={stat_item!r}"
            )
        if stat_item.is_folder:
            return StorageAPIListEntry(normalized_relative, None, None, None, is_folder=True)
        if not stat_item._resource_identity:
            raise StorageOperationError(f"Cannot classify Enumerate file entry {resource_address}: stat returned no resource identity")

        size = stat_item.size
        metadata_size = self._metadata_data_object_size(metadata)
        if size is None and metadata_size is not None:
            size = metadata_size
        modification_time = stat_item.modified_time
        metadata_modification_time = self._metadata_last_modified_time(metadata)
        if modification_time is None and metadata_modification_time is not None:
            modification_time = metadata_modification_time
        entry = StorageAPIListEntry(
            resource_address=normalized_relative,
            resource_identity=stat_item._resource_identity,
            size=size,
            modification_time=modification_time,
            is_folder=False,
        )
        if stat_item.nucleus_hash:
            entry.nucleus_hash = stat_item.nucleus_hash
        return entry

    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(url)
        # Anchor the prefix with a trailing slash. The server treats
        # EnumerateRequest.resource_address as a string prefix, so a request
        # for ".../foo" would also pull in sibling ".../foo-2/..." entries.
        # Always end the prefix in '/' to scope to the intended subtree.
        prefix = resource_address.rstrip("/") + "/"
        encoded_prefix = _encode_resource_address_for_storage_service(prefix)
        max_attempts = self._read_max_attempts
        seen_entries: set[tuple[str, str]] = set()
        yielded_folder_paths: set[str] = set()
        for attempt in range(1, max_attempts + 1):
            try:
                enumerate_response = self._iterate_grpc(
                    self._throttler_serverdata,
                    self._fileobject_api.Enumerate,
                    self._fileobject_msgs.EnumerateRequest(resource_address=encoded_prefix),
                )

                async for results in enumerate_response:
                    for entry in results.items:
                        raw_entry_resource_address = entry.resource_address
                        entry_resource_address = _decode_resource_address_from_storage_service(raw_entry_resource_address)
                        if not urllib.parse.urlsplit(entry_resource_address).scheme:
                            relative_entry_address = entry_resource_address.lstrip("/\\")
                            entry_resource_address = f"{prefix.rstrip('/')}/{relative_entry_address}"
                            relative_resource_address = relative_entry_address
                        else:
                            relative_resource_address = StorageAPIListEntry.relative_resource_address(prefix, raw_entry_resource_address)
                        normalized_relative = relative_resource_address.rstrip("/\\")
                        if not normalized_relative:
                            continue
                        entry_item = await self._list_entry_from_enumerate_address(
                            entry_resource_address,
                            relative_resource_address,
                            getattr(entry, "metadata", None),
                        )
                        if entry_item is None:
                            continue

                        path_parts = normalized_relative.split("/")
                        for parent_index in range(1, len(path_parts)):
                            folder_path = "/".join(path_parts[:parent_index])
                            if folder_path in yielded_folder_paths:
                                continue
                            yielded_folder_paths.add(folder_path)
                            yield StorageAPIListEntry(folder_path, None, None, None, is_folder=True)

                        entry_kind = "folder" if entry_item.is_folder else "file"
                        entry_key = (entry_kind, entry_item.relative_path)
                        if entry_key in seen_entries:
                            continue
                        if entry_item.is_folder:
                            if entry_item.relative_path in yielded_folder_paths:
                                continue
                            yielded_folder_paths.add(entry_item.relative_path)
                        seen_entries.add(entry_key)
                        yield entry_item
                return
            except grpc.RpcError as e:
                if await self._sleep_before_grpc_stream_retry(prefix, "list_file_tree", attempt, max_attempts, e):
                    continue
                if e.code() == grpc.StatusCode.NOT_FOUND:
                    # The Enumerate RPC reports a missing prefix (e.g. a package with no .metadata folder) as NOT_FOUND.
                    # Surface it as ERROR_NOT_FOUND so callers treat an absent directory as an empty listing, making the
                    # net behavior uniform across backends: object stores (S3/Azure/GCS) already yield an empty listing
                    # for a missing prefix, and the local backend raises ERROR_NOT_FOUND the same way.
                    raise StorageOperationError(f"Error calling list_file_tree: {str(e)}", Result.ERROR_NOT_FOUND) from e
                raise StorageOperationError(f"Error calling list_file_tree: {str(e)}") from e
        raise AssertionError("list_file_tree retry loop exited without returning or raising")

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        await self._ensure_grpc_stubs()
        if not self._filefolder_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(url)
        resource_address = resource_address.lstrip("/")
        logger.info(f"Listing {resource_address}")
        request = self._filefolder_msgs.ListStatRequest(
            folder=self._filefolder_msgs.FolderAddress(uri=_encode_resource_address_for_storage_service(resource_address.rstrip("/")))
        )
        seen_entries: set[tuple[str, str]] = set()
        max_attempts = self._read_max_attempts
        for attempt in range(1, max_attempts + 1):
            try:
                async for results in self._iterate_grpc(
                    self._throttler_roundtrip,
                    self._filefolder_api.ListStat,
                    request,
                ):
                    partial_result = results
                    # First the subfolders
                    for entry in partial_result.subfolder_addresses:
                        relative_folder_address = StorageAPIListEntry.relative_resource_address(resource_address, entry.uri)
                        if relative_folder_address:
                            key = ("folder", relative_folder_address)
                            if key in seen_entries:
                                continue
                            seen_entries.add(key)
                            yield StorageAPIListEntry(relative_folder_address, None, None, None, is_folder=True)
                    # Then the files
                    for file_entry in partial_result.entries:
                        list_entry = StorageAPIListEntry.from_list_item(resource_address, file_entry)
                        key = ("file", list_entry.relative_path)
                        if key in seen_entries:
                            continue
                        seen_entries.add(key)
                        yield list_entry
                return
            except grpc.RpcError as e:
                if await self._sleep_before_grpc_stream_retry(resource_address, "list_directory", attempt, max_attempts, e):
                    continue
                raise StorageOperationError(f"Error calling list_directory: {str(e)}") from e
        raise AssertionError("list_directory retry loop exited without returning or raising")

    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        raise NotImplementedError

    async def copy(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        src_resource_address = await self._sanitize_address_for_storage_service(src_url)
        dst_resource_address = await self._sanitize_address_for_storage_service(dst_url)

        if self._fileobject_version == "v1alpha":
            try:
                return await self._server_side_copy(src_resource_address, dst_resource_address)
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.UNIMPLEMENTED:
                    logger.info("Server-side Copy RPC not available, falling back to client-side copy")
                else:
                    raise StorageOperationError(f"Error calling server-side copy: {str(e)}") from e

        raise NotImplementedError("StorageAPI backend has no client-side copy; caller should use download+upload via temp file")

    async def _server_side_copy(self, src_resource_address: str, dst_resource_address: str) -> Result:
        """Use the v1alpha-only Copy RPC for server-side copy."""
        assert self._fileobject_api is not None
        assert self._fileobject_msgs is not None
        source_stat = await self._run_grpc(
            self._throttler_roundtrip,
            self._fileobject_api.Stat,
            self._fileobject_msgs.StatRequest(resource_address=_encode_resource_address_for_storage_service(src_resource_address)),
        )
        await self._run_grpc(
            self._throttler_serverdata,
            self._fileobject_api.Copy,
            self._fileobject_msgs.CopyRequest(
                source_resource_identity=source_stat.resource_info.resource_identity,
                destination_resource_address=_encode_resource_address_for_storage_service(dst_resource_address),
            ),
        )
        # Best-effort hash propagation: if the source is overwritten between
        # the Copy RPC and this read, the hash may be stale.  Acceptable
        # because metadata is advisory and the next write will correct it.
        src_hash = await self._get_nucleus_hash(src_resource_address)
        if src_hash:
            await self._set_nucleus_hash(dst_resource_address, src_hash)
        return Result.OK

    @staticmethod
    def _metadata_data_object_size(metadata) -> Optional[int]:
        if metadata is None:
            return None
        has_field = getattr(metadata, "HasField", None)
        if callable(has_field):
            try:
                if not has_field("data_object_size"):
                    return None
            except ValueError:
                return None
        return getattr(metadata, "data_object_size", None)

    @staticmethod
    def _metadata_last_modified_time(metadata) -> Optional[datetime]:
        if metadata is None:
            return None
        has_field = getattr(metadata, "HasField", None)
        if callable(has_field):
            try:
                if not has_field("last_modified_timestamp"):
                    return None
            except ValueError:
                return None
        timestamp = getattr(metadata, "last_modified_timestamp", None)
        if timestamp is None:
            return None
        to_datetime = getattr(timestamp, "ToDatetime", None)
        if callable(to_datetime):
            return to_datetime()
        if isinstance(timestamp, datetime):
            return timestamp
        return None

    @staticmethod
    def _parse_content_range_header(content_range: str | None) -> Optional[tuple[int, int, int]]:
        if not content_range:
            return None
        m = _CONTENT_RANGE_PATTERN.match(content_range.strip())
        if not m:
            return None
        total_str = m.group(3)
        if total_str == "*":
            return None
        return int(m.group(1)), int(m.group(2)), int(total_str)

    @staticmethod
    def _iter_byte_ranges(total_size: int, part_size: int) -> Generator[tuple[int, int], None, None]:
        start = 0
        while start < total_size:
            end = min(total_size - 1, start + part_size - 1)
            yield start, end
            start = end + 1

    async def _download_redirect_single_stream_to_file(
        self,
        session: aiohttp.ClientSession,
        redirect_target_url: str,
        headers: Dict[str, str],
        file_path: str,
        expected_size: Optional[int],
    ) -> None:
        downloaded_bytes = 0

        async def _download():
            nonlocal downloaded_bytes
            async with session.get(_presigned_url(redirect_target_url), headers=headers) as redirected_response:
                redirected_response.raise_for_status()
                with open(file_path, "wb") as f:
                    async for chunk in redirected_response.content.iter_chunked(_STORAGEAPI_DOWNLOAD_CHUNK_SIZE):
                        f.write(chunk)
                        downloaded_bytes += len(chunk)

        await self._throttler_networkdata.run(_download())
        if expected_size is not None and downloaded_bytes != expected_size:
            logger.warning(
                "Downloaded %d bytes from redirect for %s but metadata reported %d bytes",
                downloaded_bytes,
                redirect_target_url,
                expected_size,
            )

    async def _probe_redirect_range_download_size(
        self, session: aiohttp.ClientSession, redirect_target_url: str, headers: Dict[str, str]
    ) -> Optional[int]:
        probe_headers = dict(headers)
        probe_headers["Range"] = "bytes=0-0"

        async def _probe() -> Optional[int]:
            async with session.get(_presigned_url(redirect_target_url), headers=probe_headers) as response:
                if response.status != 206:
                    await response.read()
                    return None
                parsed = self._parse_content_range_header(response.headers.get("Content-Range"))
                await response.read()
                if parsed is None:
                    return None
                start, end, total_size = parsed
                if start != 0 or end != 0 or total_size < 1:
                    return None
                return total_size

        return await self._throttler_networkdata.run(_probe())

    async def _download_redirect_ranges_to_file(
        self,
        session: aiohttp.ClientSession,
        redirect_target_url: str,
        headers: Dict[str, str],
        file_path: str,
        total_size: int,
    ) -> None:
        if total_size < 1:
            raise _RangeDownloadNotSupported("Range download requires a positive total size")

        with open(file_path, "wb") as f:
            f.truncate(total_size)

        async def _download_one_range(start: int, end: int):
            range_headers = dict(headers)
            range_headers["Range"] = f"bytes={start}-{end}"

            async def _download():
                async with session.get(_presigned_url(redirect_target_url), headers=range_headers) as response:
                    if response.status != 206:
                        await response.read()
                        raise _RangeDownloadNotSupported(f"Range request {start}-{end} returned HTTP {response.status}, expected 206")
                    parsed = self._parse_content_range_header(response.headers.get("Content-Range"))
                    if parsed is None:
                        await response.read()
                        raise _RangeDownloadNotSupported(f"Range request {start}-{end} did not return a valid Content-Range header")
                    response_start, response_end, response_total = parsed
                    if response_start != start or response_end != end or response_total != total_size:
                        await response.read()
                        raise _RangeDownloadNotSupported(
                            f"Range request {start}-{end} got unexpected Content-Range '{response.headers.get('Content-Range')}'"
                        )
                    bytes_written = 0
                    with open(file_path, "r+b") as f:
                        f.seek(start)
                        async for chunk in response.content.iter_chunked(_STORAGEAPI_DOWNLOAD_CHUNK_SIZE):
                            f.write(chunk)
                            bytes_written += len(chunk)
                    expected_bytes = end - start + 1
                    if bytes_written != expected_bytes:
                        raise StorageOperationError(
                            f"Range request {start}-{end} for {redirect_target_url} wrote {bytes_written} bytes, expected {expected_bytes}"
                        )

            await self._throttler_networkdata.run(_download())

        ranges = list(self._iter_byte_ranges(total_size, self._download_range_part_size))
        queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        for part in ranges:
            queue.put_nowait(part)

        worker_count = min(self._download_range_max_concurrency, len(ranges))
        if worker_count < 1:
            raise StorageOperationError("Cannot execute ranged download with zero workers")

        async def _worker():
            while True:
                try:
                    start, end = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await _download_one_range(start, end)
                finally:
                    queue.task_done()

        await asyncio.gather(*[asyncio.create_task(_worker()) for _ in range(worker_count)])

        final_size = os.path.getsize(file_path)
        if final_size != total_size:
            raise StorageOperationError(
                f"Range download for {redirect_target_url} produced file of size {final_size}, expected {total_size}"
            )

    async def _download_redirect_to_file(
        self,
        session: aiohttp.ClientSession,
        redirect_target_url: str,
        headers: Dict[str, str],
        file_path: str,
        expected_size: Optional[int],
    ) -> None:
        max_attempts = self._parse_positive_int_env(
            _DOWNLOAD_RETRIES_ENV_VAR,
            3,
            legacy_env_var=_LEGACY_DOWNLOAD_RETRIES_ENV_VAR,
        )
        base_delay = 1.0

        for attempt in range(1, max_attempts + 1):
            try:
                if self._enable_range_download:
                    total_size = await self._probe_redirect_range_download_size(session, redirect_target_url, headers)
                    if total_size is not None:
                        try:
                            await self._download_redirect_ranges_to_file(
                                session=session,
                                redirect_target_url=redirect_target_url,
                                headers=headers,
                                file_path=file_path,
                                total_size=total_size,
                            )
                            return
                        except _RangeDownloadNotSupported as e:
                            logger.debug("Range download unsupported for %s, falling back to single stream: %s", redirect_target_url, e)

                await self._download_redirect_single_stream_to_file(
                    session=session,
                    redirect_target_url=redirect_target_url,
                    headers=headers,
                    file_path=file_path,
                    expected_size=expected_size,
                )
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, StorageOperationError) as e:
                if attempt == max_attempts:
                    raise
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(f"[{redirect_target_url}] download attempt {attempt}/{max_attempts} failed: {e}. Retrying in {delay:.1f}s...")
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)

    async def _download_redirect_to_bytes(
        self,
        session: aiohttp.ClientSession,
        redirect_target_url: str,
        headers: Dict[str, str],
        expected_size: Optional[int],
    ) -> bytes:
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name
            await self._download_redirect_to_file(
                session=session,
                redirect_target_url=redirect_target_url,
                headers=headers,
                file_path=tmp_path,
                expected_size=expected_size,
            )
            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except FileNotFoundError:
                    pass

    async def read_resource_identity(self, resource_identity: str) -> Tuple[Result, bytes]:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        try:
            return await self._run_read_operation_with_retries(
                resource_identity,
                lambda: self._read_resource_identity_once(resource_identity),
            )
        except grpc.RpcError as e:
            raise StorageOperationError(f"Error calling read: {str(e)}") from e

    async def _read_resource_identity_once(self, resource_identity: str) -> Tuple[Result, bytes]:
        try:
            data = bytearray()
            expected_size: Optional[int] = None
            async for reply in self._iterate_grpc(
                self._throttler_networkdata,
                self._fileobject_api.Read,
                self._fileobject_msgs.ReadRequest(
                    resource_identity=self._fileobject_types.ResourceIdentity(encoded_identity=resource_identity),
                ),
            ):
                if reply.HasField("metadata"):
                    expected_size = self._metadata_data_object_size(reply.metadata)
                elif reply.HasField("chunk"):
                    data.extend(reply.chunk.chunk)
                elif reply.HasField("redirect"):
                    try:
                        session = await self._ensure_aiohttp_session()
                        redirect_headers = {header.name: header.value for header in reply.redirect.additional_headers}
                        redirect_content = await self._download_redirect_to_bytes(
                            session=session,
                            redirect_target_url=reply.redirect.redirect_target_url,
                            headers=redirect_headers,
                            expected_size=expected_size,
                        )
                        data.extend(redirect_content)
                        logger.debug("Done downloading from webserver")
                        return Result.OK, bytes(data)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        raise StorageOperationError(f"{reply.redirect.redirect_target_url} failed to download: {e}") from e
                else:
                    raise StorageOperationError(f"Unexpected reply {reply}")
            logger.debug("Done downloading via grpc)")
            return Result.OK, bytes(data)
        except grpc.RpcError:
            raise

    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(file_url)
        try:
            return await self._run_read_operation_with_retries(
                resource_address,
                lambda: self._read_file_content_once(file_url, resource_address),
            )
        except grpc.RpcError as e:
            raise StorageOperationError(f"Error calling read: {str(e)}") from e

    async def _read_file_content_once(self, file_url: str, resource_address: str) -> Tuple[Result, bytes]:
        try:
            data = bytearray()
            expected_size: Optional[int] = None
            async for reply in self._iterate_grpc(
                self._throttler_networkdata,
                self._fileobject_api.ReadFromAddress,
                self._fileobject_msgs.ReadFromAddressRequest(
                    resource_address=_encode_resource_address_for_storage_service(resource_address)
                ),
            ):
                if reply.HasField("resource_info"):
                    expected_size = self._metadata_data_object_size(reply.resource_info.metadata)
                    logger.debug(f"Found {file_url}, downloading {expected_size if expected_size is not None else 'unknown'} bytes")
                elif reply.HasField("chunk"):
                    data.extend(reply.chunk.chunk)
                elif reply.HasField("redirect"):
                    try:
                        session = await self._ensure_aiohttp_session()
                        redirect_headers = {header.name: header.value for header in reply.redirect.additional_headers}
                        redirect_content = await self._download_redirect_to_bytes(
                            session=session,
                            redirect_target_url=reply.redirect.redirect_target_url,
                            headers=redirect_headers,
                            expected_size=expected_size,
                        )
                        data.extend(redirect_content)
                        logger.debug("Done downloading from webserver")
                        return Result.OK, bytes(data)
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        raise StorageOperationError(f"{reply.redirect.redirect_target_url} failed to download: {e}") from e
                else:
                    raise StorageOperationError(f"Unexpected reply {reply}")
            logger.debug(f"Done downloading via grpc)")
            return Result.OK, bytes(data)
        except grpc.RpcError:
            raise

    async def _download_to_file(self, src_url: str, file_path: str) -> Result:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(src_url)
        attempt_path = f"{file_path}.wrapp-download-{secrets.token_hex(8)}.tmp"

        async def _download_once_to_temp_file() -> Result:
            result = await self._download_to_file_once(src_url, attempt_path, resource_address)
            if result == Result.OK:
                os.replace(attempt_path, file_path)
            else:
                await _remove_partial_download()
            return result

        async def _remove_partial_download() -> None:
            try:
                os.unlink(attempt_path)
            except FileNotFoundError:
                pass

        try:
            return await self._run_read_operation_with_retries(
                resource_address,
                _download_once_to_temp_file,
                on_retry=_remove_partial_download,
            )
        except grpc.RpcError as e:
            await _remove_partial_download()
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return Result.ERROR_NOT_FOUND
            raise StorageOperationError(f"Error calling read: {str(e)}") from e

    async def _download_to_file_once(self, src_url: str, file_path: str, resource_address: str) -> Result:
        request = self._fileobject_msgs.ReadFromAddressRequest(
            resource_address=_encode_resource_address_for_storage_service(resource_address)
        )

        async def _throttled_grpc_download() -> Tuple[Optional[Any], Optional[int], Result]:
            """Drain the Read stream under ONE held NetworkDataTransfer slot.

            The RPC is created only after the slot is held (mirroring _iterate_lazy)
            and the destination is opened lazily on the first chunk, so at most
            file_transfer_jobs destination files are open at once (OMPE-74369).
            Nothing in here may re-acquire networkdata: a redirect reply is returned
            to the caller and handled after the slot is released, because the
            redirect download acquires its own slots.

            Returns (redirect_reply, expected_size, result); redirect_reply is None
            when the transfer was handled via gRPC chunks, in which case result is
            ERROR (as a Result, so the caller's partial-file cleanup runs) when the
            streamed byte count does not match the declared object size.
            """
            expected_size: Optional[int] = None
            downloaded_bytes = 0
            dst_file = None
            grpc_metadata = await self._grpc_metadata()
            if grpc_metadata:
                stream = self._fileobject_api.ReadFromAddress(request, metadata=grpc_metadata)
            else:
                stream = self._fileobject_api.ReadFromAddress(request)
            try:
                async for reply in stream:
                    if reply.HasField("resource_info"):
                        metadata = reply.resource_info.metadata
                        if metadata.HasField("data_object_size"):
                            expected_size = metadata.data_object_size
                        logger.debug(f"Found {src_url}, downloading {expected_size if expected_size is not None else 'unknown'} bytes")
                    elif reply.HasField("chunk"):
                        if dst_file is None:
                            dst_file = open(file_path, "wb")
                        chunk_data = reply.chunk.chunk
                        dst_file.write(chunk_data)
                        downloaded_bytes += len(chunk_data)
                    elif reply.HasField("redirect"):
                        if dst_file is not None:
                            raise StorageOperationError(f"Unexpected redirect message for {src_url} after streaming chunks had started.")
                        return reply, expected_size, Result.OK
                    else:
                        raise StorageOperationError(f"Unexpected reply {reply}")

                if dst_file is None:
                    # Represents a valid 0-byte object delivered without chunk messages.
                    with open(file_path, "wb"):
                        pass
                if expected_size is not None and downloaded_bytes != expected_size:
                    # A stream that ended cleanly but short (or long) must not be
                    # reported as a successful download of the declared object.
                    logger.error(
                        "Downloaded %d bytes for %s via gRPC chunks, but metadata reported %d bytes; failing the download",
                        downloaded_bytes,
                        src_url,
                        expected_size,
                    )
                    return None, expected_size, Result.ERROR
                logger.debug("Done downloading via grpc")
                return None, expected_size, Result.OK
            finally:
                if dst_file is not None:
                    dst_file.close()
                # No-op once the stream completed; stops the RPC when we leave it
                # early on a redirect reply. (Guarded: test fakes are plain async
                # generators without cancel().)
                cancel = getattr(stream, "cancel", None)
                if cancel is not None:
                    cancel()

        try:
            redirect_reply, expected_size, grpc_result = await self._throttler_networkdata.run(_throttled_grpc_download())
        except grpc.RpcError as e:
            # Mirror _iterate_grpc's UNAUTHENTICATED handling for the raw stream.
            if e.code() != grpc.StatusCode.UNAUTHENTICATED or not await self._try_refresh_auth():
                raise
            redirect_reply, expected_size, grpc_result = await self._throttler_networkdata.run(_throttled_grpc_download())

        if redirect_reply is None:
            return grpc_result

        session = await self._ensure_aiohttp_session()
        redirect_headers = {header.name: header.value for header in redirect_reply.redirect.additional_headers}
        try:
            await self._download_redirect_to_file(
                session=session,
                redirect_target_url=redirect_reply.redirect.redirect_target_url,
                headers=redirect_headers,
                file_path=file_path,
                expected_size=expected_size,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise StorageOperationError(f"{redirect_reply.redirect.redirect_target_url} failed to download: {e}") from e
        logger.debug("Done downloading from webserver")
        return Result.OK

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(file_url)
        result, sent_hash = await self._write_source_content(resource_address, _WriteSource.from_bytes(content))
        if result == Result.OK:
            if sent_hash is None:
                sent_hash, _ = calc_nucleus_hash_from_stream(io.BytesIO(content))
            await self._set_nucleus_hash(resource_address, sent_hash)
        return result

    async def write_file_content(self, resource_address: str, content: bytes) -> Result:
        """Upload the contents of a file object at the specified address."""
        result, _ = await self._write_source_content(resource_address, _WriteSource.from_bytes(content))
        return result

    async def _write_source_content(self, resource_address: str, source: _WriteSource) -> Tuple[Result, Optional[str]]:
        """Upload the source's content; returns the result and the nucleus hash of the bytes actually sent.

        The hash is computed from the bytes each transport streams out, so the
        stored hash can never go stale relative to the uploaded content, even if
        the source file changes undetectably while queued (OMPE-74369). It is
        None when the transferred bytes could not be observed (e.g. a request
        body that was not read sequentially); callers then hash separately.
        """
        max_attempts = self._parse_positive_int_env(
            _UPLOAD_RETRIES_ENV_VAR,
            3,
            legacy_env_var=_LEGACY_UPLOAD_RETRIES_ENV_VAR,
        )
        base_delay = 1.0

        for attempt in range(1, max_attempts + 1):
            try:
                return await self._write_file_content_once(resource_address, source)
            except grpc.RpcError as e:
                if attempt == max_attempts or not self._is_retryable_grpc_error(e):
                    raise StorageOperationError(f"Error calling Write for {resource_address}: {str(e)}") from e
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(f"[{resource_address}] gRPC write attempt {attempt}/{max_attempts} failed: {e}. Retrying in {delay:.1f}s...")
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)

        raise AssertionError("_write_source_content retry loop exited without returning or raising")

    async def _write_file_content_once(self, resource_address: str, source: "_WriteSource") -> Tuple[Result, Optional[str]]:
        """Upload once using a fresh request stream; see _write_source_content for the return contract."""
        assert self._fileobject_msgs is not None
        assert self._fileobject_types is not None
        content_length = source.size
        # Fresh per attempt: hashes the bytes this attempt actually sends.
        sent_hasher = IncrementalNucleusHasher()
        async with _write_message_queue_async() as (requests, request_iterator):
            await requests.put(
                self._fileobject_msgs.WriteRequest(
                    params=self._fileobject_msgs.WriteParameters(
                        destination_resource_address=_encode_resource_address_for_storage_service(resource_address),
                        data_object_size=content_length,
                    ),
                ),
            )

            write_responses = self._iterate_grpc(self._throttler_networkdata, self._fileobject_api.Write, request_iterator)

            flow_control_message = await anext(aiter(write_responses))

            # A service may immediately respond with a "resource info" message, meaning that a 0-byte object write has
            # been performed immediately without the need for any further actions on the client end.
            if flow_control_message.HasField("resource_info"):
                if content_length == 0:
                    # The server wrote the object without the client ever opening the
                    # source; make sure it is still the 0-byte file that was advertised
                    # (OMPE-74369).
                    source.ensure_unchanged()
                    empty_hash, _ = sent_hasher.finalize()
                    return Result.OK, empty_hash
                return Result.ERROR, None

            # If we receive a "write chunks accepted" message, stream the chunks and finally await for a "resource info"
            # response message.
            if flow_control_message.HasField("write_chunks_accepted"):
                Chunk = self._fileobject_types.Chunk
                WReq = self._fileobject_msgs.WriteRequest

                async def _stream_chunks() -> None:
                    # The source is opened and read only while this task holds ONE
                    # NetworkDataTransfer slot for the whole data transfer; previously
                    # the chunk loop ran unthrottled with the entire file in memory
                    # (OMPE-74369). Nothing in here may re-acquire networkdata.
                    with source.open() as f_src:
                        while True:
                            data = f_src.read(_STORAGEAPI_WRITE_CHUNK_SIZE)
                            if not data:
                                break
                            sent_hasher.update(data)
                            await requests.put(WReq(chunk=Chunk(chunk=data)))

                await self._throttler_networkdata.run(_stream_chunks())
                # End chunk transmission.
                await requests.put(None)
                resource_info_response = await anext(aiter(write_responses))
                if resource_info_response.HasField("resource_info"):
                    sent_hash, _ = sent_hasher.finalize()
                    return Result.OK, sent_hash
                else:
                    # Resource info message is expected.
                    return Result.ERROR, None

        # Write redirect and multipart upload methods don't operate over the same stream of Write requests, so we should
        # close the write stream.
        if flow_control_message.HasField("write_redirect"):
            redirect_response = await self._write_via_redirect(flow_control_message.write_redirect, source, sent_hasher)
            if 200 <= redirect_response.status < 300:
                await self._complete_redirect_upload(resource_address, flow_control_message.write_redirect, redirect_response)
                if sent_hasher.is_valid and sent_hasher.size == content_length:
                    sent_hash, _ = sent_hasher.finalize()
                    return Result.OK, sent_hash
                return Result.OK, None
            else:
                raise StorageOperationError(
                    f"Failed to write to redirect URL {flow_control_message.write_redirect.redirect_target_url}: {redirect_response.status}"
                )

        if flow_control_message.HasField("multipart_upload"):
            multipart_result = await self._write_via_multipart(flow_control_message.multipart_upload, resource_address, source, sent_hasher)
            if multipart_result == Result.OK and sent_hasher.is_valid and sent_hasher.size == content_length:
                sent_hash, _ = sent_hasher.finalize()
                return multipart_result, sent_hash
            return multipart_result, None

        # "Unexpected flow control message."
        return Result.ERROR, None

    async def _complete_redirect_upload(self, resource_address: str, parameters, response) -> None:
        """Synchronously register a redirect upload via CompleteRedirectUpload.

        The RPC is optional in the protocol, but servers that otherwise learn of
        uploads only through storage-event pipelines can lag by minutes, leaving
        the object invisible to Stat/List ("phantom"). Failure is non-fatal —
        we fall back to the previous fire-and-forget behavior.
        """
        try:
            headers = [
                self._fileobject_types.Header(name=name, value=response.headers[name])
                for name in getattr(parameters, "completion_header_names", [])
                if name in response.headers
            ]
            await self._run_grpc(
                self._throttler_roundtrip,
                self._fileobject_api.CompleteRedirectUpload,
                self._fileobject_msgs.CompleteRedirectUploadRequest(
                    destination_resource_address=_encode_resource_address_for_storage_service(resource_address),
                    additional_headers=headers,
                ),
            )
        except grpc.RpcError as e:
            logger.info(f"CompleteRedirectUpload not accepted for {resource_address} ({e.code()}); " "relying on server-side registration")

    async def _write_via_redirect(
        self, parameters, source: "_WriteSource", sent_hasher: Optional[IncrementalNucleusHasher] = None
    ) -> aiohttp.ClientResponse:
        max_attempts = self._parse_positive_int_env(
            _UPLOAD_RETRIES_ENV_VAR,
            3,
            legacy_env_var=_LEGACY_UPLOAD_RETRIES_ENV_VAR,
        )
        base_delay = 1.0

        for attempt in range(1, max_attempts + 1):
            try:
                session = await self._ensure_aiohttp_session()
                headers = {header.name: header.value for header in parameters.additional_headers}
                method = self._upload_method_to_string(parameters.method)

                async def _upload_request():
                    # Open the payload only while holding the NetworkDataTransfer slot
                    # (OMPE-74369); a fresh open per attempt rewinds for the retry loop.
                    with source.open_payload() as payload:
                        reader: Optional[_HashingPayloadReader] = None
                        data: Union[bytes, BinaryIO] = payload
                        if not isinstance(payload, (bytes, bytearray)):
                            # Always wrap file payloads: the wrapper hashes the bytes
                            # aiohttp actually reads, and it keeps the request headers
                            # independent of the local filename (see the class note).
                            reader = _HashingPayloadReader(payload)
                            data = reader
                        async with session.request(
                            method=method,
                            url=_presigned_url(parameters.redirect_target_url),
                            headers=headers,
                            data=data,
                        ) as response:
                            response.raise_for_status()
                            # Read the response to ensure it's complete
                            await response.read()
                            # Feed the shared hasher only for the attempt that succeeded.
                            if sent_hasher is not None:
                                if reader is None:
                                    sent_hasher.update(payload)
                                elif reader.hasher.is_valid and reader.hasher.size == source.size:
                                    sent_hasher.copy_state_from(reader.hasher)
                                else:
                                    sent_hasher.invalidate()
                            return response

                return await self._throttler_networkdata.run(_upload_request())
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                if attempt == max_attempts:
                    raise StorageOperationError(f"Failure to HTTP upload data to {parameters.redirect_target_url}: {str(e)}") from e
                delay = base_delay * (2 ** (attempt - 1))
                logger.info(
                    f"[{parameters.redirect_target_url}] upload attempt {attempt}/{max_attempts} failed: {e}. " f"Retrying in {delay:.1f}s…"
                )
                Stats.get().increment(StatType.GRPC_RETRY)
                await asyncio.sleep(delay)
        raise AssertionError("_write_via_redirect retry loop exited without returning or raising")

    async def _write_via_multipart(
        self, parameters, resource_address: str, source: "_WriteSource", sent_hasher: Optional[IncrementalNucleusHasher] = None
    ) -> Result:
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        # Calculate the number of parts
        part_size = 1024 * 1024
        if parameters.HasField("minimum_size_per_part"):
            part_size = max(part_size, parameters.minimum_size_per_part)
        if parameters.HasField("maximum_size_per_part"):
            part_size = min(part_size, parameters.maximum_size_per_part)
        if part_size == 0:
            raise StorageOperationError(f"Cannot upload multipart upload, allowed part_size calculated as 0")
        total_part_count = ceil(source.size / part_size)
        if parameters.HasField("maximum_parts_number"):
            if total_part_count > parameters.maximum_parts_number:
                # TODO - we'd need to change the default buffer size of 1 MByte now...
                raise StorageOperationError(
                    f"Cannot upload multipart upload, allowed part_number calculated as {total_part_count} exceeds maximum part number {parameters.maximum_parts_number}, need to change default buffer size"
                )

        try:
            # Build a list of redirect URLs to upload the parts to. The first we got from the CreateMultipartUploadResponse
            redirects = [parameters.first_part_write_redirect]
            if total_part_count > 1:
                # We need more than one part, generate the additional upload URLs via a server roundtrip
                response = await self._run_grpc(
                    self._throttler_roundtrip,
                    self._fileobject_api.UploadPart,
                    self._fileobject_msgs.UploadPartRequest(
                        upload_id=parameters.upload_id,
                        destination_resource_address=_encode_resource_address_for_storage_service(resource_address),
                        part_number=1,  # Part number 0 has already been delivered by the first CreateMultipartUploadReponse
                        part_count=total_part_count - 1,
                    ),
                )
                redirects.extend(response.part_write_redirects)

            completed_parts = []
            for part_number in range(total_part_count):
                # Each part is a bounded view of the source; the part's bytes are read
                # from disk only inside _write_via_redirect's throttled request, so at
                # most part_size bytes per held slot are in memory (OMPE-74369). Parts
                # are uploaded in order, so the shared hasher sees the sent bytes in
                # file order.
                part_source = source.slice(part_number * part_size, part_size)
                upload_response = await self._write_via_redirect(redirects[part_number], part_source, sent_hasher)
                if not (200 <= upload_response.status < 300):
                    raise StorageOperationError(f"Got unexpected response from part upload: {upload_response.status}")
                return_headers = []
                for requested_header in redirects[part_number].completion_header_names:
                    if requested_header in upload_response.headers:
                        return_headers.append(
                            self._fileobject_types.Header(name=requested_header, value=upload_response.headers[requested_header])
                        )
                    else:
                        raise StorageOperationError(f"Part upload response missing required return header: '{requested_header}'")
                completed_parts.append(self._fileobject_msgs.CompletedUploadPart(part_number=part_number, headers=return_headers))

            await self._run_grpc(
                self._throttler_roundtrip,
                self._fileobject_api.CompleteMultipartUpload,
                self._fileobject_msgs.CompleteMultipartUploadRequest(
                    upload_id=parameters.upload_id,
                    destination_resource_address=_encode_resource_address_for_storage_service(resource_address),
                    parts=completed_parts,
                ),
            )
            return Result.OK
        except grpc.RpcError as e:
            raise StorageOperationError(f"Error calling MultipartUpload Write: {str(e)}")

    def _upload_method_to_string(self, value) -> str:
        assert self._fileobject_msgs is not None
        um = self._fileobject_msgs
        if value == um.UPLOAD_METHOD_POST:
            return "POST"
        elif value == um.UPLOAD_METHOD_PUT:
            return "PUT"
        raise StorageOperationError(f"Unsupported upload method {value!r}.")

    async def delete_item(self, url: str) -> Result:
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(url)
        try:
            result, is_folder = await self.is_folder(url)
            if result == Result.OK and is_folder:
                await self.delete_item(url + "/.empty")
            await self._run_grpc(
                self._throttler_roundtrip,
                self._fileobject_api.Delete,
                self._fileobject_msgs.DeleteRequest(resource_address=_encode_resource_address_for_storage_service(resource_address)),
            )
            return Result.OK
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.NOT_FOUND:
                return Result.ERROR_NOT_FOUND
            raise StorageOperationError(f"Error calling delete: {str(e)}")

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        async def _perform_delete(resource_address):
            r = await self.delete_item(resource_address)
            if r != Result.OK:
                logger.error(f"Delete tree '{url}': failed to delete item '{resource_address}'")
            return r

        task_results: dict[asyncio.Task, Result] = {}
        async with task_node.create_task_group_node(f"Delete {url}", task_results) as sub_task_node:
            # We cannot delete the tree, but we can enumerate it
            throttled_paginator = self.list_file_tree(url)

            async for item in throttled_paginator:
                if item.is_regular_file:
                    if isinstance(item, StorageAPIListEntry):
                        storageapi_item: StorageAPIListEntry = item
                        # list_file_tree() returns relative paths for catalog-style consumers.
                        # delete_item() needs an address valid for Storage API RPCs, so resolve
                        # relative entries against the tree root while preserving absolute ones.
                        delete_target = storageapi_item._resource_address
                        if not urllib.parse.urlsplit(delete_target).scheme:
                            relative_delete_target = delete_target.lstrip("/\\")
                            delete_target = f"{url.rstrip('/')}/{relative_delete_target}"
                        sub_task_node.create_task(_perform_delete(delete_target), name="Deleting object")
                    else:
                        raise RuntimeError("Expected a StorageAPIListEntry from list_file_tree!")
        for r in task_results.values():
            if r != Result.OK:
                return r
        return Result.OK

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        if not is_file_url(src_url):
            raise RuntimeError(f"{src_url} is not a file")
        if not overwrite:
            stat_result, entry = await self.stat(dst_url)
            if stat_result == Result.OK:
                return Result.ERROR_ALREADY_EXISTS
        file_path = get_path_from_url(src_url)
        await self._ensure_grpc_stubs()
        if not self._fileobject_api:
            raise StorageOperationError("Failed to initialize Storage API backend!")
        resource_address = await self._sanitize_address_for_storage_service(dst_url)
        try:
            # No pre-hash pass and nothing opened before throttling (OMPE-74369):
            # only the path travels on, the transfer opens it once a
            # NetworkDataTransfer slot is held, and the nucleus hash is computed
            # from the bytes the transfer actually sends — so the stored hash can
            # never go stale relative to the uploaded content, and the file is
            # read exactly once. The stat snapshot taken here still rejects
            # detectably-changed sources early (see _WriteSource).
            source = _WriteSource.from_path(file_path)
            result, sent_hash = await self._write_source_content(resource_address, source)
        except FileNotFoundError:
            return Result.ERROR_NOT_FOUND
        except PermissionError as e:
            logger.error(f"Storage API upload_file, permission error {src_url}: {e}")
            return Result.ERROR_ACCESS_DENIED
        if result == Result.OK:
            if sent_hash is None:
                # The transferred bytes could not be observed (e.g. the request body
                # was not read sequentially). Never hash the mutable path after the
                # fact — that could store a hash of different bytes than were
                # uploaded. Leave the hash metadata unset instead; a missing nucleus
                # hash is an accepted state (like writes without a metadata service).
                # An overwritten destination may still carry the previous object's
                # hash, which must not be left describing the new content.
                logger.warning(f"Not storing a nucleus hash for {dst_url}: the uploaded bytes could not be hashed while streaming")
                await self._delete_nucleus_hash(resource_address)
            else:
                await self._set_nucleus_hash(resource_address, sent_hash)
        return result

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        if not is_file_url(dst_url):
            raise RuntimeError(f"{dst_url} is not a file url")
        file_path = get_path_from_url(dst_url)
        if not overwrite and os.path.exists(file_path):
            return Result.ERROR_ALREADY_EXISTS
        if os.path.isdir(file_path):
            return Result.ERROR_WRONG_TYPE
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        return await self._download_to_file(src_url, file_path)


@asynccontextmanager
async def _write_message_queue_async() -> AsyncGenerator[tuple[asyncio.Queue, AsyncGenerator], None]:
    request_messages: asyncio.Queue = asyncio.Queue(5)

    async def yield_request_messages():
        while True:
            message = await request_messages.get()
            if message is not None:
                yield message
            else:
                break

    try:
        yield request_messages, yield_request_messages()
    finally:
        await request_messages.put(None)
