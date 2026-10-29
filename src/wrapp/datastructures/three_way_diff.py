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

import copy
import json
from enum import Enum
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
    TypeGuard,
)

from wrapp.datastructures.patch import (
    ItemType,
    PatchOpAddTag,
    PatchOpBase,
    PatchOpCopy,
    PatchOpCreateFolder,
    PatchOpDelete,
    PatchOpDeleteTag,
)
from wrapp.datastructures.two_way_diff import Diff
from wrapp.utils.utils import (
    drop_parameters_from_url,
    join_url_path,
)


#
# Table of cases for 3-way comparison
# (two cases if there can be a conflict or not, X means the case is not possible because A and B have a common baseline)
#                   B Unchanged B Added	    B Modified	B Deleted (<B diff baseline)
#   A Unchanged	    1           2           3           4
#   A Added 	    5           6+7         X           X
#   A Modified	    8           X           9+10        11
#   A Deleted	    12          X           13          14
#   (^A diff baseline)
class ThreeWayDiffCase(Enum):
    UNCHANGED = 1
    ADDED_IN_B = 2
    MODIFIED_IN_B = 3
    DELETED_IN_B = 4
    ADDED_IN_A = 5
    ADDED_IN_BOTH_EQUAL = 6
    ADDED_IN_BOTH_CONFLICT = 7
    MODIFIED_IN_A = 8
    MODIFIED_IN_BOTH_EQUAL = 9
    MODIFIED_IN_BOTH_CONFLICT = 10
    DELETED_IN_B_MODIFIED_IN_A = 11
    DELETED_IN_A = 12
    DELETED_IN_A_MODIFIED_IN_B = 13
    DELETED_IN_BOTH = 14

    @property
    def is_same_in_a_and_b(self) -> bool:
        return self in [self.UNCHANGED, self.DELETED_IN_BOTH, self.ADDED_IN_BOTH_EQUAL, self.MODIFIED_IN_BOTH_EQUAL]


class ThreeWayDiffContext:
    def __init__(
        self,
        a: Dict[str, Dict],
        b: Dict[str, Dict],
        baseline: Dict[str, Dict],
        a_root: str,
        tags: bool = False,
        *,
        name_a: str | None = None,
        name_b: str | None = None,
        name_base: str | None = None,
    ):
        self.a = a
        self.b = b
        self.baseline = baseline
        self.a_root = a_root
        self.tags = tags
        self.name_a = name_a or "A"
        self.name_b = name_b or "B"
        self.name_base = name_base or "base"
        self.compare_fields = self._build_field_list()
        self.fields_without_tags = sorted(list(set(self.compare_fields) - {"tags"}))

    def _build_field_list(self) -> List[str]:
        fields_to_ignore = copy.copy(Diff.not_to_compare_fields)
        if not self.tags:
            fields_to_ignore.append("tags")
        fields = []
        for d in [self.a, self.b, self.baseline]:
            for key, value in d.items():
                attributes = value.keys()
                # Filter out built-in attributes and methods
                filtered_attributes = [attr for attr in attributes if not attr.startswith("__")]
                fields.extend(filtered_attributes)
        # Remove duplicates and make order deterministic
        return sorted(list(set(fields) - set(fields_to_ignore)))

    def determine_diff_case(self, key: str) -> tuple[ThreeWayDiffCase, Optional[Dict], Optional[Dict], Optional[Dict]]:
        base_item, a_item, b_item = self.items_to_compare(key)

        a_modified = not Diff.compare_catalog_entries(base_item, a_item, self.compare_fields)
        b_modified = not Diff.compare_catalog_entries(base_item, b_item, self.compare_fields)
        a_and_b_equal = Diff.compare_catalog_entries(a_item, b_item, self.compare_fields)

        case: ThreeWayDiffCase
        if not a_modified and not b_modified:
            case = ThreeWayDiffCase.UNCHANGED
        elif base_item is None:
            if a_item is None and b_item is not None:
                case = ThreeWayDiffCase.ADDED_IN_B
            elif a_item is not None and b_item is None:
                case = ThreeWayDiffCase.ADDED_IN_A
            elif a_and_b_equal:
                case = ThreeWayDiffCase.ADDED_IN_BOTH_EQUAL
            else:
                case = ThreeWayDiffCase.ADDED_IN_BOTH_CONFLICT
        elif b_item is None:
            if a_item is None:
                case = ThreeWayDiffCase.DELETED_IN_BOTH
            else:
                if a_modified:
                    case = ThreeWayDiffCase.DELETED_IN_B_MODIFIED_IN_A
                else:
                    case = ThreeWayDiffCase.DELETED_IN_B
        elif a_item is None:
            if b_modified:
                case = ThreeWayDiffCase.DELETED_IN_A_MODIFIED_IN_B
            else:
                case = ThreeWayDiffCase.DELETED_IN_A
        elif not a_modified and b_modified:
            case = ThreeWayDiffCase.MODIFIED_IN_B
        elif a_modified and not b_modified:
            case = ThreeWayDiffCase.MODIFIED_IN_A
        elif not a_and_b_equal:
            case = ThreeWayDiffCase.MODIFIED_IN_BOTH_CONFLICT
        else:
            case = ThreeWayDiffCase.MODIFIED_IN_BOTH_EQUAL

        return case, base_item, a_item, b_item

    def diff_explanation(self, key: str) -> str:
        case, base_item, a_item, b_item = self.determine_diff_case(key)
        a_diff_str = Diff.reason_for_compare_catalog_entries(base_item, a_item, self.compare_fields, "base", "A")
        b_diff_str = Diff.reason_for_compare_catalog_entries(base_item, b_item, self.compare_fields, "base", "B")
        ab_diff_str = Diff.reason_for_compare_catalog_entries(a_item, b_item, self.compare_fields, "A", "B")
        return f"{case} diff(A,base):{a_diff_str} diff(B,base):{b_diff_str} diff(A,B):{ab_diff_str}"

    def items_to_compare(self, key: str) -> Tuple[Optional[Dict], Optional[Dict], Optional[Dict]]:
        return self.baseline.get(key), self.a.get(key), self.b.get(key)

    def all_keys(self) -> List[str]:
        return sorted(list(set(self.baseline.keys()).union(self.a.keys()).union(self.b.keys())))


class ThreeWayDiff:
    @staticmethod
    def _tag_catalog_to_dict(catalog: List[Dict[str, Any]], key_field: str = "relative_path") -> Dict[str, Dict]:
        result: Dict[str, Dict] = {}
        for item in catalog:
            if key_field in item:
                key: str = item.get(key_field, "")
                result[key] = item
            else:
                raise RuntimeError(f"Missing key field {key_field} in {item}")
        return result

    @staticmethod
    def diff_with_context(
        context: ThreeWayDiffContext,
    ) -> Tuple[List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]]]:
        def item_type(item: Optional[Dict]) -> str:
            if item is None:
                raise RuntimeError("Program error, item_type should not be called with None argument")
            return item["type"]

        # Fix mypy issues without having to add an assert all over the place below
        def item_is_folder(item: Optional[Dict]) -> TypeGuard[Dict]:
            return item_type(item) == "folder"

        def item_is_file(item: Optional[Dict]) -> TypeGuard[Dict]:
            return item_type(item) == "file"

        def item_is_tag(item: Optional[Dict]) -> TypeGuard[Dict]:
            return item_type(item) == "tag"

        def unknown_type_exception(item: Optional[Dict]):
            if item is None:
                raise RuntimeError("program error, item is None is three way merge")
            else:
                raise RuntimeError(f"unknown item type {item_type(item)} encountered during merge, program error?")

        next_id = 0
        tag_ids = {}

        tags = context.tags
        all_keys = context.all_keys()
        a_root = context.a_root
        fields_without_tags = context.fields_without_tags

        def make_tag(tag: Dict, relative_path: str):
            nonlocal next_id, tag_ids
            primary_key = (tag["name"], tag["ns"])
            if primary_key not in tag_ids:
                tag_ids[primary_key] = next_id
                next_id += 1

            return {"id": tag_ids[primary_key], "relative_path": relative_path, "type": "tag", **tag}

        def _build_tag_set(catalog_item: Optional[Dict]) -> List[Dict]:
            if catalog_item is not None and "tags" in catalog_item and catalog_item["tags"] is not None:
                return [make_tag(tag, catalog_item["relative_path"]) for tag in catalog_item["tags"]]
            else:
                return []

        def merge_tag_sets(
            a_item: Optional[Dict], b_item: Optional[Dict], base_item: Optional[Dict]
        ) -> Tuple[List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]]]:
            a_tags = _build_tag_set(a_item)
            b_tags = _build_tag_set(b_item)
            base_tags = _build_tag_set(base_item)
            return ThreeWayDiff.diff(
                ThreeWayDiff._tag_catalog_to_dict(a_tags, key_field="id"),
                ThreeWayDiff._tag_catalog_to_dict(b_tags, key_field="id"),
                baseline=ThreeWayDiff._tag_catalog_to_dict(base_tags, key_field="id"),
                a_root=a_root,
                tags=False,
            )

        def create_tag_patch_lists(
            a_item: Optional[Dict], b_item: Optional[Dict], base_item: Optional[Dict]
        ) -> Tuple[List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]]]:
            tag_patches: List[Tuple[str, PatchOpBase]] = []
            tag_conflicts: List[Tuple[str, PatchOpBase]] = []
            if (
                not Diff.compare_catalog_entries(a_item, b_item, ["tags"])
                or not Diff.compare_catalog_entries(base_item, b_item, ["tags"])
                or not Diff.compare_catalog_entries(base_item, a_item, ["tags"])
            ):
                # Tags were modified. We need to run a separate Tags merge and add the result to our conflict list
                tag_patches, tag_conflicts, tag_extras = merge_tag_sets(a_item, b_item, base_item)
                tag_conflicts.extend(tag_extras)
            return tag_patches, tag_conflicts

        patch_list: List[Tuple[str, PatchOpBase]] = []
        conflict_list: List[Tuple[str, PatchOpBase]] = []
        extras_list: List[Tuple[str, PatchOpBase]] = []

        for key in all_keys:
            case, base_item, a_item, b_item = context.determine_diff_case(key)

            if case.is_same_in_a_and_b:
                continue
            elif case == ThreeWayDiffCase.ADDED_IN_B:
                assert b_item is not None
                target_filename = join_url_path(a_root, b_item["relative_path"])
                if item_is_folder(b_item):
                    patch_list.append((key, PatchOpCreateFolder(url=target_filename, explanation=f"Creating {key} to {context.name_a} as in {context.name_b}")))  # type: ignore
                elif item_is_file(b_item):
                    patch_list.append(
                        (
                            key,
                            PatchOpCopy(
                                src=b_item["source_path"],
                                dst=target_filename,
                                explanation=f"Adding {key} to {context.name_a} from {context.name_b}",
                                item_type=ItemType.file,
                                hash=b_item["hash"],
                                resource_identity_v2=b_item.get("resource_identity_v2"),
                            ),  # type: ignore
                        )
                    )
                    if tags:
                        tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                        patch_list.extend(tag_patches)
                        conflict_list.extend(tag_conflicts)
                elif item_is_tag(b_item):
                    patch_list.append(
                        (key, PatchOpAddTag(url=target_filename, tag=json.dumps(b_item), explanation=f"Adding tag to {context.name_a} from {context.name_b}"))  # type: ignore
                    )
                else:
                    unknown_type_exception(b_item)
            elif case == ThreeWayDiffCase.ADDED_IN_A:
                if item_is_file(a_item) or item_is_folder(a_item):
                    extras_list.append(
                        (
                            key,
                            PatchOpDelete(
                                url=drop_parameters_from_url(a_item["source_path"]),
                                explanation=f"Extra: {key} added to {context.name_a} but not to {context.name_b}. Force: Delete {context.name_a}",
                            ),  # type: ignore
                        )
                    )
                elif item_is_tag(a_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpDeleteTag(
                                url=join_url_path(a_root, a_item["relative_path"]),
                                tag=json.dumps(a_item),
                                explanation=f"Conflict: tag {a_item['name']} added to {context.name_a} but not {context.name_b}. Force: Delete tag from {context.name_a}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(a_item)
            elif case == ThreeWayDiffCase.ADDED_IN_BOTH_CONFLICT:
                if item_is_file(a_item) or item_is_folder(a_item):
                    if a_item is not None and b_item is not None:
                        conflict_list.append(
                            (
                                key,
                                PatchOpCopy(
                                    src=b_item["source_path"],
                                    dst=drop_parameters_from_url(a_item["source_path"]),
                                    explanation=f"Conflict: {key} added to both {context.name_a} and {context.name_b} but not identical. Force: take {context.name_b} version",
                                    item_type=ItemType[item_type(a_item)],
                                    hash=b_item["hash"],
                                    resource_identity_v2=b_item.get("resource_identity_v2"),
                                ),  # type: ignore
                            )
                        )
                        if tags:
                            # Tags might be modified. We need to run a separate Tags merge and add the result to our conflict list
                            tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                            patch_list.extend(tag_patches)
                            conflict_list.extend(tag_conflicts)
                    else:
                        raise RuntimeError(f"Error in 3-way merge: {a_item} and {b_item} should both not be None")
                elif item_is_tag(a_item):
                    # The new tag will overwrite the old one, making it to be the B tag
                    if a_item is not None and b_item is not None:
                        conflict_list.append(
                            (
                                key,
                                PatchOpAddTag(
                                    url=join_url_path(a_root, a_item["relative_path"]),
                                    tag=json.dumps(b_item),
                                    explanation=f"Conflict: tag {json.dumps(a_item)} added to {context.name_a} and {json.dumps(b_item)} to {context.name_b}. Force: take {context.name_b} version",
                                ),  # type: ignore
                            )
                        )
                    else:
                        raise RuntimeError(f"Error in 3-way merge: {a_item} and {b_item} should both not be None")
                else:
                    unknown_type_exception(a_item)
            elif case == ThreeWayDiffCase.DELETED_IN_B_MODIFIED_IN_A:
                if item_is_file(a_item) or item_is_folder(a_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpDelete(
                                url=drop_parameters_from_url(a_item["source_path"]),
                                explanation=f"Conflict: {key} deleted in {context.name_b} but modified in {context.name_a}. Force: Delete from {context.name_a}",
                            ),  # type: ignore
                        )
                    )
                elif item_is_tag(a_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpDeleteTag(
                                url=join_url_path(a_root, a_item["relative_path"]),
                                tag=json.dumps(a_item),
                                explanation=f"Conflict: tag {json.dumps(a_item)} deleted in {context.name_b} but modified in {context.name_a}. Force: Delete from {context.name_a}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(a_item)
            elif case == ThreeWayDiffCase.DELETED_IN_B:
                if item_is_file(a_item) or item_is_folder(a_item):
                    patch_list.append(
                        (
                            key,
                            PatchOpDelete(
                                url=drop_parameters_from_url(a_item["source_path"]),
                                explanation=f"Delete {key} from {context.name_a}, as it was removed from {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                elif item_is_tag(a_item):
                    patch_list.append(
                        (
                            key,
                            PatchOpDeleteTag(
                                url=join_url_path(a_root, a_item["relative_path"]),
                                tag=json.dumps(a_item),
                                explanation=f"Delete tag {json.dumps(a_item)} from {context.name_a}, as it was removed from {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(a_item)
            elif case == ThreeWayDiffCase.DELETED_IN_A_MODIFIED_IN_B:
                if item_is_file(b_item) or item_is_folder(b_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpCopy(
                                src=b_item["source_path"],
                                dst=join_url_path(a_root, b_item["relative_path"]),
                                explanation=f"Conflict: {key} deleted in {context.name_a} but modified in {context.name_b}. Force: Recreate with modified {context.name_b}",
                                item_type=ItemType[item_type(b_item)],
                                hash=b_item["hash"],
                                resource_identity_v2=b_item.get("resource_identity_v2"),
                            ),  # type: ignore
                        )
                    )
                    if tags:
                        # Tags might be modified. We need to run a separate Tags merge and add the result to our conflict list
                        tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                        patch_list.extend(tag_patches)
                        conflict_list.extend(tag_conflicts)
                elif item_is_tag(b_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpAddTag(
                                url=join_url_path(a_root, b_item["relative_path"]),
                                tag=json.dumps(b_item),
                                explanation=f"Conflict: Tag {json.dumps(b_item)} deleted in {context.name_a}. Force: Recreate with modified {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(b_item)
            elif case == ThreeWayDiffCase.DELETED_IN_A:
                if item_is_file(b_item) or item_is_folder(b_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpCopy(
                                src=b_item["source_path"],
                                dst=join_url_path(a_root, b_item["relative_path"]),
                                explanation=f"Conflict: {key} deleted in {context.name_a}. Force: Recreate from {context.name_b}.",
                                item_type=ItemType[item_type(b_item)],
                                hash=b_item["hash"],
                                resource_identity_v2=b_item.get("resource_identity_v2"),
                            ),  # type: ignore
                        )
                    )
                    if tags:
                        # Tags might be modified. We need to run a separate Tags merge and add the result to our conflict list
                        tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                        patch_list.extend(tag_patches)
                        conflict_list.extend(tag_conflicts)
                elif item_is_tag(b_item):
                    conflict_list.append(
                        (
                            key,
                            PatchOpAddTag(
                                url=join_url_path(a_root, b_item["relative_path"]),
                                tag=json.dumps(b_item),
                                explanation=f"Conflict: Tag {json.dumps(b_item)} deleted in {context.name_a}. Force: Recreate from {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(b_item)
            elif case == ThreeWayDiffCase.MODIFIED_IN_B:
                assert a_item is not None, "This is case ThreeWayDiffCase.DELETED_IN_A_MODIFIED_IN_B"
                if item_is_file(b_item) or item_is_folder(b_item):
                    if not Diff.compare_catalog_entries(a_item, b_item, fields_without_tags):
                        patch_list.append(
                            (
                                key,
                                PatchOpCopy(
                                    src=b_item["source_path"],
                                    dst=drop_parameters_from_url(a_item["source_path"]),
                                    explanation=f"Update {key} in {context.name_a} from {context.name_b}",
                                    item_type=ItemType[item_type(b_item)],
                                    hash=b_item["hash"],
                                    resource_identity_v2=b_item.get("resource_identity_v2"),
                                ),  # type: ignore
                            )
                        )
                    if tags:
                        # Tags might be modified. We need to run a separate Tags merge and add the result to our patch list
                        tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                        patch_list.extend(tag_patches)
                        conflict_list.extend(tag_conflicts)

                elif item_is_tag(b_item):
                    patch_list.append(
                        (
                            key,
                            PatchOpAddTag(
                                url=join_url_path(a_root, a_item["relative_path"]),
                                tag=json.dumps(b_item),
                                explanation=f"Update tag {json.dumps(a_item)} in {context.name_a} from {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(b_item)
            elif case == ThreeWayDiffCase.MODIFIED_IN_A:
                assert a_item is not None, "This is case ThreeWayDiffCase.DELETED_IN_B_MODIFIED_IN_A"
                if item_is_file(b_item) or item_is_folder(b_item):
                    if not Diff.compare_catalog_entries(a_item, b_item, fields_without_tags):
                        conflict_list.append(
                            (
                                key,
                                PatchOpCopy(
                                    src=b_item["source_path"],
                                    dst=drop_parameters_from_url(a_item["source_path"]),
                                    explanation=f"Conflict: {key} modified in {context.name_a} but not {context.name_b}. Force: Revert to {context.name_b}",
                                    item_type=ItemType[item_type(b_item)],
                                    hash=b_item["hash"],
                                    resource_identity_v2=b_item.get("resource_identity_v2"),
                                ),  # type: ignore
                            )
                        )
                    if tags:
                        # Tags might be modified. We need to run a separate Tags merge and add the result to our conflict list
                        tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                        patch_list.extend(tag_patches)
                        conflict_list.extend(tag_conflicts)

                elif item_type(b_item) == "tag":
                    conflict_list.append(
                        (
                            key,
                            PatchOpAddTag(
                                url=join_url_path(a_root, a_item["relative_path"]),
                                tag=json.dumps(b_item),
                                explanation=f"Conflict: Tag {json.dumps(a_item)} modified in {context.name_a} but not {context.name_b}. Force: Revert to {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(b_item)
            elif case == ThreeWayDiffCase.MODIFIED_IN_BOTH_CONFLICT:
                assert (
                    a_item is not None and b_item is not None
                ), "This is case ThreeWayDiffCase.DELETED_IN_A_MODIFIED_IN_B or DELETED_IN_B_MODIFIED_IN_A"
                if item_is_file(b_item) or item_is_folder(b_item):
                    if not Diff.compare_catalog_entries(a_item, b_item, fields_without_tags):
                        conflict_list.append(
                            (
                                key,
                                PatchOpCopy(
                                    src=b_item["source_path"],
                                    dst=drop_parameters_from_url(a_item["source_path"]),
                                    explanation=f"Conflict: {key} modified in both {context.name_a} and {context.name_b} with different values. Force: Override changes in {context.name_a} with {context.name_b}",
                                    item_type=ItemType[item_type(b_item)],
                                    hash=b_item["hash"],
                                    resource_identity_v2=b_item.get("resource_identity_v2"),
                                ),  # type: ignore
                            )
                        )
                    if tags:
                        # Tags might be modified. We need to run a separate Tags merge and add the result to our conflict list
                        tag_patches, tag_conflicts = create_tag_patch_lists(a_item, b_item, base_item)
                        patch_list.extend(tag_patches)
                        conflict_list.extend(tag_conflicts)
                elif item_type(b_item) == "tag":
                    conflict_list.append(
                        (
                            key,
                            PatchOpAddTag(
                                url=join_url_path(a_root, a_item["relative_path"]),
                                tag=json.dumps(b_item),
                                explanation=f"Conflict: Tag {json.dumps(a_item)} modified in both {context.name_a} and {context.name_b} with different values. Force: Override changes in {context.name_a} with {context.name_b}",
                            ),  # type: ignore
                        )
                    )
                else:
                    unknown_type_exception(b_item)
            else:
                raise RuntimeError(f"Unknown case {case} encountered during 3-way merge")

        return patch_list, conflict_list, extras_list

    @staticmethod
    def diff(
        a: Dict[str, Dict],
        b: Dict[str, Dict],
        baseline: Dict[str, Dict],
        a_root: str,
        tags: bool = False,
        *,
        name_a: str | None = None,
        name_b: str | None = None,
        name_base: str | None = None,
    ) -> Tuple[List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]]]:
        """
        Given three Dicts, calculate the list of update operations required to turn a into b using baseline as a common ancestor. Returns the list of
        updates and as second item of the returned tuple the list of update operations that are in conflict. Each list is a list of tuples with the
        primary key being the first item and the patch operation the second item.
        Applying the conflicts actually erases local modifications in the target.

        :param a: dictionary mapping string keys to a dictionary of elements
        :param b: dictionary mapping string keys to a dictionary of elements
        :param baseline: dictionary mapping string keys to a dictionary of elements, being the known common ancestor of a and b
        :param a_root: base path to prepend to relative paths of 'a' to create full source URLs.
        :param tags: flag to set if tags should be compared. Tags will be ignored if not set
        :return: Three lists of PatchOp objects: patches (changes which can be applied to make a match b),
            conflicts (files changed in a and b), and extras (files added to a, but not b)
        """

        context = ThreeWayDiffContext(a, b, baseline, a_root, tags, name_a=name_a, name_b=name_b, name_base=name_base)
        return ThreeWayDiff.diff_with_context(context)
