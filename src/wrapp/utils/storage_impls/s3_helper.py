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

from typing import (
    Optional,
    Tuple,
)


def parse_s3_bucket_key(url: str) -> Tuple[str, str, Optional[str]]:
    """Given an S3 URL in any of the s3, http, or https styles, returns Bucket, Key, and Region.

    Region might be None if not specified in the URL.

    Raises ValueError if the URL is not a valid S3 URL."""
    # Local import, so we don't accidentally use urlparse anywhere else.
    from urllib.parse import (
        unquote,
        urlparse,
    )

    parsed = urlparse(url)
    pure_path = unquote(parsed.path.lstrip("/"))
    if parsed.hostname is None:
        raise ValueError(f"No hostname in URL {url}")
    if parsed.scheme == "s3":
        return parsed.hostname, pure_path, None

    if parsed.scheme in ("http", "https") and "amazonaws.com" in parsed.hostname:
        split_host = parsed.hostname.split(".")

        path_split = pure_path.split("/", 1)
        bucket, key = path_split[0], path_split[1] if len(path_split) > 1 else ""

        if len(split_host) == 3:
            # Legacy hostname path style
            if parsed.hostname == "s3.amazonaws.com":
                return bucket, key, None
            # Dash style legacy outlier
            if split_host[0].startswith("s3"):
                region = split_host[0].split("-", 1)[-1] if "-" in split_host[0] else None
                return bucket, key, region
        elif len(split_host) == 4:
            # Deprecated path-style
            if split_host[0] == "s3" and split_host[-2:] == ["amazonaws", "com"]:
                return bucket, key, split_host[1]
            # S3-accelerate transfer endpoint URL
            if split_host[1].startswith("s3") and split_host[-2:] == ["amazonaws", "com"]:
                return split_host[0], pure_path, None
        elif len(split_host) == 5:
            # Modern virtual host style
            if split_host[1] == "s3" and split_host[-2:] == ["amazonaws", "com"]:
                return split_host[0], pure_path, split_host[2]
            # Dualstack (IP6) URL, potentially with s3-accelerate
            if split_host[1].startswith("s3") and split_host[-3:] == ["dualstack", "amazonaws", "com"]:
                return split_host[0], pure_path, None
            # Legacy hostname with virtual host style
            if split_host[2:] == ["s3", "amazonaws", "com"]:
                return split_host[0], pure_path, split_host[1]
        elif len(split_host) == 6:
            # Dualstack (IP6) with region
            if split_host[1:3] == ["s3", "dualstack"] and split_host[-2:] == ["amazonaws", "com"]:
                return split_host[0], pure_path, split_host[3]

    raise ValueError(f"Not an S3 url: {url}")
