# Extract-catalog

## Description

Given a package name and a version plus a repository location, extract and return the
`Catalog` object.

For use with `diff`, `create-patch`, and `apply-patch` commands, we have the ability to extract a catalog
out of a package version.

By default, this will produce the list of files within the package itself only. To get a full, flattened catalog
of all files within a package including its subpackages, specify the `--recursive` option.

For CLI options, run `ovpackage extract-catalog --help`.

## Python API Reference

See the `ovpackage.extract_catalog` function docstring for the full Python API reference (signature, parameters, and error handling).
