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

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Tuple,
)
from urllib.parse import (
    quote,
    unquote,
    urlparse,
)

from wrapp.datastructures.command_context import ThrottlingParameters
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode
from wrapp.utils.storage_interface import (
    ConnectionStatus,
    DirectorySemantics,
    HuggingFaceAuthInfo,
    ListEntry,
    LocalReadOnlyFile,
    ResourceIdentity,
    Result,
    SerializedResourceIdentity,
    ServerInfo,
    StorageAuthInfo,
    StorageInterface,
    URLResourceIdentity,
)
from wrapp.utils.task_throttler import (
    TaskThrottler,
    ThrottlerType,
)
from wrapp.utils.utils import (
    StorageMissingDependenciesError,
    StorageOperationError,
    file_url_to_path,
)

logger = logging.getLogger("omni.wrapp")

try:
    import httpx
    from huggingface_hub import (
        HfApi,
        RepoFile,
        RepoFolder,
        hf_hub_download,
    )
    from huggingface_hub.errors import (
        EntryNotFoundError,
        GatedRepoError,
        RepositoryNotFoundError,
        RevisionNotFoundError,
    )
except ImportError as e:
    HfApi = None  # type: ignore[assignment]
    RepoFile = None  # type: ignore[assignment]
    RepoFolder = None  # type: ignore[assignment]
    hf_hub_download = None  # type: ignore[assignment]
    _not_found_errors: tuple[type[Exception], ...] = ()
    _repository_errors: tuple[type[Exception], ...] = ()
    _gated_errors: tuple[type[Exception], ...] = ()
    _connection_errors: tuple[type[Exception], ...] = (ConnectionError, TimeoutError)
    _hf_import_error: Optional[str] = (
        "failed to import huggingface_hub. Required dependency: huggingface_hub. "
        "Install with 'pip install ovpackage[huggingface]'. "
        f"Exception: {e}"
    )
else:
    _hf_import_error = None
    _not_found_errors = (EntryNotFoundError, RepositoryNotFoundError, RevisionNotFoundError)
    _repository_errors = (RepositoryNotFoundError,)
    _gated_errors = (GatedRepoError,)
    _connection_errors = (httpx.TransportError, ConnectionError, TimeoutError)


HF_DATASET_REPO_TYPE = "dataset"
HF_DATASET_URL_NETLOC = "datasets"
HF_TOKEN_ENV_VARS = ("WRAPP_HUGGINGFACE_TOKEN", "WRAPP_HF_TOKEN", "HF_TOKEN")
HF_ENDPOINT_ENV_VARS = ("WRAPP_HUGGINGFACE_ENDPOINT", "WRAPP_HF_ENDPOINT", "HF_ENDPOINT")


@dataclass(frozen=True)
class ParsedHuggingFaceUrl:
    repo_id: str
    path_in_repo: str
    revision: Optional[str] = None

    @property
    def server_url(self) -> str:
        repo = self.repo_id if self.revision is None else f"{self.repo_id}@{quote(self.revision, safe='')}"
        return f"hf://datasets/{repo}"

    def to_url(self) -> str:
        if self.path_in_repo:
            return f"{self.server_url}/{quote(self.path_in_repo, safe='/')}"
        return self.server_url


def parse_huggingface_url(url: str) -> ParsedHuggingFaceUrl:
    parsed = urlparse(url)
    if parsed.scheme != "hf" or parsed.netloc != HF_DATASET_URL_NETLOC:
        raise ValueError(f"Not a Hugging Face dataset URL: {url}")

    parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"Hugging Face dataset URLs must be of the form hf://datasets/<owner>/<repo>/<path>: {url}")

    owner = parts[0]
    repo_name = parts[1]
    revision = None
    if "@" in repo_name:
        repo_name, revision = repo_name.split("@", 1)
        if not repo_name or not revision:
            raise ValueError(f"Invalid Hugging Face dataset revision in URL: {url}")

    repo_id = f"{owner}/{repo_name}"
    path_in_repo = "/".join(parts[2:]).strip("/")
    return ParsedHuggingFaceUrl(repo_id=repo_id, path_in_repo=path_in_repo, revision=revision)


def is_huggingface_url(url: str) -> bool:
    try:
        parse_huggingface_url(url)
        return True
    except ValueError:
        return False


class HuggingFaceListEntry(ListEntry):
    @classmethod
    def from_repo_entry(cls, entry: Any, parent_path: str = "") -> "HuggingFaceListEntry":
        entry_path = getattr(entry, "path", "")
        is_folder = RepoFolder is not None and isinstance(entry, RepoFolder)

        size = getattr(entry, "size", None)
        lfs = getattr(entry, "lfs", None)
        if size is None and isinstance(lfs, dict):
            size = lfs.get("size")

        version = getattr(entry, "blob_id", None) or getattr(entry, "tree_id", None) or ""
        return HuggingFaceListEntry(
            entry_path,
            is_folder=is_folder,
            parent_path=parent_path,
            size=size,
            modified_time=_extract_modified_time(entry),
            version=str(version),
        )

    @classmethod
    def folder(cls, path: str, parent_path: str = "", version: str = "") -> "HuggingFaceListEntry":
        return HuggingFaceListEntry(path.rstrip("/"), is_folder=True, parent_path=parent_path, version=version)

    def __init__(
        self,
        path: str,
        *,
        is_folder: bool,
        parent_path: str = "",
        size: Optional[int] = None,
        modified_time: Optional[datetime] = None,
        version: str = "",
    ):
        if parent_path and not parent_path.endswith("/"):
            parent_path += "/"
        if path and parent_path and not path.startswith(parent_path):
            raise ValueError(f"Entry path {path!r} is not under parent path {parent_path!r}")

        self._is_folder = is_folder
        self._relative_path = path[len(parent_path) :] if parent_path else path
        self._size = size
        self._modified_time = modified_time
        self._version = version
        self._nucleus_hash: Optional[str] = None

    @property
    def is_folder(self) -> bool:
        return self._is_folder

    @property
    def is_regular_file(self) -> bool:
        return self._relative_path != "" and not self._is_folder

    @property
    def has_checkpoint(self) -> bool:
        return self.is_regular_file

    @property
    def version(self) -> str:
        return self._version

    @property
    def relative_path(self) -> str:
        return self._relative_path

    @property
    def nucleus_hash(self) -> Optional[str]:
        return self._nucleus_hash

    @nucleus_hash.setter
    def nucleus_hash(self, value: str):
        if self._nucleus_hash is not None:
            raise AttributeError("Nucleus hash cannot be changed once set")
        self._nucleus_hash = value

    @property
    def size(self) -> Optional[int]:
        return self._size

    @property
    def modified_time(self) -> Optional[datetime]:
        return self._modified_time


class HuggingFaceServerInfo(ServerInfo):
    def __init__(self, auth_token: str = ""):
        self._auth_token = auth_token

    def supports_create_with_hash(self, target_url: str) -> bool:
        return False

    @property
    def auth_token(self) -> str:
        return self._auth_token


class HuggingFaceLocalReadOnlyFile(LocalReadOnlyFile):
    def __init__(self, file_path: str):
        self._file_path = file_path

    def local_path(self) -> str:
        return self._file_path

    def close(self):
        pass


def _extract_modified_time(entry: Any) -> Optional[datetime]:
    direct = getattr(entry, "last_modified", None)
    if isinstance(direct, datetime):
        return direct

    last_commit = getattr(entry, "last_commit", None)
    if isinstance(last_commit, dict):
        value = last_commit.get("date")
    else:
        value = getattr(last_commit, "date", None)

    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _exception_status_code(e: Exception) -> Optional[int]:
    response = getattr(e, "response", None)
    return getattr(response, "status_code", None)


def _is_not_found_exception(e: Exception) -> bool:
    status = _exception_status_code(e)
    if isinstance(e, _gated_errors) or status in (401, 403):
        return False
    return isinstance(e, _not_found_errors) or status == 404


class HuggingFaceStorage(StorageInterface):
    """Read-only storage backend for Hugging Face dataset repositories.

    Supports listing, stat and download operations on hf://datasets/<owner>/<repo>[@<revision>]/<path>
    URLs. All mutating operations return ERROR_NOT_SUPPORTED. The Router rejects
    write destinations before I/O: Hugging Face dataset repositories can be used as a source
    (mirror/install from them) but not as a destination.
    """

    # Available without constructing the optional-SDK-backed instance, so a
    # destination still gets a read-only error when the extra is absent.
    is_read_only = True

    @classmethod
    def version_info(cls) -> str:
        if _hf_import_error is not None:
            return "Hugging Face Backend (huggingface_hub not available)"
        try:
            from importlib.metadata import version

            return f"Hugging Face Backend (huggingface_hub {version('huggingface_hub')})"
        except Exception:
            return "Hugging Face Backend (version unknown)"

    def __init__(self, wrapp_version: str, debug_level: bool, throttling_params: ThrottlingParameters):
        if _hf_import_error is not None:
            raise StorageMissingDependenciesError(_hf_import_error)

        self._wrapp_version = wrapp_version
        self._debug_level = debug_level
        self._auth_info_by_server: Dict[str, StorageAuthInfo] = {}
        self._api_by_endpoint: Dict[str, Any] = {}
        self._last_connection_status: Dict[str, ConnectionStatus] = {}
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "HuggingFace", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "HuggingFace", throttling_params.file_transfer_jobs)

    def backend_throttlers(self) -> list[TaskThrottler]:
        return [self._throttler_roundtrip, self._throttler_networkdata]

    def reconfigure_throttlers(self, throttling_params: ThrottlingParameters) -> bool:
        if (
            self._throttler_roundtrip.max_parallel_tasks == throttling_params.jobs
            and self._throttler_networkdata.max_parallel_tasks == throttling_params.file_transfer_jobs
        ):
            return True
        for t in self.backend_throttlers():
            if t.pending_tasks or t.processing_tasks:
                logger.warning(f"Cannot update throttling parameters for Hugging Face: Throttler {t.server_backend_name} has open tasks.")
                return False
        self._throttler_roundtrip = TaskThrottler(ThrottlerType.ServerRoundTrip, "HuggingFace", throttling_params.jobs)
        self._throttler_networkdata = TaskThrottler(ThrottlerType.NetworkDataTransfer, "HuggingFace", throttling_params.file_transfer_jobs)
        return True

    def supports_tags(self, url: str) -> bool:
        return False

    def directory_semantics(self) -> DirectorySemantics:
        return DirectorySemantics.PREFIX

    def supports_comments(self) -> bool:
        return False

    async def shutdown(self, exc_type=None, exc_val=None, exc_tb=None):
        self._api_by_endpoint.clear()

    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        try:
            parsed = parse_huggingface_url(url)
            token, _ = self._credentials_for(parsed.server_url)
            return Result.OK, HuggingFaceServerInfo(token or "")
        except ValueError:
            return Result.ERROR_BAD_REQUEST, HuggingFaceServerInfo()

    async def normalize(self, url: str) -> str:
        return parse_huggingface_url(url).to_url()

    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        try:
            parsed = parse_huggingface_url(url)
            wants_folder = url.endswith("/") and parsed.path_in_repo != ""
            if not parsed.path_in_repo:
                await self._repo_info(parsed)
                return Result.OK, HuggingFaceListEntry.folder("")

            parent_path = _parent_path(parsed.path_in_repo)
            entries = await self._get_paths_info(parsed, expand=True)
            target_path = parsed.path_in_repo.rstrip("/")
            for entry in entries:
                if getattr(entry, "path", "") == target_path:
                    list_entry = HuggingFaceListEntry.from_repo_entry(entry, parent_path)
                    if not wants_folder or list_entry.is_folder:
                        return Result.OK, list_entry

            return Result.ERROR_NOT_FOUND, None
        except ValueError as e:
            logger.error(f"Hugging Face stat URL parse error {url}: {e}")
            return Result.ERROR_BAD_REQUEST, None
        except Exception as e:
            return self._handle_result_exception(e, url), None

    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, ResourceIdentity]:
        if not item.is_regular_file:
            raise TypeError("Can only generate ResourceIdentity for Hugging Face files")

        parsed = parse_huggingface_url(file_url)
        if not parsed.path_in_repo:
            raise TypeError("Can only generate ResourceIdentity for Hugging Face files")
        if not item.version:
            raise StorageOperationError(
                f"Cannot generate an exact Hugging Face resource identity for {file_url}: the listed file has no blob ID",
                Result.ERROR_BAD_VERSION,
            )
        if item.size is None:
            raise StorageOperationError(
                f"Cannot generate an exact Hugging Face resource identity for {file_url}: the listed file has no size",
                Result.ERROR_BAD_VERSION,
            )

        commit_sha = _extract_repo_commit_sha(await self._repo_info(parsed))
        if not commit_sha:
            raise StorageOperationError(
                f"Could not resolve Hugging Face revision for {file_url} to an immutable commit SHA",
                Result.ERROR_NOT_SUPPORTED,
            )

        locked = ParsedHuggingFaceUrl(
            repo_id=parsed.repo_id,
            path_in_repo=parsed.path_in_repo,
            revision=commit_sha,
        )
        entry = await self._repo_file_entry(locked)
        blob_id = _extract_repo_entry_object_id(entry)
        if item.version != blob_id:
            raise StorageOperationError(
                f"Hugging Face file {file_url} changed while generating a resource identity: {item.version} != {blob_id}",
                Result.ERROR_BAD_VERSION,
            )
        size = _extract_repo_entry_size(entry)
        if item.size != size:
            raise StorageOperationError(
                f"Hugging Face file {file_url} changed size while generating a resource identity: {item.size} != {size}",
                Result.ERROR_BAD_VERSION,
            )

        locked_url = locked.to_url()
        return file_url, relative_url, URLResourceIdentity(locked_url)

    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        result, item = await self.stat(url)
        return result, item.is_folder if item else False

    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        try:
            parsed = parse_huggingface_url(url)
            parent_path = parsed.path_in_repo.rstrip("/")
            try:
                entries = await self._list_repo_tree(parsed, parent_path, recursive=False, expand=True)
            except Exception as e:
                if not _is_not_found_exception(e):
                    raise
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    raise StorageOperationError(f"Trying to list a file {url}", Result.ERROR_WRONG_TYPE) from e
                raise StorageOperationError(f"Resource not found: {url}", Result.ERROR_NOT_FOUND) from e
            if not entries:
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    raise StorageOperationError(f"Trying to list a file {url}", Result.ERROR_WRONG_TYPE)
                if stat_result == Result.ERROR_NOT_FOUND:
                    raise StorageOperationError(f"Resource not found: {url}", Result.ERROR_NOT_FOUND)
                if stat_result != Result.OK:
                    raise StorageOperationError(f"Could not list {url}", stat_result)

            for entry in entries:
                yield HuggingFaceListEntry.from_repo_entry(entry, parent_path)
        except ValueError as e:
            raise StorageOperationError(f"Invalid Hugging Face URL: {e}", Result.ERROR_BAD_REQUEST) from e
        except Exception as e:
            if isinstance(e, StorageOperationError):
                raise
            result = self._handle_result_exception(e, url)
            raise StorageOperationError(f"Hugging Face list_directory failed for {url}: {e}", result) from e

    def supports_list_file_tree(self) -> bool:
        return True

    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        try:
            parsed = parse_huggingface_url(url)
            parent_path = parsed.path_in_repo.rstrip("/")
            try:
                entries = await self._list_repo_tree(parsed, parent_path, recursive=True, expand=True)
            except Exception as e:
                if not _is_not_found_exception(e):
                    raise
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    yield stat_entry
                    return
                raise StorageOperationError(f"Resource not found: {url}", Result.ERROR_NOT_FOUND) from e
            if not entries:
                stat_result, stat_entry = await self.stat(url)
                if stat_result == Result.OK and stat_entry is not None and stat_entry.is_regular_file:
                    yield stat_entry
                    return
                if stat_result == Result.ERROR_NOT_FOUND:
                    raise StorageOperationError(f"Resource not found: {url}", Result.ERROR_NOT_FOUND)
                if stat_result != Result.OK:
                    raise StorageOperationError(f"Could not list {url}", stat_result)

            emitted_folders: set[str] = set()
            for entry in entries:
                entry_path = getattr(entry, "path", "")
                for folder_path in _ancestor_folders(entry_path, parent_path):
                    if folder_path not in emitted_folders:
                        emitted_folders.add(folder_path)
                        yield HuggingFaceListEntry.folder(folder_path, parent_path)
                if RepoFolder is not None and isinstance(entry, RepoFolder):
                    emitted_folders.add(entry_path.rstrip("/"))
                yield HuggingFaceListEntry.from_repo_entry(entry, parent_path)
        except ValueError as e:
            raise StorageOperationError(f"Invalid Hugging Face URL: {e}", Result.ERROR_BAD_REQUEST) from e
        except Exception as e:
            if isinstance(e, StorageOperationError):
                raise
            result = self._handle_result_exception(e, url)
            raise StorageOperationError(f"Hugging Face list_file_tree failed for {url}: {e}", result) from e

    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        return Result.ERROR_NOT_SUPPORTED, []

    async def copy_address(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def copy_resource_identity(
        self,
        resource_identity: str,
        dst_url: str,
        overwrite: bool = False,
        message: str = "",
    ) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        return False

    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def _validate_resource_identity(self, resource_identity: SerializedResourceIdentity) -> Result:
        try:
            parsed = parse_huggingface_url(resource_identity)
        except ValueError:
            return Result.ERROR_BAD_REQUEST

        if parsed.revision is None:
            return Result.ERROR_BAD_VERSION

        try:
            resolved_sha = _extract_repo_commit_sha(await self._repo_info(parsed))
        except Exception as e:
            return self._handle_result_exception(e, resource_identity)
        if resolved_sha != parsed.revision:
            return Result.ERROR_BAD_VERSION

        return Result.OK

    async def read_resource_identity(
        self, resource_identity: SerializedResourceIdentity, *, expected_hash: Optional[str] = None
    ) -> Tuple[Result, bytes]:
        """Read an immutable commit-qualified Hugging Face identity."""
        result = await self._validate_resource_identity(resource_identity)
        if result != Result.OK:
            return result, bytes()
        return await self.read_file_content(resource_identity)

    async def download_resource_identity(
        self,
        resource_identity: SerializedResourceIdentity,
        dst_url: str,
        overwrite: bool,
        *,
        expected_hash: Optional[str] = None,
    ) -> Result:
        """Download the recorded commit without loading its content into memory."""
        result = await self._validate_resource_identity(resource_identity)
        if result != Result.OK:
            return result
        return await self.download_file(resource_identity, dst_url, overwrite)

    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        result, local_file = await self.get_cached_file_async(file_url, download=True)
        if result != Result.OK or local_file is None:
            return result, bytes()
        try:
            with open(local_file.local_path(), "rb") as f:
                return Result.OK, f.read()
        except OSError as e:
            logger.error(f"Hugging Face read_file_content failed for {file_url}: {e}")
            return Result.ERROR, bytes()
        finally:
            local_file.close()

    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        try:
            parsed = parse_huggingface_url(file_url)
            if not parsed.path_in_repo or file_url.endswith("/"):
                return Result.ERROR_WRONG_TYPE, None
            token, endpoint = self._credentials_for(parsed.server_url)

            def _download() -> str:
                assert hf_hub_download is not None
                return hf_hub_download(
                    repo_id=parsed.repo_id,
                    filename=parsed.path_in_repo,
                    repo_type=HF_DATASET_REPO_TYPE,
                    revision=parsed.revision,
                    token=token,
                    endpoint=endpoint,
                    local_files_only=not download,
                )

            local_path = await self._throttler_networkdata.run(_run_sync_in_executor(_download))
            self._set_status(parsed.server_url, ConnectionStatus.CONNECTED)
            return Result.OK, HuggingFaceLocalReadOnlyFile(local_path)
        except ValueError as e:
            logger.error(f"Hugging Face get_cached_file_async URL parse error {file_url}: {e}")
            return Result.ERROR_BAD_REQUEST, None
        except Exception as e:
            return self._handle_result_exception(e, file_url), None

    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def delete_item(self, url: str) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    def can_be_cached(self, url: str) -> bool:
        return True

    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        return Result.ERROR_NOT_SUPPORTED

    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        """Download an address to a native path or an escaped file URL."""
        try:
            dst_path = file_url_to_path(dst_url)
            if os.path.exists(dst_path) and not overwrite:
                return Result.ERROR_ALREADY_EXISTS

            result, local_file = await self.get_cached_file_async(src_url, download=True)
            if result != Result.OK or local_file is None:
                return result

            try:
                dst_dir = os.path.dirname(dst_path)
                if dst_dir:
                    os.makedirs(dst_dir, exist_ok=True)
                shutil.copyfile(local_file.local_path(), dst_path)
            finally:
                local_file.close()
            return Result.OK
        except OSError as e:
            logger.error(f"Hugging Face download_file failed for {src_url}: {e}")
            return Result.ERROR

    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        self._auth_info_by_server.clear()
        for auth in auth_info:
            self.add_auth_info(auth)

    def add_auth_info(self, auth_info: StorageAuthInfo):
        if not isinstance(auth_info.auth_data, HuggingFaceAuthInfo):
            raise StorageOperationError(
                f"Incompatible authentication information for server {auth_info.server_url}: "
                f"expected HuggingFaceAuthInfo, got {auth_info.auth_data.__class__}"
            )
        server_url = parse_huggingface_url(auth_info.server_url).server_url
        logger.info(f"Setting Hugging Face auth information for {server_url}")
        self._auth_info_by_server[server_url] = auth_info

    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        parts = auth_param.split(",")
        if len(parts) > 3:
            raise StorageOperationError("Authentication parameter for Hugging Face needs to be of the form server_url[,token[,endpoint]]!")
        server_url = parts[0].strip()
        if not server_url:
            raise StorageOperationError("Authentication parameter for Hugging Face requires a server_url")
        parse_huggingface_url(server_url)
        token = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        endpoint = parts[2].strip().rstrip("/") if len(parts) > 2 and parts[2].strip() else None
        return StorageAuthInfo.huggingface(server_url=server_url, token=token, endpoint=endpoint)

    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        try:
            normalized = parse_huggingface_url(server_url).server_url
        except ValueError:
            normalized = server_url.rstrip("/")
        return self._last_connection_status.get(normalized)

    def _api(self, endpoint: Optional[str]):
        endpoint_key = endpoint or ""
        if endpoint_key not in self._api_by_endpoint:
            assert HfApi is not None
            self._api_by_endpoint[endpoint_key] = HfApi(endpoint=endpoint) if endpoint else HfApi()
        return self._api_by_endpoint[endpoint_key]

    async def _repo_info(self, parsed: ParsedHuggingFaceUrl) -> Any:
        token, endpoint = self._credentials_for(parsed.server_url)

        def _info() -> Any:
            api = self._api(endpoint)
            return api.repo_info(
                repo_id=parsed.repo_id,
                repo_type=HF_DATASET_REPO_TYPE,
                revision=parsed.revision,
                token=token,
            )

        result = await self._throttler_roundtrip.run(_run_sync_in_executor(_info))
        self._set_status(parsed.server_url, ConnectionStatus.CONNECTED)
        return result

    async def _list_repo_tree(self, parsed: ParsedHuggingFaceUrl, path_in_repo: str, *, recursive: bool, expand: bool) -> list[Any]:
        token, endpoint = self._credentials_for(parsed.server_url)
        query_path = path_in_repo.rstrip("/") or None

        def _list() -> list[Any]:
            api = self._api(endpoint)
            return list(
                api.list_repo_tree(
                    repo_id=parsed.repo_id,
                    path_in_repo=query_path,
                    recursive=recursive,
                    expand=expand,
                    revision=parsed.revision,
                    repo_type=HF_DATASET_REPO_TYPE,
                    token=token,
                )
            )

        entries = await self._throttler_roundtrip.run(_run_sync_in_executor(_list))
        self._set_status(parsed.server_url, ConnectionStatus.CONNECTED)
        return entries

    async def _get_paths_info(self, parsed: ParsedHuggingFaceUrl, *, expand: bool) -> list[Any]:
        token, endpoint = self._credentials_for(parsed.server_url)

        def _info() -> list[Any]:
            return self._api(endpoint).get_paths_info(
                repo_id=parsed.repo_id,
                paths=[parsed.path_in_repo.rstrip("/")],
                revision=parsed.revision,
                repo_type=HF_DATASET_REPO_TYPE,
                token=token,
                expand=expand,
            )

        entries = await self._throttler_roundtrip.run(_run_sync_in_executor(_info))
        self._set_status(parsed.server_url, ConnectionStatus.CONNECTED)
        return entries

    async def _repo_file_entry(self, parsed: ParsedHuggingFaceUrl) -> Any:
        target_path = parsed.path_in_repo.rstrip("/")
        entries = await self._get_paths_info(parsed, expand=False)
        for entry in entries:
            if getattr(entry, "path", "") == target_path and RepoFile is not None and isinstance(entry, RepoFile):
                return entry
        raise StorageOperationError(f"Hugging Face file not found at immutable revision: {parsed.to_url()}", Result.ERROR_NOT_FOUND)

    def _credentials_for(self, server_url: str) -> Tuple[Optional[str], Optional[str]]:
        server_urls = [server_url.rstrip("/")]
        try:
            parsed = parse_huggingface_url(server_url)
            if parsed.revision is not None:
                server_urls.append(ParsedHuggingFaceUrl(parsed.repo_id, "").server_url)
        except ValueError:
            pass

        for normalized_server_url in server_urls:
            auth_info = self._auth_info_by_server.get(normalized_server_url)
            if auth_info is not None:
                assert isinstance(auth_info.auth_data, HuggingFaceAuthInfo)
                token = auth_info.auth_data.token or _first_env_value(HF_TOKEN_ENV_VARS)
                endpoint = auth_info.auth_data.endpoint or _first_env_value(HF_ENDPOINT_ENV_VARS)
                return token, endpoint.rstrip("/") if endpoint else None

        token = _first_env_value(HF_TOKEN_ENV_VARS)
        endpoint = _first_env_value(HF_ENDPOINT_ENV_VARS)
        return token, endpoint.rstrip("/") if endpoint else None

    def _handle_result_exception(self, e: Exception, url: str) -> Result:
        try:
            parsed = parse_huggingface_url(url)
            server_url = parsed.server_url
        except ValueError:
            server_url = url.rstrip("/")

        result = _map_huggingface_exception_to_result(e)
        if result == Result.ERROR_ACCESS_DENIED:
            self._set_status(server_url, ConnectionStatus.AUTH_ERROR)
            if isinstance(e, _repository_errors) and _exception_status_code(e) == 401:
                logger.error(
                    "Hugging Face repository %s may not exist or may be private or gated. "
                    "Authenticate with --auth %s,<token> or HF_TOKEN and check your token's access permissions.",
                    server_url,
                    server_url,
                )
        elif result == Result.ERROR_CONNECTION:
            self._set_status(server_url, ConnectionStatus.CONNECT_ERROR)
        elif result == Result.ERROR_NOT_FOUND:
            self._set_status(server_url, ConnectionStatus.CONNECTED)
            # A "not found" outcome is a routine, expected result of existence/overwrite
            # probes (e.g. stat before upload), so log it at debug rather than error to
            # avoid alarming, misleading ERROR lines during normal operations.
            logger.debug(f"Hugging Face entry not found for {url}: {e}")
            return result
        else:
            self._set_status(server_url, ConnectionStatus.CONNECTION_STATUS_UNKNOWN)
        logger.error(f"Hugging Face storage error for {url}: {e}")
        return result

    def _set_status(self, server_url: str, status: ConnectionStatus):
        self._last_connection_status[server_url.rstrip("/")] = status


def _map_huggingface_exception_to_result(e: Exception) -> Result:
    if isinstance(e, StorageOperationError):
        return e.result
    status_code = _exception_status_code(e)
    if isinstance(e, _gated_errors) or status_code in (401, 403):
        return Result.ERROR_ACCESS_DENIED
    if isinstance(e, _connection_errors):
        return Result.ERROR_CONNECTION
    if _is_not_found_exception(e):
        return Result.ERROR_NOT_FOUND
    if status_code is not None and status_code >= 500:
        return Result.ERROR_CONNECTION
    if status_code is not None and status_code >= 400:
        return Result.ERROR_BAD_REQUEST
    if isinstance(e, ValueError):
        return Result.ERROR_BAD_REQUEST
    return Result.ERROR


async def _run_sync_in_executor(func):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func)


def _extract_repo_commit_sha(repo_info: Any) -> Optional[str]:
    if isinstance(repo_info, dict):
        value = repo_info.get("sha") or repo_info.get("commit_sha")
    else:
        value = getattr(repo_info, "sha", None) or getattr(repo_info, "commit_sha", None)
    return str(value) if value else None


def _extract_repo_entry_object_id(entry: Any) -> str:
    value = getattr(entry, "blob_id", None) or getattr(entry, "tree_id", None)
    return str(value) if value else ""


def _extract_repo_entry_size(entry: Any) -> Optional[int]:
    size = getattr(entry, "size", None)
    if size is None:
        lfs = getattr(entry, "lfs", None)
        if isinstance(lfs, dict):
            size = lfs.get("size")
    return size


def _parent_path(path: str) -> str:
    path = path.rstrip("/")
    if "/" not in path:
        return ""
    return path.rsplit("/", 1)[0]


def _ancestor_folders(entry_path: str, parent_path: str) -> list[str]:
    entry_path = entry_path.rstrip("/")
    if not entry_path:
        return []
    parent_path = parent_path.rstrip("/")
    if parent_path and entry_path.startswith(parent_path + "/"):
        relative = entry_path[len(parent_path) + 1 :]
        current = parent_path
    else:
        relative = entry_path
        current = ""

    folders: list[str] = []
    parts = relative.split("/")[:-1]
    for part in parts:
        current = f"{current}/{part}" if current else part
        folders.append(current)
    return folders


def _first_env_value(names: Tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None
