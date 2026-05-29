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

import json
import logging
from copy import copy
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.two_way_diff import Diff
from wrapp.options import (
    AuthOption,
    IgnoreTagsFlag,
    InteractiveAuthFlag,
    LogFileOption,
    ShowFlag,
    StatsFlag,
    TimeFlag,
    UniqueShowFlag,
    VerboseFlag,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.coro import async_typer
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="diff")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def diff_command(
    catalog_a: str = typer.Argument(..., help="File name of base catalog for comparison"),
    catalog_b: str = typer.Argument(..., help="File name of second catalog for comparison"),
    ignore_tags: bool = IgnoreTagsFlag.default_help(),
    show_unique: bool = UniqueShowFlag,
    show: bool = ShowFlag,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    verbose: bool = VerboseFlag,
    stats: bool = StatsFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Run a difference on two catalog files produced by the catalog command. This will just exit with 0 or 1 unless --show
    is specified, which will print a list of all differences found.
    """

    context, _ = context_from_params()
    try:
        unique_to_a, unique_to_b, differing_items = await diff(catalog_a, catalog_b, ignore_tags=ignore_tags)

        # Print results
        if show or show_unique:
            n_diff = 0
            for item in unique_to_a:
                if verbose:
                    print(f"Diff {n_diff}: Item unique to {catalog_a}:\n{json.dumps(item, indent=4)}")
                else:
                    print(f"Diff {n_diff}: Item unique to {catalog_a}: {item['relative_path']}")
                n_diff += 1
            for item in unique_to_b:
                if verbose:
                    print(f"Diff {n_diff}: Item unique to {catalog_b}:\n{json.dumps(item, indent=4)}")
                else:
                    print(f"Diff {n_diff}: Item unique to {catalog_b}: {item['relative_path']}")
                n_diff += 1
            if show:
                for item_tuple in differing_items:
                    if verbose:
                        print(f"Diff {n_diff}: Pair differs:\n{json.dumps(item_tuple[0], indent=4)}\n{json.dumps(item_tuple[1], indent=4)}")
                    else:
                        print(f"Diff {n_diff}: Pair differs: {item_tuple[0]['relative_path']} {item_tuple[1]['relative_path']}")
                    n_diff += 1
            if n_diff == 0:
                print("No differences found")
            if not verbose:
                print("\nuse --verbose --show to display all fields")
        else:
            n_diff = len(unique_to_a) + len(unique_to_b) + len(differing_items)
        if n_diff > 0:
            if show or show_unique:
                print()
            print(f"{catalog_a} and {catalog_b} differ in {n_diff} positions")
            print(f"{len(unique_to_a)} are unique to {catalog_a}")
            print(f"{len(unique_to_b)} are unique to {catalog_b}")
            print(f"{len(differing_items)} differ in content. Use --show --verbose for full details.")
            # This is not an error, but we want to communicate to the command line whether there are conflicts or not via exit code
            raise typer.Exit(1)

    except typer.Exit as e:
        # Just rethrow to propagate exit status code
        raise e
    except FailedCommand as e:
        logger.error(f"Failure to diff '{catalog_a}' and '{catalog_b}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while calculating diff of {catalog_a} and {catalog_b}: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@wrapp_public_api
@log_args
async def diff(
    catalog_a: str, catalog_b: str, ignore_tags: bool
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """
    Take two catalog files produced by the catalog command, and return files unique to a, unique to b, and different in three lists

    :param catalog_a: File name of catalog A
    :param catalog_b: File name of catalog B
    :param ignore_tags: Specify this to ignore tags

    :return: Tuple of three lists (unique_to_a, unique_to_b, differing_items)
    """
    validate_non_empty_string("catalog_a", catalog_a)
    validate_non_empty_string("catalog_b", catalog_b)

    fields_to_ignore = copy(Diff.not_to_compare_fields)
    if ignore_tags:
        fields_to_ignore.append("tags")
    unique_to_a, unique_to_b, differing_items = await Diff.diff_files_internal(catalog_a, catalog_b, fields_to_ignore)
    return unique_to_a, unique_to_b, differing_items


@wrapp_public_api
@log_args
async def diff_catalogs(
    catalog_a: Catalog, catalog_b: Catalog, ignore_tags: bool
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Tuple[Dict[str, Any], Dict[str, Any]]]]:
    """
    Take two catalogs, and return files unique to a, unique to b, and different in three lists

    :param catalog_a: catalog A
    :param catalog_b: catalog B
    :param ignore_tags: Specify this to ignore tags

    :return: Tuple of three lists (unique_to_a, unique_to_b, differing_items)
    """
    fields_to_ignore = copy(Diff.not_to_compare_fields)
    if ignore_tags:
        fields_to_ignore.append("tags")
    unique_to_a, unique_to_b, differing_items = Diff.diff_catalogs_internal(catalog_a, catalog_b, fields_to_ignore)
    return unique_to_a, unique_to_b, differing_items
