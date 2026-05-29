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

from abc import ABC
from enum import Enum
from typing import (
    Annotated,
    Literal,
    Optional,
    Union,
)

from pydantic import (
    BaseModel,
    Field,
)

from wrapp.datastructures.previous_versions.serialized_data import SerializedData
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
    all_old_wrapp_file_model_classes,
)


class PatchOperationType_V1(str, Enum):
    create_folder = "create_folder"
    delete = "delete"
    copy = "copy"
    add_tag = "add_tag"
    delete_tag = "delete_tag"


class ItemType_V1(str, Enum):
    file = "file"
    folder = "folder"


class PatchOpBase_V1(BaseModel, ABC):
    explanation: Optional[str] = None


class PatchOpCopy_V1(PatchOpBase_V1):
    operation_type: Literal[PatchOperationType_V1.copy] = PatchOperationType_V1.copy
    src: str
    dst: str
    hash: Optional[str] = None
    item_type: ItemType_V1


class PatchOpDelete_V1(PatchOpBase_V1):
    operation_type: Literal[PatchOperationType_V1.delete] = PatchOperationType_V1.delete
    url: str


class PatchOpCreateFolder_V1(PatchOpBase_V1):
    operation_type: Literal[PatchOperationType_V1.create_folder] = PatchOperationType_V1.create_folder
    url: str


class PatchOpAddTag_V1(PatchOpBase_V1):
    operation_type: Literal[PatchOperationType_V1.add_tag] = PatchOperationType_V1.add_tag
    url: str
    tag: str


class PatchOpDeleteTag_V1(PatchOpBase_V1):
    operation_type: Literal[PatchOperationType_V1.delete_tag] = PatchOperationType_V1.delete_tag
    url: str
    tag: str


PatchOpUnion_V1 = Annotated[
    Union[PatchOpCopy_V1, PatchOpDelete_V1, PatchOpCreateFolder_V1, PatchOpAddTag_V1, PatchOpDeleteTag_V1],
    Field(discriminator="operation_type"),
]


class PatchOpList_V1(SerializedData):
    # Without format_version: original format.
    # v1: add_tag operations are guaranteed to come before delete operations, but after all other operations.
    format_version: str = "1"
    operations: list[PatchOpUnion_V1]


class PatchOpList_V2_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="2.0")
    operations: list[PatchOpUnion_V1]

    @classmethod
    def migrate_major_version(cls, read_data: PatchOpList_V1) -> "PatchOpList_V2_0":
        # v3: introduction of WRAPPFileModel/WRAPPModelVersion
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return PatchOpList_V2_0(**data_dict)


all_old_wrapp_file_model_classes[("PatchOpList", WRAPPModelVersion(version_string="0"))] = PatchOpList_V1
all_old_wrapp_file_model_classes[("PatchOpList", WRAPPModelVersion(version_string="1"))] = PatchOpList_V1
all_old_wrapp_file_model_classes[("PatchOpList", WRAPPModelVersion(version_string="2.0"))] = PatchOpList_V2_0
