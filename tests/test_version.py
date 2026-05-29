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

import os

from tests.common import *
from wrapp import __version__
from wrapp.commands.version import executable_name
from wrapp.utils.router import g_nucleus_enabled

# Check actual nucleus availability, not just config flag
try:
    import omni.client

    _nucleus_available = os.getenv("WRAPP_DISABLE_NUCLEUS", "").strip().lower() not in ("true", "1", "yes")
except ImportError:
    _nucleus_available = False


@pytest.mark.parametrize(
    ("argv0", "expected"),
    [
        ("/usr/local/bin/wrapp", "wrapp"),
        ("/usr/local/bin/ovpackage", "ovpackage"),
        ("C:/Tools/ovpackage.exe", "ovpackage"),
        ("/repo/src/wrapp/cli.py", "wrapp"),
        ("/repo/src/ovpackage/cli.py", "ovpackage"),
    ],
)
def test_version_executable_name(argv0, expected):
    assert executable_name(argv0) == expected


@pytest.mark.skipif(not _nucleus_available or not g_nucleus_enabled, reason="Nucleus (omni.client) is not available or disabled")
def test_version_hub_enabled(storage_backend):
    output = run_wrapp_with_hub(["version"], 0)
    assert __version__ in output
    # This deliberately assumes Hub is running. All the other tests are meant to be run with Hub enabled.
    assert "Hub cache is ON" in output


@pytest.mark.skipif(not _nucleus_available or not g_nucleus_enabled, reason="Nucleus (omni.client) is not available or disabled")
def test_version_hub_disabled(storage_backend):
    output = run_wrapp_without_hub(["version"], 0)
    assert __version__ in output
    # This deliberately assumes Hub is running. All the other tests are meant to be run with Hub enabled.
    assert "Hub cache is OFF" in output
