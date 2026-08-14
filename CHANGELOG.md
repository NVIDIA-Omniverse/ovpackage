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

Initial public release. See the [documentation](docs/public/README.md) for setup and usage.
