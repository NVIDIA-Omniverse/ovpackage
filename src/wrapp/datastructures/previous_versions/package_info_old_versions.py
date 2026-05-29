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
    Annotated,
    Optional,
)

from pydantic import (
    BaseModel,
    model_validator,
)

from wrapp.datastructures.previous_versions.catalog_old_versions import (
    Catalog_V1,
    Catalog_V2_0,
    Catalog_V3_0,
    Catalog_V4_0,
    Catalog_V5_0,
)
from wrapp.datastructures.previous_versions.dependency_old_versions import (
    Dependency_V2,
    Dependency_V3_0,
)
from wrapp.datastructures.previous_versions.dependency_set_old_versions import (
    DependencySet_V1_0,
)
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
    all_old_wrapp_file_model_classes,
)


class PackageInfo_V2(BaseModel):
    format_version: str = "2"
    name: Annotated[str, "Name of the package"] = ""
    version: Annotated[str, "Version referenced"] = ""
    catalog: Annotated[Optional[Catalog_V1], "Detailed content information"] = None
    repository: Annotated[Optional[str], "Optional repository"] = None
    dependencies: Annotated[Optional[list[Dependency_V2]], "Optional list of dependency entries"] = None

    @model_validator(mode="before")
    @classmethod
    def migrate_format_version(cls, values):
        """
        File format history for PackageInfo data:

        format version
        1 - first version
        2 - use 'repository' instead of 'remote'
        3 - introduce WRAPPFileModel/WRAPPModeLVersion
        """
        if "format_version" not in values:
            values["format_version"] = "2"
        if values["format_version"] == "1":
            values["repository"] = values["remote"]
            del values["remote"]
            if "source_url" in values:
                # This was already deprecated before changing the format_version to 2, so it might or might not be in a version 1
                del values["source_url"]
            values["format_version"] = "2"
        return values


class PackageInfo_V3_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="3.0")
    name: Annotated[str, "Name of the package"] = ""
    version: Annotated[str, "Version referenced"] = ""
    catalog: Annotated[Optional[Catalog_V2_0], "Detailed content information"] = None
    repository: Annotated[Optional[str], "Optional repository"] = None
    dependencies: Annotated[Optional[list[Dependency_V3_0]], "Optional list of dependency entries"] = None

    @classmethod
    def migrate_major_version(cls, read_data: PackageInfo_V2) -> "PackageInfo_V3_0":
        # v3: introduction of WRAPPFileModel/WRAPPModelVersion
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return PackageInfo_V3_0(**data_dict)


class PackageInfo_V4_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="4.0")
    name: Annotated[str, "Name of the package"] = ""
    version: Annotated[str, "Version referenced"] = ""
    catalog: Annotated[Optional[Catalog_V3_0], "Detailed content information"] = None
    repository: Annotated[Optional[str], "Optional repository"] = None
    dependencies: Annotated[Optional[list[Dependency_V3_0]], "Optional list of dependency entries"] = None

    @classmethod
    def migrate_major_version(cls, read_data: PackageInfo_V3_0) -> "PackageInfo_V4_0":
        # v4: New Catalog_V3_0
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return PackageInfo_V4_0(**data_dict)


class PackageInfo_V5_0(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="5.0")
    name: Annotated[str, "Name of the package"] = ""
    version: Annotated[str, "Version referenced"] = ""
    catalog: Annotated[Optional[Catalog_V5_0], "Detailed content information"] = None
    repository: Annotated[Optional[str], "Optional repository"] = None
    dependencies: Annotated[Optional[list[Dependency_V3_0]], "Optional list of dependency entries"] = None

    @classmethod
    def migrate_major_version(cls, read_data: PackageInfo_V4_0) -> "PackageInfo_V5_0":
        # v5: New Catalog_V4_0
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return PackageInfo_V5_0(**data_dict)


class PackageInfo_V5_1(WRAPPFileModel):
    """The PackageInfo class encapsulates all information about a wrapp package. These are stored in .wrapp files

    To load and save wrapp files, use the load() and save() member functions of the PackageInfo class.
    """

    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="5.1")
    name: str = ""  #: Name of the package
    version: str = ""  #: "Version referenced
    catalog: Optional[Catalog_V5_0] = None  #: Detailed content information
    source: Optional[str] = None  #: Optional URL of source of last create operation
    repository: Optional[str] = None  #: Optional repository of last create or install operation.
    dependencies: Optional[list[Dependency_V3_0]] = None  #: Optional list of dependency entries

    @classmethod
    def migrate_major_version(cls, read_data: PackageInfo_V5_0) -> "PackageInfo_V5_1":
        # v5.1: Added source field
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return PackageInfo_V5_1(**data_dict)


class PackageInfo_V5_2(WRAPPFileModel):
    """The PackageInfo class encapsulates all information about a wrapp package. These are stored in .wrapp files

    To load and save wrapp files, use the load() and save() member functions of the PackageInfo class.
    """

    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="5.2")
    name: str = ""  #: Name of the package
    version: str = ""  #: "Version referenced
    catalog: Optional[Catalog_V5_0] = None  #: Detailed content information
    source: Optional[str] = None  #: Optional URL of source of last create operation
    repository: Optional[str] = None  #: Optional repository of last create or install operation.
    dependencies: Optional[list[Dependency_V3_0]] = None  #: Optional list of dependency entries

    @classmethod
    def migrate_major_version(cls, read_data: PackageInfo_V5_1) -> "PackageInfo_V5_2":
        # v5.2: Uses Catalog_V5_0 instead of Catalog_V4_0
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return PackageInfo_V5_2(**data_dict)


all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="1"))] = PackageInfo_V2
all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="2"))] = PackageInfo_V2
all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="3.0"))] = PackageInfo_V3_0
all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="4.0"))] = PackageInfo_V4_0
all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="5.0"))] = PackageInfo_V5_0
all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="5.1"))] = PackageInfo_V5_1
all_old_wrapp_file_model_classes[("PackageInfo", WRAPPModelVersion(version_string="5.2"))] = PackageInfo_V5_2
