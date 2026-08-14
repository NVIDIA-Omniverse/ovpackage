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
import os
from typing import (
    List,
    Optional,
    Union,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.catalog import catalog
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.ignore import (
    IgnoreEvaluator,
    create_ignore_eval_from_params,
)
from wrapp.datastructures.package import (
    PackageInfo,
    validate_package_and_version,
    write_package_info,
)
from wrapp.datastructures.package_standard import write_package_standard_definition_seed
from wrapp.options import (
    AuthOption,
    CreateCatalogFlag,
    CreatePackageStandardOption,
    DebugLevelFlag,
    HashLocalFlag,
    IgnoreFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    ProgressReportOption,
    StatsFlag,
    TaggingJobsNumber,
    TagsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.logging import log_args
from wrapp.utils.storage import wrapp_url_exists
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    join_paths,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

logger = logging.getLogger("omni.wrapp")


@app.command(name="new")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def new_command(
    package_name: str = typer.Argument(..., help="Specify the name of the package"),
    version: str = typer.Argument(..., help="Specify the initial version string"),
    destination: Optional[str] = typer.Argument(
        None, help="Specify the target URL where to create the file (defaults to the current directory)"
    ),
    create_catalog: bool = CreateCatalogFlag,
    create_package_standard_license: Optional[str] = CreatePackageStandardOption,
    local_hash: bool = HashLocalFlag,
    ignore_file: str = IgnoreFileOption,
    tags: bool = TagsFlag.default_help(),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    tagging_jobs: int = TaggingJobsNumber,
    stats: bool = StatsFlag,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,
) -> None:
    """
    This creates a new .<package>.wrapp package file that can be used as a target for the install-command to record dependencies.
    Name and version are just strings, destination needs to be a URL pointing to a folder.
    \n\n
    Example:
    \n\n
        wrapp new vegetation_assets 1.0.0 omniverse://localhost/asset_library/vegetation
    """

    if destination is None:
        destination = os.getcwd()

    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            await new(
                package_name,
                version,
                destination,
                create_catalog,
                tags=tags,
                ignores=ignore_file,
                local_hash=local_hash,
                create_package_standard_license=create_package_standard_license,
                context=context,
                scheduler=scheduler,
            )
    except FailedCommand as e:
        logger.error(f"Failure to create new package file for '{package_name}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Storage error during creation of new package file for '{package_name}', operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def new(
    package_name: str,
    version: str,
    destination: str,
    create_catalog: bool,
    tags: bool = False,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    local_hash: bool = False,
    create_package_standard_license: Optional[str] = None,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Takes package name and version, and creates a new .<package>.wrapp file

    This creates a new .<package>.wrapp package file that can be used as a target for the install-command to record dependencies.
    Name and version are just strings, destination needs to be a URL pointing to a folder.

    :param package_name: Name of the package
    :param version: Initial version. Can be overridden by the first create command for this package
    :param destination: URL of the package. In this directory the new .wrapp file will be created.
    :param create_catalog: Specify whether to catalog the location and put the catalog into the wrapp file created.
    :param tags: Specify this to also catalog the tags when cataloging the destination
    :param ignores: Specify a name of an ignore rule file to use during cataloging, or an IgnoreEvaluator object
    :param local_hash: Set this to allow calculating local hashes during cataloging. Might download data when creating in a remote server.
    :param create_package_standard_license: SPDX license identifier. When set, a package-standard definition seed
        (identity only, no BOM) is written next to the new .wrapp file. A later ``create`` from this directory picks
        up the license automatically without re-specifying it.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("package_name", package_name)
    validate_non_empty_string("version", version)
    validate_non_empty_string("destination", destination)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    # Resolve ignores (handles deprecated ignore_file parameter)
    ignore_rules = await create_ignore_eval_from_params(ignores, destination)

    validate_package_and_version(package_name, version)
    package_file_name = f".{package_name}.wrapp"
    full_package_path = join_paths(destination, package_file_name)
    if await wrapp_url_exists(full_package_path):
        raise FailedCommand(f"File already exists: {full_package_path}")
    catalog_to_create = None
    if create_catalog:
        scheduler = scheduler.create_sub_context_and_node(f"New '{package_name}' '{version}'", expected_num_sub_jobs=1)
        catalog_to_create = await catalog(
            root_dir=destination,
            checkpoints=False,
            tags=tags,
            ignores=ignore_rules,
            local_hash=local_hash,
            context=context,
            scheduler=scheduler,
        )
    package_info = PackageInfo(name=package_name, version=version, catalog=catalog_to_create, dependencies=None, repository=None)
    await write_package_info(full_package_path, package_info, context)

    if create_package_standard_license is not None and create_package_standard_license.strip():
        await write_package_standard_definition_seed(
            full_package_path,
            package_id=f"{package_name}.{version}",
            license_id=create_package_standard_license.strip(),
            context=context,
        )
