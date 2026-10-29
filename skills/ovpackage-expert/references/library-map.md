Up: [ovpackage-expert](../SKILL.md)

# The ovpackage library at a glance

Use this to describe what ovpackage is, name the command or Python function for a task, and explain the data it reads and writes. For exact options, read [cli-reference.md](../../ovpackage-cli/references/cli-reference.md) or run `ovpackage COMMAND --help`. For the async calling pattern, read [ovpackage-api](../../ovpackage-api/SKILL.md). Sources: `src/ovpackage/__init__.py`, `src/wrapp/commands/`, `src/wrapp/datastructures/package.py`, and the [manual](../../../docs/public/manual.md).

## What it is

ovpackage is a CLI (`ovpackage`) and an async Python API (`import ovpackage`) for versioned asset packaging. It doesn't need Omniverse Kit, and it doesn't parse or rewrite file contents, including USD. It works on files and directories and records metadata beside them. The `ovpackage` namespace exposes the implementation in `src/wrapp/`; the `wrapp` command and the `.wrapp` and `.wrappignore` file names are part of the compatible on-disk format.

## Data model

| Thing | Where it lives | What it holds |
| --- | --- | --- |
| **Repository** | Any storage URL; versions live under `<repo>/.packages/<name>/<version>/` | Immutable published package versions. Names and versions are any valid folder names. |
| **Package version** | One folder in a repository | The content files plus `.<name>.wrapp` metadata. |
| **Package metadata file** (`.<name>.wrapp`, `PackageInfo`) | Next to installed or published content | `name`, `version`, `catalog`, `source` (where `create` read from), `repository` (where it was published or installed from), and `dependencies`. |
| **Dependency** | An entry in `dependencies` | `package`, `version`, `relative_destination` (install path relative to the parent), and `repository`. |
| **DependencySet** | A JSON input file | A list of `Dependency` entries, used to create a metapackage. |
| **Catalog** (`Catalog`, `CatalogItem`) | Inside `.wrapp` files, or standalone JSON from `catalog` | Paths, sizes, and content hashes of a directory tree. Ignore rules come from `.wrappignore`. |
| **Patch** (`Patch`) | JSON from `create-patch` or `install --patch` | Copy, delete, and folder operations that reconcile two catalogs. |
| **Manifest** (`Manifest`) | JSON from `build-manifest` | Storage-core manifest (wire format 1.0): relative paths with durable Storage API identities. |
| **Package-standard sidecars** | `com.nvidia.simready.packaging.json` and `.metadata/` next to `.wrapp` | Optional SimReady package identity, license, and bill of materials. Written by `create` when a license is given, either through `--create-package-standard` or a seed from `new`. Carried verbatim by `install`, `mirror`, `export`, and `import`. Not written for packages with dependencies. |

## Commands and functions

Every CLI command has an `async` counterpart in `ovpackage`. Call it inside `ovpackage.ContextManager()` and pass `scheduler=`.

| Purpose | CLI command | Python function |
| --- | --- | --- |
| Create a project metadata file | `new` | `new` |
| Publish a version | `create` | `create` |
| List packages and versions | `list-repo` | `list_repo` |
| Install, update, or roll back | `install` | `install` |
| Remove an installed package | `uninstall` | `uninstall` |
| Report local changes | `status` | `status` |
| List installed dependencies | `installed-package-list` | `installed_package_list` |
| Trace provenance | `traceback` | `traceback` |
| Pin a dependency set | `create-metapackage` | `create_metapackage` |
| Verify stored content | `check` | `check_package_content` |
| Copy between repositories | `mirror` | `mirror`, `mirror_with_dependencies` |
| Archive for offline transfer | `export` / `import` | `export_package` / `import_package` |
| Serve metadata from a CDN | `publish-redirect` | `publish_redirect` |
| Snapshot a directory | `catalog` | `catalog` |
| Read a published catalog | `extract-catalog` | `extract_catalog` |
| Compare snapshots | `diff` | `diff`, `diff_catalogs` |
| Copy a tree or catalog to a location | `freeze` | `freeze`, `freeze_catalog`, `freeze_file_tree` |
| Plan and apply reconciliation | `create-patch` / `apply-patch` | `create_patch` / `apply_patch` |
| Find unreferenced versions | `find-orphans` | `find_orphaned_package_versions` |
| Delete versions | `delete-packages` | `delete_packages_from_repo` |
| Write a storage-core manifest | `build-manifest` | `build_manifest` |
| Browse and fetch storage | `ls`, `get`, `cat` | `ls`, `get` |
| Count catalog statistics | `count` | — |
| Read or write `.wrapp` files | — | `read_package_info`, `write_package_info` |

Supporting API: `ContextManager`, `initialize` and `shutdown`, `StorageAuthInfo` and `AuthManager` for credentials, `SchedulingParameters`, `ThrottlingParameters`, and `ProgressReport` for throughput and progress, `IgnoreEvaluator` for programmatic ignore rules, and `CommandParameters` for logging and dry runs. The [API guide](../../../docs/public/api.md) documents each one.

## Exit codes

`0` success; `1` failure, or a reported difference from `status`, `diff`, and `check`; `2` usage error. Messages and logs, `wrapp.log` by default, say which.

## How to describe it in one paragraph

> ovpackage publishes a directory of assets as an immutable, named version in a repository on local disk or cloud storage, installs exact versions into projects at stable paths, and records each project's dependencies so teams can see what changed, update to a chosen version, and reproduce the same content anywhere. It ships as a CLI and an async Python API and runs without Omniverse Kit.
