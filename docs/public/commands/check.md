# Check

## Description

The check command checks repository package catalogs. Entries without
`resource_identity_v2` are verified by comparing file hashes (SHA-256) and
sizes at their source addresses against the recorded catalog values. The
deprecated `resource_identity` field does not bypass this content check.

For identity-backed entries, the recorded version is authoritative; changes at
the mutable source address do not invalidate that version.

**Current limitation:** `check` trusts recorded v2 identities and checks
catalog structure without testing whether those identities remain available.
A successful check is therefore not an availability audit. Applications must
not rely on malformed or unavailable identities passing `check`; this
limitation is not a guarantee that identity validation will always be skipped.
Actual reads using v2 fail if the token cannot be resolved; they never fall
back to the address.

Use `--hash-cache-file` (`--hcf`) to speed up repeated content checks by caching
file hashes between runs. See generic parameters for details.

Use `--file-transfer-jobs` (`--ftj`) to control the number of concurrent file
transfers when checking content against a Storage API backend.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage check --help`.

## Python API Reference

See the `ovpackage.check_package_content` function docstring for the full Python API reference (signature, parameters, and error handling).
