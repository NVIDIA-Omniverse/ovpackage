# Catalog

## Description

Takes a root URL or a TSV file list to build a catalog via either a file tree walk or a TSV file list, returning a catalog manifest.

The catalog command can be used to create a list of files in a specified subtree and store the result together with
explicit version information in a catalog (aka manifest) file. It will not capture empty folders.

The JSON file produced archives the files and their versions at the time the command is run. Being a server,
running the command again might produce a different catalog when files are added, deleted, or updated in the meantime.

To determine if the cataloged version is still the same, use the [Diff](diff.md) command to compare two
catalogs made at different points in time or even different copy locations of the same asset.

Use `--hash-cache-file` (`--hcf`) to speed up repeated catalog operations by
caching file hashes between runs. See generic parameters for details.
For entries with a recorded resource identity, the cache is keyed by that exact
opaque token and captured size, separately from address-based entries. Repeating
a catalog operation over S3, Azure, or GCS objects uploaded without ovpackage hash
metadata can therefore reuse the computed ovpackage hashes without downloading the
same identified bytes again. Changed identities are hashed separately.
Legacy pickle cache files (ovpackage `<=2.1.0`) are rejected; see [CLI](../cli.md) for JSON format and
one-time migration guidance.

## Resource identities

Catalog entries keep `source_path` as the logical user input path. For
storage backends that expose immutable object selectors, ovpackage also records
`resource_identity_v2` as ovpackage-owned backend metadata for the exact resource
that was cataloged. This may include S3 `versionId` plus `etag` or ETag
only, Azure `versionid` plus `etag` or ETag-only, and GCS `generation`.

S3 and Azure ETags are now enforced during identity reads and copies. For
ETag-only identities, changing an ETag in place makes later reads fail even
when file bytes are unchanged. Enable native versioning before writing and
cataloging package content; see cloud etag read compatibility for known
triggers, including multipart re-uploads, S3 metadata copies, and Azure
metadata or HTTP-property updates.

S3 consumers of catalogs containing `versionId` now also require
`s3:GetObjectVersion` on the source objects. `s3:GetObject` alone is
insufficient for version-qualified reads. See s3 consumer permissions
for the required policy and why current-version catalog/check metadata
operations can succeed before a later identity read fails.

Commands and APIs that consume catalogs use `resource_identity_v2` for reads and
copies when it is present. If the recorded identity is stale or no longer
resolves, ovpackage fails closed instead of falling back to the mutable
`source_path` and reading newer content from the same path.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage catalog --help`.

## Resource identities and compatibility

`source_path` is the input read address; explicit checkpoint/version URL
selectors are preserved. `relative_path` is the logical package path, without
URL selectors. Literal filename characters such as `?` remain part of
`relative_path`. Read addresses use URL escaping: `asset?.txt` is addressed
as `asset%3F.txt`, while a file literally named `asset%3F.txt` is addressed
as `asset%253F.txt`. Explicit file lists (including TSV files) contain URL
references, so escape filename characters there too; append checkpoint/version
queries after the escaped path. Native local paths passed as source directories
are treated literally. The destination filesystem must support the filenames;
normal Windows filenames cannot contain `?`.

Remote storage addresses now consistently use URL escaping. When loading older
catalogs and packages, ovpackage converts their literal Nucleus and cloud paths to
escaped URLs, preserving what those legacy addresses refer to. Existing file
URLs and ordinary HTTP URLs retain their escaping.

Backends supporting durable reads also populate
`resource_identity_v2`: an opaque token that returns the recorded bytes or
fails, without falling back to the current contents of `source_path`.

Equal non-null tokens imply equal bytes. Different tokens do **not** imply
different bytes. Content hashes remain the portable basis for comparisons and
fingerprints. `size` and UTC `modified_time` describe the observation captured
with the token; timestamps and token differences do not count as content changes.

The old `resource_identity` field is deprecated. Older ovpackage versions could
record it against the wrong content or retain it after relocation. It is preserved
when loading legacy catalogs, but is never trusted for read selection or copied
into v2. Entries without v2 continue to use the established address/hash path.
Explicit `extract-catalog --recalculate-identities` requires a recorded content
hash for every entry being recalculated. It validates newly observed bytes
against that hash before establishing v2; legacy entries without a stored hash
cannot be upgraded by this operation. Merely loading or saving a legacy catalog
does not upgrade its identity claim.

`build-manifest` enables this validation by default for legacy repository
catalogs, including dependencies, without modifying the published package.
Use `--no-recalculate-identities` to require identities already recorded in
the catalog. Storage-core manifests retain format **1.0** and require durable
identities for every file, even in minimal format.

Manifests for local file content are currently unsupported because the only
manifest consumer is the storage service, which serves S3, Azure and GCS
through Storage API. Recalculating identities does not enable local manifests.
This does not prevent writing a manifest for supported content to a local
output file. See [/api](../api.md) for `build_manifest`.

The minimum reader capability for newly written files is Catalog **6.0** and
PackageInfo **6.0** (the ovpackage release containing the v2 identity contract).
Copy patches require PatchOpList **3.0**. Earlier ovpackage readers reject these
major formats; they must not silently discard v2 and read a mutable address.
New ovpackage can still load the older formats. Catalog and package content
fingerprints retain their pre-change projection.

## Python API Reference

See the `ovpackage.catalog` function docstring for the full Python API reference (signature, parameters, and error handling).
