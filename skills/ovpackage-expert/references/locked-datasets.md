Up: [ovpackage-expert](../SKILL.md)

# Locked data sets

Use this to explain why and how to lock the content a scene, simulation, synthetic-data job, training run, or evaluation depends on, and what a manifest adds. The commands are in [ovpackage-dependencies](../../ovpackage-dependencies/SKILL.md#lock-a-data-set). Sources: the [manual](../../../docs/public/manual.md), the "Building a manifest" and manifest data objects sections of the [API guide](../../../docs/public/api.md), and the [check](../../../docs/public/commands/check.md) and [create-metapackage](../../../docs/public/commands/create-metapackage.md) commands.

## Why lock

A result is only reproducible if its inputs are. When inputs come from shared folders, rerunning a job months later, or on another site, quietly uses different content. A locked data set names every input by an immutable version, so a run can record exactly what it used and later reproduce or audit it.

## What locks what

1. **Immutable versions** lock each package's content. A published name and version never changes; recreating it fails.
2. **Dependency records** lock which versions a project uses. A project's `.wrapp` file names exact versions and install locations.
3. **A versioned set** locks the whole bill of materials under one name:
   - a **metapackage** (`create-metapackage`) pins versions with no files of its own, which suits "the data set used by run 42";
   - a **published project** (`create` on a workspace with installed dependencies) pins its own files plus its dependencies.
4. **Integrity checks** prove the bytes still match. `check NAME VERSION --repo URL --recursive` compares stored files with recorded hashes and exits `1` on a mismatch. After installing, `status` confirms the installed copy is unmodified.
5. **A storage-core manifest** (`build-manifest`) describes the files of a package and its dependencies by relative path and durable Storage API identity, for consumers that read storage-core manifests. It requires every file to have a durable Storage API identity, so it applies to Storage API repositories; other backends rely on steps 1–4.

Steps 1–4 work on every backend; step 5 adds an identity-based description where a Storage API service is in use.

## Pattern: lock, use, record, reproduce

1. Publish or select the input versions.
2. Pin them: `create-metapackage dataset_sdg 2026.10.13 --repo URL --json deps.json`.
3. Verify: `check dataset_sdg 2026.10.13 --repo URL --recursive`.
4. Materialize for the job: `install dataset_sdg 2026.10.13 ./job/data --repo URL`, then `status` on the installed `.dataset_sdg.wrapp`.
5. Record `dataset_sdg 2026.10.13` and the repository with the job's outputs.
6. To reproduce, install the same name and version, from the same repository or a mirror, and verify again.

To move a locked set to another site, `mirror --recursive` it and run `check --recursive` at the destination. `import` cannot yet restore a metapackage archive; see [offline delivery](../../ovpackage-pipelines/SKILL.md#deliver-to-an-offline-site).

## Limits to state plainly

- ovpackage locks the files you package. It doesn't discover files a USD stage references from outside the package, so include them or depend on the packages that contain them.
- A lock is only as strong as repository immutability: never edit `.packages/` contents, and restrict who can delete versions. `delete-packages` breaks every set that depends on the deleted version.
- Hugging Face inputs are read through `hf://datasets/<owner>/<repo>[@<revision>]/...`. Pin a revision there when the upstream repository can change.
- Package-standard sidecars are not written for packages with dependencies, which includes metapackages.
