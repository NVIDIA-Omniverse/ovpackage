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

from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar
from typing import Optional as _Optional
from typing import Union as _Union

from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from nvidia.omniverse.storage.fileobject.v1alpha import (
    fileobject_pb2 as _fileobject_pb2,
)

DESCRIPTOR: _descriptor.FileDescriptor

class CopyRequest(_message.Message):
    __slots__ = ("source_resource_identity", "destination_resource_address", "previous_version")
    SOURCE_RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_RESOURCE_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    PREVIOUS_VERSION_FIELD_NUMBER: _ClassVar[int]
    source_resource_identity: _fileobject_pb2.ResourceIdentity
    destination_resource_address: str
    previous_version: _fileobject_pb2.ResourceIdentity
    def __init__(self, source_resource_identity: _Optional[_Union[_fileobject_pb2.ResourceIdentity, _Mapping]] = ..., destination_resource_address: _Optional[str] = ..., previous_version: _Optional[_Union[_fileobject_pb2.ResourceIdentity, _Mapping]] = ...) -> None: ...

class CopyResponse(_message.Message):
    __slots__ = ("resource_identity",)
    RESOURCE_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    resource_identity: _fileobject_pb2.ResourceIdentity
    def __init__(self, resource_identity: _Optional[_Union[_fileobject_pb2.ResourceIdentity, _Mapping]] = ...) -> None: ...
