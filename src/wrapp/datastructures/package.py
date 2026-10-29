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
import os.path
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

from pydantic import (
    BaseModel,
    field_validator,
)

from wrapp.datastructures.catalog import (
    Catalog,
)
from wrapp.datastructures.command_context import CommandParameters
from wrapp.datastructures.previous_versions.dependency_old_versions import Dependency_V2
from wrapp.datastructures.previous_versions.package_info_old_versions import (
    PackageInfo_V5_2,
)
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
)
from wrapp.repo.repository import Repository
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.coro import force_wrapp_thread
from wrapp.utils.storage import (
    is_url_pointing_to_sub_url,
    wrapp_list_directory,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    escape_legacy_storage_address,
    get_base_directory_from_url,
    get_filename_from_url,
    is_file_url,
    is_folder,
    is_relative_path,
    is_valid_file_or_directory_name,
    join_paths,
    join_url_path,
    make_file_url_absolute,
    normalize_path,
    normalize_url,
    paths_are_equal,
)

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")

PACKAGE_FILE_EXTENSION = ".wrapp"


@wrapp_public_api
class Dependency(WRAPPFileModel):
    """A Dependency records the relative installation path of a subpackage, its name and version and the repository from which it was installed."""

    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="3.0")
    package: str  #: Name of the referenced package
    version: str  #: Version of the referenced package
    relative_destination: str  #: Relative installation path
    repository: str  #:The repository URL from which the package was installed

    @classmethod
    def migrate_major_version(cls, read_data: Dependency_V2) -> "Dependency":
        # v3: introduction of WRAPPFileModel/WRAPPModelVersion
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        return Dependency(**data_dict)


@wrapp_public_api
class DependencySet(WRAPPFileModel):
    """A DependencySet represents a set of Dependencies, e.g. to create a metapackage easily."""

    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="1.0")
    dependencies: list[Dependency]


@wrapp_public_api
class PackageInfo(WRAPPFileModel):
    """The PackageInfo class encapsulates all information about a wrapp package. These are stored in .wrapp files

    To load and save wrapp files, use the load() and save() member functions of the PackageInfo class.
    """

    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="6.0")
    name: str = ""  #: Name of the package
    version: str = ""  #: "Version referenced
    catalog: Optional[Catalog] = None  #: Detailed content information
    source: Optional[str] = None  #: Optional URL of source of last create operation
    repository: Optional[str] = None  #: Optional repository of last create or install operation.
    dependencies: Optional[list[Dependency]] = None  #: Optional list of dependency entries

    @classmethod
    def migrate_major_version(cls, read_data: PackageInfo_V5_2) -> "PackageInfo":
        # v6: Catalog identities and observation metadata have a new contract.
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        for key in ("source", "repository"):
            if data_dict.get(key) is not None:
                data_dict[key] = escape_legacy_storage_address(data_dict[key])
        for dependency in data_dict.get("dependencies") or []:
            dependency["repository"] = escape_legacy_storage_address(dependency["repository"])
        return PackageInfo(**data_dict)

    @field_validator("name")
    @classmethod
    def name_must_be_suited_as_file_name(cls, package_name):
        if not is_valid_file_or_directory_name(package_name):
            raise ValueError(
                f"{package_name} is not a valid package name. Characters that are not allowed in directory names and spaces cannot be used."
            )
        return package_name

    @field_validator("version")
    @classmethod
    def version_must_be_suited_as_file_name(cls, version):
        if not is_valid_file_or_directory_name(version):
            raise ValueError(
                f"{version} is not a valid version string. Characters that are not allowed in directory names and spaces cannot be used"
            )
        return version


@wrapp_public_api
@dataclass(eq=True, frozen=True)
class UniquePackageLocation:
    """
    Unique identifier for the location of a package based on name, version, and repository.
    """

    package_name: str
    version: str
    repo_url: str


def wrapp_file_name(package_name: str) -> str:
    """Given a package name, returns the standard filename to use (.<packagename>.wrapp)"""
    return f".{package_name}.wrapp"


def is_package_file_name(filename: str) -> bool:
    """Return whether a file name/path uses a supported package metadata extension."""
    return os.path.splitext(get_filename_from_url(filename))[1].lower() == PACKAGE_FILE_EXTENSION


def wrapp_file_path(base_url: str, package_name: str):
    """Given a path and a package name, returns the standard wrapp file URL"""
    return join_paths(base_url, wrapp_file_name(package_name))


def wrapp_file_in_repo(repo: Repository, package_name: str, version: str):
    """Given a Repostiory, a package name and version, returns the expeced wrapp file URL."""
    return join_paths(repo.package_url(package_name, version), wrapp_file_name(package_name))


def _base_directory_from_url(url: str):
    if is_relative_path(url):
        return str(os.path.dirname(url))
    else:
        return get_base_directory_from_url(url)


def repository_from_wrapp_file_path(wrapp_file: str) -> Optional[str]:
    package_file = get_filename_from_url(wrapp_file)
    package_base, ext = os.path.splitext(package_file)
    if ext.lower() != PACKAGE_FILE_EXTENSION:
        return None
    wrapp_version_directory = _base_directory_from_url(wrapp_file)
    if wrapp_version_directory != wrapp_file:
        wrapp_package_directory = _base_directory_from_url(wrapp_version_directory)
        if wrapp_package_directory != wrapp_version_directory and f".{get_filename_from_url(wrapp_package_directory)}" == package_base:
            wrapp_repo_directory = _base_directory_from_url(wrapp_package_directory)
            if wrapp_repo_directory != wrapp_package_directory:
                if wrapp_repo_directory.endswith(".packages"):
                    wrapp_repo_root = _base_directory_from_url(wrapp_repo_directory)
                    if wrapp_repo_root != wrapp_repo_directory:
                        return wrapp_repo_root
    return None


def add_dependency_to_package(
    package: PackageInfo, package_name: str, version: str, repo: Repository, relative_destination: str
) -> Dependency:
    """Given package name, version, Repository, and relative destination path, adds a new dependency to the referenced sub-package. Returns the new Dependency"""
    package_entry = Dependency(
        package=package_name, version=version, repository=normalize_path(repo.base_url()), relative_destination=relative_destination
    )
    if package.dependencies is not None:
        # TODO TBD if we want to be able to install a package into multiple locations. Then the primary key would be (package, destination)?
        package.dependencies = list(
            filter(lambda x: x.package != package_name or x.relative_destination != relative_destination, package.dependencies)
        )
        package.dependencies.append(package_entry)
    else:
        package.dependencies = [package_entry]
    return package_entry


def _ensure_paths_in_filesystem_repo_are_absolute(package: PackageInfo, wrapp_file_path: str):
    # If the repository is a repository, make sure that it stores absolute source_paths and root after loading.
    # source_path and root are expected to be absolute path by e.g. the Diff and Patch operations,
    # but we want to be able to transport full repos through e.g. gitlab or by just moving them on disk, requiring
    # to change the path stored in the package file.
    # Unfortunately with earlier WRAPP versions it was possible to create local repositories with
    # relative paths for the repository path and the catalog root and the source paths stored in the package file
    # if the path for the "--repo" parameter was a relative path.
    if not is_file_url(wrapp_file_path):
        return
    if not package.catalog:
        return
    abs_wrapp_file_path = wrapp_file_path
    if is_relative_path(abs_wrapp_file_path):
        abs_wrapp_file_path = make_file_url_absolute(wrapp_file_path)
        logger.info(
            f"Relative wrapp file path {wrapp_file_path} was given to ensure_paths_in_filesystem_repo_are_absolute. Using {abs_wrapp_file_path} to continue."
        )
    wrapp_file_dir = get_base_directory_from_url(abs_wrapp_file_path).rstrip("/")
    if package.repository and is_relative_path(package.repository):
        # This is dodgy. Should only happen for file system URLs created with WRAPP 2.0.0-pre1 or earlier where a
        # relative path for the repo parameter on the command line would make relative paths for the stored
        # repository path, as well as the catalog root and the source paths.
        # Attempt to make the repository path absolute for packages read from a file system repository
        # which was created with a relative "--repo" parameter.
        if (
            f"{package.repository}/.packages".lstrip(".") in wrapp_file_dir
        ):  # Check if the loaded package is loaded from the repo, not from a locally installed version
            repo_dir = get_base_directory_from_url(get_base_directory_from_url(get_base_directory_from_url(wrapp_file_dir))).rstrip("/")
            package.repository = repo_dir
    if package.catalog.root and is_relative_path(package.catalog.root):
        rel_root = package.catalog.root
        logger.info(f"Using the base directory {wrapp_file_dir} as a root directory for the package {abs_wrapp_file_path}.")
        package.catalog.root = wrapp_file_dir
        abs_root = package.catalog.root
        for item in package.catalog.items:
            if item.source_path.startswith(rel_root):
                item.source_path = abs_root + item.source_path[len(rel_root) :]
            if is_relative_path(item.source_path):
                raise FailedCommand(
                    f"Failed to load package {wrapp_file_path}. Failed to fix relative source path {item.source_path} for item {item.relative_path}!"
                )


def normalize_install_path_from_user_input(install_path: str) -> str:
    if not isinstance(install_path, str):
        raise FailedCommand(f"The install path '{install_path}' is not a string.")
    # Empty string is allowed as an install path
    if not install_path:
        return ""
    if not is_relative_path(install_path):
        raise FailedCommand(f"The install path '{install_path}' is not a relative path.")
    install_path = normalize_path(install_path)
    components = install_path.split("/")
    for component in components:
        if component == "":
            raise FailedCommand(f"The install path '{install_path}' contains a double slash.")
        if component == "..":
            raise FailedCommand(f"The install path '{install_path}' contains a '..'.")
        if not is_valid_file_or_directory_name(component):
            raise FailedCommand(f"The component '{component}' in the install path '{install_path}' is not a valid directory name.")
    return install_path


def validate_package_and_version(package_name: str, version: str) -> None:
    if not is_valid_file_or_directory_name(package_name):
        raise FailedCommand(
            f"{package_name} is not a valid package name. Characters that are not allowed in directory names and spaces cannot be used."
        )
    if not is_valid_file_or_directory_name(version):
        raise FailedCommand(
            f"{version} is not a valid version string. Characters that are not allowed in directory names and spaces cannot be used."
        )


def apply_source_policy(
    package: PackageInfo,
    *,
    strip_source: bool = False,
    canonical_source: Optional[str] = None,
    include_repository: bool = True,
) -> None:
    """Rewrite the top-level ``source``/``repository`` provenance of a package just before persisting.

    Used by the publish flows (create/mirror/export/import/publish-redirect) to keep internal
    source addresses out of published ``.wrapp`` files. The two policies are mutually exclusive
    and both default to off, so an unconfigured call is a no-op.

    :param package: PackageInfo to mutate in place.
    :param strip_source: When True, clear ``source`` (and ``repository`` unless ``include_repository``
        is False). Clearing ``repository`` is safe because it is re-derived from the file's own path
        on load (see ``repository_from_wrapp_file_path``).
    :param canonical_source: When set, record this normalized URL as ``source`` (and ``repository``
        unless ``include_repository`` is False) instead of the internal address.
    :param include_repository: Set to False to leave ``repository`` untouched (used by publish-redirect,
        where ``repository`` points at the redirect repo and is load-bearing).
    :raises FailedCommand: When both ``strip_source`` and ``canonical_source`` are provided.
    """
    if strip_source and canonical_source:
        raise FailedCommand("Use either --strip-source or --canonical-source, not both.")
    if canonical_source:
        value = normalize_url(canonical_source)
        package.source = value
        if include_repository:
            package.repository = value
    elif strip_source:
        package.source = None
        if include_repository:
            package.repository = None


@force_wrapp_thread
@wrapp_public_api
async def write_package_info(
    package_url: str,
    package: PackageInfo,
    context: Optional[CommandParameters] = None,
    *,
    create_package_standard_license: Optional[str] = None,
    package_standard_content_hashes: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    """Given a destination URL including the file name and a package info, write the given information in the current JSON format.

    :param package_url: Destination path for the wrapp file to be created, including the file name
    :param package: PackageInfo object to be stored
    :param context: optional CommandParameters() object to control the storage operation
    :param create_package_standard_license: SPDX license identifier; when set, package-standard sidecars are generated
        for the written package (used by ``create`` only)
    :param package_standard_content_hashes: precomputed ``{relative_path: {hash_name: value}}`` map (from cataloging) used
        to build the package-standard BOM without re-reading the payload
    :returns: The filename written to

    On failure, this will raise a StorageOperationError.
    """
    return await _write_package_info(
        package_url,
        package,
        context,
        create_package_standard_license=create_package_standard_license,
        package_standard_content_hashes=package_standard_content_hashes,
    )


@force_wrapp_thread
async def _write_package_info(
    package_url: str,
    package: PackageInfo,
    context: Optional[CommandParameters] = None,
    *,
    package_standard_seed_url: Optional[str] = None,
    create_package_standard_license: Optional[str] = None,
    package_standard_content_hashes: Optional[Dict[str, Dict[str, str]]] = None,
) -> str:
    resolved_context = context if context else CommandParameters()
    await package.save(context=resolved_context, destination=package_url)
    if not resolved_context.dry_run:
        from wrapp.datastructures.package_standard import (
            copy_package_standard_sidecars,
            write_package_standard_sidecars,
        )

        if package_standard_seed_url is not None:
            # Non-create paths (install/mirror) carry existing sidecars verbatim, never regenerate.
            await copy_package_standard_sidecars(package_standard_seed_url, package_url, resolved_context)
        elif create_package_standard_license is not None:
            await write_package_standard_sidecars(
                package_url, package, resolved_context, create_package_standard_license, package_standard_content_hashes
            )
    return package_url


@force_wrapp_thread
@wrapp_public_api
async def read_package_info(package_url: str) -> Optional[PackageInfo]:
    """
    Given a source url of a wrapp file, load and parse the content returning a PackageInfo object.

    :param package_url: URL of the wrapp file
    :return: The PackageInfo retrieved and parsed. None if the package file does not exist.

    On failure, raises FailedCommand or StorageOperationError exception.
    """
    return await load_package_setup(package_url)


def _detect_common_catalog_root(catalog: Catalog) -> Optional[str]:
    """
    Detect a common root directory from all source_paths in the catalog items.
    Empty folder placeholder files are ignored.

    Returns the common base URL/path if one exists, None otherwise.
    """
    source_paths: List[str] = []
    for item in catalog.items:
        if item.source_path.endswith(Catalog.filename_to_keep_empty_folder()):
            # Skip empty folder placeholder files
            continue
        source_paths.append(item.source_path)

    if not source_paths:
        return None

    # Start with the directory of the first source path as the candidate root
    candidate_root = get_base_directory_from_url(source_paths[0]).rstrip("/")

    # Keep going up the directory tree until we find a common root or run out of directories
    while candidate_root:
        candidate_prefix = candidate_root + "/"
        all_match = all(source_path.startswith(candidate_prefix) for source_path in source_paths)

        if all_match:
            return candidate_root

        # Go up one level
        parent = get_base_directory_from_url(candidate_root).rstrip("/")
        if parent == candidate_root:
            # We've reached the root, can't go higher
            break
        candidate_root = parent

    return None


# TODO - this needs to go away and everybody needs to call read_package_info instead, which is now the public API
async def load_package_setup(package: Optional[str]) -> Optional[PackageInfo]:
    if package and await wrapp_url_exists(package):
        package_info = await PackageInfo.load(package)
        # The source paths in the catalogs must be absolute paths. In previous versions of WRAPP it was
        # possible to create packages in file system repositories with relative paths by specifying a relative
        # path for the repository on the command line. Patch/Diff rely on those paths being absolute paths,
        # so fix that.
        _ensure_paths_in_filesystem_repo_are_absolute(package_info, package)
        # There is a related, but distinct problem what should happen if a package (or an entire repository)
        # was relocated. Those packages still get all the content from the original source. But at this
        # point it is not obvious if the loaded package is from a relocated repository or an installed package,
        # or a package from a source folder. So we are using a boolean parameter to add this extra check, when reading
        # a package from its repository only.
        if is_relative_path(package):
            abs_wrapp_file_path = make_file_url_absolute(package)
        else:
            abs_wrapp_file_path = package
        extracted_repo = repository_from_wrapp_file_path(abs_wrapp_file_path)
        if extracted_repo is not None:
            # We need to check that the package file is referencing local data, i.e. the source paths are within
            # the directory the wrapp file is stored. Else, this is a temporary, non-repository package, or a package
            # that was copied from a repository, and these should not be used.
            wrapp_file_dir = get_base_directory_from_url(abs_wrapp_file_path).rstrip("/")

            # For redirect packages (with storage_map), check that the repository field matches the location.
            # Redirect packages have data at a different location (via storage_map), so we check the source_path
            # against the catalog root, and we verify the .wrapp file is in the repository it claims to be from.
            if package_info.catalog:
                if package_info.catalog.storage_map:
                    if package_info.repository and not await is_url_pointing_to_sub_url(
                        package_info.repository.rstrip("/"), wrapp_file_dir
                    ):
                        logging.warning(
                            f"Detected package problem: redirect package at {abs_wrapp_file_path} claims to be from repository {package_info.repository}, "
                            f"but is located at {wrapp_file_dir}. This package may have been copied from another location."
                        )
                    expected_base_url_for_items = package_info.catalog.root
                else:
                    expected_base_url_for_items = wrapp_file_dir

                # For normal packages (without storage_map), check that source_path is within wrapp_file_dir
                # AND that catalog.root matches where the source_paths actually are.
                # However, for redirect packages (where source_paths point to external URLs),
                # if catalog.root already matches the source_paths, that's valid and not broken.
                catalog_root_broken = False
                for item in package_info.catalog.items:
                    if item.source_path.endswith(Catalog.filename_to_keep_empty_folder()):
                        # Skip empty folder placeholder files, they have a weird absolute path pointing to a local drive tmp
                        continue
                    source_path_matches_wrapp_dir = await is_url_pointing_to_sub_url(expected_base_url_for_items, item.source_path)
                    source_path_matches_catalog_root = await is_url_pointing_to_sub_url(package_info.catalog.root, item.source_path)

                    if not source_path_matches_wrapp_dir and not source_path_matches_catalog_root:
                        # source_path doesn't match wrapp_file_dir AND doesn't match catalog.root
                        # This is a genuinely broken package
                        logging.warning(
                            f"Detected package problem: while reading package from {abs_wrapp_file_path}, "
                            f"source_path '{item.source_path}' is not under wrapp_file_dir '{expected_base_url_for_items}' "
                            f"and not under catalog.root '{package_info.catalog.root}'."
                        )
                        catalog_root_broken = True
                        break
                    elif not source_path_matches_wrapp_dir and source_path_matches_catalog_root:
                        # source_path matches catalog.root but not wrapp_file_dir
                        # This could be a valid redirect package OR a broken copied package.
                        # Either way, warn the user that data will be pulled from a different location.
                        # Don't set catalog_root_broken since catalog.root correctly matches source_paths.
                        logging.warning(
                            f"Detected package problem: while reading package from {abs_wrapp_file_path} at {wrapp_file_dir}, "
                            f"this package will pull data from {item.source_path} which is not in {expected_base_url_for_items}. "
                            f"This may be a redirect package or a package that was copied from another location."
                        )
                        break  # Only warn once
                    elif source_path_matches_wrapp_dir and not source_path_matches_catalog_root:
                        # source_path matches wrapp_file_dir but catalog.root is wrong
                        # This needs fixing
                        logging.warning(
                            f"Detected package problem: catalog.root '{package_info.catalog.root}' does not match "
                            f"source_path '{item.source_path}'. The catalog.root appears to be incorrect."
                        )
                        catalog_root_broken = True
                        break
                    # else: both match, which is fine

                if catalog_root_broken:
                    # Need to detect a common catalog_root from all items in package_info.catalog.items, and only fail if no top level URL
                    # is shared (empty folder placeholders need to be ignored)
                    detected_root = _detect_common_catalog_root(package_info.catalog)
                    if detected_root:
                        logging.info(
                            f"Package '{abs_wrapp_file_path}' has a broken catalog root '{package_info.catalog.root}'. "
                            f"Detected common root '{detected_root}' from source paths. Updating catalog root."
                        )
                        package_info.catalog.root = detected_root
                    else:
                        # TODO - re-enable this as a FailedCommand once we know the data is fixed and this warning is
                        # no longer seen in the real life pipeline.
                        logging.warning(
                            f"Package '{abs_wrapp_file_path}' has a broken catalog root '{package_info.catalog.root}'"
                            " and no common root could be detected from source paths. The package data may be inconsistent."
                        )
        return package_info
    else:
        return None


def find_dependency(package_info: PackageInfo, package_name: str, relative_location: str) -> Optional[Dependency]:
    if package_info.dependencies is not None:
        for dep in package_info.dependencies:
            if dep.package == package_name and paths_are_equal(dep.relative_destination, relative_location):
                return dep
    return None


class VersionConflict(BaseModel):
    node: str
    versions: Set[str]


async def collect_wrapp_files_in_directory(directory: str) -> Tuple[List[str], List[str]]:
    """Given a directory URL, return a list of all wrapp files inside that directory, and the list of all sub-folders for recursion."""
    try:
        wrapp_files: List[str] = []
        to_inspect: List[str] = []
        async for item in wrapp_list_directory(directory):
            if is_folder(item):
                to_inspect.append(join_url_path(directory, item.relative_path))
            else:
                file_extension = os.path.splitext(item.relative_path)
                if len(file_extension) > 1:
                    if file_extension[1].lower() == PACKAGE_FILE_EXTENSION:
                        wrapp_files.append(join_url_path(directory, item.relative_path))
        return wrapp_files, to_inspect
    except StorageOperationError as e:
        raise StorageOperationError(f"Failed to list folder {directory} with exception {e}", e.result)
