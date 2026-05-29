# Uninstall

## Description

Given a package name and either an install_location or the URL of an ovpackage file, uninstall that package after checking it has no local modifications that could be lost.

> NOTE:
>
> On storage systems that treat folders as first-class citizens (such as local file systems
>), empty folders may remain after uninstalling a package. ovpackage removes
> the files that were part of the package but does not remove the folders that contained them.
> These empty folders need to be removed manually if desired.
>
> WARNING:
>
> Uninstall will fail if there are local modifications to the installed package that would be lost.
> To proceed, either discard the modifications manually or use the appropriate force options.
>
Any package that has been installed can be uninstalled again. There are two modes of uninstallation:

1. Via the directory in which the package has been installed
2. Via pointing to the dependency file which had been used to record the install operation. This will also remove the
   dependency information recorded in that file.

Use `--hash-cache-file` (`--hcf`) to speed up repeated uninstall operations by
caching file hashes between runs. See generic parameters for details.
Legacy pickle cache files (ovpackage `<=2.1.0`) are rejected; see [CLI](../cli.md) for JSON format and
one-time migration guidance.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage uninstall --help`.

## Python API Reference

See the `ovpackage.uninstall` function docstring for the full Python API reference (signature, parameters, and error handling).
