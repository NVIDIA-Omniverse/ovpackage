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
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.patch import Patch
from wrapp.options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
    StatsFlag,
    TaggingJobsNumber,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.utils import (
    FailedCommand,
    FailedPatchCommand,
    StorageOperationError,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="apply-patch")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def apply_patch_command(
    patch: str = typer.Argument(..., help="File name of patch to apply"),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,  # This needs to be declared for measure_time
    stats: bool = StatsFlag,
) -> None:
    """
    Taking a patch file as input, apply the changes previously calculated by the create-patch command.
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await apply_patch(patch, context=context, scheduler=scheduler)
    except FailedCommand as e:
        logger.error(f"Failed to apply patch {patch}: {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while applying patch {patch}, operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def apply_patch(
    patch: str, *, context: CommandParameters = CommandParameters(), scheduler: Optional[SchedulerContext] = None
) -> None:
    """
    Taking a patch file as input, apply the changes previously calculated by the create-patch command.

    :param patch: Specify filename of patch file (local file path)
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: in case the patch file specified cannot be found
    :raises StorageOperationError: in case of failed patch operations
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    if os.path.exists(patch):
        p = Patch()
        try:
            await p.load(patch)
            if not await p.execute(context=context, scheduler=scheduler):
                raise FailedPatchCommand(f"Errors during applying patch {patch}. Check log for details.", patch=p)
        finally:
            await p.disconnect_tagging_service()
    else:
        raise FailedCommand(f"Could not find patch file specified: '{patch}'")
