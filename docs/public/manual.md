# ovpackage Introduction

## Overview

ovpackage provides a command line tool helping with asset packaging and publishing operations for assets stored
in S3 buckets, Azure containers, GCS buckets, or file systems in a decentralized way.
It encourages a structured workflow for defining the content of an asset package, and methods to publish
and consume those packages in a version-safe manner. ovpackage does not require
a whole-sale shift of your workflow, but instead helps you to migrate to a structured, version managed workflow.

Below is a diagram illustrating the data flow with generic content files using ovpackage. As a new package is created, that specific
content is stored within the package version in the repository. This package can then be installed in new locations as needed.

![Alt text](images/overview_diagram.png "ovpackage Overview Diagram")

## Key Points and Assumptions
- ovpackage does not force you into a defined directory structure, it allows the user to define the package structure.
- ovpackage does not modify your files or reference paths in your files. It is assumed, if you want to have your reference versions managed by ovpackage, that the references are all relative within the cataloged directory.
- ovpackage allows for multiple repositories on a single server or across multiple S3 buckets or Storage API services.
- ovpackage allows you to aggregate packages from multiple repositories into "target" directories, and even allows for package dependencies across repositories.
- ovpackage supports creating redirect packages for CDN distribution, allowing package metadata to be served from a CDN while data files remain in the original storage location.

```{warning}
Running an ovpackage write operation concurrently with any other ovpackage operations on the same data can lead to undefined behavior.
Avoid overlapping writes on the same paths; serialize write operations per repository/package path.
```

## Supported Storage Systems

ovpackage supports the following storage systems:

- **S3** — Amazon S3 and compatible object storage (via boto3)
- **Azure Blob Storage** — Microsoft Azure containers and blobs
- **Local file system** — Local directories and files

```{eval-rst}
```

For URL formats, configuration options, and authentication details, see [Supported Storage Systems](storage.md).

## Core Concepts

### Repository

Packages in ovpackage are stored in a **repository**. The concept of a repository is known from distributed versioning
systems like git, and denotes a location where versioned content is stored. In ovpackage, a repository is a directory
used as intermediate safe storage for frozen/archived package versions. Consumers of these packages use it as a
copy source.

The repository directory is called `.packages`. Each folder within represents a named package, containing sub-folders
for each version of that package. No prescriptions are made for how packages or versions have to be named—they just
need to be valid file and folder names.

An example repository structure:

```text
/.packages
/.packages/vegetation_pack
/.packages/vegetation_pack/20230505
/.packages/vegetation_pack/20230512
/.packages/vegetation_pack/20230519
/.packages/rocks_pack
/.packages/rocks_pack/v1.0.0
/.packages/rocks_pack/v1.1.0
```

### Package Lifecycle

The typical workflow for managing packages involves these primary commands:

1. **[create](commands/create.md)** — Snapshot content from a source folder and store it as a versioned package in the repository
2. **[install](commands/install.md)** — Install a package version from the repository to a target location
3. **[uninstall](commands/uninstall.md)** — Remove an installed package from a target location
4. **[status](commands/status.md)** — Check the status of installed packages
5. **[list-repo](commands/list-repo.md)** — Browse available packages and versions in a repository

### Package Distribution

ovpackage allows transferring packages between repositories using the `mirror` command. This enables
workflows like promoting packages from development to staging to production, or distributing packages
across different teams or locations. For air-gapped environments, `export` and `import` commands
support offline transfer via tar files.

(package-standard-sidecars)=

### ovpackage Metadata and SimReady Package-Standard Sidecars

ovpackage packages are driven by `.wrapp` metadata files. A `.wrapp` file is the
source of truth for ovpackage commands, dependency resolution, install/update diffs,
and backwards compatibility with existing repositories.

ovpackage can also write companion metadata for the
[SimReady Foundation packaging standard](https://github.com/NVIDIA/simready-foundation/tree/main/nv_core/sr_specs/docs/capabilities/packaging).
The standard is part of SimReady Foundation's packaging capabilities, which
define package identity, integrity, metadata, discovery, and delivery
requirements for SimReady OpenUSD asset packages and registries. In the long run,
ovpackage's `.wrapp` format is expected to be replaced by this packaging standard,
but ovpackage must remain backwards compatible while that migration happens.

When enabled, ovpackage writes these files next to the `.wrapp` file:

```text
com.nvidia.simready.packaging.json
.metadata/com.nvidia.simready.packaging.bom.json
```

The package definition records the package identity, license, metadata hashes,
content hash, and package hash. The BOM lists the package content files with
sizes and SHA-256 hashes. The `.metadata/` folder may also hold additional
companion files placed there by other tooling; ovpackage does not interpret them,
but it carries the whole folder along with the package. ovpackage treats all of
these files as sidecars, not package payload:

- they are ignored when ovpackage catalogs package content;
- they are ignored by install conflict checks and extra-file detection;
- they are not included in `PackageInfo.catalog.items`;
- they are generated by `create` when a license is available (their content
  hashes are computed during the create-time catalog pass, so payload files are
  read only once); other commands carry existing sidecars verbatim;
- they are not written during `--dry-run` operations.

Because the sidecars are companion metadata, not ovpackage package content, they
should not make repeated install, mirror, export, or import operations look
dirty by themselves.

Internally, ovpackage models the package definition and BOM as `PkgStdFileModel`
files. That gives the sidecars the same previous-version compatibility checks as
`.wrapp` files while preserving the package-standard JSON shape, including the
plain string `format_version` field required by the standard. In code, that
field is represented by `PkgStdModelVersion`, a string subclass.

#### Creating Sidecars

ovpackage does not assume a built-in package license. To create sidecars for a
package that does not already have package-standard metadata, pass an initial
SPDX license identifier:

```bash
ovpackage create my_package 1.0.0 ./source --repo ./repo \
  --create-package-standard LicenseRef-MyStudio
```

The shorter form is equivalent:

```bash
ovpackage create my_package 1.0.0 ./source --repo ./repo \
  --cps LicenseRef-MyStudio
```

The same default can be provided with the
`OVPACKAGE_CREATE_PACKAGE_STANDARD_LICENSE` environment variable.

The license can also be declared up front with `new`. Running
`ovpackage new my_package 1.0.0 ./source --create-package-standard LicenseRef-MyStudio`
drops a package-standard *definition seed* (identity only — package id and
license, no BOM) into `./source`. After populating the directory with content, a
plain `ovpackage create my_package 1.0.0 ./source --repo ./repo` picks up that
license automatically and generates the full sidecars, so it does not need to be
repeated. An explicit `--cps` on `create` overrides a seed.

Sidecar *creation* is available on the `create` and `new` commands only. Other
commands do not create package-standard sidecars; they carry existing sidecars
between locations (see *Preserving Package Identity* below).

The released package-standard metadata format does not yet model ovpackage package
dependencies. When sidecar generation is requested for a package with
dependencies, ovpackage logs an error and skips writing package-standard metadata
instead of writing incomplete sidecars.

#### Preserving Package Identity

Once a package has sidecars, repository movement preserves them: `install`,
`mirror`, `export`, and `import` copy the existing
`com.nvidia.simready.packaging.json` definition and every file in the
`.metadata/` folder (the BOM and any additional companion files) verbatim,
without regenerating or re-hashing them. The package identity (`package_id`,
`license`) and content hashes therefore stay byte-for-byte identical as a
package moves between repositories. `import` in particular trusts the archive
the same way it trusts the archived `.wrapp` catalog and content hashes:
whatever sidecars the archive carries are written as-is, with no license,
dependency, or schema gating.

## Getting Started

For step-by-step examples of common workflows including creating packages, installing them,
handling updates, and working with multiple servers, see the [Tutorial](tutorial.md).

For detailed documentation of all commands, see the [Commands Reference](commands/index.md).
