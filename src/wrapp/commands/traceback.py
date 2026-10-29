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
import sys
from enum import (
    Enum,
    auto,
)
from typing import (
    List,
    NamedTuple,
    Optional,
    Set,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.package import (
    PackageInfo,
    load_package_setup,
    repository_from_wrapp_file_path,
    wrapp_file_in_repo,
    wrapp_file_path,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    InteractiveAuthFlag,
    JsonLoggingFlag,
    LogFileOption,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.repo.repository import Repository
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.storage import (
    wrapp_url_exists,
    wrapp_urls_are_equivalent,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_base_directory_from_url,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@wrapp_public_api
class PackageLocationType(Enum):
    """
    Enum to classify the location of the wrapp file in a traceback chain

    Values:
        REPOSITORY - the wrapp file is located in a repository (a subfolder with version and package name within a .packages hierarchy)
        SOURCE - the wrapp file is located in a non-repository folder
        ERROR - the data contains a validation error, and it can not be correctly classified
    """

    REPOSITORY = auto()
    SOURCE = auto()
    ERROR = auto()


@wrapp_public_api
class TracebackNode:
    """
    Represents a node in the traceback tree.

    Attributes:
        package_location_type: The type of package location, defined by the PackageLocationType enum
        path: The path to the wrapp file
        message: Human readable description of the wrapp file location found
        source: Optional pointer to the TracebackNode pointed to by the source field
        repository: Optional pointer to the TracebackNode pointed to by the repository field
    """

    def __init__(
        self,
        package_location_type: PackageLocationType,
        path: str,
        message: str = "",
        source: Optional["TracebackNode"] = None,
        repository: Optional["TracebackNode"] = None,
    ):
        self.package_location_type = package_location_type
        self.path = path
        self.message = message
        self.source = source
        self.repository = repository


class _TreeConnectors(NamedTuple):
    branch: str
    last_branch: str
    vertical: str


_UNICODE_TREE_CONNECTORS = _TreeConnectors(branch="├── ", last_branch="└── ", vertical="│   ")
_ASCII_TREE_CONNECTORS = _TreeConnectors(branch="+-- ", last_branch="`-- ", vertical="|   ")


def _traceback_tree_connectors() -> _TreeConnectors:
    encoding = getattr(sys.stdout, "encoding", None)
    if not encoding:
        return _UNICODE_TREE_CONNECTORS

    try:
        "".join(_UNICODE_TREE_CONNECTORS).encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return _ASCII_TREE_CONNECTORS

    return _UNICODE_TREE_CONNECTORS


def print_traceback_tree(
    node: TracebackNode,
    indent: int = 0,
    is_last: bool = True,
    prefix: str = "",
    connectors: Optional[_TreeConnectors] = None,
):
    """
    Print a traceback tree with proper indentation and branch indicators.

    Args:
        node: The TracebackNode to print
        indent: Current indentation level
        is_last: Whether this node is the last child of its parent
        prefix: Prefix string for the current line
    """
    if connectors is None:
        connectors = _traceback_tree_connectors()

    # Print the current node
    if indent == 0:
        print(node.message)
    else:
        # Child nodes with proper indentation
        branch = connectors.last_branch if is_last else connectors.branch
        print(f"{prefix}{branch} {node.message}")

    # Prepare the prefix for children
    if indent > 0:
        new_prefix = prefix + ("    " if is_last else connectors.vertical)
    else:
        new_prefix = "    "

    # Print children
    if node.repository:
        print_traceback_tree(node.repository, indent + 1, node.source is None, new_prefix, connectors)
    if node.source:
        print_traceback_tree(node.source, indent + 1, True, new_prefix, connectors)


@app.command(name="traceback")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def traceback_command(
    wrapp_file: str = typer.Argument(..., help="source of wrapp file to traceback"),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    log_file: str = LogFileOption,
    verbose: bool = VerboseFlag,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
):
    """
    Print the traceback tree of a wrapp file, showing its heritage via source and repository fields
    """
    context, _ = context_from_params()
    try:
        trace_tree = await traceback(wrapp_file)
        print_traceback_tree(trace_tree)
    except FailedCommand as e:
        logger.error(f"Failure to traceback file '{wrapp_file}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while running traceback '{wrapp_file}': {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@wrapp_public_api
@log_args
async def traceback(wrapp_file: str, visited_paths: Optional[Set[str]] = None) -> TracebackNode:
    """
    Build a traceback tree for a wrapp file, following both source and repository paths.

    Args:
        wrapp_file: Path to the wrapp file to traceback
        visited_paths: Set of already visited paths to prevent circular references

    Returns:
        A TracebackNode representing the root of the traceback tree
    """
    validate_non_empty_string("wrapp_file", wrapp_file)
    if visited_paths is not None and not isinstance(visited_paths, set):
        raise FailedCommand(f"visited_paths must be a set, got {type(visited_paths).__name__}")

    # Initialize visited paths set if not provided
    if visited_paths is None:
        visited_paths = set()

    # Check for circular references
    if wrapp_file in visited_paths:
        message = f"Circular reference detected: {wrapp_file} has already been visited"
        logger.warning(message)
        return TracebackNode(PackageLocationType.ERROR, path=wrapp_file, message=message)

    # Add current path to visited paths
    visited_paths.add(wrapp_file)

    self_repository: Optional[str] = repository_from_wrapp_file_path(wrapp_file)
    package: Optional[PackageInfo] = await load_package_setup(wrapp_file)

    if not package:
        if self_repository:
            message = f"Could not find wrapp file in repository at {wrapp_file}"
            logger.warning(message)
            return TracebackNode(PackageLocationType.ERROR, path=wrapp_file, message=message)
        else:
            wrapp_dir = get_base_directory_from_url(wrapp_file)
            if await wrapp_url_exists(wrapp_dir):
                return TracebackNode(
                    PackageLocationType.SOURCE, path=wrapp_dir, message=f"Primary source folder (no package file) {wrapp_dir}"
                )
            else:
                message = f"Source package missing: Could not find wrapp file at {wrapp_file}"
                logger.warning(message)
                return TracebackNode(PackageLocationType.ERROR, path=wrapp_file, message=message)

    if self_repository:
        # A repository package's name/version is its structural identity: the wrapp file must live
        # at the path matching that identity. A path-vs-identity mismatch is genuine corruption,
        # regardless of the repository/source pointers.
        expected_wrapp_file = wrapp_file_in_repo(Repository(repo_url=self_repository), package.name, package.version)
        if not await wrapp_urls_are_equivalent(expected_wrapp_file, wrapp_file):
            message = (
                f"Package metadata at {wrapp_file} does not match its repository path "
                f"(metadata declares {package.name} {package.version})"
            )
            logger.warning(message)
            return TracebackNode(PackageLocationType.ERROR, path=wrapp_file, message=message)

    node_type = None
    source_node = None
    repo_node = None
    message = "unknown"
    if package.repository:
        if self_repository and await wrapp_urls_are_equivalent(package.repository, self_repository):
            # This is the root repository, no more mirrors to follow. Let's report on the source!
            message = f"Primary repository at {wrapp_file}"
            logger.info(message)
            node_type = PackageLocationType.REPOSITORY
        else:
            if self_repository:
                message = f"Mirror repository at {wrapp_file}"
                logger.info(message)
                node_type = PackageLocationType.REPOSITORY

            # Follow repository path
            repo = Repository(repo_url=package.repository)
            expected_wrapp_file = wrapp_file_in_repo(repo, package.name, package.version)
            repo_node = await traceback(expected_wrapp_file, visited_paths.copy())

    if package.source:
        # When the package is not in a repository, it is either a primary source or an installed package
        if not node_type:
            if package.repository:
                message = f"Intermediate source installed at {wrapp_file}"
                logger.info(message)
                node_type = PackageLocationType.SOURCE
            else:
                message = f"Source without repository pointer at {wrapp_file}"
                logger.warning(message)
                node_type = PackageLocationType.ERROR

        expected_wrapp_file = wrapp_file_path(package.source, package.name)
        source_node = await traceback(expected_wrapp_file, visited_paths.copy())
    else:
        if not node_type:
            if self_repository:
                message = f"Primary repository at {wrapp_file}"
                logger.info(message)
                node_type = PackageLocationType.REPOSITORY
            else:
                message = f"Primary source at {wrapp_file}"
                logger.info(message)
                node_type = PackageLocationType.SOURCE

    return TracebackNode(package_location_type=node_type, path=wrapp_file, source=source_node, repository=repo_node, message=message)
