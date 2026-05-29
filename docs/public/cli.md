# Command Line Interface

The `ovpackage` command line tool provides access to all ovpackage functionality for managing versioned
asset packages.

## Usage

ovpackage commands are self-documenting via the `--help` flag.

To see all available commands, run::

    ovpackage --help

To get help for a specific command::

    ovpackage create --help

## Generic Parameters

Most if not all commands support the following parameters:

* `--verbose`, `-v` Verbose output of operation.
* `--time` Measure time taken for the operation.
* `--stats` Collect and print statistics about this operation.
* `--jobs`, `-j` Set the number of parallel operations to be sent to the storage. Default is 200. Can also be set via `OVPACKAGE_JOBS` environment variable.
* `--file-transfer-jobs`, `--ftj` Set the number of parallel file transfers. Default is 50.
  * `--log-file`, `-l` Specify name for log file. Default is `ovpackage.log`. Can also be set via `OVPACKAGE_LOG_FILE` environment variable.
* `--debug`, `-d` Set logging level of client library to debug.
* `--json-logging` Do JSON structured logging instead of human readable text.

## Hash Cache

The `--hash-cache-file` (short: `--hcf`) option tells ovpackage to store and reuse
file-hash results in a local cache file. When a file's modification time and size
have not changed since the last run, ovpackage skips re-hashing it and uses the cached
value instead. This can dramatically speed up repeated operations on large file
trees regardless of the storage backend (local files, S3, Azure, GCS, etc.).

The option is available on the following commands: `catalog`, `check`,
`install`, `mirror`, and `uninstall`.

Usage::

    ovpackage install my-package 1.0 /destination --hash-cache-file hashes.cache
    ovpackage catalog omniverse://server/assets/ catalog.json --hcf hashes.cache

The path can also be set via the `OVPACKAGE_HASH_CACHE_FILE` environment variable.

If the file does not exist yet, ovpackage creates it on the first run. Subsequent
runs load the file and update it with any new or changed entries. Use
`--stats` to see cache hit/miss counts.

Hash cache files are stored as JSON with package metadata file-model metadata
(`format_version.version_string`).
Legacy pickle-based cache files are no longer accepted and fail with a clear error
message. "Legacy" means cache files produced by ovpackage versions up to and including
`2.1.0`. Starting with ovpackage `2.2.0`, hash cache files use JSON.
If you still have an old pickle cache, migrate it once or delete it.

One-time migration example:

```python
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
```
Run it like this::

    python -m wrapp_examples.migrate_hash_cache_pickle_to_json old-cache.pickle new-cache.json

## Further Information

For detailed command descriptions and Python API documentation,
see the [Commands Reference](commands/index.md).

For information on supported storage systems, URL formats, and authentication, see [Supported Storage Systems](storage.md).

For documentation on `.wrappignore` files and the `--ignore-file` parameter, see [Ignore Rules](ignore-rules.md).
