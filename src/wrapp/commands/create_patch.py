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
    Tuple,
)

import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import (
    CommandParameters,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.patch import (
    Patch,
    PatchOpBase,
)
from wrapp.datastructures.three_way_diff import ThreeWayDiff
from wrapp.options import (
    AuthOption,
    ForceFlag,
    IgnoreFlag,
    InteractiveAuthFlag,
    LogFileOption,
    OverwriteFlag,
    PatchOutputOption,
    ShowFlag,
    StatsFlag,
    TagsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.coro import async_typer
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="create-patch")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def create_patch_command(
    catalog_a: str = typer.Argument(..., help="File name of base catalog for comparison"),
    catalog_b: str = typer.Argument(..., help="File name of second catalog for comparison"),
    base_catalog: str = typer.Argument(..., help="File name of a base line catalog of the last common ancestor"),
    patch: str = PatchOutputOption.better_help(
        help="File name of patch to store the merge operation, to be used with the apply-patch command"
    ),
    tags: bool = TagsFlag.default_help(),
    overwrite: bool = OverwriteFlag,
    ignore: bool = IgnoreFlag,
    force: bool = ForceFlag.better_help(help="Write a patch file including overriding conflicts to ensure A becomes B after applying it"),
    show: bool = ShowFlag,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    verbose: bool = VerboseFlag,
    stats: bool = StatsFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Create a patch file specifying a list of operations that when applied will convert A to B, where the base catalog specifies the last common
    ancestor catalog. This will check if A is unmodified, else it will fail. If you want to keep changes in A but copy over all non-conflicting changes,
    specify the --ignore parameter. In case you want record all operations that will roll back A and make A identical to B, specify the --force
    parameter. Specify --show to get a detailed list of changes planned.
    """
    context, _ = context_from_params()
    try:
        try:
            a_catalog: Catalog = await Catalog.load(catalog_a)
            b_catalog: Catalog = await Catalog.load(catalog_b)
            baseline: Catalog = await Catalog.load(base_catalog)
        except Exception as e:
            raise FailedCommand(f"Failed to load catalog: {e}")

        if ignore and force:
            raise FailedCommand("Argument error - ignore and force options cannot be used together!")

        patch_list, conflict_list = await create_patch(
            a_catalog,
            b_catalog,
            baseline,
            patch=patch,
            tags=tags,
            overwrite=overwrite,
            ignore=ignore,
            force=force,
            context=context,
        )

        if len(patch_list) == 0 and len(conflict_list) == 0:
            print("no differences, empty patch")
        elif show:
            if len(patch_list) > 0:
                print("Patching:")
                for patch_op in patch_list:
                    print(patch_op)
            if len(conflict_list) > 0:
                print("Conflicts:")
                for conflict in conflict_list:
                    print(conflict)
        else:
            patch_text = f"Number of patch operations: {len(patch_list)}" if len(patch_list) > 0 else ""
            conflict_text = f"Number of conflicts: {len(conflict_list)}" if len(conflict_list) > 0 else ""
            print("\n".join([patch_text, conflict_text]))

        if len(conflict_list) > 0 and not ignore and not force:
            # This is not an error, but we want to communicate to the command line whether there are conflicts or not via exit code
            raise typer.Exit(1)

    except typer.Exit as e:
        # Just rethrow to propagate exit status code
        raise e
    except FailedCommand as e:
        logger.error(f"Failure to create patch for catalogs '{catalog_a}' and '{catalog_b}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while creating patch for catalogs '{catalog_a}' and '{catalog_b}': {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


@wrapp_public_api
@log_args
async def create_patch(
    a_catalog: Catalog,
    b_catalog: Catalog,
    baseline: Catalog,
    patch: Optional[str],
    tags: bool = False,
    overwrite: bool = False,
    ignore: bool = False,
    force: bool = False,
    context: CommandParameters = CommandParameters(),
) -> Tuple[List[Tuple[str, PatchOpBase]], List[Tuple[str, PatchOpBase]]]:
    """
    Takes three catalogs, and calculates the patch operation to turn A into B. Returns list of operation and list of conflicts if any.

    Create a patch file specifying a list of operations that when applied will convert A to B, where the base catalog specifies the last common
    ancestor catalog. This will check if A is unmodified, else it will fail. If you want to keep changes in A but copy over all non-conflicting changes,
    specify the --ignore parameter. In case you want record all operations that will roll back A and make A identical to B, specify the --force
    parameter. Specify --show to get a detailed list of changes planned.

    :param a_catalog: The target catalog
    :param b_catalog: The source catalog
    :param baseline: The last common catalog before A and B diverged. For simple merge just specify B again.
    :param patch: Optionally specify a file name to have the patch written out to a file for later use by apply_patch
    :param tags: Flag to respect tags stored in catalogs. If set, differences in tags can cause conflicts and update operations.
    :param overwrite: Specify to allow to overwrite the patch file if it already exists
    :param ignore: Specify whether to ignore conflicting changes in A, e.g. files modified in both sets, or extra files in A. Don't touch conflicting files, leave them as is.
    :param force: Specify whether to rollback changes, guarantee that after apply patch A becomes B.
    :param context: Global configuration parameters

    :return: (patch_operation_list, conflict_list) tuple both being sorted lists of PatchOp instances.  Patch operations must be executed in that order, see PatchOpBase.__lt__.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    a_dict = a_catalog._catalog_to_dict()
    b_dict = b_catalog._catalog_to_dict()
    base_dict = baseline._catalog_to_dict()
    patch_list, conflict_list, extras_list = ThreeWayDiff.diff(a_dict, b_dict, base_dict, a_catalog.root, tags=tags)
    # For patch creation, we assume extras are conflicts. To reconcile, they need to be removed.
    conflict_list += extras_list

    if patch is not None:
        if os.path.exists(patch) and not overwrite:
            raise FailedCommand(f"Can't store patch file as file already exists and --overwrite is not specified: {patch}")

        p = Patch()
        try:
            for op in patch_list:
                p.add(op[1])

            if len(conflict_list) > 0 and not ignore:
                if not force:
                    raise FailedCommand(
                        f"Got {len(conflict_list)} conflict(s), and neither --ignore nor --force is specified. Not writing patch file!"
                    )
                else:
                    for op in conflict_list:
                        p.add(op[1])
                    for op in extras_list:
                        p.add(op[1])
            await p.save(context, patch)
            logger.info(f"Wrote {len(p.ops.operations)} operations to patch file {patch}")
        finally:
            await p.disconnect_tagging_service()
    return patch_list, conflict_list
