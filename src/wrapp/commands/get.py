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
import pathlib
from typing import (
    List,
    Optional,
)
from urllib.parse import urlparse

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.freeze import freeze
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    InteractiveAuthFlag,
    JsonLoggingFlag,
    LogFileOption,
    OutputFilenameOption,
    OverwriteFlag,
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
from wrapp.utils.storage import (
    Result,
    wrapp_copy,
    wrapp_read_file_content,
    wrapp_url_is_folder,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="get")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def get_command(
    url: str = typer.Argument(..., help="source URL to download, e.g. omniverse://localhost/assets/lib1/.dependencies.wrapp"),
    filename: str = OutputFilenameOption,
    overwrite: bool = OverwriteFlag,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Download a file or folder from the given URL to the specified local path
    """
    context, _ = context_from_params()
    try:
        await get(url, filename, overwrite=overwrite)
    except FailedCommand as e:
        logger.error(f"Failure to get '{url}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while getting '{url}': {e}, operation incomplete.")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def get(
    url: str,
    filename: Optional[str],
    overwrite: bool = False,
    context: CommandParameters = CommandParameters(),
    *,
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Download a file or folder from the given URL to the specified local path.

    If the URL points to a folder, downloads the folder and its contents recursively.

    :param url: URL to download (file or folder)
    :param filename: Optional alternate local path to download to. If not given, uses the same name as in the source URL
    :param overwrite: Specify to allow overwriting the target if it already exists
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    if filename is not None:
        target_name = filename
    else:
        parsed = urlparse(url)
        os_path = pathlib.Path(parsed.path)
        target_name = os_path.name

    # Check if it is a folder
    if await wrapp_url_is_folder(url):
        logger.info(f"Downloading folder {url} recursively...")
        await freeze(url, target_name, overwrite=overwrite, context=context, scheduler=scheduler)
    else:
        logger.info(f"Downloading {url} to {target_name}")
        result, content_raw = await wrapp_read_file_content(url)
        if result != Result.OK:
            raise StorageOperationError(f"Error to read file {url}: {result}")
        if not overwrite and os.path.exists(target_name):
            raise FailedCommand(f"Target file {target_name} exists and --overwrite is not specified")
        with open(target_name, "wb") as output:
            output.write(bytes(memoryview(content_raw)))
