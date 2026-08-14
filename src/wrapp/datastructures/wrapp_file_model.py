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

import inspect
import json
import traceback
from typing import (
    Annotated,
    Any,
    TypeAlias,
    TypeVar,
    cast,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)
from pydantic_core import core_schema
from typing_extensions import Self

from wrapp.datastructures.command_context import CommandParameters
from wrapp.utils.storage import (
    wrapp_read_text_file,
    wrapp_url_exists,
    wrapp_url_is_folder,
    wrapp_write_file_content,
)
from wrapp.utils.storage_interface import Result
from wrapp.utils.utils import (
    FailedCommand,
    FileParseException,
    StorageOperationError,
)


def _version_string_tuple(version_string: str) -> tuple[int, int]:
    # For backwards compatibility, also handle simple integer
    if "." in version_string:
        major, minor = version_string.split(".")
        return int(major), int(minor) if minor != "dev" else -1
    else:
        return int(version_string), 0


class WRAPPModelVersion(BaseModel):
    """
    Version number in the form major.minor.
    WRAPP versions writing files with major version A can only read a file if "A" is equal or greater than
    the major version number of the read file (so if really necessary we break forwards compatibility,
    but never backwards compatibility).
    The minor version must change if any change is done to the model. For this purpose, a file with
    the JSON schema of the entire model is stored, and used to verify that the model
    does not change unintentionally. For development, set it to 'dev'. Check the documentation of
    "WRAPPFileModel" for further details.
    """

    version_string: Annotated[str, "Version string in the form X.Y", Field(frozen=True, pattern=r"[0-9]+(\.[0-9]+)?")]

    def major(self) -> int:
        return self.version_tuple()[0]

    def minor(self) -> int:
        return self.version_tuple()[1]

    def is_dev_version(self) -> bool:
        return self.version_tuple()[1] < 0

    def version_tuple(self) -> tuple[int, int]:
        return _version_string_tuple(self.version_string)

    def version_equal_to(self, version: tuple[int, int]) -> bool:
        return self.version_tuple() == version

    def can_read_and_write(self, version: tuple[int, int]) -> bool:
        return self.major() == version[0]

    def can_read(self, version: tuple[int, int]) -> bool:
        return self.major() >= version[0]

    def __hash__(self):
        return hash(self.version_string)

    def __repr__(self):
        return self.version_string

    def __str__(self):
        return self.version_string

    ## __eq__ is defined by BaseModel


class PkgStdModelVersion(str):
    """
    Version number in the package-standard string wire format.
    """

    def __new__(cls, version_string: str) -> "PkgStdModelVersion":
        _version_string_tuple(version_string)
        return str.__new__(cls, version_string)

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: Any, handler: Any) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls,
            core_schema.str_schema(),
            serialization=core_schema.to_string_ser_schema(),
        )

    def major(self) -> int:
        return self.version_tuple()[0]

    def minor(self) -> int:
        return self.version_tuple()[1]

    def is_dev_version(self) -> bool:
        return self.version_tuple()[1] < 0

    def version_tuple(self) -> tuple[int, int]:
        return _version_string_tuple(self)

    def version_equal_to(self, version: tuple[int, int]) -> bool:
        return self.version_tuple() == version

    def can_read_and_write(self, version: tuple[int, int]) -> bool:
        return self.major() == version[0]

    def can_read(self, version: tuple[int, int]) -> bool:
        return self.major() >= version[0]

    def __repr__(self) -> str:
        return str(self)


VersionedFileModelVersion: TypeAlias = WRAPPModelVersion | PkgStdModelVersion
OldVersionedFileModelClasses: TypeAlias = dict[tuple[str, VersionedFileModelVersion], type[BaseModel]]


class VersionedFileModelBase(BaseModel):
    """
    Base class for persisted WRAPP JSON root models.

    All subclasses have a "format_version" field and preserve unknown fields so
    older and newer WRAPP versions can read and write the same files within the
    compatibility rules. Detailed compatibility rules, version bump workflows,
    generated fixture handling, and merge checklists live in
    architecture/versioned-file-models.md.

    :meta private:
    """

    model_config = ConfigDict(extra="allow")
    """ Make sure the model keeps any new fields it does not know about. """

    _Subclass = TypeVar("_Subclass", bound="VersionedFileModelBase")
    _Subclass_Previous_Version = TypeVar("_Subclass_Previous_Version", bound=BaseModel)

    @classmethod
    def _file_model_name(cls) -> str:
        return "VersionedFileModelBase"

    @classmethod
    def _format_version_type_name(cls) -> str:
        return "supported version type"

    @classmethod
    def _is_format_version_instance(cls, format_version: Any) -> bool:
        raise NotImplementedError(f"{cls} must implement _is_format_version_instance")

    @classmethod
    def _coerce_format_version(cls, format_version: Any) -> VersionedFileModelVersion:
        raise NotImplementedError(f"{cls} must implement _coerce_format_version")

    @classmethod
    def _format_version_major(cls, format_version: VersionedFileModelVersion) -> int:
        return cls._format_version_tuple(format_version)[0]

    @classmethod
    def _format_version_tuple(cls, format_version: VersionedFileModelVersion) -> tuple[int, int]:
        return _version_string_tuple(cls._format_version_to_string(format_version))

    @classmethod
    def _format_version_to_string(cls, format_version: VersionedFileModelVersion) -> str:
        return str(format_version)

    @classmethod
    def _format_version_from_string(cls, format_version: str) -> VersionedFileModelVersion:
        raise NotImplementedError(f"{cls} must implement _format_version_from_string")

    @classmethod
    def _format_version_is_dev(cls, format_version: VersionedFileModelVersion) -> bool:
        return cls._format_version_tuple(format_version)[1] < 0

    @classmethod
    def verify_format_version_field(cls) -> VersionedFileModelVersion:
        """
        Raise a ValueError if this model either has no or an invalid "format_version" field.
        """
        if "format_version" not in cls.model_fields:
            raise ValueError(f"{cls._file_model_name()} {cls} needs 'format_version' field.")
        model_version = cls.model_fields["format_version"].default
        if not cls._is_format_version_instance(model_version):
            raise ValueError(
                f"{cls._file_model_name()} {cls} needs 'format_version' field of type {cls._format_version_type_name()} with a default value."
            )
        return model_version

    @model_validator(mode="after")
    def check_format_version(self) -> Self:
        self.verify_format_version_field()
        return self

    @model_validator(mode="before")
    @classmethod
    def call_migrate_major_version(cls: type[_Subclass], data: Any, info: ValidationInfo) -> Any:
        # Same-major files are read through normal validation. Migration starts
        # only when the incoming file's major version is older than this class.
        if not isinstance(data, dict):
            # Initialization via constructor with another object
            return data
        if "format_version" not in data:
            # Initialization via constructor with keyword arguments
            return data
        data_format_version = cls._coerce_format_version(data["format_version"])
        if "format_version" not in cls.model_fields:
            raise ValueError(f"{cls._file_model_name()} {cls} needs 'format_version' field")
        class_format_version = cls.verify_format_version_field()
        try:
            if cls._format_version_major(data_format_version) == cls._format_version_major(class_format_version):
                # Don't deserialize the version number, it would override the minor version in the class
                data.pop("format_version")
                return data
            elif cls._format_version_major(data_format_version) < cls._format_version_major(class_format_version):
                previous_version_class = cls._lookup_previous_version()
                if not previous_version_class:
                    raise ValueError(
                        f"Major version {cls._format_version_major(data_format_version)} of class {cls} cannot be read, no migration available! Please double-check that {cls}.migrate_major_version(...) has been implemented, and the first parameter has the correct type annotation."
                    )
                # The annotated previous-version class defines the next hop in
                # the migration chain. It may be a same-major minor snapshot:
                # same-major reads never call migration directly, but a chain
                # started by an older major version can pass through every
                # shipped snapshot.
                previous_version_instance = previous_version_class(**data)
                instance = cls.migrate_major_version(previous_version_instance)
            else:
                raise ValueError(
                    f"cannot read version {data_format_version} of class {cls}. Only versions up to {cls._format_version_major(class_format_version)}.X are supported."
                )
            return instance.model_dump()
        except Exception:
            raise ValueError(
                f"Failed to migrate {cls} version {class_format_version} using version {data_format_version} as input: {traceback.format_exc()}"
            )

    @classmethod
    def migrate_major_version(cls: type[_Subclass], read_data: _Subclass_Previous_Version) -> _Subclass:
        """
        Implement this on classes that can read files from an older major
        version.

        The validator calls this hook only when the incoming file's major
        version is older than the target class's major version. The type
        annotation of "read_data" selects the previous persisted model snapshot
        in the migration chain. That snapshot is often the latest class from
        the previous major version, but it can also be a same-major minor
        snapshot when the migration chain passes through every shipped format
        version.

        Same-major file reads do not call this hook. They rely on the
        minor-version compatibility rules and unknown-field preservation.

        Suggested implementation pattern:

        >>> def migrate_major_version(cls, read_data: MyModel_V20) -> MyModel:
        >>>     data_dict = read_data.model_dump()
        >>>     data_dict.pop("format_version")
        >>>     # Transform fields that cannot be handled by same-major rules.
        >>>     return MyModel(**data_dict)

        :param read_data: instance of the previous persisted model snapshot.
            The original file's major version is guaranteed to be lower than
            the target class's major version, otherwise this hook is not called.
        :return: instance of the target model class.

        :meta private:
        """
        # This is called during pydantics validation, so raise a ValueError so it is handled properly.
        raise ValueError(f"migrate_major_version is not implemented for {cls}")

    @classmethod
    def _has_custom_migrate_major_version(cls) -> bool:
        for model_class in cls.__mro__:
            if model_class is VersionedFileModelBase:
                return False
            if "migrate_major_version" in model_class.__dict__:
                return True
        return False

    @classmethod
    def _lookup_previous_version(cls) -> type[BaseModel] | None:
        # The look-up works as follows: if our current class can migrate from a previous version,
        # it has a function "migrate_major_version" defined, which accepts as a (type annotated) parameter
        # an instance of the previous persisted model snapshot. Look up the type annotation, and return
        # the class which this one can be migrated from.
        if not cls._has_custom_migrate_major_version():
            # migrate_major_version function was not implemented
            return None
        sig = inspect.signature(cls.migrate_major_version)
        # Get the type annotation of the first parameter
        params = list(sig.parameters.values())
        if len(params) < 1:
            return None
        previous_version_class = params[0].annotation
        try:
            # Do not use VersionedFileModelBase here - we need to stay backwards compatible to the old classes
            # based on BaseModel
            if not issubclass(previous_version_class, BaseModel):
                return None
        except TypeError:
            return None
        if "format_version" not in previous_version_class.model_fields:
            return None
        previous_version = previous_version_class.model_fields["format_version"].default
        try:
            cls._coerce_format_version(previous_version)
        except ValueError:
            return None
        return previous_version_class

    @classmethod
    async def load(cls: type[_Subclass], source: str) -> _Subclass:
        """Given a source path, try to load and parse the data at the given location and return the constructed object.

        This will raise an exception on failure, for expected errors exceptions will be wrapped into either FailedCommand
        or StorageOperationError exceptions. If a parse error occurs, a FileParseException is raised which is based
        on FailedCommand.
        """
        if await wrapp_url_exists(source):
            if await wrapp_url_is_folder(source):
                raise FailedCommand(f"File is a folder and not a file, cannot read: {source}")
            try:
                content = await wrapp_read_text_file(source)
                parsed_content = json.loads(content)
                return cls(**parsed_content)
            except FailedCommand:
                raise
            except StorageOperationError:
                raise
            except json.JSONDecodeError as e:
                raise FileParseException(f"File specified {source} cannot be parsed as a JSON file: {e}") from e
            except ValidationError as e:
                raise FileParseException(f"Failed to load data from {source}, validation error: {e.json()}") from e
            except Exception as e:
                raise RuntimeError(f"Unexpected error reading file: {e}") from e
        else:
            raise FailedCommand(f"Source {source} not found - please specify a valid file!")

    async def save(self, context: CommandParameters, destination: str) -> None:
        """Given command context and destination path, store the object in JSON format at the given location.

        This will raise a StorageOperationError exception in case of failure.
        """
        result = await wrapp_write_file_content(context, destination, self.model_dump_json(indent=2))
        if result != Result.OK:
            raise StorageOperationError(f"Failed to save data to {destination}: {result}")


class WRAPPFileModel(VersionedFileModelBase):
    """
    WRAPP file model specialization using WRAPPModelVersion in the existing
    object-shaped .wrapp JSON format.
    """

    @classmethod
    def _file_model_name(cls) -> str:
        return "WRAPPFileModel"

    @classmethod
    def _format_version_type_name(cls) -> str:
        return "WRAPPModelVersion"

    @classmethod
    def _is_format_version_instance(cls, format_version: Any) -> bool:
        return isinstance(format_version, WRAPPModelVersion)

    @classmethod
    def _coerce_format_version(cls, format_version: Any) -> WRAPPModelVersion:
        if isinstance(format_version, WRAPPModelVersion):
            return format_version
        # Handle files with old version numbers
        if format_version == "1" or format_version == "2":
            return WRAPPModelVersion(version_string=f"{format_version}.0")
        if isinstance(format_version, dict):
            return WRAPPModelVersion(**format_version)
        raise ValueError(f"WRAPPFileModel {cls} needs 'format_version' field of type WRAPPModelVersion")

    @classmethod
    def _format_version_from_string(cls, format_version: str) -> WRAPPModelVersion:
        return WRAPPModelVersion(version_string=format_version)

    @classmethod
    def verify_format_version_field(cls) -> WRAPPModelVersion:
        return cast(WRAPPModelVersion, super().verify_format_version_field())


class PkgStdFileModel(VersionedFileModelBase):
    """
    Package-standard file model specialization using PkgStdModelVersion in a string
    format_version in JSON.
    """

    @classmethod
    def _file_model_name(cls) -> str:
        return "PkgStdFileModel"

    @classmethod
    def _format_version_type_name(cls) -> str:
        return "PkgStdModelVersion"

    @classmethod
    def _is_format_version_instance(cls, format_version: Any) -> bool:
        return isinstance(format_version, PkgStdModelVersion)

    @classmethod
    def _coerce_format_version(cls, format_version: Any) -> PkgStdModelVersion:
        if isinstance(format_version, str):
            try:
                return PkgStdModelVersion(format_version)
            except ValueError:
                pass
        raise ValueError(f"PkgStdFileModel {cls} needs 'format_version' field of type PkgStdModelVersion")

    @classmethod
    def _format_version_from_string(cls, format_version: str) -> PkgStdModelVersion:
        return PkgStdModelVersion(format_version)

    @classmethod
    def verify_format_version_field(cls) -> PkgStdModelVersion:
        return cast(PkgStdModelVersion, super().verify_format_version_field())


all_old_file_model_classes: OldVersionedFileModelClasses = {}
