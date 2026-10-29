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

import pytest

import wrapp
import wrapp.datastructures.ignore as ignore_module
from wrapp._compat import apply_ovpackage_env_aliases
from wrapp.datastructures.ignore import IgnoreEvaluator
from wrapp.datastructures.package import (
    is_package_file_name,
    repository_from_wrapp_file_path,
)


@pytest.fixture
def ovpackage_module():
    return pytest.importorskip("ovpackage")


def test_ovpackage_reexports_wrapp_public_api(ovpackage_module):
    assert ovpackage_module.__version__ == wrapp.__version__
    assert ovpackage_module.PackageInfo is wrapp.PackageInfo
    assert ovpackage_module.ContextManager is wrapp.ContextManager


def test_common_ovpackage_submodules_alias_wrapp_modules(ovpackage_module):
    assert importlib.import_module("ovpackage.commands") is importlib.import_module("wrapp.commands")
    assert importlib.import_module("ovpackage.datastructures") is importlib.import_module("wrapp.datastructures")
    assert importlib.import_module("ovpackage.utils") is importlib.import_module("wrapp.utils")


def test_nested_ovpackage_submodules_alias_wrapp_modules(ovpackage_module):
    # Without aliasing every level, `import ovpackage.X.Y` would load a fresh
    # copy of wrapp/X/Y.py and produce a duplicate class object — breaking
    # identity checks and re-running typer command decorators.
    assert importlib.import_module("ovpackage.datastructures.package") is importlib.import_module("wrapp.datastructures.package")
    assert importlib.import_module("ovpackage.commands.version") is importlib.import_module("wrapp.commands.version")
    pkg_via_top = ovpackage_module.PackageInfo
    pkg_via_nested = importlib.import_module("ovpackage.datastructures.package").PackageInfo
    assert pkg_via_top is pkg_via_nested


def test_ovpackage_does_not_eagerly_import_native_backends(ovpackage_module):
    # The alias is lazy: optional/native backends (clientlib pulls in
    # omni.client, s3storage pulls in aioboto3, etc.) must not be imported
    # just because someone did `import ovpackage` — they should only load
    # when the caller explicitly asks for them.
    import sys

    for mod in (
        "wrapp.utils.storage_impls.clientlib",
        "wrapp.utils.storage_impls.s3storage",
        "wrapp.utils.storage_impls.azure_blob_storage",
        "wrapp.utils.storage_impls.gcs_storage",
        "wrapp.utils.storage_impls.huggingface_storage",
    ):
        # We can't fully assert "never imported" because earlier tests in
        # the same session may have pulled them in. But at least confirm
        # they're not transitively required by `import ovpackage` itself:
        # if a fresh subprocess imports ovpackage only, the count is 0.
        sys.modules.pop(mod, None)
    # Re-trigger the package __init__ side effects by re-importing
    # nothing relevant — the meta-path finder is already installed.
    # The real coverage is the subprocess test below.

    import subprocess

    code = "import sys; import ovpackage; " "print(','.join(k for k in sys.modules if 'storage_impls' in k))"
    out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    loaded = set(filter(None, out.split(",")))
    forbidden = {
        "wrapp.utils.storage_impls.clientlib",
        "wrapp.utils.storage_impls.s3storage",
        "wrapp.utils.storage_impls.azure_blob_storage",
        "wrapp.utils.storage_impls.gcs_storage",
        "wrapp.utils.storage_impls.huggingface_storage",
    }
    leaked = loaded & forbidden
    assert not leaked, f"`import ovpackage` eagerly loaded native backends: {leaked}"


def test_ovpackage_env_aliases_do_not_override_wrapp_values():
    environ = {
        "OVPACKAGE_REPO": "public-repo",
        "OVPACKAGE_AUTH": "public-auth",
        "WRAPP_AUTH": "internal-auth",
    }

    apply_ovpackage_env_aliases(environ)

    assert environ["WRAPP_REPO"] == "public-repo"
    assert environ["WRAPP_AUTH"] == "internal-auth"


def test_wrapp_package_file_extension_remains_canonical():
    assert is_package_file_name(".package.wrapp")
    assert not is_package_file_name(".package.json")
    assert repository_from_wrapp_file_path("/repo/.packages/pkg/1.0/.pkg.wrapp") == "/repo"
    assert repository_from_wrapp_file_path("/repo/.packages/pkg/1.0/.pkg.json") is None


@pytest.mark.asyncio
async def test_wrappignore_remains_default_ignore_file(monkeypatch):
    checked_paths = []

    async def fake_exists(path):
        checked_paths.append(path)
        return path.endswith(".wrappignore")

    async def fake_read_text_file(path):
        assert path == "/source/.wrappignore"
        return "ignored.txt"

    monkeypatch.setattr(ignore_module, "wrapp_url_exists", fake_exists)
    monkeypatch.setattr(ignore_module, "wrapp_read_text_file", fake_read_text_file)

    evaluator = await IgnoreEvaluator.create_from_file(None, "/source")

    assert checked_paths == ["/source/.wrappignore"]
    assert evaluator is not None
    assert evaluator.is_ignored("/source/ignored.txt")
    assert not evaluator.is_ignored("/source/kept.txt")
