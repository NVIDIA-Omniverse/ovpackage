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

from collections.abc import Iterable as _Iterable
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar
from typing import Optional as _Optional
from typing import Union as _Union

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from nvidia.omniverse.storage.filefolder.v1alpha import (
    filefolder_service_pb2 as _filefolder_service_pb2,
)
from nvidia.omniverse.storage.fileobject.v1alpha import (
    fileobject_pb2 as _fileobject_pb2,
)

DESCRIPTOR: _descriptor.FileDescriptor

class GetMetadataRequest(_message.Message):
    __slots__ = ("uri", "user_metadata_keys")
    URI_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_KEYS_FIELD_NUMBER: _ClassVar[int]
    uri: str
    user_metadata_keys: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, uri: _Optional[str] = ..., user_metadata_keys: _Optional[_Iterable[str]] = ...) -> None: ...

class GetMetadataResponse(_message.Message):
    __slots__ = ("user_metadata",)

    class UserMetadataEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: UserMetadataValue
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[UserMetadataValue, _Mapping]] = ...) -> None: ...
    USER_METADATA_FIELD_NUMBER: _ClassVar[int]
    user_metadata: _containers.MessageMap[str, UserMetadataValue]
    def __init__(self, user_metadata: _Optional[_Mapping[str, UserMetadataValue]] = ...) -> None: ...

class UpdateMetadataRequest(_message.Message):
    __slots__ = ("uri", "user_metadata_key", "user_metadata", "expected_etag")
    URI_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_KEY_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ETAG_FIELD_NUMBER: _ClassVar[int]
    uri: str
    user_metadata_key: str
    user_metadata: _struct_pb2.Value
    expected_etag: str
    def __init__(
        self,
        uri: _Optional[str] = ...,
        user_metadata_key: _Optional[str] = ...,
        user_metadata: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...,
        expected_etag: _Optional[str] = ...,
    ) -> None: ...

class UpdateMetadataResponse(_message.Message):
    __slots__ = ("etag",)
    ETAG_FIELD_NUMBER: _ClassVar[int]
    etag: str
    def __init__(self, etag: _Optional[str] = ...) -> None: ...

class DeleteMetadataRequest(_message.Message):
    __slots__ = ("uri", "user_metadata_key", "expected_etag")
    URI_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_KEY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ETAG_FIELD_NUMBER: _ClassVar[int]
    uri: str
    user_metadata_key: str
    expected_etag: str
    def __init__(self, uri: _Optional[str] = ..., user_metadata_key: _Optional[str] = ..., expected_etag: _Optional[str] = ...) -> None: ...

class DeleteMetadataResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class UserMetadataValue(_message.Message):
    __slots__ = ("value", "etag")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    ETAG_FIELD_NUMBER: _ClassVar[int]
    value: _struct_pb2.Value
    etag: str
    def __init__(self, value: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ..., etag: _Optional[str] = ...) -> None: ...

class GetMetadataBatchRequest(_message.Message):
    __slots__ = ("requests",)
    REQUESTS_FIELD_NUMBER: _ClassVar[int]
    requests: _containers.RepeatedCompositeFieldContainer[GetMetadataBatchRequestEntry]
    def __init__(self, requests: _Optional[_Iterable[_Union[GetMetadataBatchRequestEntry, _Mapping]]] = ...) -> None: ...

class GetMetadataBatchRequestEntry(_message.Message):
    __slots__ = ("resource_location", "user_metadata_key", "namespace")
    RESOURCE_LOCATION_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_KEY_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    resource_location: ResourceLocation
    user_metadata_key: str
    namespace: str
    def __init__(
        self,
        resource_location: _Optional[_Union[ResourceLocation, _Mapping]] = ...,
        user_metadata_key: _Optional[str] = ...,
        namespace: _Optional[str] = ...,
    ) -> None: ...

class GetMetadataBatchResponse(_message.Message):
    __slots__ = ("results",)
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[GetMetadataBatchResponseEntry]
    def __init__(self, results: _Optional[_Iterable[_Union[GetMetadataBatchResponseEntry, _Mapping]]] = ...) -> None: ...

class GetMetadataBatchResponseEntry(_message.Message):
    __slots__ = ("success", "error", "request_index")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    REQUEST_INDEX_FIELD_NUMBER: _ClassVar[int]
    success: UserMetadataValue
    error: BatchErrorEntry
    request_index: int
    def __init__(
        self,
        success: _Optional[_Union[UserMetadataValue, _Mapping]] = ...,
        error: _Optional[_Union[BatchErrorEntry, _Mapping]] = ...,
        request_index: _Optional[int] = ...,
    ) -> None: ...

class QueryMetadataRequest(_message.Message):
    __slots__ = ("resource_location", "namespace")
    RESOURCE_LOCATION_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    resource_location: ResourceLocation
    namespace: str
    def __init__(self, resource_location: _Optional[_Union[ResourceLocation, _Mapping]] = ..., namespace: _Optional[str] = ...) -> None: ...

class QueryMetadataResponse(_message.Message):
    __slots__ = ("results",)
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[QueryMetadataResponseEntry]
    def __init__(self, results: _Optional[_Iterable[_Union[QueryMetadataResponseEntry, _Mapping]]] = ...) -> None: ...

class QueryMetadataResponseEntry(_message.Message):
    __slots__ = ("user_metadata_key", "user_metadata")
    USER_METADATA_KEY_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_FIELD_NUMBER: _ClassVar[int]
    user_metadata_key: str
    user_metadata: UserMetadataValue
    def __init__(
        self, user_metadata_key: _Optional[str] = ..., user_metadata: _Optional[_Union[UserMetadataValue, _Mapping]] = ...
    ) -> None: ...

class UpdateMetadataBatchRequest(_message.Message):
    __slots__ = ("requests",)
    REQUESTS_FIELD_NUMBER: _ClassVar[int]
    requests: _containers.RepeatedCompositeFieldContainer[UpdateMetadataBatchRequestEntry]
    def __init__(self, requests: _Optional[_Iterable[_Union[UpdateMetadataBatchRequestEntry, _Mapping]]] = ...) -> None: ...

class UpdateMetadataBatchRequestEntry(_message.Message):
    __slots__ = ("resource_location", "user_metadata_key", "user_metadata", "expected_etag", "namespace")
    RESOURCE_LOCATION_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_KEY_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ETAG_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    resource_location: ResourceLocation
    user_metadata_key: str
    user_metadata: _struct_pb2.Value
    expected_etag: str
    namespace: str
    def __init__(
        self,
        resource_location: _Optional[_Union[ResourceLocation, _Mapping]] = ...,
        user_metadata_key: _Optional[str] = ...,
        user_metadata: _Optional[_Union[_struct_pb2.Value, _Mapping]] = ...,
        expected_etag: _Optional[str] = ...,
        namespace: _Optional[str] = ...,
    ) -> None: ...

class UpdateMetadataBatchResponse(_message.Message):
    __slots__ = ("results",)
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[UpdateMetadataBatchResponseEntry]
    def __init__(self, results: _Optional[_Iterable[_Union[UpdateMetadataBatchResponseEntry, _Mapping]]] = ...) -> None: ...

class UpdateMetadataBatchResponseEntry(_message.Message):
    __slots__ = ("success", "error", "request_index")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    REQUEST_INDEX_FIELD_NUMBER: _ClassVar[int]
    success: UpdateMetadataBatchResponseEntrySuccess
    error: BatchErrorEntry
    request_index: int
    def __init__(
        self,
        success: _Optional[_Union[UpdateMetadataBatchResponseEntrySuccess, _Mapping]] = ...,
        error: _Optional[_Union[BatchErrorEntry, _Mapping]] = ...,
        request_index: _Optional[int] = ...,
    ) -> None: ...

class UpdateMetadataBatchResponseEntrySuccess(_message.Message):
    __slots__ = ("etag",)
    ETAG_FIELD_NUMBER: _ClassVar[int]
    etag: str
    def __init__(self, etag: _Optional[str] = ...) -> None: ...

class DeleteMetadataBatchRequest(_message.Message):
    __slots__ = ("requests",)
    REQUESTS_FIELD_NUMBER: _ClassVar[int]
    requests: _containers.RepeatedCompositeFieldContainer[DeleteMetadataBatchRequestEntry]
    def __init__(self, requests: _Optional[_Iterable[_Union[DeleteMetadataBatchRequestEntry, _Mapping]]] = ...) -> None: ...

class DeleteMetadataBatchRequestEntry(_message.Message):
    __slots__ = ("resource_location", "user_metadata_key", "expected_etag", "namespace")
    RESOURCE_LOCATION_FIELD_NUMBER: _ClassVar[int]
    USER_METADATA_KEY_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_ETAG_FIELD_NUMBER: _ClassVar[int]
    NAMESPACE_FIELD_NUMBER: _ClassVar[int]
    resource_location: ResourceLocation
    user_metadata_key: str
    expected_etag: str
    namespace: str
    def __init__(
        self,
        resource_location: _Optional[_Union[ResourceLocation, _Mapping]] = ...,
        user_metadata_key: _Optional[str] = ...,
        expected_etag: _Optional[str] = ...,
        namespace: _Optional[str] = ...,
    ) -> None: ...

class DeleteMetadataBatchResponse(_message.Message):
    __slots__ = ("results",)
    RESULTS_FIELD_NUMBER: _ClassVar[int]
    results: _containers.RepeatedCompositeFieldContainer[DeleteMetadataBatchResponseEntry]
    def __init__(self, results: _Optional[_Iterable[_Union[DeleteMetadataBatchResponseEntry, _Mapping]]] = ...) -> None: ...

class DeleteMetadataBatchResponseEntry(_message.Message):
    __slots__ = ("success", "error", "request_index")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ERROR_FIELD_NUMBER: _ClassVar[int]
    REQUEST_INDEX_FIELD_NUMBER: _ClassVar[int]
    success: DeleteMetadataBatchResponseEntrySuccess
    error: BatchErrorEntry
    request_index: int
    def __init__(
        self,
        success: _Optional[_Union[DeleteMetadataBatchResponseEntrySuccess, _Mapping]] = ...,
        error: _Optional[_Union[BatchErrorEntry, _Mapping]] = ...,
        request_index: _Optional[int] = ...,
    ) -> None: ...

class DeleteMetadataBatchResponseEntrySuccess(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ResourceLocation(_message.Message):
    __slots__ = ("resource_address", "resource_identity", "folder_address")
    RESOURCE_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    FOLDER_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    resource_address: str
    resource_identity: _fileobject_pb2.ResourceIdentity
    folder_address: _filefolder_service_pb2.FolderAddress
    def __init__(
        self,
        resource_address: _Optional[str] = ...,
        resource_identity: _Optional[_Union[_fileobject_pb2.ResourceIdentity, _Mapping]] = ...,
        folder_address: _Optional[_Union[_filefolder_service_pb2.FolderAddress, _Mapping]] = ...,
    ) -> None: ...

class BatchErrorEntry(_message.Message):
    __slots__ = ("code", "message")

    class Code(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
        __slots__ = ()
        CODE_UNSPECIFIED: _ClassVar[BatchErrorEntry.Code]
        CODE_INVALID_ARGUMENT: _ClassVar[BatchErrorEntry.Code]
        CODE_NOT_FOUND: _ClassVar[BatchErrorEntry.Code]
        CODE_PERMISSION_DENIED: _ClassVar[BatchErrorEntry.Code]
        CODE_FAILED_PRECONDITION: _ClassVar[BatchErrorEntry.Code]
        CODE_INTERNAL: _ClassVar[BatchErrorEntry.Code]
        CODE_RESOURCE_EXHAUSTED: _ClassVar[BatchErrorEntry.Code]
        CODE_UNAVAILABLE: _ClassVar[BatchErrorEntry.Code]
    CODE_UNSPECIFIED: BatchErrorEntry.Code
    CODE_INVALID_ARGUMENT: BatchErrorEntry.Code
    CODE_NOT_FOUND: BatchErrorEntry.Code
    CODE_PERMISSION_DENIED: BatchErrorEntry.Code
    CODE_FAILED_PRECONDITION: BatchErrorEntry.Code
    CODE_INTERNAL: BatchErrorEntry.Code
    CODE_RESOURCE_EXHAUSTED: BatchErrorEntry.Code
    CODE_UNAVAILABLE: BatchErrorEntry.Code
    CODE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    code: BatchErrorEntry.Code
    message: str
    def __init__(self, code: _Optional[_Union[BatchErrorEntry.Code, str]] = ..., message: _Optional[str] = ...) -> None: ...
