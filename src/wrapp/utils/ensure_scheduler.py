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

import inspect
from functools import wraps

from wrapp.datastructures.command_context import (
    WrappScheduler,
)


def ensure_scheduler(f):
    """
    This decorator is used on API functions to ensure that regardless of the way the function is called,
    the context contains a scheduler even if the user does not provide one in the context.
    """

    @wraps(f)
    async def wrapper(*args, **kwargs):
        assert inspect.iscoroutinefunction(f), f"ensure_scheduler can only be used on async functions, {f} is not an async function."
        # Make sure the wrapper has been applied to a function which has a context parameter.
        orig_f = f.__wrapped__ if hasattr(f, "__wrapped__") else f
        all_kwargs = dict(orig_f.__kwdefaults__ if orig_f.__kwdefaults__ else {})
        all_kwargs.update(kwargs)
        if "scheduler" not in all_kwargs:
            raise TypeError(
                f"Function {orig_f} is missing the expected scheduler parameter. All functions decorated with 'ensure_scheduler' need a keyword-only 'scheduler' parameter of type Optional[Scheduling] with None as default value"
            )

        if "scheduler" not in kwargs:
            # We are using the default parameter. Make a unique object out of it.
            # This is required - otherwise the object instantiated by the default value
            # for the context parameter will be used for all calls to the same function -
            # causing trouble when one call shuts down the scheduler but the other call
            # is still running.
            # TODO This is a quick fix - functions running at the same time should default to
            # using the same automatically created scheduler.
            async def wrap_f(*args, **kwargs):
                async with WrappScheduler() as scheduler:
                    kwargs["scheduler"] = scheduler
                    if inspect.iscoroutinefunction(f):
                        return await f(*args, **kwargs)
                    else:
                        return f(*args, **kwargs)

            return await wrap_f(*args, **kwargs)
        else:
            if inspect.iscoroutinefunction(f):
                return await f(*args, **kwargs)
            else:
                return f(*args, **kwargs)

    return wrapper
