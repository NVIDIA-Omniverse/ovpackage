# Export

## Description

Creates an uncompressed tar archive (.tar) of a package from the given repository.

Instead of directly copying a package from server to server using the mirror command, you can also have ovpackage create
a tar archive with all contents of a package for a subsequent import operation.

Catalog entries with `resource_identity_v2` are exported from the recorded
version. An unavailable or changed identity fails instead of reading newer
content from the source address. Nucleus checkpoint reads can reuse Hub when
it is available; ovpackage checks the checkpoint's server hash against the recorded
identity first. The local file remains available until its archive entry has
been written, and cache handles and temporary files are released on success,
failure, or cancellation.

If the source package has SimReady package-standard sidecars, `export` carries
them into the archive verbatim; it does not regenerate or re-hash them. Those
sidecars are metadata companions and are not package catalog content. See
package standard sidecars.

Use `--strip-source` or `--canonical-source <url>` to keep internal source
addresses out of the exported `.wrapp` file. See
source provenance options for details.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage export --help`.

## Python API Reference

See the `ovpackage.export_package` function docstring for the full Python API reference (signature, parameters, and error handling).
