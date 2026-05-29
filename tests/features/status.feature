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

Feature: wrapp status command is able to show all variants of packages being locally modified

  Background:
    Given a new test_folder
    And the user is authenticated
    And a source directory workspace
    And a package subpackage at version 1 with file prefix ONE
    And a package subpackage at version 2 with file prefix TWO
    And a package subsubpackage at version 1 with file prefix SUB

  Scenario: Modify a file in the top level package and detect that it has changed
    Given a package modified_package_name at version 1 with file prefix MOD
    When modified_package_name version 1 is installed into source workspace at install_location
    Then the file MODsource_package/MODtestfile.txt exists at install_location in source workspace
    When file workspace/source_package/install_location/MODsource_package/MODtestfile.txt is modified
    Then the output wrapp status for workspace/source_package/install_location/.modified_package_name.wrapp contains 'ITEM_DIFFERS'
    And the output wrapp status for workspace/source_package/install_location/.modified_package_name.wrapp contains 'modified_package_name'

  Scenario: Status detects a draft package without a catalog
    Given a source package draft_package with file prefix DRAFT in folder top_level
    Then the output wrapp status for top_level/.draft_package.wrapp contains 'Package is draft package without a catalog'

  Scenario: Modify a file in the subpackage and detect that it has changed
    Given a package top_level_package at version 1 with file prefix MOD
    When installing package top_level_package version 1 into a folder at top
    And dependency installing subpackage version 1 into the 'installed' location 'subfolder' of 'top_level_package'
    Then the file ONEtestfile.txt exists at top/subfolder/ONEsource_package
    When file top/subfolder/ONEsource_package/ONEtestfile.txt is modified
    Then the output wrapp status for top/.top_level_package.wrapp contains 'ITEM_DIFFERS'
    And the output wrapp status for top/.top_level_package.wrapp contains 'subpackage'
    And the output wrapp status for top/.top_level_package.wrapp contains 'top_level_package'

Scenario: Deleted file is detected
    Given a package modified_package_name at version 1 with file prefix MOD
    When modified_package_name version 1 is installed into source workspace at install_location
    Then the file MODsource_package/MODtestfile.txt exists at install_location in source workspace
    When the file MODsource_package/MODtestfile.txt at install_location in source workspace is deleted
    Then the output wrapp status for workspace/source_package/install_location/.modified_package_name.wrapp contains 'MODtestfile.txt is missing'

Scenario: Additional file is detected
  Given a package modified_package_name at version 1 with file prefix MOD
  When modified_package_name version 1 is installed into source workspace at install_location
  And a file extra_file.txt with random content is created in installed package modified_package_name
  Then the output wrapp status for workspace/source_package/install_location/.modified_package_name.wrapp contains 'extra file at extra_file.txt not referenced by any package'
  And the output wrapp status for workspace/source_package/install_location/.modified_package_name.wrapp contains 'modified_package_name'

Scenario: Missing wrapp file of a dependency is detected
  Given a package top_level_package at version 1 with file prefix TOP
  When installing package top_level_package version 1 into a folder at top
  And dependency installing subpackage version 1 into the 'installed' location 'subfolder' of 'top_level_package'
  Then the file subfolder/.subpackage.wrapp exists at top
  When the installed file .subpackage.wrapp at subfolder in top_level_package is deleted
  Then the output wrapp status for top/.top_level_package.wrapp contains 'Wrapp file referenced by dependency'
  And the output wrapp status for top/.top_level_package.wrapp contains 'top_level_package'

Scenario: Additional wrapp file is detected
  Given a package top_level_package at version 1 with file prefix TOP
  When installing package top_level_package version 1 into a folder at top
  And installing package subpackage version 1 into a folder at top
  Then the output wrapp status for top/.top_level_package.wrapp contains 'wrapp file .subpackage.wrapp not referenced by any package'
  And the output wrapp status for top/.top_level_package.wrapp contains 'top_level_package'

Scenario: Dependency version is different from installed version is detected
  Given a package top_level_package at version 1 with file prefix TOP
  When installing package top_level_package version 1 into a folder at top
  And dependency installing subpackage version 1 into the 'installed' location 'subfolder' of 'top_level_package'
  Then the file subfolder/.subpackage.wrapp exists at top
  When installing package subpackage version 2 into a folder at top/subfolder
  Then the output wrapp status for top/.top_level_package.wrapp contains 'expects version 1, but found version 2'
  And the output wrapp status for top/.top_level_package.wrapp contains 'top_level_package'

Scenario: Clean status is reported for unmodified package
  Given a package clean_package at version 1 with file prefix CLEAN
  When clean_package version 1 is installed into source workspace at clean_location
  Then the output wrapp status for workspace/source_package/clean_location/.clean_package.wrapp does not contain 'is dirty'

Scenario: Multiple issues are detected and reported
  Given a package multi_issue_package at version 1 with file prefix MULTI
  When multi_issue_package version 1 is installed into source workspace at multi_location
  Then the file MULTIsource_package/MULTItestfile.txt exists at multi_location in source workspace
  When file workspace/source_package/multi_location/MULTIsource_package/MULTItestfile.txt is modified
  And a file extra_file.txt with random content is created in installed package multi_issue_package
  And the file MULTIfile_in_folder.txt at multi_location/MULTIsource_package/MULTIsubfolder in source workspace is deleted
  Then the output wrapp status for workspace/source_package/multi_location/.multi_issue_package.wrapp contains 'ITEM_DIFFERS'
  And the output wrapp status for workspace/source_package/multi_location/.multi_issue_package.wrapp contains 'extra file at extra_file.txt not referenced by any package'
  And the output wrapp status for workspace/source_package/multi_location/.multi_issue_package.wrapp contains 'MULTIfile_in_folder.txt is missing'

Scenario: Parent package reports dirty status of subpackage
  Given a package parent_package at version 1 with file prefix PARENT
  When installing package parent_package version 1 into a folder at parent
  And dependency installing subpackage version 1 into the 'installed' location 'subfolder' of 'parent_package'
  Then the file ONEsource_package/ONEtestfile.txt exists at parent/subfolder
  When file parent/subfolder/ONEsource_package/ONEtestfile.txt is modified
  Then the output wrapp status for parent/.parent_package.wrapp contains 'subpackage'
  And the output wrapp status for parent/.parent_package.wrapp contains 'Subpackage'
  And the output wrapp status for parent/.parent_package.wrapp contains 'subpackage at subfolder is modified'
  And the output wrapp status for parent/.parent_package.wrapp contains 'parent_package'

Scenario: Status command with TSV format contains full paths
  Given a package verbose_package at version 1 with file prefix VERBOSE
  When verbose_package version 1 is installed into source workspace at verbose_location
  Then the file VERBOSEsource_package/VERBOSEtestfile.txt exists at verbose_location in source workspace
  When file workspace/source_package/verbose_location/VERBOSEsource_package/VERBOSEtestfile.txt is modified
  Then the stdout output of wrapp status with --output-format "tsv" for workspace/source_package/verbose_location/.verbose_package.wrapp contains "workspace/source_package/verbose_location/.verbose_package.wrapp"

Scenario: Status command with normal format prints output to stdout
  Given a package stdout_package at version 1 with file prefix STDOUT
  When stdout_package version 1 is installed into source workspace at stdout_location
  Then the file STDOUTsource_package/STDOUTtestfile.txt exists at stdout_location in source workspace
  When file workspace/source_package/stdout_location/STDOUTsource_package/STDOUTtestfile.txt is modified
  Then the stdout output of wrapp status with --output-format "normal" for workspace/source_package/stdout_location/.stdout_package.wrapp contains "stdout_package  1 item differs"

Scenario: Status reports packages in correct dependency order (leaf packages first)
  Given a package leaf_package at version 1 with file prefix LEAF
  And a package mid_package at version 1 with file prefix MID
  And a package top_package at version 1 with file prefix TOP
  When installing package top_package version 1 into a folder at top_level
  And installing package mid_package version 1 into a folder at top_level/mid_folder
  And installing package leaf_package version 1 into a folder at top_level/mid_folder/leaf_folder
  Then the file LEAFsource_package/LEAFtestfile.txt exists at top_level/mid_folder/leaf_folder
  When file top_level/mid_folder/leaf_folder/LEAFsource_package/LEAFtestfile.txt is modified
  And file top_level/mid_folder/MIDsource_package/MIDtestfile.txt is modified
  And file top_level/TOPsource_package/TOPtestfile.txt is modified
  Then the output wrapp status for top_level/.top_package.wrapp contains 'leaf_package'
  And the output wrapp status for top_level/.top_package.wrapp contains 'mid_package'
  And the output wrapp status for top_level/.top_package.wrapp contains 'top_package'
  And in the output of wrapp status for top_level/.top_package.wrapp, 'leaf_package' appears before 'mid_package'
  And in the output of wrapp status for top_level/.top_package.wrapp, 'mid_package' appears before 'top_package'
