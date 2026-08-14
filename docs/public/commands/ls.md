Ls
==

## Description

Given a URL, `ls` returns all files in that subtree. It supports recursive listing and
shell-style glob pattern matching (e.g., `*.usd`, `**/*.png`).

Multiple patterns can be provided; if none are given, all entries are listed.

## Output Formats

`--output-format tsv` writes two tab-separated columns, base URL and relative
path. This format is intended for `catalog --file-list` input.

`--output-format long-tsv` writes the same columns as the normal console
output as tab-separated values: Modified, Type, Size, URL, Hash, and Version.

For CLI options, run `ovpackage ls --help`.

## Python API Reference

See the `ovpackage.ls` function docstring for the full Python API reference (signature, parameters, and error handling).
