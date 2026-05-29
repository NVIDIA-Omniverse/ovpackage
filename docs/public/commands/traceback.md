# Traceback

## Description

Build a traceback tree for an ovpackage file, following both source and repository paths.

### Package Ancestry

Each package records two pointers to trace its ancestry:

1. The **source** pointer is set on `create` to point to the root directory of the create operation.
2. The **repository** pointer is set on `create` to point to the target repository, updated on `install`
   to point to the source repository, and updated on `mirror` to point to the source repository.

This allows tracing the origin of an install or mirror operation via the repository pointer, and finding
the original source directory via the source pointer. Repeated install/create cycles can be traced back
through multiple repositories as long as no intermediate packages are deleted.

The `traceback` command produces a user-friendly inheritance tree by following these pointers.

### Reported Cases

The following cases can be reported by traceback:

* **Primary source** at `wrapp_file`.
* **Primary source folder** (no package file) `wrapp_dir`.
* **Intermediate source** installed at `wrapp_file`.
* **Primary repository** at `wrapp_file`.
* **Mirror repository** at `wrapp_file`.
* **Source without repository pointer** at `wrapp_file`. This is considered a data error.
* **Could not find ovpackage file in repository** at `wrapp_file`. This is flagged as an error.
* **Source package missing**: Could not find ovpackage file at `wrapp_file`. This is flagged as an error.
* **Circular reference detected**: `wrapp_file` has already been visited. This is considered a data error.

For CLI options, run `ovpackage traceback --help`.

## Python API Reference

### ovpackage.traceback

See the `ovpackage.traceback` function docstring for the full Python API reference (signature, parameters, and error handling).
### ovpackage.TracebackNode

The traceback function returns a tree of `TracebackNode` objects:

See the `ovpackage.TracebackNode` class docstring for the full Python API reference.
### ovpackage.PackageLocationType

Each node contains a location type:

See the `ovpackage.PackageLocationType` class docstring for the full Python API reference.
