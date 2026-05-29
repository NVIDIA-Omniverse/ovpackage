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

import pytest

pytest_plugins = ["tests.wrapp_test_fixtures"]


def pytest_addoption(parser):
    parser.addoption(
        "--run-xdist-incompatible-tests",
        action="store_true",
        default=False,
        help="Only run tests marked xdist_incompatible (for sequential CI passes).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "xdist_incompatible: test mutates global singletons (Router, WrappEventLoopThread) "
        "and must not run concurrently with other tests. "
        "Automatically skipped when pytest-xdist is active (-n > 0).",
    )


def _is_xdist_session(config):
    if hasattr(config, "workerinput"):
        return True
    try:
        return config.getoption("numprocesses", None) not in (None, 0)
    except ValueError:
        return False


def pytest_collection_modifyitems(config, items):
    run_only_incompatible = config.getoption("--run-xdist-incompatible-tests", default=False)

    if run_only_incompatible:
        if _is_xdist_session(config):
            raise pytest.UsageError(
                "--run-xdist-incompatible-tests cannot be used together with pytest-xdist parallelism (-n). "
                "These tests must run sequentially."
            )
        selected = []
        deselected = []
        for item in items:
            if "xdist_incompatible" in item.keywords:
                selected.append(item)
            else:
                deselected.append(item)
        if deselected:
            config.hook.pytest_deselected(items=deselected)
        items[:] = selected
        return

    if not _is_xdist_session(config):
        return

    skip = pytest.mark.skip(reason="Test mutates global singletons and cannot run under xdist")
    for item in items:
        if "xdist_incompatible" in item.keywords:
            item.add_marker(skip)
