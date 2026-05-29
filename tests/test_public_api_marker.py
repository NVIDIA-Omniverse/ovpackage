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


def iter_modules(package):
    """Yield package and all its submodules."""
    yield package
    if hasattr(package, '__path__'):
        for finder, modname, ispkg in pkgutil.walk_packages(package.__path__, package.__name__ + '.'):
            mod = importlib.import_module(modname)
            yield mod

def get_public_members(module):
    """Return all public members from a module."""
    # If __all__ is defined, use that as the public API
    if hasattr(module, '__all__'):
        names = module.__all__
    else:
        names = [name for name in dir(module) if not name.startswith('_')]
    return [getattr(module, name) for name in names]


# TODO - we need to discuss if we want to expose anything other than the top level wrapp module's items.
# For now, only checking top level wrapp
def check_annotation(annotation, marker):
    # If the annotation is a builtin type, we assume it's fine.
    if isinstance(annotation, type) and annotation.__module__ == "builtins":
        return
    # If it's a user-defined type, it should be marked.
    if isinstance(annotation, type):
        assert hasattr(annotation, marker) and getattr(annotation, marker), (
            f"Type {annotation.__name__} is not marked with @{marker}"
        )
    # Handle generic types (e.g. List[MyType], Optional[MyType], etc.)
    elif hasattr(annotation, '__origin__'):
        for arg in getattr(annotation, '__args__', ()):
            check_annotation(arg, marker)
    else:
        # If we can't inspect the annotation, we raise an error.
        assert False, f"Cannot verify annotation {annotation}"

@pytest.mark.parametrize("module", [wrapp])
def test_public_api_annotation(module):
    from wrapp.utils.api_marker import WRAPP_API_MARKER_NAME
    for member in get_public_members(module):
        if inspect.isfunction(member):
            # Verify the function is marked as public.
            assert hasattr(member, WRAPP_API_MARKER_NAME) and member.wrapp_public_api, (
                f"Function {member.__module__}.{member.__name__} is not marked with @{WRAPP_API_MARKER_NAME}"
            )
            # Check that the function has a docstring.
            assert member.__doc__, f"Function {member.__module__}.{member.__name__} has no docstring"
            # Check parameter type annotations.
            hints = typing.get_type_hints(member)
            for param, annotation in hints.items():
                if param == 'return':
                    continue
                check_annotation(annotation, WRAPP_API_MARKER_NAME)
        elif inspect.isclass(member):
            # If the class itself is marked as public, skip checking its methods.
            if not (hasattr(member, WRAPP_API_MARKER_NAME) and member.wrapp_public_api):
                # Inspect public methods of the class.
                for name, method in inspect.getmembers(member, predicate=inspect.isfunction):
                    if name.startswith('_'):
                        continue
                    assert hasattr(method, WRAPP_API_MARKER_NAME) and method.wrapp_public_api, (
                        f"Method {member.__name__}.{name} is not marked with @{WRAPP_API_MARKER_NAME} "
                        "(class not marked as public)"
                    )
                    # Check that the method has a docstring.
                    assert method.__doc__, f"Method {member.__name__}.{name} has no docstring"
                    hints = typing.get_type_hints(method)
                    for param, annotation in hints.items():
                        if param == 'return':
                            continue
                        check_annotation(annotation, WRAPP_API_MARKER_NAME)
            else:
                # Check that the class has a docstring.
                assert member.__doc__, f"Class {member.__name__} has no docstring"

