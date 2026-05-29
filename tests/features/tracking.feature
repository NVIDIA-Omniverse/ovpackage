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

Feature: wrapp tracks the source of a repository across all operations

  Background:
    Given a new test_folder
    And the user is authenticated
    And a package subpackage at version 1.0

  Scenario: A wrapp package created by new does not have a source pointer and no repository pointer
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'new_without_files' 'without' catalog
    Then inspecting the 'source' package 'new_without_files' with version 'none' provides no source URL
    And inspecting the 'source' package 'new_without_files' with version 'none' provides no repository URL

  Scenario: A wrapp package created by new with catalog does not have a source pointer and no repository pointer
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'new_without_files' 'with' catalog
    Then inspecting the 'source' package 'new_without_files' with version 'none' provides no source URL
    And inspecting the 'source' package 'new_without_files' with version 'none' provides no repository URL

  Scenario: A wrapp package created in the repo remembers where it came from and points to its own repository
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'new_without_files' 'without' catalog
    And a new version 1.0 of new_without_files is created
    Then inspecting the 'created' package 'new_without_files' with version '1.0' provides the link to the 'standard' repository
    And inspecting the 'created' package 'new_without_files' with version '1.0' points to 'source' as 'workspace'

Scenario: A wrapp package without source wrapp file created in the repo remembers where it came from and points to its own repository
    Given a package new_without_files at version DRAFT with file prefix s
    And a new version 1.0 of new_without_files is created
    Then inspecting the 'created' package 'new_without_files' with version '1.0' provides the link to the 'standard' repository
    And inspecting the 'created' package 'new_without_files' with version '1.0' points to 'source' as 'new_without_files'

  Scenario: A wrapp package installed from the repository still remembers from where it was installed
    Given a package to_be_installed at version 1.0
    When installing package to_be_installed version 1.0 into a folder at install_test
    Then inspecting the 'installed' package 'to_be_installed' with version '1.0' provides the link to the 'standard' repository

  Scenario: Recreating a new version from an installed package points to the install folder as source, and no longer to the original source
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'to_be_installed' 'without' catalog
    And a new version 1.0 of to_be_installed is created
    And installing package to_be_installed version 1.0 into a folder at install_test
    And making a new version '2.0' of 'installed' package 'to_be_installed'
    Then inspecting the 'created' package 'to_be_installed' with version '2.0' provides the link to the 'standard' repository
    And inspecting the 'created' package 'to_be_installed' with version '2.0' points to 'installed' as 'to_be_installed'

  Scenario: Mirror keeps the repository, and the source
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'to_be_mirrored' 'without' catalog
    And a new version 1.0 of to_be_mirrored is created
    And to_be_mirrored version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    Then inspecting the 'alternate' package 'to_be_mirrored' with version '1.0' provides the link to the 'standard' repository
    And inspecting the 'alternate' package 'to_be_mirrored' with version '1.0' points to 'source' as 'workspace'

  Scenario: Installing from a mirrored package points to the alternate repository, but keeps the source
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'to_be_mirrored' 'without' catalog
    And a new version 1.0 of to_be_mirrored is created
    And to_be_mirrored version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    And the alternate version '1.0' of 'to_be_mirrored' is installed to folder 'mirrored_install'
    Then inspecting the 'installed' package 'to_be_mirrored' with version '1.0' provides the link to the 'alternate' repository
    And inspecting the 'installed' package 'to_be_mirrored' with version '1.0' points to 'source' as 'workspace'

  Scenario: Export/Import keeps the repository, and the source
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'to_be_exported' 'without' catalog
    And a new version 1.0 of to_be_exported is created
    And to_be_exported version 1.0 is exported into a tar file
    And the to_be_exported version 1.0 tar file is imported into import_repo
    Then inspecting the 'alternate' package 'to_be_exported' with version '1.0' provides the link to the 'standard' repository
    And inspecting the 'alternate' package 'to_be_exported' with version '1.0' points to 'source' as 'workspace'

  Scenario: Installing a mirrored package with subpackage has the subpackage pointing to the original repository if not mirrored with update
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'to_be_mirrored' 'without' catalog
    And subpackage version 1.0 is installed into source workspace at subfolder
    And a new version 1.0 of to_be_mirrored is created
    And to_be_mirrored version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    And subpackage version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    And the alternate version '1.0' of 'to_be_mirrored' is installed to folder 'mirrored_install'
    Then inspecting the 'installed' package 'to_be_mirrored' with version '1.0' provides the link to the 'alternate' repository
    And inspecting the 'installed' package 'to_be_mirrored' with version '1.0' points to 'source' as 'workspace'
    And inspecting the 'bypath|mirrored_install' package 'subpackage' with version '1.0' points to 'source' as 'subpackage'
    And inspecting the 'bypath|mirrored_install' package 'subpackage' with version '1.0' provides the link to the 'standard' repository


  Scenario: Installing a mirrored package with redirected subpackage has the subpackage pointing to the alternate repository, but source is the same
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'to_be_mirrored' 'without' catalog
    And subpackage version 1.0 is installed into source workspace at subfolder
    And a new version 1.0 of to_be_mirrored is created
    And to_be_mirrored version 1.0 is mirrored to the alternate repository 'with' redirect turned on
    And subpackage version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    And subpackage version 1.0 is removed from the repository
    And the alternate version '1.0' of 'to_be_mirrored' is installed to folder 'mirrored_install'
    Then inspecting the 'installed' package 'to_be_mirrored' with version '1.0' provides the link to the 'alternate' repository
    And inspecting the 'installed' package 'to_be_mirrored' with version '1.0' points to 'source' as 'workspace'
    And inspecting the 'bypath|mirrored_install' package 'subpackage' with version '1.0' points to 'source' as 'subpackage'
    And inspecting the 'bypath|mirrored_install' package 'subpackage' with version '1.0' provides the link to the 'alternate' repository


  Scenario: Installing a mirrored package without redirected subpackage will fail if the subpackage is deleted from the source repo
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'mirrored_but_subpackage_gone' 'without' catalog
    And subpackage version 1.0 is installed into source workspace at subfolder
    And a new version 1.0 of mirrored_but_subpackage_gone is created
    And mirrored_but_subpackage_gone version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    And subpackage version 1.0 is mirrored to the alternate repository 'without' redirect turned on
    And subpackage version 1.0 is removed from the repository
    Then installing the alternate version 1.0 of mirrored_but_subpackage_gone to folder wont_work fails with 'cannot load package'


  Scenario: Installing a mirrored package with redirected subpackage will fail if the subpackage is not mirrored as well
    Given a source directory workspace
    When a new command is run in directory 'workspace' for package 'mirrored_but_subpackage_not_mirrored' 'without' catalog
    And subpackage version 1.0 is installed into source workspace at subfolder
    And a new version 1.0 of mirrored_but_subpackage_not_mirrored is created
    And mirrored_but_subpackage_not_mirrored version 1.0 is mirrored to the alternate repository 'with' redirect turned on
    Then installing the alternate version 1.0 of mirrored_but_subpackage_not_mirrored to folder wont_work fails with 'cannot load package'
