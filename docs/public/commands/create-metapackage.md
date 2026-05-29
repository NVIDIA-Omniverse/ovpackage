# Create-metapackage

## Description

Creates a metapackage containing only dependencies to other packages, with no files of its own.

The create-metapackage command creates packages that contain only dependencies to other packages,
with no files of their own.

Instead of installing packages into a worktree and calling `create`, create-metapackage simplifies
this by directly specifying package names, versions, repository paths, and their relative locations
in the metapackage.

Metapackages can reference multiple repositories and servers. Consumers need access to all referenced
repositories, or alternatively, use [mirror](mirror.md) with `--recursive` to transport everything to a single location.

Note that the metapackage is a normal ovpackage package, the only difference is that it is created with an empty catalog
of itself having only dependencies to other packages that were already created.

See also: [create](create.md), [install](install.md), [catalog](catalog.md).

For CLI options, run `ovpackage create-metapackage --help`.

## Python API Reference

See the `ovpackage.create_metapackage` function docstring for the full Python API reference (signature, parameters, and error handling).
