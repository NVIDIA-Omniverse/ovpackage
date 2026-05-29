# Apply-patch

## Description

Taking a patch file as input, apply the changes previously calculated by the create-patch command.

The apply-patch command applies the changes recorded in a patch file to the target location. This is typically
used after `create-patch` has generated a patch file, or after `install --patch` has recorded conflicts.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage apply-patch --help`.

## Python API Reference

See the `ovpackage.apply_patch` function docstring for the full Python API reference (signature, parameters, and error handling).
