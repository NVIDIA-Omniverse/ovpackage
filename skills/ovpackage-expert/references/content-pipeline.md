Up: [ovpackage-expert](../SKILL.md)

# ovpackage in a USD content pipeline

Use this to explain when, why, where, and how versioned packages fit into a pipeline that authors, assembles, and consumes OpenUSD content. Commands are in [ovpackage-dependencies](../../ovpackage-dependencies/SKILL.md) and [ovpackage-pipelines](../../ovpackage-pipelines/SKILL.md). Sources: the [manual](../../../docs/public/manual.md), the [tutorial](../../../docs/public/tutorial.md), and the [install command](../../../docs/public/commands/install.md).

## The problem it solves

A USD stage is assembled from many files: root layers, sublayers, references, payloads, materials, and textures. Each arc names an asset path that the resolver opens. When those files live in shared folders that keep changing, a scene silently picks up whatever is there today. Nobody can say which texture revision a render or a training run used, and an upstream fix can break a downstream scene without warning.

ovpackage freezes each content set as an immutable **version**, installs exact versions into the places that consume them, and records which versions each consumer uses. Content changes become explicit events: publish a new version, then decide which consumers move to it.

## What ovpackage does and does not do with USD

- It **copies files and records metadata**; it never opens or rewrites USD layers. Asset paths inside your files stay exactly as authored.
- It **installs at version-free paths.** A package installs at a stable location such as `shot010/assets/textures/`, so layers reference `@./assets/textures/wood.png@` regardless of which version is installed. Updating a dependency changes the files at that path and the recorded version, not the references.
- It **does not discover dependencies from USD.** It doesn't follow references to find the files a stage needs. Include every file a package's layers reference, or record the package that provides them as a dependency.

## Authoring rules that make packages portable

1. **Keep references inside a package relative** (`@./materials/oak.mdl@`, `@../textures/wood.png@`), so a version works wherever it is installed. Absolute local paths and URLs that point back into a working area break once content moves.
2. **Make cross-package references explicit dependencies.** Dependencies install at a recorded `relative_destination`, such as `assets/textures` and `assets/props`, so relative paths between them stay valid within that project. If a package's own layers reference another package, make that package its dependency: install it inside the package's directory with `--package`, then `create`. That way the reference resolves wherever the package is installed, instead of only in one project's layout.
3. **Keep local overrides out of installed packages.** Put a shot's changes in the shot's own layers, which reference or sublayer the installed content. Edits made inside an installed package show up in `status` and block updates.
4. **Package at the granularity you version.** One package per independently released unit: an asset, an asset library or kit, an environment, or a shot or scene. Content that always changes together belongs in one package; content with separate owners or release cadences belongs in separate packages joined by dependencies.
5. **Exclude working files** such as caches, thumbnails, and logs with `.wrappignore`.

## A typical flow

```text
author ──create──▶ studio repository ──mirror──▶ release repository ──install──▶ consumers
 (DCC, tools)      (immutable versions)         (promoted versions)            (scenes, sims, CI)
                         ▲                                                       │
                         └─────── publish a new version ◀── status / update ─────┘
```

1. **Author** in a working directory or storage location with any DCC or tool.
2. **Publish** a version with `create` when the content is ready for others. This is a review gate: the version is what everyone downstream will see.
3. **Assemble** a scene or project. Create its metadata file with `new`, then `install --package` each dependency at a fixed location. Author the scene's layers against those paths.
4. **Consume:** renderers, simulators, synthetic-data jobs, and evaluations install the project, or a metapackage, at an exact version and record it with their outputs.
5. **Update deliberately:** when a dependency publishes a new version, check `status`, install the new version at the same location, validate the scene, then publish the project as a new version if others depend on it.
6. **Promote and deliver:** `mirror --recursive` to release or site repositories; `export`/`import` or repository transfer for offline sites.

## When to use packages, and when not to

| Use packages for | Why |
| --- | --- |
| Shared asset libraries consumed by several projects | Each project pins and updates independently |
| Scenes, environments, or data sets that must be reproduced later | Exact versions plus integrity checks reproduce the content |
| Content crossing a boundary: team, site, customer, or offline network | Versions move as units with their dependencies |
| CI and batch jobs | Jobs install known content instead of reading a moving folder |

| Don't use packages for | Use instead |
| --- | --- |
| Live, multi-user editing of the same layers | A collaboration service; publish a version when work is ready to share |
| Single files that change every few seconds | The storage layer directly |
| Fast repeated reads near compute | A cache in front of storage; see [related repositories](related-repositories.md) |

## Explaining dependency tracking and updates

- Each project's `.wrapp` file is its bill of materials. `installed-package-list` prints it, and `traceback` explains where each item came from.
- `status` tells you whether the installed content still matches its versions, which is the precondition for a safe update.
- Updating means installing a **specified** version at the same location. Rolling back is the same command with the older version. Nothing updates implicitly.
- Publishing the project captures the whole dependency set as one version, so `install project 1.0.0` reproduces it.
