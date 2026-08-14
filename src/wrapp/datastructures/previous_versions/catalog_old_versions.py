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

import os
import sys
import tempfile
from bisect import bisect_left
from enum import Enum
from tempfile import NamedTemporaryFile
from typing import (
    Any,
    ClassVar,
    Optional,
)

from pydantic import BaseModel

from wrapp.datastructures.previous_versions.serialized_data import SerializedData
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
    all_old_file_model_classes,
)


class Tag_V1(BaseModel):
    """Represents a tag as defined by the Nucleus Tagging Service"""

    name: str
    ns: str
    value: str


class CatalogItemType_V1(str, Enum):
    """Designated the type of the item that can be stored in a catalog"""

    file = "file"
    folder = "folder"


class CatalogItem_V1(BaseModel):
    source_path: str  # An absolute URL pointing to the content of the item at the time of catalog creation
    relative_path: str  # PRIMARY KEY in catalog, the location of the item relative to the root of the catalog
    type: CatalogItemType_V1
    hash: Optional[str] = None  # A content-based hash. For comparison, beware there are multiple algorithms around
    size: Optional[int] = None
    tags: Optional[list[Tag_V1]] = None


class CatalogItem_V2(BaseModel):
    source_path: str  # An absolute URL pointing to the content of the item at the time of catalog creation
    relative_path: str  # PRIMARY KEY in catalog, the location of the item relative to the root of the catalog
    hash: Optional[str] = None  # A content-based hash. For comparison, beware there are multiple algorithms around
    size: Optional[int] = None
    tags: Optional[list[Tag_V1]] = None


class CatalogItem_V3(BaseModel):
    source_path: str  # An absolute URL pointing to the content of the item at the time of catalog creation
    relative_path: str  # PRIMARY KEY in catalog, the location of the item relative to the root of the catalog
    hash: Optional[str] = None  # A content-based hash. For comparison, beware there are multiple algorithms around
    size: Optional[int] = None
    tags: Optional[list[Tag_V1]] = None
    resource_identity: Optional[str] = None  # A string handle that is guaranteed to produce the exact same data bytes when calling read


class StorageMap_V1(BaseModel):
    storage_map: dict[str, str]  # Map relative_path to a path containing the content of that item. Used for deduplication


class Catalog_V1(SerializedData):
    root: str  # Root URL of catalog operation, all relative_path entries of the Item resolve to this
    # When you change the format_version value, make sure to
    # 1. add a Catalog.migrate_format_version below (similar to PackageInfo.migrate_format_version)
    # 2. add a test to test_backwards_compatibility.py
    format_version: str = "1"  # To designate backward compatibility of stored catalogs (they are stored as JSON)
    items: list[CatalogItem_V1]  # Content of catalog
    storage_map: Optional[
        StorageMap_V1
    ] = None  # Currently only used in TAR files to map the relative_path to the file with the content for deduplication


class Catalog_V2_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="2.0")
    root: str  # Root URL of catalog operation, all relative_path entries of the Item resolve to this
    items: list[CatalogItem_V1]  # Content of catalog
    storage_map: Optional[
        StorageMap_V1
    ] = None  # Currently only used in TAR files to map the relative_path to the file with the content for deduplication

    @classmethod
    def migrate_major_version(cls, read_data: Catalog_V1) -> "Catalog_V2_0":
        # v2: introduction of WRAPPFileModel/WRAPPModelVersion
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return Catalog_V2_0(**data_dict)


class Catalog_V3_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="3.0")
    root: str  # Root URL of catalog operation, all relative_path entries of the Item resolve to this
    items: list[CatalogItem_V2]  # Content of catalog
    storage_map: Optional[
        StorageMap_V1
    ] = None  # Currently only used in TAR files to map the relative_path to the file with the content for deduplication

    @classmethod
    def migrate_major_version(cls, read_data: Catalog_V2_0) -> "Catalog_V3_0":
        # Version 2.0 -> 3.0:
        # * Dropped field type from CatalogItem
        # * Create .empty file entry for each previous empty item of type Folder
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        non_folders = list(filter(lambda x: x["type"] != CatalogItemType_V1.folder, data_dict["items"]))
        folders = list(filter(lambda x: x["type"] == CatalogItemType_V1.folder, data_dict["items"]))
        for item in non_folders:
            item.pop("type")
        for item in folders:
            if "type" in item:
                item.pop("type")
            item["size"] = 0
            item["relative_path"] = item["relative_path"] + "/.empty"
        return Catalog_V3_0(**data_dict)


class Catalog_V4_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="4.0")
    root: str  # Root URL of catalog operation, all relative_path entries of the Item resolve to this
    items: list[CatalogItem_V2]  # Content of catalog
    storage_map: Optional[
        StorageMap_V1
    ] = None  # Currently only used in TAR files to map the relative_path to the file with the content for deduplication
    _empty_folder_file: ClassVar[Optional[Any]] = None

    @classmethod
    def source_path_for_empty_folder_files(cls):
        if cls._empty_folder_file is None:
            # Create a NamedTemporaryFile and keep it around until the process ends.
            # On Windows, we must use delete=False because Windows locks files exclusively
            # when they are open, preventing concurrent reads from multiple patch operations.
            # The file is empty and will be cleaned up by the OS temp file cleanup.
            cls._empty_folder_file = NamedTemporaryFile(mode="rb", delete=(sys.platform != "win32"), prefix=".wrapp_preserve_empty_folder")
            if sys.platform == "win32":
                # Close the file on Windows so other processes can read it
                cls._empty_folder_file.close()
        return cls._empty_folder_file.name

    @classmethod
    def filename_to_keep_empty_folder(cls):
        # The only use of this name is when migrating packages with empty folder support created by WRAPP 1.x
        # to packages without empty folder support for WRAPP 2.x and higher. When migrating,
        # the empty folder is replaced with an empty file with this name in that folder.
        # Use a wrapp specific name, so people know to look in the docs.
        return ".wrapp_preserve_empty_folder"

    @classmethod
    def migrate_major_version(cls, read_data: Catalog_V3_0) -> "Catalog_V4_0":
        # Version 3.0 -> 4.0:
        # * Due to a bug WRAPP 2.0.0-pre1 copied content into the ".empty" catalog entries during install from a 1.1.2 package.
        #   * We ended up with ".empty" folders in each folder with the same content as the folder itself.
        #   * When a package was created from that installed folder, the actual empty folders were lost (because the
        #     .empty folders were folders and empty, not dummy files).
        # * There are therefore two different kind of Catalog_V3_0 encountered in this function:
        #   1. Catalogs originating by a read from a WRAPP 1.X package, transformed by the migrate_major_version for Catalog_V2_0->Catalog_V3_0.
        #      * Those catalogs contain ".empty" entries for every folder in the Catalog_V2_0 structure.
        #   2. Catalogs originating by reading a package created by WRAPP 2.0.0-pre1 after installing/creating a WRAPP 1.X package.
        #      * Those catalogs contain plenty of files in ".empty" folders which should not exist at all.
        # * Resolution:
        #   * Remove all ".empty" entries from catalog for non-empty folders. Otherwise, we litter the entire directory structure.
        #   * Replace ".empty" entries with the more explicitly named new placeholder '.wrapp_keep_empty_folder'.
        #   * Ignore any files in ".empty" folders (but not the .empty placeholder files).
        #   * For the source path of the file use an empty file which lives as long as the Catalog class, so the copy op works
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version

        def is_placeholder_file_for_folder(x) -> bool:
            return x["relative_path"].endswith("/.empty")

        dir_set_with_empty_file = set()
        new_items = []
        non_placeholder_paths = []
        for item in data_dict["items"]:
            # Ignore any files in ".empty" folders (but not the .empty placeholder files and the .empty directories).
            # Due to a bug WRAPP 2.0.0-pre1 copied content into the ".empty" catalog entries during install from
            # a 1.1.2 package. Whereas after just reading a WRAPP 1.1.2 catalog, we get .empty files, as expected.
            # The aim is to keep the .empty entries (files and folders) in dirs_with_empty_file,
            # and ditch all files in the ".empty" folders created by the install process.
            empty_index = item["relative_path"].find("/.empty/")
            if empty_index != -1:
                dir_set_with_empty_file.add(item["relative_path"][:empty_index])
            elif item["relative_path"].endswith("/.empty"):
                dir_set_with_empty_file.add(item["relative_path"].removesuffix("/.empty"))
            else:
                new_items.append(item)
                non_placeholder_paths.append(item["relative_path"])

        dirs_with_empty_file = sorted(dir_set_with_empty_file)
        non_placeholder_paths.sort()

        def is_prefix_of_any(path, sorted_paths):
            pos = bisect_left(sorted_paths, path)
            if sorted_paths[pos] != path:
                return sorted_paths[pos].startswith(path)
            return pos < len(sorted_paths) - 1 and sorted_paths[pos + 1].startswith(path)

        # Remove all .empty files which are in a directory which has another directory in it containing a .empty file
        empty_dirs = []
        for d in dirs_with_empty_file:
            if not is_prefix_of_any(d, dirs_with_empty_file) and not is_prefix_of_any(d, non_placeholder_paths):
                empty_dirs.append(d)

        for d in empty_dirs:
            # Exclude empty folders in .thumbs - nobody cares about those, and they get in the way.
            if "/.thumbs/" in d or d.startswith(".thumbs/"):
                continue
            # Create a full item entry for an empty file to avoid diff getting confused about what changed and what did not.
            new_items.append(
                {
                    "size": 0,
                    "relative_path": d + "/" + cls.filename_to_keep_empty_folder(),
                    "source_path": cls.source_path_for_empty_folder_files(),
                    # Set the hash to the value of an empty file.
                    "hash": "sha-256-flat;1048576;e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                }
            )

        data_dict["items"] = new_items
        return Catalog_V4_0(**data_dict)


class Catalog_V5_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="5.0")
    root: str  # Root URL of catalog operation, all relative_path entries of the Item resolve to this
    items: list[CatalogItem_V3]  # Content of catalog
    storage_map: Optional[
        StorageMap_V1
    ] = None  # Currently only used in TAR files to map the relative_path to the file with the content for deduplication
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
        # Actually, the Catalog_V4 was broken in that it used .wrapp_keep_empty_folder for newly created catalogs
        # and .wrapp_preserve_empty_folder for migrated Catalogs. The update to Catalog_V5 fixes this should
        # even entries with the new name be created on non-migrated catalogs they are now also called
        # with the name the migration used.
        return ".wrapp_preserve_empty_folder"

    @classmethod
    def migrate_major_version(cls, read_data: Catalog_V4_0) -> "Catalog_V5_0":
        # Version 4.0 -> 5.x:
        # * Fixing wrongly named .wrapp_keep_empty_folder to .wrapp_preserve_empty_folder
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version

        # Update any items that use the wrong name to the correct naming
        old_filename = ".wrapp_keep_empty_folder"
        new_filename = cls.filename_to_keep_empty_folder()

        for item in data_dict["items"]:
            if item["relative_path"].endswith(f"/{old_filename}"):
                item["relative_path"] = item["relative_path"].replace(f"/{old_filename}", f"/{new_filename}")
                # Also update the source path filename (but keep only the directory, not any random suffix)
                # Source path is like /tmp/.wrapp_keep_empty_folder or /tmp/.wrapp_keep_empty_folderXXXXX
                source_basename = os.path.basename(item["source_path"])
                if source_basename.startswith(old_filename):
                    source_dir = os.path.dirname(item["source_path"])
                    # Don't keep any random suffix - use canonical name
                    item["source_path"] = os.path.join(source_dir, new_filename)

        return Catalog_V5_0(**data_dict)


# Versioned classes above are compatibility snapshots. Do not edit an
# existing class body after it has shipped; add a new class and registry entry
# for a new file format version instead.
all_old_file_model_classes[("Catalog", WRAPPModelVersion(version_string="0"))] = Catalog_V1
all_old_file_model_classes[("Catalog", WRAPPModelVersion(version_string="1"))] = Catalog_V1
all_old_file_model_classes[("Catalog", WRAPPModelVersion(version_string="2.0"))] = Catalog_V2_0
all_old_file_model_classes[("Catalog", WRAPPModelVersion(version_string="3.0"))] = Catalog_V3_0
all_old_file_model_classes[("Catalog", WRAPPModelVersion(version_string="4.0"))] = Catalog_V4_0
all_old_file_model_classes[("Catalog", WRAPPModelVersion(version_string="5.0"))] = Catalog_V5_0
