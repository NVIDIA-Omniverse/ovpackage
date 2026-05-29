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
    List,
    Tuple,
)
from urllib.parse import urlparse

import semver

import wrapp
from wrapp import (
    CommandParameters,
)


def calculate_next_version(versions: List[str], version_part: str) -> Tuple[str, str]:
    """
    Given a list of strings with version numbers and the version part to increment (major, minor, patch), returns a pair of latest version and the next version
    """
    sem_versions = [semver.Version.parse(v) for v in versions]
    latest_version = sorted(sem_versions)[-1]
    return str(latest_version), str(latest_version.next_version(version_part))


async def create_new_version(
    root_url: str, package_name: str, repository: str, scheduler: wrapp.SchedulerContext, context: CommandParameters()
) -> None:
    """
    Example function that creates a package in omniverse://localhost/.packages, calculating the next minor semver version from listing the existing packages
    in that repository. If no package is found, it starts with 1.0.0. This uses the semver package to parse version numbers and calculate the next minor version.

    :param root_url: URL of source package
    :param package_name: name to give to the package.
    :param repository: URL of the repository
    :param scheduler: pre-constructed SchedulerContext object determining the amount of jobs to run
    :param context: command context to use
    """
    # Calculate the next version number
    packages_in_repo = await wrapp.list_repo(repository, scheduler=scheduler)
    next_version = None
    for package, versions in packages_in_repo:
        parsed_url = urlparse(package)
        path_parts = parsed_url.path.split("/")
        if path_parts[-1] == package_name:
            if len(versions) > 0:
                latest_version, next_version = calculate_next_version(versions, "minor")
                print(f"Latest version of package {package_name} is {latest_version}, using {next_version}")
                break
    if next_version is None:
        print(f"No prior version found for {package_name}, starting at 1.0.0")
        next_version = "1.0.0"

    print(f"Creating package {package_name} version {next_version}")
    await wrapp.create(package_name, next_version, source=root_url, catalog=False, repo=repository, context=context, scheduler=scheduler)
    print("Done")
