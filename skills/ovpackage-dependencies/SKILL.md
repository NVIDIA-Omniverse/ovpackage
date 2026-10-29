---
name: ovpackage-dependencies
description: Track, pin, inspect, and update the package dependencies of an ovpackage project, compose dependency sets as metapackages, and lock data sets with integrity checks and storage-core manifests. Use when a user wants to record which asset package versions a scene, shot, simulation, or training run uses; update or roll back a dependency to a specified version; find what is installed and where it came from; handle local edits that block an update; bundle a fixed set of versions; or produce a manifest for a locked data set.
license: Apache-2.0
compatibility: Run where ovpackage is installed with the extras for the repositories in use. Examples use local paths; any supported repository URL works.
---

# ovpackage dependencies

A project's dependencies live in its **package metadata file**, `.<project>.wrapp`. Each dependency entry records the package name, the exact version, its install location relative to the project, and the repository it was installed from. Because installed content sits at a version-free path, USD references in the project never change when a dependency's version does. ovpackage records the dependency; it never rewrites asset paths inside files.

For exact option names, read [cli-reference.md](../ovpackage-cli/references/cli-reference.md). For why and when to lock content, read the expert's [locked data sets](../ovpackage-expert/references/locked-datasets.md).

## Start tracking a project

```bash
ovpackage new shot010 1.0.0 ./shot010
ovpackage install textures 1.0.0 ./shot010/assets/textures \
    --repo ./studio_repo --package ./shot010/.shot010.wrapp --progress-report off
ovpackage install props 1.0.0 ./shot010/assets/props \
    --repo ./studio_repo --package ./shot010/.shot010.wrapp --progress-report off
```

`new` creates the project metadata file only. Each `install --package` installs one package and records it as a dependency. Without `--package`, the content installs but the project does not record it.

## Inspect what is pinned

| Question | Command | Reading the result |
| --- | --- | --- |
| Which versions does the project use, and where? | `ovpackage installed-package-list ./shot010/.shot010.wrapp` | One row per package: name, version, install path, repository. `--direct-only` limits it to direct dependencies. |
| Has anything changed locally? | `ovpackage status ./shot010/.shot010.wrapp` | `all clean` and exit `0` when nothing differs. Exit `1` lists modified packages, and also reports an unpublished project as a `draft package`. |
| Where did this content come from? | `ovpackage traceback <installed .wrapp>` | Follows the recorded repository and source pointers back through mirrors to the original source. |

For automation, use `status --output-format tsv`. Each row carries an error type, such as `package_modified` or `package_no_catalog`, the latter for a draft project. Gate on the types you care about rather than on the exit code alone; [pipelines](../ovpackage-pipelines/SKILL.md#gate-a-job-on-package-state) shows the pattern.

## Update or roll back to a specified version

1. Run `status` on the project file. Proceed when the package you are updating is clean.
2. Install the target version at the **same** location through the same project file:

   ```bash
   ovpackage install textures 2.0.0 ./shot010/assets/textures \
       --repo ./studio_repo --package ./shot010/.shot010.wrapp --progress-report off
   ```

3. Verify with `installed-package-list`, which shows the new version, and with `status` on the installed package's own `.wrapp` file, which reports `all clean`.

The same command with an older version rolls back. Preview any update with `--dry-run`, which writes nothing.

**If local edits block the update,** `install` exits `1` and names the conflicting files. Choose one approach with the user:

- **Keep the edits as new content:** copy them out, update, then publish the edited content as a new version of that package.
- **Review and apply:** rerun the install with `--patch conflicts.patch`, which writes a JSON patch and installs nothing. Review it, then `ovpackage apply-patch conflicts.patch`.
- **Merge three ways:** extract catalogs and run `create-patch`; see recipe 3 in [pipeline-recipes.md](../ovpackage-cli/references/pipeline-recipes.md#3-handle-install-conflicts-with-patches).
- **Discard the edits:** `install --force` overwrites them. Do this only with explicit approval.

## Publish the project with its dependencies

Run `ovpackage create shot010 1.0.0 ./shot010 --repo ./studio_repo` to publish the project. The published version records its dependencies, so installing `shot010 1.0.0` anywhere reproduces the whole set. `create` fails if a dependency is modified, unless you pass `--allow-dirty-subpackages`. `--check-subpackages` verifies that dependencies exist in the target repository. After `create`, the working copy's `status` still reports a draft; the published version is in the repository.

## Pin a set of versions as a metapackage

A metapackage contains only dependencies: a named, versioned bill of materials. Write a `DependencySet` JSON file:

```json
{
  "format_version": {"version_string": "1.0"},
  "dependencies": [
    {"package": "textures", "version": "1.0.0", "relative_destination": "textures", "repository": "file:///studio_repo"},
    {"package": "props", "version": "1.0.0", "relative_destination": "props", "repository": "file:///studio_repo"}
  ]
}
```

Then create and consume it:

```bash
ovpackage create-metapackage lookdev_kit 2026.10.13 --repo ./studio_repo --json deps.json --progress-report off
ovpackage install lookdev_kit 2026.10.13 ./consumer/kit --repo ./studio_repo --progress-report off
```

Installing the metapackage installs every pinned dependency under its `relative_destination`. A CSV file with the columns `name`, `version`, `path`, and `repo` works through `--csv`. Dependencies may point at several repositories; consumers need access to all of them, or use `mirror --recursive` to collect them in one place first. Metapackages carry no package-standard sidecars.

## Lock a data set

| Guarantee | Mechanism | Command |
| --- | --- | --- |
| Content of a version never changes | Published versions are immutable; recreating one fails | `create` |
| A project names exact versions | Dependency entries in `.wrapp` | `install --package`, `installed-package-list` |
| The set itself is versioned | Metapackage or published project | `create-metapackage`, `create` |
| Stored bytes match what was published | Content hashes in the catalog | `check NAME VERSION --repo URL --recursive` (exit `1` on mismatch) |
| An install still matches its version | Catalog comparison | `status` |
| Storage-core consumers can address each file durably | Manifest with Storage API identities | `build-manifest WRAPP_FILE OUTPUT` |

`build-manifest` writes a storage-core manifest (wire format 1.0) of a package and its dependencies, with relative paths and durable Storage API identities. Every file needs a `resource_identity_v2`, so it works for Storage API repositories. On local, S3, Azure, GCS, and other backends it fails with `lacks resource_identity_v2`. For legacy repository catalogs it reconstructs missing identities by default, which can download every file and validates them against recorded hashes; `--no-recalculate-identities` requires identities already recorded. `--minimal-format` omits size and modification time. The Python equivalent is `ovpackage.build_manifest(wrapp_file)`, which returns an `ovpackage.Manifest`. The details are under "Building a manifest" in the [API guide](../../docs/public/api.md).

## Clean up

- `ovpackage uninstall NAME INSTALL_LOCATION --package PROJECT.wrapp` removes an installed package and its dependency record; it refuses if the installed copy was edited.
- `find-orphans` lists repository versions that no seed package references. Seeds must be `.wrapp` files inside a repository, not workspace files. Review the list before `delete-packages`, run `delete-packages --dry-run` first, and get approval. Deleting a version breaks every package that depends on it.

## Python API

`install(..., package=...)`, `status`, `installed_package_list`, `traceback`, `create_metapackage(name, version, repo, DependencySet(...))`, `check_package_content`, and `build_manifest` mirror the commands above. Read [ovpackage-api](../ovpackage-api/SKILL.md) for the async `ContextManager` pattern, and the expert's [library map](../ovpackage-expert/references/library-map.md) for every command-to-function pairing.
