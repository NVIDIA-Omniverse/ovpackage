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
from typing import (
    List,
    Optional,
)

from wrapp.datastructures.command_context import CommandParameters
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage import (
    Result,
    wrapp_get_server_info,
)
from wrapp.utils.storage_impls.import_tagging_client import *
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    is_nucleus_url,
)

logger = logging.getLogger("omni.wrapp")


class Tagging:
    def __init__(self, context: CommandParameters, nucleus_host: str = "127.0.0.1"):
        if not g_tagging_client_available:
            raise FailedCommand("Nucleus tagging service is not available")
        self.context = context
        self.nucleus_host = nucleus_host
        self.connected_url: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.capabilities: Optional[tc.TaggingServiceCheckCapabilitiesResult] = None
        self.client_id: Optional[str] = None
        self.tagging_helper: Optional[tc.TaggingClientHelper] = None
        self.tagging_service: Optional[tc.TaggingService] = None
        self._connect_lock: asyncio.Lock = asyncio.Lock()

    async def connect(self, url: str):
        # Avoid connect and disconnect running at the same time or multiple times concurrently:
        await self._run_with_lock(self._connect_internal(url))

    async def disconnect(self):
        # Avoid connect and disconnect running at the same time or multiple times concurrently:
        await self._run_with_lock(self._disconnect_internal())

    async def _run_with_lock(self, awaitable):
        timeout = 5
        try:
            await asyncio.wait_for(self._connect_lock.acquire(), timeout=timeout)
            try:
                await awaitable
            finally:
                self._connect_lock.release()
        except TimeoutError:
            raise StorageOperationError(
                f"Failed to connect to/disconnect from tagging service for {self.nucleus_host}: blocked by concurrent connect/disconnect not completing after {timeout} seconds."
            )

    async def _connect_internal(self, url: str):
        if self.client_id is not None:
            # Already connected
            return

        target_is_nucleus = is_nucleus_url(url)
        if not target_is_nucleus:
            raise FailedCommand(f"Tagging is only supported for Nucleus Server")

        self.connected_url = url
        await self.refresh_token()

        # tagging helper handles discovery and transport creation for you
        logger.info(f"Contacting tagging service on {self.nucleus_host}")
        self.tagging_helper = tc.TaggingClientHelper(self.nucleus_host)
        self.tagging_service = await self.tagging_helper.get_tagging_service()
        await self.tagging_service.transport.prepare()

        # get server capabilities
        self.capabilities = await self.tagging_service.check_capabilities()

        # create client id
        create_id_res: tc.GenerateClientIdResult = await self.tagging_service.generate_client_id()
        self.client_id = create_id_res.client_id

    async def _disconnect_internal(self):
        if self.client_id is None:
            # Already disconnected
            return

        logger.info(f"disconnecting tagging service for {self.nucleus_host}")
        if self.tagging_helper:
            await self.tagging_helper.close()
            self.tagging_helper = None
        self.client_id = None

    async def refresh_token(self):
        logger.info(f"getting server info to refresh token")
        result, server_info = await wrapp_get_server_info(self.connected_url)
        if result != Result.OK:
            raise StorageOperationError(f"Failed to get server info and auth token for {self.connected_url}")
        self.auth_token = server_info.auth_token

    async def get_tags(self, test_path: List[str]) -> "GetTagsResult":
        for _ in range(2):
            Stats.get().increment(StatType.GET_TAGS)
            logger.info(f"getting tags from {test_path}")
            if self.tagging_service is None:
                raise StorageOperationError(f"No tagging service available for {self.nucleus_host}")
            else:
                if self.auth_token is None:
                    raise StorageOperationError(f"No auth_token for tagging service, can't get tags")
                else:
                    res: GetTagsResult = await self.tagging_service.get_tags(auth_token=self.auth_token, paths=test_path)
                    if res.status == StatusCode.TokenExpired:
                        await self.refresh_token()
                    else:
                        return res
        raise StorageOperationError(f"Lost connection to tagging service at {self.nucleus_host}")

    async def delete_tag(self, path: str, name: str, namespace: str) -> Optional["ModifyTagsResult"]:
        for _ in range(2):
            if self.context.dry_run:
                logger.info("Dry run: Deleting tag {name} from '{path}'")
                return None

            if self.tagging_service is None:
                raise StorageOperationError(f"No tagging service available for {self.nucleus_host}")
            else:
                Stats.get().increment(StatType.SET_TAGS)
                tag = tc.Tag(name=name, tag_namespace=namespace, value=None)
                logger.info(f"deleting tag ('{name}', '{namespace}') on {path}")
                if self.auth_token is None:
                    raise StorageOperationError(f"No auth_token for tagging service, can't delete tag")
                else:
                    res: ModifyTagsResult = await self.tagging_service.modify_tags(
                        auth_token=self.auth_token, client_id=self.client_id, paths=[path], tags=[tag], modify_type=tc.TagModifyType.Remove  # type: ignore
                    )
                    if any([res.status == StatusCode.TokenExpired for res in res.path_result]):
                        await self.refresh_token()
                    else:
                        return res
        raise StorageOperationError(f"Lost connection to tagging service at {self.nucleus_host}")

    async def query_tags(self, test_path: str) -> "tc.QueryResult":
        for _ in range(2):
            if self.tagging_service is None:
                raise StorageOperationError(f"No tagging service available for {self.nucleus_host}")
            else:
                Stats.get().increment(StatType.QUERY_TAGS)
                query = tc.Query(path=test_path, tag_name="", tag_namespace="", value="")
                ret_filter = tc.ReturnFilter(
                    return_tags=True, return_values=True, return_namespaces=True, return_paths=True, exclude_hidden=True
                )
                logger.info(f"querying tag on {test_path}")
                if self.auth_token is None:
                    raise StorageOperationError(f"No auth_token for tagging service, can't query tags")
                else:
                    res: tc.QueryResult = await self.tagging_service.tag_query(
                        auth_token=self.auth_token, query=query, ret_filter=ret_filter, max_results=100
                    )

                    if res.status == StatusCode.TokenExpired:
                        await self.refresh_token()
                    else:
                        return res
        raise StorageOperationError(f"Lost connection to tagging service at {self.nucleus_host}")

    async def set_tag(self, path, tag_name, tag_value, tag_namespace) -> Optional["tc.ModifyTagsResult"]:
        for _ in range(2):
            if self.context.dry_run:
                logger.info(f"Dry run: Setting tag {tag_name} on '{path}'")
                return None

            if self.tagging_service is None:
                raise StorageOperationError(f"No tagging service available for {self.nucleus_host}")
            else:
                Stats.get().increment(StatType.SET_TAGS)
                tag = tc.Tag(name=tag_name, tag_namespace=tag_namespace, value=tag_value)
                logger.info(f"Setting tag ({tag_name}, '{tag_value}', '{tag_namespace}') on {path}")
                if self.auth_token is None:
                    raise StorageOperationError(f"No auth_token for tagging service, can't set tag")
                else:
                    res: tc.ModifyTagsResult = await self.tagging_service.modify_tags(
                        auth_token=self.auth_token, client_id=self.client_id, paths=[path], tags=[tag], modify_type=tc.TagModifyType.Add  # type: ignore
                    )
                    if any([res.status == StatusCode.TokenExpired for res in res.path_result]):
                        await self.refresh_token()
                    else:
                        return res
        raise StorageOperationError(f"Lost connection to tagging service at {self.nucleus_host}")

    async def set_tags(self, path, tags: List["tc.Tag"]) -> Optional["ModifyTagsResult"]:
        for _ in range(2):
            if self.context.dry_run:
                logger.info("Dry run: Setting tags {tags} on '{path}'")
                return None

            if self.tagging_service is None:
                raise StorageOperationError(f"No tagging service available for {self.nucleus_host}")
            else:
                Stats.get().increment(StatType.SET_TAGS)
                logger.info(f"Setting tags {tags} on {path}")
                if self.auth_token is None:
                    raise StorageOperationError(f"No auth_token for tagging service, can't set tags")
                else:
                    res: tc.ModifyTagsResult = await self.tagging_service.modify_tags(
                        auth_token=self.auth_token, client_id=self.client_id, paths=[path], tags=tags, modify_type=tc.TagModifyType.Add  # type: ignore
                    )

                    if any([res.status == StatusCode.TokenExpired for res in res.path_result]):
                        await self.refresh_token()
                    else:
                        return res
        raise StorageOperationError(f"Lost connection to tagging service at {self.nucleus_host}")
