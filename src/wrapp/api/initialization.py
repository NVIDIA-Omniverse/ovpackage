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
from typing import Optional

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.storage import (
    wrapp_initialize,
    wrapp_shutdown,
)

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@wrapp_public_api
@log_args
def initialize(debug_level: bool = False, throttling_params: Optional[ThrottlingParameters] = None) -> None:
    """
    Initialize the wrapp API. This is safe to be called multiple times.

    Initialization performs the following steps in order:

    1. **Router** — Creates all enabled storage backend objects (S3, Azure, GCS,
       Nucleus, Local, and optionally Storage API). Each backend is only created
       when its corresponding environment flag allows it (e.g.
       ``WRAPP_ENABLE_STORAGE_API=True``).
    2. **Event loop thread** — Starts the internal async event loop used by all
       storage operations.
    3. **Storage API eager initialization** — If the Storage API backend is
       enabled, WRAPP attempts to connect to the gRPC endpoint, negotiate
       protocol versions, and populate URL route information so that subsequent
       URL routing is correct from the start. If this step fails (e.g. the
       discovery service is unreachable), it falls back to lazy initialization
       on the first actual storage operation.
    4. **Temp file manager** — Initialises the temporary file pool used for
       cross-backend file transfers.

    :param debug_level: Enable debug level logging.
    :param throttling_params: Optional scheduling parameters for throttling control.
        If not provided, uses default :class:`~wrapp.ThrottlingParameters`.

    :raises RuntimeError: If the wrapp API is unable to initialize.
    """
    from .._version import __version__

    if throttling_params is None:
        throttling_params = ThrottlingParameters()

    logger.info(f"wrapp {__version__}.")

    wrapp_version = __version__
    wrapp_initialize(wrapp_version, debug_level, throttling_params)


@wrapp_public_api
@log_args
def shutdown(exc_type=None, exc_val=None, exc_tb=None, shutdown_client_lib=True, stop_timeout: Optional[float] = None) -> None:
    """
    Shuts down the WRAPP API and should be invoked before process exit.
    Afterwards, no further WRAPP commands may be issued unless you call `initialize()` again **and** `shutdown_client_lib` was `False`.
    When `shutdown_client_lib=True`, the underlying client library is fully torn down – no subsequent WRAPP or `omni.client` calls (including a new
    `initialize()`) are possible in the same process.

    The exception parameters can be passed on using ``shutdown(*sys.exc_info())`` if the function is called within code handling exceptions.

    :param stop_timeout: Timeout in seconds to wait for the event loop thread to stop.
        ``None`` (default) waits indefinitely. If a finite timeout is given and the
        thread does not stop in time, raises RuntimeError.
    """
    wrapp_shutdown(exc_type, exc_val, exc_tb, shutdown_client_lib, stop_timeout=stop_timeout)
