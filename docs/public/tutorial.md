# ovpackage Tutorials

This guide walks through ovpackage installation and common workflows with practical examples. For command reference
and API documentation, see the [Commands Reference](commands/index.md).

## Installation

ovpackage is distributed as a Python wheel through the [NVIDIA published package](this repository).
Installing the wheel into a Python 3.11 or 3.12 environment gives you both the `ovpackage` command-line
tool and the `ovpackage` Python API. If you have never used Python before, follow the steps below in order.

### Prerequisites: Installing Python

ovpackage requires Python **3.11 or 3.12** (`>=3.11, <3.13`). Skip this step if you already have a
compatible version installed.

- **Windows:** Download the installer from <https://www.python.org/downloads/> and, during setup,
  check **"Add python.exe to PATH"**.
- **Linux:** Install it with your distribution's package manager, for example on Ubuntu/Debian:

  ```bash
  sudo apt install python3.12 python3.12-venv
  ```

Confirm Python is available and reports a 3.11.x or 3.12.x version:

```bash
python --version
```

### Installing the ovpackage Wheel

2. (Recommended) Create and activate a virtual environment so ovpackage and its dependencies stay
   isolated from the rest of your system:

   ```bash
   python -m venv ovpackage-env
   ```

   Activate it on **Linux**:

   ```bash
   source ovpackage-env/bin/activate
   ```

   Or on **Windows (PowerShell)**:

   ```powershell
   ovpackage-env\Scripts\Activate.ps1
   ```

3. Install the wheel using pip, specifying `pypi.org` as an extra index URL to resolve the
   `omniverseclient` dependency:

   ```bash
   ```

   Replace `<version>` with the actual version number of the downloaded wheel file.

### Using the ovpackage CLI

Installing the wheel places the `ovpackage` command-line tool on your PATH (inside the active
environment). Verify the installation and explore the available commands:

```bash
ovpackage --help
ovpackage version
```

Every command has its own help, for example:

```bash
ovpackage freeze --help
```

The same functionality is available programmatically through the Python API:

```bash
python -c "import ovpackage; print('ovpackage installed successfully')"
```

---

## Working with Packages

This section covers the core ovpackage workflows for creating, distributing, and managing versioned
asset packages.

### Basic Package Lifecycle

Let's walk through a typical scenario: you're an asset developer who has created a vegetation
library at `omniverse://localhost/lib/props/vegetation`, and a colleague needs to use it in
their `SanDiego` project.

#### Creating the First Package Version

Your vegetation assets are ready for release. Let's publish them as version `20230505` using
[create](commands/create.md):

```bash
ovpackage create vegetation_pack 20230505 omniverse://localhost/lib/props/vegetation
```

ovpackage snapshots your content and stores it at `omniverse://localhost/.packages/vegetation_pack/20230505`.
Your original files remain untouched—the repository now holds an immutable copy. Files in an ovpackage repository
should never be modified.

By default, packages go to the `.packages` folder at the server root. For project-specific
repositories, specify a different location with `--repo`:

```bash
ovpackage create vegetation_pack 20230505 omniverse://localhost/lib/props/vegetation \
    --repo omniverse://localhost/project_repos/assets
```

#### Checking What's Available

Your colleague wants to know what packages they can use. They run [list-repo](commands/list-repo.md):

```bash
ovpackage list-repo omniverse://localhost
```

```text
vegetation_pack: 20230505
rocks_pack: v1.0.0
```

Great—your `vegetation_pack` is listed and ready to use.

#### Installing in a Project

Your colleague installs the vegetation pack into the SanDiego project using [install](commands/install.md):

```bash
ovpackage install vegetation_pack 20230505 omniverse://localhost/scenarios/SanDiego/vegetation
```

This creates a copy at the destination. USD scenes in the project can now reference files under
`/scenarios/SanDiego/vegetation/` without version numbers in the paths. ovpackage tracks which
version is installed via a `.<package>.wrapp` file (e.g., `.sandiego.wrapp`).

#### A Week Later: Updating the Package

You've improved the vegetation assets and want to release an update. Create a new version:

```bash
ovpackage create vegetation_pack 20230512 omniverse://localhost/lib/props/vegetation
```

Your colleague can see the new version is available:

```bash
ovpackage list-repo omniverse://localhost
```

```text
vegetation_pack: 20230512, 20230505
rocks_pack: v1.0.0
```

Before updating, they check if their installed copy has local modifications using [status](commands/status.md):

```bash
ovpackage status omniverse://localhost/scenarios/SanDiego/.sandiego.wrapp
```

If everything is clean, they update to the new version by running install again:

```bash
ovpackage install vegetation_pack 20230512 omniverse://localhost/scenarios/SanDiego/vegetation
```

ovpackage replaces the old version with the new one. If there were local modifications, the install
would fail with a conflict message—see [Handling Conflicts](#handling-conflicts) for how to resolve this.

#### Removing an Installed Package

If the vegetation pack is no longer needed in the project, remove it with [uninstall](commands/uninstall.md):

```bash
ovpackage uninstall vegetation_pack omniverse://localhost/scenarios/SanDiego/vegetation
```

### Multi-Repository Workflow

Your team has grown. Development happens on `dev.example.com`, but releases need to go to
`staging.example.com` for QA, and eventually to `prod.example.com`. Let's see how to move
packages between these environments.

#### Promoting a Package to Staging

The vegetation pack is ready for QA. Use [mirror](commands/mirror.md) to copy it from dev to staging:

```bash
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com
```

The package now exists in both repositories. QA can install it from staging without touching
the development server.

If the package depends on other packages (like a `textures_base` pack), mirror them all at once:

```bash
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com \
    --recursive
```

#### Handling Large Transfers

Mirroring a 50GB package over a slow connection? If the transfer gets interrupted, don't
start over—resume where you left off:

```bash
ovpackage mirror vegetation_pack 20230505 \
    --source-repo omniverse://dev.example.com \
    --destination-repo omniverse://staging.example.com \
    --resume
```

### Handling Conflicts

Your colleague made some tweaks to the installed vegetation pack—adjusted a few materials,
added a custom tree variant. Now they want to update to your new version. What happens?

#### When Updates Fail

They try to install the new version:

```bash
ovpackage install vegetation_pack 20230512 omniverse://localhost/scenarios/SanDiego/vegetation
```

ovpackage detects the local modifications and refuses to overwrite them. Good—no work is lost.
But how do they proceed?

#### Recording What Changed

Run the install again with `--patch` to capture the conflicts:

```bash
ovpackage install vegetation_pack 20230512 omniverse://localhost/scenarios/SanDiego/vegetation \
    --patch install_conflicts.patch
```

This creates a patch file (JSON format) listing every conflict: files that were modified
locally but also changed in the new version.

#### Applying the Update

After reviewing the patch file to understand what will change, apply it with
[apply-patch](commands/apply-patch.md):

```bash
ovpackage apply-patch install_conflicts.patch
```

---

## Catalog-Based Workflows

Sometimes you need to work with file trees outside of the normal package workflow—tracking
changes over time, comparing directories, or creating one-off archives. Catalogs are the tool
for this.

### Taking a Snapshot

You want to track what's in the company's sky assets folder. Create a catalog using
[catalog](commands/catalog.md):

```bash
ovpackage catalog omniverse://localhost/NVIDIA/Assets/Skies/Cloudy/ cloudy_skies.json
```

This JSON file now contains a complete inventory: every file, its size, and its hash. It's
a snapshot of that directory at this moment in time.

For assets stored on S3 or Azure, calculate hashes locally:

```bash
ovpackage catalog s3://my-bucket/assets/ assets.json --local-hash
```

### Excluding Files from Catalogs

Thumbnail caches, logs, and temp files shouldn't be tracked. Create a `.wrappignore` file
(same syntax as `.gitignore`):

```text
.thumbs
*.txt
*.log
```

ovpackage automatically uses this file when cataloging. For one-off exclusions:

```bash
ovpackage catalog omniverse://localhost/assets/ assets.json --ignore-file custom_ignore.txt
```

### Comparing Snapshots Over Time

A month later, you want to know what changed. Take another catalog and compare them with
[diff](commands/diff.md):

```bash
ovpackage diff cloudy_skies_january.json cloudy_skies_february.json --show
```

You'll see exactly which files were added, removed, or modified.

### Creating Archives with Freeze

The art director needs a frozen copy of the vegetation assets for a client demo. Use
[freeze](commands/freeze.md) to copy everything to an archive location:

```bash
ovpackage freeze omniverse://content.example.com/lib/props/vegetation \
    omniverse://content.example.com/archive/vegetation_20230505
```

For CI/CD pipelines, freeze directly to local disk:

```bash
ovpackage freeze vegetation.json file:///C:/build_jobs/vegetation_20230505 --catalog
```

---

## Offline Transfer (Export/Import)

Your client's production environment is air-gapped—no internet access for security reasons.
How do you deliver packages to them?

### Exporting to a File

Use [export](commands/export.md) to bundle a package into a portable tar file:

```bash
ovpackage export vegetation_pack 20230505 --repo omniverse://dev.example.com
```

This creates `vegetation_pack.20230505.tar` on your local machine. Copy it to a USB drive,
ship it, or transfer it however you need.

For large packages with many duplicate files (common with texture variations), enable
deduplication to shrink the archive:

```bash
ovpackage export vegetation_pack 20230505 --repo omniverse://dev.example.com --dedup
```

### Importing at the Destination

At the client site, someone with access to their internal server uses [import](commands/import.md):

```bash
ovpackage import vegetation_pack.20230505.tar --repo omniverse://client.example.com
```

The package is now in their repository, ready to install—no network connection to your
servers required.

---

## Verifying Package Integrity

You have trouble with your content in an ovpackage package and want to make sure nothing got corrupted. Use
[check](commands/check.md) to verify every file in a package matches its recorded hash:

```bash
ovpackage check vegetation_pack 20230505 --repo omniverse://localhost
```

If the package depends on others, verify the entire dependency tree:

```bash
ovpackage check vegetation_pack 20230505 --repo omniverse://localhost --recursive
```

Any mismatches are reported immediately—you'll know exactly which files have problems.

---

## Tracing Package History

Six months later, someone asks: "Where did this package originally come from?" Use
[traceback](commands/traceback.md) to follow the ancestry:

```bash
ovpackage traceback omniverse://localhost/scenarios/SanDiego/.sandiego.wrapp
```

ovpackage shows you the full history: which repository it was installed from, which repository
that was mirrored from, and ultimately which source directory it was created from. Useful
for auditing and debugging provenance issues.

---

## Further Information

For all available options on any command, run:

```bash
ovpackage <command> --help
```

For detailed documentation of all commands including the Python API, see the [Commands Reference](commands/index.md).
