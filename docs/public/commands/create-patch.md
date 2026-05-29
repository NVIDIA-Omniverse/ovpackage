# Create-patch

## Description

Takes three catalogs, and calculates the patch operation to turn A into B. Returns list of operations and list of conflicts if any.

The create-patch command uses a three-way comparison to produce a patch file that will merge one file tree into another
given their common ancestor.

For example, when we have a file tree at time-point 1 and have created a catalog file for this file tree called
`catalog_week1.json`. We do a copy of this state to a new "target" location for use by downstream users. Now work
continues in the original location, then we create a new catalog at time point 2 called `catalog_week2.json`.

If we now want to update the copy of the file tree at our "target" location, and want to know if it is safe to overwrite
the tree or if there are local modifications we want to be alerted about, we use the following steps:

1. First, catalog the "target" location, let's call this `current_target.json`.
2. Then, run the create-patch command to produce the patch file.
3. When the command returns `no differences, empty patch`, it indicates no local changes have been made and there are no conflicts.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage create-patch --help`.

## Python API Reference

See the `ovpackage.create_patch` function docstring for the full Python API reference (signature, parameters, and error handling).
