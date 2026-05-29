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

from typing import (
    Dict,
    Set,
)

from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
    StorageMap,
)


class BlobStore:
    def __init__(self, catalog: Catalog):
        self.storage_map: Dict[str, str] = {}
        self._create_storage_map(catalog)

    def get_storage_map(self):
        return StorageMap(storage_map=self.storage_map)

    @staticmethod
    def _storage_path_for_item(item: CatalogItem) -> str:
        if item.hash is None:
            return item.relative_path
        else:
            safe_hash = item.hash.replace(' <>"/\\|?*\0', "_")
            return f"blobs/{safe_hash[-2:]}/{safe_hash}"

    def _create_storage_map(self, catalog: Catalog):
        used_storage_paths: Set[str] = set()
        if catalog.items is not None:
            for item in catalog.items:
                if item.hash is not None and item.hash not in self.storage_map:
                    storage_path_base = BlobStore._storage_path_for_item(item)
                    storage_path = storage_path_base
                    i = 1
                    while storage_path in used_storage_paths:
                        storage_path = f"{storage_path_base}_{i}"
                        i += 1
                    self.storage_map[item.hash] = storage_path
                    used_storage_paths.add(storage_path)
