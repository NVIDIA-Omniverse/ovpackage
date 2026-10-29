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

import datetime
import json
import logging
import os.path
from typing import (
    Dict,
    Optional,
)

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
)

from wrapp.datastructures.previous_versions import (  # noqa: F401
    hash_cache_old_versions as _hash_cache_old_versions,
)
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
)
from wrapp.utils.coro import assert_is_wrapp_thread
from wrapp.utils.stats import (
    Stats,
    StatType,
)
from wrapp.utils.storage_interface import SerializedResourceIdentity
from wrapp.utils.utils import (
    FailedCommand,
    is_file_url,
)

logger = logging.getLogger("omni.wrapp")

HASH_CACHE_VERSION = WRAPPModelVersion(version_string="1.1")


class HashCacheEntry(BaseModel):
    modification_time: datetime.datetime
    filesize: int
    hash: str


class IdentityHashCacheEntry(BaseModel):
    filesize: int
    hash: str


class HashCacheFile(WRAPPFileModel):
    format_version: WRAPPModelVersion = HASH_CACHE_VERSION
    entries: Dict[str, HashCacheEntry]
    identity_entries: Dict[str, IdentityHashCacheEntry] = Field(default_factory=dict)


class HashCache:
    """Separate address hashes (get/set) from opaque identity hashes (get_identity/set_identity)."""

    def __init__(self, filename: str, verbose: bool = False):
        assert_is_wrapp_thread()
        self.filename = filename
        self.verbose = verbose
        if os.path.exists(self.filename):
            logger.info(f"Found existing hash cache, loading file {filename}")
            cache_file = self._load_cache_file()
        else:
            cache_file = HashCacheFile(entries={})
        self.cache: Dict[str, HashCacheEntry] = cache_file.entries
        self.identity_cache: Dict[str, IdentityHashCacheEntry] = cache_file.identity_entries

    def save(self) -> None:
        assert_is_wrapp_thread()
        logger.info(f"Saving hash cache to {self.filename}")
        cache_file = HashCacheFile(entries=self.cache, identity_entries=self.identity_cache)
        try:
            with open(self.filename, "wt", encoding="utf-8") as hash_file:
                hash_file.write(cache_file.model_dump_json(indent=2))
                hash_file.write("\n")
        except OSError as e:
            raise FailedCommand(f"Failed to write hash cache file '{self.filename}': {e}") from e

    def _load_cache_file(self) -> HashCacheFile:
        try:
            with open(self.filename, "rb") as hash_file:
                raw_data = hash_file.read()
        except OSError as e:
            raise FailedCommand(f"Failed to read hash cache file '{self.filename}': {e}") from e

        if not raw_data:
            return HashCacheFile(entries={})

        if self._looks_like_pickle(raw_data):
            raise FailedCommand(
                f"Hash cache file '{self.filename}' uses legacy pickle format which is no longer supported. "
                "Delete it or migrate it to the JSON hash cache format."
            )

        try:
            parsed_cache = json.loads(raw_data.decode("utf-8"))
            cache_file = HashCacheFile.model_validate(parsed_cache)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as e:
            raise FailedCommand(
                f"Hash cache file '{self.filename}' is invalid. Expected WRAPP hash cache JSON format " "with a compatible format_version."
            ) from e

        return cache_file

    @staticmethod
    def _looks_like_pickle(raw_data: bytes) -> bool:
        # Binary pickle payloads start with 0x80 followed by a protocol version byte.
        return len(raw_data) >= 2 and raw_data[0] == 0x80

    @staticmethod
    def _make_key(filename: str):
        # We could add the mod_time to the key, but then the cache file would grow unbound
        # if we just use the filename we need to additionally check the modification time on get()
        if is_file_url(filename):
            return os.path.abspath(filename)
        else:
            return filename

    def get(self, filename: str, filesize: int, mod_time: datetime.datetime) -> Optional[str]:
        assert_is_wrapp_thread()
        key = self._make_key(filename)
        if key in self.cache:
            if self.cache[key].modification_time == mod_time and self.cache[key].filesize == filesize:
                logger.debug(f"Hash cache hit on key {key}: {self.cache[key]}")
                Stats.get().increment(StatType.HASH_CACHE_HIT)
                return self.cache[key].hash
        logger.debug(f"Hash cache miss for key {key}")
        Stats.get().increment(StatType.HASH_CACHE_MISS)
        return None

    def set(self, filename: str, filesize: int, mod_time: datetime.datetime, hash: str):
        assert_is_wrapp_thread()
        key = self._make_key(filename)
        logger.debug(f"Storing hash in hash cache key {key}: {hash}")
        self.cache[key] = HashCacheEntry(modification_time=mod_time, filesize=filesize, hash=hash)
        Stats.get().increment(StatType.HASH_CACHE_INSERT)

    def get_identity(self, identity: SerializedResourceIdentity, filesize: int) -> Optional[str]:
        """Look up an exact opaque identity, never an address or a normalized URL."""
        assert_is_wrapp_thread()
        entry = self.identity_cache.get(identity)
        if entry is not None and entry.filesize == filesize:
            logger.debug(f"Identity hash cache hit for {identity}")
            Stats.get().increment(StatType.HASH_CACHE_HIT)
            return entry.hash
        logger.debug(f"Identity hash cache miss for {identity}")
        Stats.get().increment(StatType.HASH_CACHE_MISS)
        return None

    def set_identity(self, identity: SerializedResourceIdentity, filesize: int, hash: str) -> None:
        """Store an identity-derived hash separately, even if the token looks like an address."""
        assert_is_wrapp_thread()
        self.identity_cache[identity] = IdentityHashCacheEntry(filesize=filesize, hash=hash)
        Stats.get().increment(StatType.HASH_CACHE_INSERT)
