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

import functools
import inspect
import json
import logging
import os
import reprlib
from typing import (
    Any,
    Callable,
    Mapping,
    ParamSpec,
    TypeVar,
)

import pydantic

logger = logging.getLogger("wrapp")


class CustomJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data = {"timestamp": self.formatTime(record, self.datefmt), "level": record.levelname, "message": record.getMessage()}
        if hasattr(record, "component"):
            log_data["component"] = record.component
        if hasattr(record, "clientlib_thread"):
            log_data["clientlib_thread"] = record.clientlib_thread
        return json.dumps(log_data)


g_logging_initialized = False


def allow_reinit_logging_for_test():
    global g_logging_initialized
    g_logging_initialized = False


def configure_logging(json_logging: bool, verbose: bool, debug_level: bool, log_file: str) -> None:
    global g_logging_initialized
    if not g_logging_initialized:
        log_format = "%(asctime)s [%(name)s] [%(levelname)s] %(message)s"
        log_level = logging.DEBUG if debug_level else (logging.INFO if verbose else logging.WARNING)

        # Setup formatter
        formatter = CustomJsonFormatter() if json_logging else logging.Formatter(log_format)

        # Setup file logger
        if os.environ.get("PYTEST_XDIST_WORKER", "") != "":
            log_file = log_file + "." + os.environ.get("PYTEST_XDIST_WORKER", "")
            log_level = logging.DEBUG
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)

        # Configure root logger. Some other module might already have done basicConfig, then the level is ignored and we need to set it explicitly
        logging.basicConfig(level=log_level, format=log_format)

        # Set the root logger name to "wrapp" so all direct logging calls show [omni.wrapp] instead of [root]
        logger = logging.getLogger("omni.wrapp")
        logger.addHandler(file_handler)
        logger.setLevel(log_level)

        from wrapp._version import __version__

        logger.info(f"wrapp {__version__}.")

        g_logging_initialized = True


P = ParamSpec("P")
R = TypeVar("R")


# Use a precise generic type here so if the wrapped function returns an object,
# mypy does not complain later that the object might be None.
def log_args(func: Callable[P, R]) -> Callable[P, R]:
    # Unwrap the function to check if the underlying function is a coroutine function
    # This handles cases where func is wrapped by other decorators
    unwrapped_func = func
    while hasattr(unwrapped_func, "__wrapped__"):
        unwrapped_func = unwrapped_func.__wrapped__

    _short = reprlib.Repr()
    _short.maxlevel = 3
    _short.maxlist = _short.maxtuple = _short.maxset = _short.maxfrozenset = _short.maxdict = _short.maxdeque = _short.maxarray = 3
    _short.maxstring = 10240
    _short.maxother = 1024

    def short_repr(x: Any) -> str:
        if isinstance(x, pydantic.BaseModel):
            parts = []
            for name in x.model_fields.keys():
                value = getattr(x, name)
                parts.append(f"{name}={short_repr(value)}")
            return f"{x.__class__.__name__}({', '.join(parts)})"
        return _short.repr(x)

    def format_call(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> str:
        parts = [short_repr(a) for a in args]
        parts.extend(f"{k}={short_repr(v)}" for k, v in kwargs.items())
        return ", ".join(parts)

    if inspect.iscoroutinefunction(unwrapped_func):

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Avoid doing anything if nothing is logged
            if not logger.isEnabledFor(logging.DEBUG):
                return await func(*args, **kwargs)

            logger.debug(">>%s(%s)", func.__qualname__, format_call(args, kwargs))

            try:
                r = await func(*args, **kwargs)
                logger.debug("<<%s(%s) -> %s", func.__qualname__, format_call(args, kwargs), short_repr(r))
                return r
            except BaseException as e:
                logger.debug("!!%s(%s) -> %s", func.__qualname__, format_call(args, kwargs), short_repr(e))
                raise

    else:

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Avoid doing anything if nothing is logged
            if not logger.isEnabledFor(logging.DEBUG):
                return func(*args, **kwargs)

            logger.debug(">>%s(%s)", func.__qualname__, format_call(args, kwargs))
            try:
                r = func(*args, **kwargs)
                logger.debug("<<%s(%s) -> %s", func.__qualname__, format_call(args, kwargs), short_repr(r))
                return r
            except BaseException as e:
                logger.debug("!!%s(%s) -> %s", func.__qualname__, format_call(args, kwargs), short_repr(e))
                raise

    return wrapper
