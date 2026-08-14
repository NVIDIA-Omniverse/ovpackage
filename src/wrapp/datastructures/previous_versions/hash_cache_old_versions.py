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

from pydantic import BaseModel

from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
    all_old_file_model_classes,
)


class HashCacheEntry_v1_0(BaseModel):
    modification_time: datetime.datetime
    filesize: int
    hash: str


class HashCacheFile_v1_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="1.0")
    entries: dict[str, HashCacheEntry_v1_0]


# Versioned classes above are compatibility snapshots. Do not edit an
# existing class body after it has shipped; add a new class and registry entry
# for a new file format version instead.
all_old_file_model_classes[("HashCacheFile", WRAPPModelVersion(version_string="1.0"))] = HashCacheFile_v1_0
