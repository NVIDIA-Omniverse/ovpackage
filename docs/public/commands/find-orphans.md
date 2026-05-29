# Find-orphans

## Description

Return all packages from all repos which are not needed to install the list of top-level package versions specified.

The find-orphans command helps identify packages in a repository that are no longer used by a given set of top-level packages.
This is useful for repository cleanup and maintenance.

This command will analyze the repository and return a list of package versions that are not referenced by the specified
top-level packages or their dependencies.

For large repositories, this operation may take some time as it needs to traverse all package dependencies.

For CLI options, run `ovpackage find-orphans --help`.

## Python API Reference

See the `ovpackage.find_orphaned_package_versions` function docstring for the full Python API reference (signature, parameters, and error handling).
