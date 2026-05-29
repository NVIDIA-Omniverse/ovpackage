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
from urllib.parse import (
    urlparse,
    urlunparse,
)

from wrapp.utils.storage import (
    wrapp_stat,
    wrapp_url_exists,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    FailedCommand,
    is_file_url,
    is_relative_path,
    join_paths,
    make_file_url_absolute,
)


class Repository:
    def __init__(self, *, repo_url: Optional[str] = None, fallback_base: Optional[str] = None):
        """
        Given either a verbatim repository URL (which can contain the final .packages, it will not double append .packages) or a fallback base, construct
        an object that can be used to calculate package paths for packages with names and version within this repository.

        If the repo_url is not None it will be used, else the fallback_base is used to construct a repository within the root of that URL.
        You can omit the repo_url parameter and specify only a fallback_base if it is known upfront that it will be None

        An exception is thrown if repo_url is None and no fallback_base is given.
        """
        if repo_url is not None:
            if repo_url.endswith("/"):
                repo_url = repo_url[:-1]
                if repo_url.endswith("/"):
                    raise FailedCommand(f"Repo URL ending with two slashes or more: '{repo_url}/'")
            if repo_url.endswith(".packages"):
                self._url = str(repo_url[: -len(".packages")])
            else:
                self._url = repo_url
            # Convert relative file paths to absolute paths to ensure consistent behavior
            # across platforms (fixes OMPE-79544: relative paths fail on Windows)
            if is_file_url(self._url) and is_relative_path(self._url):
                self._url = make_file_url_absolute(self._url)
        elif fallback_base is not None:
            # If no repo_url is specified, the default repository is the same server as the base url
            source_url = urlparse(fallback_base)
            self._url = str(urlunparse((source_url.scheme, source_url.netloc, "/", None, None, None)))
        else:
            raise Exception("You need to specify either repo_url or fallback_base or both!")

    def __eq__(self, other):
        if not isinstance(other, Repository):
            return NotImplemented
        return self._url == other._url

    def __hash__(self):
        return hash(self._url)

    def package_url(self, package_name: str, version: str) -> str:
        return join_paths(self._url, ".packages", package_name, version)

    def package_cache_url(self) -> str:
        return join_paths(self._url, ".packages")

    def base_url(self) -> str:
        return self._url

    def is_http_url(self) -> bool:
        """Check if the repository URL is HTTP/HTTPS based."""
        parsed = urlparse(self._url)
        return parsed.scheme in ("http", "https")

    async def find_package(self, package_name: str, package_version) -> bool:
        wrapp_file = f".{package_name}.wrapp"
        wrapp_file_url = join_paths(self.package_url(package_name, package_version), wrapp_file)
        return await wrapp_url_exists(wrapp_file_url)

    def __repr__(self):
        url = getattr(self, "_url", None)
        return f"Repository(url={url!r})"
