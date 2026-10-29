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

import fnmatch
import logging
from enum import Enum
from typing import (
    List,
    Optional,
)

import typer
from pydantic import BaseModel
from rich.console import Console

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    LsOutputFormat,
    RecursiveOption,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage import (
    Result,
    Router,
    wrapp_list_directory,
    wrapp_list_file_tree,
    wrapp_stat,
)
from wrapp.utils.storage_interface import ListEntry
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    drop_query_params_fragments,
    format_size,
    get_base_directory_from_url,
    is_folder,
    is_regular_file,
    join_url_path,
    make_relative_url,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="ls")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def ls_command(
    url: str = typer.Argument(...),
    debug: bool = DebugLevelFlag,
    recursive: bool = RecursiveOption,
    pattern: List[str] = typer.Option(
        [],
        "--pattern",
        "-p",
        help="Specify file matching pattern to filter which files are listed. Use wildcards, escape them with backslash to prevent shell expansion. "
        "Can be specified multiple times for either or matches.  '*' matches everything (including directory separators), '?' a single character, "
        "'[seq]' any character in the seq, and '[!seq]' any character not in seq. Case sensitive.",
    ),
    output_format: LsOutputFormat = typer.Option(
        LsOutputFormat.normal,
        "--output-format",
        "--of",
        case_sensitive=False,
        help="Specify the output format of the command",
    ),
    with_hash: bool = typer.Option(
        False,
        "--with-hash",
        help="Issue an extra metadata fetch (stat) per file so the Hash column "
        "is populated. Required for backends whose list response omits the "
        "nucleus hash (e.g. Storage API, where hashes live in a separate "
        "Metadata service). Adds one round-trip per listed file. "
        "Fails if a hash cannot be retrieved.",
    ),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
):
    """
    Given an url, list - optionally recursively - all content in that subtree matching any of the given patterns or all files if no pattern is given.
    Specify TSV as output format to produce a result suited for catalog --file-list.
    Specify long-tsv to produce tab-separated output matching the normal output columns.
    """

    context, scheduler_params = context_from_params()
    try:
        # TODO reenable this when we release the cd command
        # if url is None:
        #     if "WRAPP_CWD" not in os.environ or os.environ["WRAPP_CWD"] is None:
        #         raise FailedCommand("No current working directory set, use cd first or set the environment variable WRAPP_CWD!")
        #     url = os.environ["WRAPP_CWD"]

        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await ls(
                url,
                url,
                recursive,
                pattern,
                output_format,
                print_to_console=True,
                with_hash=with_hash,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        print(f"Failed to ls: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        print(f"Error while listing {url}: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


class LsResultItemType(str, Enum):
    """Designated the type of the item that can be produced by a list"""

    file = "file"
    folder = "folder"


@wrapp_public_api
class LsResultItem(BaseModel):
    """TheLsResultItem represents a single directory entry returned by the ls command, and it could be a file or a folder."""

    source_path: str  #: An absolute URL pointing to the content of the item at the time of catalog creation
    relative_path: str  #: The primary key in the catalog, the location of the item relative to the root of the catalog
    item_type: LsResultItemType  #: Designating the item as a file or folder
    hash: Optional[str] = None  #: A content-based hash. For comparison, be aware there are multiple algorithms around.
    size: Optional[int] = None  #: The size of the item. None if the item is a folder.


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def ls(
    base_url: str,
    url: str,
    recursive: bool,
    pattern: Optional[List[str]] = None,
    output_format: LsOutputFormat = LsOutputFormat.normal,
    print_to_console: bool = False,
    *,
    with_hash: bool = False,
    scheduler: Optional[SchedulerContext] = None,
) -> List[LsResultItem]:
    """
    Given a base_url and an url within that subtree, list - optionally recursively - all content in that subtree matching any of the given patterns or all
    if no pattern is given. Specify print==False to collect the items in a result set instead

    :param base_url: The base url of all items that will be output as the first column if TSV format is specified (for the --file-list of catalog)
    :param url: The URL to list
    :param recursive: Whether to stop after the directory or do a recursive traversal
    :param pattern: List of patterns used with fnmatch() to check if an item is part of the output set. At least one pattern should match for inclusion. Case sensitive!
    :param output_format: One of the possible output formats
    :param print_to_console: Whether to print the result instead of collecting and returning a list
    :param with_hash: When True, issue an extra stat per file so the nucleus hash is populated even on backends whose list response omits it (e.g. Storage API). Adds one round-trip per file.
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :return: The List of items discovered, empty if print is specified
    :raises FailedCommandValueError: When a required parameter is empty
    """
    # Note: base_url can be empty when listing a single file directly
    validate_non_empty_string("url", url)

    if scheduler is None:
        raise RuntimeError("Failed to create scheduler for ls()")

    result, item = await wrapp_stat(url)
    if result == Result.ERROR_NOT_FOUND:
        raise FailedCommand(f"'{url}' not found")
    if item is None:
        raise FailedCommand(f"Could not list items for '{url}', error code returned was {result}")
    if is_regular_file(item) and (not base_url or base_url == url):
        # Super special case: on the command line a user specified a file as the URL to list (and possibly included a pattern),
        # or an API caller specified a file as the URL to list but no base_url.
        # We don't want the code downstream to call wrapp_list_directory for files, instead list the base directory
        # and use the filename as a pattern. And if there is an actual pattern, check if the filename matches the pattern.
        base_url = get_base_directory_from_url(url)
        if pattern and not any([fnmatch.fnmatchcase(url, p) for p in pattern]):
            return []
        if base_url is not None:
            filename = url[len(base_url) + 1 :]
        else:
            filename = url
        pattern = [drop_query_params_fragments(filename)]
        url = base_url
    if pattern:
        pattern = [p.replace("\\*", "*") for p in pattern]

    lister = Lister(
        base_url,
        recursive,
        pattern,
        output_format,
        print_to_console=print_to_console,
        with_hash=with_hash,
        scheduler=scheduler,
    )
    return await lister.list(url)


class Lister:
    def __init__(
        self,
        base_url: str,
        recursive: bool,
        pattern: Optional[List[str]],
        output_format: LsOutputFormat,
        print_to_console: bool,
        scheduler: SchedulerContext,
        with_hash: bool = False,
    ):
        self.base_url = base_url
        self.recursive = recursive
        self.pattern = pattern
        self.output_format = output_format
        self.print_to_console = print_to_console
        self.with_hash = with_hash
        self.parent_task_group = scheduler.parent_task_node()
        if self.print_to_console:
            self.console = Console()
            header = ("Modified", "Type", "Size", "URL", "Hash", "Version")
            if output_format == LsOutputFormat.normal:
                self.console.print(*header, sep="\t", soft_wrap=True)
            elif output_format == LsOutputFormat.long_tsv:
                print("\t".join(header))
        self.result_list: List[LsResultItem] = []
        # When --with-hash is set we need every metadata fetch to finish before
        # printing, otherwise the Hash column is wrong on backends whose list
        # response omits it. We collect (item, full_path) tuples here and
        # flush them in list() after the task group has joined.
        self._deferred_print: List[tuple] = []

    async def list(self, url):
        async with self.parent_task_group.create_task_group_node(f"list '{url}'") as task_node:
            task_node.create_task(self._list_internal(task_node, url), name=f"list '{url}'")
        self.result_list.sort(key=lambda r: r.source_path)  # We want to source the return value, the entries might be in arbitrary order
        if self.with_hash and self.print_to_console and self._deferred_print:
            self._deferred_print.sort(key=lambda t: t[1])
            for item, full_path in self._deferred_print:
                self._print_item_row(item, full_path)
        return self.result_list

    async def _list_internal(self, task_node: SchedulerTaskGroupNode, url: str):
        if self.recursive and Router.me().storage_for_read(url).supports_list_file_tree():
            items_generator = wrapp_list_file_tree(url)
            manual_recursive = False
        else:
            items_generator = wrapp_list_directory(url)
            manual_recursive = True

        current_folder = url
        try:
            async for item in items_generator:
                full_path = join_url_path(current_folder, item.relative_path)
                if is_folder(item) and self.recursive and manual_recursive:
                    # schedule folder recursion
                    task_node.create_task(self._list_internal(task_node, full_path), name="List directory")
                self._process_item(task_node, item, full_path)
        except StorageOperationError as e:
            if e.result in [Result.ERROR_NOT_FOUND, Result.ERROR_WRONG_TYPE]:
                # Could be a file and not a folder, try a stat
                stat_result, item = await wrapp_stat(url)
                if stat_result == Result.OK:
                    # Special case of listing a file
                    self._process_item(task_node, item, url)
                else:
                    raise StorageOperationError(f"Failed to list/stat {url}: {e}") from e
            elif e.result != Result.OK:
                raise StorageOperationError(f"Failed to list {url}: {e}") from e

    async def _enrich_with_hash(self, item: ListEntry, result_item: "LsResultItem", full_path: str) -> None:
        """Fetch nucleus_hash for *item* via stat() and patch it onto both the
        ListEntry (so deferred printing sees it) and the LsResultItem (so the
        returned list carries it).

        Raises ``StorageOperationError`` when the hash cannot be obtained,
        because the caller explicitly requested ``--with-hash``.
        """
        stat_result, stat_item = await wrapp_stat(full_path)
        if stat_result != Result.OK or stat_item is None:
            raise StorageOperationError(f"stat failed for '{full_path}' (result={stat_result})")
        hash_value = stat_item.nucleus_hash
        if not hash_value:
            raise StorageOperationError(f"backend returned no hash for '{full_path}'")
        result_item.hash = hash_value
        try:
            item.nucleus_hash = hash_value
        except Exception as e:
            # Some ListEntry implementations reject re-setting; harmless here.
            logger.debug("Could not set nucleus_hash on ListEntry for %s: %s", full_path, e)

    def _process_item(self, task_node: SchedulerTaskGroupNode, item: ListEntry, full_path: str):
        # Decide whether to output this item or not
        relative_url = make_relative_url(self.base_url, full_path)
        if self.pattern and not any([fnmatch.fnmatchcase(relative_url, p) for p in self.pattern]):
            return

        result_item = LsResultItem(
            source_path=full_path,
            relative_path=relative_url,
            item_type=LsResultItemType.folder if is_folder(item) else LsResultItemType.file,
            size=item.size,
            hash=item.nucleus_hash,
        )
        self.result_list.append(result_item)

        # Schedule a metadata enrichment task on the same task group when the
        # caller asked for hashes and the list response didn't already supply
        # one. Files only — folders never carry a content hash.
        if self.with_hash and is_regular_file(item) and item.nucleus_hash is None:
            task_node.create_task(
                self._enrich_with_hash(item, result_item, full_path),
                name="ls fetch metadata",
            )

        if not self.print_to_console:
            return

        if self.with_hash:
            # Defer printing to list() so the Hash column reflects enrichment.
            self._deferred_print.append((item, full_path))
            return

        self._print_item_row(item, full_path)

    def _print_item_row(self, item: ListEntry, full_path: str) -> None:
        relative_to_base = make_relative_url(self.base_url, full_path)
        item_type = "folder" if is_folder(item) else "file" if is_regular_file(item) else "special"
        row = (
            item.modified_time if item_type == "file" else "",
            item_type,
            format_size(item.size) if item_type == "file" else "",
            full_path,
            item.nucleus_hash,
            item.version,
        )
        if self.output_format == LsOutputFormat.TSV:
            print(f"{self.base_url}\t{relative_to_base}")
        elif self.output_format == LsOutputFormat.long_tsv:
            print("\t".join("" if value is None else str(value) for value in row))
        elif self.output_format == LsOutputFormat.short:
            print(full_path)
        elif self.output_format == LsOutputFormat.normal:
            self.console.print(*row, sep="\t", soft_wrap=True)
        else:
            raise Exception(f"Invalid argument to ls: {self.output_format} format is unknown")
