---
name: ovpackage-troubleshooting
description: Diagnose failed or surprising ovpackage commands from their exit codes, messages, and logs, recover without losing local edits or published data, and upgrade the ovpackage library safely with validation and rollback. Use when an ovpackage command exits nonzero, a backend is disabled, authentication fails, an install or import is refused, status reports changes, a manifest cannot be built, or the user is moving to a newer ovpackage version.
license: Apache-2.0
compatibility: Run in the environment where the failure occurred; diagnostics depend on the installed ovpackage version and extras.
---

# Troubleshooting ovpackage

## First steps

1. Record `ovpackage version` from the failing environment, the exact command, its exit code, and the backend involved.
2. Rerun with `--log-file job.log -v` for detail, or `--debug` for client-library diagnostics, where `--help` lists them. The default log file is `wrapp.log` in the working directory, or `OVPACKAGE_LOG_FILE` when set.
3. Read the message before retrying. Exit `1` from `status`, `diff`, or `check` reports a difference, not a crash. Exit `2` is a usage error: an unknown option or a missing argument.
4. Never "fix" a failure by editing files under a repository's `.packages/` folder, or by deleting or forcing without the user's approval.

## Symptoms

| Symptom | Cause | Action |
| --- | --- | --- |
| `ovpackage: command not found` | Environment not active or package not installed | Activate the project `.venv`; install with `python -m pip install '.[local]'` ([getting started](../../docs/getting-started.md)). |
| `No such option: --progress-report` (exit `2`) | Not every command takes every common option | Check `ovpackage COMMAND --help` and drop the option for that command. |
| A backend line in `ovpackage version` reads `DISABLED` | Its extra is not installed in this environment | Install the named extra into the same environment, then rerun `ovpackage version`. |
| `StorageAPI: ENABLED but not configured` | Storage API discovery is not set | Harmless unless you use that backend; otherwise configure it from the [storage guide](../../docs/public/storage.md#storage-api-authentication). |
| Authentication or permission errors | Missing, malformed, or wrong-scope credentials | Verify the `--auth` / `OVPACKAGE_AUTH` format for the backend ([storage skill](../ovpackage-storage/SKILL.md)). Quote Azure connection strings. Prove access with a read-only `list-repo` first. |
| `status` exits `1` and lists a `draft package` | The project's `.wrapp` was created with `new` and never published (still true after `create` in the working copy) | Expected; installed packages still report their own state. Gate automation as in [pipelines](../ovpackage-pipelines/SKILL.md#gate-a-job-on-package-state). |
| `status` reports `item_differs` or `package_modified` | Files in an installed copy were edited | Decide with the user whether to keep, republish, or discard the edits ([dependencies](../ovpackage-dependencies/SKILL.md#update-or-roll-back-to-a-specified-version)). |
| `install` fails: `Can't update, local copy is modified` | Local edits conflict with the target version | Use `--patch FILE` to review the changes, publish the edits as a new version, or `--force` only with approval. |
| `Cross platform case insensitive path conflicts found` | Package paths differ only by letter case and the destination is case-insensitive | Install to a case-sensitive destination, or publish a new version with distinct names. |
| `create` fails: `Package already exists` | Versions are immutable | Publish under a new version string. |
| `import` fails: `catalog root URL cannot be found` | The archived package has no files of its own, such as a metapackage or a project made only of dependencies | Transfer through a file-system repository with `mirror --recursive` ([offline delivery](../ovpackage-pipelines/SKILL.md#deliver-to-an-offline-site)). |
| `import` or `install` cannot find a dependency | `export` writes one package without its dependencies | Export and import each dependency too, or mirror recursively. |
| `build-manifest` fails: `lacks resource_identity_v2` | Storage-core manifests need durable Storage API identities | Build the manifest from a Storage API repository; lock other backends with versions, metapackages, and `check` ([dependencies](../ovpackage-dependencies/SKILL.md#lock-a-data-set)). |
| `find-orphans`: `must be packages inside a repository, not inside a work tree` | Seeds must be published `.wrapp` files | Pass the seed's `.packages/NAME/VERSION/.NAME.wrapp` URL. |
| `mirror --repo-mapping`: `was mapped before to` | The main package's source repository is mapped implicitly | Map only the other repositories that dependencies come from. |
| `check` reports `modified on storage` (exit `1`) | Stored bytes no longer match the recorded hash | Treat the version as corrupted: don't install it; restore from a mirror or republish under a new version. |
| A legacy pickle hash cache is rejected | Hash caches are JSON | Point `--hash-cache-file` at a new path and let it rebuild. Convert an old cache only if you trust it, with `src/wrapp_examples/migrate_hash_cache_pickle_to_json.py`. |
| GCS credentials select the wrong identity | Several credential sources are present | Follow the GCS [credential order and troubleshooting](../../docs/public/storage.md#troubleshooting). |

## Verify a recovery

- After `install`: inspect the files and run `status` on the installed package's `.wrapp`; expect `all clean`.
- After `mirror` or `import`: `list-repo` the destination, run `check NAME VERSION --repo DEST --recursive`, and install into a fresh directory.
- After a publish: `list-repo`, then install the new version into a fresh directory.

## Upgrade the ovpackage library

The library version from `ovpackage version` is separate from content versions such as `textures 2.0.0`; upgrading one never changes the other.

1. Record the current environment: `ovpackage version`, the install source, extras, and configuration. Keep that environment for comparison and rollback.
2. Read the target release's notes in [CHANGELOG.md](../../CHANGELOG.md) and [releases](https://github.com/NVIDIA-Omniverse/ovpackage/releases): Python requirements, compatibility notes, renamed options or variables, and metadata format changes.
3. Install the target into a **new** virtual environment with the same extras. Confirm `ovpackage version` and `ovpackage --help`.
4. Validate against disposable repositories and fresh destinations: install existing versions and check `status`; run your Python integration inside `ContextManager`; repeat your mirror or export/import path; and validate any catalog or manifest parsers against the new output.
5. If older ovpackage versions must read packages published by the new one, publish a disposable package with the new version and install it with each older version you support.
6. Switch only after the required workflows pass. To roll back, return to the preserved environment; original package versions stay untouched.

## Report a problem

Collect the version, command, exit code, backend, OS, and sanitized log, with credentials, tokens, and signed URLs removed. Ask the user to file a [GitHub issue](https://github.com/NVIDIA-Omniverse/ovpackage/issues), or to follow [SECURITY.md](../../SECURITY.md) for vulnerabilities. If a skill gave wrong guidance, report the prompt, the command the agent ran, and the outcome in an issue.
