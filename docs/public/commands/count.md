# Count

## Description

Count files and analyze statistics for a catalog file.

This is a helpful command to understand how many files exist in a catalog that will be used to create a package,
as well as the on-disk size and duplication stats.

For CLI options, run `ovpackage count --help`.

> NOTE:
>
> This command is available only via the CLI. For Python API usage, load the catalog directly
> using `Catalog` and inspect its `items` list::
>
>     import ovpackage
>
>     catalog = await ovpackage.Catalog.load("my_catalog.json")
>     print(f"Number of files: {len(catalog.items)}")
>     print(f"Total size: {sum(item.size or 0 for item in catalog.items)}")
