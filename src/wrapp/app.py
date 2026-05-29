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

import time as timelib
from functools import wraps
from typing import Callable

import typer

from wrapp.utils.stats import Stats

app = typer.Typer(context_settings={"help_option_names": ["--help", "-h"]}, no_args_is_help=True)


def measure_time(func) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        start_time = timelib.time()
        try:
            result = func(*args, **kwargs)
        except Exception:
            raise
        finally:
            if "time" in kwargs and kwargs["time"]:
                elapsed_time = timelib.time() - start_time
                print(f"\n {elapsed_time:.2f}s elapsed")
        return result

    return wrapper


def print_stats(func) -> Callable:
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
        except Exception:
            raise
        finally:
            if "stats" in kwargs and kwargs["stats"]:
                Stats.get().pretty_print()
        return result

    return wrapper
