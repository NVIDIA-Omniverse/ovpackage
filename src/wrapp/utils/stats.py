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

from enum import Enum
from typing import (
    Dict,
    Optional,
    Set,
)

from wrapp.utils.utils import format_size


class StatType(Enum):
    # Careful with renaming any of these, there are some unit tests that check for the occurrence or *absence* of this text
    FILES = "files"
    CHECKPOINTS = "checkpoints"
    FOLDERS = "folders"
    BYTES_IN_CATALOG = "in catalog"
    BYTES_COPIED = "copied"
    TAGS = "tags"
    LIST_CHECKPOINTS_ASYNC = "list_checkpoints_async calls"
    LIST = "list calls"
    LIST_ASYNC = "list_async calls"
    WRITE_FILE_ASYNC = "write_file_async calls"
    READ_FILE = "read_file calls"
    READ_FILE_ASYNC = "read_file_async calls"
    COPY = "copy calls"
    COPY_ASYNC = "copy_async calls"
    STAT = "stat calls"
    STAT_ASYNC = "stat_async calls"
    DELETE_SINGLE_ASYNC = "delete_single_async calls"
    DELETE_ASYNC = "delete_async calls"
    GET_LOCAL_FILE_ASYNC = "get_local_file_async calls"
    SET_TAGS = "modify_tags calls"
    GET_TAGS = "get_tags calls"
    QUERY_TAGS = "query_tags calls"
    CREATE_WITH_HASH_ASYNC = "create_with_hash_async calls"
    GET_SERVER_INFO = "get_server_info calls"
    GET_SERVER_INFO_ASYNC = "get_server_info_async calls"
    GET_HUB_VERSION = "get_hub_version calls"
    HASH_CACHE_HIT = "hash cache hits"
    HASH_CACHE_MISS = "hash cache misses"
    HASH_CACHE_INSERT = "hash cache inserts"
    # The following Stats are automatically calculated by the increment function, no need to manually set increment those
    AUTO_NUM_CLIENTLIB_CALLS = "total calls to storage provider"
    AUTO_NUM_TAGGING_SERVICE_CALLS = "total calls to tagging service"


# Define which of the Stats auto increments one of the Auto StatType entries
g_CLIENTLIB_STATS = {
    StatType.LIST,
    StatType.LIST_ASYNC,
    StatType.LIST_CHECKPOINTS_ASYNC,
    StatType.WRITE_FILE_ASYNC,
    StatType.READ_FILE,
    StatType.READ_FILE_ASYNC,
    StatType.COPY,
    StatType.COPY_ASYNC,
    StatType.STAT,
    StatType.STAT_ASYNC,
    StatType.DELETE_SINGLE_ASYNC,
    StatType.GET_LOCAL_FILE_ASYNC,
    StatType.CREATE_WITH_HASH_ASYNC,
    StatType.GET_SERVER_INFO,
    StatType.GET_SERVER_INFO_ASYNC,
    StatType.GET_HUB_VERSION,
}
g_TAGGING_SERVICE_STATS = {StatType.GET_TAGS, StatType.GET_TAGS, StatType.QUERY_TAGS}


class Stats:
    _instance = None

    def __init__(self) -> None:
        if Stats._instance is not None:
            raise Exception("SingletonClass is a singleton class. Use get_instance() to access the instance.")
        Stats._instance = self
        self.stats: Dict[StatType, int] = {}

    @classmethod
    def get(cls) -> "Stats":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def clear(self):
        self.stats = {}

    def increment(self, name: StatType, value: int = 1) -> None:
        if name not in self.stats:
            self.stats[name] = 0
        self.stats[name] += value
        if name in g_TAGGING_SERVICE_STATS:
            self.increment(StatType.AUTO_NUM_TAGGING_SERVICE_CALLS)
        if name in g_CLIENTLIB_STATS:
            self.increment(StatType.AUTO_NUM_CLIENTLIB_CALLS)

    def get_stat(self, name: StatType) -> int:
        return self.stats.get(name, 0)

    def get_all_stats(self) -> Dict[StatType, int]:
        return self.stats.copy()

    def print_statistics_set(
        self,
        included: Optional[Set[StatType]] = None,
        excluded: Optional[Set[StatType]] = None,
        total_stat_type: Optional[StatType] = None,
        start_with_divider=False,
    ) -> bool:
        if start_with_divider:
            print("---------------")
        had_output = False
        for item in self.stats:
            if included and item not in included:
                continue
            if excluded and item in excluded:
                continue
            had_output = True
            if item in [StatType.BYTES_IN_CATALOG, StatType.BYTES_COPIED]:
                print(f"{format_size(self.stats[item])} {item.value}")
            else:
                print(f"{self.stats[item]} {item.value}")
        if total_stat_type and total_stat_type in self.stats:
            print(f"{self.stats[total_stat_type]} {total_stat_type.value}")
        return had_output

    def pretty_print(self) -> None:
        print("Statistics:")
        need_divider = self.print_statistics_set(
            None,
            g_CLIENTLIB_STATS.union(g_TAGGING_SERVICE_STATS).union(
                {StatType.AUTO_NUM_TAGGING_SERVICE_CALLS, StatType.AUTO_NUM_CLIENTLIB_CALLS}
            ),
        )
        need_divider = self.print_statistics_set(g_TAGGING_SERVICE_STATS, None, StatType.AUTO_NUM_TAGGING_SERVICE_CALLS, need_divider)
        self.print_statistics_set(g_CLIENTLIB_STATS, None, StatType.AUTO_NUM_CLIENTLIB_CALLS, need_divider)
