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

import json
import logging
import os
import shutil
import tarfile
from copy import deepcopy
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
)

import typer
from pydantic import (
    ValidationError,
)

from wrapp.app import (
    app,
    measure_time,
    print_stats,
)
from wrapp.commands.case_insensitive_paths import (
    raise_if_case_insensitive_path_conflicts,
)
from wrapp.commands.catalog import catalog
from wrapp.datastructures.authentication import capture_auth
from wrapp.datastructures.catalog import (
    Catalog,
    CatalogItem,
)
from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
    WrappSchedulerCLI,
    context_from_params,
    log_exception_and_raise_exit,
)
from wrapp.datastructures.package import (
    PackageInfo,
    apply_source_policy,
    is_package_file_name,
    write_package_info,
)
from wrapp.datastructures.package_standard import (
    PACKAGE_STANDARD_DEFINITION_FILENAME,
    PACKAGE_STANDARD_METADATA_FOLDER,
    guard_package_standard_metadata_relative_path,
    is_package_standard_sidecar_path,
    package_standard_metadata_folder_url,
    package_standard_sidecar_paths,
)
from wrapp.options import (
    AuthOption,
    CanonicalSourceOption,
    DebugLevelFlag,
    FileTransferJobsNumber,
    InteractiveAuthFlag,
    JsonLoggingFlag,
    LogFileOption,
    OverwriteFlag,
    ProgressReportOption,
    RepoRequiredOption,
    StatsFlag,
    StripSourceFlag,
    TimeFlag,
    VerboseFlag,
)
from wrapp.repo.repository import Repository
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import ProgressReportVerbosity
from wrapp.utils.coro import (
    async_typer,
    force_wrapp_thread,
)
from wrapp.utils.ensure_scheduler import ensure_scheduler
from wrapp.utils.storage import (
    Result,
    wrapp_copy_address_optimized,
    wrapp_require_writable,
    wrapp_url_exists,
    wrapp_write_file_bytes,
)
from wrapp.utils.storage_interface import ResourceAddress
from wrapp.utils.temp_file_manager import TempFileManager
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    join_url_path,
    validate_non_empty_string,
)
from wrapp.utils.wrapp_init_for_cmd import wrapp_init_for_cmd

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


@app.command(name="import")
@measure_time
@print_stats
@wrapp_init_for_cmd
@capture_auth
@async_typer
async def import_command(
    package_file: str = typer.Argument(..., help="Specify the name of the tar package to import"),
    repo: str = RepoRequiredOption,
    overwrite: bool = OverwriteFlag,
    strip_source: bool = StripSourceFlag,
    canonical_source: Optional[str] = CanonicalSourceOption,
    auth: Optional[List[str]] = AuthOption,
    interactive_auth: bool = InteractiveAuthFlag,
    file_transfer_jobs: int = FileTransferJobsNumber,
    verbose: bool = VerboseFlag,
    progress_report: ProgressReportVerbosity = ProgressReportOption,
    log_file: str = LogFileOption,
    debug: bool = DebugLevelFlag,
    json_logging: bool = JsonLoggingFlag,
    stats: bool = StatsFlag,
    time: bool = TimeFlag,
) -> None:
    """
    Import a tar file from a previous export into the specified repository
    """
    context, scheduler_params = context_from_params()
    try:
        async with WrappSchedulerCLI(params=scheduler_params) as scheduler:
            logger.info(f"Connecting to repository {repo}...")
            await import_package(
                package_file,
                repo,
                overwrite=overwrite,
                strip_source=strip_source,
                canonical_source=canonical_source,
                context=context,
                scheduler=scheduler,
            )

    except FailedCommand as e:
        logger.error(f"Failure to import '{package_file}' to '{repo}': {e}")
        raise typer.Exit(1)
    except StorageOperationError as e:
        logger.error(f"Error while importing '{package_file}' to '{repo}', operation incomplete: {e}")
        raise typer.Exit(2)
    except Exception as e:
        log_exception_and_raise_exit(e, context)


def read_package_info_from_package(tar: tarfile.TarFile) -> Tuple[PackageInfo, tarfile.TarInfo]:
    wrapp_files = [member for member in tar.getmembers() if is_package_file_name(member.name)]
    if not wrapp_files:
        raise FailedCommand("Package archive does not contain any package metadata file ending with '.wrapp', can't import")
    if len(wrapp_files) > 1:
        raise FailedCommand(
            f"Package archive does contain more than one package metadata file ending with '.wrapp', can't import: {wrapp_files}"
        )
    package_catalog = wrapp_files[0]
    try:
        catalog_content = tar.extractfile(package_catalog.path)
    except KeyError:
        raise FailedCommand(f"Member '{package_catalog.path}' not found in tarfile")
    if catalog_content is None:
        raise FailedCommand(f"Member '{package_catalog.name}' is not a regular file (directory or special member)")

    content = catalog_content.read().decode("utf-8")
    if content == "":
        package_setup = PackageInfo()
    else:
        try:
            package_dict = json.loads(content)
        except json.JSONDecodeError as j:
            raise FailedCommand(f"Could not parse package file either as json:\nJSON error: {j}\nContent:{content}")
        try:
            package_setup = PackageInfo(**package_dict)
        except ValidationError as e:
            raise FailedCommand(f"Failed to load package info, validation error: {e.errors()}")

    return package_setup, package_catalog


def _read_archived_member(tar: tarfile.TarFile, name: str) -> Optional[bytes]:
    members = [member for member in tar.getmembers() if member.name == name]
    if not members:
        return None
    content = tar.extractfile(members[0].path)
    if content is None:
        return None
    return content.read()


def read_package_standard_definition_from_package(tar: tarfile.TarFile) -> Optional[bytes]:
    return _read_archived_member(tar, PACKAGE_STANDARD_DEFINITION_FILENAME)


def read_package_standard_metadata_from_package(tar: tarfile.TarFile) -> Dict[str, bytes]:
    """Read every archived ``.metadata`` sidecar, keyed by path relative to the ``.metadata`` folder."""
    prefix = f"{PACKAGE_STANDARD_METADATA_FOLDER}/"
    contents: Dict[str, bytes] = {}
    for member in tar.getmembers():
        if not member.isfile() or not member.name.startswith(prefix):
            continue
        # Tar member names are archive/attacker-controlled, so reject any ".." traversal or absolute
        # escape before the suffix is used to build a write URL (join_paths does not collapse "..").
        try:
            rel_path = guard_package_standard_metadata_relative_path(member.name[len(prefix) :])
        except ValueError as e:
            raise FailedCommand(str(e)) from e
        extracted = tar.extractfile(member.path)
        if extracted is None:
            continue
        contents[rel_path] = extracted.read()
    return contents


async def _write_imported_sidecar(context: CommandParameters, url: str, content: bytes, *, overwrite: bool) -> None:
    if not overwrite and await wrapp_url_exists(url):
        return
    result = await wrapp_write_file_bytes(context, url, content)
    if result != Result.OK:
        raise StorageOperationError(f"Can't write package-standard sidecar to {url}: {result}")


@force_wrapp_thread
@ensure_scheduler
@wrapp_public_api
@log_args
async def import_package(
    package_file: str,
    repository: str,
    overwrite: bool = False,
    *,
    strip_source: bool = False,
    canonical_source: Optional[str] = None,
    context: CommandParameters = CommandParameters(),
    scheduler: Optional[SchedulerContext] = None,
) -> None:
    """
    Given a previously exported tar file and a repository location, import the package and make it available for install commands using that repository

    :param package_file: Name of tar file to import. Note the naming convention is that it is either <package_name>.tar or <package_name>.<version>.tar
    :param repository: URL of the repository to write the package to (excluding the .packages directory)
    :param overwrite: Set this to allow overwriting an existing package. Note this might leave extra files in the repository.
    :param strip_source: When True, clear the source and repository provenance fields in the imported .wrapp file so internal addresses are not published. Mutually exclusive with canonical_source.
    :param canonical_source: When set, record this canonical base URL as the source/repository provenance instead of the internal address. Mutually exclusive with strip_source.
    :param context: Global configuration parameters
    :param scheduler: Optionally pre-constructed SchedulerContext. When calling many functions in a row make sure to pre-construct the scheduler.

    :raises FailedCommand: When prerequisites not matched
    :raises StorageOperationError: Raised when network or file operations fail
    """
    validate_non_empty_string("repository", repository)
    wrapp_require_writable(repository)

    if scheduler is None:
        raise RuntimeError("No scheduler present and ensure_scheduler malfunctioned, program error!")
    if strip_source and canonical_source:
        raise FailedCommand("Use either --strip-source or --canonical-source, not both.")
    logger.info(f"Opening tar {package_file}...")
    # Task structure:
    #  Uploading files
    #  Create catalog
    scheduler = scheduler.create_sub_context_and_node(f"Import {package_file}", expected_num_sub_jobs=2)
    with tarfile.open(package_file, "r") as tar:
        package_setup, package_catalog_in_tar = read_package_info_from_package(tar)
        package_standard_definition_seed = read_package_standard_definition_from_package(tar)
        package_standard_metadata = read_package_standard_metadata_from_package(tar)

        repo_object = Repository(repo_url=repository)
        target_url = repo_object.package_url(package_setup.name, package_setup.version)
        if not overwrite and await wrapp_url_exists(target_url):
            raise FailedCommand(f"target package {target_url} already exists and --overwrite is not specified")

        if package_setup.catalog is None:
            raise FailedCommand("package catalog is empty, nothing to import!")
        else:
            stripped_package = deepcopy(package_setup)
            if stripped_package.catalog is None:
                raise RuntimeError("Catalog is None, should be copy of non-none catalog")
            stripped_package.catalog.items = [
                item for item in stripped_package.catalog.items if not is_package_standard_sidecar_path(item.relative_path)
            ]
            raise_if_case_insensitive_path_conflicts(stripped_package.catalog, target_url)

            # Now loop the catalog content.
            # Concurrency is bounded by TempFileManager's capacity limits
            # (max_files / max_bytes), which replace the previous manual semaphore
            # and TemporaryDirectory.
            async with scheduler.parent_task_node().create_task_group_node(
                f"Uploading {len(stripped_package.catalog.items)} files"
            ) as task_group:
                for item in stripped_package.catalog.items:
                    task_group.create_task(
                        upload_task(
                            context,
                            scheduler.get_parameters().progress_report,
                            tar,
                            item,
                            target_url,
                            package_file,
                            stripped_package.catalog,
                        ),
                        name="Upload",
                    )

        # Lastly, the wrapp file itself. As we import, we strip the additional storage_path info from the catalog before uploading
        # and update the catalog to have the source_path point to the new location
        imported_files_catalog = await catalog(
            root_dir=target_url,
            tags=False,
            ignores=None,
            context=context,
            scheduler=scheduler.create_sub_context_and_node(f"Create catalog for '{package_setup.name}' '{package_setup.version}'"),
        )
        stripped_package.catalog.storage_map = None
        not_updated = stripped_package.catalog._update_source_paths(imported_files_catalog)
        if len(not_updated) != 1:
            for item in not_updated:
                logger.warning(f"Could not update source_path of item '{item.relative_path}', missing in import destination")
        upload_path = join_url_path(target_url, package_catalog_in_tar.path)
        # Carry whatever package-standard sidecars the archive contains, verbatim. The import trusts the
        # archive's .wrapp catalog and content hashes without re-validation, so the sidecars are trusted the
        # same way: no license, dependency, or schema gating, and no regeneration or re-hashing.
        sidecar_paths = package_standard_sidecar_paths(upload_path)
        if package_standard_definition_seed is not None:
            await _write_imported_sidecar(context, sidecar_paths.definition_url, package_standard_definition_seed, overwrite=overwrite)
        target_metadata_dir = package_standard_metadata_folder_url(upload_path)
        for rel_path, metadata_bytes in package_standard_metadata.items():
            await _write_imported_sidecar(context, join_url_path(target_metadata_dir, rel_path), metadata_bytes, overwrite=overwrite)
        # Keep internal source addresses out of the imported (published) package if requested
        apply_source_policy(stripped_package, strip_source=strip_source, canonical_source=canonical_source)
        await write_package_info(upload_path, stripped_package, context)


async def upload_task(
    context: CommandParameters,
    progress_verbosity: ProgressReportVerbosity,
    tar: tarfile.TarFile,
    item: CatalogItem,
    target_url: str,
    package_file: str,
    catalog: Catalog,
):
    upload_path = join_url_path(target_url, item.relative_path)
    if progress_verbosity == ProgressReportVerbosity.off:
        logger.info(f"Uploading {upload_path}")
    storage_path = catalog.get_storage_path_for_item(item)
    try:
        file_obj = tar.extractfile(storage_path)
    except KeyError:
        file_obj = None
    if file_obj is None:
        raise StorageOperationError(f"Failed to extract '{storage_path}' from tar archive '{package_file}'")
    # TODO: tar.extractfile() already provides a stream — once the storage backends
    # support a stream-based upload API (upload_stream), we can pass file_obj directly
    # instead of materializing it to a temp file first.
    async with TempFileManager.me().create_temp_file() as tmp:
        with open(tmp.path, "wb") as tmp_file:
            shutil.copyfileobj(file_obj, tmp_file)
        tmp.write_completed()
        result = await wrapp_copy_address_optimized(
            context,
            ResourceAddress(tmp.url),
            ResourceAddress(upload_path),
            item.hash,
            overwrite=True,
            force_upload=False,
        )
        if result != Result.OK:
            raise StorageOperationError(f"Failed to upload file to {upload_path}: {result}")
