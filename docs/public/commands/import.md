# Import

## Description

Given a previously exported tar file and a repository location, import the package and make it available for install commands using that repository.

An exported package can be imported again into a repository. This is typically used after creating a tar file
with the `export` command. Supported archive formats are `.tar` and `.tar.gz`.

If an archive contains SimReady package-standard sidecars, `import` writes them
next to the imported `.wrapp` file verbatim, preserving package-standard
identity; it does not regenerate, re-hash, or validate them. The archive is
trusted the same way its `.wrapp` catalog and content hashes are trusted, so
whatever sidecars the archive carries are imported as-is. See
package standard sidecars.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage import --help`.

## Python API Reference

See the `ovpackage.import_package` function docstring for the full Python API reference (signature, parameters, and error handling).
