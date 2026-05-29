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

import asyncio
import os
import random
import sys

import wrapp


async def async_main():
    """
    Example main that shows how to use the WRAPP commands within a wrapp.ContextManager block
    """
    auth_info = wrapp.StorageAuthInfo.nucleus(
        server_url=os.getenv("HOST_URL", "omniverse://localhost"),
        username=os.getenv("OMNI_USER", "omniverse"),
        password_or_token=os.getenv("OMNI_PASS", "omniverse"),
    )
    random_version = str(random.randint(1, 1000))
    async with wrapp.ContextManager([auth_info]) as wrapp_context:
        await wrapp.create(
            "test_project",
            random_version,
            source=sys.argv[1],
            catalog=False,
            scheduler=wrapp_context,
        )
    print(f"Created test_project version {random_version} in {auth_info.server_url}")


asyncio.run(async_main())
