<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Third-Party Notices

This is the consolidated third-party software inventory for `ovpackage`.
It covers Python build, runtime, and optional dependencies declared in this
repository.

NVIDIA's own license terms (see [LICENSE](LICENSE)) do **not** override the
upstream licenses of these components. Your use of each component is subject
to its license.

No third-party source code is vendored in this repository. The Python packages
listed below are resolved from PyPI at install time.

If you discover a missing or incorrect attribution, please update this file.

---

## 1. Python build / runtime / optional dependencies

These dependencies are declared in `pyproject.toml` or referenced by the
README install instructions. They are resolved from PyPI at install time and
are not vendored.

### 1a. Build-system requires (PEP 517)

| Package | Required version | License |
|---|---|---|
| `setuptools` | >=71 | MIT |
| `wheel` | (unpinned) | MIT |

### 1b. Runtime / optional dependencies (declared)

| Package | Version | License | Scope |
|---|---|---|---|
| `aioboto3` | (unpinned) | Apache-2.0 | optional:all, optional:s3 |
| `aiofiles` | >=23.2.1 | Apache-2.0 | optional:all, optional:local |
| `aiohttp` | >=3.11.18 | Apache-2.0 AND MIT | optional:all, optional:azure, optional:storage-api |
| `azure-storage-blob` | >=12.25.1 | MIT | optional:all, optional:azure |
| `deepdiff` | >=8.0.0 | MIT | optional:test |
| `gitignore-parser` | >=0.1.11 | MIT | runtime |
| `google-cloud-storage` | >=3.9.0 | Apache-2.0 | optional:all, optional:gcs |
| `grpcio` | >=1.71.0, <1.73.0dev0 | Apache-2.0 | optional:all, optional:storage-api |
| `grpcio-tools` | >=1.71.0, <1.73.0dev0 | Apache-2.0 | optional:all, optional:storage-api |
| `jsonschema` | >=4.23.0 | MIT | optional:test |
| `keyring` | >=25.0.0 | MIT | optional:all, optional:storage-api |
| `networkx` | >=3.3 | BSD-3-Clause | runtime |
| `prompt-toolkit` | >=3.0.47 | BSD-3-Clause | runtime |
| `pydantic` | >=2.9 | MIT | runtime |
| `pytest` | >=8.3.1 | MIT | optional:test |
| `pytest-asyncio` | >=0.23.8 | Apache-2.0 | optional:test |
| `pytest-bdd` | >=7.2.0 | MIT | optional:test |
| `pytest-cov` | >=5.0.0 | MIT | optional:test |
| `pytest-dotenv` | >=0.5.2 | MIT | optional:test |
| `pytest-timeout` | >=2.3.1 | MIT | optional:test |
| `pytest-xdist` | >=3.6.1 | MIT | optional:test |
| `requests` | >=2.32.3 | Apache-2.0 | optional:all, optional:storage-api |
| `rich` | >=13.7.1 | MIT | runtime |
| `semver` | >=3.0.2 | BSD-3-Clause | runtime |
| `toml` | >=0.10.2 | MIT | runtime |
| `typer` | >=0.16 | MIT | runtime |

Notes:

- The `all` optional dependency group is an aggregate convenience extra for
  backend-specific optional dependencies.
- `typer` is declared with the `all` extra in `pyproject.toml`; transitive
  packages pulled by PyPI dependency resolution are not listed here because
  they are not declared directly by this repository.
- License identifiers are normalized from upstream PyPI project metadata.

---
