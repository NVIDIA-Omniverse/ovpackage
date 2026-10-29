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

"""Read-only HTTP/HTTPS storage backend."""

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncGenerator
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage_interface import (
    ConnectionStatus,
    DirectorySemantics,
    ListEntry,
    LocalReadOnlyFile,
    ResourceIdentity,
    Result,
    SerializedResourceIdentity,
    ServerInfo,
    StorageAuthInfo,
    StorageInterface,
)
from wrapp.utils.task_throttler import (
    TaskThrottler,
    ThrottlerType,
)
from wrapp.utils.utils import (
    StorageMissingDependenciesError,
    StorageOperationError,
    file_url_to_path,
    is_file_url,
)

try:
    import aiohttp

    from wrapp.utils.storage_impls._http_transfer import (
        encoded_http_url,
        stream_http_response_to_file,
    )
except ImportError as exc:
    raise StorageMissingDependenciesError(
        "HTTP storage dependencies are missing. Install ovpackage[http]. " f"Original import error: {exc}"
    ) from exc

logger = logging.getLogger("omni.wrapp")

_DOWNLOAD_CHUNK_SIZE = 1024 * 1024
_MAX_REDIRECTS = 16


def is_http_url(url: str) -> bool:
    return urlsplit(url).scheme.lower() in ("http", "https")


def _result_for_status(status: int) -> Result:
    if 200 <= status < 300:
        return Result.OK
    if status in (401, 403):
        return Result.ERROR_ACCESS_DENIED
    if status == 404:
        return Result.ERROR_NOT_FOUND
    if status == 400:
        return Result.ERROR_BAD_REQUEST
    return Result.ERROR_CONNECTION if status >= 500 else Result.ERROR


def _modified_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None


class HTTPListEntry(ListEntry):
    def __init__(self, url: str, size: int | None, modified_time: datetime | None, etag: str = ""):
        self._url = url
        self._size = size
        self._modified_time = modified_time
        self._etag = etag
        self._nucleus_hash: str | None = None

    @property
    def is_folder(self) -> bool:
        return False

    @property
    def is_regular_file(self) -> bool:
        return True

    @property
    def has_checkpoint(self) -> bool:
        return False

    @property
    def version(self) -> str:
        return self._etag

    @property
    def relative_path(self) -> str:
        return self._url.rsplit("/", 1)[-1].split("?", 1)[0]

    @property
    def nucleus_hash(self) -> str | None:
        return self._nucleus_hash

    @nucleus_hash.setter
    def nucleus_hash(self, value: str):
        if self._nucleus_hash is not None:
            raise AttributeError("Nucleus hash cannot be changed once set")
        self._nucleus_hash = value

    @property
    def size(self) -> int | None:
        return self._size

    @property
    def modified_time(self) -> datetime | None:
        return self._modified_time


class HTTPServerInfo(ServerInfo):
    def supports_create_with_hash(self, target_url: str) -> bool:
        return False

    @property
    def auth_token(self) -> str:
        return ""


class HTTPResourceIdentity(ResourceIdentity):
    def __init__(self, url: str):
        self._url = url

    async def read_data(self, storage) -> tuple[Result, bytes]:
        return await storage.read_file_content(self._url)

    def to_string(self) -> SerializedResourceIdentity:
        return SerializedResourceIdentity(self._url)


class HTTPLocalReadOnlyFile(LocalReadOnlyFile):
    def __init__(self, path: str):
        self._path = path

    def local_path(self) -> str:
        return self._path

    def close(self):
        if self._path:
            try:
                os.remove(self._path)
            except FileNotFoundError:
                pass
            finally:
                self._path = ""


class HTTPStorage(StorageInterface):
    """Read files from ordinary HTTP and HTTPS endpoints without clientlib."""

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_parameters: ThrottlingParameters):
        self._wrapp_version = wrapp_version
        self._debug_level = debug_level
        self._session: aiohttp.ClientSession | None = None
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "HTTP", throttling_parameters.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "HTTP", throttling_parameters.file_transfer_jobs)

    @staticmethod
    def version_info() -> str:
        from importlib.metadata import (
            PackageNotFoundError,
            version,
        )

        try:
            return f"HTTPStorage (read-only) with aiohttp {version('aiohttp')}"
        except PackageNotFoundError:
            return "HTTPStorage (read-only) with aiohttp"

    @property
    def is_read_only(self) -> bool:
        return True

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Accept-Encoding": "identity", "User-Agent": f"WRAPP/{self._wrapp_version}"},
                auto_decompress=False,
                requote_redirect_url=False,
            )
        return self._session

    def supports_tags(self, url: str) -> bool:
        return False

    def directory_semantics(self) -> DirectorySemantics:
        return DirectorySemantics.PREFIX

    def supports_comments(self) -> bool:
        return False

    async def get_server_info(self, url: str) -> tuple[Result, ServerInfo]:
        return Result.OK, HTTPServerInfo()

    async def normalize(self, url: str) -> str:
        return url

    async def _stat_with_get(self, url: str) -> tuple[Result, ListEntry | None]:
        session = await self._get_session()
        headers = {"Range": "bytes=0-0"}
        async with session.get(encoded_http_url(url), headers=headers, max_redirects=_MAX_REDIRECTS) as response:
            content_range = response.headers.get("Content-Range", "")
            if response.status == 416 and content_range.strip().endswith("/0"):
                return Result.OK, HTTPListEntry(
                    url,
                    0,
                    _modified_time(response.headers.get("Last-Modified")),
                    response.headers.get("ETag", ""),
                )
            result = _result_for_status(response.status)
            if result != Result.OK:
                return result, None
            size: int | None = None
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1]
                if total.isdigit():
                    size = int(total)
            if size is None and response.content_length is not None:
                size = response.content_length
            return Result.OK, HTTPListEntry(
                url, size, _modified_time(response.headers.get("Last-Modified")), response.headers.get("ETag", "")
            )

    async def stat(self, url: str) -> tuple[Result, ListEntry | None]:
        if not is_http_url(url):
            raise ValueError(f"HTTP storage cannot handle URL: {url}")

        async def _stat():
            try:
                session = await self._get_session()
                async with session.head(encoded_http_url(url), allow_redirects=True, max_redirects=_MAX_REDIRECTS) as response:
                    if response.status in (403, 405, 501):
                        return await self._stat_with_get(url)
                    result = _result_for_status(response.status)
                    if result != Result.OK:
                        return result, None
                    return Result.OK, HTTPListEntry(
                        url,
                        response.content_length,
                        _modified_time(response.headers.get("Last-Modified")),
                        response.headers.get("ETag", ""),
                    )
            except TimeoutError:
                return Result.ERROR_CONNECTION, None
            except aiohttp.ClientError:
                return Result.ERROR_CONNECTION, None

        return await self._throttler_roundtrip.run(_stat())

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> tuple[str, str, ResourceIdentity]:
        return file_url, relative_url, HTTPResourceIdentity(file_url)

    async def is_folder(self, url: str) -> tuple[Result, bool]:
        result, _ = await self.stat(url)
        return result, False

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        raise StorageOperationError("HTTP storage does not support directory listing", Result.ERROR_NOT_SUPPORTED)
        yield  # pragma: no cover

    def supports_list_directory(self) -> bool:
        return False

    async def list_checkpoints(self, url: str) -> tuple[Result, list[ListEntry]]:
        return Result.ERROR_NOT_SUPPORTED, []

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        return False

    def _read_only(self, url: str) -> StorageOperationError:
        return StorageOperationError(f"Cannot write to '{url}': HTTP storage is read-only", Result.ERROR_NOT_SUPPORTED)

    async def copy_address(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        raise self._read_only(dst_url)

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        raise self._read_only(copy_target)

    async def read_file_content(self, file_url: str) -> tuple[Result, bytes]:
        async def _read():
            try:
                session = await self._get_session()
                async with session.get(encoded_http_url(file_url), max_redirects=_MAX_REDIRECTS) as response:
                    result = _result_for_status(response.status)
                    return (result, await response.read()) if result == Result.OK else (result, b"")
            except (TimeoutError, aiohttp.ClientError):
                return Result.ERROR_CONNECTION, b""

        return await self._throttler_networkdata.run(_read())

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> tuple[Result, LocalReadOnlyFile | None]:
        if not download:
            return Result.ERROR_NOT_FOUND, None
        fd, path = tempfile.mkstemp(prefix="wrapp_http_")
        os.close(fd)
        result = await self._download_to_path(file_url, path, overwrite=True)
        if result != Result.OK:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            return result, None
        return Result.OK, HTTPLocalReadOnlyFile(path)

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        raise self._read_only(file_url)

    async def delete_item(self, url: str) -> Result:
        raise self._read_only(url)

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        raise self._read_only(url)

    def can_be_cached(self, url: str) -> bool:
        return True

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: str | None = None) -> Result:
        raise self._read_only(dst_url)

    async def _download_to_path(self, src_url: str, dst_path: str, overwrite: bool) -> Result:
        should_remove_partial = False
        try:
            parent = os.path.dirname(dst_path)
            if parent:
                os.makedirs(parent, exist_ok=True)

            async def _download():
                nonlocal should_remove_partial
                # Match the existing Storage API redirect path: writes are
                # chunked and bounded by the transfer throttler.
                with open(dst_path, "wb" if overwrite else "xb") as destination:  # noqa: ASYNC230
                    should_remove_partial = True
                    session = await self._get_session()
                    await stream_http_response_to_file(session, src_url, {}, destination, chunk_size=_DOWNLOAD_CHUNK_SIZE)

            await self._throttler_networkdata.run(_download())
            should_remove_partial = False
            return Result.OK
        except FileExistsError:
            return Result.ERROR_ALREADY_EXISTS
        except PermissionError:
            return Result.ERROR_ACCESS_DENIED
        except aiohttp.ClientResponseError as exc:
            return _result_for_status(exc.status)
        except (TimeoutError, aiohttp.ClientError):
            return Result.ERROR_CONNECTION
        except asyncio.CancelledError:
            raise
        except OSError:
            return Result.ERROR
        finally:
            if should_remove_partial:
                try:
                    os.remove(dst_path)
                except FileNotFoundError:
                    pass

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        """Download an address to a native path or an escaped file URL."""
        if not is_file_url(dst_url):
            raise ValueError(f"HTTP downloads require a file URL destination: {dst_url}")
        return await self._download_to_path(src_url, file_url_to_path(dst_url), overwrite)

    def set_auth_info(self, auth_info: list[StorageAuthInfo]):
        if auth_info:
            raise StorageOperationError("HTTP storage does not support authentication parameters", Result.ERROR_NOT_SUPPORTED)

    def add_auth_info(self, auth_info: StorageAuthInfo):
        raise StorageOperationError("HTTP storage does not support authentication parameters", Result.ERROR_NOT_SUPPORTED)

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        raise StorageOperationError("HTTP storage does not support authentication parameters", Result.ERROR_NOT_SUPPORTED)

    def get_last_connection_status(self, server_url: str) -> ConnectionStatus | None:
        return ConnectionStatus.CONNECTED

    def backend_throttlers(self) -> list[TaskThrottler]:
        return [self._throttler_roundtrip, self._throttler_networkdata]

    def reconfigure_throttlers(self, params: ThrottlingParameters) -> bool:
        if any(throttler.pending_tasks or throttler.processing_tasks for throttler in self.backend_throttlers()):
            return False
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "HTTP", params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "HTTP", params.file_transfer_jobs)
        return True

    async def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        if self._session is not None:
            await self._session.close()
            self._session = None
