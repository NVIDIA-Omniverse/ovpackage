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
from typing import (
    Callable,
    List,
    Optional,
)

from typing_extensions import Self

from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.storage import (
    Router,
    wrapp_add_auth_info,
    wrapp_get_last_connection_status,
    wrapp_parse_auth_param,
    wrapp_read_file_content,
    wrapp_set_auth_info,
)
from wrapp.utils.storage_interface import (
    AuthInfo,
    ConnectionStatus,
    NucleusAuthInfo,
    Result,
    StorageAuthInfo,
)
from wrapp.utils.utils import (
    StorageOperationError,
)

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@wrapp_public_api
class AuthManager:
    """
    Use this class to register credential information to be used by subsequent API calls.
    This class can be used to programmatically set credentials used by a storage provider.

    Example code for setting up credentials for Nucleus and using them::

        >>> def download_file_from_staging(url, api_token):
        >>>     with AuthManager([StorageAuthInfo.nucleus(server_url="omniverse://staging.nvidia.com", username= "$omni-api-token", password_or_token= api_token)], interactive_fallback=False) as auth:
        >>>         result, version, content = wrapp_read_file_content(url)
        >>>     if result == Result.OK:
        >>>         return content
        >>>     else:
        >>>         raise Exception(f"Download error: {result} for {url}")

    Note the use of the special username to designate an API token created for that server.
    """

    @log_args
    def __init__(self, auth_infos: Optional[List[StorageAuthInfo]] = None, interactive_fallback: bool = True):
        self._auth_infos: List[StorageAuthInfo] = auth_infos if auth_infos else []
        self._interactive_fallback: bool = interactive_fallback

    @log_args
    def __enter__(self):
        self.initialize()
        return self

    @log_args
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown(exc_type, exc_val, exc_tb)

    def initialize(self) -> Self:
        """
        Initializes the AuthManager after creation and installs authentication hook. Only after this the authentication info provided will be
        used. Make sure to call shutdown() in a 'finally' block somewhere to uninstall the authentication hook again.

        :returns: self
        """
        logger.debug("Entering AuthManager, registering with storage")
        Router.me().enable_auth_interactive_fallback(self._interactive_fallback)
        wrapp_set_auth_info(self._auth_infos)
        return self

    @log_args
    def get_last_status(self, server_url: str) -> Optional[ConnectionStatus]:
        return wrapp_get_last_connection_status(server_url)

    @log_args
    def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        """
        De-registers the authentication callback hook installed by a previous call to initialize().

        If called when handling exceptions, the function can be called using ``auth_manager.shutdown(*sys.exc_info())``.
        Otherwise the exception parameters can be left to their default values.
        """
        logger.debug("Exiting AuthManager")
        # Clear authentication data
        wrapp_set_auth_info([])

    @log_args
    def clear(self):
        """
        Clear all previously stored credential information. This does not log out of a server or disable the registered callback
        """
        logger.info(f"Clearing all AuthManager credentials")
        self._auth_infos = []
        wrapp_set_auth_info([])

    @log_args
    def add_auth(self, auth_info: StorageAuthInfo) -> None:
        """
        Add one credential pair to be used for the given server. These are not persisted but only kept in RAM.

        :param auth_info: - new auth info to be stored
        """
        logger.info(f"Adding auth information for server {auth_info.server_url}")
        self._auth_infos.append(auth_info)
        wrapp_add_auth_info(auth_info)

    def __repr__(self):
        auth_infos = getattr(self, "_auth_infos", None)
        interactive_fallback = getattr(self, "_interactive_fallback", None)
        return f"AuthManager(_auth_infos={auth_infos!r}, _interactive_fallback={interactive_fallback!r})"


def capture_auth(func) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs) -> Callable:
        allow_interactive = False
        infos: List[StorageAuthInfo] = []
        if "auth" in kwargs and kwargs["auth"]:
            for auth in kwargs["auth"]:
                if not auth:
                    logger.error("Ignoring empty auth parameter!")
                    continue
                server_url = auth.split(",")[0]
                if not server_url:
                    logger.error("Ignoring auth parameter without server_url")
                    continue
                try:
                    auth_info = wrapp_parse_auth_param(server_url, auth)
                    infos.append(auth_info)
                except StorageOperationError as e:
                    logger.error(f"Failed to parse authentication information for server, server auth info is ignored: {server_url}")
        if "interactive_auth" in kwargs:
            allow_interactive = kwargs["interactive_auth"]
            if allow_interactive:
                logger.debug("Turning user interactive auth fallback on")

        with AuthManager(auth_infos=infos, interactive_fallback=allow_interactive) as auth_manager:
            return func(*args, **kwargs)

    return wrapper
