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

import pytest

# Only add imports which a user is supposed to use
import wrapp
from wrapp.utils.coro import force_wrapp_thread
from wrapp.utils.storage import wrapp_url_exists


@pytest.mark.asyncio
@force_wrapp_thread
async def test_catalog_api(test_folder_url, authenticator, local_tmp_folder, storage_backend):
    source_package = await storage_backend.create_source_package(test_folder_url)

    # Calling the function without any preparation is inefficient, but should work
    c: wrapp.Catalog = await wrapp.catalog(test_folder_url)
    assert 4 == len(c.items)
    assert 52 == c.size_of_catalog(), "Number of bytes in standard catalog changed?"
    assert any([item.relative_path == "source_package/testfile.txt" for item in c.items])

    # Test the API calls as it is meant to work in a more complex program
    context = wrapp.CommandParameters()

    # From WrappFileModel we inherited load and save
    local_catalog_file = os.path.join(local_tmp_folder, "catalog.json")
    await c.save(context, local_catalog_file)
    assert await wrapp_url_exists(local_catalog_file)

    d: wrapp.Catalog = await wrapp.Catalog.load(local_catalog_file)
    # TODO, catalog comparator. This is part of the fingerprinting story?
    assert len(c.items) == len(d.items)

    # Avoid overloading the server, only run 2 jobs in parallel
    async with wrapp.WrappScheduler(params=wrapp.SchedulingParameters(jobs=2)) as scheduler:
        # Create 10 different tasks
        tasks = []
        for i in range(10):
            tasks.append(asyncio.create_task(wrapp.catalog(test_folder_url, context=context, scheduler=scheduler), name=f"Catalog{i}"))
        # Await them in reverse order (any order should be fine, as should be concurrently waiting for them)
        # Not sure how we could test that the tasks are all executed concurrently and not one by one?
        results = []
        for i in range(9, 0, -1):
            results.append(await tasks[i])

    for r in results:
        assert 4 == len(r.items)
        assert any([item.relative_path == "source_package/testfile.txt" for item in r.items])
