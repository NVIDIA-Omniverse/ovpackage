# Where ovpackage fits in Omniverse Content Services

Back to the [ovpackage README](../../README.md).

Omniverse Content Services connects content in existing storage systems to the teams, applications, and compute that use it. Each product has its own repository; **ovpackage** adds versioned packaging, dependency tracking, and distribution.

| Product repository | What it provides | How it fits with packaging |
| --- | --- | --- |
| **ovpackage** (this repository) | A CLI and Python API for content snapshots, package dependencies, versioned installs, mirroring, and offline transfer. | Identifies, publishes, and distributes reproducible content versions. |
| [**ovstorage**](https://github.com/NVIDIA-Omniverse/ovstorage) | A backend-neutral data-access library with storage plugins, language bindings, CLI, and MCP tools. | Provides application-facing object access. ovpackage adds package identity, versions, and distribution on top of storage. |
| [**usd-content-workspace**](https://github.com/NVIDIA-Omniverse/usd-content-workspace) | Service APIs, reference implementations, conformance tests, and deployment material for storage, identity, permissions, metadata, notifications, discovery, and thumbnails. | Supplies the service layer around content repositories. ovpackage's Storage API adapter publishes and installs packages through a configured service endpoint. |
| [**usd-caches**](https://github.com/NVIDIA-Omniverse/usd-caches) | Local, shared content, and shared derived-data caches. | Reuses fetched content near the applications and compute that consume installed package versions. |

These are complementary roles, not ovpackage runtime dependencies. ovpackage uses its own local, S3, Azure, GCS, Hugging Face, Nucleus, and Storage API adapters, and a local packaging workflow needs none of the other services. Each product has its own installation, compatibility, and support requirements.

For example, a pipeline can author content in storage reached through **ovstorage** or **usd-content-workspace** services, publish and pin versions with **ovpackage**, and serve repeated reads of installed content through **usd-caches**.
