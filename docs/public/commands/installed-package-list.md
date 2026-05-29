# Installed Package List

## Description

Given a top-level ovpackage file, outputs the list of all packages within that top-level package.

Usage::

    ovpackage installed-package-list <top_level_wrapp_file>

This recursively loads all ovpackage files and prints, for each subpackage:

- Package name
- Version
- Install location
- Source repository
- Number of file items

This will pick up both properly installed packages with their dependency recorded and stray ovpackage files
which are present in the folder tree and will only be picked up by the next create command.

For CLI options, run `ovpackage installed-package-list --help`.

## Python API Reference

See the `ovpackage.installed_package_list` function docstring for the full Python API reference (signature, parameters, and error handling).
