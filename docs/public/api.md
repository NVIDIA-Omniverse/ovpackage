# Python API

ovpackage provides a Python API for programmatic access to all package management functionality.
Use it to integrate ovpackage into your build pipelines, automation scripts, or custom tools.

The API is organized into two parts:

- **Command functions** (`create`, `install`, `mirror`, etc.) — the primary operations
  for managing packages. These functions are mirrored by the CLI commands and are documented in the
  [Commands Reference](commands/index.md).
- **Supporting infrastructure** — initialization, authentication, configuration, and data
  structures, documented on this page

## Commands

See the [Commands Reference](commands/index.md) for the primary API functions including
`create`, `install`, `mirror`, `status`, and more.

## Setup and tear down

Here is an example of a main function that sets everything up for running one or more ovpackage commands
using the supplied class `ContextManager`. That class takes full responsibility for setup and tear down
and calls initialize and shutdown appropriately:

```python
import asyncio
import os
import random
import sys

import ovpackage

async def async_main():
    """
    Example main that shows how to use the ovpackage commands within an ovpackage.ContextManager block
    """
    auth_info = ovpackage.StorageAuthInfo.nucleus(
        server_url=os.getenv("HOST_URL", "omniverse://localhost"),
        username=os.getenv("OMNI_USER", "omniverse"),
        password_or_token=os.getenv("OMNI_PASS", "omniverse"),
    )
    random_version = str(random.randint(1, 1000))
    async with ovpackage.ContextManager([auth_info]) as wrapp_context:
        await ovpackage.create(
            "test_project",
            random_version,
            source=sys.argv[1],
            catalog=False,
            scheduler=wrapp_context,
        )
    print(f"Created test_project version {random_version} in {auth_info.server_url}")

asyncio.run(async_main())
```
You should use either the ContextManager class as above to manage the setup and tear down of ovpackage, or
use manual initialize and shutdown methods. The latter is only required when embedding ovpackage in a
larger existing program. See the examples in the api:Initializing ovpackage section for working example code.

See the `ovpackage.initialize` function docstring for the full Python API reference (signature, parameters, and error handling).
See the `ovpackage.shutdown` function docstring for the full Python API reference (signature, parameters, and error handling).
## Authentication and Credentials

Authentication is controlled by the class AuthManager, which has two static methods to control the number
of available credentials.

See the `ovpackage.AuthInfo` class docstring for the full Python API reference.
See the `ovpackage.StorageAuthInfo` class docstring for the full Python API reference.
See the `ovpackage.AuthManager` class docstring for the full Python API reference.
If no AuthManager is used, default client library behavior is enabled which is first checking for environment variables, and then
falling back to an interactive login flow when the server supports it. This might open a
browser window, so don't run this in non-interactive shells.

## Working with package files

For more advanced use cases, ovpackage allows to read and write the content of the ovpackage files themselves, exposed
as the `PackageInfo` class.

Any given ovpackage file can be read using the function

    package = await read_package_info(file_url)

and subsequently inspected. The file format is versioned JSON, and the best way to write a potentially modified package
back to the storage is via

   await write_package_info(destination_file_url, package)

The relevant functions with their full parameter specifications:

See the `ovpackage.read_package_info` function docstring for the full Python API reference (signature, parameters, and error handling).
See the `ovpackage.write_package_info` function docstring for the full Python API reference (signature, parameters, and error handling).
## Building a manifest

A storage-core manifest for an ovpackage file can be built with the following function, which
returns a `Manifest` describing the files of the package.

See the `ovpackage.build_manifest` function docstring for the full Python API reference (signature, parameters, and error handling).
## Ignore Rules

These classes are used to configure which files should be ignored during package operations
like `create`, `catalog`, `install`, `freeze`, `new`, and `status`.

API functions that support ignore rules accept an `ignores` parameter that can be either:

1. A **string** path or URL to an ignore file (using gitignore syntax)
2. An `IgnoreEvaluator` object for programmatic control

When `ignores` is not specified, functions automatically look for a `.wrappignore` file
in the root of the source directory being processed.

**Path resolution behavior:**

- When an ignore file path is provided, it is resolved relative to the current working
  directory (for local paths) or used as-is (for URLs).
- Patterns inside the ignore file are always resolved relative to the source directory
  being processed, regardless of where the ignore file is located.

For full documentation of the ignore file syntax, see ignore file format.

Example using a file path::

    await ovpackage.commands.create(
        "my_package", "1.0.0", "omniverse://server/source",
        catalog=False,
        ignores="/path/to/myignore.txt"
    )

Example using an IgnoreEvaluator::

    evaluator = await ovpackage.IgnoreEvaluator.create_from_file(
        "omniverse://server/config/.wrappignore",
        "omniverse://server/source"
    )
    await ovpackage.commands.create(
        "my_package", "1.0.0", "omniverse://server/source",
        catalog=False,
        ignores=evaluator
    )

See the `ovpackage.IgnoreEvaluator` class docstring for the full Python API reference.
See the `ovpackage.IgnoreFilePredicate` class docstring for the full Python API reference.
See `ovpackage.IgnorePredicate` in the source for details.
## Data Objects

These simple data objects are used by the API to carry more complex data. We use pydantic to
make it easier to write code with structs in Python.

See the `ovpackage.Catalog` class docstring for the full Python API reference.
See the `ovpackage.CatalogItem` class docstring for the full Python API reference.
See the `ovpackage.PackageInfo` class docstring for the full Python API reference.
See the `ovpackage.Dependency` class docstring for the full Python API reference.
See the `ovpackage.DependencySet` class docstring for the full Python API reference.
See the `ovpackage.LsResultItem` class docstring for the full Python API reference.
See the `ovpackage.LsResultItemType` class docstring for the full Python API reference.
See the `ovpackage.datastructures.catalog.StorageMap` class docstring for the full Python API reference.
See the `ovpackage.datastructures.catalog.Tag` class docstring for the full Python API reference.
See the `ovpackage.InstalledPackage` class docstring for the full Python API reference.
See the `ovpackage.datastructures.workspace_analysis.PackageStatusItemType` class docstring for the full Python API reference.
See the `ovpackage.datastructures.workspace_analysis.PackageStatusItem` class docstring for the full Python API reference.
The following data objects describe the manifest produced by the build-manifest command,
which lists the files of a package together with their backend resource metadata.

See the `ovpackage.Manifest` class docstring for the full Python API reference.
See the `ovpackage.ManifestFileEntry` class docstring for the full Python API reference.
See the `ovpackage.ManifestMinimalFileEntry` class docstring for the full Python API reference.
See the `ovpackage.ManifestResourceInfo` class docstring for the full Python API reference.
## Patch Operations

See the `ovpackage.Patch` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOperationType` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOpBase` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOpCopy` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOpDelete` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOpCreateFolder` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOpAddTag` class docstring for the full Python API reference.
See the `ovpackage.datastructures.patch.PatchOpDeleteTag` class docstring for the full Python API reference.
## Common Configuration

All Python API functions can be called without explicitly specifying the
context parameter. However, if any of the standard command-line configuration
options need to be specified, it is possible to provide them with a CommandParameters
object.

See the `ovpackage.CommandParameters` class docstring for the full Python API reference.
## Progress Reporting

When initializing the SchedulingParameters object, it is possible to provide
a progress report callback. It can also be configured how frequently that callback is called.
The callback receives a hierarchy of ProgressReport objects reflecting the state of the
current operations ovpackage is doing.

See the `ovpackage.ProgressReport` class docstring for the full Python API reference.
## Async operations

To avoid server overload, it is strongly recommended to make use of the
WrappScheduler context manager to create a block in which API rate throttling and
parallel execution is performed as specified by an instance of the SchedulingParameters object.

See the `ovpackage.ProgressReportVerbosity` class docstring for the full Python API reference.
See the `ovpackage.SchedulingParameters` class docstring for the full Python API reference.
See the `ovpackage.SchedulerContext` class docstring for the full Python API reference.
See the `ovpackage.ThrottlingParameters` class docstring for the full Python API reference.
See the `ovpackage.WrappScheduler` class docstring for the full Python API reference.
## Initializing ovpackage

#### Simple: ContextManager

The easiest way to initialize ovpackage in a stand-alone Python script is the ContextManager.

See the `ovpackage.ContextManager` class docstring for the full Python API reference.
#### Complex: individual calls

If you need to embed ovpackage in a larger program, using the ovpackage.ContextManager might be difficult due
to your program structure. In those cases it is possible to call the individual initialize and shutdown methods separately:

```python
import asyncio
import logging
import os
import sys

import omni.client
from rich.live import Live
from rich.logging import RichHandler
from rich.table import (
    Column,
    Table,
)

import ovpackage.api
from ovpackage import (
    CommandParameters,
    ProgressReport,
    ProgressReportVerbosity,
    SchedulerContext,
    SchedulingParameters,
    ThrottlingParameters,
)
from ovpackage.utils.progress_report import ProgressReportStatus

from .create_new_version import create_new_version

# Setup rich live display
def setup_live_display() -> Live:
    # Lazy creation to make the redirect of logging work
    _live_display = Live()
    rich_handler = RichHandler()
    rich_handler.setLevel(logging.getLogger().level)
    formatter = logging.Formatter("%(message)s")
    rich_handler.setFormatter(formatter)
    logging.getLogger().handlers = [
        rich_handler,
        *filter(lambda x: isinstance(x, logging.FileHandler), logging.getLogger().handlers),
    ]
    _live_display.start()
    return _live_display

_live_display = setup_live_display()

def print_progress(progress_report: ProgressReport):
    """
    Example function handling the progress report output.
    """
    progress_table = Table(
        "Progress  ",
        Column("Name", overflow="fold", ratio=1),
        "Discovered",
        "Running   ",
        "Done      ",
        expand=True,
    )

    def add_rows(job: ProgressReport, indent: int = 0):
        if job.status() == ProgressReportStatus.Running:
            progress_str = f"{(job.progress()) * 100:.0f}%"
        else:
            progress_str = job.status().value

        progress_table.add_row(
            progress_str,
            (" " * indent) + job.job_name(),
            str(job.total_num_tasks_executing() + job.total_num_tasks_done()),
            str(job.total_num_tasks_executing() - job.total_num_tasks_waiting()),
            str(job.total_num_tasks_done()),
        )
        if indent < 3:
            for sub_job in job.sub_jobs():
                add_rows(sub_job, indent + 1)

    # Skip the main root node called "ovpackage"
    for j in progress_report.sub_jobs():
        add_rows(j)
    _live_display.update(progress_table, refresh=True)

def main():
    """
    Example main function that shows how to use the ovpackage commands in a larger application using the initialize() and shutdown() pattern
    """
    # First, initialize the ovpackage API which will also initialize the omni.client library
    ovpackage.api.initialize(throttling_params=ThrottlingParameters(file_transfer_jobs=1))
    try:
        # Setup authentication for subsequent calls
        auth = ovpackage.AuthManager(interactive_fallback=False)
        auth.initialize()
        try:
            # Read credentials from environment variables, supplying test defaults
            auth_info = ovpackage.AuthInfo(
                server=os.getenv("HOST_URL", "omniverse://localhost"),
                username=os.getenv("OMNI_USER", "omniverse"),
                password_or_token=os.getenv("OMNI_PASS", "omniverse"),
            )
            auth.add_auth(auth_info)

            # To make sure we're not using cached credentials, force a sign-out and reconnect to the server
            omni.client.sign_out(auth_info.server)
            omni.client.reconnect(auth_info.server)

            # Define common parameters for all subsequent commands
            command_params = CommandParameters(verbose=True)

            # Define test data location
            root_url = f"{auth_info.server}/Projects/wrapp_examples"

            async def async_main():
                scheduler_context = SchedulerContext(
                    SchedulingParameters(progress_report_callback=print_progress, progress_report=ProgressReportVerbosity.normal)
                )
                scheduler_context.start_scheduler()
                try:
                    tasks = []
                    tasks.append(
                        asyncio.create_task(
                            create_new_version(
                                root_url=root_url,
                                package_name="all_projects",
                                repository=f"{auth_info.server}",
                                scheduler=scheduler_context.sub_job("Creating sample project"),
                                context=command_params,
                            )
                        )
                    )
                    results = await asyncio.gather(*tasks)
                finally:
                    await scheduler_context.stop_scheduler()

            asyncio.run(async_main())
        finally:
            # Deregister the AuthManager
            auth.shutdown()
    except ovpackage.FailedCommand as e:
        print(f"Could not create package, error message by ovpackage is: '{e}'")
    finally:
        # Shutdown the ovpackage API
        ovpackage.api.shutdown(*sys.exc_info())

if __name__ == "__main__":
    main()
```
The example function called takes the ovpackage.SchedulerContext and hands it to all ovpackage API functions called:

```python
from typing import (
    List,
    Tuple,
)
from urllib.parse import urlparse

import semver

import ovpackage
from ovpackage import (
    CommandParameters,
)

def calculate_next_version(versions: List[str], version_part: str) -> Tuple[str, str]:
    """
    Given a list of strings with version numbers and the version part to increment (major, minor, patch), returns a pair of latest version and the next version
    """
    sem_versions = [semver.Version.parse(v) for v in versions]
    latest_version = sorted(sem_versions)[-1]
    return str(latest_version), str(latest_version.next_version(version_part))

async def create_new_version(
    root_url: str, package_name: str, repository: str, scheduler: ovpackage.SchedulerContext, context: CommandParameters()
) -> None:
    """
    Example function that creates a package in omniverse://localhost/.packages, calculating the next minor semver version from listing the existing packages
    in that repository. If no package is found, it starts with 1.0.0. This uses the semver package to parse version numbers and calculate the next minor version.

    :param root_url: URL of source package
    :param package_name: name to give to the package.
    :param repository: URL of the repository
    :param scheduler: pre-constructed SchedulerContext object determining the amount of jobs to run
    :param context: command context to use
    """
    # Calculate the next version number
    packages_in_repo = await ovpackage.list_repo(repository, scheduler=scheduler)
    next_version = None
    for package, versions in packages_in_repo:
        parsed_url = urlparse(package)
        path_parts = parsed_url.path.split("/")
        if path_parts[-1] == package_name:
            if len(versions) > 0:
                latest_version, next_version = calculate_next_version(versions, "minor")
                print(f"Latest version of package {package_name} is {latest_version}, using {next_version}")
                break
    if next_version is None:
        print(f"No prior version found for {package_name}, starting at 1.0.0")
        next_version = "1.0.0"

    print(f"Creating package {package_name} version {next_version}")
    await ovpackage.create(package_name, next_version, source=root_url, catalog=False, repo=repository, context=context, scheduler=scheduler)
    print("Done")
```
