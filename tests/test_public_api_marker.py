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
import inspect
import pkgutil
import typing

import pytest

import wrapp
from wrapp.utils.api_marker import is_wrapp_public_api
from wrapp.utils.utils import StorageMissingDependenciesError


def iter_modules(package):
    """Yield package and all its importable submodules.

    Submodules that fail to import because an OPTIONAL backend dependency is not
    installed raise ``StorageMissingDependenciesError`` at import time (e.g.
    ``clientlib`` without ``omni.client``, ``s3storage`` without ``boto3``,
    ``azure_blob_storage``/``gcs_storage``/``storageapi`` without their SDKs).
    Such modules are skipped: in an install that lacks the dependency their
    symbols are not part of the reachable public surface, so they cannot -- and
    need not -- be checked here. The matrix of wheel variants (minimal, nucleus,
    s3, azure, gcs, full) exercises every backend's symbols across runs, and the
    dev environment with all backends installed covers them in one pass. Without
    this guard the test errors out in any environment missing a backend (such as
    a ``--no-nucleus`` setup).
    """
    yield package
    if hasattr(package, "__path__"):
        for _, modname, _ in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            try:
                mod = importlib.import_module(modname)
            except StorageMissingDependenciesError:
                continue
            yield mod


def get_public_members(module):
    """Return all public members from a module."""
    # If __all__ is defined, use that as the public API
    if hasattr(module, "__all__"):
        names = module.__all__
    else:
        names = [name for name in dir(module) if not name.startswith("_")]
    return [getattr(module, name) for name in names]


# TODO - we need to discuss if we want to expose anything other than the top level wrapp module's items.
# For now, only checking top level wrapp
def check_annotation(annotation):
    # If the annotation is a builtin type, we assume it's fine.
    if isinstance(annotation, type) and annotation.__module__ == "builtins":
        return
    # If it's a user-defined type, it should be marked.
    if isinstance(annotation, type):
        assert is_wrapp_public_api(annotation), f"Type {annotation.__name__} is not marked with @wrapp_public_api"
    # Handle generic types (e.g. List[MyType], Optional[MyType], etc.)
    elif hasattr(annotation, "__origin__"):
        for arg in getattr(annotation, "__args__", ()):
            check_annotation(arg)
    else:
        # If we can't inspect the annotation, we raise an error.
        raise AssertionError(f"Cannot verify annotation {annotation}")


def test_marked_symbols_are_exported():
    """Reverse direction of ``test_public_api_annotation``: every @wrapp_public_api
    MODULE-LEVEL symbol (function/class defined at module scope) anywhere in the
    ``wrapp`` package must be reachable from the top-level ``wrapp`` module by identity.

    This guards against a symbol being decorated public but never bound in
    ``wrapp/__init__.py`` (the gap that left ``FileParseException`` invisible to users).
    Marked METHODS (functions defined inside a class) are intentionally NOT required to
    be top-level attributes -- they belong to their class's surface, not as standalone
    exports. The module-level guard below (``getattr(module, obj.__name__, None) is obj``)
    naturally excludes them as well as re-exported aliases and imported foreign symbols.

    The marker check is OWN-CLASS (``is_wrapp_public_api``), so a subclass that merely
    inherits the marker from a decorated base (e.g. the ``PatchOp*`` subclasses inheriting
    from ``PatchOpBase``) is NOT counted -- only individually decorated symbols are.
    """
    top_level_names = [name for name in dir(wrapp)]

    # Collect marked module-level symbols, de-duplicated by object identity so a symbol
    # re-exported through several modules is only checked once.
    marked_by_id: dict[int, object] = {}
    for module in iter_modules(wrapp):
        for obj in vars(module).values():
            if not (inspect.isfunction(obj) or inspect.isclass(obj)):
                continue
            if not is_wrapp_public_api(obj):
                continue
            # Module-level guard: only count objects actually defined at this module's
            # scope. Excludes re-exported aliases, imported foreign symbols, and (because
            # methods are not module attributes) class methods.
            if getattr(module, obj.__name__, None) is not obj:
                continue
            marked_by_id.setdefault(id(obj), obj)

    missing = []
    for obj in marked_by_id.values():
        reachable = any(getattr(wrapp, name, None) is obj for name in top_level_names)
        if not reachable:
            missing.append(f"{obj.__module__}.{obj.__qualname__}")

    assert not missing, (
        "The following @wrapp_public_api-marked module-level symbols are not reachable "
        "from top-level `wrapp` (export them in src/wrapp/__init__.py): " + ", ".join(sorted(missing))
    )


@pytest.mark.parametrize("module", [wrapp])
def test_public_api_annotation(module):
    for member in get_public_members(module):
        if inspect.isfunction(member):
            # Verify the function is marked as public.
            assert is_wrapp_public_api(member), f"Function {member.__module__}.{member.__name__} is not marked with @wrapp_public_api"
            # Check that the function has a docstring.
            assert member.__doc__, f"Function {member.__module__}.{member.__name__} has no docstring"
            # Check parameter type annotations.
            hints = typing.get_type_hints(member)
            for param, annotation in hints.items():
                if param == "return":
                    continue
                check_annotation(annotation)
        elif inspect.isclass(member):
            # If the class itself is marked as public, skip checking its methods.
            if not is_wrapp_public_api(member):
                # Inspect public methods of the class.
                for name, method in inspect.getmembers(member, predicate=inspect.isfunction):
                    if name.startswith("_"):
                        continue
                    assert is_wrapp_public_api(method), (
                        f"Method {member.__name__}.{name} is not marked with @wrapp_public_api " "(class not marked as public)"
                    )
                    # Check that the method has a docstring.
                    assert method.__doc__, f"Method {member.__name__}.{name} has no docstring"
                    hints = typing.get_type_hints(method)
                    for param, annotation in hints.items():
                        if param == "return":
                            continue
                        check_annotation(annotation)
            else:
                # Check that the class has a docstring.
                assert member.__doc__, f"Class {member.__name__} has no docstring"
