# ovpackage CLI Skill

An agent skill that enables AI coding assistants to execute `ovpackage` commands for managing versioned asset packages across S3, Azure, Nucleus, and local storage.

Compatible with any AI coding agent that supports the [agentskills.io](https://agentskills.io) specification, including Cursor, Claude Code, GitHub Copilot, Codex, and others.

## Prerequisites

- An AI coding assistant that supports agent skills
- ovpackage CLI installed and available on PATH (or in a local venv)

## Installation

Copy or symlink the `ovpackage-cli/` skill directory into your agent's skill search path. Refer to your agent's documentation for the exact location:

| Agent | Skill path |
|-------|-----------|
| **Cursor** | `.cursor/skills/ovpackage-cli/` in your project or `~/.cursor/skills/ovpackage-cli/` for personal use |
| **Claude Code** | Pass `--plugin-dir` or use `/install-plugin` within a session |
| **Repository-local agents** | Use this repository's `.agents/skills/ovpackage-cli/` directory when supported |

## Usage

Once installed, the agent can help with ovpackage operations. Example prompts:

- "List all packages in my repository at s3://my-bucket"
- "Create a new package version from /path/to/assets"
- "Install vegetation_pack v2 into my project"
- "Mirror this package from dev to staging"
- "Export a package for offline delivery"
- "Check if my installed packages have been modified"
- "What's the provenance of this package?"

The agent will automatically:
- Locate the ovpackage binary (PATH or local venv)
- Construct the correct ovpackage commands with appropriate flags
- Suppress progress bars for clean output
- Handle auth configuration for different storage backends
- Follow multi-step recipes for complex workflows

## What's Included

| File | Purpose |
|------|---------|
| `SKILL.md` | Core skill: concepts, command reference, workflows, gotchas |
| `references/cli-reference.md` | Complete syntax for all 26 commands |
| `references/pipeline-recipes.md` | Step-by-step multi-command workflows |
| `references/storage-and-auth.md` | Storage URLs, auth methods, env vars |

## ovpackage Overview

ovpackage is a CLI tool for versioned asset management. It supports:

- **Creating** immutable package versions from source directories
- **Installing** packages into project workspaces with dependency tracking
- **Mirroring** packages between repositories (dev -> staging -> prod)
- **Exporting/importing** packages as tar files for air-gapped delivery
- **Cataloging and diffing** directory trees for change tracking
- **Freezing** reproducible builds from catalogs
- **Verifying** package integrity with hash checks

Supported storage backends: Amazon S3, Azure Blob Storage, Google Cloud Storage, Nucleus, and local filesystem.
