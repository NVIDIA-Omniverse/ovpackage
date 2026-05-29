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

import importlib
import importlib.abc
import importlib.machinery
import sys

import wrapp as _wrapp
from wrapp import *  # noqa: F401,F403

__version__ = _wrapp.__version__


def __getattr__(name: str):
    return getattr(_wrapp, name)


class _OvpackageAliasLoader(importlib.abc.Loader):
    """Loader that reuses an already-loaded wrapp module as its ovpackage alias.

    create_module returns the wrapp module object directly, so sys.modules
    ends up with two keys (wrapp.X.Y and ovpackage.X.Y) pointing at the
    same object. exec_module is a no-op because the module's top-level code
    ran when wrapp loaded it.
    """

    def __init__(self, target_module):
        self._target = target_module

    def create_module(self, spec):
        return self._target

    def exec_module(self, module):
        pass


class _OvpackageAliasFinder(importlib.abc.MetaPathFinder):
    """Lazily alias ``ovpackage.X.Y`` to ``wrapp.X.Y`` on first import.

    Without this, Python's import machinery sees the parent ovpackage
    package in sys.modules but no entry for the nested path, so it locates
    the .py file via the parent's __path__ and *re-executes* it under the
    ovpackage.X.Y name — producing duplicate class objects and
    re-registering typer command decorators.

    Doing it lazily (vs. eagerly walking the whole wrapp tree at ovpackage
    import time) means optional/native backends like
    wrapp.utils.storage_impls.clientlib are only loaded when a caller
    explicitly asks for them.
    """

    _OVPACKAGE_PREFIX = "ovpackage."
    _WRAPP_PREFIX = "wrapp."

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self._OVPACKAGE_PREFIX):
            return None
        wrapp_name = self._WRAPP_PREFIX + fullname[len(self._OVPACKAGE_PREFIX) :]
        try:
            module = importlib.import_module(wrapp_name)
        except Exception:
            # Module genuinely doesn't exist or its own top-level execution
            # failed. Returning None lets the rest of sys.meta_path (and
            # ultimately ImportError) surface the real failure to the caller.
            return None
        spec = importlib.machinery.ModuleSpec(
            fullname,
            _OvpackageAliasLoader(module),
            origin=getattr(module, "__file__", None),
            is_package=hasattr(module, "__path__"),
        )
        if hasattr(module, "__path__"):
            spec.submodule_search_locations = list(module.__path__)
        return spec


sys.meta_path.insert(0, _OvpackageAliasFinder())
