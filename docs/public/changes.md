# Changes

# 0.3.0

## Compatibility 0.3.0

* **Breaking compatibility:** Packages written by ovpackage 0.3.0 use catalog/package format 6.0 and copy-patch format 3.0. ovpackage 0.2.1 and earlier cannot consume them. Upgrade readers before consuming packages written with this release.
* Catalog-backed reads now treat recorded resource identities as authoritative and fail if an identity cannot be resolved instead of falling back to a mutable source path. S3 and Azure catalogs enforce recorded ETags when no immutable object version is available; enable object/blob versioning before cataloging content and retain referenced versions. Reading S3 identities that contain `versionId` additionally requires `s3:GetObjectVersion`.
* Storage-core manifests require durable identities for every file. Generating manifests for local file content is unsupported; `--recalculate-identities` does not enable local manifests.

## New Features 0.3.0

* Publish operations can omit private provenance with `--strip-source`/`OVPACKAGE_STRIP_SOURCE`, or replace it with a canonical public base URL using `--canonical-source` (`--cs`)/`OVPACKAGE_CANONICAL_SOURCE`.
* Added a read-only Hugging Face dataset backend for `hf://datasets/<owner>/<repo>[@<revision>]/...` URLs. Install the `huggingface` extra to list, catalog, install, mirror, or otherwise consume packages from dataset repositories.
* S3, Azure Blob Storage, and Google Cloud Storage catalogs now retain logical source paths and version-bound resource identities. Repeated `catalog --hash-cache-file` operations reuse hashes for exact identities without redownloading unchanged objects.
* Build-manifest validates legacy catalog bytes and reconstructs missing durable identities by default. Use `--no-recalculate-identities` or `recalculate_identities=False` to disable reconstruction.

## Improvements 0.3.0

* Package operations preserve literal question marks and percent escapes in supported filenames and directories, including across legacy catalog migration and archive round trips.
* Storage API retries now use capped jittered backoff, and upload streams retain a transfer slot for their complete lifetime to prevent queued streams from being reset.
* Every CLI command now accepts the shared `--log-file` option.
* `create` warns when the final package contains paths that require case-sensitive filenames.
* Removed the upper version bound on Python while retaining Python 3.11 as the minimum. Storage API supports `grpcio>=1.73.1` and `protobuf>=6.31.0` without upper version bounds; `grpcio-tools` is no longer required at runtime.
* Backend-disabled diagnostics now identify optional backends and recommend the appropriate distribution extra.

## Bug Fixes 0.3.0

* Fixed transfers of files with spaces, Unicode, or literal percent escapes between local filesystems and storage backends, including mirroring locally created packages to S3, Azure, and GCS. Filenames such as `sp ace.txt` and `sp%20ace.txt` remain distinct.
* Fixed Storage API uploads on Windows incorrectly rejecting unchanged files as changed while the upload was queued.
* Fixed `ovpackage traceback` crashing when stdout uses a strict non-UTF-8 encoding.
* Fixed recursive local `ovpackage ls --pattern` matching for leading-star globs on Windows.
* Tightened Storage API wildcard route matching so trailing `/**` and single `*` patterns cannot claim lookalike paths or identifiers outside their advertised scope.
* Rejected unsafe Windows absolute paths and embedded NUL bytes in preserved `.metadata` companion files.

# 0.2.1

## New Features 0.2.1

* ovpackage can now connect to multiple Storage API services at once. Configure additional services with named `[storage_api.<name>]` tables in the configuration file; route scopes select the service for each URL and allow operations such as `mirror` to copy packages between services.

## Improvements 0.2.1

* Azure and Storage API transfers now open local files only after acquiring a file-transfer slot, bounding open file handles by `file_transfer_jobs`. Azure downloads stream to disk, Storage API uploads read source files lazily, and uploaded hashes are calculated from the transferred bytes.
* `install`, `mirror`, `export`, and `import` now preserve every companion file in a package's `.metadata/` folder, not only the package-standard BOM. `uninstall` removes those companion files with the package.

## Bug Fixes 0.2.1

* Improved the `install` diagnostic when package paths differ only by letter case and the destination storage is case-insensitive.
* Fixed `403 AccessDenied` failures when downloading through CloudFront-backed Storage API services whose presigned URLs contain percent-encoded signature data.
* Fixed redirect uploads remaining temporarily invisible to Storage API `Stat` and `List` calls after the upload completed.

# 0.2.0

## Compatibility 0.2.0

* Changed experimental build-manifest output to the storage-core manifest format. `ovpackage.build_manifest(...)` now returns a `ovpackage.Manifest`, and the CLI writes a top-level manifest object with full resource metadata by default. Use `--minimal-format` for compact per-file entries.
* Removed the `OVPACKAGE_GCS_EXTRA_RETRIES` and `OVPACKAGE_GCS_RETRY_WAIT_SECONDS` environment variables. GCS retry behavior is now controlled by the `google-cloud-storage` client's built-in retry settings (see Improvements).

## New Features 0.2.0

* Added [package-standard](https://github.com/NVIDIA/simready-foundation/tree/main/nv_core/sr_specs/docs/capabilities/packaging) metadata sidecar support. `ovpackage create` can write sidecars when `--create-package-standard`, `--cps`, or `OVPACKAGE_CREATE_PACKAGE_STANDARD_LICENSE` supplies a license, and `ovpackage new` can declare that license up front for a later `create`.
* Added `ovpackage ls --output-format long-tsv` for tab-separated output that mirrors the normal console columns. The existing `tsv` format remains compatible with catalog file-list input.

## Improvements 0.2.0

* Storage API tree catalogs now use the `Enumerate` stream by default for large prefixes, avoiding read-throttler contention during tree walks. Set `OVPACKAGE_STORAGE_API_DISABLE_ENUMERATE` to fall back to recursive `ListStat` traversal.
* Storage API read-side operations now retry transient gRPC timeouts during route discovery, unary calls, listing streams, and streamed reads/downloads. Tune attempts with `OVPACKAGE_STORAGE_API_READ_RETRIES`.
* GCS operations now use the `google-cloud-storage` client's built-in retry and exponential backoff. Retry deadlines, delays, and per-request timeouts can be tuned with `OVPACKAGE_GCS_TIMEOUT_SECONDS`, `OVPACKAGE_GCS_RETRY_INITIAL_SECONDS`, `OVPACKAGE_GCS_RETRY_MAXIMUM_SECONDS`, `OVPACKAGE_GCS_RETRY_MULTIPLIER`, and `OVPACKAGE_GCS_REQUEST_TIMEOUT_SECONDS`. For details, please refer to the "Supported Storage Systems" docs.
* S3 downloads now recover from transient connection failures, remove partial files after failed transfers, and fail cleanly if an object changes during download. `OVPACKAGE_S3_READ_TIMEOUT_S` controls socket-read timeouts, while the existing multipart, concurrency, and retry settings now apply to downloads as well as uploads. For details, please refer to the "Supported Storage Systems" docs.
* `ovpackage install --hash-cache-file` now warns when the destination storage already stores ovpackage hashes natively, since the separate hash cache file is ignored for those destinations.
* Additional public API types are now importable from the top-level `ovpackage` module, including `CheckResult`, `StorageMap`, `PatchOpBase`, `PackageStatusItem`, `PackageStatusItemType`, `LsOutputFormat`, `FileParseException`, and `FailedPatchCommand`.

## Bug Fixes 0.2.0

* Fixed Storage API route overrides from the configuration file not taking effect until after lazy discovery. Configured `omniverse://` and `file://` routes now apply immediately.
* Fixed `install` so re-installing a package with a bumped subpackage version updates the subpackage's `repository` field to the install repository instead of retaining the stale source repository.
* Fixed recursive package content checks to resolve nested dependencies from the dependency package's own repository metadata.
* Fixed `ovpackage status --ignore-file` so ignored files no longer cause a clean install to appear modified.
* Fixed `ovpackage count` so files without a stored hash are counted as unique entries in the reported storage size.
* Fixed `ovpackage create-patch --force` recording target-only file removals twice in patch output.
* `ovpackage new`, `ovpackage create-metapackage`, `ovpackage traceback`, `ovpackage.import_package`, `ovpackage.export_package`, and `ovpackage.create_patch` now report clearer errors for invalid or contradictory inputs.
* Removed a spurious `typer ... does not provide the extra 'all'` warning when installing `ovpackage`.

# 0.1.0

Initial public release. See the [documentation](README.md) for setup and usage.
