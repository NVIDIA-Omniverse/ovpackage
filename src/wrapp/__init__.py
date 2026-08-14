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

from . import _compat
from ._version import __version__

_compat.apply_ovpackage_env_aliases()
from .api import *
from .commands import *
from .commands.check import CheckResult
from .commands.traceback import (
    PackageLocationType,
    TracebackNode,
)
from .datastructures.authentication import AuthInfo  # deprecated
from .datastructures.authentication import (
    AuthManager,
    StorageAuthInfo,
)
from .datastructures.catalog import (
    Catalog,
    CatalogItem,
    StorageMap,
)
from .datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    SchedulingParameters,
    ThrottlingParameters,
    WrappScheduler,
)
from .datastructures.ignore import (
    IgnoreEvaluator,
    IgnoreFilePredicate,
    IgnorePredicate,
)
from .datastructures.installed_package import InstalledPackage
from .datastructures.manifest import (
    Manifest,
    ManifestFileEntry,
    ManifestMinimalFileEntry,
    ManifestResourceInfo,
)
from .datastructures.package import (
    Dependency,
    DependencySet,
    PackageInfo,
    UniquePackageLocation,
    read_package_info,
    write_package_info,
)
from .datastructures.patch import (
    Patch,
    PatchOpBase,
)
from .datastructures.workspace_analysis import (
    PackageStatusItem,
    PackageStatusItemType,
)
from .options import LsOutputFormat
from .utils.cli_progress_printer import ProgressReportVerbosity
from .utils.progress_report import ProgressReport
from .utils.utils import (
    FailedCommand,
    FailedCommandValueError,
    FailedPatchCommand,
    FileParseException,
    InvalidConfigurationError,
    StorageOperationError,
)
