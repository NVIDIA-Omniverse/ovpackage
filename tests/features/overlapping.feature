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

Feature: Overlapping packages
  Installing multiple packages into the same folder with overlaps needs special treatment

  Background:
    Given a new test_folder
    And the user is authenticated
    And a source directory top
    And a package one at version 1 with file prefix one
    And a package two at version 1 with file prefix two

  Scenario: Installing two packages into the same location is possible and the overlapping folder is there
    When one version 1 is installed into source top at sub
    And two version 1 is installed into source top at sub

    Then the file onesource_package/onetestfile.txt exists at sub in source top
    And the file overlapping/file_in_overlapping.txt exists at sub in source top
    And the file .one.wrapp exists at sub in source top
    And the file .two.wrapp exists at sub in source top

  Scenario: Installing a package and uninstalling it does not leave empty folders
    When one version 1 is installed into source top at sub
    Then the file overlapping/file_in_overlapping.txt exists at sub in source top

    When one is uninstalled from sub in source package top
    Then no file overlapping/file_in_overlapping.txt exists at sub in source top

Scenario: Installing two packages into the same location and uninstalling one leaves the empty folder intact
    Given a source package named_top with file prefix named in folder named_top
    When one version 1 is installed into source named_top at sub
    And two version 1 is installed into source named_top at sub
    Then the file overlapping/file_in_overlapping.txt exists at sub in source named_top

    When one is uninstalled from sub in source package named_top
    Then the file overlapping/file_in_overlapping.txt exists at sub in source named_top
    When two is uninstalled from sub in source package named_top
    Then no file overlapping/file_in_overlapping.txt exists at sub in source named_top
