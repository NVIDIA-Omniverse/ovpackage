# Status

## Description

Given the URL for a top level ovpackage file, return an ordered list of packages that are dirty, and a map of ovpackage file to the list of detailed status entries found in that subpackage.

To help detect issues of a modified folder content, the status command can be used to compare the content of an installed
folder with the originally installed content. This checks if an installed package with all its subpackages is unmodified.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage status --help`.

### Return Value

The `status` function returns a tuple of:

1. `List[str]` - Ordered list of ovpackage file URLs for packages that are dirty
2. `Dict[str, List[PackageStatusItem]]` - Map of ovpackage file URL to detailed status entries

Each `PackageStatusItem` contains:

- `package_name`: Name of the package
- `absolute_wrapp_file_path`: Location of the package file
- `status_message`: Human-readable description of the issue
- `status_item_type`: One of the `PackageStatusItemType` values

## Python API Reference

See the `ovpackage.status` function docstring for the full Python API reference (signature, parameters, and error handling).
