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
import sys
from collections.abc import MutableMapping

WRAPP_ENV_PREFIX = "WRAPP_"
OVPACKAGE_ENV_PREFIX = "OVPACKAGE_"


def is_ovpackage_invocation() -> bool:
    """True when the running executable was launched as ``ovpackage`` (or any
    name starting with ``ovpackage``). Used to gate ovpackage-specific
    behavior such as hiding the Nucleus backend, which is a legacy
    wrapp-only backend with no place in the public ovpackage surface."""
    argv0 = sys.argv[0] if sys.argv else ""
    return os.path.basename(argv0).startswith("ovpackage")


def public_env_prefix() -> str:
    """Return the user-facing env-var prefix that matches the invoked executable.

    When the script was launched as ``ovpackage`` (or any name starting with
    ``ovpackage``) this returns ``OVPACKAGE_``; otherwise ``WRAPP_``. Used so
    diagnostic messages reference the env-var names the user actually sets,
    even though WRAPP_* remains the authoritative internal name.
    """
    if is_ovpackage_invocation():
        return OVPACKAGE_ENV_PREFIX
    return WRAPP_ENV_PREFIX


def apply_ovpackage_env_aliases(environ: MutableMapping[str, str] | None = None) -> None:
    """Mirror OVPACKAGE_* environment variables to WRAPP_* compatibility names.

    WRAPP_* remains authoritative when both names are provided. This keeps the
    current implementation and existing users stable while allowing the external
    ovpackage CLI/API to document OVPACKAGE_* names.
    """
    if environ is None:
        environ = os.environ

    for key, value in list(environ.items()):
        if not key.startswith(OVPACKAGE_ENV_PREFIX):
            continue
        wrapp_key = WRAPP_ENV_PREFIX + key[len(OVPACKAGE_ENV_PREFIX) :]
        if wrapp_key not in environ:
            environ[wrapp_key] = value
