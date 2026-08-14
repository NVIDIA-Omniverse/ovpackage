# NVIDIA ovpackage

`ovpackage` is a command-line tool and async Python API for reproducible asset packaging and publishing. It snapshots a directory of assets into an immutable, named **package version**, publishes that version to a **repository** on local disk, Amazon S3, Azure Blob, Google Cloud Storage, an Omniverse Storage API endpoint, installs a chosen version into a workspace, and later proves exactly what was installed — using content-hash **catalogs**, gitignore-style `.wrappignore` rules, `.wrapp` dependency metadata, and commands for diffing, mirroring, patching, freezing, and offline export/import. It is built for asset-heavy build pipelines, shared content libraries, CI/CD promotion, offline delivery, and agents that need deterministic, verifiable package operations across storage backends.

> [!NOTE]
> ovpackage is **pre-release** software and is not enterprise-supported. This project is currently not accepting contributions.

## Layout

| Path | Purpose |
|---|---|
| [`src/ovpackage/`](src/ovpackage/) | Public `ovpackage` import namespace and CLI entry point; lazily aliases onto the `wrapp` implementation |
| [`src/wrapp/`](src/wrapp/) | Core implementation: CLI app, async package/repo engine, data structures, and storage backends |
| [`src/wrapp/commands/`](src/wrapp/commands/) | One module per CLI command (create, install, mirror, export, diff, freeze, …) |
| [`src/wrapp/utils/storage_impls/`](src/wrapp/utils/storage_impls/) | Backend implementations: local, S3, Azure, GCS, and Omniverse Storage API |
| [`src/nvidia/omniverse/storage/`](src/nvidia/omniverse/storage/) | Generated gRPC/protobuf stubs for the Omniverse Storage API backend |
| [`docs/public/`](docs/public/) | User docs: manual, tutorial, CLI and per-command reference, storage and API guides |
| [`.agents/skills/`](.agents/skills/) | ovpackage specific skills |
| [`tests/`](tests/) | Pytest suite and BDD features; runnable examples of public behavior |

## Start Here

| Goal | Entry |
|---|---|
| Install and run the CLI | This README, [Install from source](#install-from-source) |
| Learn the concepts and workflows | [`docs/public/manual.md`](docs/public/manual.md), [`docs/public/tutorial.md`](docs/public/tutorial.md) |
| Look up a command | [`docs/public/commands/index.md`](docs/public/commands/index.md), or `ovpackage COMMAND --help` |
| Configure a storage backend | [`docs/public/storage.md`](docs/public/storage.md), [Storage Backends](#storage-backends) below |
| Use the Python API | [`docs/public/api.md`](docs/public/api.md), [`src/wrapp_examples/example_simple.py`](src/wrapp_examples/example_simple.py), [`src/wrapp_examples/example_complex.py`](src/wrapp_examples/example_complex.py) |
| Drive ovpackage CLI/API from an agent | [`.agents/skills/ovpackage-cli/`](.agents/skills/ovpackage-cli/), [`.agents/skills/ovpackage-api/`](.agents/skills/ovpackage-api/) |

## Install from source

```bash
python -m pip install -e .[local]
```

Optional backends are available through extras: `s3`, `azure`, `gcs`, and `storage-api`.

```bash
python -m pip install -e .[local,s3,azure,gcs,storage-api]
```

Check the installed command:

```bash
ovpackage --help
ovpackage version
```

## Core Concepts

- **Package**: A named collection of files.
- **Version**: An immutable snapshot of a package. Use a new version string for every changed snapshot.
- **Repository**: A storage location that contains a `.packages` directory with package versions.
- **Install location**: A workspace path where a package version is materialized for a project.
- **Package metadata file**: A JSON file named `.<package>.wrapp` that records package metadata and dependencies. The `.wrapp` extension is part of the package format.
- **Ignore file**: `.wrappignore` uses gitignore-style rules and is picked up by create, catalog, and freeze workflows.
- **Catalog**: A JSON description of a directory tree, including paths, sizes, and hashes.

## Local Quickstart

Create and install a package using only local files:

```bash
mkdir -p demo/source demo/repo demo/workspace
printf "hello assets\n" > demo/source/readme.txt

ovpackage create sample_assets 1.0.0 demo/source --repo demo/repo --progress-report off
ovpackage list-repo demo/repo
ovpackage install sample_assets 1.0.0 demo/workspace/sample_assets --repo demo/repo --progress-report off
```

Track installed package dependencies in a project-level metadata file:

```bash
mkdir -p demo/project
ovpackage new demo_project 1.0.0 demo/project
ovpackage install sample_assets 1.0.0 demo/project/assets \
    --repo demo/repo \
    --package demo/project/.demo_project.wrapp \
    --progress-report off
ovpackage status demo/project/.demo_project.wrapp --progress-report off
```

## Common Workflows

### Publish a new immutable version

```bash
ovpackage create textures 2026.05.11 ./textures --repo s3://asset-packages --progress-report off
ovpackage list-repo s3://asset-packages
```

### Install a specific version

```bash
ovpackage install textures 2026.05.11 ./project/assets/textures \
    --repo s3://asset-packages \
    --progress-report off
```

### Compare two directory snapshots

```bash
ovpackage catalog ./assets assets-before.json --progress-report off
ovpackage catalog ./assets assets-after.json --progress-report off
ovpackage diff assets-before.json assets-after.json --show
```

### Mirror packages between repositories

```bash
ovpackage mirror textures 2026.05.11 \
    --source-repo s3://dev-packages \
    --destination-repo s3://release-packages \
    --recursive \
    --progress-report off
```

### Export and import for offline delivery

```bash
ovpackage export textures 2026.05.11 --repo s3://release-packages --dedup --progress-report off
ovpackage import textures.2026.05.11.tar --repo ./offline-repo --progress-report off
```

## Storage Backends

Install only the extras you need:

| Backend | URL examples | Install extra |
|---------|--------------|---------------|
| Local filesystem | `./repo`, `/mnt/packages`, `file:///mnt/packages` | `local` |
| Amazon S3 and compatible endpoints | `s3://bucket/path` | `s3` |
| Azure Blob Storage | `https://account.blob.core.windows.net/container/path` | `azure` |
| Google Cloud Storage | `gs://bucket/path` | `gcs` |
| Storage API | Service-specific URLs | `storage-api` |

`omniverse://` Nucleus URLs are also supported when the required client libraries are available in the environment.

Authentication can be passed with `--auth` or environment variables. Prefer `OVPACKAGE_*` variables in new automation, for example `OVPACKAGE_REPO`, `OVPACKAGE_AUTH`, `OVPACKAGE_LOG_FILE`, and `OVPACKAGE_PROGRESS_REPORT`.

## Python API

The public import namespace is `ovpackage`:

```python
import ovpackage

print(ovpackage.__version__)
```

Most command implementations are asynchronous. A typical application initializes storage backends through `ContextManager` and then calls package operations with that scheduler. See tests such as `tests/test_api.py` and `tests/test_ovpackage_aliases.py` for executable API examples.

## Agent Usage

This repository includes an agent skill at `.agents/skills/ovpackage-cli/`. Agents that support repository-local skills can use it directly. Otherwise, copy or symlink that directory into the agent's skill search path.

The skill contains a command reference, storage/auth notes, and multi-step recipes. It is meant to compensate for the intentionally minimal initial documentation set.

## Package Format Notes

- Published package versions are intended to be immutable. Publish a new version instead of changing files under an existing package version.
- Package metadata filenames use `.<package>.wrapp`; keep that filename format.
- Ignore rules use `.wrappignore`; keep that filename format.
- Object stores do not preserve empty folders. Add placeholder files if empty folders are meaningful to your pipeline.
- Use `--progress-report off` in automation and agent runs to avoid interactive progress output.

## Tests

Install test dependencies:

```bash
python -m pip install -e .[local,test]
```

Run the default public test set:

```bash
make test
```

Run emulator-backed backend tests:

```bash
make start-test-services
make test-emulators
make stop-test-services
```

The GitHub workflow in `.github/workflows/tests.yml` runs both the default public tests and emulator-backed S3, Azure, and GCS tests.

## Contributing

This project is currently not accepting contributions.

Generated version: `0.2.1`.