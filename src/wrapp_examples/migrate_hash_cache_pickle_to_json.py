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

import json
import pickle
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

HASH_CACHE_FORMAT_VERSION = "1.0"


def _entry_to_json(entry: Any) -> dict[str, Any]:
    modification_time = getattr(entry, "modification_time", None)
    if isinstance(modification_time, datetime):
        modification_time_json = modification_time.isoformat()
    elif modification_time is not None:
        modification_time_json = str(modification_time)
    else:
        raise ValueError("Missing modification_time in hash cache entry")

    return {
        "modification_time": modification_time_json,
        "filesize": int(getattr(entry, "filesize")),
        "hash": str(getattr(entry, "hash")),
    }


def migrate_hash_cache(pickle_path: Path, json_path: Path) -> int:
    with pickle_path.open("rb") as f:
        old_cache = pickle.load(f)

    if not isinstance(old_cache, dict):
        raise TypeError(f"Expected dict in pickle cache, got {type(old_cache).__name__}")

    entries = {str(key): _entry_to_json(value) for key, value in old_cache.items()}
    payload = {"format_version": {"version_string": HASH_CACHE_FORMAT_VERSION}, "entries": entries}

    with json_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")

    return len(entries)


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python -m wrapp_examples.migrate_hash_cache_pickle_to_json <old-cache.pickle> <new-cache.json>")
        raise SystemExit(2)

    pickle_path = Path(sys.argv[1])
    json_path = Path(sys.argv[2])
    count = migrate_hash_cache(pickle_path, json_path)
    print(f"Migrated {count} hash cache entries from {pickle_path} to {json_path}")


if __name__ == "__main__":
    main()
