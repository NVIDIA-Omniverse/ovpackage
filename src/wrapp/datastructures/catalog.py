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
import tempfile
import warnings
from bisect import bisect_left
from collections import defaultdict
from datetime import (
    datetime,
    timezone,
)
from typing import (
    Any,
    ClassVar,
    Dict,
    List,
    Optional,
)

from pydantic import (
    BaseModel,
    Field,
    field_validator,
)

from wrapp.datastructures.previous_versions.catalog_old_versions import Catalog_V5_0
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.utils import (
    StorageOperationError,
    escape_legacy_storage_address,
    join_url_path,
    make_relative_url,
)

logger = logging.getLogger("omni.wrapp")


class Tag(BaseModel):
    """Represents a tag as defined by the Nucleus Tagging Service"""

    name: str
    ns: str
    value: str


@wrapp_public_api
class CatalogItem(BaseModel):
    """
    A single item in a catalog represents a file. folders are not part of the catalog.
    """

    source_path: str  #: The logical input address used when the item was catalogued
    relative_path: str  #: PRIMARY KEY in catalog, the location of the item relative to the root of the catalog
    hash: Optional[str] = None  #: content-based hash. For comparison, beware there are multiple algorithms around
    size: Optional[int] = None  #: size of the file
    tags: Optional[List[Tag]] = None  #: list of tags applied to the file (Nucleus only)
    resource_identity: Optional[str] = None  #: Deprecated: historical identity claims are not trusted for reads.
    resource_identity_v2: Optional[str] = Field(default=None, min_length=1)
    """Read token for the recorded bytes: returns those bytes or fails. Equal tokens
    imply equal content; different tokens do not imply different content.
    """
    modified_time: Optional[datetime] = None  #: Modification time captured with the identity and size; absent in legacy catalogs.

    @field_validator("modified_time")
    @classmethod
    def _utc_modified_time(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@wrapp_public_api
class StorageMap(BaseModel):
    """This is used to define a mapping between the hash of a CatalogItem and an alternative storage path.
    This is used to map hashes to file names in tar files for deduplication."""

    storage_map: Dict[str, str]  #: Map hash to a path containing the content of that item.


@wrapp_public_api
class Catalog(WRAPPFileModel):
    """The Catalog contains a bill of materials (BOM) of files at a specific version identified by their relative path relative to the root URL recorded."""

    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="6.0")
    root: str  #: Root URL of catalog operation, all relative_path entries of the Item resolve to this
    items: list[CatalogItem]  #: Content of catalog
    storage_map: Optional[
        StorageMap
    ] = None  #: Currently only used in TAR files to map the hash to the file with the content for deduplication
    _empty_folder_file_path: ClassVar[Optional[str]] = None

    @classmethod
    def source_path_for_empty_folder_files(cls):
        if cls._empty_folder_file_path is None:
            temp_dir = tempfile.gettempdir()
            cls._empty_folder_file_path = os.path.join(temp_dir, cls.filename_to_keep_empty_folder())
            # create the empty file
            with open(cls._empty_folder_file_path, "wb") as f:
                pass
        return cls._empty_folder_file_path

    @classmethod
    def filename_to_keep_empty_folder(cls):
        # The only use of this name is when migrating packages with empty folder support created by WRAPP 1.x
        # to packages without empty folder support for WRAPP 2.x and higher. When migrating,
        # the empty folder is replaced with an empty file with this name in that folder.
        # Use a wrapp specific name, so people know to look in the docs.
        #
        # Actually, the Catalog_V4 was broken in that it used .wrapp_keep_empty_folder for newly created catalogs
        # and .wrapp_preserve_empty_folder for migrated Catalogs. The update to Catalog_V5 fixes this should
        # even entries with the new name be created on non-migrated catalogs they are now also called
        # with the name the migration used.
        return ".wrapp_preserve_empty_folder"

    @staticmethod
    def _is_prefix_of_any_but_not_only_exact_match(path, sorted_paths):
        pos = bisect_left(sorted_paths, path)
        if pos >= len(sorted_paths):
            return False
        if sorted_paths[pos] != path:
            return sorted_paths[pos].startswith(path)
        return pos < len(sorted_paths) - 1 and sorted_paths[pos + 1].startswith(path)

    @classmethod
    def migrate_major_version(cls, read_data: Catalog_V5_0) -> "Catalog":
        # Historical identity claims must never be promoted to v2. The v5
        # snapshot preserves the old field and leaves the new fields absent.
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")
        data_dict["root"] = escape_legacy_storage_address(data_dict["root"])
        for item in data_dict["items"]:
            item["source_path"] = escape_legacy_storage_address(item["source_path"])
        if data_dict.get("storage_map") is not None:
            storage_map = data_dict["storage_map"]["storage_map"]
            for key, address in storage_map.items():
                storage_map[key] = escape_legacy_storage_address(address)
        return Catalog(**data_dict)

    @staticmethod
    async def load_catalog(source: str) -> "Catalog":
        # TODO in Python 3.10 we can only runtime warn, but newer pythons have @deprecated annotation
        warnings.warn("naming harmonization for wrapp 2.0.0: use load() instead of load_catalog()", DeprecationWarning, stacklevel=2)
        return await Catalog.load(source)

    def size_of_catalog(self) -> int:
        """Return the sum of bytes in all items of this catalog."""
        return sum([item.size if item.size is not None else 0 for item in self.items])

    def size_of_catalog_deduped(self) -> int:
        """Return the sum of bytes in all items of this catalog, counting each distinct hash only once."""
        by_hash: Dict[str, CatalogItem] = {}
        for item in self.items:
            if item.hash is not None and item.hash != "":
                by_hash[item.hash] = item
            else:
                by_hash[item.relative_path] = item
        size_deduped = 0
        for value in by_hash:
            if value in by_hash:
                file_size: Optional[int] = by_hash[value].size
                if file_size is not None:
                    size_deduped += file_size
        return size_deduped

    def _catalog_to_dict(self, key_field: str = "relative_path") -> Dict[str, Dict]:
        result: Dict[str, Dict] = {}
        for catalog_item in self.items:
            item = catalog_item.model_dump()
            if key_field in item and isinstance(item.get(key_field), str):
                # Optionally resolve storage_map URLs for redirect packages
                item["source_path"] = self.get_source_url_for_item(catalog_item)
                result[item.get(key_field)] = item  # type: ignore
                if not hasattr(result[item.get(key_field)], "type"):
                    result[item.get(key_field)]["type"] = "file"
            else:
                raise Exception(f"Program error - key {key_field} not found in {item} or not a str")
        return result

    def _build_index(self) -> Dict[str, CatalogItem]:
        result: Dict[str, CatalogItem] = {}
        for item in self.items:
            result[item.relative_path] = item
        if len(self.items) != len(result.keys()):
            logger.warning(
                "Built index for catalog with duplicate entries for 'relative_path', subsequent operations might not produce expected result."
            )
        return result

    def _update_source_paths(self, new_catalog: "Catalog") -> List[CatalogItem]:
        items_not_updated = []
        new_paths = new_catalog._build_index()
        for item in self.items:
            if item.relative_path in new_paths:
                new_item = new_paths[item.relative_path]
                if item.hash is not None and item.hash != new_item.hash:
                    raise StorageOperationError(
                        f"Failed to update source_path - new catalog has hash {new_item.hash} but original catalog had {item.hash}"
                    )
                item.source_path = new_item.source_path
                # resource_identity is location-bound: it encodes how to read the
                # bytes back from a specific storage backend. After mirror/import
                # the bytes live at the new location, so the source-side identity
                # would resolve to the wrong (or unreachable) place. Adopt the
                # destination identity, including None when the destination
                # storage doesn't expose one.
                item.resource_identity = new_item.resource_identity
                item.resource_identity_v2 = new_item.resource_identity_v2
                item.modified_time = new_item.modified_time
                item.size = new_item.size
            else:
                items_not_updated.append(item)
        return items_not_updated

    def _add_catalog(self, base_path, catalog_base, additional_catalog: "Catalog") -> None:
        index = self._build_index()
        # Now merge the sub catalog into our catalog. We must recalculate the relative paths though!
        for item in additional_catalog.items:
            abs_path = join_url_path(catalog_base, item.relative_path)
            new_relative_path = make_relative_url(base_path, abs_path)
            # Only add item if it is not already in the catalog, merging overlaps here
            if new_relative_path not in index:
                copied_item = item.model_copy()
                copied_item.relative_path = new_relative_path
                self.items.append(copied_item)

                # Merge storage_map entry for this item if additional_catalog has one
                storage_map_value = additional_catalog._get_storage_map_value_for_item(item)
                if storage_map_value is not None:
                    self._add_storage_map_entry(copied_item, storage_map_value)

    def _add_storage_map_entry(self, item: CatalogItem, value: str) -> None:
        """Add a storage_map entry for an item using its hash as the key.

        If storage_map doesn't exist yet, it will be created.
        """
        if self.storage_map is None:
            self.storage_map = StorageMap(storage_map={})

        if item.hash:
            self.storage_map.storage_map[item.hash] = value

    def _get_storage_map_value_for_item(self, item: CatalogItem) -> Optional[str]:
        """
        Returns the storage_map value for an item, or None if not found.

        Looks up the item in the storage_map using its hash as the key.

        :param item: The CatalogItem to look up
        :return: The mapped value, or None if no storage_map or item not found
        """
        if self.storage_map and item.hash and item.hash in self.storage_map.storage_map:
            return self.storage_map.storage_map[item.hash]
        return None

    def get_source_url_for_item(self, item: CatalogItem) -> str:
        """
        Returns the URL to use for fetching the actual data for a catalog item.

        If a storage_map is present and has an entry for this item, returns the
        mapped URL (used for redirect packages). Otherwise returns the item's source_path.

        :param item: The CatalogItem to get the source URL for
        :return: The URL to use for fetching the item's data
        """
        return self._get_storage_map_value_for_item(item) or item.source_path

    def get_storage_path_for_item(self, item: CatalogItem) -> str:
        """
        Returns the storage path for a catalog item within a TAR file or similar archive.

        If a storage_map is present, returns the mapped path. Otherwise returns
        the item's relative_path. Used for TAR file operations with hash-based deduplication.

        :param item: The CatalogItem to get the storage path for
        :return: The path to use for storing/retrieving the item's data
        """
        return self._get_storage_map_value_for_item(item) or item.relative_path

    def check_case_insensitive_path_conflicts(self) -> tuple[bool, List[str]]:
        """Check if any catalog items have paths that conflict on a case-insensitive filesystem.

        On case-insensitive systems (Windows, default macOS) paths like 'foo.psd' and 'Foo.psd'
        reference the same file and would cause conflicts during install, mirror, or import.

        :return: Tuple of (has_conflicts, conflicting_paths) where conflicting_paths is a flat
                 list of all paths involved in at least one conflict.
        """
        path_map: defaultdict[str, List[str]] = defaultdict(list)
        for item in self.items:
            path_map[item.relative_path.lower()].append(item.relative_path)

        conflicting_paths: List[str] = []
        for paths_with_same_lowercase in path_map.values():
            if len(paths_with_same_lowercase) > 1:
                conflicting_paths.extend(paths_with_same_lowercase)

        return len(conflicting_paths) > 0, conflicting_paths
