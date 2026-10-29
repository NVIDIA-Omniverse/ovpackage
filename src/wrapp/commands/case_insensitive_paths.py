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

import logging

from wrapp.datastructures.catalog import Catalog
from wrapp.utils.storage import wrapp_is_case_sensitive
from wrapp.utils.utils import FailedCommand

logger = logging.getLogger("omni.wrapp")


def is_case_insensitive_destination(destination: str) -> bool:
    return not wrapp_is_case_sensitive(destination)


def _catalog_case_insensitive_path_conflicts(catalog: Catalog) -> list[str]:
    has_conflicts, conflicting_paths = catalog.check_case_insensitive_path_conflicts()
    if not has_conflicts:
        return []
    return conflicting_paths


def _case_insensitive_path_conflicts(catalog: Catalog, destination: str) -> list[str]:
    if not is_case_insensitive_destination(destination):
        return []

    return _catalog_case_insensitive_path_conflicts(catalog)


def _format_case_insensitive_path_conflicts(conflicting_paths: list[str]) -> str:
    return (
        f"Cross platform case insensitive path conflicts found: {conflicting_paths}. "
        "Please ensure that the paths are unique on all platforms (Windows, Linux, macOS)."
    )


def raise_if_case_insensitive_path_conflicts(catalog: Catalog, destination: str) -> None:
    conflicting_paths = _case_insensitive_path_conflicts(catalog, destination)
    if conflicting_paths:
        raise FailedCommand(_format_case_insensitive_path_conflicts(conflicting_paths))


def warn_if_case_insensitive_path_conflicts(catalog: Catalog, destination: str) -> None:
    conflicting_paths = _catalog_case_insensitive_path_conflicts(catalog)
    if conflicting_paths:
        logger.warning(
            "%s The package was created, but will not work on case-insensitive filesystems.",
            _format_case_insensitive_path_conflicts(conflicting_paths),
        )
