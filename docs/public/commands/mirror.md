# Mirror

## Description

Take package name and version, and copy it from the source repository to the destination repository.

When working with multiple servers, it might make sense to transfer created packages (or rather specific versions of these) into
the .packages folder on another server so install operations on that server are fast and don't need to specify the source server as a repository.

This is what the mirror operation is built for - it will copy a package version from one server's .packages directory into another server's .packages directory.

Use `--hash-cache-file` (`--hcf`) to speed up repeated mirror operations by
caching file hashes between runs. See generic parameters for details.
Legacy pickle cache files (ovpackage `<=2.1.0`) are rejected; see [CLI](../cli.md) for JSON format and
one-time migration guidance.

If the source package has SimReady package-standard sidecars, `mirror` copies
them to the mirrored package verbatim, preserving package-standard identity; it
does not create or regenerate sidecars. See package standard sidecars.

The source repository may be a read-only Hugging Face dataset repository
(`hf://datasets/<owner>/<repo>/...`); the destination cannot, because ovpackage
does not write to Hugging Face. See [Supported Storage Systems](../storage.md).

Use `--strip-source` or `--canonical-source <url>` to keep internal source
addresses out of the mirrored `.wrapp` file. See
source provenance options for details.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage mirror --help`.

## Python API Reference

See the `ovpackage.mirror` function docstring for the full Python API reference (signature, parameters, and error handling).
See the `ovpackage.mirror_with_dependencies` function docstring for the full Python API reference (signature, parameters, and error handling).
