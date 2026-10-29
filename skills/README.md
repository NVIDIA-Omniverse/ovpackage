# ovpackage agent skills

Back to the [ovpackage README](../README.md).

Agent skills for using, automating, and explaining ovpackage. Start with the expert; it routes to the others. Agents that discover skills under `.agents/skills/` or `.claude/skills/` find these through links to this directory; otherwise open a `SKILL.md` directly.

- [ovpackage-expert](ovpackage-expert/SKILL.md) — start here: explains the library, designs package flow through USD content pipelines and repositories, and locks data sets. Routes to the skills below.
- [ovpackage-cli](ovpackage-cli/SKILL.md) — runs commands, with a complete [command reference](ovpackage-cli/references/cli-reference.md), [storage and authentication notes](ovpackage-cli/references/storage-and-auth.md), and [multi-command recipes](ovpackage-cli/references/pipeline-recipes.md).
- [ovpackage-api](ovpackage-api/SKILL.md) — calls the async Python API with `ContextManager`.
- [ovpackage-dependencies](ovpackage-dependencies/SKILL.md) — records, inspects, updates, and rolls back project dependencies; metapackages; integrity checks and manifests.
- [ovpackage-pipelines](ovpackage-pipelines/SKILL.md) — CI publishing and gates, promotion between repositories, reproducible checkout, offline and public distribution.
- [ovpackage-storage](ovpackage-storage/SKILL.md) — chooses, authenticates, and verifies storage backends.
- [ovpackage-troubleshooting](ovpackage-troubleshooting/SKILL.md) — diagnoses failures and upgrades the library safely.

The [root agent entry](../AGENTS.md) sets the ground rules for working in this repository.
