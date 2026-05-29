# Diff

## Description

Take two catalog files produced by the catalog command, and return files unique to a, unique to b, and different in three lists.

The diff command compares two catalogs, and can be used to find out what has changed or what are the differences between
two different copies of the same subtree. Comparison is performed by file path and content hash.

The function returns three lists: `(unique_to_a, unique_to_b, different)` where each list contains relative file paths.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage diff --help`.

## Python API Reference

See the `ovpackage.diff` function docstring for the full Python API reference (signature, parameters, and error handling).
