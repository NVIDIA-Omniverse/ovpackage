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
import json
import logging
import threading
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
)
from contextlib import (
    AsyncExitStack,
    asynccontextmanager,
)
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
)
from urllib.parse import (
    urlsplit,
)

from wrapp.datastructures.command_context import (
    CommandParameters,
    ThrottlingParameters,
)
from wrapp.utils.coro import (
    WrappEventLoopThread,
    assert_not_wrapp_thread,
    async_for_in_wrapp_thread,
    force_wrapp_thread,
    sync_force_wrapp_thread,
)
from wrapp.utils.nucleus_hash import calc_nucleus_hash_from_stream
from wrapp.utils.router import (
    Router,
    StorageType,
)
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage_impls.import_tagging_client import g_tagging_client_available
from wrapp.utils.storage_impls.s3_helper import parse_s3_bucket_key
from wrapp.utils.temp_file_manager import TempFileManager

logger = logging.getLogger("omni.wrapp")

from wrapp.utils.storage_interface import (
    ConnectionStatus,
    ListEntry,
    LocalReadOnlyFile,
    ResourceAddress,
    ResourceIdentity,
    Result,
    SerializedResourceIdentity,
    ServerInfo,
    StorageAuthInfo,
    StorageInterface,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    is_nucleus_url,
    make_relative_url,
    normalize_url,
    urlunsplit,
)


def wrapp_is_source_and_destination_same_storage(source: ResourceAddress, destination: ResourceAddress) -> bool:
    source_is_nucleus = is_nucleus_url(source)
    target_is_nucleus = is_nucleus_url(destination)
    if source_is_nucleus and target_is_nucleus:
        return True
    source_is_s3 = Router.me().is_s3_url(source)
    target_is_s3 = Router.me().is_s3_url(destination)
    if source_is_s3 and target_is_s3:
        return True
    source_is_azure = Router.me().is_azure_url(source)
    target_is_azure = Router.me().is_azure_url(destination)
    if source_is_azure and target_is_azure:
        return True
    source_is_gcs = Router.me().is_gcs_url(source)
    target_is_gcs = Router.me().is_gcs_url(destination)
    if source_is_gcs and target_is_gcs:
        return True
    source_is_huggingface = Router.me().is_huggingface_url(source)
    target_is_huggingface = Router.me().is_huggingface_url(destination)
    if source_is_huggingface and target_is_huggingface:
        return True
    # Storage API: "same storage" means the *same connected service instance*.
    # Two different Storage API services are distinct storages even though both
    # are StorageType.StorageAPI, so a copy between them is a cross-storage copy.
    source_storage_api = Router.me().storage_api_instance_for(source)
    if source_storage_api is not None and source_storage_api is Router.me().storage_api_instance_for(destination):
        return True
    return False


@sync_force_wrapp_thread
def wrapp_require_writable(url: str) -> None:
    """Raise a storage error when *url* selects a read-only backend."""

    Router.me().storage_for_write(url)


def _nucleus_identity_source_url(resource_identity: SerializedResourceIdentity) -> Optional[str]:
    """Return the Nucleus checkpoint URL encoded by a resource identity."""
    if is_nucleus_url(resource_identity):
        return resource_identity
    try:
        payload = json.loads(resource_identity)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "nucleus":
        return None
    checkpoint_url = payload.get("checkpoint_url")
    if isinstance(checkpoint_url, str) and is_nucleus_url(checkpoint_url):
        return checkpoint_url
    return None


def _is_same_nucleus_server(source_url: str, target_url: str) -> bool:
    if not is_nucleus_url(source_url) or not is_nucleus_url(target_url):
        return False
    return urlsplit(source_url).netloc.lower() == urlsplit(target_url).netloc.lower()


def _is_resource_identity_copy_same_storage(
    resource_identity: SerializedResourceIdentity,
    copy_target: ResourceAddress,
    source_storage: StorageInterface,
    source_storage_type: StorageType,
    target_storage: StorageInterface,
    target_storage_type: StorageType,
) -> bool:
    if source_storage_type != target_storage_type or source_storage is not target_storage:
        return False
    if source_storage_type == StorageType.Nucleus:
        source_url = _nucleus_identity_source_url(resource_identity)
        if source_url is not None:
            return _is_same_nucleus_server(source_url, copy_target)
    return True


@force_wrapp_thread
async def wrapp_stat(url: str) -> Tuple[Result, ListEntry]:
    Stats.get().increment(StatType.STAT)
    return await Router.me().storage_for_read(url).stat(url)


@force_wrapp_thread
async def wrapp_make_resource_identity(url: str, item: ListEntry, relative_url: str) -> Tuple[str, str, ResourceIdentity]:
    """Build the resource identity for ``url`` via its read backend.

    Wraps ``storage_for_read(url).make_resource_identity(...)`` and returns the
    backend's ``(absolute_url, relative_path, ResourceIdentity)`` tuple unchanged.
    """
    return await Router.me().storage_for_read(url).make_resource_identity(url, item, relative_url)


@dataclass(frozen=True)
class FileObservation:
    """Metadata captured with a read token, never recovered by statting that token."""

    resource_identity: Optional[SerializedResourceIdentity]
    hash: str
    size: int
    modified_time: datetime


@force_wrapp_thread
async def wrapp_capture_file_observation(
    url: str,
    relative_path: str,
    *,
    expected_hash: Optional[str] = None,
    expected_size: Optional[int] = None,
) -> FileObservation:
    """Capture one observation; validate recorded content before upgrading/relocating it.

    Hash missing metadata through the captured token, not the mutable address.
    When validating an existing catalog, verify the bytes even if the backend
    supplies a hash: historical hash/identity pairs are not sufficient evidence.
    """
    result, entry = await wrapp_stat(url)
    if result != Result.OK:
        raise StorageOperationError(f"Cannot capture file observation for {url}: {result}")
    if not entry.is_regular_file or entry.size is None or entry.modified_time is None:
        raise FailedCommand(f"Cannot capture file observation for {url}: missing regular-file size/modified_time metadata")
    identity = None
    if entry.has_checkpoint:
        _, _, token = await wrapp_make_resource_identity(url, entry, relative_path)
        identity = token.to_string()
        if not identity:
            raise FailedCommand(f"Backend returned an empty resource identity for {url}")
    content_hash = entry.nucleus_hash
    if not content_hash or expected_hash is not None:
        async with TempFileManager.me().create_temp_file() as tmp:
            if identity is not None:
                result = await wrapp_download_resource_identity(identity, tmp.url, overwrite=True, expected_hash=expected_hash)
            else:
                result = await wrapp_copy_address(url, tmp.url, overwrite=True)
            if result != Result.OK:
                raise StorageOperationError(f"Cannot read captured observation for {url}: {result}")
            tmp.write_completed()
            with open(tmp.path, "rb") as stream:
                content_hash, size = calc_nucleus_hash_from_stream(stream)
            if size != entry.size or (entry.nucleus_hash and content_hash != entry.nucleus_hash):
                raise FailedCommand(f"Captured metadata does not match the bytes for {url}")
    if expected_hash is not None and content_hash != expected_hash:
        raise FailedCommand(f"Captured content hash does not match the catalog for {url}")
    if expected_size is not None and entry.size != expected_size:
        raise FailedCommand(f"Captured content size does not match the catalog for {url}")
    return FileObservation(identity, content_hash, entry.size, entry.modified_time)


@force_wrapp_thread
async def wrapp_stat_and_calculate_hash_if_missing(url: str) -> Tuple[Result, ListEntry]:
    """
    Stat the file and if necessary, download the file and create the hash locally.
    """
    Stats.get().increment(StatType.STAT)
    r, e = await Router.me().storage_for_read(url).stat(url)
    if r == Result.OK and not e.nucleus_hash:
        if wrapp_can_be_cached(url):
            r, local_file = await wrapp_get_cached_file(url)
            try:
                if r != Result.OK or local_file is None:
                    return r, e
                # Using standard file I/O here since this is reading from a cached local file
                # LocalFileStorage uses aiofiles for all its file operations
                with open(local_file.local_path(), "rb") as f:
                    nucleus_hash, size = calc_nucleus_hash_from_stream(f)
            finally:
                if local_file:
                    local_file.close()
            if size != e.size:
                raise StorageOperationError(
                    f"Unexpected size difference in local immutable file {local_file.local_path()} and list entry for corresponding url {url}: {size} != {e.size}"
                )
            e.nucleus_hash = nucleus_hash
        else:
            r, content = await wrapp_read_file_content(url)
            if r != Result.OK:
                return r, e
            nucleus_hash, size = calc_nucleus_hash_from_stream(io.BytesIO(content))
            if size != e.size:
                raise StorageOperationError(
                    f"Unexpected size difference in local file {url} and list entry for the same file: {size} != {e.size}"
                )
            e.nucleus_hash = nucleus_hash
    return r, e


@async_for_in_wrapp_thread
async def wrapp_list_directory(url: str) -> AsyncGenerator[ListEntry, bool]:
    Stats.get().increment(StatType.LIST_ASYNC)
    r = Router.me().storage_for_read(url)
    async for e in r.list_directory(url):
        yield e


@sync_force_wrapp_thread
def wrapp_list_file_tree_supported(url: str) -> bool:
    r = Router.me().storage_for_read(url)
    return r.supports_list_file_tree()


@sync_force_wrapp_thread
def wrapp_list_directory_supported(url: str) -> bool:
    return Router.me().storage_for_read(url).supports_list_directory()


@async_for_in_wrapp_thread
async def wrapp_list_file_tree(url: str) -> AsyncGenerator[ListEntry, bool]:
    r = Router.me().storage_for_read(url)
    if r.supports_list_file_tree():
        Stats.get().increment(StatType.LIST_ASYNC)
        async for e in r.list_file_tree(url):
            yield e
    else:
        raise NotImplementedError


@force_wrapp_thread
async def wrapp_list_checkpoints(url: str) -> Tuple[Result, List[ListEntry]]:
    Stats.get().increment(StatType.LIST_CHECKPOINTS_ASYNC)
    result, checkpoints = await Router.me().storage_for_read(url).list_checkpoints(url)
    Stats.get().increment(StatType.CHECKPOINTS, len(checkpoints))
    return result, checkpoints


@force_wrapp_thread
async def wrapp_copy_address(
    src_url: ResourceAddress,
    dst_url: ResourceAddress,
    overwrite: bool = False,
    message: str = "",
) -> Result:
    """Copy the bytes currently addressed by ``src_url``."""
    Stats.get().increment(StatType.COPY_ASYNC)
    # Route both URLs once and keep the backend *instances*: two different Storage
    # API services share StorageType.StorageAPI, so we must compare instances (not
    # just types) to decide whether a copy is same-storage.
    read_storage, read_storage_type, write_storage, write_storage_type = Router.me().storages_for_address_copy(src_url, dst_url)

    # Case 1 - same storage copy (same backend *instance*).
    # A same-instance copy may use the backend's server-side copy(). A
    # cross-instance copy (e.g. between two Storage API services) must NOT take
    # this path: the source service's server-side copy() resolves the
    # destination against its own endpoint and would write to a same-named path
    # on the wrong server. Such copies fall through to Case 4 (download+upload).
    if read_storage_type == write_storage_type and read_storage is write_storage:
        try:
            return await read_storage.copy_address(src_url, dst_url, overwrite, message)
        except NotImplementedError:
            pass  # Fall through to Case 4 (download to temp file, upload from temp file)

    # Case 2 - upload from local disk
    if read_storage_type == StorageType.Local:
        Stats.get().increment(StatType.WRITE_FILE_ASYNC)
        return await write_storage.upload_file(src_url, dst_url, overwrite, message)

    # Case 3 - download to local disk
    if write_storage_type == StorageType.Local:
        # This is a download operation
        Stats.get().increment(StatType.READ_FILE_ASYNC)
        return await read_storage.download_file(src_url, dst_url, overwrite)

    # Case 4 - client needs to download and then upload
    # Ok, this is a real cross storage operation. As long as we cannot stream data, we need to download and then upload
    if not overwrite:
        stat_result, entry = await write_storage.stat(dst_url)
        if stat_result == Result.OK:
            return Result.ERROR_ALREADY_EXISTS

    async with TempFileManager.me().create_temp_file() as tmp:
        # Download from source to temp file
        Stats.get().increment(StatType.READ_FILE_ASYNC)
        result = await read_storage.download_file(src_url, tmp.url, overwrite=True)
        if result != Result.OK:
            return result
        tmp.write_completed()
        # Upload from temp file to destination
        Stats.get().increment(StatType.WRITE_FILE_ASYNC)
        return await write_storage.upload_file(tmp.url, dst_url, overwrite, message)


@force_wrapp_thread
async def wrapp_copy_resource_identity(
    resource_identity: SerializedResourceIdentity,
    dst_url: ResourceAddress,
    overwrite: bool = False,
    message: str = "",
    *,
    expected_hash: Optional[str] = None,
) -> Result:
    """Copy exact bytes selected by a backend-issued resource identity."""
    Stats.get().increment(StatType.COPY_ASYNC)
    read_storage, read_storage_type, write_storage, write_storage_type = Router.me().storages_for_resource_identity_copy(
        resource_identity, dst_url
    )

    if read_storage_type == write_storage_type and read_storage is write_storage:
        try:
            result = await read_storage.copy_resource_identity(
                resource_identity,
                dst_url,
                overwrite,
                message,
            )
            if result != Result.ERROR_NOT_SUPPORTED:
                return result
        except NotImplementedError:
            pass

    if not overwrite:
        stat_result, _ = await write_storage.stat(dst_url)
        if stat_result == Result.OK:
            return Result.ERROR_ALREADY_EXISTS
        if stat_result != Result.ERROR_NOT_FOUND:
            return stat_result

    async def _download_identity(download_target: ResourceAddress, download_overwrite: bool) -> Result:
        if expected_hash is None:
            return await read_storage.download_resource_identity(
                resource_identity,
                download_target,
                overwrite=download_overwrite,
            )
        return await read_storage.download_resource_identity(
            resource_identity,
            download_target,
            overwrite=download_overwrite,
            expected_hash=expected_hash,
        )

    if write_storage_type == StorageType.Local:
        Stats.get().increment(StatType.READ_FILE_ASYNC)
        return await _download_identity(dst_url, overwrite)

    # Cross-backend identity copies are materialized through a temporary file.
    # This keeps the exact-version read streaming and avoids retaining the
    # complete object in memory.
    async with TempFileManager.me().create_temp_file() as tmp:
        Stats.get().increment(StatType.READ_FILE_ASYNC)
        result = await _download_identity(ResourceAddress(tmp.url), True)
        if result != Result.OK:
            return result
        tmp.write_completed()
        Stats.get().increment(StatType.WRITE_FILE_ASYNC)
        return await write_storage.upload_file(tmp.url, dst_url, overwrite, message)


@force_wrapp_thread
async def wrapp_copy_address_optimized(
    context: CommandParameters,
    copy_source: ResourceAddress,
    copy_target: ResourceAddress,
    hash: Optional[str],
    overwrite: bool,
    force_upload: bool,
) -> Result:
    target_storage = Router.me().storage_for_write(copy_target)
    if (
        not wrapp_is_source_and_destination_same_storage(copy_source, copy_target)
        and (hash is not None and hash != "")
        and not force_upload
        and target_storage.supports_create_with_hash(copy_target, hash)
    ):
        # This is a cross server or disk to server copy - try to create the file with a hash first, which would save us the upload
        if context.dry_run:
            logger.info(f"Dry run: Copying {copy_source} to {copy_target} with hash")
            return Result.OK
        else:
            Stats.get().increment(StatType.CREATE_WITH_HASH_ASYNC)
            result = await target_storage.create_with_hash(copy_target, hash, overwrite)
            if result == Result.OK:
                # That worked, we're done!
                if context.verbose:
                    logger.info(f"Created {copy_target} from hash")
                return result
            elif result != Result.ERROR:
                raise StorageOperationError(f"Failed to create with hash on URL {copy_target} with hash {hash}: {result}")

    if context.dry_run:
        logger.info(f"Dry run: Copying {copy_source} to {copy_target}")
        return Result.OK
    else:
        # From here on, regular code
        result = await wrapp_copy_address(copy_source, copy_target, overwrite=overwrite)
        if context.verbose:
            if result == Result.OK:
                logger.info(f"Copied {copy_source} to {copy_target} with result {result}")
            else:
                logger.error(f"Copied {copy_source} to {copy_target} with result {result}")
        return result


@force_wrapp_thread
async def wrapp_copy_resource_identity_optimized(
    context: CommandParameters,
    resource_identity: SerializedResourceIdentity,
    copy_target: ResourceAddress,
    hash: Optional[str],
    overwrite: bool,
    force_upload: bool,
    *,
    source_description: Optional[str] = None,
) -> Result:
    """Copy version-locked catalog bytes, using create-with-hash when possible."""
    source_description = source_description or resource_identity
    router = Router.me()
    source_storage, source_storage_type, target_storage, target_storage_type = router.storages_for_resource_identity_copy(
        resource_identity, copy_target
    )
    same_storage = _is_resource_identity_copy_same_storage(
        resource_identity,
        copy_target,
        source_storage,
        source_storage_type,
        target_storage,
        target_storage_type,
    )
    if not same_storage and hash and not force_upload and target_storage.supports_create_with_hash(copy_target, hash):
        if context.dry_run:
            logger.info(f"Dry run: Copying {source_description} to {copy_target} with hash")
            return Result.OK
        Stats.get().increment(StatType.CREATE_WITH_HASH_ASYNC)
        result = await target_storage.create_with_hash(copy_target, hash, overwrite)
        if result == Result.OK:
            if context.verbose:
                logger.info(f"Created {copy_target} from hash")
            return result
        if result != Result.ERROR:
            raise StorageOperationError(f"Failed to create with hash on URL {copy_target} with hash {hash}: {result}")

    if context.dry_run:
        logger.info(f"Dry run: Copying {source_description} to {copy_target}")
        return Result.OK
    result = await wrapp_copy_resource_identity(
        resource_identity,
        copy_target,
        overwrite=overwrite,
        expected_hash=hash,
    )
    if context.verbose:
        if result == Result.OK:
            logger.info(f"Copied {source_description} to {copy_target} with result {result}")
        else:
            logger.error(f"Copied {source_description} to {copy_target} with result {result}")
    return result


@force_wrapp_thread
async def wrapp_url_exists(url: str) -> bool:
    if not Router.me().can_handle_url(url):
        raise StorageOperationError(f"No backend configured to serve {url}")
    result, item = await wrapp_stat(url)
    if result == Result.OK:
        return True
    if result == Result.ERROR_NOT_FOUND:
        return False
    raise StorageOperationError(f"storage error trying to stat {url}: {result}")


@force_wrapp_thread
async def wrapp_url_is_folder(url: str) -> bool:
    result, is_folder = await Router.me().storage_for_read(url).is_folder(url)
    if result == Result.OK:
        return is_folder
    elif result in [Result.ERROR_NOT_FOUND, Result.ERROR_WRONG_TYPE]:
        return False
    else:
        raise RuntimeError(f"Can't determine if {url} is a folder, received {result}")


async def _wrapp_read_file_content_async(file_url: str) -> Tuple[Result, bytes]:
    result, content = await Router.me().storage_for_read(file_url).read_file_content(file_url)
    return result, content


@force_wrapp_thread
async def wrapp_urls_are_equivalent(url1: str, url2: str) -> bool:
    normalized1 = await Router.me().storage_for_read(url1).normalize(url1)
    normalized2 = await Router.me().storage_for_read(url2).normalize(url2)
    return normalized1 == normalized2


@force_wrapp_thread
async def wrapp_read_file_content(file_url: str) -> Tuple[Result, bytes]:
    Stats.get().increment(StatType.READ_FILE)
    return await _wrapp_read_file_content_async(file_url)


@force_wrapp_thread
async def wrapp_read_resource_identity(
    resource_identity: SerializedResourceIdentity,
    *,
    expected_hash: Optional[str] = None,
) -> Tuple[Result, bytes]:
    """Read a backend-issued identity through its owning backend.

    The identity selects its backend directly.
    """
    Stats.get().increment(StatType.READ_FILE)
    router = Router.me()
    storage = router.storage_for_resource_identity(resource_identity)
    if expected_hash is None:
        return await storage.read_resource_identity(resource_identity)
    return await storage.read_resource_identity(resource_identity, expected_hash=expected_hash)


@force_wrapp_thread
async def wrapp_download_resource_identity(
    resource_identity: SerializedResourceIdentity,
    dst_url: ResourceAddress,
    *,
    overwrite: bool = False,
    expected_hash: Optional[str] = None,
) -> Result:
    """Stream a backend-issued identity to a local file."""
    Stats.get().increment(StatType.READ_FILE_ASYNC)
    storage = Router.me().storage_for_resource_identity(resource_identity)
    if expected_hash is None:
        return await storage.download_resource_identity(resource_identity, dst_url, overwrite)
    return await storage.download_resource_identity(resource_identity, dst_url, overwrite, expected_hash=expected_hash)


@force_wrapp_thread
async def _materialize_local_file(
    stack: AsyncExitStack,
    address: ResourceAddress,
    resource_identity: Optional[SerializedResourceIdentity],
    expected_hash: Optional[str],
) -> LocalReadOnlyFile:
    router = Router.me()
    backend = router.storage_for_resource_identity(resource_identity) if resource_identity is not None else router.storage_for_read(address)
    # Reserve staging capacity outside backend throttlers, including when a
    # backend ultimately borrows a local/Hub file instead of downloading.
    tmp = await stack.enter_async_context(TempFileManager.me().create_temp_file())
    Stats.get().increment(StatType.READ_FILE_ASYNC)
    result, local_file = await backend.materialize(address, tmp.path, resource_identity=resource_identity, expected_hash=expected_hash)
    if local_file is not None:
        stack.callback(local_file.close)
    if result != Result.OK or local_file is None:
        source_description = f"resource identity for '{address}'" if resource_identity is not None else f"'{address}'"
        raise StorageOperationError(f"Failed to materialize {source_description}: {result}", result)
    tmp.write_completed()
    return local_file


@force_wrapp_thread
async def _release_materialized_file(stack: AsyncExitStack) -> None:
    await stack.aclose()


@asynccontextmanager
async def wrapp_materialize(
    address: ResourceAddress,
    *,
    resource_identity: Optional[SerializedResourceIdentity] = None,
    expected_hash: Optional[str] = None,
) -> AsyncIterator[LocalReadOnlyFile]:
    """Lease a local file selected by an identity, or by an ordinary address.

    An explicit identity selects the backend and takes precedence over the
    address. The utility layer owns staging capacity, accounting, and deletion.
    It reserves staging before backend throttling to preserve TempFileManager's
    lock ordering. A borrowed local/cache file leaves the staging file empty.

    Backend selection, acquisition, and release run on the WRAPP thread.
    The lease remains valid until context exit, including while a synchronous
    consumer reads it. Failed acquisition, cancellation, and consumer errors
    close cache handles and release/delete owned staging files.
    """
    stack = AsyncExitStack()
    try:
        local_file = await _materialize_local_file(stack, address, resource_identity, expected_hash)
        yield local_file
    finally:
        await _release_materialized_file(stack)


def wrapp_resource_identity_for_manifest(
    resource_identity: SerializedResourceIdentity,
    *,
    expected_hash: Optional[str] = None,
) -> SerializedResourceIdentity:
    """Project a catalog identity into the identity accepted by manifests."""
    try:
        storage = Router.me().storage_for_resource_identity(resource_identity)
    except (RuntimeError, StorageOperationError):
        # Preserve identities from unavailable/unknown backends. This matches
        # the historical manifest behavior and avoids requiring every source
        # backend merely to copy an already-recorded identity.
        return resource_identity
    return storage.resource_identity_for_manifest(resource_identity, expected_hash=expected_hash)


@force_wrapp_thread
async def wrapp_read_text_file(file_url: str) -> str:
    Stats.get().increment(StatType.READ_FILE_ASYNC)
    result, content = await _wrapp_read_file_content_async(file_url)
    if result != Result.OK:
        raise StorageOperationError(f"Error to read file {file_url}: {result}")
    return content.decode("utf-8")


@force_wrapp_thread
async def wrapp_get_cached_file(file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
    Stats.get().increment(StatType.GET_LOCAL_FILE_ASYNC)
    result, cache_file = await Router.me().storage_for_read(file_url).get_cached_file_async(file_url, download)
    return result, cache_file


@force_wrapp_thread
async def wrapp_write_file_content(context: CommandParameters, file_url: str, content: str, comment: str = "") -> Result:
    return await wrapp_write_file_bytes(context, file_url, bytes(content, "utf-8"), comment)


@force_wrapp_thread
async def wrapp_write_file_bytes(context: CommandParameters, file_url: str, content: bytes, comment: str = "") -> Result:
    storage = Router.me().storage_for_write(file_url)
    if context.dry_run:
        logger.info(f"Dry run: Writing to file {file_url}")
        return Result.OK
    else:
        Stats.get().increment(StatType.WRITE_FILE_ASYNC)
        return await storage.write_file(file_url, content, comment)


@force_wrapp_thread
async def wrapp_delete_item(context: CommandParameters, url: str) -> Result:
    storage = Router.me().storage_for_write(url)
    if context.dry_run:
        logger.info(f"Dry run: deleting {url}")
        return Result.OK
    else:
        if context.verbose:
            logger.info(f"Deleting {url}")
        Stats.get().increment(StatType.DELETE_SINGLE_ASYNC)
        return await storage.delete_item(url)


@force_wrapp_thread
async def wrapp_delete_folder_tree(context: CommandParameters, url: str, task_node: SchedulerTaskGroupNode) -> Result:
    storage = Router.me().storage_for_write(url)
    if context.dry_run:
        logger.info(f"Dry run: deleting {url}")
        return Result.OK
    else:
        if context.verbose:
            logger.info(f"Deleting {url}")
        Stats.get().increment(StatType.DELETE_ASYNC)
        return await storage.delete_tree(url, task_node)


@sync_force_wrapp_thread
def wrapp_can_be_cached(url: str) -> bool:
    return Router.me().storage_for_read(url).can_be_cached(url)


@sync_force_wrapp_thread
def wrapp_is_case_sensitive(url: str) -> bool:
    return Router.me().storage_for_read(url).is_case_sensitive(url)


@force_wrapp_thread
async def wrapp_get_server_info(url: str) -> Tuple[Result, ServerInfo]:
    Stats.get().increment(StatType.GET_SERVER_INFO_ASYNC)
    return await Router.me().storage_for_read(url).get_server_info(url)


g_wrapp_initializer_lock = threading.Lock()


@assert_not_wrapp_thread
def wrapp_initialize(wrapp_version: str, debug_level: bool, throttling_params: Optional[ThrottlingParameters] = None) -> None:
    """
    Initialize WRAPP with all storage backends.

    Sequence:

    1. ``Router.initialize()`` — instantiates every enabled storage backend
       (S3, Azure, GCS, Hugging Face, Nucleus, Local, Storage API). Backend availability is
       controlled by environment variables such as ``WRAPP_ENABLE_STORAGE_API``,
       ``WRAPP_DISABLE_AWS``, ``WRAPP_DISABLE_AZURE``, etc.
    2. ``WrappEventLoopThread.initialize()`` — starts the dedicated async event
       loop thread used for all I/O operations.
    3. **Storage API eager initialization** — if the Storage API backend was
       created, calls ``eager_initialize()`` to discover the gRPC endpoint,
       negotiate protocol versions, and populate URL routes/TLAs. On failure
       the error is logged as a warning and routing falls back to lazy
       initialization on the first real storage operation.
    4. ``TempFileManager.initialize()`` — sets up the temporary file pool for
       cross-backend copy operations.

    :param wrapp_version: Version string for WRAPP.
    :param debug_level: Enable debug level logging.
    :param throttling_params: Optional throttling parameters for throttling control.
        If not provided, uses default ThrottlingParameters().

    :raises RuntimeError: If there is any issue during the initialization.
    """
    if throttling_params is None:
        throttling_params = ThrottlingParameters()

    global g_wrapp_initializer_lock
    with g_wrapp_initializer_lock:
        Router.initialize(wrapp_version, debug_level, throttling_params)
        assert Router.is_initialized()
        WrappEventLoopThread.initialize()
        assert WrappEventLoopThread.is_initialized()

        # Eagerly connect every configured Storage API service and warn about
        # any overlapping route scopes between them. Per-instance failures are
        # isolated inside the Router and fall back to lazy initialization.
        Router.me().eager_initialize_storage_apis()

        TempFileManager.initialize()


@assert_not_wrapp_thread
def wrapp_shutdown(exc_type=None, exc_val=None, exc_tb=None, shutdown_client_lib=True, *, stop_timeout: Optional[float] = None) -> None:
    """
    Shut down WRAPP with all storage backends.

    The exception parameters can be passed on using ``wrapp_shutdown(*sys.exc_info())``
    if the function is called within code handling exceptions.

    :param stop_timeout: Timeout in seconds to wait for the event loop thread to stop.
        ``None`` waits indefinitely. If a finite timeout
        is given and the thread does not stop in time, raises RuntimeError.
    """
    if TempFileManager.is_initialized():
        TempFileManager.me().shutdown()

    if Router.is_initialized():
        Router.me().shutdown(exc_type, exc_val, exc_tb, shutdown_client_lib)

    if WrappEventLoopThread.is_initialized():
        WrappEventLoopThread.stop(timeout=stop_timeout)


@force_wrapp_thread
async def wrapp_supports_tags(url: str):
    return g_tagging_client_available and Router.me().storage_for_read(url).supports_tags(url)


@sync_force_wrapp_thread
def wrapp_set_auth_info(auth_info: List[StorageAuthInfo]):
    """
    Set authentication information for all storages.

    interactive_fallback: if True, storages might pop up user dialogs to
        authenticate the user if no authentication information is available
        or if the authentication with the given information fails.
        If False, user interaction will never be required.
    """
    auth_info_for_server: Dict[StorageInterface, List[StorageAuthInfo]] = {}
    for a in auth_info:
        if not a.server_url:
            raise FailedCommand(f"Invalid authentication information: no server URL")
        storage = Router.me().storage_for_auth(a.server_url)
        auth_info_for_server.setdefault(storage, []).append(a)
    for storage, auth_info in auth_info_for_server.items():
        storage.set_auth_info(auth_info)


@sync_force_wrapp_thread
def wrapp_add_auth_info(auth_info: StorageAuthInfo):
    Router.me().storage_for_auth(auth_info.server_url).add_auth_info(auth_info)


@sync_force_wrapp_thread
def wrapp_parse_auth_param(server_url: str, auth_param: str) -> StorageAuthInfo:
    return Router.me().storage_for_auth(server_url).parse_auth_param(auth_param)


@sync_force_wrapp_thread
def wrapp_get_last_connection_status(server_url: str) -> ConnectionStatus:
    return Router.me().storage_for_auth(server_url).get_last_connection_status(server_url)


def _is_http_url(url: str) -> bool:
    """Check if URL is an HTTP or HTTPS URL."""
    return url.startswith("http://") or url.startswith("https://")


def _normalize_http_url(url: str) -> str:
    """Normalize an HTTP/HTTPS URL for comparison.

    Performs the following normalizations:
    - Path normalization (resolves .., removes trailing slashes)
    - Lowercases scheme and host for case-insensitive comparison
    - Removes default ports (80 for http, 443 for https)

    This matches what the clientlib would do (nothing) but adds proper
    normalization for reliable URL comparison.
    """
    # First use the standard normalize_url for path normalization
    normalized = normalize_url(url)

    # Parse and apply HTTP-specific normalization
    parsed = urlsplit(normalized)

    # Lowercase scheme and host
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Remove default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    return urlunsplit((scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _try_normalize_s3_url(url: str) -> Optional[str]:
    """Try to normalize an S3 URL to s3://bucket/key format.

    S3 URLs can come in many formats:
    - s3://bucket/key
    - https://bucket.s3.region.amazonaws.com/key
    - https://s3.region.amazonaws.com/bucket/key

    Returns the normalized s3:// URL if the input is an S3 URL, None otherwise.
    """
    try:
        bucket, key, _ = parse_s3_bucket_key(url)
        normalized = f"s3://{bucket}"
        if key:
            normalized = f"{normalized}/{key}"
        return normalized
    except ValueError:
        return None


def normalize_repo_url_for_comparison(url: str) -> str:
    """Normalize a repository URL for comparison.

    Handles all URL types (file://, s3://, https://, etc.) with special handling
    for S3 URLs which can come in many formats:
    - s3://bucket/path
    - https://bucket.s3.region.amazonaws.com/path
    - https://s3.region.amazonaws.com/bucket/path

    All S3 URL formats are normalized to s3://bucket/path for comparison.
    Non-S3 URLs are normalized using the standard normalize_url function.
    Trailing slashes are stripped for consistent comparison.

    This is a synchronous function that can be used for simple URL comparisons
    without needing the async storage backend.
    """
    s3_normalized = _try_normalize_s3_url(url)
    if s3_normalized is not None:
        return s3_normalized.rstrip("/")
    return normalize_url(url).rstrip("/")


async def _normalize_url_for_comparison(url: str) -> str:
    """Normalize a URL for comparison purposes.

    For S3 URLs (in any format), we normalize to s3://bucket/key format.
    This ensures that s3://bucket/path and https://bucket.s3.region.amazonaws.com/path
    compare equal.

    For other HTTP/HTTPS URLs, we perform standard URL normalization without requiring
    a storage backend. This allows URL comparison to work even when the
    Nucleus/clientlib backend is not available.

    For other URL types (Azure, file://, omniverse://), we use the storage
    backend's normalize function.
    """
    # First, try to normalize as an S3 URL (handles both s3:// and https://...amazonaws.com)
    s3_normalized = _try_normalize_s3_url(url)
    if s3_normalized is not None:
        return s3_normalized

    if _is_http_url(url):
        return _normalize_http_url(url)
    return await Router.me().storage_for_read(url).normalize(url)


@force_wrapp_thread
async def is_url_pointing_to_sub_url(base_url: str, full_url: str) -> bool:
    # This operation needs storage specific normalization
    normalized_base = await _normalize_url_for_comparison(base_url)
    normalized_full = await _normalize_url_for_comparison(full_url)
    return not (make_relative_url(normalized_base, normalized_full) == normalized_full)


@force_wrapp_thread
async def wrapp_normalize_url(url: str) -> str:
    # This operation needs storage specific normalization
    return await Router.me().storage_for_read(url).normalize(url)
