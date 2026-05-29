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

logger = logging.getLogger("omni.wrapp")

# isort: off
try:
    import omni.tagging.client as tc
    from omni.tagging.client import Tag
    from omni.tagging.client._generated.data import (
        GetTagsResult,
        ModifyTagsResult,
        StatusCode,
    )

    import importlib.metadata
    import websockets

    if tuple(map(int, importlib.metadata.version("websockets").split("."))) >= (14, 0):
        raise ImportError(
            f"The discovery service needed to support Nucleus tagging is incompatible with websockets>=14.0. Websockets version found: {importlib.metadata.version('websockets')}"
        )

    g_tagging_client_available = True
except ImportError as e:
    logger.info(f"DISABLED: Nucleus tagging service. Failed to import {e.name}. Exception: {e}")
    g_tagging_client_available = False

    # Mocks so the rest of the code can be interpreted even if the tagging client is not available
    class Tag:  # type: ignore
        pass

    class GetTagsResult:  # type: ignore
        pass

    class ModifyTagsResult:  # type: ignore
        pass

    class StatusCode:  # type: ignore
        pass


# isort: on
