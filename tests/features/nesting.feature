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

Feature: Nesting
  Installing packages into each other requires a lot of scenarios

  Background:
    Given a new test_folder
    And the user is authenticated
    And a source directory toplevel
    And a package subpackage at version 1 with file prefix ONE
    And a package subsubpackage at version 1 with file prefix TWO


  Scenario: Installing two disjoint packages into the same location
    When subpackage version 1 is installed into source toplevel at third_party
    And subsubpackage version 1 is installed into source toplevel at third_party

    Then the file ONEsource_package/ONEtestfile.txt exists at third_party in source toplevel
    And the file .subpackage.wrapp exists at third_party in source toplevel
    And the file TWOsource_package/TWOtestfile.txt exists at third_party in source toplevel
    And the file .subsubpackage.wrapp exists at third_party in source toplevel


  Scenario: Installing two disjoint packages into the same location and uninstalling one of them leaves the first intact
    When subpackage version 1 is installed into source toplevel at third_party
    And subsubpackage version 1 is installed into source toplevel at third_party
    And subpackage is uninstalled from third_party in source package toplevel

    Then no file ONEsource_package/ONEtestfile.txt exists at third_party in source toplevel
    And no file .subpackage.wrapp exists at third_party in source toplevel
    And the file TWOsource_package/TWOtestfile.txt exists at third_party in source toplevel
    And the file .subsubpackage.wrapp exists at third_party in source toplevel


  Scenario: Installing two packages which have a file name conflict fails on second install
    Given a package conflicting at version 1 with file prefix ONE
    When subpackage version 1 is installed into source toplevel at third_party
    Then conflicting version 1 fails to install into toplevel at third_party
    And no file .conflicting.wrapp exists at third_party in source toplevel


  Scenario: Installing two packages which have a file name conflict succeeds on second install with force
    Given a package conflicting at version 1 with file prefix ONE
    And subpackage version 1 is installed into source toplevel at third_party
    When conflicting version 1 is forcefully installed into toplevel at third_party
    Then the file .conflicting.wrapp exists at third_party in source toplevel


  Scenario: Uninstalling a package via folder path
    When subpackage version 1 is installed into source toplevel at third_party
    Then the file ONEsource_package/ONEtestfile.txt exists at third_party in source toplevel
    Then the file .subpackage.wrapp exists at third_party in source toplevel

    When subpackage is uninstalled from third_party in source package toplevel
    Then no file ONEsource_package/ONEtestfile.txt exists at third_party in source toplevel
    Then no file .subpackage.wrapp exists at third_party in source toplevel


  Scenario: Uninstalling a package which has a subpackage
    When subsubpackage version 1 is installed into source subpackage at sub
    And a new version 2 of subpackage is created
    And subpackage version 2 is installed into source toplevel at third_party

    Then the file .subsubpackage.wrapp exists at third_party/sub in source toplevel
    And the file TWOsource_package/TWOtestfile.txt exists at third_party/sub in source toplevel

    When subpackage is uninstalled from third_party in source package toplevel

    Then no file .subsubpackage.wrapp exists at third_party/sub in source toplevel
    And no file TWOsource_package/TWOtestfile.txt exists at third_party/sub in source toplevel


  Scenario: Downgrading a package with a subpackage to a version without removes the dependency
    When subsubpackage version 1 is installed into source subpackage at sub
    And a new version 2 of subpackage is created
    And subpackage version 2 is installed into source toplevel at third_party

    Then the file .subsubpackage.wrapp exists at third_party/sub in source toplevel
    And the file TWOsource_package/TWOtestfile.txt exists at third_party/sub in source toplevel

    When subpackage version 1 is installed into source toplevel at third_party

    Then no file TWOsource_package/TWOtestfile.txt exists at third_party/sub in source toplevel
    And no file .subsubpackage.wrapp exists at third_party/sub in source toplevel


  Scenario: Uninstalling a package which has a subpackage within a named_toplevel_package
    Given a package named_top_level at version 1 with file prefix TOPLEVEL

    When subsubpackage version 1 is installed into source subpackage at sub
    And a new version 2 of subpackage is created
    And subpackage version 2 is installed into source named_top_level at third_party
    And a new version 2 of named_top_level is created
    And named_top_level version 2 is installed into source toplevel at named_top_dir

    Then the file .subsubpackage.wrapp exists at named_top_dir/third_party/sub in source toplevel
    And the file TWOsource_package/TWOtestfile.txt exists at named_top_dir/third_party/sub in source toplevel

    When subpackage is uninstalled from third_party in installed package named_top_level

    Then no file .subsubpackage.wrapp exists at third_party/sub in source toplevel
    And no file TWOsource_package/TWOtestfile.txt exists at third_party/sub in source toplevel


  Scenario: Exporting and Importing a package with a subpackage does not include the subpackage
    Given subsubpackage version 1 is installed into source subpackage at sub
    And a new version 2 of subpackage is created

    When subpackage version 2 is exported into a tar file
    And the subpackage version 2 tar file is imported into import_location

    Then the file .subpackage.wrapp exists at import_location/.packages/subpackage/2
    And no file .subsubpackage.wrapp exists at import_location/.packages/subpackage/2/sub/.subsubpackage.wrapp
    And no file .subsubpackage.wrapp exists at import_location/.packages/subsubpackage/1


  Scenario: Install the same package twice into the same project in two different locations is possible
    Given a source directory project
    When subpackage version 1 is installed into source toplevel at third_party/first_location
    And subpackage version 1 is installed into source toplevel at third_party/second_location

    Then subpackage version 1 is present at toplevel/source_package/third_party/first_location
    And subpackage version 1 is present at toplevel/source_package/third_party/second_location

    When a new version 2 of toplevel is created
    Then no file .subpackage.wrapp exists at .packages/toplevel/2/third_party/first_location/

    When toplevel version 2 is installed into source project at i123
    Then subpackage version 1 is present at project/source_package/i123/third_party/first_location
    And subpackage version 1 is present at project/source_package/i123/third_party/second_location


Scenario: Install the same package twice into the same project in two different locations is possible and uninstall works
    Given a source directory project
    When subpackage version 1 is installed into source toplevel at first_location
    Then the file overlapping/file_in_overlapping.txt exists at first_location in source toplevel

    When subpackage version 1 is installed into source toplevel at second_location
    Then the file overlapping/file_in_overlapping.txt exists at second_location in source toplevel

    When subpackage is uninstalled from first_location in source package toplevel
    Then no file overlapping/file_in_overlapping.txt exists at first_location in source toplevel

    When subpackage is uninstalled from second_location in source package toplevel
    Then no file overlapping/file_in_overlapping.txt exists at second_location in toplevel


Scenario: Installing a package with a dependency recorded in a subfolder should not work, this would be a cyclic dependency
  Given a source directory cyclic
  When subpackage version 1 is installed into source cyclic at folder1/folder2
  Then subsubpackage version 1 can not be installed into source cyclic at folder1 adding the dependency to the installed package subpackage


Scenario: Installing a nested package creates the correct catalog in the top-level package OMPE-40580
  Given a source package root with file prefix root in folder root_folder
  And a source directory install
  When subpackage version 1 is installed into source root at sub_package1
  And a new version 1 of root is created
  And installing package root version 1 into a folder at install
  Then the catalogs of the installed and repository package 'root' version '1' are identical


Scenario: Creating and installing a nested package from same repos OMPE-47578
  Given a source package root with file prefix root in folder root_folder
  And a source package nested_package with file prefix nested in folder nested_package
  When a new version 1 of nested_package is created
  And nested_package version 1 is installed into source root at nested_package_1
  And a new version 1 of root is created
  And installing package root version 1 into a folder at repeated_install
  Then installing package root version 1 into a folder at repeated_install has no conflicts
  And the log contains no errors


Scenario: Creating and installing a nested package from different repos OMPE-47578
  Given a source package root with file prefix root in folder root_folder
  And a source package nested_package with file prefix nested in folder nested_package
  When a new version 1 of nested_package is created
  And nested_package version 1 is mirrored to the alternate repository 'without' redirect turned on
  And the alternate version '1' of 'nested_package' is installed to folder 'root_folder/nested_package_1'
  And a new version 1 of root is created
  And installing package root version 1 into a folder at repeated_install
  Then installing package root version 1 into a folder at repeated_install has no conflicts
  And the log contains no errors
