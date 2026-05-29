# Ignore Rules

Several ovpackage commands support ignore rules to exclude files from processing. This applies to
commands like `create`, `catalog`, `install`, `freeze`, `new`, and `status`.

Ignore rules can be specified via:

- **CLI**: The `--ignore-file` parameter
- **Python API**: The `ignores` parameter (accepts a file path/URL or an `IgnoreEvaluator` object)

By default, commands look for a file called `.wrappignore` in the root directory of the data
being processed. The ignore file uses the same format as `.gitignore` files.

> IMPORTANT:
>
> **Difference from** `.gitignore`: Unlike `.gitignore` files which can be placed in any
> subdirectory and affect files relative to that location, ovpackage currently only reads
> `.wrappignore` from the root directory of the source data. Placing `.wrappignore` files
> in subdirectories has no effect.
>
>
## Basic Usage

To ignore all thumbnail directories, create a file called `.wrappignore` containing::

    .thumbs

You can also add `.wrappignore` itself so the ignore file is not included in packages::

    .thumbs
    .wrappignore

## Specifying a Custom Ignore File

If you don't want to modify the source data or when the same ignore rules should be shared
across multiple packages, you can specify a different ignore file using the `--ignore-file`
parameter (CLI) or `ignores` parameter (Python API). The ignore file can be specified as:

- A local absolute path: `--ignore-file /path/to/ignore.txt`
- A local relative path (resolved against current working directory): `--ignore-file myignore.txt`
- A remote URL: `--ignore-file omniverse://server/path/to/ignore.txt`

Regardless of where the ignore file is located, the patterns inside it are always resolved
relative to the root of the source directory being processed.

> NOTE:
>
> When using a relative path for `--ignore-file` (e.g., `--ignore-file myignore.txt`),
> it is resolved against your current local working directory, NOT against the source
> directory being processed. This allows you to keep ignore files locally while processing
> remote data.
>
>
## Pattern Syntax

The ignore file uses the same format as `.gitignore` files, supporting:

- **Wildcard patterns**: `*.txt` matches all `.txt` files, `*.auto.*` matches files with `.auto.` in their name
- **Directory patterns**: `target/` (with trailing slash) matches directories named `target` and excludes them entirely. This can significantly speed up operations if you have large directories that don't need to be scanned by ovpackage.
- **Recursive content patterns**: `target/**` matches all files within `target` and its subdirectories
- **Single-level wildcards**: `ovpackage/*` matches files directly in `ovpackage/` but not in subdirectories
- **Negation rules**: `!keep.txt` excludes `keep.txt` from being ignored (even if a previous pattern matched it)
- **Comment lines**: Lines starting with `#` are ignored
- **Leading slash**: `/target/` anchors the pattern to the root directory only

## Pattern Matching Subtleties

**Pattern matching scope**

If a pattern contains a path separator (`/`) at the beginning or middle, it only matches
relative to the `.wrappignore` location. Otherwise, it matches at any directory level.
For example:

- `target/` matches directories named `target` at **any level** in the tree
- `target/**` or `/target/` only matches a `target` directory at the **root level**

**Negation and directory exclusion**

Negation rules cannot re-include a file if its parent directory is excluded. When a directory
pattern like `target/` is used, the directory is skipped entirely for performance, so
`!target/file.txt` has no effect.

However, if you use `target/**` (which matches the contents but not the directory itself),
negation rules can re-include specific files from the `target/` directory itself. To re-include
files from subfolders, you need to explicitly re-include those subfolders using a negation
**without** a trailing slash.

For example, to include only `target/sub/file.txt` while excluding everything else in `target/`::

    target/**
    !target/sub
    !target/sub/file.txt

## Python API

For programmatic control over ignore rules, see the `IgnoreEvaluator` and
`IgnoreFilePredicate` classes in the [Python API documentation](api.md).
