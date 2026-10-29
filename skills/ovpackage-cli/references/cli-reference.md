# ovpackage CLI Reference

Complete command reference for ovpackage 2.2.0. Generated from `ovpackage <command> --help`.

## Common Command Options

These options are common, not guaranteed global. Supported options vary by command and installed version, and Typer requires command options after the subcommand. Treat `ovpackage <command> --help` as authoritative before adding any option to an automation command.

| Option | Short | Description | Default |
|--------|-------|-------------|---------|
| `--auth` | | Auth triplet: `URL,user,pass` (Nucleus) or `URL,access_key,secret_key` (S3). Repeatable. | env `OVPACKAGE_AUTH` |
| `--interactive-auth` | `-I` | Fall back to interactive browser auth for Nucleus | off |
| `--jobs` | `-j` | Parallel storage operations | `200` (env `OVPACKAGE_JOBS`) |
| `--file-transfer-jobs` | | Parallel file transfers | `50` |
| `--progress-report` | | `off\|normal\|verbose` | `normal` (env `OVPACKAGE_PROGRESS_REPORT`) |
| `--verbose` | `-v` | Verbose output | off |
| `--log-file` | `-l` | Log file path | `ovpackage.log` (env `OVPACKAGE_LOG_FILE`) |
| `--debug` | `-d` | Debug logging for client library | off |
| `--json-logging` | | JSON structured logging | off |
| `--stats` | | Collect and print operation statistics | off |
| `--time` | | Measure and print elapsed time | off |
| `--help` | `-h` | Show help and exit | |

---

## Package Lifecycle

### create

Catalog a source tree and store it as a versioned package in a repository.

```
ovpackage create [PACKAGE_NAME] [VERSION] [SOURCE]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes* | Package name |
| `VERSION` | yes* | Version string |
| `SOURCE` | yes* | Source URL or catalog file |

*Can alternatively use `--package` to specify a `.wrapp` file.

| Option | Short | Description |
|--------|-------|-------------|
| `--package` | `-p` | URL to a `.wrapp` file (alternative to positional args) |
| `--new-version` | | Override version from package metadata file |
| `--catalog` | | Indicate SOURCE is a catalog file, not a directory |
| `--remote, --repo` | `-r` | Repository URL (env `OVPACKAGE_REPO`) |
| `--ignore-file` | `-i` | Path to `.wrappignore`-style file (env `OVPACKAGE_IGNORE_FILE`) |
| `--check-subpackages` | | Verify subpackages exist in target repo |
| `--allow-dirty-subpackages` | `-a` | Allow creating even if subpackages are modified |
| `--tags` | `-t` | Include tagging service |
| `--force-upload` | | Force upload, skip hash-based dedup |

### install

Install a package version from a repository into a destination directory.

```
ovpackage install PACKAGE_NAME VERSION DESTINATION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes | Package name |
| `VERSION` | yes | Version string |
| `DESTINATION` | yes | Target URL/path to install into |

| Option | Short | Description |
|--------|-------|-------------|
| `--remote, --repo` | `-r` | Repository URL (default: `/.packages` on same server) |
| `--package` | `-p` | `.wrapp` file to record the dependency in |
| `--patch, --output` | `-o` | Write conflict patch file instead of failing |
| `--force` | `-f` | Overwrite locally modified files |
| `--dry-run` | | Test without copying |
| `--ignore-file` | `-i` | Path to ignore rules file |
| `--tags` | `-t` | Include tagging service |

### uninstall

Remove an installed package from a target location.

```
ovpackage uninstall PACKAGE_NAME INSTALL_LOCATION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes | Package name to uninstall |
| `INSTALL_LOCATION` | yes | Directory where package was installed |

| Option | Short | Description |
|--------|-------|-------------|
| `--package` | `-p` | `.wrapp` file to remove dependency record from |
| `--force` | `-f` | Ignore local modifications, delete anyway |
| `--ignore-file` | `-i` | Path to ignore rules file |
| `--tags` | `-t` | Check tags before uninstall |
| `--ignore-tags` | | Skip tag checking |

### new

Create a new `.wrapp` package file for recording dependencies.

```
ovpackage new PACKAGE_NAME VERSION DESTINATION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes | Package name |
| `VERSION` | yes | Initial version string |
| `DESTINATION` | yes | Target URL where to create the package metadata file |

| Option | Description |
|--------|-------------|
| `--create-catalog` | Build initial catalog at destination |
| `--local-hash` | Download files to calculate hash locally |
| `--ignore-file` | Path to ignore rules file |
| `--tags` | Include tagging service |

### list-repo

List all packages and versions in a repository.

```
ovpackage list-repo REPOSITORY
```

| Argument | Required | Description |
|----------|----------|-------------|
| `REPOSITORY` | yes | Repository URL (without `.packages` suffix) |

No command-specific options beyond globals.

### status

Check the status of installed packages in a workspace.

```
ovpackage status PACKAGE_FILE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_FILE` | yes | Path to top-level `.wrapp` file |

| Option | Description |
|--------|-------------|
| `--output-format` | `tsv\|normal\|short` (default: `normal`) |
| `--ignore-file` | Path to ignore rules file |

### installed-package-list

List all packages (recursively) within a top-level package.

```
ovpackage installed-package-list PACKAGE_FILE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_FILE` | yes | Path to top-level `.wrapp` file |

| Option | Description |
|--------|-------------|
| `--output-format` | `tsv\|normal\|short` (default: `normal`) |
| `--direct-only` | Report direct dependencies only |

---

## Catalog & Comparison

### catalog

Create a JSON catalog (snapshot) of a directory tree.

```
ovpackage catalog SOURCE_DIR CATALOG_FILE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SOURCE_DIR` | yes | Source URL to catalog |
| `CATALOG_FILE` | yes | Output JSON file (overwritten) |

| Option | Short | Description |
|--------|-------|-------------|
| `--file-list` | | Indicate SOURCE_DIR is a file list, not a directory |
| `--root` | | Root directory for file list entries |
| `--overwrite` | `-w` | Allow overwriting existing catalog |
| `--checkpoints` | | Use Nucleus checkpoints (default: on). Use `--no-checkpoints` for HEAD revision |
| `--local-hash, --lh` | | Download files to compute hash locally (needed for S3/Azure) |
| `--hash-cache-file` | | Local file to cache hash results (env `OVPACKAGE_HASH_CACHE_FILE`) |
| `--ignore-file` | `-i` | Path to ignore rules file |
| `--tags` | `-t` | Include tagging service |

### diff

Compare two catalog files and report differences.

```
ovpackage diff CATALOG_A CATALOG_B
```

| Argument | Required | Description |
|----------|----------|-------------|
| `CATALOG_A` | yes | First catalog file |
| `CATALOG_B` | yes | Second catalog file |

| Option | Description |
|--------|-------------|
| `--show` | Print all differences (default: just exit code 0/1) |
| `--show-unique` | Print files unique to one catalog |
| `--ignore-tags` | Ignore tags during comparison |

### extract-catalog

Extract the catalog from a published package version.

```
ovpackage extract-catalog PACKAGE VERSION CATALOG
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE` | yes | Package name |
| `VERSION` | yes | Version string |
| `CATALOG` | yes | Output catalog file |

| Option | Short | Description |
|--------|-------|-------------|
| `--remote, --repo` | `-r` | Repository URL (**required**) |
| `--recursive` | `-R` | Include subpackage catalogs |
| `--overwrite` | `-w` | Allow overwriting existing file |

### count

Print file/folder statistics from a catalog.

```
ovpackage count CATALOG_FILE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `CATALOG_FILE` | yes | Catalog file to evaluate |

No command-specific options beyond globals.

---

## Conflict Resolution

### create-patch

Create a patch file to merge changes between two catalogs relative to a common ancestor.

```
ovpackage create-patch CATALOG_A CATALOG_B BASE_CATALOG
```

| Argument | Required | Description |
|----------|----------|-------------|
| `CATALOG_A` | yes | Current state catalog |
| `CATALOG_B` | yes | Desired state catalog |
| `BASE_CATALOG` | yes | Last common ancestor catalog |

| Option | Short | Description |
|--------|-------|-------------|
| `--patch, --output` | `-o` | Output patch file |
| `--overwrite` | `-w` | Allow overwriting existing patch |
| `--ignore` | | Write patch despite conflicts, leaving conflicted files untouched |
| `--force` | `-f` | Override conflicts to make A identical to B |
| `--show` | | Print planned changes |
| `--tags` | `-t` | Include tagging service |

### apply-patch

Apply a previously created patch file.

```
ovpackage apply-patch PATCH
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PATCH` | yes | Patch file to apply |

No command-specific options beyond globals.

---

## Distribution & Transfer

### mirror

Copy a package version between repositories.

```
ovpackage mirror PACKAGE_NAME VERSION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes | Package name |
| `VERSION` | yes | Version string |

| Option | Description |
|--------|-------------|
| `--source-repo` | Source repository URL (**required**, env `OVPACKAGE_SOURCE_REPO`) |
| `--destination-repo` | Destination repository URL (**required**, env `OVPACKAGE_DESTINATION_REPO`) |
| `--recursive` | Mirror all subpackages and rewrite dependencies |
| `--repo-mapping` | JSON file with repository mappings for subpackages (list of `{"source": URL, "dest": URL}`). Implies `--recursive`. Source/dest repos of main package are added implicitly. |
| `--resume` | Resume interrupted mirror by cataloging destination and fixing gaps |
| `--template-version` | Differential mirror based on already-mirrored version |
| `--redirect-dependencies` | Rewrite dependency repos to target (even if subpackages not yet mirrored) |
| `--tags` | Include tagging service |

### publish-redirect

Create redirect packages in a redirect repository for all packages in source repositories. Redirect packages contain only `.wrapp` files (no data files) with `source_path` entries pointing to original locations or external URLs (e.g., CDN).

```
ovpackage publish-redirect
```

| Option | Short | Description |
|--------|-------|-------------|
| `--source-repo` | `--sr` | Source repository URL(s) (**required**, repeatable) |
| `--redirect-repo` | `--rr` | Redirect repository URL (**required**) |
| `--external-url` | `--eu` | External base URL for catalog `source_path` entries (e.g., `https://cdn.example.com`). If not specified, source paths are preserved. |

### export

Create a tar archive of a package for offline transfer.

```
ovpackage export PACKAGE_NAME VERSION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes | Package name |
| `VERSION` | yes | Version string |

| Option | Short | Description |
|--------|-------|-------------|
| `--remote, --repo` | `-r` | Repository URL |
| `--catalog` | `-c` | Use a catalog file instead of `--repo` |
| `--output` | `-o` | Output filename (default: `name.version.tar`) |
| `--overwrite` | `-w` | Allow overwriting existing tar |
| `--dedup` | `-2` | Deduplicate content by hash in the tar |

### import

Import a tar archive into a repository.

```
ovpackage import PACKAGE_FILE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_FILE` | yes | Tar file to import |

| Option | Short | Description |
|--------|-------|-------------|
| `--remote, --repo` | `-r` | Target repository URL (**required**) |
| `--overwrite` | `-w` | Allow overwriting existing data |

---

## Archive & Copy

### freeze

Copy a directory tree to an archive location, preserving content integrity.

```
ovpackage freeze SOURCE DEST_DIR
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SOURCE` | yes | Source URL or catalog (with `--catalog`) |
| `DEST_DIR` | yes | Destination URL |

| Option | Short | Description |
|--------|-------|-------------|
| `--catalog` | | Indicate SOURCE is a catalog file |
| `--overwrite` | `-w` | Allow overwriting destination |
| `--ignore-file` | `-i` | Path to ignore rules file |
| `--ignore-packages` | | Skip files referenced in package metadata files found in the tree |
| `--force-upload` | | Force upload, skip hash-based dedup |
| `--tags` | `-t` | Include tagging service |

### check

Verify package integrity by checking file hashes against the catalog.

```
ovpackage check PACKAGE VERSION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE` | yes | Package name |
| `VERSION` | yes | Version string |

| Option | Short | Description |
|--------|-------|-------------|
| `--remote, --repo` | `-r` | Repository URL (**required**) |
| `--recursive` | `-R` | Check all subpackages too |

---

## Inspection & Utilities

### traceback

Show the provenance chain of a package metadata file.

```
ovpackage traceback PACKAGE_FILE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_FILE` | yes | URL/path to `.wrapp` file |

No command-specific options beyond globals.

### find-orphans

Find packages in repositories not referenced by any seed package.

```
ovpackage find-orphans
```

All options are required flags:

| Option | Short | Description |
|--------|-------|-------------|
| `--package` | `-p` | Seed package URL(s) (**required**, repeatable) |
| `--repo` | | Repository URL(s) to inspect (**required**, repeatable) |
| `--output` | | Write results to file instead of stdout |
| `--overwrite` | `-w` | Allow overwriting output file |

### delete-packages

Delete packages from a repository. **Destructive** -- will break dependent packages.

```
ovpackage delete-packages [PACKAGE]...
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE` | no | URL(s) of package `.wrapp` files in a repository |

| Option | Description |
|--------|-------------|
| `--package-list` | File containing package URLs to delete (one per line) |
| `--dry-run` | Test without actually deleting |

### create-metapackage

Create a package containing only dependency references (no files).

```
ovpackage create-metapackage [PACKAGE_NAME] [VERSION]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_NAME` | yes | Package name |
| `VERSION` | yes | Version string |

| Option | Short | Description |
|--------|-------|-------------|
| `--remote, --repo` | `-r` | Repository URL (**required**) |
| `--json` | | Path to JSON DependencySet file |
| `--csv` | | Path to CSV file (columns: name, version, path, repo) |

### build-manifest

Build a V1 storage-core manifest with relative paths and durable Storage API identities. For repository-based package metadata files, it recursively extracts the catalog, validating legacy bytes against recorded hashes to reconstruct missing identities by default. Existing identities and metadata are preserved; published packages are not modified. Changed content, missing hashes, or unavailable identities cause failure. For workspace-based package metadata files, it catalogs the current file tree.

```
ovpackage build-manifest PACKAGE_FILE OUTPUT
```

| Argument | Required | Description |
|----------|----------|-------------|
| `PACKAGE_FILE` | yes | URL to the `.wrapp` file |
| `OUTPUT` | yes | Output JSON file path |

| Option | Short | Description |
|--------|-------|-------------|
| `--overwrite` | `-w` | Allow overwriting existing output file |
| `--recalculate-identities / --no-recalculate-identities` | | Validate and reconstruct missing trusted identities (default: on); off requires recorded identities |
| `--minimal-format` | | Omit size and modification time; identities are still required |

### ls

List contents of a URL, optionally with pattern matching.

```
ovpackage ls URL
```

| Argument | Required | Description |
|----------|----------|-------------|
| `URL` | yes | URL to list |

| Option | Short | Description |
|--------|-------|-------------|
| `--recursive` | `-R` | List recursively |
| `--pattern` | `-p` | File matching pattern (repeatable, supports wildcards) |
| `--output-format` | | `tsv\|normal\|short` (default: `normal`) |
| `--with-hash` | | Issue an extra stat per file to populate the Hash column (required for Storage API; no-op for backends that already provide hashes) |

### get

Download a single file from a URL.

```
ovpackage get URL
```

| Argument | Required | Description |
|----------|----------|-------------|
| `URL` | yes | Source URL |

| Option | Short | Description |
|--------|-------|-------------|
| `--filename, --output` | `-o` | Local filename (default: same as source) |
| `--overwrite` | `-w` | Allow overwriting existing file |

### cat

Print the content of a remote text file to the console.

```
ovpackage cat URL
```

| Argument | Required | Description |
|----------|----------|-------------|
| `URL` | yes | Source URL |

No command-specific options beyond globals.

### version

Print the installed ovpackage version.

```
ovpackage version
```

No arguments or command-specific options.
