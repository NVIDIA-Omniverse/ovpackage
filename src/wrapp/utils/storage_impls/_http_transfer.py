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

"""Small HTTP transfer primitives shared by storage backends."""

from collections.abc import Mapping
from typing import (
    TYPE_CHECKING,
    BinaryIO,
)

if TYPE_CHECKING:
    import aiohttp
    import yarl


def encoded_http_url(url: str) -> "yarl.URL":
    """Return *url* without normalizing its already-encoded path or query.

    Presigned URLs are byte-sensitive. In particular, normalizing ``%2F`` in an
    AWS credential query parameter invalidates its signature.
    """

    import yarl

    return yarl.URL(url, encoded=True)


async def stream_http_response_to_file(
    session: "aiohttp.ClientSession",
    url: str,
    headers: Mapping[str, str],
    destination: BinaryIO,
    *,
    chunk_size: int,
) -> int:
    """GET *url* into an open binary file and return the byte count.

    Session ownership, throttling, retries, destination publication, and error
    mapping remain the responsibility of the calling backend.
    """

    downloaded_bytes = 0
    async with session.get(encoded_http_url(url), headers=headers) as response:
        response.raise_for_status()
        async for chunk in response.content.iter_chunked(chunk_size):
            destination.write(chunk)
            downloaded_bytes += len(chunk)
    return downloaded_bytes
