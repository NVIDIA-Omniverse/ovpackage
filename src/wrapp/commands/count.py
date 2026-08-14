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
import os.path
from typing import (
    Dict,
    List,
    Optional,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
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
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.coro import async_typer
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    format_size,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="count")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def count_command(
    catalog_file: str = typer.Argument(..., help="File name of catalog to evaluate"),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,  # This needs to be declared for measure_time
    stats: bool = StatsFlag,
) -> None:
    """
    Count files and folders contained in a catalog supplied, and print on disk and duplication statistics
    """
    context, _ = context_from_params()
    try:
        await count(catalog_file, context=context)
    except FailedCommand as e:
        logger.error(f"Failed to count files in catalog {catalog_file}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while trying to count files in catalog {catalog_file}, operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


async def count(catalog_file: str, context: CommandParameters = CommandParameters()) -> None:
    catalog = await Catalog.load(catalog_file)
    num_files = 0
    num_unknown_size = 0
    size = 0
    size_via_hash = 0
    via_hash: Dict[str, CatalogItem] = {}
    extension_size_hist = {}
    for item in catalog.items:
        if item.size is None:
            num_unknown_size += 1
        item_size = item.size if item.size is not None else 0
        size += item_size
        num_files += 1

        _, extension = os.path.splitext(item.relative_path)
        if extension not in extension_size_hist:
            extension_size_hist[extension] = [0, 0]
        extension_size_hist[extension][0] += item_size

        if item.hash in via_hash:
            logger.info(f"Found duplicate: {item} is duplicate of {via_hash[item.hash]}")
        else:
            # A new (non-duplicate) hash, or a hashless item: a hashless item
            # cannot be proven a duplicate, so it counts as its own unique
            # storage entry, consistent with the per-extension histogram.
            extension_size_hist[extension][1] += item_size
            size_via_hash += item_size
        if item.hash is not None:
            # Only items with hashes can be put into the duplicate list
            via_hash[item.hash] = item

    print(f"number of files: {num_files}")
    print(f"size on disk: {format_size(size)}")
    print(f"size in storage: {format_size(size_via_hash)}")
    if num_unknown_size > 0:
        print(f"number of files with unknown size: {num_unknown_size}")

    print("\nvia extension (disk/storage):")
    for ext in extension_size_hist.keys():
        print(f"size {ext}: {format_size(extension_size_hist[ext][0])}/{format_size(extension_size_hist[ext][1])}")
