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
    TypeVar,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    model_validator,
)
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
        # For backwards compatibility, also handle simple integer
        if "." in self.version_string:
            major, minor = self.version_string.split(".")
            return int(major), int(minor) if minor != "dev" else -1
        else:
            return int(self.version_string), 0

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


class WRAPPFileModel(BaseModel):
    """
    Usage
    =====
    * All persisted data in WRAPP must be serialized by a class based on WRAPPFileModel (inner classes can be based on either WRAPPFileModel or BaseModel).
    * All classes based on WRAPPFileModel come with a field "format_version" of type "WRAPPModelVersion".
      The contained version string is of the form "major.minor". Don't confuse this with normal semantic versioning.
      For details see the documentation in  WRAPPModelVersion.

    How to add a completely new serialized model?
    ----------------------------------
    * Create a class based on WRAPPFileModel, define fields as required.
    * Add to the class the field "format_version" of type "WRAPPModelVersion" and give it the default value
      WRAPPModelVersion(version_string="1.dev"). The ".dev" disables the version checks for development.
    * Add the class to the list of classes in the test_file_format_compatibility test to make sure all version-related tests are run. **DO NOT FORGET THIS STEP!**
    * Develop.
    * Once you are ready to integrate your change into master, follow the instructions "Finalize a new version" below.

    How to change an existing model in a forward compatible way (preferred option)?
    -------------------------------------------------------------------------------
    * Keeping forward compatibility means by and large that the only possible
      modification is to add new fields which are independent of existing fields
      or to remove old fields if the functionality they provide is no longer required.
      Details see below.
    * Change the minor version in the "format_version" field of the model to "dev", so
      the version_string is of the form "major.dev". This disables the version checks
      for development.
    * Develop. You will get test errors if your version is no longer compatible to other
      existing versions with the same major version.
    * Once you are ready to integrate your change into master, follow
      the instructions "Finalize a new version" below.

    I need to really change my model and break forwards compatibility (avoid this if possible). How?
    -------------------------------------------------------------------------------
    * Change the "format_version" field of the model to "X.dev", with X being the
      previous major version number plus one. The ".dev" disables the version checks
      for development.
    * Add the class function "migrate_major_version" to your model, accepting an instance
      of the previous major model version as a parameter, and returning an instance
      of the current model version. There must already be a class named
      "MyClass_V<major>_<minor>" in wrapp.datastructures.previous_versions representing the
      previous version (created when the previous model version was finalized).
      See the documentation of the "migrate_major_version" function in WRAPPFileModel for more details.
    * Develop. You are basically free to change the model as you see fit, as long as you provide
      backwards compatibility. Tests will fail if you do not provide backwards compatibility.
    * Once you are ready to integrate your change into master, follow
      the instructions "Finalize a new version" below.

    How to finalize a new version?
    ------------------------------
    * Change "format_version" field of the model from "major.dev" to "major.minor".
      Obviously "minor" should be the next minor version.
    * This will cause the test_file_format_compatibility tests to fail because the new version
      has not been integrated into the forward/backward compatibility checks.
      The failing test_file_format_compatibility will write out two files into
      the folder tests/test_data/previous_versions:

      1. The schema of the new version called "class_vX.Y_schema_dev.json".
         Rename the file to  "class_vX.Y_schema.json" (replacing the existing file if there
         is one), and verify the contents of the schema.
      2. A new file with test data in the current model version called
         "class_vX.Y_dev.json".
         Rename the file to  "class_vX.Y.json" (replacing the existing file if there
         is one), and verify the contents of the file.
         If your model is entirely new and not just a new version of an existing
         model, you need to manually create a suitable test file.
    * Copy your class into wrapp.datastructures.previous_versions and add it to all_old_wrapp_file_model_classes,
      so it becomes part of the standard test suit (see the other files in the module for examples).
    * Now the test test_file_format_compatibility should pass without warnings.
    * Add further tests specific to the changed model required to ensure functionality beyond
      the simple ability to read/write the format with all applicable versions without error.

    Miscellaneous
    -------------
    * Checklist when merging an MR with a file format change (things not covered by tests):

      1. Make sure nothing is persisted which is not based on WRAPPFileModel.
      2. Make sure any new persisted root model is listed in test_file_format_compatibility.
      3. Make sure new fields do not use the value of old fields unless there is a change in the
         major revision. Otherwise, the files loaded with the new revision, modified, saved,
         then loaded with the old revision, modified, saved, and then loaded with the new
         revision again might be invalid.
    * If you change a model by accident (e.g. by changing the fields in a nested class),
      the test_file_format_compatibility tests will fail because the stored schema
      no longer matches the schema in code. If that happens realize that you are actually
      changing an existing model, and follow the instructions
      for that use case.
    * Assertion errors will remind you to do most of the steps above, a few must be done manually.
      For those, check out the checklist for merging a MR with a file format change.

    Concept
    =======

    All serialized data in WRAPP must be backwards and forwards compatible:

    When a data format is changed, there will be "older" WRAPP versions which do
    not contain that change, and "newer" WRAPP versions which do contain the change,
    as well as files using the old data format and files using the new data format.

    Backwards compatibility means that the newer WRAPP versions can read the old
    data format, and will write the new data format.
    Forwards compatibility means that the new data format is compatible to the
    old data format, so older WRAPP versions can read and write the new data format, too.

    To make this work, the all data persisted by WRAPP follows these rules:

    1. Existing fields are never changed. They should also never be removed.
       For forwards compatibility, they must be maintained by newer versions and are never removed.

        * The only exception is that existing fields can only be removed if they are no longer
          needed in the code at all, and their value can be entirely discarded. Do *not*
          read the value of an old field into a new field and remove the old field - if this
          is required, new versions written will almost certainly no longer be
          compatible with the old versions, as the old field will be preserved under its old
          name, but no longer be updated by the new versions. If this seems complicated,
          just stick to the rule to never change or remove existing fields.
    2. New fields can be added in newer versions of WRAPP. They will be kept
       unchanged by older versions reading the same data.
       All new fields must have a default to allow new code to read old versions.
       Do not calculate the value of new fields based on values in the old fields - if you do
       that, any changes to the data in the new field must still be applied to the old field
       for forwards compatibility, which is likely to cause problems.
       Because the files are written by WRAPP versions aware of the new fields
       and by WRAPP versions unaware of the new fields, any new fields must be
       completely independent of old fields and vice versa.
    3. When the read data contains an unknown field, the field is kept and written out again:
       Fields no longer in the model in code are preserved by newer WRAPP versions for older WRAPP versions,
       fields new in the model in code are preserved by older WRAPP versions for newer WRAPP versions.
       This includes fields which contain other completely unknown models.

    Unfortunately those rules will eventually need to be broken. If you find
    you need to break the rules for any class part of a WRAPPFileModel,
    the major version number of the WRAPPFileModel must be increased.
    A change of the major version number will break forwards compatibility:
    only code with a major version number greater or equal than the major
    version number in the file can read the file.
    For details follow the steps described in the usage section above.

    :meta private:
    """

    model_config = ConfigDict(extra="allow")
    """ Make sure the model keeps any new fields it does not know about. """

    _Subclass = TypeVar("_Subclass", bound="WRAPPFileModel")
    _Subclass_Previous_Version = TypeVar("_Subclass_Previous_Version", bound=BaseModel)

    @classmethod
    def verify_format_version_field(cls) -> WRAPPModelVersion:
        """
        Raise a ValueError if this model either has no or an invalid "format_version" field
        """
        if "format_version" not in cls.model_fields:
            raise ValueError(f"WRAPPFileModel {cls} needs 'format_version' field.")
        model_version = cls.model_fields["format_version"].default
        if not isinstance(model_version, WRAPPModelVersion):
            raise ValueError(f"WRAPPFileModel {cls} needs 'format_version' field of type WRAPPModelVersion with a default value.")
        return model_version

    @model_validator(mode="after")
    def check_format_version(self) -> Self:
        self.verify_format_version_field()
        return self

    @model_validator(mode="before")
    @classmethod
    def call_migrate_major_version(cls: type[_Subclass], data: Any, info: ValidationInfo) -> Any:
        # Check if major version is a match or if we need to migrate
        if not isinstance(data, dict):
            # Initialization via constructor with another object
            return data
        if "format_version" not in data:
            # Initialization via constructor with keyword arguments
            return data
        dict_format_version = data["format_version"]
        # Handle files with old version numbers
        if dict_format_version == "1" or dict_format_version == "2":
            data_format_version = WRAPPModelVersion(version_string=f"{dict_format_version}.0")
        else:
            data_format_version = WRAPPModelVersion(**dict_format_version)
        if "format_version" not in cls.model_fields:
            raise ValueError(f"WRAPPFileModel {cls} needs 'format_version' field")
        class_format_version = cls.model_fields["format_version"].default
        try:
            if data_format_version.major() == class_format_version.major():
                # Don't deserialize the version number, it would override the minor version in the class
                data.pop("format_version")
                return data
            elif data_format_version.major() < class_format_version.major():
                previous_version_class = cls._lookup_previous_version()
                if not previous_version_class:
                    raise ValueError(
                        f"Major version {data_format_version.major()} of class {cls} cannot be read, no migration available! Please double-check that {cls}.migrate_major_version(...) has been implemented, and the first parameter has the correct type annotation."
                    )
                # If the previous_version_class is for version 2, and data_format_version is version 1,
                # and class_format_version is for version 3, the next call will try to construct
                # version 2 from data for version 1, which will be re-entrant in this function with
                # cls=previous_version_class, and _Subclass the class for version 1 (assuming previous_version_class
                # has a migrate_major_version function acception version 1 as a parameter).
                previous_version_instance = previous_version_class(**data)
                instance = cls.migrate_major_version(previous_version_instance)
            else:
                raise ValueError(
                    f"cannot read version {data_format_version} of class {cls}. Only versions up to {class_format_version.major()}.X are supported."
                )
            return instance.model_dump()
        except Exception:
            raise ValueError(
                f"Failed to migrate {cls} version {class_format_version} using version {data_format_version} as input: {traceback.format_exc()}"
            )

    @classmethod
    def migrate_major_version(cls: type[_Subclass], read_data: _Subclass_Previous_Version) -> _Subclass:
        """
        This function needs to be implemented by any class inheriting from WRAPPFileModel
        with more than one major version. The function is called with the instance of the
        class matching the previous version of the data model as read from the file, and needs to create
        an instance of the class with current version of the data model.
        If a file needs to be migrated over several major versions, calls to this function
        will be chained.

        Suggested implementation pattern:

        >>> def migrate_major_version(cls, read_data: MyModel_V20) -> MyModel:
        >>>     v = read_version
        >>>     if v.major() == 1:
        >>>         # ... convert data from v1.x to v2.x ... ###
        >>>         v = WRAPPModelVersion(version_string="2.1")
        >>>     if v.major() == 2:
        >>>         # ... convert data ... ###
        >>>         v = WRAPPModelVersion(version_string="3.0")
        >>>     # If a major version is updated and this function not appropriately updated,
        >>>     # loading old data will immediately fail.
        >>>     return v

        :param read_version: version of file read from disk. The major version is guaranteed
            to be lesser than the major current file format version, otherwise the function
            is not called.
        :return: Return the version number the class was updated to. Must be
            the same as the current version number in the class - the return value
            is only used to ensure that migrate_versions has been updated
            on a version change.

        :meta private:
        """
        # This is called during pydantics validation, so raise a ValueError so it is handled properly.
        raise ValueError(f"migrate_major_version is not implemented for {cls}")

    @classmethod
    def _lookup_previous_version(cls) -> type[BaseModel] | None:
        # The look-up works as follows: if our current class can migrate from a previous version,
        # it has a function "migrate_major_version" defined, which accepts as a (type annotated) parameter
        # an instance of the previous major version. Look up the type annotation, and return the
        # class which this one can be migrated from, as well as the version this class represents.
        if cls.migrate_major_version == WRAPPFileModel.migrate_major_version:
            # migrate_major_version function was not even implemented
            return None
        sig = inspect.signature(cls.migrate_major_version)
        # Get the type annotation of the first parameter
        params = list(sig.parameters.values())
        if len(params) < 1:
            return None
        previous_version_class = params[0].annotation
        try:
            # Do not use WRAPPFileModel here - we need to stay backwards compatible to the old classes
            # based on BaseModel
            if not issubclass(previous_version_class, BaseModel):
                return None
        except TypeError:
            return None
        if "format_version" not in previous_version_class.model_fields:
            return None
        previous_version = previous_version_class.model_fields["format_version"].default
        # Handle backwards compatible classes with old version numbers
        if previous_version == "1" or previous_version == "2":
            previous_version = WRAPPModelVersion(version_string=f"{previous_version}.0")
        if not isinstance(previous_version, WRAPPModelVersion):
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
                raise FileParseException(f"File specified cannot be parsed as a JSON file: {e}") from e
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


all_old_wrapp_file_model_classes: dict[tuple[str, WRAPPModelVersion], type[BaseModel]] = {}
