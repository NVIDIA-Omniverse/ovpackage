# Delete-packages

## Description

Given a list of package versions and their repository, run a parallel delete tree operation to remove the packages and their files from storage.

The delete-packages command allows you to remove specific packages from a repository. This should be used with caution
as it permanently removes the package data.

> NOTE:
>
> On storage systems that treat folders as first-class citizens (such as local file systems
>), empty parent folders may remain after deleting a package version.
> For example, if you delete the last version of a package, the package name folder within
> the repository may be left empty. These empty folders need to be removed manually if desired.
>
This command is often used in conjunction with [find-orphans](find-orphans.md) to clean up unused packages from a repository.

For CLI options, run `ovpackage delete-packages --help`.

## Python API Reference

See the `ovpackage.delete_packages_from_repo` function docstring for the full Python API reference (signature, parameters, and error handling).
