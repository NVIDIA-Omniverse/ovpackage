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

import sys
from pathlib import Path

from wrapp import __version__
from wrapp.app import (
    app,
    measure_time,
)
from wrapp.options import (
    DebugLevelFlag,
    LogFileOption,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.storage import (
    Router,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd


def executable_name(argv0: str | None = None) -> str:
    if argv0 is None:
        argv0 = sys.argv[0]

    path = Path(argv0)
    name = path.name
    for suffix in (".exe", ".cmd", ".bat"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break

    if name in ("cli.py", "__main__.py"):
        parts = path.parts
        if "ovpackage" in parts:
            return "ovpackage"
        if "wrapp" in parts:
            return "wrapp"

    return name or "wrapp"


@app.command()
@wrapp_init_for_cmd
@measure_time
def version(time: bool = TimeFlag, verbose: bool = VerboseFlag, log_file: str = LogFileOption, debug: bool = DebugLevelFlag):
    """
    Print version of the command installed
    """
    print(f"you are running {executable_name()} version '{__version__}'")
    for v in Router.me().get_version_info():
        print(v)
