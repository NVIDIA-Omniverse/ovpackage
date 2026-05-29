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

from dataclasses import dataclass
from typing import (
    Optional,
)

from wrapp.datastructures import (
    AuthManager,
    SchedulerContext,
    StorageAuthInfo,
)
from wrapp.datastructures.command_context import (
    SchedulingParameters,
    ThrottlingParameters,
)
from wrapp.utils.api_marker import wrapp_public_api

from ..utils.logging import log_args
from .initialization import (
    initialize,
    shutdown,
)


@wrapp_public_api
class ContextManager:
    """
    Helper class to make it easy to initialize WRAPP. If you don't know what to do to initialize WRAPP, use this class
    to get started.

    The context manager handles the full lifecycle — initialization, authentication,
    scheduling, and shutdown — so you can focus on WRAPP commands::

        async with wrapp.ContextManager([auth_info]) as ctx:
            await wrapp.list_repo("s3://my-bucket/repo", scheduler=ctx)

    **Storage API usage** — To use Storage API backends, set the environment
    variables ``WRAPP_ENABLE_STORAGE_API=True`` and
    ``WRAPP_STORAGE_API_DISCOVERY_URL=<url>`` *before* entering the context
    manager. Authentication can be supplied via :class:`StorageAuthInfo`
    constructed with :class:`StorageAPIBearerAuthInfo` or
    :class:`StorageAPIClientCredentialsAuthInfo`, or through CLI ``--auth`` /
    ``WRAPP_AUTH``. See :ref:`storage-api-authentication` for full details.

    See :ref:`setup-and-tear-down` for example code.
    """

    @log_args
    def __init__(
        self,
        auth_infos: Optional[list[StorageAuthInfo]] = None,
        interactive_login_allowed=False,
        standalone_mode=True,
        scheduler_context: Optional[SchedulerContext | SchedulingParameters] = None,
        throttling_params: Optional[ThrottlingParameters] = None,
    ):
        """
        Initialize the context manager setting up the desired mode of operation

        :param auth_infos: Optional list of AuthInfo instances giving credentials for the servers to be involved
        :param interactive_login_allowed: If False [default], no interactive browser window will pop up, and all authentication information needs to be supplied via the AuthInfo data
        :param standalone_mode: If True [default], call wrapp.shutdown() on exit. No further wrapp calls are possible beyond this
        :param scheduler_context: Assigned to the command_context field, which can be used in calls to the API for command throttling
            Alternatively, a "SchedulingParameters" object can be passed in that parameter, which will be used to construct a "SchedulerContext" object.
        :param throttling_params: The maximum number of concurrent tasks for several categories.
        """
        self._auth_infos = auth_infos if auth_infos else []
        self._auth_manager: Optional[AuthManager] = None
        self._interactive_login_allowed = interactive_login_allowed
        self._standalone_mode = standalone_mode
        self._throttling_params = throttling_params
        self._user_supplied_scheduler_context = True
        if isinstance(scheduler_context, SchedulerContext):
            self.command_context = scheduler_context
        else:
            if scheduler_context is not None and not isinstance(scheduler_context, SchedulingParameters):
                raise TypeError(f"Invalid type '{type(scheduler_context)}' for scheduler_context parameter!")
            self._user_supplied_scheduler_context = False
            self.command_context = SchedulerContext(params=scheduler_context)

    async def __aenter__(self) -> "SchedulerContext":
        # Extract scheduling params from the scheduler context and pass to initialize
        initialize(throttling_params=self._throttling_params)

        self._auth_manager = AuthManager(auth_infos=self._auth_infos, interactive_fallback=self._interactive_login_allowed)
        self._auth_manager.__enter__()

        if not self._user_supplied_scheduler_context:
            self.command_context.start_scheduler()
        return self.command_context

    @log_args
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if not self._user_supplied_scheduler_context:
            await self.command_context.stop_scheduler(exc_type=exc_type, exc_val=exc_val, exc_tb=exc_tb)
        self._auth_manager.__exit__(exc_type, exc_val, exc_tb)
        if self._standalone_mode:
            shutdown(exc_type, exc_val, exc_tb)

    @log_args
    def add_auth(self, auth_info: StorageAuthInfo) -> None:
        """
        Add another StorageAuthInfo credential to the AuthManager used

        :param auth_info:
        """
        if self._auth_manager:
            self._auth_manager.add_auth(auth_info)
        else:
            raise RuntimeError("Adding auth_infos is only possible after the async context has been entered!")

    def __repr__(self):
        auth_infos = getattr(self, "_auth_infos", None)
        auth_manager = getattr(self, "_auth_manager", None)
        standalone_mode = getattr(self, "_standalone_mode", None)
        throttling_params = getattr(self, "_throttling_params", None)
        user_supplied_scheduler_context = getattr(self, "_user_supplied_scheduler_context", None)
        ctx = getattr(self, "command_context", None)

        return (
            f"ContextManager("
            f"_auth_infos={auth_infos!r}, "
            f"_auth_manager={auth_manager!r}, "
            f"_standalone_mode={standalone_mode!r}, "
            f"_throttling_params={throttling_params!r}, "
            f"_user_supplied_scheduler_context={user_supplied_scheduler_context!r}, "
            f"command_context={ctx!r})"
        )
