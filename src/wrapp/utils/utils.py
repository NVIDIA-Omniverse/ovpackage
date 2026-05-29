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

import logging
import os
import random
import re
import string
from typing import (
    TYPE_CHECKING,
    List,
    Optional,
)
from urllib.parse import (
    urlparse,
    urlsplit,
    urlunparse,
    urlunsplit,
)

from .api_marker import wrapp_public_api
from .storage_interface import (
    ListEntry,
    Result,
)

# We need the type "Patch" for the type annotations on FailedPatchCommand,
# but simply importing it would create circular dependencies.
if TYPE_CHECKING:
    from wrapp.datastructures.patch import Patch

logger = logging.getLogger("omni.wrapp")


def parse_env_int(env_var: str, default: int) -> int:
    """Parse an environment variable as a positive integer, falling back to *default*."""
    raw = os.environ.get(env_var)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(f"Ignoring invalid {env_var}={raw!r} (not an integer), using default {default}")
        return default
    if value <= 0:
        logger.warning(f"Ignoring non-positive {env_var}={value}, using default {default}")
        return default
    return value


@wrapp_public_api
class StorageOperationError(Exception):
    """The StorageOperationError indicates a potentially intermittent failure of the storage, but could also indicate missing authorization."""

    def __init__(self, msg: str, result: Result = Result.ERROR):
        super().__init__(msg)
        self.result = result


class StorageInitializationError(Exception):
    def __init__(self, msg: str, result: Result = Result.ERROR):
        super().__init__(msg)
        self.result = result


class StorageMissingDependenciesError(StorageInitializationError):
    """Error raised when a storage implementation is missing required dependencies."""

    def __init__(self, msg: str):
        super().__init__(msg)


@wrapp_public_api
class FailedCommand(Exception):
    """The FailedCommand indicates a command has failed due to business logic, and should not be retried unmodified."""

    pass


# Inherits from FailedCommand so that CLI commands catch it via their
# standard ``except FailedCommand`` handler and exit cleanly instead of
# printing a traceback. Inherits from RuntimeError for backwards
# compatibility with wrapp.initialize() callers that catch RuntimeError.
@wrapp_public_api
class InvalidConfigurationError(FailedCommand, RuntimeError):
    """Raised when a user-provided configuration file (e.g. ``wrapp.toml``) is malformed or contains invalid values."""

    pass


@wrapp_public_api
class FailedCommandValueError(FailedCommand, ValueError):
    """Raised when a parameter has an invalid value (e.g., empty string for required parameter).

    Inherits from both FailedCommand (for backwards compatibility) and ValueError (for semantics).
    """

    def __init__(self, parameter_name: str, value, message: Optional[str] = None):
        self.parameter_name = parameter_name
        self.value = value
        if message is None:
            message = f"Invalid value for parameter '{parameter_name}': {value!r}"
        super().__init__(message)


def validate_non_empty_string(parameter_name: str, value: str) -> None:
    """Validate that a string parameter is not empty or whitespace-only.

    Raises FailedCommandValueError if the value is empty or contains only whitespace.
    """
    if not value or not value.strip():
        raise FailedCommandValueError(parameter_name, value, f"{parameter_name} cannot be empty")


class FailedPatchCommand(FailedCommand):
    def __init__(self, msg: str, patch: "Patch"):
        super().__init__(msg)
        self.patch = patch


@wrapp_public_api
class FileParseException(FailedCommand):
    """Exception raised when parsing a file during load failed."""

    pass


class MicroServiceOperationError(Exception):
    pass


def is_folder(item: ListEntry) -> bool:
    return item.is_folder


def is_regular_file(item: ListEntry) -> bool:
    return item.is_regular_file


def drop_trailing_slashes(path: str) -> str:
    # Count the trailing slashes
    trailing_slash_count = 0
    for char in reversed(path):
        if char in ("/", "\\"):
            trailing_slash_count += 1
        else:
            break
    # A single '/' or '\' is a valid path indicating a root folder. So never remove all characters
    to_remove = min(len(path) - 1, trailing_slash_count)
    return path if to_remove == 0 else path[:-to_remove]


def normalize_path(path: str) -> str:
    return drop_trailing_slashes(path).replace("\\", "/")


def paths_are_equal(path1: str, path2: str):
    path1 = normalize_path(path1)
    path2 = normalize_path(path2)
    return path1 == path2


def has_checkpoint(item: ListEntry) -> bool:
    return item.has_checkpoint


def join_paths(*path_segments) -> str:
    """
    For Windows, make sure not to have backslashes in our URLs
    """
    return str(os.path.join(*path_segments).replace(os.sep, "/"))


def format_size(size: Optional[float]) -> str:
    if size is None:
        return "unknown"
    else:
        n = 0
        label = ["", "K", "M", "G", "T", "P"]
        while size > 2**10:
            size /= 2**10
            n += 1
        if n > 0:
            return f"{size:.2f} {label[n]}Bytes"
        else:
            return f"{size:.0f} {label[n]}Bytes"


def drop_parameters_from_url(url: str) -> str:
    parsed_url = urlparse(url)
    return urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", "", ""))


def get_path_from_url(url: str) -> str:
    if is_windows_path(url):
        return url

    parsed_url = urlparse(url)
    path = parsed_url.path

    if parsed_url.scheme == "file":
        netloc = parsed_url.netloc
        if netloc and netloc.lower() != "localhost":
            if len(netloc) == 2 and netloc[1] == ":":
                # file://C:/path -> netloc is drive, path is /path
                return f"{netloc}{path}"
            if path:
                # UNC path form: file://server/share -> //server/share
                return f"//{netloc}{path}"
            # Legacy/non-canonical Windows style: file://C:\path\to\file
            return netloc

        # On Windows, file:///C:/path is parsed as path="/C:/path" by urlparse.
        # Strip the leading "/" so we get a valid Windows path like "C:/path".
        if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
            path = path[1:]

    return path


def get_base_directory_from_url(url: str) -> str:
    if is_windows_path(url):
        return os.path.split(url)[0]
    parsed_url = urlparse(url)
    path_parts = parsed_url.path.split("/")
    base_path = "/".join(path_parts[:-1])  # Exclude the last component (filename)
    if not base_path and is_file_url(url):
        base_path = "/"  # required for file:// (incorrect) vs file:/// (correct)

    base_parts = parsed_url._replace(path=base_path, params="", query="", fragment="")
    base_url = urlunparse(base_parts)
    return str(base_url)


def get_base_from_absolute_url(absolute_url: str, relative_url: str) -> str:
    """Extract the base portion of an absolute URL up to where the relative URL portion begins.

    Args:
        absolute_url: The full absolute URL
        relative_url: The relative portion to find within the absolute URL

    Returns:
        The base portion of the absolute URL

    Raises:
        ValueError: If relative_url is not found in absolute_url
    """
    if not relative_url:
        raise ValueError("Need to specify relative url, got empty string")
    if not absolute_url:
        raise ValueError("Need to specify absolute url, got empty string")
    index = absolute_url.find(relative_url)
    if index == -1:
        raise ValueError(f"Relative URL '{relative_url}' not found in absolute URL '{absolute_url}'")
    return absolute_url[:index]


def drop_query_params_fragments(url: str) -> str:
    """Remove query parameters and fragments from a URL.

    Args:
        url: The URL to clean

    Returns:
        The URL without query parameters and fragments
    """
    if is_windows_path(url):
        return url
    parsed_url = urlparse(url)
    base_path = parsed_url.path
    if not base_path and is_file_url(url):
        base_path = "/"  # required for file:// (incorrect) vs file:/// (correct)
    base_parts = parsed_url._replace(path=base_path, params="", query="", fragment="")
    base_url = urlunparse(base_parts)
    return str(base_url)


def get_filename_from_url(url: str) -> str:
    if is_windows_path(url) or is_relative_path(url):
        return os.path.basename(normalize_path(url))
    else:
        parsed_url = urlparse(url)
        path_parts = parsed_url.path.split("/")
        return path_parts[-1]


def is_windows_path(url: str) -> bool:
    windows_path_regex = re.compile(r"^(?:[A-Za-z]:|[\\/]{2}[^?\\/:*<>|]+[\\/][^?\\/:*<>|]+)(?:[\\/][^?\\/:*<>|]+)*[\\/]?$", re.IGNORECASE)
    return windows_path_regex.match(url) is not None


def is_file_url(url: str) -> bool:
    parsed_url = urlparse(url)
    return parsed_url.scheme == "file" or parsed_url.scheme == "" or is_windows_path(url)


def is_nucleus_url(url: str) -> bool:
    return urlparse(url).scheme in ["omniverse", "omni"]


def is_wrapp_file_url(url: str) -> bool:
    if is_windows_path(url):
        _, ext = os.path.splitext(url)
    else:
        parsed_url = urlparse(url)
        _, ext = os.path.splitext(parsed_url.path)
    last_ext = ext.split(".")[-1]
    return last_ext == "wrapp"


def is_relative_path(path_or_url: str) -> bool:
    if urlparse(path_or_url).scheme:
        # No such thing as a relative URL with scheme
        return False
    # A file URL without scheme => a path
    return not os.path.isabs(path_or_url)


def normalize_url(path_or_url: str) -> str:
    if is_windows_path(path_or_url):
        return os.path.normpath(normalize_path(path_or_url)).replace("\\", "/").rstrip("/")
    parsed_url = urlsplit(path_or_url)
    if parsed_url.scheme == "":
        # No scheme, this must be treated as an absolute or relative file path
        return os.path.normpath(path_or_url).replace("\\", "/").rstrip("/")
    normalized_path = os.path.normpath(parsed_url.path).replace("\\", "/")
    if normalized_path == ".":
        normalized_path = ""
    return urlunsplit((parsed_url.scheme, parsed_url.netloc, normalized_path, parsed_url.query, parsed_url.fragment))


def is_path(string: str) -> bool:
    return bool(os.path.dirname(string))


def is_valid_file_or_directory_name(name: str) -> bool:
    forbidden_chars = ["<", ">", ":", '"', "/", "\\", "|", "?", "*", " ", "\0"]

    if any(char in name for char in forbidden_chars):
        return False
    if name.strip() == "":
        return False
    if len(name) > 255:
        return False
    return True


# If the URL is a file URL (or just a path without scheme), normalize it to an absolute file URL with scheme
def make_file_url_absolute(url: str) -> str:
    if is_windows_path(url):
        # On Windows, urlparse misinterprets drive letters (e.g. C:\path) as URL schemes,
        # so we must convert the original path directly.
        abs_path = os.path.abspath(url).replace("\\", "/")
        return "file:///" + abs_path
    parsed_url = urlparse(url)
    if (parsed_url.scheme and parsed_url.scheme != "file") or parsed_url.netloc or not parsed_url.path:
        return url
    path = parsed_url.path
    # On Windows, file:///C:/path is parsed as path="/C:/path". Strip the leading "/"
    # before os.path.abspath, which otherwise treats it as root-relative on the current
    # drive and duplicates the drive letter (e.g. /C:/foo -> C:\C:\foo).
    if len(path) >= 3 and path[0] == "/" and path[1].isalpha() and path[2] == ":":
        path = path[1:]
    # On Windows, os.path.abspath returns backslashes, but file URLs must use forward slashes
    abs_path = os.path.abspath(path).replace("\\", "/")
    if not abs_path.startswith("/"):
        abs_path = "/" + abs_path
    parsed_url = parsed_url._replace(path=abs_path, scheme="file", netloc="")
    return str(urlunparse(parsed_url))


def make_relative_url(base_url: str, url: str) -> str:
    parsed_base = urlparse(base_url.replace("\\", "/"))
    parsed_url = urlparse(url.replace("\\", "/"))

    if parsed_base.netloc == parsed_url.netloc:
        if parsed_url.path.startswith(parsed_base.path.rstrip("/") + "/"):
            relative_path = parsed_url._replace(scheme="", netloc="", path=parsed_url.path[len(parsed_base.path.rstrip("/")) + 1 :])
            return urlunparse(relative_path).lstrip("/")

        # Handle the case where the URL is exactly the base URL
        if parsed_url.path == parsed_base.path.rstrip("/"):
            relative_path = parsed_url._replace(scheme="", netloc="", path="")
            return urlunparse(relative_path).lstrip("/")

    return url  # Return the original URL if it is not relative to the base URL


def all_parent_directories(url: str) -> List[str]:
    result: List[str] = []
    root = get_base_directory_from_url(url)
    while root:
        result.append(root)
        if get_path_from_url(root) in ["", "/"]:
            # Done, that was root
            break
        # One directory upwards
        root = get_base_directory_from_url(root)
    return result


def random_string(k: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=k))
