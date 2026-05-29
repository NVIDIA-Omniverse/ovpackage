# New

## Description

Takes package name and version, and creates a new .<package>.wrapp file.

The new command does not operate on any files or packages, it rather is a shortcut to create a suitable
`.<package>.wrapp` file to be used by subsequent install-commands.

For instance, when creating a new scenario and wanting to capture the asset packages used, it is useful to
have an ovpackage file that will record the dependencies installed.

Example::

    ovpackage new my_package 1.0.0
    # Creates .my_package.wrapp in the current directory

`.wrapp` files are JSON files. The file format includes a schema version field and may evolve with new ovpackage versions.

For CLI options, run `ovpackage new --help`.

## Python API Reference

See the `ovpackage.new` function docstring for the full Python API reference (signature, parameters, and error handling).
