---
name: ovpackage-storage
description: Choose, configure, authenticate, and verify the storage behind ovpackage repositories and workspaces — local disk, Amazon S3 and compatible endpoints, Azure Blob, Google Cloud Storage, read-only Hugging Face datasets, Nucleus, and the Storage API. Use when a user asks where to keep a package repository, which install extra to add, how to write a repository URL, how to pass credentials with --auth or OVPACKAGE_AUTH, why a backend shows as disabled, or how to prove a repository is reachable before publishing.
license: Apache-2.0
compatibility: Run from an environment where ovpackage is installed with the extras for the backends in use. Remote backends need network access and credentials.
---

# ovpackage storage

Every ovpackage command takes storage URLs: a **repository** (the location whose `.packages` folder holds published versions), a **source** for `create`, and a **destination** for `install`. Each can be on a different backend. This skill picks the backend and proves access; the exact URL forms, credential formats, and tuning variables live in the references below.

## Choose the backend

| Need | Backend | Extra | URL form |
| --- | --- | --- | --- |
| Single machine, CI scratch space, or a repository directory you move as a whole | Local file system | `local` | `./repo`, `/mnt/packages`, `file:///mnt/packages` |
| Shared cloud repository; S3-compatible object stores | Amazon S3 | `s3` | `s3://bucket/path` |
| Shared repository in Azure | Azure Blob Storage | `azure` | `https://account.blob.core.windows.net/container/path` |
| Shared repository in Google Cloud | Google Cloud Storage | `gcs` | `gs://bucket/path` |
| Consume packages published to a Hugging Face dataset | Hugging Face (read-only) | `huggingface` | `hf://datasets/owner/repo/path` |
| Existing Nucleus server | Nucleus | Omniverse client libraries | `omniverse://server/path` |
| A service that implements the Storage API, or durable identities for manifests | Storage API | `storage-api` | service-specific, after discovery is configured |

Hugging Face is a read-only source: `install`, `list-repo`, and `mirror --source-repo` work; `create`, `import`, and `mirror --destination-repo` cannot target it. A [storage-core manifest](../ovpackage-dependencies/SKILL.md#lock-a-data-set) requires durable Storage API identities, so `build-manifest` fails on the other backends.

## Connect and verify

1. Install the extra in the same environment as ovpackage, for example `python -m pip install '.[local,s3]'`. Run `ovpackage version`: each backend line reports whether it is enabled. A Storage API line that reads `ENABLED but not configured` is harmless unless you use that backend.
2. Provide credentials without writing them to files. Use `--auth` per command or `OVPACKAGE_AUTH` in automation, or the backend's default credential chain. Each backend's format is in [storage-and-auth.md](../ovpackage-cli/references/storage-and-auth.md#authentication) and the [storage guide](../../docs/public/storage.md#authentication). Quote Azure `URL,connection_string` values so semicolons stay in one argument.
3. Prove read access before any write: `ovpackage list-repo <repo-url>` for an existing repository, or `ovpackage ls <url>` for a source or empty location. Use the credentials the real job will use.
4. Prove write access on a disposable path: publish a one-file test package to a scratch repository, install it into a fresh directory, and confirm `status` reports `all clean`.

## Storage behaviors that change workflows

- **Empty folders:** object stores keep files only. Add a placeholder file to any folder your pipeline needs to exist. On file systems and Nucleus, `uninstall` can leave empty folders behind. See [storage semantics](../../docs/public/storage.md#storage-semantics-and-empty-folders).
- **Hashing:** S3 and Azure don't provide ovpackage hashes for unpublished files. `catalog --local-hash` downloads files to hash them, and `--hash-cache-file` reuses hashes between runs.
- **Case-only path differences:** `install` refuses packages whose paths differ only by letter case when the destination is case-insensitive. Install to a case-sensitive destination or publish a corrected version.
- **Concurrency:** serialize writes to the same repository or package path, whatever the backend.
- **Provenance:** `.wrapp` files record source and repository URLs. Before publishing outside your organization, use `--strip-source` or `--canonical-source` ([pipelines](../ovpackage-pipelines/SKILL.md#publish-outside-your-organization)).

## References

- [storage-and-auth.md](../ovpackage-cli/references/storage-and-auth.md) — URL formats, `--auth` and `OVPACKAGE_AUTH` formats, default credential chains, parallelism and transfer tuning, environment variable summary.
- [Storage guide](../../docs/public/storage.md) — the full per-backend reference: Storage API discovery and authentication, S3 consumer permissions, GCS credential order and troubleshooting, Hugging Face credentials and content hashes.
- [Troubleshooting](../ovpackage-troubleshooting/SKILL.md) — authentication and backend failures.
