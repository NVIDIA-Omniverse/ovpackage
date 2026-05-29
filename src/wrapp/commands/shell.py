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
import shlex
from pathlib import Path

import typer
from prompt_toolkit import prompt
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory

from wrapp.app import app
from wrapp.utils.stats import Stats

logger = logging.getLogger("omni.wrapp")


from wrapp.options import (
    DebugLevelFlag,
    FileTransferJobsNumber,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    TaggingJobsNumber,
    VerboseFlag,
)


@app.command()
def shell(
    ctx: typer.Context,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    debug: bool = DebugLevelFlag,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    json_logging: bool = JsonLoggingFlag,
):
    """
    Enter interactive mode with history and prompt auto-complete
    """

    logging.info(f"[SHELL]")

    from wrapp._version import __version__
    from wrapp.datastructures.command_context import ThrottlingParameters
    from wrapp.utils.logging import configure_logging
    from wrapp.utils.storage import wrapp_initialize

    # Initialize logging
    try:
        configure_logging(json_logging=json_logging, debug_level=debug, verbose=verbose, log_file=log_file)
    except Exception as e:
        print(f"Error configuring logging subsystem: {e}")

    # Initialize WRAPP with throttling parameters
    throttling_params = ThrottlingParameters(
        jobs=jobs,
        file_transfer_jobs=file_transfer_jobs,
        tagging_jobs=tagging_jobs,
    )

    try:
        wrapp_initialize(__version__, debug, throttling_params)
    except RuntimeError as e:
        print(f"Error initializing WRAPP: {e}")
        raise typer.Exit(3) from e

    typer.echo("Entering wrapp shell, type exit to leave...")

    # Get the user's home directory
    home_dir = str(Path.home())

    # Construct the path to the history file
    history_file = os.path.join(home_dir, ".wrapp_history")
    history = FileHistory(history_file)

    # Determine the string Click (used by Typer) uses to designate our executable
    root_name = ctx.find_root().info_name

    while True:
        user_input = prompt(
            "wrapp> ",
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
        )

        if user_input == "exit" or user_input == "q" or user_input == "x":
            break

        # Split the user's input into arguments
        args = shlex.split(user_input)

        # If the very first argument is wrapp, drop it. This keeps happening when you paste full command lines into the shell prompt
        if len(args) > 0 and args[0] in ["wrapp", root_name]:
            args = args[1:]

        if len(args) > 0:
            try:
                # Run the command programmatically
                Stats.get().clear()
                typer.main.get_command(app)(args)
            except SystemExit:
                pass
            except Exception as e:
                logger.error(e)
