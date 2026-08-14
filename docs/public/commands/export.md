# Export

## Description

Creates an uncompressed tar archive (.tar) of a package from the given repository.

Instead of directly copying a package from server to server using the mirror command, you can also have ovpackage create
a tar archive with all contents of a package for a subsequent import operation.

If the source package has SimReady package-standard sidecars, `export` carries
them into the archive verbatim; it does not regenerate or re-hash them. Those
sidecars are metadata companions and are not package catalog content. See
package standard sidecars.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage export --help`.

## Python API Reference

See the `ovpackage.export_package` function docstring for the full Python API reference (signature, parameters, and error handling).
