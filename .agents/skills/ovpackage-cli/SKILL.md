---
name: ovpackage-cli
description: >
  Run ovpackage CLI commands
  to create, install, update, mirror, export, import, freeze, catalog, diff, check, and manage
  versioned asset packages across S3, Azure, GCS, Nucleus, and local storage.
  Use when the user asks about ovpackage, asset packaging, package versioning, .wrapp files,
  asset publishing, asset mirroring, or reproducible builds.
license: Apache-2.0  
metadata:
  author: "ovpackage maintainers"
  team: ovpackage
  tags:
    - ovpackage
    - asset-packaging
    - versioning
    - publishing
    - mirroring
    - s3
    - azure
    - nucleus
    - omniverse
    - reproducible-builds
  languages:
    - python
    - bash
  domain: asset-management
---

# ovpackage Skill

You are an expert at using ovpackage, a CLI tool for reproducible asset packaging and publishing.
Execute `ovpackage` commands via the shell and read reference docs from this skill's `references/` directory when needed.

## Finding ovpackage

Before running any ovpackage command, locate the binary:

1. Check if `ovpackage` is on PATH: `which ovpackage`
2. If not found, check for a local venv: `ls ./venv/bin/ovpackage`
3. If found in venv, use the full path: `./venv/bin/ovpackage`
4. Verify the version: `ovpackage version`

`ovpackage version` prints the version and lists available storage backends. Storage API is an optional service-specific backend that most users don't have. When it's enabled (default) but no discovery URL or gRPC address is configured, the version line will read **"ENABLED but not configured"** -- **this is harmless** and does NOT affect S3, Azure, Nucleus, or local file operations. Example output:
```
you are running ovpackage version '2.2.0'
S3 Storage Backend
GCS Storage Backend
StorageAPI: ENABLED but not configured: set OVPACKAGE_STORAGE_API_DISCOVERY_URL or OVPACKAGE_STORAGE_API_GRPC_ADDRESS   ← ignore this
```

If ovpackage is not installed, tell the user to install it from this repository with `python -m pip install .[local]`, or from the published package when one is available.

## Always Use These Flags

Add to every ovpackage command you run:

- `--progress-report off` -- suppresses interactive progress bars that pollute output
- `--log-file ovpackage.log` -- keeps log output in a predictable location

## Core Concepts

**Repository**: A `.packages` directory containing named packages with versioned subdirectories.
```
/.packages/vegetation_pack/20230505/
/.packages/vegetation_pack/20230512/
/.packages/rocks_pack/v1.0.0/
```

**Package**: A named collection of files stored as immutable versions in a repository.

**Version**: An arbitrary string (date, semver, etc.) identifying a snapshot. Once published, versions are immutable.

**package metadata file**: A `.<name>.wrapp` JSON file that records package metadata, version, source, and dependencies. Created by `install --package` or `new`.

**Catalog**: A JSON snapshot of a directory tree (files, sizes, hashes). Used for diff, freeze, create-patch.

**Subpackages**: Packages installed inside other packages. Dependencies are tracked in package metadata files.

## Command Quick Reference

### Package Lifecycle
| Command | Usage |
|---------|-------|
| `create` | `ovpackage create NAME VERSION SOURCE [--repo URL]` |
| `install` | `ovpackage install NAME VERSION DEST [--repo URL] [--package FILE]` |
| `uninstall` | `ovpackage uninstall NAME INSTALL_LOCATION [--package FILE]` |
| `new` | `ovpackage new NAME VERSION DEST` |
| `list-repo` | `ovpackage list-repo REPOSITORY_URL` |
| `status` | `ovpackage status PACKAGE_FILE` |
| `installed-package-list` | `ovpackage installed-package-list PACKAGE_FILE` |

### Distribution
| Command | Usage |
|---------|-------|
| `mirror` | `ovpackage mirror NAME VERSION --source-repo URL --destination-repo URL` |
| `publish-redirect` | `ovpackage publish-redirect --source-repo URL --redirect-repo URL [--external-url URL]` |
| `export` | `ovpackage export NAME VERSION [--repo URL] [--dedup]` |
| `import` | `ovpackage import TARFILE --repo URL` |

### Catalog & Comparison
| Command | Usage |
|---------|-------|
| `catalog` | `ovpackage catalog SOURCE_DIR CATALOG_FILE` |
| `diff` | `ovpackage diff CATALOG_A CATALOG_B --show` |
| `extract-catalog` | `ovpackage extract-catalog NAME VERSION CATALOG --repo URL` |
| `count` | `ovpackage count CATALOG_FILE` |
| `freeze` | `ovpackage freeze SOURCE DEST_DIR [--catalog]` |

### Conflict Resolution
| Command | Usage |
|---------|-------|
| `create-patch` | `ovpackage create-patch CATALOG_A CATALOG_B BASE_CATALOG -o PATCH` |
| `apply-patch` | `ovpackage apply-patch PATCH` |

### Inspection & Maintenance
| Command | Usage |
|---------|-------|
| `check` | `ovpackage check NAME VERSION --repo URL [-R]` |
| `traceback` | `ovpackage traceback PACKAGE_FILE` |
| `find-orphans` | `ovpackage find-orphans -p SEED_PACKAGE_FILE --repo URL` |
| `delete-packages` | `ovpackage delete-packages PACKAGE_URL... [--dry-run]` |
| `create-metapackage` | `ovpackage create-metapackage NAME VERSION --repo URL` |

### Utilities
| Command | Usage |
|---------|-------|
| `build-manifest` | `ovpackage build-manifest PACKAGE_FILE OUTPUT [-w] [--recalculate-identities]` |
| `ls` | `ovpackage ls URL [-R] [--pattern "*.usd"]` |
| `get` | `ovpackage get URL [-o LOCAL_FILE]` |
| `cat` | `ovpackage cat URL` |
| `version` | `ovpackage version` |

## Common Workflows

### Publish a package
```bash
ovpackage create my_package 1.0.0 /path/to/source --progress-report off
ovpackage list-repo /path/to  # verify
```

### Install a package into a project
```bash
ovpackage new my_project 1.0.0 /path/to/project
ovpackage install vegetation_pack 20230505 /path/to/project/vegetation \
    --package /path/to/project/.my_project.wrapp --progress-report off
```

### Check for modifications before updating
```bash
ovpackage status /path/to/project/.my_project.wrapp --progress-report off
# If clean:
ovpackage install vegetation_pack 20230512 /path/to/project/vegetation \
    --package /path/to/project/.my_project.wrapp --progress-report off
```

### Handle install conflicts
```bash
ovpackage install vegetation_pack 20230512 /dest \
    --patch /tmp/conflicts.patch --progress-report off
# Review /tmp/conflicts.patch, then:
ovpackage apply-patch /tmp/conflicts.patch --progress-report off
```

### Mirror between environments
```bash
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com \
    --recursive --progress-report off
```

### Export for offline delivery
```bash
ovpackage export vegetation_pack 20230505 --repo omniverse://dev.example.com \
    --dedup --progress-report off
# Transfer the tar file, then at destination:
ovpackage import vegetation_pack.20230505.tar --repo omniverse://client.com \
    --progress-report off
```

### Track changes over time
```bash
ovpackage catalog /path/to/assets snapshot_jan.json --progress-report off
# Later:
ovpackage catalog /path/to/assets snapshot_feb.json --progress-report off
ovpackage diff snapshot_jan.json snapshot_feb.json --show
```

### Verify package integrity
```bash
ovpackage check my_package 1.0.0 --repo omniverse://server -R --progress-report off
```

### Clean up unused packages
```bash
ovpackage find-orphans -p /project/.project.wrapp --repo omniverse://server \
    --output orphans.txt -w --progress-report off
ovpackage delete-packages --package-list orphans.txt --dry-run  # preview first
ovpackage delete-packages --package-list orphans.txt --progress-report off
```

### Trace package provenance
```bash
ovpackage traceback /path/to/project/.my_project.wrapp
```

## Warnings and Gotchas

1. **Immutability**: Published package versions must never be modified. ovpackage assumes repository content is read-only after creation. Modifying files in `.packages/` will corrupt integrity checks.

2. **Empty folders**: Object storage (S3, Azure) cannot store empty folders. Add `.keep` placeholder files if you need them. After uninstall on filesystem/Nucleus, empty folders may remain.

3. **Conflict on install**: If the destination has local modifications, `install` fails. Use `--patch` to capture conflicts, then `apply-patch` to resolve. Or use `--force` to overwrite (data loss risk).

4. **Concurrency**: Running an ovpackage write operation concurrently with any other ovpackage operation on the same data leads to undefined behavior. Serialize write operations.

5. **No `--interactive-auth` in automation**: The `-I` flag opens a browser and blocks. Always use `--auth` or `OVPACKAGE_AUTH` in scripts and CI/CD.

6. **S3 catalogs need `--local-hash`**: S3 and Azure don't have server-side checksums like Nucleus. Use `--local-hash` with `catalog` to download files and compute hashes locally.

7. **`delete-packages` is destructive**: Deleting a package breaks all packages that depend on it. Always use `--dry-run` first.

8. **Auth escaping on Linux**: When using `$omni-api-token` in `--auth`, escape the `$` with backslash to prevent shell expansion: `\$omni-api-token`.

9. **Default repository location**: Without `--repo`, ovpackage looks for `.packages` at the root of the same server/path. Specify `--repo` explicitly for non-default locations.

10. **`.wrappignore`**: Works like `.gitignore`. Automatically picked up from the source directory during `catalog`, `create`, and `freeze`. Override with `--ignore-file`.

11. **Storage API is NOT Nucleus**: ovpackage has six backends -- S3, Azure, GCS (`gs://`), Nucleus (`omniverse://`), local filesystem, and Storage API. Storage API is a separate optional service-specific gRPC service; it is **not** Nucleus. The Storage API backend is enabled by default but stays inert until either `OVPACKAGE_STORAGE_API_DISCOVERY_URL` or `OVPACKAGE_STORAGE_API_GRPC_ADDRESS` is set; until then the `version` command reports "ENABLED but not configured" -- this is harmless. Nucleus servers (`omniverse://` URLs) work independently and have nothing to do with Storage API.

## Reporting Issues

When an ovpackage command crashes, produces wrong output, or corrupts data:

1. Re-run the failing command with `-v` for extra detail, or `--debug` for maximum diagnostics
2. Collect the log file (`ovpackage.log`, or whatever `--log-file` was set to)
3. Summarize the issue:
   - ovpackage version (`ovpackage version`)
   - Exact command that was run
   - Expected vs actual behavior
   - OS and platform
4. Ask the user to open a GitHub issue with the summary, command output, and log file

If the skill gave wrong guidance, ask the user to open a GitHub issue with the relevant prompt,
the command the agent tried, and what went wrong.

## Reference Files

For detailed information, read these files from the skill's references directory:

- **`references/cli-reference.md`** -- Complete syntax for all 26 commands with every option and default. Read when you need exact flag names, argument order, or option details.

- **`references/storage-and-auth.md`** -- URL formats for each backend, auth methods, `OVPACKAGE_AUTH` format, storage semantics, performance tuning env vars. Read when dealing with auth errors, URL formats, or S3/Azure configuration.

- **`references/pipeline-recipes.md`** -- Step-by-step multi-command recipes for 10 common workflows. Read when the user needs a complete workflow or you need to chain multiple commands.
