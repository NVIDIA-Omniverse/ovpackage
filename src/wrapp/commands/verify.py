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

import itertools
import logging
import os
import pathlib
from typing import (
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger("omni.wrapp")

try:
    import httpx

    g_httpx_available = True
except ImportError as e:
    logger.info(f"DISABLED: verify feature. Failed to import {e.name}. Exception: {e}")
    g_httpx_available = False

import pydantic
import typer

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.catalog import catalog as catalog_func
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import (
    Catalog,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.references import CollectedReferences
from wrapp.options import (
    AuthOption,
    FileListFlag,
    IgnoreFileOption,
    InteractiveAuthFlag,
    LogFileOption,
    OutputCatalogOption,
    OverwriteFlag,
    StatsFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.utils.coro import async_typer
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.microservice import MicroserviceManager
from wrapp.utils.storage import (
    Result,
    wrapp_get_server_info,
    wrapp_url_exists,
    wrapp_url_is_folder,
)
from wrapp.utils.utils import (
    FailedCommand,
    MicroServiceOperationError,
    StorageOperationError,
    get_base_directory_from_url,
    is_relative_path,
    make_file_url_absolute,
    make_relative_url,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args


@app.command(name="catalog-with-ref", hidden=not os.environ.get("WRAPP_ENABLE_DEV_FEATURES", False))
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def collect_command(
    source: str = typer.Argument(..., help="A URL to a file, a source folder or a file list to be cataloged."),
    output_catalog_path: str = typer.Argument(..., help="Output catalog which can be used for create etc"),
    file_list: bool = FileListFlag,
    root: Optional[str] = typer.Option(None, help="The root directory used for the catalog"),
    service_url: Optional[str] = typer.Option(None, help="The server used for the wrapp microservice.", envvar="WRAPP_COLLECT_SERVICE_URL"),
    ignore_file: Optional[str] = IgnoreFileOption,
    output_path: Optional[str] = typer.Option(None, help="Path to the output file"),
    overwrite: bool = OverwriteFlag,
    ignore_errors: bool = typer.Option(False, "--ignore-errors", "--ie", help="Create the catalog even if it is not complete."),
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    auth_token: Optional[str] = typer.Option(
        None, help="An optional auth token to log into the microservice", envvar="WRAPP_COLLECT_AUTH_TOKEN"
    ),
    kit_exe_path: Optional[pathlib.Path] = typer.Option(
        None,
        file_okay=True,
        exists=True,
        help="Only supported if you run the tool from source. "
        "Automatically start the microservice on the localhost with "
        "specified executable using port <service-port> "
        "if it is not already running. Ignored if <service-url> is set.",
    ),
    kit_root_path: Optional[pathlib.Path] = typer.Option(
        None,
        dir_okay=True,
        help="If kit_exe_path is set, use this directory as the root folder "
        "for the started Kit process. If not set, a temporary directory "
        "will be used. Ignored if <kit-exe-path> is not set.",
    ),
    service_port: int = typer.Option(
        9211,
        help="The port number used for the localhost microservice. Defaults to the "
        "value of the environment variable WRAPP_SERVICE_PORT, or 9211 if that "
        "is not set. Ignored if <kit-exe-path> is not set.",
        envvar="WRAPP_SERVICE_PORT",
    ),
    stop_service: bool = typer.Option(
        True,
        help="Automatically stop the microservice if it was started by wrapp "
        "once wrapp terminates. Ignored if <kit-exe-path> is not set.",
    ),
    debug_service: bool = typer.Option(
        False, help="Start the service such that it connects to a PyCharm debugger. " "Ignored if <kit-exe-path> is not set."
    ),
    stats: bool = StatsFlag,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Create a catalog from the specified files and all their references.
    """

    if not g_httpx_available:
        raise FailedCommand("Collect is not available - missing dependencies!")

    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            if await wrapp_url_exists(output_catalog_path) and not overwrite:
                raise FailedCommand(f"Catalog {output_catalog_path} already exists and --overwrite is not specified!")

            if not service_url:
                if not kit_exe_path:
                    kit_exe_path_str, kit_root_path_str = MicroserviceManager.find_kit_exe()
                    kit_exe_path = pathlib.Path(kit_exe_path_str)
                    kit_root_path = pathlib.Path(kit_root_path_str)
                if not kit_exe_path or not os.path.exists(kit_exe_path):
                    raise FailedCommand(f"Kit could not be found in '{kit_exe_path}'.")
                await MicroserviceManager.start(
                    str(kit_exe_path), str(kit_root_path), service_port, auto_shutdown=stop_service, debug_service=debug_service
                )
                service_url = MicroserviceManager.service_host()

            keep_auth_alive = None
            if auth_token:
                from wrapp.utils.storage_impls.clientlib import (
                    wrapp_register_authentication_callback,
                )

                keep_auth_alive = wrapp_register_authentication_callback(lambda _: ("$omni-api-token", auth_token))

            source = make_file_url_absolute(source)
            if not await wrapp_url_exists(source):
                raise FailedCommand(f"Cannot find source '{source}'")
            catalog_struct: Catalog
            if file_list:
                logger.info(f"Reading file list {source}...")
                try:
                    catalog_struct = await catalog_func(
                        file_list=source, tags=False, ignores=ignore_file, context=context, scheduler=scheduler
                    )
                except Exception as e:
                    raise FailedCommand(f"Failed to read file list {source}: {str(e)}") from e
            elif await wrapp_url_is_folder(source):
                logger.info(f"Building catalog for directory {source}...")
                try:
                    catalog_struct = await catalog_func(source, tags=False, ignores=ignore_file, context=context, scheduler=scheduler)
                except Exception as e:
                    raise FailedCommand(f"Failed to create catalog for {source}: {str(e)}") from e
            else:
                logger.info(f"Reading file {source}...")
                try:
                    if not root:
                        root = get_base_directory_from_url(source)
                    catalog_struct = await catalog_func(
                        file_list=[(root, source)], tags=False, ignores=ignore_file, context=context, scheduler=scheduler
                    )
                except Exception as e:
                    raise FailedCommand(f"Failed to read file list {source}: {str(e)}") from e
                try:
                    logger.info(f"Loading catalog from {source}...")
                    catalog_struct = await Catalog.load(source)
                except Exception as e:
                    raise FailedCommand(f"Failed to read catalog from {source} for verify.") from e

            # We can get the auth token from the get_server_info
            if auth_token is None:
                result, server_info = await wrapp_get_server_info(source)
                if result == Result.OK:
                    auth_token = server_info.auth_token

            if root:
                catalog_struct.root = root
            refs: CollectedReferences = await verify(
                catalog_struct, include_all_references=True, auth_token=auth_token, service_url=service_url, context=context
            )

            if verbose:
                if refs.unresolved_items:
                    logger.warning("References in the root directory which cannot be resolved:")
                    for i, ius in refs.unresolved_items.items():
                        logger.warning(f"  '{i}'")
                if refs.external_items:
                    logger.warning("References outside of the root directory:")
                    for i, ius in refs.external_items.items():
                        logger.warning(f"  '{i}'")
            if output_path:
                await refs.save(context, output_path)
                if verbose:
                    logger.info(f"Output written to '{output_path}'")
            if not refs.file_set_complete:
                logger.warning(
                    f"Catalog detected {len(refs.unresolved_items)} unresolved and {len(refs.external_items)} "
                    f"external references of a total of {len(refs.resolved_items) + len(refs.unresolved_items) + len(refs.external_items)} references in '{source}'"
                )
                if not ignore_errors:
                    raise FailedCommand(f"Catalog is incomplete, catalog failed!")
            if output_catalog_path:
                await _save_references_as_catalog(
                    context,
                    scheduler,
                    output_catalog_path,
                    catalog_struct.root,
                    refs,
                    ignore_file,
                    ignore_externals=ignore_errors,
                    ignore_unresolved=ignore_errors,
                )
                logger.info(f"Catalog '{output_catalog_path}' written for '{source}'.")
            else:
                logger.info(f"Catalog successfully completed for '{source}'.")
    except typer.Exit:
        # Just rethrow to propagate exit status code
        raise
    except FailedCommand as e:
        logger.error(f"Failed to catalog {source} with references: {e}")
        raise typer.Exit(1000)
    except StorageOperationError as e:
        logger.error(f"Error while cataloging {source} with references: {e}")
        raise typer.Exit(2000)
    except Exception as e:
        log_exception_and_raise_exit(e, context, exit_code=3000)


@app.command(name="verify", hidden=not os.environ.get("WRAPP_ENABLE_DEV_FEATURES", False))
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def verify_command(
    source: str = typer.Argument(..., help="A URL to a source folder or a catalog file to be checked for " "consistency and completeness."),
    file_list: bool = FileListFlag,
    service_url: str = typer.Option(None, help="The server used for the wrapp microservice.", envvar="WRAPP_COLLECT_SERVICE_URL"),
    ignore_file: str = IgnoreFileOption,
    output_path: str = typer.Option(None, help="Path to the output file"),
    output_catalog_path: str = OutputCatalogOption,
    overwrite: bool = OverwriteFlag,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    auth_token: Optional[str] = typer.Option(
        None, help="An optional auth token to log into the microservice", envvar="WRAPP_COLLECT_AUTH_TOKEN"
    ),
    kit_exe_path: pathlib.Path = typer.Option(
        None,
        file_okay=True,
        exists=True,
        help="Only supported if you run the tool from source. "
        "Automatically start the microservice on the localhost with "
        "specified executable using port <service-port> "
        "if it is not already running. Ignored if <service-url> is set.",
    ),
    kit_root_path: pathlib.Path = typer.Option(
        None,
        dir_okay=True,
        help="If kit_exe_path is set, use this directory as the root folder "
        "for the started Kit process. If not set, a temporary directory "
        "will be used. Ignored if <kit-exe-path> is not set.",
    ),
    service_port: int = typer.Option(
        os.getenv("WRAPP_SERVICE_PORT", 9211),
        help="The port number used for the localhost microservice. Defaults to the "
        "value of the environment variable WRAPP_SERVICE_PORT, or 9211 if that "
        "is not set. Ignored if <kit-exe-path> is not set.",
    ),
    stop_service: bool = typer.Option(
        True,
        help="Automatically stop the microservice if it was started by wrapp "
        "once wrapp terminates. Ignored if <kit-exe-path> is not set.",
    ),
    debug_service: bool = typer.Option(
        False, help="Start the service such that it connects to a PyCharm debugger. " "Ignored if <kit-exe-path> is not set."
    ),
    stats: bool = StatsFlag,
    verbose: bool = VerboseFlag,
    log_file: str = LogFileOption,
    time: bool = TimeFlag,
) -> None:
    """
    Verify that all references in the files in the given file set are referencing files inside the file set.
    """
    if not g_httpx_available:
        raise FailedCommand("Verify is not available - missing dependencies!")

    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            if output_catalog_path and await wrapp_url_exists(output_catalog_path) and not overwrite:
                raise FailedCommand(f"Catalog {output_catalog_path} already exists and --overwrite is not specified!")

            if not service_url:
                if not kit_exe_path:
                    kit_exe_path_str, kit_root_path_str = MicroserviceManager.find_kit_exe()
                    kit_exe_path = pathlib.Path(kit_exe_path_str)
                    kit_root_path = pathlib.Path(kit_root_path_str)
                if not kit_exe_path or not os.path.exists(kit_exe_path):
                    raise FailedCommand(f"Kit could not be found in '{kit_exe_path}'.")
                await MicroserviceManager.start(
                    str(kit_exe_path), str(kit_root_path), service_port, auto_shutdown=stop_service, debug_service=debug_service
                )
                service_url = MicroserviceManager.service_host()

            keep_auth_alive = None
            if auth_token:
                from wrapp.utils.storage_impls.clientlib import (
                    wrapp_register_authentication_callback,
                )

                keep_auth_alive = wrapp_register_authentication_callback(lambda _: ("$omni-api-token", auth_token))

            source = make_file_url_absolute(source)
            if not await wrapp_url_exists(source):
                raise FailedCommand(f"Cannot find source '{source}'")
            catalog_struct: Catalog
            if file_list:
                logger.info(f"Reading file list {source}...")
                try:
                    catalog_struct = await catalog_func(
                        file_list=source, tags=False, ignores=ignore_file, context=context, scheduler=scheduler
                    )
                except Exception as e:
                    raise FailedCommand(f"Failed to read file list {source}: {str(e)}") from e
            elif await wrapp_url_is_folder(source):
                logger.info(f"Building catalog for directory {source}...")
                try:
                    catalog_struct = await catalog_func(source, tags=False, ignores=ignore_file, context=context, scheduler=scheduler)
                except Exception as e:
                    raise FailedCommand(f"Failed to create catalog for {source}: {str(e)}") from e
            else:
                try:
                    logger.info(f"Loading catalog from {source}...")
                    catalog_struct = await Catalog.load(source)
                except Exception as e:
                    raise FailedCommand(f"Failed to read catalog from {source} for verify.") from e

            # We can get the auth token from the get_server_info
            if auth_token is None:
                result, server_info = await wrapp_get_server_info(source)
                if result == Result.OK:
                    auth_token = server_info.auth_token

            refs: CollectedReferences = await verify(
                catalog_struct, include_all_references=True, auth_token=auth_token, service_url=service_url, context=context
            )

            if verbose:
                if refs.unresolved_items:
                    print("References in the root directory which cannot be resolved:")
                    for i, ius in refs.unresolved_items.items():
                        print(f"  '{i}'")
                if refs.external_items:
                    print("References outside of the root directory:")
                    for i, ius in refs.external_items.items():
                        print(f"  '{i}'")
            if output_path:
                await refs.save(context, output_path)
                if verbose:
                    logger.info(f"Output written to '{output_path}'")
            if output_catalog_path:
                await _save_references_as_catalog(
                    context,
                    scheduler,
                    output_catalog_path,
                    catalog_struct.root,
                    refs,
                    ignore_file,
                    ignore_externals=True,
                    ignore_unresolved=True,
                )
            if not refs.file_set_complete:
                print(
                    f"Verify detected {len(refs.unresolved_items)} unresolved and {len(refs.external_items)} "
                    f"external references of a total of {len(refs.resolved_items) + len(refs.unresolved_items) + len(refs.external_items)} references in '{source}'"
                )
                raise typer.Exit(1)
            else:
                print(f"Verify successfully completed for '{source}'!")
    except typer.Exit:
        # Just rethrow to propagate exit status code
        raise
    except FailedCommand as e:
        logger.error(f"Failed to verify {source}: {e}")
        raise typer.Exit(1000)
    except StorageOperationError as e:
        logger.error(f"Error while verifying {source}: {e}")
        raise typer.Exit(2000)
    except Exception as e:
        log_exception_and_raise_exit(e, context, exit_code=3000)


@ensure_scheduler
async def verify(
    catalog_struct: Catalog,
    include_all_references: bool = False,
    service_url: Optional[str] = None,
    auth_token: Optional[str] = None,
    *,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> CollectedReferences:
    """
    Verify that all references in the files in the given catalog are referencing files inside the catalog.
    """
    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")

    if not g_httpx_available:
        raise FailedCommand("Verify is not available - missing dependencies!")

    if context.verbose:
        logger.info(f"Collecting assets in catalog with root {catalog_struct.root}...")

    url = f"{service_url}/wrapp/collect"

    headers = {
        "accept": "application/json",
        "Content-Type": "application/json",
    }

    # TODO: This is copied from the microservice and should be shared somehow?
    class RequestWrappCollect(pydantic.BaseModel):
        all_paths: List[str] = pydantic.Field(..., title="All paths in the package.")
        usd_only: bool = pydantic.Field(False, title="Collects usd files only or not. It will ignore all asset types.")
        material_only: bool = pydantic.Field(
            False, title="Collects material and textures only or not. It will ignore all " "other asset types."
        )
        auth_token: str = pydantic.Field(
            None, title="Auth token to log into the server - not needed if the service is already connected to the server."
        )
        recursive: bool = pydantic.Field(False, title="Collect the references recursively.")

    paths = list([e.source_path for e in catalog_struct.items])
    root = catalog_struct.root
    logger.info(f"Found {len(paths)} files, checking references...")
    req = RequestWrappCollect(
        all_paths=paths, usd_only=False, material_only=False, auth_token=auth_token if auth_token else "", recursive=include_all_references
    )

    try:
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, data=req.model_dump_json(), timeout=600)
            except httpx.ConnectError as e:
                raise MicroServiceOperationError(f"Post command failed to connect: {e}. Make sure the microservice is running.") from e
            if response.is_error:
                raise MicroServiceOperationError(
                    f"Post command failed: {response.content.decode('utf-8')}. Check the Kit log for more details."
                )
            refs = response.json()["refs"]

            logger.info(f"Checking {len(refs)} files for completeness...")
            r = CollectedReferences(root=root)
            async with scheduler.parent_task_node().create_task_group_node() as task_group:
                for referencing_path, refs_in_file in refs.items():
                    all_ref_paths: List[Tuple[str, str]] = []
                    for _, referenced_paths in refs_in_file.items():
                        all_ref_paths.extend(referenced_paths)
                    if all_ref_paths:
                        for ref_path in all_ref_paths:

                            async def add_item(referencing_path, ref_path):
                                item_exists = await wrapp_url_exists(ref_path[0])
                                r.add_item(referencing_path, ref_path[0], ref_path[1], item_exists)

                            task_group.create_task(add_item(referencing_path, ref_path), name="Verify reference")

    except MicroServiceOperationError as e:
        logger.error(f"Error while verifying catalog with root {root}: {e}")
        raise FailedCommand(f"Microservice error {e} while verifying.") from e

    if context.verbose:
        logger.info(
            f"Collecting assets in catalog with root {root} completed: package has {len(r.unresolved_items)} references "
            f"which cannot be resolved, {len(r.external_items)} references which are referencing files outside the root "
            f"directory of the catalog '{root}' and {len(r.resolved_items)} valid references."
        )

    return r


async def _save_references_as_catalog(
    context: CommandParameters,
    scheduler: SchedulerContext,
    output_catalog_path: str,
    root: str,
    refs: CollectedReferences,
    ignore_file: Optional[str],
    ignore_unresolved: bool = False,
    ignore_externals: bool = False,
):
    if not ignore_unresolved and len(refs.unresolved_items) > 1:
        raise FailedCommand(f"Cannot create a catalog from a collection of references with unresolved items: {refs.unresolved_items}")
    if not ignore_externals and len(refs.external_items) > 1:
        raise FailedCommand(f"Cannot create a catalog from a collection of references with external items: {refs.external_items}")
    all_rel_urls = set()
    for url in itertools.chain(refs.referencing_items.keys(), refs.resolved_items.keys()):
        rel_url = make_relative_url(root, url)
        if (
            not rel_url in refs.unresolved_items
            and not rel_url in refs.external_items
            and not url in refs.external_items
            and not url in refs.unresolved_items
        ):
            if not is_relative_path(rel_url):
                raise RuntimeError(f"Unexpectedly cannot create relative URL for path '{rel_url}' in root '{root}'.")
            all_rel_urls.add(rel_url)
    file_list = [(root, rel_url) for rel_url in all_rel_urls]
    logger.info(f"File list expanded to {len(file_list)} files, cataloging...")
    output_catalog = await catalog_func(
        root_dir=root, file_list=file_list, tags=False, ignores=ignore_file, context=context, scheduler=scheduler
    )
    await output_catalog.save(context, output_catalog_path)
    logger.info(f"Catalog written to {output_catalog_path}")
