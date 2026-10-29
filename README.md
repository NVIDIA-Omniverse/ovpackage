# ovpackage — versioned asset packages and dependency tracking for USD content pipelines

`ovpackage` is a command-line tool and async Python API that turns a directory of assets into an immutable, named package version, publishes it to a repository, and installs exact versions into project workspaces while recording what each project depends on. It runs standalone, with no Omniverse Kit installation, against local disk, Amazon S3, Azure Blob, Google Cloud Storage, Hugging Face, and Storage API repositories.

> **`pip install "ovpackage[local]"`** · Publish asset versions once, install the same content wherever a project, simulation, or CI job needs it, and move to a chosen version without editing USD references. Built on content-hash catalogs, `.wrapp` dependency metadata, and storage adapters for local and cloud repositories.
>
> *Pre-release (version 0.3.0); not enterprise-supported. Commands, APIs, and metadata formats may change before 1.0.*

With Python 3.11 or newer in an activated virtual environment:

```bash
mkdir -p demo/source && printf "hello assets\n" > demo/source/readme.txt
ovpackage create sample_assets 1.0.0 demo/source --repo demo/repo --progress-report off
ovpackage install sample_assets 1.0.0 demo/workspace/sample_assets --repo demo/repo --progress-report off
ovpackage status demo/workspace/sample_assets/.sample_assets.wrapp --progress-report off
```

The installed copy reports `all clean`. Continue with [getting started](docs/getting-started.md) for source installs, backend extras, dependency tracking, and the Python API.

---

## 1. What is ovpackage?

**ovpackage** is the versioned packaging and distribution component of Omniverse Content Services, exposing a CLI and an asynchronous Python API. It snapshots a source directory into a **package version** stored under a repository's `.packages` folder, installs that version at a version-free path inside a workspace, and records each installed package's name, version, location, and source repository in a `.<package>.wrapp` file. Projects can then report local changes, update or downgrade to a specified version, and reproduce the exact content set they were built with.

ovpackage preserves your directory layout and file contents. It does not discover or rewrite USD asset paths: keep references relative within each package, and record cross-package relationships as package dependencies. See [where ovpackage fits](docs/overview/content_services.md) for its relationship to the other Omniverse Content Services repositories.

---

## 2. What functionalities are available, and who are the target users?

**What you can do with it:**

- **Publish immutable versions** — `create` stores a content-hashed snapshot of a directory; publishing an existing name and version fails instead of overwriting it.
- **Install and update to exact versions** — `install` places a version at a stable path, so USD references don't change; installing another version updates or downgrades it in place and refuses to overwrite local edits.
- **Track package dependencies** — `new` and `install --package` record dependencies in `.wrapp` metadata; `status`, `installed-package-list`, and `traceback` report changes, contents, and provenance.
- **Lock data sets** — metapackages pin a set of package versions, `check --recursive` verifies stored content against recorded hashes, and `build-manifest` writes a storage-core manifest for Storage API repositories.
- **Promote and distribute** — `mirror --recursive` copies a version and its dependencies between repositories; `export` and `import` move packages through tar archives; `publish-redirect` serves package metadata from a CDN.
- **Compare and reconcile content** — catalogs, `diff`, `create-patch`, and `apply-patch` describe and merge changes between directory snapshots.
- **Automate from Python or an agent** — call every command as an `async` function within `ovpackage.ContextManager`, or use the shipped agent skills.

**Who benefits:**

- **USD content pipeline and asset library teams** — publish shared assets as versions and give every project a recorded, updatable dependency set.
- **Simulation, synthetic data, and digital twin engineers** — pin the exact content behind a scene, training run, or evaluation, and verify it before use.
- **Build and release engineers** — promote packages from development to release repositories, gate CI on status and integrity checks, and deliver offline.
- **Python tool developers and AI coding agents** — embed package operations in pipeline tools, or drive them through the [skills](skills/README.md).

---

## 3. Documentation and reference links

- **Getting started:** [docs/getting-started.md](docs/getting-started.md) — install, quickstart, verification, and the Python API example.
- **Concepts and workflows:** [manual](docs/public/manual.md) · [tutorial](docs/public/tutorial.md)
- **Command reference:** [CLI guide](docs/public/cli.md) · [commands](docs/public/commands/index.md) · `ovpackage COMMAND --help`
- **Python API:** [API guide](docs/public/api.md)
- **Storage backends and authentication:** [storage guide](docs/public/storage.md) · [ignore rules](docs/public/ignore-rules.md)
- **Agent entry point:** [AGENTS.md](AGENTS.md) · [skills catalog](skills/README.md)
- **Package and release notes:** <https://pypi.org/project/ovpackage/> · [CHANGELOG.md](CHANGELOG.md)
- **Source and issues:** <https://github.com/NVIDIA-Omniverse/ovpackage> · [releases](https://github.com/NVIDIA-Omniverse/ovpackage/releases) · [issues](https://github.com/NVIDIA-Omniverse/ovpackage/issues)

---

## 4. System requirements

- **Python:** `>=3.11`, as declared in [pyproject.toml](pyproject.toml).
- **Storage extras:** install only the backends you use: `local`, `s3`, `azure`, `gcs`, `huggingface` (read-only), and `storage-api`, or `all`. `ovpackage version` lists the backends available in the current environment.
- **Remote repositories:** a reachable endpoint and credentials for each repository, passed with `--auth` or `OVPACKAGE_AUTH`. `omniverse://` Nucleus URLs additionally require the Omniverse client libraries in the environment.
- **Development tests:** the `test` extra and `make`; emulator-backed S3, Azure, and GCS tests also require Docker Compose.

---

## 5. Licensing

- **Source code and agent skills:** [Apache License, Version 2.0](LICENSE).
- **Third-party components:** remain under their own licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- **Contributions and security:** this project is not currently accepting contributions; see [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

> **Note:** run one write operation at a time against the same repository or package path; overlapping writes have undefined results.

---

*ovpackage · Omniverse Content Services · Copyright (c) 2026 NVIDIA Corporation.*
