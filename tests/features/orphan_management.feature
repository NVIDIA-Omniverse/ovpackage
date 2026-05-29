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

Feature: Orphan Package Management
  Finding and purging orphaned packages with basic scenarios

  Background:
    Given a new test_folder
    And the user is authenticated

  Scenario: Finding orphans with empty locking packages
    Given a package test_package_1 at version 1.0
    And a package test_package_2 at version 1.0
    When orphans are found using no locking packages
    Then test_package_1 version 1.0 should be identified as orphan
    And test_package_2 version 1.0 should be identified as orphan

  Scenario: No repositories to inspect should return empty orphan list
    Given a package test_package at version 1.0
    When orphans are found using test_package version 1.0 as locking package with no repositories to inspect
    Then no orphans should be identified

  Scenario: Basic orphan detection with single locking package
    Given a package locking_package at version 1.0
    And a package orphan_package at version 1.0
    When orphans are found using locking_package version 1.0 as locking package
    Then orphan_package version 1.0 should be identified as orphan
    But locking_package version 1.0 should not be identified as orphan

  Scenario: Purging orphans from repository
    Given a package locking_package at version 1.0
    And a package orphan_package at version 1.0
    When orphans are found using locking_package version 1.0 as locking package
    And the identified orphans are purged from the repository
    Then orphan_package version 1.0 should not exist in repository
    But locking_package version 1.0 should exist in repository
