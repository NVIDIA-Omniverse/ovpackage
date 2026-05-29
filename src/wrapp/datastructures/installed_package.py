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

from typing import Optional

from pydantic import BaseModel

from wrapp.datastructures.package import PackageInfo
from wrapp.utils.api_marker import wrapp_public_api


@wrapp_public_api
class InstalledPackage(BaseModel):
    """
        Represents a package that has potentially been installed in a storage location, it also might be just a wrapp
        file found in a folder tree underneath a top level wrapp file.

        This class stores metadata about an installed package including its name,
        version, location, and repository information. It inherits from Pydantic's BaseModel
        for data validation and serialization capabilities.

        Attributes:
            name (str): The package name.
            version (str): The package version installed.
            wrapp_file_url (str): The absolute URL of the wrapp file in the installed location.
            install_location (str): The path of the package relative to the top level package.
            repository (Optional[str]): The URL of the repository this package was installed from.
                                       None indicates this is a fresh package that has not been
                                       created in a repo yet.
            package (Optional[PackageInfo]): The detailed package information including the catalog, if already loaded
        """

    name: str
    version: str
    wrapp_file_url: str
    install_location: str
    repository: Optional[str]
    package: Optional[PackageInfo]

    @property
    def num_items(self):
        """For backward compatibility with the 2.0.0-a9 and 2.0.0-a10, which had the num_items field but not the package and the catalog."""
        return len(self.package.catalog.items) if self.package and self.package.catalog else 0
