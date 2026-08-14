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
import sys
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Self,
    Tuple,
)
from urllib.parse import (
    quote,
    urlparse,
    urlunparse,
)

import semver

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils import storage_interface
from wrapp.utils.nucleus_hash import is_nucleus_hash
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage_interface import (
    NucleusAuthInfo,
    StorageAuthInfo,
)
from wrapp.utils.task_throttler import (
    TaskThrottler,
    ThrottlerType,
)
from wrapp.utils.utils import (
    StorageInitializationError,
    StorageMissingDependenciesError,
    StorageOperationError,
    drop_parameters_from_url,
    get_base_directory_from_url,
    is_file_url,
    is_nucleus_url,
)

logger = logging.getLogger("omni.wrapp")

try:
    import omni.client
except ImportError as e:
    # The list of dependencies should be the same as in pyproject.toml
    error_msg = f"failed to import omni.client. As of now omni.client is required for WRAPP. Required dependencies: omniverseclient>=2.53.0. Install with 'pip install ovpackage[nucleus]'. Exception: {e}"
    raise StorageMissingDependenciesError(error_msg) from e


# If this version is changed, also change the client-library version in the gitlab job test-nucleus-linux-old-client-lib
g_min_client_library_expected_version_str = "2.53.2"
g_client_library_version_check_done = False
g_client_library_was_shutdown = False


def client_lib_log_handler(thread, component, level: omni.client.LogLevel, message) -> None:
    level_map = {
        omni.client.LogLevel.VERBOSE: logging.DEBUG,
        omni.client.LogLevel.DEBUG: logging.DEBUG,
        omni.client.LogLevel.INFO: logging.DEBUG,
        omni.client.LogLevel.WARNING: logging.WARNING,
        omni.client.LogLevel.ERROR: logging.ERROR,
    }
    logger.log(level_map[level], message, extra={"component": component, "clientlib_thread": thread})


class ClientLibListEntry(storage_interface.ListEntry):
    def __init__(self, e: omni.client.ListEntry):
        self._list_entry = e
        self._calculated_hash: Optional[str] = None

    @property
    def version(self) -> str:
        return self._list_entry.version

    @property
    def relative_path(self) -> str:
        return self._list_entry.relative_path

    @property
    def nucleus_hash(self) -> Optional[str]:
        return self._list_entry.hash if self._list_entry.hash else self._calculated_hash

    @nucleus_hash.setter
    def nucleus_hash(self, value: str):
        if self.nucleus_hash is not None:
            raise AttributeError("Nucleus hash cannot be changed once set")
        self._calculated_hash = value

    @property
    def size(self) -> Optional[int]:
        return self._list_entry.size

    @property
    def modified_time(self) -> Optional[datetime]:
        return self._list_entry.modified_time

    @property
    def is_folder(self) -> bool:
        return self._list_entry.flags & omni.client.ItemFlags.CAN_HAVE_CHILDREN != 0

    @property
    def is_regular_file(self) -> bool:
        return (
            self._list_entry.flags & omni.client.ItemFlags.CAN_HAVE_CHILDREN == 0
            and self._list_entry.flags & omni.client.ItemFlags.IS_OMNI_OBJECT == 0
            and self._list_entry.flags & omni.client.ItemFlags.IS_CHANNEL == 0
        )

    @property
    def has_checkpoint(self) -> bool:
        return self._list_entry.flags & omni.client.ItemFlags.IS_CHECKPOINTED != 0


class ClientLibResourceIdentity(storage_interface.ResourceIdentity):
    def __init__(self, checkpoint_url: str):
        # TODO - should we check a checkpoint number is appended? Not sure, there were Nucleus versions which
        # allowed non-checkpointed URLs for file versions
        self._checkpoint_url = checkpoint_url

    async def read_data(self, storage) -> Tuple[storage_interface.Result, bytes]:
        # TODO How can we validate this actually works? We could store the hash in addition, and check on
        # read that the data returned matches the hash we expected
        return await storage.read_file_content(self._checkpoint_url)

    def to_string(self) -> str:
        return self._checkpoint_url


class ClientLibServerInfo(storage_interface.ServerInfo):
    def __init__(self, s: omni.client.ServerInfo):
        self._server_info = s

    def supports_create_with_hash(self, target_url: str) -> bool:
        return is_nucleus_url(target_url)

    @property
    def auth_token(self) -> str:
        return self._server_info.auth_token


class ClientLibCacheFile(storage_interface.LocalReadOnlyFile):
    _leaked_urls: List[str] = []  # for debugging/tests only

    def __init__(self, handle: int, path: str, url: str):
        self._path: str = path
        self._url: str = url
        self._handle: Optional[int] = handle

    def __del__(self):
        if self._handle is not None and sys.exc_info()[0] != KeyboardInterrupt:
            logger.warning(
                f"Cache file '{self._path}' for url '{self._url}' was not closed explicitly. Do not rely on Pythons garbage collection for releasing resources."
            )
            self.close()
            ClientLibCacheFile._leaked_urls.append(self._url)

    def local_path(self):
        if self._handle is None:
            raise StorageOperationError(f"CacheFileHandle for file {self._url}, local file {self._path} has already been closed!")
        return self._path

    def close(self):
        if self._handle is not None:
            omni.client.close_cached_file(self._handle)
            self._handle = None


class _ThrottlerHandler:
    def __init__(self: Self, throttling_params: ThrottlingParameters):
        # Extract throttling parameters
        roundtrip_jobs = throttling_params.jobs
        transfer_jobs = throttling_params.file_transfer_jobs

        # One network data throttler per server - while the local network connection might be the limiting factor, this has
        # to be solved across all backends in a different manner? And this way, we are consistent with the roundtrips.
        self._nucleus_networkdata: dict[str, TaskThrottler] = {}
        self._nucleus_networkdata_max_jobs = transfer_jobs  # was 50
        # Roundtrip is one throttler per host - technically we would have to take the stat cache into account. Not sure how to do that?
        self._nucleus_roundtrip: dict[str, TaskThrottler] = {}
        self._nucleus_roundtrip_max_jobs = roundtrip_jobs  # was 200
        self._nucleus_longrunningserverop: dict[str, TaskThrottler] = {}
        self._nucleus_longrunningserverop_max_jobs = transfer_jobs  # was 50
        # Local file system has no network data transfer on read/write - it does the equivalent of a server internal data transfer
        self._local_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "Local", roundtrip_jobs)
        self._local_datatransfer = TaskThrottler(ThrottlerType.LongRunningServerOp, "Local", transfer_jobs)
        # For http request routed here - is that still necessary?
        self._catchall_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "ClientLibOther", roundtrip_jobs)
        self._catchall_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "ClientLibOther", transfer_jobs)
        self._catchall_servercopy = TaskThrottler(ThrottlerType.LongRunningServerOp, "ClientLibOther", transfer_jobs)

    def are_scheduling_params_equivalent(self, throttling_params: ThrottlingParameters) -> bool:
        return (
            self._nucleus_networkdata_max_jobs == throttling_params.file_transfer_jobs
            and self._nucleus_roundtrip_max_jobs == throttling_params.jobs
        )

    def throttlers(self) -> list[TaskThrottler]:
        return [
            *self._nucleus_networkdata.values(),
            *self._nucleus_roundtrip.values(),
            *self._nucleus_longrunningserverop.values(),
            self._local_roundtrip,
            self._local_datatransfer,
            self._catchall_roundtrip,
            self._catchall_networkdata,
            self._catchall_servercopy,
        ]

    def __call__(self, url: str, throttler_type: ThrottlerType, *, url2: str = ""):
        if is_nucleus_url(url):
            split_url = urlparse(url)
            host = str(split_url.netloc)
            if not host:
                raise ValueError(f"No host in url '{url}'")
            if throttler_type == ThrottlerType.NetworkDataTransfer:
                return self._nucleus_networkdata.setdefault(host, TaskThrottler(throttler_type, host, self._nucleus_networkdata_max_jobs))
            elif throttler_type == ThrottlerType.ServerRoundTrip:
                return self._nucleus_roundtrip.setdefault(host, TaskThrottler(throttler_type, host, self._nucleus_roundtrip_max_jobs))
            elif throttler_type == ThrottlerType.LongRunningServerOp:
                split_url2 = urlparse(url2)
                host2 = str(split_url2.netloc)
                if not host2:
                    # Delete tree
                    return self._nucleus_longrunningserverop.setdefault(
                        host,
                        TaskThrottler(ThrottlerType.LongRunningServerOp, host, self._nucleus_longrunningserverop_max_jobs),
                    )
                if host.lower() == host2.lower():
                    # Nucleus server copy within one server. This is a roundtrip, the server does not actually need to copy any data
                    return self._nucleus_roundtrip.setdefault(
                        host, TaskThrottler(ThrottlerType.ServerRoundTrip, host, self._nucleus_roundtrip_max_jobs)
                    )
                else:
                    # Nucleus server A to server B copy. Client-library implements this as download/upload
                    return self._nucleus_networkdata.setdefault(
                        host, TaskThrottler(ThrottlerType.NetworkDataTransfer, host, self._nucleus_networkdata_max_jobs)
                    )
            else:
                raise ValueError(f"Unknown throttler type {throttler_type} for Nucleus URLs")
        elif is_file_url(url):
            if throttler_type == ThrottlerType.ServerRoundTrip:
                return self._local_roundtrip
            if throttler_type in [ThrottlerType.LongRunningServerOp, ThrottlerType.NetworkDataTransfer]:
                return self._local_datatransfer
            raise ValueError(f"Unknown throttler type {throttler_type} for local file URLs")
        else:
            if throttler_type == ThrottlerType.ServerRoundTrip:
                return self._catchall_roundtrip
            if throttler_type == ThrottlerType.NetworkDataTransfer:
                return self._catchall_networkdata
            if throttler_type == ThrottlerType.LongRunningServerOp:
                return self._catchall_servercopy
            raise ValueError(f"Unknown throttler type {throttler_type} for other URLs processed by the client-library")


class ClientLibStorage(storage_interface.StorageInterface):
    def _map_result(self, r: omni.client.Result) -> storage_interface.Result:
        try:
            return storage_interface.Result[r.name]
        except KeyError:
            return storage_interface.Result.ERROR_UNKNOWN

    def _map_list_entry(self, e: omni.client.ListEntry) -> storage_interface.ListEntry:
        return ClientLibListEntry(e)

    def _map_connection_status(self, cs: omni.client.ConnectionStatus) -> storage_interface.ConnectionStatus:
        try:
            return storage_interface.ConnectionStatus[cs.name]
        except KeyError:
            return storage_interface.ConnectionStatus.CONNECTION_STATUS_UNKNOWN

    @staticmethod
    def _encode_url_for_omniclient(url: str) -> str:
        """Percent-encode the path component of an Omniverse URL for omni.client.

        omni.client treats URL paths as percent-encoded, so literal special
        characters in path names (e.g. '%' in a folder named "Mini%20map")
        must be encoded before being passed to the client library.  Without
        this, omni.client would decode "%20" to a space and fail to find the
        actual item on the server.

        Only omniverse:// and omni:// URLs are encoded; other schemes (e.g.
        file://) are returned unchanged.  Query parameters (used for
        checkpoints) and fragments are preserved as-is.
        """
        parsed = urlparse(url)
        if parsed.scheme in ("omniverse", "omni"):
            encoded_path = quote(parsed.path, safe="/")
            return urlunparse((parsed.scheme, parsed.netloc, encoded_path, parsed.params, parsed.query, parsed.fragment))
        return url

    @classmethod
    def version_info(cls) -> str:
        hub_runs, hub_version = _is_hub_running()
        return f"Client Library {omni.client.get_version()}. Hub cache is {f'ON ({hub_version})' if hub_runs else 'OFF'}."

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_params: ThrottlingParameters):
        if g_client_library_was_shutdown:
            raise StorageInitializationError(
                "The client-library can only be initialized once. Please make sure to use Router.me().shutdown(..., shutdown_client_lib = False)"
            )
        omni.client.set_log_level(omni.client.LogLevel.DEBUG if debug_level else omni.client.LogLevel.INFO)
        omni.client.set_log_callback(client_lib_log_handler)
        omni.client.initialize()
        omni.client.set_product_info(name="wrapp", version=wrapp_version, extra=f"Python Version: {sys.version}")
        global g_client_library_version_check_done
        if not g_client_library_version_check_done:
            g_client_library_version_check_done = True
            min_expected_version_str = g_min_client_library_expected_version_str
            actual_version_str = omni.client.get_version()
            if actual_version_str != min_expected_version_str:
                min_expected_version = semver.Version.parse(min_expected_version_str)
                try:
                    actual_version = semver.Version.parse(actual_version_str)
                except ValueError as e:
                    raise StorageInitializationError(
                        f"Found client-library with version {actual_version_str} which could not be parsed as a SemVer: {e}. For local development, consider setting g_min_client_library_expected_version_str to {actual_version_str}."
                    )
                if actual_version.major != min_expected_version.major or actual_version.minor < min_expected_version.minor:
                    # Raise RuntimeError (not StorageInitializationError) so this is not caught by Router
                    # An incompatible client library version is a critical error that should not be silently handled
                    raise StorageInitializationError(
                        f"Found client-library {actual_version_str} which is incompatible to client-library {min_expected_version_str} wrapp was tested with!"
                    )
        self._authentication_store: Dict[str, NucleusAuthInfo] = {}
        self._interactive_fallback = False  # Do not allow client library to do user interaction, e.g. to open a browser window
        self._last_status: Dict[str, storage_interface.ConnectionStatus] = {}
        self._registration = omni.client.register_authentication_callback(self._authentication_callback)
        self._registration.__enter__()
        self._connection_status = omni.client.register_connection_status_callback(self._connection_status_callback)
        self._connection_status.__enter__()
        self._throttlers = _ThrottlerHandler(throttling_params)

    def backend_throttlers(self) -> list[TaskThrottler]:
        return self._throttlers.throttlers()

    def reconfigure_throttlers(self, throttling_params: ThrottlingParameters) -> bool:
        if self._throttlers.are_scheduling_params_equivalent(throttling_params):
            return True
        for t in self.backend_throttlers():
            if t.pending_tasks or t.processing_tasks:
                logger.warning(f"Cannot update throttling parameters for clientlib: Throttler {t.server_backend_name} has open tasks.")
                return False
        self._throttlers = _ThrottlerHandler(throttling_params)
        return True

    def supports_tags(self, url: str) -> bool:
        return is_nucleus_url(url)

    def directory_semantics(self) -> storage_interface.DirectorySemantics:
        return storage_interface.DirectorySemantics.FILESYSTEM

    def supports_comments(self) -> bool:
        return True

    def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        logger.debug("De-registering client library callback")
        if self._registration:
            self._registration.__exit__(exc_type, exc_val, exc_tb)
        self._registration = None
        if self._connection_status:
            self._connection_status.__exit__(exc_type, exc_val, exc_tb)
        self._connection_status = None
        omni.client.shutdown()
        # Once the client-library has been shut down, it cannot be started again.
        # Make sure we do not try that.
        global g_client_library_was_shutdown
        g_client_library_was_shutdown = True

    @classmethod
    def register_authentication_callback(cls, callback: Callable[[str], object]) -> object:
        return omni.client.register_authentication_callback(callback)

    async def get_server_info(self, url: str) -> Tuple[storage_interface.Result, storage_interface.ServerInfo]:
        encoded = self._encode_url_for_omniclient(url)
        r, i = await self._throttlers(url, ThrottlerType.ServerRoundTrip).run(omni.client.get_server_info_async(encoded))
        return self._map_result(r), ClientLibServerInfo(i)

    async def normalize(self, url: str) -> str:
        return url

    async def make_resource_identity(
        self, file_url, item: storage_interface.ListEntry, relative_url
    ) -> Tuple[str, str, storage_interface.ResourceIdentity]:
        # This is complex, we need to enumerate the versions to find out which version (checkpoint) matches the
        # hash given via the ListEntry
        result, checkpoints = await self.list_checkpoints(file_url)
        if result != storage_interface.Result.OK:
            raise StorageOperationError(f"Failed on list_checkpoints for {file_url}: {result}")
        logger.debug(f"Listed {file_url} with {len(checkpoints)} checkpoints")
        # Find the item's version in the list of checkpoints
        checkpoint_found = None
        for checkpoint in checkpoints:
            if checkpoint.version == item.version:
                checkpoint_found = checkpoint
        if checkpoint_found is not None:
            # We might have been cataloging a checkpoint - make sure the same checkpoint is returned, and don't double
            # append it.
            source_parsed = urlparse(file_url)
            if source_parsed.query:
                # Don't append the new checkpoint
                new_absolute_source_url = file_url
                new_relative_source_url = drop_parameters_from_url(relative_url)
            else:
                # Append the  checkpoint found
                new_absolute_source_url = file_url + "?" + checkpoint_found.relative_path
                new_relative_source_url = relative_url
            return new_absolute_source_url, new_relative_source_url, ClientLibResourceIdentity(new_absolute_source_url)
        else:
            if len(checkpoints) > 0 and checkpoints[-1].nucleus_hash == item.nucleus_hash:
                clean_url = drop_parameters_from_url(file_url)
                new_absolute_source_url = clean_url + "?" + checkpoints[-1].relative_path
                return new_absolute_source_url, relative_url, ClientLibResourceIdentity(new_absolute_source_url)
            else:
                raise StorageOperationError(f"Error: Checkpoint not found for {file_url} item {item} in {checkpoints}")

    async def stat(self, url: str) -> Tuple[storage_interface.Result, Optional[storage_interface.ListEntry]]:
        encoded = self._encode_url_for_omniclient(url)
        result, item = await self._throttlers(url, ThrottlerType.ServerRoundTrip).run(
            omni.client.stat_async(encoded, omni.client.ListIncludeOption.NO_DELETED_FILES)
        )
        if result == omni.client.Result.ERROR_WRONG_TYPE:
            # This can happen with client library 2.43, but only on Linux for a local file path. Revert it to the same error message of 2.42 and older
            result = omni.client.Result.ERROR_NOT_FOUND
        return self._map_result(result), self._map_list_entry(item)

    async def is_folder(self, url: str) -> Tuple[storage_interface.Result, bool]:
        encoded = self._encode_url_for_omniclient(url)
        result, item = await self._throttlers(url, ThrottlerType.ServerRoundTrip).run(
            omni.client.stat_async(encoded, omni.client.ListIncludeOption.NO_DELETED_FILES)
        )
        if result == omni.client.Result.ERROR_WRONG_TYPE:
            # This can happen with client library 2.43, but only on Linux for a local file path. Revert it to the same error message of 2.42 and older
            return storage_interface.Result.ERROR_NOT_FOUND, False
        elif result == omni.client.Result.OK:
            entry = self._map_list_entry(item)
            return storage_interface.Result.OK, entry.is_folder
        else:
            return self._map_result(result), False

    async def list_directory(self, url: str) -> AsyncGenerator[storage_interface.ListEntry, None]:
        encoded = self._encode_url_for_omniclient(url)
        r, entries = await self._throttlers(url, ThrottlerType.ServerRoundTrip).run(
            omni.client.list_async(encoded, omni.client.ListIncludeOption.NO_DELETED_FILES)
        )
        if r == omni.client.Result.OK:
            for e in entries:
                yield self._map_list_entry(e)
                await asyncio.sleep(0)  # Be a nice citizen of async world - if we have a really long list, we never yield
        else:
            r2 = self._map_result(r)
            raise StorageOperationError(f"Failed to iterate over directory '{url}': {r2}", r2)

    async def list_checkpoints(self, url: str) -> Tuple[storage_interface.Result, List[storage_interface.ListEntry]]:
        Stats.get().increment(StatType.LIST_CHECKPOINTS_ASYNC)
        encoded = self._encode_url_for_omniclient(url)
        result, checkpoints = await self._throttlers(url, ThrottlerType.ServerRoundTrip).run(omni.client.list_checkpoints_async(encoded))
        Stats.get().increment(StatType.CHECKPOINTS, len(checkpoints))
        return self._map_result(result), [self._map_list_entry(e) for e in checkpoints]

    async def copy(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> storage_interface.Result:
        encoded_src = self._encode_url_for_omniclient(src_url)
        encoded_dst = self._encode_url_for_omniclient(dst_url)
        # This additional stat is required because of the HUB-808 bug, which leads to copy not failing during async copy operations when target exists
        result, item = await self._throttlers(dst_url, ThrottlerType.ServerRoundTrip).run(omni.client.stat_async(encoded_dst))
        if result != omni.client.Result.OK and result != omni.client.Result.ERROR_NOT_FOUND:
            logger.debug(f"Error stating target file {dst_url}: {result}")
            return self._map_result(result)
        if result != omni.client.Result.ERROR_NOT_FOUND and not overwrite:
            logger.debug(f"Conflict: Target file {dst_url} already exists")
            return storage_interface.Result.ERROR_ALREADY_EXISTS

        r = await self._throttlers(src_url, ThrottlerType.LongRunningServerOp, url2=dst_url).run(
            omni.client.copy_async(
                encoded_src,
                encoded_dst,
                omni.client.CopyBehavior.OVERWRITE if overwrite else omni.client.CopyBehavior.ERROR_IF_EXISTS,
                message,
            )
        )
        # Additional code required due to HUB-947 bug - for Nucleus the client-library returns "ERROR_NOT_FOUND" if it
        # cannot create the target folder due to permissions. The assumption here is that the only way
        # the target folder does not exist after the copy call is that the client was not able to create it.
        # This is obviously not quite true, but hopefully good enough until the bug in the client-lib has been fixed.
        if r == omni.client.Result.ERROR_NOT_FOUND and is_nucleus_url(dst_url):
            parent_path = get_base_directory_from_url(dst_url)
            stat_result, _ = await self.stat(parent_path)
            if stat_result == storage_interface.Result.ERROR_NOT_FOUND:
                return storage_interface.Result.ERROR_ACCESS_DENIED
        return self._map_result(r)

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        return is_nucleus_url(target_url) and is_nucleus_hash(hash)

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> storage_interface.Result:
        encoded = self._encode_url_for_omniclient(copy_target)
        result, version, output_hash = await self._throttlers(copy_target, ThrottlerType.ServerRoundTrip).run(
            omni.client.create_with_hash_async(encoded, storage_hash, overwrite)
        )
        return self._map_result(result)

    async def read_file_content(self, file_url: str) -> Tuple[storage_interface.Result, bytes]:
        encoded = self._encode_url_for_omniclient(file_url)
        result, version, content = await self._throttlers(file_url, ThrottlerType.NetworkDataTransfer).run(
            omni.client.read_file_async(encoded)
        )
        return self._map_result(result), bytes(memoryview(content))

    async def get_cached_file_async(
        self, file_url: str, download: bool = True
    ) -> Tuple[storage_interface.Result, Optional[storage_interface.LocalReadOnlyFile]]:
        if not self.can_be_cached(file_url):
            raise StorageOperationError(
                "get_local_file can only be called if Hub is running. Program logic error or setup problem. File {file_url}"
            )
        hub_runs, _ = _is_hub_running()
        encoded = self._encode_url_for_omniclient(file_url)
        # TODO: not quite true - if the file is already cached, this is not a network data transfer
        result, handle, path = await self._throttlers(file_url, ThrottlerType.NetworkDataTransfer).run(
            omni.client.open_cached_file_async(encoded, download)
        )
        cache_file = ClientLibCacheFile(handle, path, file_url) if result == omni.client.Result.OK else None
        return self._map_result(result), cache_file

    async def write_file(self, file_url: str, content: bytes, comment: str) -> storage_interface.Result:
        encoded = self._encode_url_for_omniclient(file_url)
        return self._map_result(
            await self._throttlers(file_url, ThrottlerType.NetworkDataTransfer).run(omni.client.write_file_async(encoded, content, comment))
        )

    async def delete_item(self, url: str) -> storage_interface.Result:
        encoded = self._encode_url_for_omniclient(url)
        return self._map_result(await self._throttlers(url, ThrottlerType.ServerRoundTrip).run(omni.client.delete_single_async(encoded)))

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> storage_interface.Result:
        encoded = self._encode_url_for_omniclient(url)
        return self._map_result(await self._throttlers(url, ThrottlerType.LongRunningServerOp).run(omni.client.delete_async(encoded)))

    def can_be_cached(self, url: str) -> bool:
        return not is_file_url(url) and _is_hub_running()[0]

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> storage_interface.Result:
        encoded_src = self._encode_url_for_omniclient(src_url)
        encoded_dst = self._encode_url_for_omniclient(dst_url)
        return self._map_result(
            await self._throttlers(dst_url, ThrottlerType.NetworkDataTransfer).run(
                omni.client.copy_async(
                    encoded_src,
                    encoded_dst,
                    omni.client.CopyBehavior.OVERWRITE if overwrite else omni.client.CopyBehavior.ERROR_IF_EXISTS,
                    message if message else "",
                )
            )
        )

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> storage_interface.Result:
        encoded_src = self._encode_url_for_omniclient(src_url)
        encoded_dst = self._encode_url_for_omniclient(dst_url)
        return self._map_result(
            await self._throttlers(src_url, ThrottlerType.NetworkDataTransfer).run(
                omni.client.copy_async(
                    encoded_src, encoded_dst, omni.client.CopyBehavior.OVERWRITE if overwrite else omni.client.CopyBehavior.ERROR_IF_EXISTS
                )
            )
        )

    def enable_auth_interactive_fallback(self, interactive_fallback: bool):
        self._interactive_fallback = interactive_fallback

    def set_auth_info(self, auth_info: List[storage_interface.StorageAuthInfo]):
        self._authentication_store.clear()
        if auth_info:
            for a in auth_info:
                self.add_auth_info(a)

    def add_auth_info(self, auth_info: storage_interface.StorageAuthInfo):
        if not isinstance(auth_info.auth_data, NucleusAuthInfo):
            raise StorageOperationError(
                f"Incompatible authentication information for server {auth_info.server_url}: expected NucleusAuthInfo, got {auth_info.auth_data.__class__}"
            )
        logger.info(f"Adding auth information for client-library server {auth_info.server_url}")
        self._authentication_store[auth_info.server_url] = auth_info.auth_data

    def parse_auth_param(self, auth_param: str) -> storage_interface.StorageAuthInfo:
        try:
            server, user, password = auth_param.split(",")
        except ValueError as e:
            raise StorageOperationError(
                f"Authentication parameter for Nucleus Servers needs to be of the form server_url, username, password!"
            )
        try:
            omni_url = urlparse(server)
        except ValueError as e:
            raise StorageOperationError(f"Failed to parse server URL {server} for authentication to Nucleus: {e}")
        if omni_url.scheme not in ["omni", "omniverse"]:
            raise StorageOperationError(
                f"You can only specify Nucleus authentication information for omniverse connections (scheme 'omni:' or 'omniverse:'), not {omni_url.scheme}!"
            )
        server_key = f"{omni_url.scheme}://{omni_url.netloc}"
        return StorageAuthInfo.nucleus(server_url=server_key, username=user, password_or_token=password)

    def get_last_connection_status(self, server_url: str) -> Optional[storage_interface.ConnectionStatus]:
        if server_url in self._last_status:
            return self._last_status[server_url]
        else:
            return None

    def _authentication_callback(self, server: str) -> Any:
        # Check if we have already logged into that server
        logger.info(f"Got request for authentication for server {server}")
        if server in self._authentication_store:
            logger.info(f"Using provided authentication information for {server}")
            return self._authentication_store[server].username, self._authentication_store[server].password_or_token
        # client-library does not go into the interactive fallback if OMNI_USERNAME or OMNI_USER is set.
        elif (not self._interactive_fallback) and os.environ.get("OMNI_USERNAME", "") == "" and os.environ.get("OMNI_USER", "") == "":
            if len(self._authentication_store.keys()) > 0:
                logger.error(
                    f"No auth information provided for server {server}, only for {', '.join(self._authentication_store.keys())}, aborting authentication!"
                )
            else:
                logger.error(f"No auth information provided for server {server}, aborting authentication!")
            return omni.client.AUTH_ABORT
        else:
            logger.info(f"No auth information provided for server {server}, falling back to default authentication method!")
            return False

    def _connection_status_callback(self, server_url: str, status: omni.client.ConnectionStatus) -> Any:
        if status in [
            omni.client.ConnectionStatus.CONNECTED,
            omni.client.ConnectionStatus.CONNECTING,
            omni.client.ConnectionStatus.DISCONNECTED,
        ]:
            logger.debug(f"New connection status for {server_url} is {status}")
        elif status in [
            omni.client.ConnectionStatus.AUTH_FAILED,
            omni.client.ConnectionStatus.AUTH_CANCELLED,
            omni.client.ConnectionStatus.AUTH_ERROR,
        ]:
            # Normally, we'd throw an exception here, but the callback is on the C thread and that will be handled by C, not by Python
            logger.error(f"Authentication failed for {server_url} with {status}")
        else:
            logger.warning(f"New connection status for {server_url} is {status}")
        self._last_status[server_url] = self._map_connection_status(status)


_hub_version: Optional[str] = None


def _get_hub_version() -> Tuple[omni.client.Result, Optional[str]]:
    global _hub_version
    if _hub_version:
        return omni.client.Result.OK, _hub_version
    Stats.get().increment(StatType.GET_HUB_VERSION)
    r, hub_version = omni.client.get_hub_version()
    if r == omni.client.Result.OK:
        _hub_version = hub_version
    return r, _hub_version


def _is_hub_running() -> Tuple[bool, Optional[str]]:
    # The version number reported is the Hub Server API version, not the version number visible to the user in Launcher.
    r, hub_version = _get_hub_version()
    if r == omni.client.Result.OK:
        return True, hub_version
    if r == omni.client.Result.ERROR_CONNECTION:
        return False, None
    raise StorageOperationError(f"")


def wrapp_register_authentication_callback(callback) -> object:
    """
    Register a callback function for authentication through the client library.

    Releasing the returned object will deregister the authentication callback function.
    """
    return ClientLibStorage.register_authentication_callback(callback)
