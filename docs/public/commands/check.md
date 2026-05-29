# Check

## Description

Given a package file address, check that the content referenced by the package is available and correct.

The check command verifies if the catalog of a repository package matches its actual data content.
This is useful for validating package integrity. Verification is performed by comparing file hashes
(SHA-256) and sizes against the values recorded in the package catalog.

Use `--hash-cache-file` (`--hcf`) to speed up repeated content checks by caching
file hashes between runs. See generic parameters for details.

Use `--file-transfer-jobs` (`--ftj`) to control the number of concurrent file
transfers when checking content against a Storage API backend.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage check --help`.

## Python API Reference

See the `ovpackage.check_package_content` function docstring for the full Python API reference (signature, parameters, and error handling).
