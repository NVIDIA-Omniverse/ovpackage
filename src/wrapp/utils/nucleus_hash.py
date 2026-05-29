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

import hashlib
from typing import (
    BinaryIO,
    Tuple,
)

_NUCLEUS_HASH_CHUNK_SIZE: int = 1024 * 1024
_NUCLEUS_SHA_HEADER = f"sha-256-flat;{_NUCLEUS_HASH_CHUNK_SIZE};"


def is_nucleus_hash(hash: str) -> bool:
    return hash.startswith(_NUCLEUS_SHA_HEADER)


def calc_nucleus_hash_from_stream(f: BinaryIO) -> Tuple[str, int]:
    size = 0
    hash_list = []
    while chunk := f.read(_NUCLEUS_HASH_CHUNK_SIZE):
        m = hashlib.sha256()
        m.update(chunk)
        hash_list.append(m.digest())
        size = size + len(chunk)
    n = hashlib.sha256()
    for d in hash_list:
        n.update(d)
    local_hash = _NUCLEUS_SHA_HEADER + n.hexdigest()
    return local_hash, size
