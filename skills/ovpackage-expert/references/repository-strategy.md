Up: [ovpackage-expert](../SKILL.md)

# One repository or many

Use this to decide where package versions live: one repository, several repositories with dependencies between them, or copies distributed to other teams, sites, or customers. Commands are in [ovpackage-pipelines](../../ovpackage-pipelines/SKILL.md); backends in [ovpackage-storage](../../ovpackage-storage/SKILL.md). Sources: the [manual](../../../docs/public/manual.md#package-distribution), the [tutorial](../../../docs/public/tutorial.md), and the [mirror](../../../docs/public/commands/mirror.md) and [create-metapackage](../../../docs/public/commands/create-metapackage.md) commands.

## What a repository is

A repository is any storage location with a `.packages` folder. One storage account or server can hold several repositories, `--repo` chooses one per command, and a package's dependencies may come from different repositories. Each dependency records the repository it was installed from, so consumers need read access to every repository named in a dependency tree, or a copy of it.

## Start with one repository

One repository is the default for a team or studio whose producers and consumers share storage and access rules.

- Every version has one address, and `list-repo` shows everything available.
- Dependencies never cross access boundaries.
- `find-orphans` and `delete-packages` cleanup covers the whole set.

## Split or distribute when a boundary requires it

| Situation | Pattern | How |
| --- | --- | --- |
| Work in progress must not reach consumers | **Promotion:** dev → staging → release repositories | Publish to dev; `mirror --recursive` the tested version onward; consumers install only from release |
| Teams own different content and access rules | **Per-owner repositories** with cross-repository dependencies | Each team publishes to its own repository; projects depend on versions from several; metapackages may span repositories |
| Consumers are far from the source, or the source must not take their load | **Site mirrors** | `mirror --recursive` into a repository near each site; consumers install with `--repo` pointing at their site |
| A site has no network path to you | **Offline delivery** | `export`/`import` per package, or move a file-system repository built with `mirror --recursive` |
| Content is distributed publicly or through a CDN | **Redirect repository** | `publish-redirect --external-url` writes metadata-only packages whose files resolve to the CDN; strip internal sources with `--strip-source` or `--canonical-source` |
| Packages published on Hugging Face | **Read-only source** | `list-repo`, `install`, or `mirror --source-repo` from `hf://datasets/...`; never a write target |

## Trade-offs to state

- **Copies are cheap to trust:** mirrored versions keep their content hashes, `check --recursive` verifies them, and `mirror --template-version` transfers only the changes since a previous version.
- **Cross-repository dependencies need access everywhere.** If a consumer can't reach one repository, `mirror --recursive` the tree into one it can reach, and dependencies are rewritten to the destination. `--repo-mapping` routes dependencies from other source repositories to chosen destinations.
- **More repositories, more cleanup scope.** Run `find-orphans` per repository with seeds from every consumer.
- **Writes don't coordinate across repositories.** Serialize writes per repository, and promote by copying versions, never by publishing the same name and version independently in two places.
- **Provenance travels with packages.** `.wrapp` files record source and repository URLs; decide whether those may leave your organization before distributing.

## Recommendation shape

State the repositories, what each holds, who writes and who reads each one, how versions move between them (which command, and when), and how consumers verify them (`check --recursive`, then `status` after installing).
