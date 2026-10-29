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
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)

from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.package import (
    is_package_file_name,
    load_package_setup,
)
from wrapp.utils.utils import get_path_from_url


class Diff:
    not_to_compare_fields = ["source_path", "version", "resource_identity", "resource_identity_v2", "modified_time"]

    @staticmethod
    def _build_compare_field_list(a: List[Dict], b: List[Dict], fields_to_ignore: List[str]):
        all_fields = set().union(*(d.keys() for d in a + b))
        return [field for field in all_fields if field not in fields_to_ignore]

    @staticmethod
    def compare_catalog_entries(entry1: Optional[dict], entry2: Optional[dict], compare_fields: list) -> bool:
        if entry1 is None and entry2 is None:
            return True
        if entry1 is None or entry2 is None:
            return False
        for field in compare_fields:
            if field not in entry1 and field not in entry2:
                # Field not present in both entries, ignore
                continue
            if field not in entry1 or field not in entry2 or entry1[field] != entry2[field]:
                return False
        return True

    @staticmethod
    def reason_for_compare_catalog_entries(
        entry1: Optional[dict], entry2: Optional[dict], compare_fields: list, entry1_name: str, entry2_name: str
    ) -> str:
        if entry1 is None and entry2 is None:
            return f"Not in {entry1_name} or {entry2_name}"
        if entry1 is None or entry2 is None:
            return f"Only in {entry2_name if entry1 is None else entry1_name}"
        for field in compare_fields:
            if field not in entry1 and field not in entry2:
                # Field not present in both entries, ignore
                continue
            if field not in entry1 or field not in entry2 or entry1[field] != entry2[field]:
                return f"'{entry1_name}.{field}'!='{entry2_name}.{field}' ('{entry1[field]}'!='{entry2[field]}')"
        return "same"

    @staticmethod
    def print_diff(diffed: Tuple[Dict, Dict], ignore_fields: List[str]):
        field_list = Diff._build_compare_field_list([diffed[0]], [diffed[1]], ignore_fields)
        print(f"Item {diffed[0]} differs from {diffed[1]}:")
        for field in field_list:
            if field not in diffed[0]:
                print(f"  Difference in field {field}: missing from source")
            elif field not in diffed[1]:
                print(f"  Difference in field {field}: missing from destination")
            elif diffed[0][field] != diffed[1][field]:
                print(f"  Difference in {field}: {diffed[0][field]} != {diffed[1][field]}")

    @staticmethod
    async def load_catalog_or_package(catalog: str) -> Optional[Catalog]:
        # Check if these are catalogs or wrapp files
        if is_package_file_name(get_path_from_url(catalog)):
            a_package = await load_package_setup(catalog)
            if a_package is not None:
                return a_package.catalog
        return await Catalog.load(catalog)

    @staticmethod
    async def diff_files_internal(
        catalog1: str, catalog2: str, fields_to_ignore: List[str]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        a_cat = await Diff.load_catalog_or_package(catalog1)
        b_cat = await Diff.load_catalog_or_package(catalog2)
        return Diff.diff_catalogs_internal(a_cat if a_cat else Catalog(), b_cat if b_cat else Catalog(), fields_to_ignore)

    @staticmethod
    def diff_catalogs_internal(
        a_cat: Catalog, b_cat: Catalog, fields_to_ignore: List[str]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        a_items = a_cat.items if a_cat.items is not None else []
        b_items = b_cat.items if b_cat.items is not None else []
        return Diff.diff_internal([item.model_dump() for item in a_items], [item.model_dump() for item in b_items], fields_to_ignore)

    @staticmethod
    def diff_internal(
        a: List[Dict[str, Any]], b: List[Dict[str, Any]], fields_to_ignore: List[str]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
        # Sort (should be sorted, but if having been tampered with it might be no longer sorted)
        key_name = "relative_path"
        a.sort(key=lambda x: x[key_name])
        b.sort(key=lambda y: y[key_name])

        # Build list of fields to compare
        compare_fields = Diff._build_compare_field_list(a, b, fields_to_ignore)

        # Compare both lists and find unique and differing items
        unique_to_a, unique_to_b, differing_items = [], [], []
        i, j = 0, 0
        while i < len(a) and j < len(b):
            if a[i][key_name] < b[j][key_name]:
                unique_to_a.append(a[i])
                i += 1
            elif a[i][key_name] > b[j][key_name]:
                unique_to_b.append(b[j])
                j += 1
            else:
                if not Diff.compare_catalog_entries(a[i], b[j], compare_fields):
                    differing_items.append((a[i], b[j]))
                i += 1
                j += 1

        # Add any remaining items to the unique lists
        if i < len(a):
            unique_to_a.extend(a[i:])
        if j < len(b):
            unique_to_b.extend(b[j:])
        return unique_to_a, unique_to_b, differing_items
