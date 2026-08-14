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
from collections import Counter
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import rich
import typer
from rich.table import Table

from wrapp.utils.api_marker import wrapp_public_api

from ..app import (
    app,
    measure_time,
    print_stats,
)
from ..datastructures.authentication import capture_auth
from ..datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from ..datastructures.ignore import (
    IgnoreEvaluator,
    create_ignore_eval_from_params,
)
from ..datastructures.workspace_analysis import (
    PackageStatusItem,
    PackageStatusItemType,
    check_dirty_state,
    collect_workspace_info,
)
from ..options import (
    AuthOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    IgnoreFileOption,
    InteractiveAuthFlag,
    JobsNumber,
    JsonLoggingFlag,
    LogFileOption,
    LsOutputFormat,
    ProgressReportOption,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from ..utils.cli_progress_printer import ProgressReportVerbosity
from ..utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from ..utils.ensure_scheduler import ensure_scheduler
from ..utils.logging import log_args
from ..utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_base_directory_from_url,
)
from ..utils.wrapp_init_for_cmd import wrapp_init_for_cmd

logger = logging.getLogger("omni.wrapp")


@app.command(name="status")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def status_command(
    wrapp_file: str,
    output_format: LsOutputFormat = typer.Option(
        LsOutputFormat.normal,
        "--output-format",
        "--of",
        case_sensitive=False,
        help="Specify the output format of the command",
    ),
    ignore_file: str = IgnoreFileOption,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    jobs: int = JobsNumber,
    file_transfer_jobs: int = FileTransferJobsNumber,
    stats: bool = StatsFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    time: bool = TimeFlag,
):
    """
    Determine the status of a workspace which is specified by a top level wrapp file
    """
    context, scheduler_params = context_from_params()
    try:
        wrapp_cli_scheduler = WrappSchedulerCLI(params=scheduler_params)
        async with wrapp_cli_scheduler as scheduler:
            dirty_packages, errors_per_package = await status(wrapp_file, ignores=ignore_file, context=context, scheduler=scheduler)
            if len(dirty_packages) > 0:
                if output_format == LsOutputFormat.normal:
                    output_table = Table(box=None)
                    output_table.add_column("Package")
                    output_table.add_column("Modification summary")
                elif output_format == LsOutputFormat.TSV:
                    print("\t".join(["Package", "Error Type", "Description", "Wrapp file URL"]))
                for package in dirty_packages:
                    if output_format == LsOutputFormat.short:
                        # Short form - only print path of dirty wrapp file
                        print(package)
                    elif output_format == LsOutputFormat.normal:
                        # Normal format, print a nice table with two columns and a summary
                        name = errors_per_package[package][0].package_name
                        output_table.add_row(name, generate_error_summary(errors_per_package[package]))
                    elif output_format == LsOutputFormat.TSV:
                        # The TSV format gets the full details for each error
                        for error in errors_per_package[package]:
                            print("\t".join([error.package_name, error.status_item_type, error.status_message, package]))
                    else:
                        raise Exception(f"Unknown output format: {output_format}")

                if output_format == LsOutputFormat.normal:
                    if wrapp_cli_scheduler.cli_progress_reporter() is not None:
                        wrapp_cli_scheduler.cli_progress_reporter().live_display().console.print(output_table)
                    else:
                        rich.Console().print(output_table)
                raise typer.Exit(1)
            else:
                if output_format == LsOutputFormat.normal:
                    print("all clean")

    except typer.Exit as e:
        # Just rethrow to propagate exit status code
        raise e
    except FailedCommand as e:
        logger.error(f"Failure to calculate wrapp status for '{wrapp_file}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Storage error during calculating wrapp status for '{wrapp_file}': {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def status(
    wrapp_file: str,
    ignores: Optional[Union[str, IgnoreEvaluator]] = None,
    *,
    ignore_file: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> Tuple[List[str], Dict[str, List[PackageStatusItem]]]:
    """
    Given the URL for a top level wrapp file, return an ordered list of packages that are dirty, and a map of
    wrapp file to the list of detailed PackageStatusItem entries found in that subpackage.

    :param wrapp_file: Full URL for the top level wrapp file
    :param ignores: Specify the name of an ignore file containing rules for ignoring items, or an IgnoreEvaluator object
    :param ignore_file: Deprecated, use ignores instead. Specify the name of an ignore file containing rules for ignoring items
    :param context: Optionally, global configuration parameters.
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    scheduler = scheduler.create_sub_context_and_node(f"Analysing state of '{wrapp_file}'", expected_num_sub_jobs=2)

    # Resolve ignores (handles deprecated ignore_file parameter)
    root_dir = get_base_directory_from_url(wrapp_file)
    ignore_rules = await create_ignore_eval_from_params(ignores, root_dir, ignore_file=ignore_file)

    # Use the shared utility functions
    package, workspace_catalog, package_info, sub_package_list, extra_files = await collect_workspace_info(
        wrapp_file, ignore_rules, context=context, scheduler=scheduler
    )

    # Check for dirty state
    return await check_dirty_state(
        package,
        workspace_catalog,
        sub_package_list,
        extra_files,
        root_dir,
        ignore_rules,
        scheduler=scheduler,
    )


def generate_error_summary(errors: list[PackageStatusItem]) -> Optional[str]:
    """
    Given a list of PackageStatusItem items, generates a summary string counting the number of occurrences of each error type.

    Returns:
        A formatted human-readable string summarizing the count of each error type
    """
    if not errors or len(errors) == 0:
        return None

    # Count occurrences of each error type. These need to be all from the same package though
    assert all(errors[0].absolute_wrapp_file_path == x.absolute_wrapp_file_path for x in errors)
    error_counts = Counter([error.status_item_type for error in errors])

    # Generate the summary string
    items = []

    # Add each error type and its count to the summary
    error_message = {
        PackageStatusItemType.PACKAGE_MODIFIED: ("{} modified", "package", "packages"),
        PackageStatusItemType.SUBPACKAGE_MODIFIED: ("{} modified", "subpackage", "subpackages"),
        PackageStatusItemType.PACKAGE_VERSION_MISMATCH: ("subpackage version {}", "mismatch", "mismatches"),
        PackageStatusItemType.PACKAGE_MISSING: ("missing {}", "subpackage", "subpackages"),
        PackageStatusItemType.EXTRA_PACKAGE: ("extra {}", "package", "packages"),
        PackageStatusItemType.PACKAGE_NO_CATALOG: ("draft {}", "package", "packages"),
        PackageStatusItemType.ITEM_DIFFERS: ("{}", "item differs", "items differ"),
        PackageStatusItemType.ITEM_MISSING: ("missing {}", "item", "items"),
        PackageStatusItemType.EXTRA_ITEM: ("extra {}", "item", "items"),
    }
    for error_type, count in sorted(error_counts.items()):
        if error_type == PackageStatusItemType.PACKAGE_MODIFIED:
            # This is the general marker that a package is modified.
            # We already know that, so don't include it in the count
            continue
        error_text, singular_word, plural_word = error_message[error_type]
        items.append(f"{count} {error_text.format(singular_word if count == 1 else plural_word)}")

    return ", ".join(items)
