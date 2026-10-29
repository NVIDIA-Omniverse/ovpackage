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

import hashlib
import io
import json
import logging
from dataclasses import dataclass
from typing import (
    Dict,
    Optional,
    Tuple,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
)

from wrapp.datastructures.catalog import (
    CatalogItem,
)
from wrapp.datastructures.command_context import CommandParameters
from wrapp.datastructures.package import (
    PACKAGE_FILE_EXTENSION,
    PackageInfo,
)
from wrapp.datastructures.previous_versions import (  # noqa: F401
    package_standard_old_versions as _package_standard_old_versions,
)
from wrapp.datastructures.wrapp_file_model import (
    PkgStdFileModel,
    PkgStdModelVersion,
)
from wrapp.utils.nucleus_hash import (  # noqa: F401  (re-exported for callers/tests)
    PACKAGE_STANDARD_FIRST1M_BYTES,
    calc_package_standard_hashes_from_stream,
)
from wrapp.utils.storage import (
    Result,
    wrapp_list_directory,
    wrapp_list_directory_supported,
    wrapp_list_file_tree,
    wrapp_list_file_tree_supported,
    wrapp_read_file_content,
    wrapp_read_text_file,
    wrapp_url_exists,
    wrapp_write_file_bytes,
    wrapp_write_file_content,
)
from wrapp.utils.task_throttler import CPUThrottler
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    get_base_directory_from_url,
    is_folder,
    is_regular_file,
    join_paths,
    join_url_path,
)

logger = logging.getLogger("omni.wrapp")

PACKAGE_STANDARD_DEFINITION_FILENAME = "com.nvidia.simready.packaging.json"
PACKAGE_STANDARD_METADATA_FOLDER = ".metadata"
PACKAGE_STANDARD_BOM_FILENAME = "com.nvidia.simready.packaging.bom.json"
PACKAGE_STANDARD_BOM_RELATIVE_PATH = f"{PACKAGE_STANDARD_METADATA_FOLDER}/{PACKAGE_STANDARD_BOM_FILENAME}"
PACKAGE_STANDARD_FORMAT_VERSION = PkgStdModelVersion("1.0")
PACKAGE_STANDARD_DEPENDENCIES_UNSUPPORTED_MESSAGE = (
    "Skipping package-standard sidecars for package with dependencies because the released SimReady packaging standard "
    "does not support dependencies."
)
PACKAGE_STANDARD_BLANK_LICENSE_MESSAGE = "Package-standard license is blank; skipping sidecar generation."


class PackageStandardDefinitionSeed(BaseModel):
    package_id: Optional[str] = None
    license: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class PackageStandardMetadataEntry(BaseModel):
    name: str
    hash: Dict[str, str]


class PackageStandardDefinition(PkgStdFileModel):
    format_version: PkgStdModelVersion = PACKAGE_STANDARD_FORMAT_VERSION
    package_id: str
    license: str
    metadata: list[PackageStandardMetadataEntry]
    content_hash: Dict[str, str]
    package_hash: Dict[str, str]


class PackageStandardBomItem(BaseModel):
    relative_path: str
    size: int
    hash: Optional[Dict[str, str]] = None


class PackageStandardBom(PkgStdFileModel):
    format_version: PkgStdModelVersion = PACKAGE_STANDARD_FORMAT_VERSION
    items: list[PackageStandardBomItem]


@dataclass(frozen=True)
class PackageStandardSidecarPaths:
    definition_url: str
    bom_url: str


@dataclass(frozen=True)
class PackageStandardSidecars:
    definition: PackageStandardDefinition
    bom: PackageStandardBom

    @property
    def definition_json(self) -> str:
        return package_standard_model_json(self.definition)

    @property
    def bom_json(self) -> str:
        return package_standard_model_json(self.bom)


def package_standard_model_json(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json", exclude_none=True), indent=2)


def package_standard_sidecar_paths(package_url: str) -> PackageStandardSidecarPaths:
    package_dir = get_base_directory_from_url(package_url)
    return PackageStandardSidecarPaths(
        definition_url=join_paths(package_dir, PACKAGE_STANDARD_DEFINITION_FILENAME),
        bom_url=join_paths(package_dir, PACKAGE_STANDARD_BOM_RELATIVE_PATH),
    )


def package_standard_metadata_folder_url(package_url: str) -> str:
    """Return the URL of a package's ``.metadata`` sidecar folder."""
    return join_paths(get_base_directory_from_url(package_url), PACKAGE_STANDARD_METADATA_FOLDER)


def guard_package_standard_metadata_relative_path(relative_path: str) -> str:
    """Normalize a ``.metadata`` relative path and reject any ``..``/absolute escape.

    Returns the POSIX-normalized path (``\\`` -> ``/``, leading separators stripped) when it stays
    *inside* the ``.metadata`` folder. Raises ``ValueError`` naming the offending path when it is
    empty, ``.``/``..``, contains any ``..`` segment (``../x``, ``x/../y``, ``x/..``), contains a
    NUL byte, or is absolute on Windows (drive-prefixed or UNC/double-slash), because ``join_paths``
    does not collapse ``..`` and platform absolute paths can let a write escape the folder.

    Both write sites route their attacker-/listing-derived relative paths through this guard before
    building a write URL: import restore keys tar members verbatim, and the copy/list walk surfaces
    arbitrary object-store keys under the ``.metadata`` prefix. Callers translate the ``ValueError``
    into their own domain error (``FailedCommand`` on import, ``StorageOperationError`` on copy/list).
    """
    normalized = relative_path.replace("\\", "/")
    stripped = normalized.lstrip("/")
    has_windows_drive = len(stripped) >= 2 and stripped[0].isalpha() and stripped[1] == ":"
    if "\0" in relative_path or normalized.startswith("//") or has_windows_drive:
        raise ValueError(f"Unsafe package-standard .metadata relative path (escapes the folder): {relative_path!r}")
    if stripped in ("", ".", "..") or ".." in stripped.split("/"):
        raise ValueError(f"Unsafe package-standard .metadata relative path (escapes the folder): {relative_path!r}")
    return stripped


def is_package_standard_sidecar_path(relative_path: str) -> bool:
    path = relative_path.replace("\\", "/").lstrip("/")
    if path.endswith(f"/{PACKAGE_STANDARD_DEFINITION_FILENAME}"):
        return True
    metadata_marker = f"/{PACKAGE_STANDARD_METADATA_FOLDER}/"
    return (
        path == PACKAGE_STANDARD_DEFINITION_FILENAME
        or path == PACKAGE_STANDARD_METADATA_FOLDER
        or path.startswith(f"{PACKAGE_STANDARD_METADATA_FOLDER}/")
        or metadata_marker in path
    )


async def read_package_standard_definition(package_url: str) -> Optional[PackageStandardDefinitionSeed]:
    paths = package_standard_sidecar_paths(package_url)
    if not await wrapp_url_exists(paths.definition_url):
        return None
    try:
        definition = json.loads(await wrapp_read_text_file(paths.definition_url))
    except json.JSONDecodeError as e:
        raise FailedCommand(f"Failed to parse package-standard sidecar '{paths.definition_url}' as JSON: {e}") from e
    if not isinstance(definition, dict):
        raise FailedCommand(f"Package-standard sidecar '{paths.definition_url}' must contain a JSON object.")
    try:
        return PackageStandardDefinitionSeed(**definition)
    except ValidationError as e:
        raise FailedCommand(f"Package-standard sidecar '{paths.definition_url}' failed seed validation: {e.errors()}") from e


async def write_package_standard_definition_seed(
    package_url: str,
    package_id: str,
    license_id: str,
    context: CommandParameters,
) -> None:
    """Write a definition-only package-standard seed (identity, no BOM) next to ``package_url``.

    ``new`` uses this to declare a package's SimReady package-standard identity (``package_id``
    plus SPDX ``license``) up front. A later ``create`` from the same source directory reads the
    seed and generates the full definition + BOM sidecars licensed as declared, without re-specifying
    the license. Only the identity is written here; there is no content to hash yet.
    """
    if context.dry_run:
        return
    seed = PackageStandardDefinitionSeed(
        package_id=package_id,
        license=license_id,
        format_version=PACKAGE_STANDARD_FORMAT_VERSION,
    )
    paths = package_standard_sidecar_paths(package_url)
    result = await wrapp_write_file_content(context, paths.definition_url, package_standard_model_json(seed))
    if result != Result.OK:
        raise StorageOperationError(f"Can't write package-standard definition seed to {paths.definition_url}: {result}")


async def read_package_standard_definition_bytes(package_url: str) -> Optional[bytes]:
    """Read the raw bytes of a package's definition sidecar, or ``None`` if absent.

    The bytes are returned verbatim so callers can preserve the definition without
    re-serializing or re-hashing it.
    """
    definition_url = package_standard_sidecar_paths(package_url).definition_url
    if not await wrapp_url_exists(definition_url):
        return None
    status, content = await wrapp_read_file_content(definition_url)
    if status != Result.OK:
        raise StorageOperationError(f"Can't read package-standard sidecar {definition_url}: {status}")
    return content


async def list_package_standard_metadata_files(package_url: str) -> list[Tuple[str, str]]:
    """List every regular file in a package's ``.metadata`` folder, recursively.

    Returns ``(relative_path, url)`` pairs where ``relative_path`` is POSIX-style and
    relative to the ``.metadata`` folder itself (e.g. ``com.nvidia.simready.packaging.bom.json``
    or ``sub/extra.json``). The folder is listed by URL rather than discovered by scanning
    the package directory, so a missing ``.metadata`` folder surfaces as a not-found (or
    wrong-type) listing error which is treated as "no metadata" and yields an empty list.
    If listing is unsupported, use the paths declared in the package definition
    instead. Plain WRAPP packages without a definition return an empty list quietly;
    standard packages warn that additional, unregistered files cannot be discovered.
    """
    metadata_dir = package_standard_metadata_folder_url(package_url)
    files: list[Tuple[str, str]] = []
    if not wrapp_list_directory_supported(metadata_dir):
        definition = await read_package_standard_definition(package_url)
        if definition is None:
            return []
        try:
            metadata = TypeAdapter(list[PackageStandardMetadataEntry]).validate_python((definition.model_extra or {}).get("metadata", []))
        except ValidationError as e:
            raise FailedCommand(f"Package-standard sidecar for '{package_url}' has invalid metadata entries: {e.errors()}") from e
        for entry in metadata:
            try:
                rel_path = guard_package_standard_metadata_relative_path(entry.name)
            except ValueError as e:
                raise StorageOperationError(str(e)) from e
            files.append((rel_path, join_url_path(metadata_dir, rel_path)))
        logger.warning(
            "Storage backend for %s does not support directory listing; only metadata referenced by the package definition "
            "can be discovered, not additional files in .metadata/",
            metadata_dir,
        )
        return files

    async def _walk(url: str, prefix: str) -> None:
        use_tree = wrapp_list_file_tree_supported(url)
        items = wrapp_list_file_tree(url) if use_tree else wrapp_list_directory(url)
        async for item in items:
            rel_path = join_paths(prefix, item.relative_path) if prefix else item.relative_path
            try:
                rel_path = guard_package_standard_metadata_relative_path(rel_path)
            except ValueError as e:
                raise StorageOperationError(str(e)) from e
            if is_regular_file(item):
                files.append((rel_path, join_url_path(metadata_dir, rel_path)))
            elif is_folder(item) and not use_tree:
                await _walk(join_url_path(url, item.relative_path), rel_path)

    try:
        await _walk(metadata_dir, "")
    except StorageOperationError as e:
        if e.result in (Result.ERROR_NOT_FOUND, Result.ERROR_WRONG_TYPE):
            return []
        raise
    return files


async def read_package_standard_metadata_files(package_url: str) -> Dict[str, bytes]:
    """Read discovered metadata files, keyed by path relative to ``.metadata``.

    The bytes are returned verbatim so callers can preserve the metadata without
    re-serializing or re-hashing it. Without listing support, only paths declared in
    the package definition are read. Missing declared files raise an error.
    """
    contents: Dict[str, bytes] = {}
    for rel_path, url in await list_package_standard_metadata_files(package_url):
        status, content = await wrapp_read_file_content(url)
        if status != Result.OK:
            raise StorageOperationError(f"Can't read package-standard sidecar {url}: {status}")
        contents[rel_path] = content
    return contents


async def copy_package_standard_sidecars(
    source_package_url: str,
    target_package_url: str,
    context: CommandParameters,
) -> None:
    """Copy an existing package's definition and ``.metadata`` sidecars to the target, verbatim.

    Sidecars are not part of the package catalog, so they are not carried by normal
    content copies; install/mirror/import preserve them with this helper. The root
    definition file and every file in the ``.metadata`` folder are copied byte-for-byte,
    so no re-hashing of the payload is required. Existing target files are left untouched.
    Without listing support, only metadata referenced by the definition is copied,
    with a warning that additional files cannot be discovered.
    """
    if context.dry_run:
        return

    definition_bytes = await read_package_standard_definition_bytes(source_package_url)
    target_paths = package_standard_sidecar_paths(target_package_url)
    if definition_bytes is not None and not await wrapp_url_exists(target_paths.definition_url):
        result = await wrapp_write_file_bytes(context, target_paths.definition_url, definition_bytes)
        if result != Result.OK:
            raise StorageOperationError(f"Can't write package-standard sidecar to {target_paths.definition_url}: {result}")

    target_metadata_dir = package_standard_metadata_folder_url(target_package_url)
    for rel_path, source_url in await list_package_standard_metadata_files(source_package_url):
        target_url = join_url_path(target_metadata_dir, rel_path)
        if await wrapp_url_exists(target_url):
            continue
        status, content = await wrapp_read_file_content(source_url)
        if status != Result.OK:
            raise StorageOperationError(f"Can't read package-standard sidecar {source_url}: {status}")
        result = await wrapp_write_file_bytes(context, target_url, content)
        if result != Result.OK:
            raise StorageOperationError(f"Can't write package-standard sidecar to {target_url}: {result}")


async def generate_package_standard_sidecars(
    package_url: Optional[str],
    package: PackageInfo,
    context: CommandParameters,
    create_package_standard_license: Optional[str] = None,
    content_hashes: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[PackageStandardSidecars]:
    existing_definition = await read_package_standard_definition(package_url) if package_url is not None else None
    existing_package_id = _string_or_none(existing_definition.package_id) if existing_definition else None
    existing_license = _string_or_none(existing_definition.license) if existing_definition else None

    license_id = existing_license or create_package_standard_license
    if license_id is not None:
        license_id = license_id.strip()
    if not license_id:
        if create_package_standard_license is not None and not create_package_standard_license.strip():
            logger.error(PACKAGE_STANDARD_BLANK_LICENSE_MESSAGE)
        return None

    if package.dependencies:
        logger.error(PACKAGE_STANDARD_DEPENDENCIES_UNSUPPORTED_MESSAGE)
        return None

    package_id = existing_package_id or f"{package.name}.{package.version}"
    bom = await generate_bom_metadata(package, content_hashes or {})
    content_hash = compute_content_hash(bom.items)

    bom_json = package_standard_model_json(bom)
    bom_hash = _hash_bytes(bom_json.encode("utf-8"))
    metadata = [
        PackageStandardMetadataEntry(
            name=PACKAGE_STANDARD_BOM_FILENAME,
            hash=bom_hash,
        ),
    ]
    package_hash = compute_package_hash(package_id, license_id, content_hash, metadata)

    definition = PackageStandardDefinition(
        package_id=package_id,
        license=license_id,
        metadata=metadata,
        content_hash=content_hash,
        package_hash=package_hash,
    )

    return PackageStandardSidecars(definition=definition, bom=bom)


async def write_package_standard_sidecars(
    package_url: str,
    package: PackageInfo,
    context: CommandParameters,
    create_package_standard_license: Optional[str] = None,
    content_hashes: Optional[Dict[str, Dict[str, str]]] = None,
) -> None:
    sidecars = await generate_package_standard_sidecars(package_url, package, context, create_package_standard_license, content_hashes)
    if sidecars is None:
        return

    paths = package_standard_sidecar_paths(package_url)
    definition_result = await wrapp_write_file_content(context, paths.definition_url, sidecars.definition_json)
    if definition_result != Result.OK:
        raise StorageOperationError(f"Can't write package-standard sidecar to {paths.definition_url}: {definition_result}")

    bom_result = await wrapp_write_file_content(context, paths.bom_url, sidecars.bom_json)
    if bom_result != Result.OK:
        raise StorageOperationError(f"Can't write package-standard BOM sidecar to {paths.bom_url}: {bom_result}")


def _package_standard_hashes_from_bytes(content: bytes) -> Dict[str, str]:
    hashes, _size = calc_package_standard_hashes_from_stream(io.BytesIO(content))
    return hashes


async def _compute_content_hashes_for_url(url: str) -> Dict[str, str]:
    """Compute the package-standard content hashes for a single file, off the event loop.

    Used as a fallback when the hashes were not produced during cataloging (e.g. the catalog
    resolved the item's hash from server-side metadata on a remote backend, so the file was never
    read locally). The content is read through the universal storage API (``wrapp_get_cached_file``
    is not implemented by every backend, e.g. S3) and the SHA-256 work is offloaded to the CPU
    thread pool, so the WRAPP event loop is never blocked. The file is held in memory for the
    duration of the hash; reducing that for remote backends is a deferred optimization.
    """
    status, content = await wrapp_read_file_content(url)
    if status != Result.OK:
        raise StorageOperationError(f"Failed to read {url} for package-standard hashing: {status}")
    collected: list = []
    await CPUThrottler.me().process_local_task_multi_threaded(collected.append, _package_standard_hashes_from_bytes, content)
    return collected[0]


async def generate_bom_metadata(package: PackageInfo, content_hashes: Dict[str, Dict[str, str]]) -> PackageStandardBom:
    catalog = package.catalog
    if catalog is None or not catalog.items:
        return PackageStandardBom(items=[])

    item_pairs = sorted(
        [(item, _catalog_item_to_bom_item(item)) for item in catalog.items if not is_package_standard_content_excluded(item.relative_path)],
        key=lambda pair: pair[1].relative_path.encode("utf-8"),
    )

    for catalog_item, bom_item in item_pairs:
        hashes = content_hashes.get(catalog_item.relative_path)
        if hashes is None:
            # Not precomputed during cataloging (e.g. the catalog resolved this item's hash from
            # server metadata on a remote backend, so the file was never read locally). Compute the
            # package-standard hashes now, offloaded and streamed from a local cached copy.
            hashes = await _compute_content_hashes_for_url(catalog.get_source_url_for_item(catalog_item))
        bom_item.hash = {**(bom_item.hash or {}), **hashes}

    return PackageStandardBom(items=[bom_item for _, bom_item in item_pairs])


def is_package_standard_content_excluded(relative_path: str) -> bool:
    path = relative_path.replace("\\", "/").lstrip("/")
    return path.endswith(PACKAGE_FILE_EXTENSION) or is_package_standard_sidecar_path(path)


def _catalog_item_to_bom_item(item: CatalogItem) -> PackageStandardBomItem:
    size = item.size
    if size is None:
        logger.warning(f"BOM item '{item.relative_path}' has no size; using 0 as fallback.")
        size = 0

    return PackageStandardBomItem(
        relative_path=item.relative_path,
        size=size,
    )


def _hash_bytes(data: bytes) -> Dict[str, str]:
    return {"sha256": hashlib.sha256(data).hexdigest()}


def compute_content_hash(bom_items: list[PackageStandardBomItem]) -> Dict[str, str]:
    sorted_items = sorted(bom_items, key=lambda item: item.relative_path.encode("utf-8"))

    buf = bytearray()
    for item in sorted_items:
        item_hash = item.hash or {}
        sha256_hash = item_hash.get("sha256")
        if not isinstance(sha256_hash, str):
            raise FailedCommand(f"BOM item '{item.relative_path}' is missing its SHA-256 hash.")
        buf.extend(item.relative_path.encode("utf-8"))
        buf.append(0x00)
        buf.extend(bytes.fromhex(sha256_hash))

    return _hash_bytes(bytes(buf))


def compute_package_hash(
    package_id: str,
    license_id: str,
    content_hash: Dict[str, str],
    metadata_entries: list[PackageStandardMetadataEntry],
) -> Dict[str, str]:
    sorted_entries = sorted(metadata_entries, key=lambda entry: entry.name.encode("utf-8"))

    buf = bytearray()
    buf.extend(package_id.encode("utf-8"))
    buf.append(0x00)
    buf.extend(license_id.encode("utf-8"))
    buf.append(0x00)
    buf.extend(bytes.fromhex(content_hash["sha256"]))
    for entry in sorted_entries:
        buf.extend(entry.name.encode("utf-8"))
        buf.append(0x00)
        buf.extend(bytes.fromhex(entry.hash["sha256"]))

    return _hash_bytes(bytes(buf))


def _string_or_none(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None
