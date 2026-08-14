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
    Optional,
)

from pydantic import BaseModel

from wrapp.datastructures.wrapp_file_model import (
    PkgStdFileModel,
    PkgStdModelVersion,
    all_old_file_model_classes,
)


class PackageStandardMetadataEntry_V1_0(BaseModel):
    name: str
    hash: Dict[str, str]


class PackageStandardDefinition_V1_0(PkgStdFileModel):
    format_version: PkgStdModelVersion = PkgStdModelVersion("1.0")
    package_id: str
    license: str
    metadata: list[PackageStandardMetadataEntry_V1_0]
    content_hash: Dict[str, str]
    package_hash: Dict[str, str]


class PackageStandardBomItem_V1_0(BaseModel):
    relative_path: str
    size: int
    hash: Optional[Dict[str, str]] = None
    content_location: Optional[str] = None


class PackageStandardBom_V1_0(PkgStdFileModel):
    format_version: PkgStdModelVersion = PkgStdModelVersion("1.0")
    items: list[PackageStandardBomItem_V1_0]
    content_root: Optional[str] = None


# Versioned classes above are compatibility snapshots. Do not edit an
# existing class body after it has shipped; add a new class and registry entry
# for a new file format version instead.
all_old_file_model_classes[("PackageStandardDefinition", PkgStdModelVersion("1.0"))] = PackageStandardDefinition_V1_0
all_old_file_model_classes[("PackageStandardBom", PkgStdModelVersion("1.0"))] = PackageStandardBom_V1_0
