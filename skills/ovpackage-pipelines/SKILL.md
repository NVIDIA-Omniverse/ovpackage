---
name: ovpackage-pipelines
description: Automate ovpackage in CI/CD and content pipelines — version and publish packages from builds, gate jobs on package status and integrity, promote versions with dependencies between development, staging, and release repositories, check out reproducible content for a job, deliver packages to offline sites, and publish outside an organization without leaking internal addresses. Use when a user is scripting ovpackage, wiring it into CI, designing a promotion or release flow, or moving packages between networks.
license: Apache-2.0
compatibility: Run where ovpackage is installed non-interactively with the extras for every repository the job touches; credentials come from the CI secret store.
---

# ovpackage in pipelines

Command-by-command recipes are in [pipeline-recipes.md](../ovpackage-cli/references/pipeline-recipes.md). This skill covers the decisions and guardrails a pipeline needs around them. For how packages map onto a USD content pipeline, read the expert's [content pipeline](../ovpackage-expert/references/content-pipeline.md) and [repository strategy](../ovpackage-expert/references/repository-strategy.md) references.

## Automation defaults

- Pass `--progress-report off` (or set `OVPACKAGE_PROGRESS_REPORT=off`) on commands whose `--help` lists it, and `--log-file` per job so each run keeps its own log.
- Prefer `OVPACKAGE_*` environment variables in new automation, such as `OVPACKAGE_AUTH`, `OVPACKAGE_REPO`, and `OVPACKAGE_LOG_FILE`. Never use `--interactive-auth`, which opens a browser and blocks.
- Run one writer per repository or package path. Use a CI concurrency group or lock per repository; overlapping writes have undefined results.
- Tune throughput with `--jobs` and `--file-transfer-jobs`. Reuse hashes across runs with `--hash-cache-file`, available on `catalog`, `check`, `install`, `mirror`, and `uninstall`.
- Treat exit codes as signals, then read the output: `0` success, `1` failure or a reported difference, `2` a usage error.

## Version and publish from a build

Versions are free-form folder names, and published versions are immutable. Pick a scheme that sorts and never repeats, such as a UTC timestamp (`2026.10.13.1530`), a build number, or semantic versions for released libraries. Publish each changed snapshot as a new version:

```bash
VERSION="$(date -u +%Y.%m.%d.%H%M)"
ovpackage create textures "$VERSION" ./build/textures --repo "$DEV_REPO" --progress-report off --log-file create.log
ovpackage list-repo "$DEV_REPO"
```

`create` fails if that name and version already exist, so a retried job either chooses a new version or treats an existing one as done after confirming it with `list-repo` and `check`. Keep build output out of a package with a `.wrappignore` file in the source root; see [ignore rules](../../docs/public/ignore-rules.md).

## Gate a job on package state

| Gate | Command | Pass condition |
| --- | --- | --- |
| Published content is intact | `ovpackage check NAME VERSION --repo URL --recursive` | exit `0` |
| A workspace has no local edits | `ovpackage status PROJECT.wrapp --output-format tsv` | no rows other than the project's own draft rows |
| Two snapshots are identical | `ovpackage diff a.json b.json` | exit `0` (exit `1` means they differ; add `--show` for details) |
| An install would succeed | `ovpackage install ... --dry-run` | exit `0`; nothing is written |

`status` exits `1` both for modified content and for a project that has never been published. Gate on the reported types instead. This check ignores rows only for packages reported as drafts (`package_no_catalog`), and fails on any other change:

```bash
ovpackage status ./shot010/.shot010.wrapp --output-format tsv --progress-report off > status.tsv || true
if ! awk -F'\t' 'NR > 1 { row[NR] = $0; pkg[NR] = $1; if ($2 == "package_no_catalog") draft[$1] = 1 }
         END { for (i in row) if (!(pkg[i] in draft)) { print row[i]; bad = 1 }; exit bad }' status.tsv; then
    echo "workspace has local changes"; exit 1
fi
```

## Promote between repositories

Use `mirror --recursive` to copy a version and every dependency, rewriting dependency repositories to the destination:

```bash
ovpackage mirror shot010 1.0.0 --source-repo "$DEV_REPO" --destination-repo "$RELEASE_REPO" --recursive --progress-report off
ovpackage check shot010 1.0.0 --repo "$RELEASE_REPO" --recursive --progress-report off
```

- `--template-version OLD` transfers only what changed since a version already mirrored; `--resume` completes an interrupted mirror.
- `--repo-mapping map.json` sends dependencies that live in *other* source repositories to chosen destinations: a JSON list of `{"source": URL, "dest": URL}`. The main package's own source-to-destination pair is added implicitly and cannot be remapped.
- Promotion copies versions; it never edits them. Promote the exact version you tested.

## Check out reproducible content for a job

- **A pinned project:** `ovpackage install shot010 1.0.0 ./job/shot010 --repo "$RELEASE_REPO"` materializes the project and all its dependencies at their recorded versions.
- **A catalog snapshot:** `ovpackage freeze snapshot.json ./job/content --catalog` copies exactly the files recorded in a catalog. Files moved or deleted since cataloging cannot be frozen.
- Record the package name and version, or the catalog, in the job's outputs so a later run can reproduce it.

## Deliver to an offline site

For a package whose files are all its own, `export` and `import` move it through a tar archive. `--dedup` shrinks archives with repeated content. `export` writes one package without its dependencies, so export and import each dependency as well:

```bash
ovpackage export textures 2.0.0 --repo "$RELEASE_REPO" --dedup --progress-report off
ovpackage import textures.2.0.0.tar --repo ./site_repo --progress-report off
```

`import` currently fails with `catalog root URL cannot be found` for packages that have no files of their own, such as metapackages and projects made only of dependencies. For those, and for whole dependency trees, transfer a file-system repository instead:

```bash
ovpackage mirror shot010 1.0.0 --source-repo "$RELEASE_REPO" --destination-repo ./transfer_repo --recursive --progress-report off
# move ./transfer_repo to the offline site with any file transfer tool, then:
ovpackage mirror shot010 1.0.0 --source-repo ./transfer_repo --destination-repo "$SITE_REPO" --recursive --progress-report off
ovpackage check shot010 1.0.0 --repo "$SITE_REPO" --recursive --progress-report off
```

## Publish outside your organization

- `.wrapp` files record `source` and `repository` URLs. On `create`, `mirror`, `export`, `import`, and `publish-redirect`, add `--strip-source` to clear them, or `--canonical-source URL` to record a public base address instead. The two options are mutually exclusive.
- `publish-redirect --source-repo URL --redirect-repo URL --external-url https://cdn.example.com` creates metadata-only redirect packages, so consumers read package metadata from a CDN while data stays in the original storage. Publication does not check that the CDN already serves the data.

## Use the Python API in a pipeline

Each command is an `async` function. Run a job's calls inside one `ovpackage.ContextManager` and pass `scheduler=` to each call. `src/wrapp_examples/create_new_version.py` lists a repository, computes the next version, and publishes it. See [ovpackage-api](../ovpackage-api/SKILL.md).
