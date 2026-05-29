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
from functools import wraps
from typing import Callable

import typer

from wrapp.utils.logging import configure_logging
from wrapp.utils.storage import wrapp_initialize

logger = logging.getLogger("omni.wrapp")


def wrapp_init_for_cmd(func) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        from wrapp._version import __version__
        from wrapp.datastructures.command_context import ThrottlingParameters

        debug_log = kwargs.get("debug", False)
        verbose = kwargs.get("verbose", False)
        json_logging = kwargs.get("json_logging", False)
        log_file_name = kwargs.get("log_file", "wrapp.log")

        try:
            configure_logging(json_logging=json_logging, debug_level=debug_log, verbose=verbose, log_file=log_file_name)
        except Exception as e:
            logger.error(f"Error configuring logging subsystem: {e}")

        # Extract throttling parameters
        jobs = kwargs.get("jobs", 200)
        file_transfer_jobs = kwargs.get("file_transfer_jobs", 10)
        tagging_jobs = kwargs.get("tagging_jobs", 50)

        throttling_params = ThrottlingParameters(
            jobs=jobs,
            file_transfer_jobs=file_transfer_jobs,
            tagging_jobs=tagging_jobs,
        )

        try:
            wrapp_initialize(__version__, debug_log, throttling_params)
        except RuntimeError as e:
            print(f"Error initializing WRAPP: {e}")
            raise typer.Exit(3)

        return func(*args, **kwargs)

    return wrapper
