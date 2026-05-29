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
    Tuple,
)

from pydantic import BaseModel

from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
)
from wrapp.utils.utils import make_relative_url


class ItemReferences(BaseModel):
    referenced_item_paths: Set[str] = set()


class ItemUsages(BaseModel):
    referencing_item_paths: Set[str] = set()


class CollectedReferences(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="1.0")
    root: str = ""
    referencing_items: Dict[str, ItemReferences] = {}
    resolved_items: Dict[str, ItemUsages] = {}
    unresolved_items: Dict[str, ItemUsages] = {}
    external_items: Dict[str, ItemUsages] = {}

    @property
    def file_set_complete(self) -> bool:
        return not self.external_items and not self.unresolved_items

    def add_item(self, referencing: str, referenced_abs_path: str, referenced_path_in_file: str, referenced_exists: bool):
        referencing_rel_path = make_relative_url(self.root, referencing)
        referenced_rel_path = make_relative_url(self.root, referenced_abs_path)
        self.referencing_items.setdefault(referencing_rel_path, ItemReferences()).referenced_item_paths.add(referenced_path_in_file)
        if referenced_abs_path.startswith(self.root):
            if not referenced_exists:
                self.unresolved_items.setdefault(referenced_rel_path, ItemUsages()).referencing_item_paths.add(referencing_rel_path)
            else:
                self.resolved_items.setdefault(referenced_rel_path, ItemUsages()).referencing_item_paths.add(referencing_rel_path)
        else:
            self.external_items.setdefault(referenced_rel_path, ItemUsages()).referencing_item_paths.add(referencing_rel_path)
