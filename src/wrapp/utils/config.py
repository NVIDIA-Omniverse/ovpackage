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

"""Optional ``wrapp.toml`` configuration file.

WRAPP looks for a TOML config file in the following order of precedence:

1. The path in the ``WRAPP_CONFIG_FILE`` environment variable, if set.
2. ``~/.nvidia-omniverse/config/wrapp.toml`` (the default location).

A missing file is not an error — `load_wrapp_config()` returns an empty
:class:`WrappConfig` and logs at DEBUG. A malformed file or unknown root key
fails fast with :class:`InvalidConfigurationError` so that overrides are
never silently dropped.

Schema (only the keys understood by the current WRAPP version are listed; the
``[storage_api.<name>]`` shape is reserved for future multi-backend support
but in this release only the entry named ``default`` is honored):

.. code-block:: toml

    [storage_api.default]
    # Optional. Default discovery URL. Overridden by
    # WRAPP_STORAGE_API_DISCOVERY_URL when that env var is set.
    discovery_url = "https://storage-api.example.com"

    # Optional. When set, replaces the route table that the Storage API
    # server advertises via ListRoutes / ListTopLevelAddresses. The server's
    # response is still fetched and logged for diagnostics, but routing
    # decisions use this list.
    routes = [
        "omniverse://content.ov.nvidia.com/**",
        "https://content.ov.nvidia.com/**",
    ]

Precedence (top wins):

1. Environment variables (e.g. ``WRAPP_STORAGE_API_DISCOVERY_URL``).
2. Values in ``wrapp.toml``.
3. Built-in defaults.

This lets a user keep their default endpoint in the TOML file and override
it ad-hoc per-shell for tests / reproduction without editing the file.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import (
    dataclass,
    field,
)
from pathlib import Path
from typing import (
    Any,
    Dict,
    Optional,
    Tuple,
)

from wrapp.utils.utils import InvalidConfigurationError

logger = logging.getLogger("omni.wrapp")

DEFAULT_CONFIG_PATH = Path("~/.nvidia-omniverse/config/wrapp.toml")
CONFIG_PATH_ENV_VAR = "WRAPP_CONFIG_FILE"

# Keys understood at the top level of the TOML document. Anything else is
# rejected at load time so that typos do not silently drop overrides.
_KNOWN_TOP_LEVEL_KEYS = frozenset({"storage_api"})

# Keys understood inside each ``[storage_api.<name>]`` table.
_KNOWN_STORAGE_API_KEYS = frozenset({"discovery_url", "routes"})

# In this release, only the backend named ``default`` is wired into the
# Router. The schema accepts other names so that users can start preparing
# their config for a future multi-backend release, but we log a clear
# message when we see them so the override is never silent.
_ACTIVE_STORAGE_API_BACKEND_NAME = "default"


@dataclass(frozen=True)
class StorageAPIBackendConfig:
    """Per-backend Storage API overrides loaded from ``wrapp.toml``."""

    name: str
    discovery_url: Optional[str] = None
    routes: Optional[Tuple[str, ...]] = None


@dataclass(frozen=True)
class WrappConfig:
    """In-memory representation of the ``wrapp.toml`` config file.

    ``source_path`` is ``None`` when no config file was loaded (either
    because none was found at the resolved path, or because the loader was
    called with no path).
    """

    source_path: Optional[Path] = None
    storage_api_backends: Tuple[StorageAPIBackendConfig, ...] = field(default_factory=tuple)

    @property
    def default_storage_api(self) -> Optional[StorageAPIBackendConfig]:
        """Return the ``[storage_api.default]`` entry, or None if absent."""
        for backend in self.storage_api_backends:
            if backend.name == _ACTIVE_STORAGE_API_BACKEND_NAME:
                return backend
        return None


def _resolve_config_path(explicit_path: Optional[Path]) -> Path:
    if explicit_path is not None:
        return explicit_path.expanduser()
    env_path = os.environ.get(CONFIG_PATH_ENV_VAR)
    if env_path:
        return Path(env_path).expanduser()
    return DEFAULT_CONFIG_PATH.expanduser()


def _check_unknown_keys(table: Dict[str, Any], known: frozenset, location: str, source_path: Path) -> None:
    unknown = sorted(k for k in table.keys() if k not in known)
    if unknown:
        raise InvalidConfigurationError(f"{source_path}: unknown key(s) {unknown} in {location}; " f"recognized keys are {sorted(known)}")


def _parse_storage_api_section(raw: Any, source_path: Path) -> Tuple[StorageAPIBackendConfig, ...]:
    if not isinstance(raw, dict):
        raise InvalidConfigurationError(
            f"{source_path}: [storage_api] must be a table of named backends " f"(e.g. [storage_api.default]); got {type(raw).__name__}"
        )

    backends: list[StorageAPIBackendConfig] = []
    for backend_name, backend_table in raw.items():
        if not isinstance(backend_table, dict):
            raise InvalidConfigurationError(
                f"{source_path}: [storage_api.{backend_name}] must be a table; " f"got {type(backend_table).__name__}"
            )
        _check_unknown_keys(backend_table, _KNOWN_STORAGE_API_KEYS, f"[storage_api.{backend_name}]", source_path)

        discovery_url = backend_table.get("discovery_url")
        if discovery_url is not None and not isinstance(discovery_url, str):
            raise InvalidConfigurationError(
                f"{source_path}: [storage_api.{backend_name}].discovery_url must be a string; " f"got {type(discovery_url).__name__}"
            )

        routes_raw = backend_table.get("routes")
        routes: Optional[Tuple[str, ...]] = None
        if routes_raw is not None:
            if not isinstance(routes_raw, list) or not all(isinstance(p, str) for p in routes_raw):
                raise InvalidConfigurationError(f"{source_path}: [storage_api.{backend_name}].routes must be a list of strings")
            routes = tuple(routes_raw)

        backends.append(
            StorageAPIBackendConfig(
                name=backend_name,
                discovery_url=discovery_url or None,
                routes=routes,
            )
        )

    return tuple(backends)


def load_wrapp_config(path: Optional[Path] = None) -> WrappConfig:
    """Load and validate the ``wrapp.toml`` config file.

    Path resolution:

    1. ``path`` argument, if not None.
    2. ``$WRAPP_CONFIG_FILE``, if set.
    3. ``~/.nvidia-omniverse/config/wrapp.toml``.

    A missing file is not an error — an empty :class:`WrappConfig` is
    returned and a DEBUG log line is emitted. Malformed TOML, unknown root
    keys, or wrong value types raise :class:`InvalidConfigurationError` so
    that user mistakes are not silently absorbed.
    """
    try:
        resolved = _resolve_config_path(path)
    except RuntimeError as e:
        # Path.expanduser() raises when no home directory can be resolved
        # (clean subprocess env, locked-down service accounts, ...). The
        # config is optional, so treat this the same as "file absent".
        logger.debug("Skipping wrapp config: %s", e)
        return WrappConfig()

    if not resolved.is_file():
        logger.debug("No wrapp config file at %s", resolved)
        return WrappConfig()

    try:
        with resolved.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise InvalidConfigurationError(f"Failed to parse wrapp config {resolved}: {e}") from e
    except OSError as e:
        raise InvalidConfigurationError(f"Failed to read wrapp config {resolved}: {e}") from e

    _check_unknown_keys(raw, _KNOWN_TOP_LEVEL_KEYS, "top level", resolved)

    storage_api_section = raw.get("storage_api")
    storage_api_backends: Tuple[StorageAPIBackendConfig, ...] = ()
    if storage_api_section is not None:
        storage_api_backends = _parse_storage_api_section(storage_api_section, resolved)

    config = WrappConfig(source_path=resolved, storage_api_backends=storage_api_backends)

    backend_names = sorted(b.name for b in storage_api_backends)
    logger.info("Loaded wrapp config from %s; storage_api backends: %s", resolved, backend_names or "<none>")
    inactive = [n for n in backend_names if n != _ACTIVE_STORAGE_API_BACKEND_NAME]
    if inactive:
        logger.info(
            "wrapp config: backend(s) %s are reserved for future multi-backend support; "
            "only [storage_api.%s] is honored in this WRAPP version",
            inactive,
            _ACTIVE_STORAGE_API_BACKEND_NAME,
        )

    return config
