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
Legacy pickle cache files (ovpackage `<=2.1.0`) are rejected; see [CLI](../cli.md) for JSON format and
one-time migration guidance.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage catalog --help`.

## Python API Reference

See the `ovpackage.catalog` function docstring for the full Python API reference (signature, parameters, and error handling).
