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

import dataclasses
from typing import (
    Awaitable,
    Callable,
    Optional,
)

from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
)
from wrapp.datastructures.loaded_package import LoadedPackage
from wrapp.datastructures.package import (
    Dependency,
    UniquePackageLocation,
)
from wrapp.repo.repository import Repository


@dataclasses.dataclass
class _PackageToMirror:
    """
    Represents a package that needs to be mirrored, along with its status in the mirroring process.
    """

    loaded_package: LoadedPackage
    source_repo: Repository
    referenced_by: str
    destination_repo: Repository
    has_been_checked_in_dest: bool = False
    existing_package: Optional[LoadedPackage] = None
    has_been_mirrored: bool = False
    template_version: Optional[str] = None
    _overlapping_package_mirrored: "_PackageToMirror | None" = (
        None  # If packages are overlapping, this is the package which is actually mirrored instead of self
    )

    @property
    def package_name(self) -> str:
        return self.loaded_package.package.name

    @property
    def version(self) -> str:
        return self.loaded_package.package.version

    @property
    def unique_src_location(self) -> UniquePackageLocation:
        return UniquePackageLocation(self.package_name, self.version, self.source_repo.base_url())

    @property
    def unique_dest_location(self) -> UniquePackageLocation:
        return UniquePackageLocation(self.package_name, self.version, self.destination_repo.base_url())

    @property
    def needs_mirroring(self) -> bool:
        """
        Check if the package needs to be mirrored. Does not take the value of has_been_mirrored into account.
        """
        return self.source_repo != self.destination_repo and self.existing_package is None and not self.is_marked_as_overlapped

    @property
    def is_marked_as_overlapped(self):
        return self._overlapping_package_mirrored is not None

    def mark_package_as_overlapped_by(self, mirrored_package: "_PackageToMirror"):
        self._overlapping_package_mirrored = mirrored_package


@dataclasses.dataclass(frozen=True, eq=True)
class _UniqueDependencyID:
    """
    Class to identify a dependency within one package unambigously. Neither the install path nor the package name/version
    by themselves are unambiguous for an install location in a package.
    """

    install_path: str
    package_name: str

    @staticmethod
    def from_dep(d: Dependency):
        return _UniqueDependencyID(install_path=d.relative_destination, package_name=d.package)


class _MirrorContext:
    def __init__(
        self,
        safe_package_to_repo_callback: Callable[[UniquePackageLocation], Awaitable[str]],
        mirror_tags: bool,
        context: CommandParameters,
        scheduler: SchedulerContext,
    ):
        """
        Set up the context for the mirror operation.

        :param safe_package_to_repo_callback: Callback to determine target repository per package. The callback must return a non-empty "str" otherwise the behaviour is undefined.
        :param mirror_tags: Whether to mirror tags
        :param context: Command context
        :param scheduler: Scheduler context
        """
        self._safe_package_to_repo_callback = safe_package_to_repo_callback
        self._mirror_tags = mirror_tags
        self._context = context
        self._scheduler = scheduler

    @property
    def safe_package_to_repo_callback(self) -> Callable[[UniquePackageLocation], Awaitable[str]]:
        return self._safe_package_to_repo_callback

    @property
    def mirror_tags(self) -> bool:
        return self._mirror_tags

    @property
    def context(self) -> CommandParameters:
        return self._context

    @property
    def scheduler(self) -> SchedulerContext:
        return self._scheduler
