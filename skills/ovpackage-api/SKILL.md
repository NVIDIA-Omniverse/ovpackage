---
name: ovpackage-api
description: >
  Use the ovpackage Python API for programmatic access to package management.
  All command functions are async and require a ContextManager or manual
  initialize/shutdown. Read this skill before writing Python scripts or
  test scenarios that call ovpackage programmatically, or when reviewing code
  that uses the ovpackage Python API.
license: Apache-2.0  
metadata:
  author: "ovpackage maintainers"
  team: ovpackage
  tags:
    - ovpackage
    - python-api
    - asset-packaging
    - async
  languages:
    - python
  domain: asset-management
---

# ovpackage Python API Skill

The ovpackage Python API mirrors the CLI — every `ovpackage` CLI command has a
corresponding `async def` in the `ovpackage` package. All command functions are
**async** and take an optional `scheduler=` parameter for throttling and
progress reporting.

For the CLI reference, see `skills/ovpackage-cli/SKILL.md`.

## Reference Documentation

Read these files for the authoritative API details:

| Document | What it covers |
|----------|---------------|
| `docs/public/api.md` | Full Python API: ContextManager, authentication classes, command functions, data objects, scheduling, progress reporting |
| `docs/public/commands/` | Per-command Markdown pages with full parameter docs |
| `docs/public/storage.md` | Storage backend config, URL formats, environment variables, auth |
| `src/wrapp_examples/example_simple.py` | Minimal ContextManager example |
| `src/wrapp_examples/example_complex.py` | Manual initialize/shutdown with scheduling and progress |
| `src/wrapp_examples/create_new_version.py` | Real-world: list repo, calculate next version, create |

## Essential Pattern: ContextManager

This is the only pattern most callers need. `ContextManager` handles
`initialize()`, authentication, scheduler creation, and `shutdown()`
automatically.

```python
import asyncio
import ovpackage

async def main():
    async with ovpackage.ContextManager() as ctx:
        packages = await ovpackage.list_repo("s3://my-bucket", scheduler=ctx)
        for name, versions in packages:
            print(f"{name}: {versions}")

asyncio.run(main())
```

With authentication:

```python
auth = ovpackage.StorageAuthInfo.nucleus(
    server_url="omniverse://my-server",
    username="user",
    password_or_token="token",
)
async with ovpackage.ContextManager([auth]) as ctx:
    await ovpackage.create("pkg", "1.0.0", source="/data", catalog=False, scheduler=ctx)
```

Other `StorageAuthInfo` factory methods: `.storage_api_bearer(discovery_url, token)`,
`.storage_api_client_credentials(discovery_url, client_id, client_secret)`.

For the manual initialize/shutdown pattern (embedding ovpackage in a larger
application), see `src/wrapp_examples/example_complex.py` and the
"Initializing ovpackage" section of `docs/public/api.md`.

## Gotchas

1. **Everything is async** — all command functions must be `await`ed inside
   an `async def`. Use `asyncio.run()` at the top level. There is no sync
   wrapper.

2. **Always pass `scheduler=`** — without it, commands create a throwaway
   scheduler per call (slow, no throttling). Use `ContextManager` or create
   a `SchedulerContext` explicitly.

3. **`ContextManager` calls `shutdown()`** — when `standalone_mode=True`
   (the default), exiting the context manager shuts down the ovpackage runtime.
   Don't call commands after the `async with` block.

4. **`ls` takes two URL args** — `base_url` (for relative path display) and
   `url` (the actual path to list). For simple use, pass the same URL for
   both. Returns `List[LsResultItem]`.

5. **`list_repo` returns tuples** — `List[Tuple[str, List[str]]]` where each
   tuple is `(package_url, [version_strings])`.

6. **`StorageAuthInfo` not `AuthInfo`** — `AuthInfo` is deprecated. Use
   `StorageAuthInfo` with the appropriate factory method.

7. **`mirror` takes positional repo args** — unlike the CLI (`--source-repo`,
   `--destination-repo`), the Python API takes `source_repo` and
   `destination_repo` as positional parameters.
