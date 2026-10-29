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

"""Literal names and unique bytes shared by filename-transfer regression tests."""

from pathlib import Path

FILENAME_CONTENTS = {
    "sp ace.txt": b"a real space",
    "sp%20ace.txt": b"a literal percent escape",
    "sp%2520ace.txt": b"a literal double percent escape",
    "100%.txt": b"a literal percent sign",
    "uni_é.txt": b"a unicode filename",
    "My Folder/file.usd": b"a folder containing a space",
    "My%20Folder/file.usd": b"a folder containing a literal escape",
    "café/résumé.txt": b"unicode in both folder and filename",
    "x%2520y.txt": b"a doubly escaped looking literal name",
}


def write_filename_tree(root: Path) -> None:
    """Create distinct literal filenames with unique bytes for transfer checks."""
    for name, content in FILENAME_CONTENTS.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def assert_filename_tree(root: Path, *, metadata_files: tuple[str, ...] = ()) -> None:
    """Require exact asset names and bytes, allowing only declared metadata files."""
    actual = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert set(actual) == set(FILENAME_CONTENTS) | set(metadata_files)
    assert {name: actual[name] for name in FILENAME_CONTENTS} == FILENAME_CONTENTS
