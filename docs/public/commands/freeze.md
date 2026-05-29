# Freeze

## Description

Taking a source and a destination, freeze a version of a package for later publishing or preparing a reproducible build.

The freeze command is used to freeze or archive a specific version into a new location. This is used to make sure a
specific version can be reproducibly addressed at that location, e.g. to run a CI job on a specific version or to
create a reproducible version for QA testing and subsequent release.

The freeze command has two modes:

**Mode 1: URL to URL**

Takes a source subtree URL and creates a copy of the head version of the files at the source position. If both source and destination are on the same server with copy-on-server support, the operation is efficient as no data has to be transferred and the
files and folders at the new destination are effectively hard links to the same content, causing no data duplication.
Note that the history is not copied and the checkpoint numbers will not be the same as in the source.

**Mode 2: Catalog to URL**

Takes a catalog file as input and a destination path as second parameter, using the `--catalog` flag.

Note that while this allows to defer the copy command to a later point and only catalog the files to be used as a
first step, there is no guarantee that the freeze operation will still be able to find all files listed in the
catalog - they might have been moved away or obliterated.

Examples::

    # URL to URL mode
    ovpackage freeze omniverse://server/source/path omniverse://server/frozen/path

    # Catalog to URL mode
    ovpackage freeze --catalog catalog.json omniverse://server/frozen/path

Note that write permissions are required on the destination.

For usage examples, see the [Tutorial](../tutorial.md). For CLI options, run `ovpackage freeze --help`.

## Python API Reference

See the `ovpackage.freeze` function docstring for the full Python API reference (signature, parameters, and error handling).
