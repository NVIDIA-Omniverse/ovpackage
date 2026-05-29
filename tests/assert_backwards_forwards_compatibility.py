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
import pathlib
import re
import warnings
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from deepdiff import DeepDiff
from pydantic import (
    BaseModel,
    ValidationError,
)

from wrapp.datastructures.catalog import Catalog
from wrapp.datastructures.command_context import CommandParameters
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
    all_old_wrapp_file_model_classes,
)
from wrapp.utils.utils import FailedCommand

previous_version_class_name_regex_str = r"([a-zA-Z_][a-zA-Z0-9_]*)_[vV][0-9]+_[0-9]+"
_previous_version_class_name_regex = re.compile(r"\"(#/\$defs/)?" + previous_version_class_name_regex_str + '"')


def _get_model_schema(t: type[WRAPPFileModel]) -> str:
    """
    Return the model JSON schema as it is relevant for ensuring compatibility of the files.
    """
    # Calculate the hash of the model fields as relevant for serialization.
    model_schema = t.model_json_schema(mode="validation")
    # Drop all fields we definitely don't care about when deserializing data
    keys_to_remove = {"title", "description"}

    def remove_irrelevant_entries(d: Any) -> Any:
        if not isinstance(d, dict):
            return d
        return {k: remove_irrelevant_entries(v) for k, v in d.items() if k not in keys_to_remove}

    model_schema = remove_irrelevant_entries(model_schema)
    schema_json_string = json.dumps(model_schema, sort_keys=True, indent=4)
    # Strip the version information from the old version classes, so when the classes are used as a type of a field,
    # the hash of the class for the current version matches the old version (all other things being equal).
    schema_json_string = _previous_version_class_name_regex.sub(r'"\1\2"', schema_json_string)

    return schema_json_string


async def _save_schema_dev_files(
    test_class: type[WRAPPFileModel], current_version: WRAPPModelVersion, dir_path: pathlib.Path, emit_dev_warnings: bool
) -> str:
    """
    Save the schema _dev files. Called if any of the type checking fails and we are not on CI, to make it easy to update those files.
    """
    # Save schema file if necessary.
    current_version_schema_dev_path = str(dir_path / f"{test_class.__name__}_v{current_version.version_string}_schema_dev.json")
    if not current_version.is_dev_version():
        schema_json = _get_model_schema(test_class)
        os.makedirs(str(dir_path), exist_ok=True)
        if emit_dev_warnings:
            warnings.warn(
                f"Writing a new schema file for class {test_class.__name__} version {current_version} to {current_version_schema_dev_path}. Please commit strip '_dev' and the file, otherwise the test_file_format_compatibility test will fail on CI.",
                UserWarning,
            )
        with open(current_version_schema_dev_path, "wt") as f:
            f.write(schema_json)
    return current_version_schema_dev_path


async def _save_data_dev_files(
    test_class: type[WRAPPFileModel],
    current_version: WRAPPModelVersion,
    dir_path: pathlib.Path,
    data_file_paths: dict[WRAPPModelVersion, str],
    emit_dev_warnings: bool,
    run_ci_checks: bool,
) -> str:
    """
    Save the data _dev files. Called if any of the type checking fails and we are not on CI, to make it easy to update those files.
    """
    current_version_data_path = str(dir_path / f"{test_class.__name__}_v{current_version.version_string}.json")
    current_version_data_dev_path = (
        str(dir_path / f"{test_class.__name__}_v{current_version.version_string}_dev.json")
        if not current_version.is_dev_version()
        else current_version_data_path
    )

    # So let's produce the data file by reading the
    # previous current version and writing it with a temp name. The user
    # has to rename to make it part of the set.
    source_version: WRAPPModelVersion | None = None
    for v in data_file_paths.keys():
        if not source_version or source_version.version_tuple() < v.version_tuple():
            source_version = v
    if not source_version:
        if current_version.is_dev_version():
            warnings.warn(
                f"There are no test files yet for class {test_class.__name__} - but the file version {current_version} is a dev version, so ignoring this until a proper version is used for the class.",
                UserWarning,
            )
            return ""
        if len(data_file_paths) == 1 and (current_version in data_file_paths.keys()):
            # There is only one file and it matches the current version. We assume the user wants to replace that file,
            # so we write a dev version of that file.
            source_version = current_version
        elif len(data_file_paths) > 0:
            # Odd - there are files but they are newer versions?
            pytest.fail(
                f"Cannot find the a file for the previous version of class {test_class.__name__} {current_version}, but there are other files for that class. Cannot automatically create a test file. Please create a suitable test file in {current_version_data_path}."
            )
        else:
            # A new class. Please generate a first example file for it.
            pytest.fail(
                f"There do not seem to be any files with test data for {test_class.__name__}. Cannot automatically create a test file. Please create a suitable non-trivial test file in {current_version_data_path}."
            )

    source_version_data_path = str(dir_path / f"{test_class.__name__}_v{source_version.version_string}.json")
    try:
        source_data = await test_class.load(source_version_data_path)
    except FailedCommand as e:
        if isinstance(e.__cause__, ValidationError):
            pytest.fail(
                f"Cannot read file {source_version_data_path} for class {test_class.__name__} version {current_version}: {e.__cause__}"
            )
        raise
    if not current_version.is_dev_version():
        # Write a new version of class_vX.Y_dev.json. If we would overwrite class_v.X.Y.json, it gets confusing quickly
        # because re-running the test leads to different results.
        # Writing the "_dev" version has the disadvantage that the developer must remember to rename the file,
        # but we have the check above which fails when running in CI, so that seems ok.
        if emit_dev_warnings:
            warnings.warn(
                f"Writing a new test data file for class {test_class.__name__} version {current_version} to {current_version_data_dev_path}. Please commit strip '_dev' and the file, otherwise the test_file_format_compatibility test will fail on CI.",
                UserWarning,
            )
    elif emit_dev_warnings:
        if run_ci_checks and "CI" in os.environ:
            pytest.fail(
                f"Class {test_class.__name__} is using dev version {current_version}). 'X.dev' versions should only be used by developers in local workspaces."
            )
        else:
            warnings.warn(
                f"Class {test_class.__name__} is currently using dev version {current_version}. Please make sure to change the dev version to a proper version before committing, otherwise the test_file_format_compatibility test will fail on CI.",
                UserWarning,
            )

    await source_data.save(CommandParameters(), current_version_data_dev_path)
    return current_version_data_dev_path


async def assert_backwards_forwards_compatibility(
    test_class: type[WRAPPFileModel],
    local_tmp_folder: str,
    use_tmp_data_dir: bool = False,
    emit_dev_warnings: bool = True,
    run_ci_checks: bool = True,
    old_file_model_classes: dict[tuple[str, WRAPPModelVersion], type[BaseModel]] | None = None,
):
    if old_file_model_classes is None:
        old_file_model_classes = all_old_wrapp_file_model_classes
    # Find the test file and the persisted JSON schema for this model
    current_version: WRAPPModelVersion = test_class.model_fields["format_version"].default
    # We use the normal directory for testing production classes,
    # and the tmp folder for testing this function
    if not use_tmp_data_dir:
        test_data_dir = Path(os.path.dirname(__file__)) / "test_data" / "previous_versions"
    else:
        test_data_dir = Path(local_tmp_folder) / "test_data" / "previous_versions"
    data_regex = re.compile(test_class.__name__ + r"_v([0-9]+\.[0-9]+)\.json")
    data_file_paths: dict[WRAPPModelVersion, str] = {}
    schema_regex = re.compile(test_class.__name__ + r"_v([0-9]+\.[0-9]+)_schema\.json")
    schema_file_paths: dict[WRAPPModelVersion, str] = {}
    if test_data_dir.exists():
        for file in test_data_dir.iterdir():
            if file.is_file():
                data_match = data_regex.match(file.name)
                if data_match:
                    data_file_paths[WRAPPModelVersion(version_string=f"{data_match.group(1)}")] = str(file.resolve())
                # Pre-WRAPPFileModel versions
                elif file.name == f"{test_class.__name__}_v0.json":
                    data_file_paths[WRAPPModelVersion(version_string=f"0")] = str(file.resolve())
                elif file.name == f"{test_class.__name__}_v1.json":
                    data_file_paths[WRAPPModelVersion(version_string=f"1")] = str(file.resolve())
                else:
                    schema_match = schema_regex.match(file.name)
                    if schema_match:
                        schema_file_paths[WRAPPModelVersion(version_string=f"{schema_match.group(1)}")] = str(file.resolve())

    try:
        if current_version not in schema_file_paths or current_version not in data_file_paths:
            error_msg = ""
            if current_version not in schema_file_paths:
                error_msg = f"Missing schema file '{test_class.__name__}_v{current_version.version_string}_schema.json'"
            if current_version not in data_file_paths:
                error_msg += " and missing " if error_msg else "Missing "
                error_msg += f"data file '{test_class.__name__}_v{current_version.version_string}.json'"
            error_msg += f" for class {test_class.__name__} version {current_version}! The missing file(s) will be created on your local disk for you with a '_dev' suffix when you are running the test_file_format_compatibility test, please rename and commit them."
            if run_ci_checks:
                if "CI" in os.environ:
                    pytest.fail(error_msg)
            raise ValueError(error_msg)
        current_version_schema_path = schema_file_paths[current_version]
        current_version_data_path = data_file_paths[current_version]

        # Compare the schema on disk with the schema in Python code
        python_schema_json = _get_model_schema(test_class)
        with open(current_version_schema_path, "rt") as f:
            stored_schema_json = f.read()
        # For now we compare the schemas as strings - we could of course compare the JSON data, but right now it works as it is, and
        # the user needs to compare text, too.
        if python_schema_json != stored_schema_json:
            current_version_schema_dev_path = await _save_schema_dev_files(test_class, current_version, test_data_dir, emit_dev_warnings)
            pytest.fail(
                f"Python schema for class {test_class.__name__} version {current_version} does not match schema stored in {current_version_schema_path}! Most likely you have changed the Python model but not yet updated the version number? Or Pydantic was updated? Compare {current_version_schema_path} with {current_version_schema_dev_path}"
            )

        # Validate that the test data file conforms to the schema
        with open(current_version_data_path, "rt") as f:
            test_data_json = json.load(f)
        schema_dict = json.loads(stored_schema_json)

        # Make validation strict by disallowing additional properties
        def make_strict(schema_obj):
            if isinstance(schema_obj, dict):
                if "type" in schema_obj and schema_obj["type"] == "object":
                    # Preserve typed dictionary schemas (additionalProperties as schema object),
                    # but make regular object schemas strict.
                    if not isinstance(schema_obj.get("additionalProperties"), dict):
                        schema_obj["additionalProperties"] = False
                for key, value in schema_obj.items():
                    make_strict(value)
            elif isinstance(schema_obj, list):
                for item in schema_obj:
                    make_strict(item)

        make_strict(schema_dict)

        try:
            jsonschema.validate(instance=test_data_json, schema=schema_dict)
        except jsonschema.ValidationError as e:
            pytest.fail(
                f"Test data file {current_version_data_path} does not conform to schema {current_version_schema_path}! "
                f"Schema validation error: {e.message} at path {'.'.join(str(p) for p in e.absolute_path)}"
            )
    except ValueError as e:
        # Verification failed, but we are in developer mode (otherwise the test would have already terminated with a pytest.fail)
        # Save the new schema and data file for this class/version.
        await _save_schema_dev_files(test_class, current_version, test_data_dir, emit_dev_warnings)
        current_version_data_path = await _save_data_dev_files(
            test_class, current_version, test_data_dir, data_file_paths, emit_dev_warnings, run_ci_checks
        )
        if not current_version.is_dev_version():
            pytest.fail(str(e))

    if not current_version_data_path:
        # It is a dev version and there are no data files available yet to test against.
        assert current_version.is_dev_version(), "_save_data_dev_files should only return an empty path for dev versions"
        return

    current_instance_current_file = await test_class.load(current_version_data_path)
    # Test backwards and forwards compatibility (this only checks if the files can be read/written, not
    # if the model works as expected)
    for file_version, file_path_for_version in data_file_paths.items():
        if file_version == current_version:
            # Make sure the current class has already a backup in old_file_model_classes
            if not current_version.is_dev_version():
                assert (
                    test_class.__name__,
                    current_version,
                ) in old_file_model_classes, f"Please create a backup of the current version of class {test_class.__name__} version {current_version} in the wrapp.datastructures.previous_versions module and add an entry to all_old_wrapp_file_model_classes."
            continue
        logging.info(f"Checking backwards compatibilty for {test_class.__name__} {current_version} with version {file_version}")

        # Check backwards compatibility: the current version can read files written by the old versions
        current_instance_old_file: WRAPPFileModel = await test_class.load(str(file_path_for_version))
        assert current_instance_old_file.format_version == current_version
        # Super special case: the .wrapp_preserve_empty_folder in the source_path have different paths after converting
        # from WRAPP 1.1.2 on Windows and Linux because the temp directories are in different places. The stored
        # version is the Linux version, so normalize both instances to use the same canonical path.
        def normalize_empty_folder_paths(instance):
            if hasattr(instance, "items") and isinstance(instance.items, list):
                for item in instance.items:
                    if hasattr(item, "source_path"):
                        basename = os.path.basename(item.source_path)
                        # Check if it starts with either of the empty folder marker names
                        if basename.startswith(".wrapp_keep_empty_folder") or basename.startswith(".wrapp_preserve_empty_folder"):
                            item.source_path = Catalog.source_path_for_empty_folder_files()

        normalize_empty_folder_paths(current_instance_current_file)
        normalize_empty_folder_paths(current_instance_old_file)
        if current_instance_current_file.model_dump_json() != current_instance_old_file.model_dump_json():
            with open(os.path.join(test_data_dir, "current_instance_current_file.json"), "w") as d:
                d.write(current_instance_current_file.model_dump_json(indent=4))
            with open(os.path.join(test_data_dir, "current_instance_old_file.json"), "w") as d:
                d.write(current_instance_old_file.model_dump_json(indent=4))
            assert (
                False
            ), "Migration of data file did not produce the same data file as stored, check the current_instance_current_file.json and current_instance_old_file.json"
        try:
            old_class = old_file_model_classes[(test_class.__name__, file_version)]
        except KeyError:
            pytest.fail(
                f"Compatibility test for class {test_class.__name__} failed. Found a file with version {file_version} which has no associated class! This should not have happened - try to find a Python class matching that version (git?) and register it in all_old_wrapp_file_model_classes. Or delete the file {file_path_for_version} if you are sure that version was never shipped."
            )
            raise  # Somehow PyCharm thinks pytest.fail(..) does not raise an exception in all cases?
        # If the major version is identical, the old version must be capable of reading and saving the new file, and vice versa
        if file_version.major() == current_version.major():
            logging.info(
                f"Checking forwards compatibility for {test_class.__name__} {current_version} with version {file_version} (class {old_class.__name__})"
            )
            # Read a current version file with an old version class
            old_instance_old_file: WRAPPFileModel = await old_class.load(str(file_path_for_version))
            old_instance_current_file: WRAPPFileModel = await old_class.load(current_version_data_path)
            assert old_instance_current_file.format_version == file_version
            assert old_instance_old_file.format_version == file_version

            # Check that only new fields have been added, no field has been removed or modified
            with open(current_version_data_path, "r") as f:
                current_json_data = json.load(f)
            with open(file_path_for_version, "r") as f:
                old_json_data = json.load(f)
            diff = DeepDiff(old_json_data, current_json_data, exclude_regex_paths=["'format_version'"], view="tree")
            # If this assert fires, the current model or one of the nested models it uses has changed or removed an existing field.
            # This is almost always a bad idea (see the docs of WRAPPFileModel for a more detailed explanation).
            # If the tests seem to be too strict, please add functionality in the comparison above which allows for exceptions,
            # and make sure to add further tests for those fields.
            assert all(
                [p.report_type == "dictionary_item_added" for p in diff.affected_paths]
            ), f"Version {file_version} of {test_class.__name__} cannot be read and stored losslessly by current version {current_version}: {[p for p in diff.affected_paths if p.report_type != 'dictionary_item_added']}"
            version_diff = DeepDiff(
                old_instance_current_file.model_dump(),
                current_instance_current_file.model_dump(),
                exclude_regex_paths=["'format_version'"],
            )
            assert (
                version_diff == {}
            ), f"Data read using version {file_version} of {test_class.__name__} differs from data read using current version {current_version}: {version_diff.pretty()}"
            # Save the data in the old class initialized with the new file (making sure data in the new file is preserved by the old class)
            saved_old_instance_file_path = os.path.join(local_tmp_folder, f"{test_class.__name__}_v{file_version}_v{current_version}.json")
            await old_instance_current_file.save(CommandParameters(), saved_old_instance_file_path)
            current_instance_saved_by_old_instance = await test_class.load(saved_old_instance_file_path)
            assert (
                DeepDiff(
                    current_instance_current_file.model_dump(),
                    current_instance_saved_by_old_instance.model_dump(),
                    exclude_regex_paths=["'format_version'"],
                )
                == {}
            )
            # Save the data in the new class initialized with the old file
            saved_current_instance_file_path = os.path.join(
                local_tmp_folder, f"{test_class.__name__}_v{current_version}_v{file_version}.json"
            )
            await current_instance_old_file.save(CommandParameters(), saved_current_instance_file_path)
            # Re-read the old file read and saved by the new class, make sure it is the same.
            old_instance_saved_by_current_class = await old_class.load(saved_current_instance_file_path)
            assert (
                DeepDiff(
                    old_instance_current_file.model_dump(),
                    old_instance_saved_by_current_class.model_dump(),
                    exclude_regex_paths=["'format_version'"],
                )
                == {}
            )
        else:
            # No found version should have a greater major version than our current version
            assert file_version.major() < current_version.major()
            # Make sure previous major releases still behave in a reasonable manner when trying to read the new data
            logging.info(
                f"Checking behaviour of incompatible previous version {old_class.__name__} when loading version {current_version}."
            )
            if issubclass(old_class, WRAPPFileModel):
                with pytest.raises(
                    FailedCommand,
                    match=f"Failed to load.*Failed to migrate.*version {file_version.version_string} using version {current_version.version_string} as input.*",
                ):
                    await old_class.load(current_version_data_path)
            else:
                # Pre-WRAPPFileModel classes fail with a validation error on "format_version" when reading the new files.
                with open(current_version_data_path, "r") as f:
                    old_json_data = json.load(f)
                with pytest.raises(ValidationError, match="format_version"):
                    old_class(**old_json_data)
