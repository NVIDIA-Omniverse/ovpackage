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

import warnings
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from enum import (
    Enum,
    auto,
)
from typing import (
    List,
    Optional,
    Tuple,
)

from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.scheduler_task_node import SchedulerTaskGroupNode


class Result(Enum):
    OK = auto()
    ERROR = auto()
    ERROR_CONNECTION = auto()
    ERROR_NOT_SUPPORTED = auto()
    ERROR_ACCESS_DENIED = auto()
    ERROR_NOT_FOUND = auto()
    ERROR_BAD_VERSION = auto()
    ERROR_ALREADY_EXISTS = auto()
    ERROR_SOURCE_IS_DEST = auto()
    ERROR_ACCESS_LOST = auto()
    ERROR_LOCKED = auto()
    ERROR_BAD_REQUEST = auto()
    ERROR_FOLDER_NOT_EMPTY = auto()
    ERROR_WRONG_TYPE = auto()
    ERROR_UNKNOWN = auto()


class DirectorySemantics(Enum):
    # In normal S3 buckets, a directory exists if there is a key which has the directory path as a prefix.
    # There are no empty directories, and a directory and a file can have exactly the same path.
    PREFIX = auto()
    # In a file system, a directory is a first class citizen. Empty directories exist,
    # and it is not possible for a file and a directory to have the same path.
    FILESYSTEM = auto()


class ConnectionStatus(Enum):
    AUTH_ABORT = "AUTH_ABORT"
    AUTH_CANCELLED = "AUTH_CANCELLED"
    AUTH_ERROR = "AUTH_ERROR"
    AUTH_FAILED = "AUTH_FAILED"
    CONNECTED = "CONNECTED"
    CONNECTING = "CONNECTING"
    CONNECT_ERROR = "CONNECT_ERROR"
    DISCONNECTED = "DISCONNECTED"
    INVALID_HOST = "INVALID_HOST"
    NO_USERNAME = "NO_USERNAME"
    SERVER_INCOMPATIBLE = "SERVER_INCOMPATIBLE"
    SIGNED_OUT = "SIGNED_OUT"
    CONNECTION_STATUS_UNKNOWN = "CONNECTION_STATUS_UNKNOWN"


@dataclass
class NucleusAuthInfo:
    server: str
    username: str
    password_or_token: str

    def __repr__(self):
        return (
            f"NucleusAuthInfo("
            f"server='***{self.server[-3:]}', "
            f"username='***{self.username[-3:]}', "
            f"password_or_token='***{self.password_or_token[-3:]}')"
        )


@dataclass
class Boto3AuthInfo:
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: Optional[str] = None

    def __str__(self):
        return (
            f"Boto3AuthInfo("
            f"aws_access_key_id=***{self.aws_access_key_id[-3:]}, "
            f"aws_secret_access_key=***{self.aws_secret_access_key[-3:]}, "
            f"aws_session_token=***{(self.aws_session_token or '')[-3:]})"
        )


@dataclass
class AzureBlobAuthInfo:
    """Authentication information for Azure Blob Storage."""

    connection_string: Optional[str] = None
    account_name: Optional[str] = None
    account_key: Optional[str] = None
    sas_token: Optional[str] = None

    def __post_init__(self):
        # Validate that we have at least one authentication method
        if not self.connection_string and not (self.account_name and (self.account_key or self.sas_token)):
            raise ValueError("Either connection_string or account_name with account_key/sas_token must be provided")

    def __str__(self):
        return (
            f"AzureBlobAuthInfo("
            f"connection_string={self.connection_string}, "
            f"account_name=***{self.account_name[-3:]}, "
            f"account_key=***{self.account_key[-3:]}, "
            f"sas_token=***{self.sas_token[-3:]})"
        )


@dataclass
class GCSAuthInfo:
    """Authentication information for Google Cloud Storage."""

    project_id: Optional[str] = None
    credentials_file: Optional[str] = None
    credentials_json: Optional[str] = None  # inline JSON for service account

    def __str__(self):
        return (
            f"GCSAuthInfo("
            f"project_id={self.project_id}, "
            f"credentials_file=***{(self.credentials_file or '')[-3:]}, "
            f"credentials_json=***{(self.credentials_json or '')[-3:]})"
        )


@dataclass
class StorageAPIBearerAuthInfo:
    server: str
    bearer_token: str

    def __repr__(self):
        return f"StorageAPIBearerAuthInfo(" f"server='***{self.server[-3:]}', " f"bearer_token='***{self.bearer_token[-3:]}')"


@dataclass
class StorageAPIClientCredentialsAuthInfo:
    server: str
    client_id: str
    client_secret: str

    def __repr__(self):
        return (
            f"StorageAPIClientCredentialsAuthInfo("
            f"server='***{self.server[-3:]}', "
            f"client_id='{self.client_id}', "
            f"client_secret='***')"
        )


@wrapp_public_api
class StorageAuthInfo:
    """
    Credential container for a single storage server.

    Each instance pairs a ``server_url`` with backend-specific auth data.
    Use the class-method factories below to construct instances for the
    supported backends::

        # Nucleus
        auth = StorageAuthInfo.nucleus("omniverse://myserver", "user", "pass")

        # S3 (boto3)
        auth = StorageAuthInfo.boto3("s3://my-bucket", "<key>", "<secret>")

        # GCS
        auth = StorageAuthInfo.gcs("gs://my-bucket", credentials_file="/path/to/sa.json")

        # Storage API — bearer token
        auth = StorageAuthInfo.storage_api_bearer("https://discovery.example.com", "<TOKEN>")

        # Storage API — client credentials (machine-to-machine)
        auth = StorageAuthInfo.storage_api_client_credentials(
            "https://discovery.example.com", "<CLIENT_ID>", "<CLIENT_SECRET>"
        )

    Pass one or more of these to :class:`~wrapp.ContextManager` or
    :class:`~wrapp.AuthManager` to authenticate before running commands.
    """

    @classmethod
    def nucleus(cls, server_url: str, username: str, password_or_token: str) -> "StorageAuthInfo":
        """Convenience function to set up StorageAuthInfo for a Nucleus server."""
        # Using "AuthInfo" here, so the returned object is compatible with the "AuthInfo" in WRAPP 1.1.0 and earlier.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return AuthInfo(server_url.rstrip("/"), username=username, password_or_token=password_or_token)

    @classmethod
    def boto3(
        cls, server_url: str, aws_access_key_id: str, aws_secret_access_key: str, aws_session_token: Optional[str] = None
    ) -> "StorageAuthInfo":
        """Convenience function to set up StorageAuthInfo for accessing a S3 bucket directly using boto3."""
        return StorageAuthInfo(
            server_url,
            Boto3AuthInfo(
                aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key, aws_session_token=aws_session_token
            ),
        )

    @classmethod
    def gcs(cls, server_url: str, project_id: Optional[str] = None, credentials_file: Optional[str] = None) -> "StorageAuthInfo":
        """Convenience function to set up StorageAuthInfo for accessing Google Cloud Storage."""
        return StorageAuthInfo(
            server_url,
            GCSAuthInfo(project_id=project_id, credentials_file=credentials_file),
        )

    @classmethod
    def storage_api_bearer(cls, discovery_url: str, bearer_token: str) -> "StorageAuthInfo":
        """Create credentials for a Storage API service using a pre-obtained bearer token.

        :param discovery_url: Base URL of the Storage API discovery service
            (e.g. ``https://my-discovery.example.com``). Must match the value
            of ``WRAPP_STORAGE_API_DISCOVERY_URL``.
        :param bearer_token: OAuth2 access / bearer token.
        """
        normalized = discovery_url.strip().rstrip("/")
        return StorageAuthInfo(
            normalized,
            StorageAPIBearerAuthInfo(server=normalized, bearer_token=bearer_token),
        )

    @classmethod
    def storage_api_client_credentials(cls, discovery_url: str, client_id: str, client_secret: str) -> "StorageAuthInfo":
        """Create credentials for a Storage API service using OAuth2 client credentials.

        WRAPP will exchange these for an access token via the
        ``grant_type=client_credentials`` flow and automatically refresh it
        before expiry.

        :param discovery_url: Base URL of the Storage API discovery service.
        :param client_id: OAuth2 client identifier.
        :param client_secret: OAuth2 client secret.
        """
        normalized = discovery_url.strip().rstrip("/")
        return StorageAuthInfo(
            normalized,
            StorageAPIClientCredentialsAuthInfo(server=normalized, client_id=client_id, client_secret=client_secret),
        )

    def __init__(
        self,
        server_url: str,
        auth_data: NucleusAuthInfo
        | Boto3AuthInfo
        | AzureBlobAuthInfo
        | GCSAuthInfo
        | StorageAPIBearerAuthInfo
        | StorageAPIClientCredentialsAuthInfo,
    ):
        self._server_url = server_url
        self._auth_data = auth_data

    @property
    def server_url(self) -> str:
        return self._server_url

    @property
    def auth_data(
        self,
    ) -> NucleusAuthInfo | Boto3AuthInfo | AzureBlobAuthInfo | GCSAuthInfo | StorageAPIBearerAuthInfo | StorageAPIClientCredentialsAuthInfo:
        return self._auth_data

    _server_url: str
    _auth_data: NucleusAuthInfo | Boto3AuthInfo | AzureBlobAuthInfo | GCSAuthInfo | StorageAPIBearerAuthInfo | StorageAPIClientCredentialsAuthInfo

    def __str__(self):
        server_url = getattr(self, "_server_url", None)
        auth_data = getattr(self, "_auth_data", None)
        return f"StorageAuthInfo(_server_url={server_url}, _auth_data={auth_data})"


@wrapp_public_api
class AuthInfo(StorageAuthInfo):
    """
    @deprecated Provided for backwards compatibility. Please use StorageAuthInfo instead.

    Data object to store credentials for a given base URL.
    For example,

    >>> AuthInfo(server='omniverse://localhost',username='omniverse',password_or_token='omniverse')

    will generate credentials for a workstation Nucleus with default password.
    To use a user generated API token (e.g. for single sign-on), use the special username $omni-api-token
    """

    def __init__(self, server: str, username: str, password_or_token: str):
        warnings.warn("Provided for backwards compatibility. Please use StorageAuthInfo instead.", DeprecationWarning, stacklevel=2)
        super().__init__(server, NucleusAuthInfo(server, username, password_or_token))

    @property
    def server(self) -> str:
        return self.server_url

    @server.setter
    def server(self, value: str):
        self._server_url = value

    @property
    def username(self) -> str:
        assert isinstance(self.auth_data, NucleusAuthInfo)
        return self.auth_data.username

    @username.setter
    def username(self, value: str):
        assert isinstance(self.auth_data, NucleusAuthInfo)
        self.auth_data.username = value

    @property
    def password_or_token(self) -> str:
        assert isinstance(self.auth_data, NucleusAuthInfo)
        return self.auth_data.password_or_token

    @password_or_token.setter
    def password_or_token(self, value: str):
        assert isinstance(self.auth_data, NucleusAuthInfo)
        self.auth_data.password_or_token = value

    def __repr__(self):
        return (
            f"AuthInfo("
            f"server='{self.server}', "
            f"username='***{self.username[-3:]}', "
            f"password_or_token='***{self.password_or_token[-3:]}')"
        )


class ListEntry(ABC):
    @property
    @abstractmethod
    def is_folder(self) -> bool:
        ...

    @property
    @abstractmethod
    def is_regular_file(self) -> bool:
        ...

    @property
    @abstractmethod
    def has_checkpoint(self) -> bool:
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        ...

    @property
    @abstractmethod
    def relative_path(self) -> str:
        ...

    @property
    @abstractmethod
    def nucleus_hash(self) -> Optional[str]:
        ...

    @nucleus_hash.setter
    @abstractmethod
    def nucleus_hash(self, value: str):
        """
        Set the hash value - will raise an exception if the hash is already available.
        """
        ...

    # The size of a file. Must be implemented for files
    @property
    @abstractmethod
    def size(self) -> Optional[int]:
        ...

    # The last modification time of a file. Must be implemented for files, optional for folders.
    @property
    @abstractmethod
    def modified_time(self) -> Optional[datetime]:
        ...


class ResourceIdentity(ABC):
    @abstractmethod
    async def read_data(self, storage) -> Tuple[Result, bytes]:
        ...

    @abstractmethod
    def to_string(self) -> str:
        ...


class ServerInfo(ABC):
    @abstractmethod
    def supports_create_with_hash(self, target_url: str) -> bool:
        ...

    @property
    @abstractmethod
    def auth_token(self) -> str:
        ...


class LocalReadOnlyFile(ABC):
    @abstractmethod
    def local_path(self):
        ...

    @abstractmethod
    def close(self):
        ...


class StorageInterface(ABC):
    @abstractmethod
    def supports_tags(self, url: str) -> bool:
        ...

    @abstractmethod
    def directory_semantics(self) -> DirectorySemantics:
        ...

    @abstractmethod
    def supports_comments(self) -> bool:
        ...

    @abstractmethod
    async def get_server_info(self, url: str) -> Tuple[Result, ServerInfo]:
        ...

    @abstractmethod
    async def normalize(self, url: str) -> str:
        ...

    @abstractmethod
    async def stat(self, url: str) -> Tuple[Result, Optional[ListEntry]]:
        ...

    @abstractmethod
    async def make_resource_identity(self, file_url, item: ListEntry, relative_url) -> Tuple[str, str, ResourceIdentity]:
        ...

    @abstractmethod
    async def is_folder(self, url: str) -> Tuple[Result, bool]:
        ...

    # Return all available prefixes starting with 'url' up to the next '/'.
    # Raises an StorageErrorException on any error.
    @abstractmethod
    async def list_directory(self, url: str) -> AsyncGenerator[ListEntry, None]:
        raise NotImplementedError
        # mypy thinks of this function returning a different type if the body contains a yield. Don't ask.
        # We could also remove the "async" in the function definition, but I prefer it this way.
        yield ListEntry()  # noqa

    def supports_list_file_tree(self) -> bool:
        return False

    # Return all available keys starting with 'url'.
    # Raises an StorageErrorException on any error.
    async def list_file_tree(self, url: str) -> AsyncGenerator[ListEntry, None]:
        raise NotImplementedError
        # mypy thinks of this function returning a different type if the body contains a yield. Don't ask.
        # We could also remove the "async" in the function definition, but I prefer it this way.
        yield ListEntry()  # noqa

    @abstractmethod
    async def list_checkpoints(self, url: str) -> Tuple[Result, List[ListEntry]]:
        ...

    # Any storage needs to be able to copy files within its own domain.
    # When "copy" is called, both src_url and dst_url are guaranteed to be
    # URLs supported by the storage backend. If src_url is a local file URL,
    # upload_file is called, if dst_url is a local file URL, download_file is called.
    #
    # If the source URL does not exist, return Result.ERROR_NOT_FOUND.
    # If the destination URL exists, and overwrite is True, the destination must be overwritten.
    # If the destination URL exists, and overwrite is False, return Result.ERROR_ALREADY_EXISTS.
    # If the destination URL exists, and overwrite is True, and the file cannot be overwritten, return Result.ERROR_ACCESS_DENIED.
    # Any folders needed for dst_url to exist are created.
    # If the src_url or dst_url is a folder, the operation is undefined.
    # If the URL is not supported by the storage, raise a ValueError exception.
    @abstractmethod
    async def copy(self, src_url: str, dst_url: str, overwrite: bool = False, message: str = "") -> Result:
        """Given a src and a dst url, perform a copy. Create destination folders appropriately when they don't exist yet."""
        ...

    @abstractmethod
    def supports_create_with_hash(self, target_url: str, hash: str) -> bool:
        ...

    @abstractmethod
    async def create_with_hash(self, copy_target: str, storage_hash: str, overwrite: bool) -> Result:
        """Given a src hash and a dst url, create the content at the destination url. Create destination folders appropriately when they don't exist yet."""
        ...

    @abstractmethod
    async def read_file_content(self, file_url: str) -> Tuple[Result, bytes]:
        ...

    @abstractmethod
    async def get_cached_file_async(self, file_url: str, download: bool = True) -> Tuple[Result, Optional[LocalReadOnlyFile]]:
        ...

    @abstractmethod
    async def write_file(self, file_url: str, content: bytes, comment: str) -> Result:
        """Given a dst url and some content, write the object to the destination. Create destination folders appropriately when they don't exist yet."""
        ...

    @abstractmethod
    async def delete_item(self, url: str) -> Result:
        ...

    @abstractmethod
    async def delete_tree(self, url: str, task_node: SchedulerTaskGroupNode) -> Result:
        ...

    @abstractmethod
    def can_be_cached(self, url: str) -> bool:
        ...

    @abstractmethod
    async def upload_file(self, src_url: str, dst_url: str, overwrite: bool, message: Optional[str] = None) -> Result:
        """Given a dst url and a src_url designating a local file, write the object to the destination. Create destination folders appropriately when they don't exist yet."""
        ...

    @abstractmethod
    async def download_file(self, src_url: str, dst_url: str, overwrite: bool) -> Result:
        ...

    @abstractmethod
    def set_auth_info(self, auth_info: List[StorageAuthInfo]):
        ...

    @abstractmethod
    def add_auth_info(self, auth_info: StorageAuthInfo):
        ...

    @abstractmethod
    def parse_auth_param(self, auth_param: str) -> StorageAuthInfo:
        ...

    @abstractmethod
    def get_last_connection_status(self, server_url: str) -> Optional[ConnectionStatus]:
        ...
