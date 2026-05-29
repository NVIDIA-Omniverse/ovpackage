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

Feature: Install with dirty wrapp files
  Installing nested packages allows for upgrading and downgrading, until local modifications are made which require override

  Background:
    Given a new test_folder
    And the user is authenticated

  Scenario: Updating, adding a dependency, and downgrading package keeps the added files as they are extras - TBD
    Given a source package s1 with file prefix S in folder t
    And a new version 1 of s1 is created
    And a package c at version 1 with file prefix ONE

    When c version 1 is installed into source s1 at c
    And a new version 2 of s1 is created
    Then c version 1 is present at t/c
    And s1 version 2 is present at t

    # We can downgrade to shelter version 1, but that keeps the files from c around. To remove them, we would need to compare the
    # dependencies between the two versions of shelter (1 and local)
    When source package s1 is updated to version 1
    Then the file .c.wrapp exists at t/c
    And the file ONEsource_package/ONEtestfile.txt exists at t/c


  Scenario: Updating dependencies in-place prevents downgrades as the common ancestor had no depdendency, needs override
    Given a source package s1 with file prefix S in folder t
    And a new version 1 of s1 is created
    And a package c at version 1 with file prefix ONE
    And a package c at version 2 with file prefix TWO

    When c version 1 is installed into source s1 at c
    And a new version 2 of s1 is created
    Then c version 1 is present at t/c
    And s1 version 2 is present at t

    When c version 2 is installed into source s1 at c
    And a new version 3 of s1 is created
    Then c version 2 is present at t/c
    And s1 version 3 is present at t
    And the file .c.wrapp exists at t/c

    # Now try downgrade shelter, should remove files but fails because the .c.wrapp is different in version 2 and 3, but not present in common
    # ancestor which is 1 (because create did not update version in wrapp file, the file .s1.wrapp is still reading "version 1", even if the dependency
    # points to c version 2)
    # This would be fixed by create writing the package file back into the source
    Then source package s1 fails to update to version 2
    And c version 2 is present at t/c


  Scenario: Updating and downgrading installed package removes dependent files
    Given a source package s1 with file prefix S in folder t
    And a package c at version 1 with file prefix ONE
    And a package c at version 2 with file prefix TWO

    # Build up top level package with three different dependency sets
    When a new version 1 of s1 is created
    And c version 1 is installed into source s1 at c
    And a new version 2 of s1 is created
    And c version 2 is installed into source s1 at c
    And a new version 3 of s1 is created

    # Now install version 1 somewhere
    When installing package s1 version 1 into a folder at top_level
    Then the file .s1.wrapp exists at top_level

    # Update to version 2
    When installing package s1 version 2 into a folder at top_level
    Then the file .s1.wrapp exists at top_level
    And the file .c.wrapp exists at top_level/c
    And c version 1 is present at top_level/c
    And the file ONEsource_package/ONEtestfile.txt exists at top_level/c

    # Update to version 3
    When installing package s1 version 3 into a folder at top_level
    Then c version 2 is present at top_level/c
    And no file ONEsource_package/ONEtestfile.txt exists at top_level/c
    And the file TWOsource_package/TWOtestfile.txt exists at top_level/c

    # Now downgrade, should remove files
    When installing package s1 version 2 into a folder at top_level
    Then c version 1 is present at top_level/c
    And no file TWOsource_package/TWOtestfile.txt exists at top_level/c

    When installing package s1 version 1 into a folder at top_level
    Then no file .c.wrapp exists at top_level/c
