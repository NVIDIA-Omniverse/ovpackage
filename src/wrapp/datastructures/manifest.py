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

from typing import TypeAlias

from pydantic import BaseModel

from wrapp.datastructures.previous_versions import (  # noqa: F401
    manifest_old_versions as _manifest_old_versions,
)
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
)
from wrapp.utils.api_marker import wrapp_public_api

MANIFEST_VERSION = WRAPPModelVersion(version_string="1.0")
MANIFEST_STORAGE_API_VERSION = "v1alpha"


@wrapp_public_api
class ManifestResourceInfo(BaseModel):
    """Backend resource metadata for a manifest file entry: its resource identity, size in bytes and last-modified time."""

    resource_identity: str
    size: int
    modified_time: str


@wrapp_public_api
class ManifestFileEntry(BaseModel):
    """A full manifest entry describing a single file by its relative path together with its backend resource info."""

    relative_path: str
    resource_info: ManifestResourceInfo


@wrapp_public_api
class ManifestMinimalFileEntry(BaseModel):
    """A minimal manifest entry describing a single file by its relative path and resource identity only, omitting size and modified time."""

    relative_path: str
    resource_identity: str


ManifestEntry: TypeAlias = ManifestFileEntry | ManifestMinimalFileEntry


@wrapp_public_api
class Manifest(WRAPPFileModel):
    """A WRAPP file model listing the files of a package as manifest entries, used by the build-manifest command."""

    format_version: WRAPPModelVersion = MANIFEST_VERSION
    storage_api_version: str = MANIFEST_STORAGE_API_VERSION
    files: list[ManifestEntry]
