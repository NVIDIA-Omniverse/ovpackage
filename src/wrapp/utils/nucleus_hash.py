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
    Dict,
    Tuple,
)

_NUCLEUS_HASH_CHUNK_SIZE: int = 1024 * 1024
_NUCLEUS_SHA_HEADER = f"sha-256-flat;{_NUCLEUS_HASH_CHUNK_SIZE};"

#: Size of the leading window the package standard hashes separately as ``sha256-first1m``.
PACKAGE_STANDARD_FIRST1M_BYTES: int = 1024 * 1024


def is_nucleus_hash(hash: str) -> bool:
    return hash.startswith(_NUCLEUS_SHA_HEADER)


class IncrementalNucleusHasher:
    """Computes the nucleus hash incrementally from a stream of byte chunks.

    Feeding the concatenated bytes of a file through update() yields exactly the
    calc_nucleus_hash_from_stream() result; chunk boundaries do not matter. Used
    to hash the bytes a transfer actually sends, so the stored hash can never go
    stale relative to the uploaded content (OMPE-74369).
    """

    def __init__(self) -> None:
        self._block = bytearray()
        self._digest_hash = hashlib.sha256()
        self._size = 0
        self._valid = True

    @property
    def size(self) -> int:
        return self._size

    @property
    def is_valid(self) -> bool:
        return self._valid

    def invalidate(self) -> None:
        """Mark that the fed bytes do not represent the full transferred stream."""
        self._valid = False

    def update(self, data: bytes) -> None:
        self._size += len(data)
        self._block.extend(data)
        while len(self._block) >= _NUCLEUS_HASH_CHUNK_SIZE:
            m = hashlib.sha256()
            m.update(bytes(self._block[:_NUCLEUS_HASH_CHUNK_SIZE]))
            self._digest_hash.update(m.digest())
            del self._block[:_NUCLEUS_HASH_CHUNK_SIZE]

    def copy_state_from(self, other: "IncrementalNucleusHasher") -> None:
        """Replace this pristine hasher's state with another hasher's state."""
        assert self._size == 0 and not self._block
        self._block = other._block
        self._digest_hash = other._digest_hash
        self._size = other._size
        self._valid = other._valid

    def finalize(self) -> Tuple[str, int]:
        if self._block:
            m = hashlib.sha256()
            m.update(bytes(self._block))
            self._digest_hash.update(m.digest())
            self._block = bytearray()
        return _NUCLEUS_SHA_HEADER + self._digest_hash.hexdigest(), self._size


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


def calc_package_standard_hashes_from_stream(f: BinaryIO) -> Tuple[Dict[str, str], int]:
    """Compute the SimReady package-standard content hashes for a stream.

    Returns a ``(hashes, size)`` pair where ``hashes`` always contains a plain
    ``sha256`` of the whole file and, for files larger than
    :data:`PACKAGE_STANDARD_FIRST1M_BYTES`, an additional ``sha256-first1m`` over
    the leading window. The stream is read in chunks so the whole file is never
    held in memory.
    """
    full_hash = hashlib.sha256()
    first1m_hash = hashlib.sha256()
    first1m_remaining = PACKAGE_STANDARD_FIRST1M_BYTES
    size = 0
    while chunk := f.read(_NUCLEUS_HASH_CHUNK_SIZE):
        full_hash.update(chunk)
        if first1m_remaining > 0:
            first1m_chunk = chunk[:first1m_remaining]
            first1m_hash.update(first1m_chunk)
            first1m_remaining -= len(first1m_chunk)
        size += len(chunk)

    hashes = {"sha256": full_hash.hexdigest()}
    if size > PACKAGE_STANDARD_FIRST1M_BYTES:
        hashes["sha256-first1m"] = first1m_hash.hexdigest()
    return hashes, size
