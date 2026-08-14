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

from typing import Annotated

from pydantic import (
    BaseModel,
    model_validator,
)

from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
    all_old_file_model_classes,
)


class Dependency_V2(BaseModel):

    format_version: str = "2"
    package: Annotated[str, "Name of the referenced package"]
    version: Annotated[str, "Version of the referenced package"]
    relative_destination: Annotated[str, "Relative installation path"]
    repository: Annotated[str, "The repository URL from which the package was installed"]

    @model_validator(mode="before")
    @classmethod
    def migrate_format_version(cls, values):
        """
        File format history for Dependency data:

        format version
        1 - first version
        2 - use 'repository' instead of 'remote'
        """
        if "format_version" not in values:
            values["format_version"] = "2"
        if values["format_version"] == "1":
            values["repository"] = values["remote"]
            del values["remote"]
            values["format_version"] = "2"
        return values


class Dependency_V3_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="3.0")
    package: Annotated[str, "Name of the referenced package"]
    version: Annotated[str, "Version of the referenced package"]
    relative_destination: Annotated[str, "Relative installation path"]
    repository: Annotated[str, "The repository URL from which the package was installed"]

    @classmethod
    def migrate_major_version(cls, read_data: Dependency_V2) -> "Dependency_V3_0":
        # v3: introduction of WRAPPFileModel/WRAPPModelVersion
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return Dependency_V3_0(**data_dict)


# Versioned classes above are compatibility snapshots. Do not edit an
# existing class body after it has shipped; add a new class and registry entry
# for a new file format version instead.
all_old_file_model_classes[("Dependency", WRAPPModelVersion(version_string="1"))] = Dependency_V2
all_old_file_model_classes[("Dependency", WRAPPModelVersion(version_string="2"))] = Dependency_V2
all_old_file_model_classes[("Dependency", WRAPPModelVersion(version_string="3.0"))] = Dependency_V3_0
