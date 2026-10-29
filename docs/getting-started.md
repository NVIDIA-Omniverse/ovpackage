# Getting started with ovpackage

Back to the [ovpackage README](../README.md).

This page installs ovpackage, publishes and installs a package on local disk, records a project's dependencies, updates them to a specified version, and calls the Python API. Every step needs only local files.

## Install

Use Python 3.11 or newer. Create a virtual environment and install the published package with the `local` extra:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "ovpackage[local]"
```

To work from this repository's source instead, clone it and install the checkout:

```bash
git clone https://github.com/NVIDIA-Omniverse/ovpackage.git
cd ovpackage
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[local]'
```

On Windows PowerShell, use `py -3.12 -m venv .venv` and `.venv\Scripts\Activate.ps1` for the venv steps.

Add extras for the remote backends you use: `s3`, `azure`, `gcs`, `huggingface`, and `storage-api`, or `all`. For example, `python -m pip install "ovpackage[local,s3]"`, or `'.[local,s3]'` from a checkout. Then confirm the command and the enabled backends:

```bash
ovpackage --help
ovpackage version
```

`ovpackage version` prints the installed version and one line per backend. Backends whose extra is missing are reported as disabled; the [storage guide](public/storage.md) explains each backend's URLs and authentication.

## Publish and install a package

Create a package version from a source directory, list the repository, and install that version into a workspace:

```bash
mkdir -p demo/source
printf "hello assets\n" > demo/source/readme.txt

ovpackage create sample_assets 1.0.0 demo/source --repo demo/repo --progress-report off
ovpackage list-repo demo/repo
ovpackage install sample_assets 1.0.0 demo/workspace/sample_assets --repo demo/repo --progress-report off
```

The listing includes `demo/repo/.packages/sample_assets: 1.0.0`. Confirm that the installed file has the published content and that the install is unmodified:

```bash
cat demo/workspace/sample_assets/readme.txt
ovpackage status demo/workspace/sample_assets/.sample_assets.wrapp --progress-report off
```

The file contains `hello assets`, and `status` prints `all clean` with exit code `0`. The repository copy at `demo/repo/.packages/sample_assets/1.0.0` is immutable: publish changed content as a new version.

## Track a project's dependencies

Create a project metadata file, then install packages through it so each dependency is recorded:

```bash
ovpackage new demo_project 1.0.0 demo/project
ovpackage install sample_assets 1.0.0 demo/project/assets \
    --repo demo/repo \
    --package demo/project/.demo_project.wrapp \
    --progress-report off
ovpackage installed-package-list demo/project/.demo_project.wrapp --progress-report off
```

The list shows `sample_assets 1.0.0` at `assets`. `status` on `demo/project/.demo_project.wrapp` exits with code `1` and reports `demo_project` as a draft package, because the project itself has never been published; an unchanged installed package reports `all clean` with exit code `0`.

## Update to a specified version

Publish a new version, then install it at the same location. The project's dependency record moves to the new version, and paths inside the project stay the same:

```bash
printf "hello assets, revised\n" > demo/source/readme.txt
ovpackage create sample_assets 1.1.0 demo/source --repo demo/repo --progress-report off
ovpackage install sample_assets 1.1.0 demo/project/assets \
    --repo demo/repo \
    --package demo/project/.demo_project.wrapp \
    --progress-report off
cat demo/project/assets/readme.txt
```

Installing `1.0.0` the same way returns to the earlier version. If files in the installed copy were edited, `install` refuses to overwrite them; see the [install command](public/commands/install.md) for conflict handling.

## Use the Python API

Every command is an `async` function in the `ovpackage` namespace. Run the following from the directory that contains `demo/`, saved as `list_local_packages.py`:

```python
import asyncio

import ovpackage


async def main():
    """List local package versions; ContextManager sets up and shuts down the runtime."""
    async with ovpackage.ContextManager() as context:
        for package_url, versions in await ovpackage.list_repo("demo/repo", scheduler=context):
            print(package_url, versions)


asyncio.run(main())
```

```bash
python list_local_packages.py
```

It prints the `sample_assets` package URL followed by `['1.0.0', '1.1.0']`. The [API guide](public/api.md) covers authentication, data objects, and manual initialization for larger applications.

## Next steps

- Learn the package, repository, and dependency model in the [manual](public/manual.md), and the multi-repository and offline workflows in the [tutorial](public/tutorial.md).
- Configure a remote repository with the [storage guide](public/storage.md).
- Run the public tests with `python -m pip install '.[local,test]'` and `make test`. Emulator-backed backend tests run with `make start-test-services`, `make test-emulators`, and `make stop-test-services`, and need Docker Compose.
- Working with an AI coding agent? Start at [AGENTS.md](../AGENTS.md).

Docker Compose builds MinIO from a pinned upstream source release because the
community container images are no longer published. The first
`make start-test-services` downloads the Go build image and source dependencies;
subsequent starts reuse the locally built image.
