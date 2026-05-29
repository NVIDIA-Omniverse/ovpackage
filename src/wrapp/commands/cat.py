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
from typing import (
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
from wrapp.datastructures.command_context import (
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    InteractiveAuthFlag,
    JsonLoggingFlag,
    LogFileOption,
    StatsFlag,
    TimeFlag,
)
from wrapp.utils.coro import async_typer
from wrapp.utils.storage import (
    Result,
    wrapp_read_text_file,
    wrapp_stat,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    is_folder,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="cat")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def cat(
    url: str = typer.Argument(..., help="source URL to download, e.g. omniverse://localhost/assets/lib1/.dependencies.wrapp"),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
):
    """
    Print the content of a file (text file) to the console
    """

    context, _ = context_from_params()
    try:
        result, item = await wrapp_stat(url)
        if result == Result.ERROR_NOT_FOUND:
            raise FailedCommand("not found")
        if result != Result.OK:
            raise StorageOperationError(f"got error code {result}")
        if is_folder(item):  # Don't evaluate the item if the result os any other than Result.OK
            raise FailedCommand("can not cat a folder")
        content = await wrapp_read_text_file(url)
        print(content, end="")
    except FailedCommand as e:
        logger.error(f"Failure to cat '{url}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while running cat '{url}': {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)
