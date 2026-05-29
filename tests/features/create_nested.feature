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

Feature: Nesting issues during create
  Creating packages which have nested subpackages requires that the structure is correct and subpackages have been created

  Background:
    Given a new test_folder
    And the user is authenticated
    And a source directory toplevel
    And a package subpackage at version 1 with file prefix ONE
    And a package subsubpackage at version 1 with file prefix TWO
    And a package subsubpackage at version 2 with file prefix TWO

Scenario: OM-117372 Creating a package with local files within a nested package
  Given a source package shelter with file prefix SHELTER in folder shelter_folder
  And subpackage version 1 is installed into source shelter at subpackage_folder

  When a file omitted.txt with content 123 is created in subpackage_folder/ONEsource_package in source shelter
  And a new version 2 of shelter is created
  And shelter version 2 is installed into source toplevel at shelter_in_toplevel

  Then the file omitted.txt exists at shelter_in_toplevel/subpackage_folder/ONEsource_package in source toplevel


Scenario: Create of a top level package from source with a subpackage missing from the repo fails
  Given a source directory root_sources
  When subpackage version 1 is installed into source root_sources at source_package
  And subpackage version 1 is removed from the repository

  Then creating a new version 1 of root_sources fails with 'Sub-package does not exist in repository: .* version .* not in'


Scenario: Create of a top level package from an installed package with an incomplete subpackage fails
  Given a source package root with file prefix root in folder root_folder
  And a source package leaf with file prefix leaf_package in folder root_folder/leaf_folder

  Then creating a new version 1 of root fails with 'Package is draft package without a catalog'


Scenario: Create of a top level package from an installed package with a subpackage missing from the repo
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  And subpackage version 1 is removed from the repository

  Then creating a new version 1 of root fails with 'Sub-package does not exist in repository: .* version .* not in'


Scenario: Create of a top level package from an installed package with a subpackage in the wrong version fails
  Given a source package root with file prefix root in folder root_folder
  When subsubpackage version 1 is installed into source subpackage at subsub
  And a new version 2 of subpackage is created
  And subpackage version 2 is installed into source root at sub_package
  Then the file .subpackage.wrapp exists at sub_package in source root

  When installing package subsubpackage version 2 into a folder at root_folder/sub_package/subsub
  Then creating a new version 1 of root fails with 'Detected dirty content:'


Scenario: Create of a top level package fails when a file in a subpackage is modified
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  Then the file ONEsource_package/ONEtestfile.txt exists at sub_package in source root
  When file root_folder/sub_package/ONEsource_package/ONEtestfile.txt is modified
  Then creating a new version 2 of root fails with 'Detected dirty content:'


Scenario: Create of a top level package fails when a file in a subpackage is deleted
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  Then the file ONEsource_package/ONEtestfile.txt exists at sub_package in source root
  # Delete a file in the subpackage
  When the file ONEsource_package/ONEtestfile.txt at sub_package in source root is deleted
  Then creating a new version 2 of root fails with 'Detected dirty content:'


Scenario: Create of a top level package fails when multiple subpackages are dirty
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package1
  And subpackage version 1 is installed into source root at sub_package2
  Then the file ONEsource_package/ONEtestfile.txt exists at sub_package1 in source root
  And the file ONEsource_package/ONEtestfile.txt exists at sub_package2 in source root
  When file root_folder/sub_package1/ONEsource_package/ONEtestfile.txt is modified
  And file root_folder/sub_package2/ONEsource_package/ONEtestfile.txt is modified
  Then creating a new version 2 of root fails with 'Detected dirty content:'


Scenario: Create of a top level package succeeds when only the top-level package is dirty
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  And a file extra_file.txt with content "top level change" is created in root_folder
  Then a new version 2 of root is created


Scenario: Create of a top level package succeeds when only the top-level package is dirty, even if the file is in the subpackage
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  And a file extra_file.txt with content "top level change" is created in root_folder/sub_package/extra_file_directory
  Then a new version 2 of root is created


Scenario: Create fails when a nested subpackage is dirty
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  And subsubpackage version 1 is installed into source subpackage at subsub
  And a new version 2 of subpackage is created
  And subpackage version 2 is installed into source root at sub_package
  # Install a different version of subsubpackage to make it dirty
  And installing package subsubpackage version 2 into a folder at root_folder/sub_package/subsub
  Then creating a new version 2 of root fails with 'Detected dirty content:'


Scenario: Create with skip_dirty_check allows creating a package even with dirty subpackages
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  Then the file ONEsource_package/ONEtestfile.txt exists at sub_package in source root
  When file root_folder/sub_package/ONEsource_package/ONEtestfile.txt is modified
  Then a new version 2 of root is created with skip_dirty_check


Scenario: Create with --allow-dirty-subpackages CLI option allows creating a package even with dirty subpackages
  Given a source package root with file prefix root in folder root_folder
  When subpackage version 1 is installed into source root at sub_package
  Then the file ONEsource_package/ONEtestfile.txt exists at sub_package in source root
  When file root_folder/sub_package/ONEsource_package/ONEtestfile.txt is modified
  Then creating package root version 3 with CLI option --allow-dirty-subpackages succeeds


Scenario: Create fails when a nested subpackage is dirty even when no top level wrapp file is present
  Given a source directory workspace
  When subpackage version 1 is installed into source workspace at sub_package
  And subsubpackage version 1 is installed into source subpackage at subsub
  And a new version 2 of subpackage is created
  And subpackage version 2 is installed into source workspace at sub_package
  # Install a different version of subsubpackage to make it dirty
  And installing package subsubpackage version 2 into a folder at workspace/source_package/sub_package/subsub
  Then creating a new version 1 of workspace fails with 'Detected upgraded dependency'
