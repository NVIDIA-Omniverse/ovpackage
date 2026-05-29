# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import inspect
import logging
import math
from dataclasses import (
    dataclass,
    fields,
)
from typing import (
    Any,
    Dict,
    Iterable,
    Optional,
    Tuple,
)

import typer

from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.cli_progress_printer import (
    CLIProgressPrinter,
    ProgressReportVerbosity,
)
from wrapp.utils.progress_report import ProgressReportCallback
from wrapp.utils.progress_report_generator import ProgressReportGenerator
from wrapp.utils.router import Router
from wrapp.utils.scheduler_task_node import (
    SchedulerRootNode,
    SchedulerTreeNode,
)

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")


def _validate_positive_int(value: Any, field_name: str) -> None:
    """Validate that a value is a positive integer (not bool, inf, nan, etc.).

    Args:
        value: The value to validate.
        field_name: The name of the field for error messages.

    Raises:
        TypeError: If the value is not an integer or is a boolean.
        ValueError: If the value is not positive, inf, or nan.
    """
    # Check for bool first since bool is a subclass of int in Python
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer, not {type(value).__name__}")

    if not isinstance(value, int):
        # Handle float special cases with informative messages
        if isinstance(value, float):
            if math.isinf(value):
                raise ValueError(f"{field_name} must be a finite positive integer, not infinity")
            if math.isnan(value):
                raise ValueError(f"{field_name} must be a finite positive integer, not NaN")
            raise TypeError(f"{field_name} must be an integer, not float")
        raise TypeError(f"{field_name} must be an integer, not {type(value).__name__}")

    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer (> 0), got {value}")


@dataclass
class SelfFillingParameterClass:
    @classmethod
    def from_params(cls, stack_depth: int = 1) -> Dict:
        """
        Constructs a Dataclass object by extracting the values of parameters of the calling function and storing them in the fields
        of this class. This can be used to easily extract the common parameters and make them available to the API functions.

        :parameter stack_depth: specify how many levels in the stack the function should go up to find the parameters used to fill the dataclass
        """
        current_frame = inspect.currentframe()
        if current_frame is None:
            raise RuntimeError("no current frame found for construction of this SelfFillingParameterClass")

        # Walk up the stack frame the specified number of times
        frame = current_frame
        for i in range(stack_depth):
            if frame.f_back:
                frame = frame.f_back
            else:
                raise RuntimeError(f"Can't walk up the stack {stack_depth} times, program error")

        caller_locals: Optional[Dict[str, Any]] = frame.f_locals
        if caller_locals is not None:
            all_params = caller_locals.keys()
            given_params: Iterable[str] = filter(lambda x: x in all_params, [field.name for field in fields(cls)])
            context_parameters = {param_name: caller_locals[param_name] for param_name in given_params}
        else:
            context_parameters = {}

        return context_parameters


@dataclass
@wrapp_public_api
class CommandParameters(SelfFillingParameterClass):
    """
    Class storing all common parameters for wrapp commands.

    Construct this class with CommandParameters(<keyword params>).
    For a more detailed explanation for the individual parameters, please
    refer to the section  :ref:`generic-parameters-label`.
    """

    # Internally we also use CommandParameters.from_params before calling wrapp API functions.

    debug: bool = False  #: Debug mode.
    verbose: bool = False  #: Verbose.
    dry_run: bool = False  #: Dry run.
    log_file: Optional[str] = None  #: The path to the log file.
    hash_cache_file: Optional[str] = None  #: The path to the hash cache.


@dataclass
@wrapp_public_api
class ThrottlingParameters(SelfFillingParameterClass):
    """
    Class storing all common parameters for rate limiting for wrapp commands.

    Construct this class with ThrottlingParameters(<keyword params>).
    For a more detailed explanation for the parameters related to the job numbers, please refer to the section :ref:`generic-parameters-label`.
    """

    # Internally we also use ThrottlingParameters.from_params before calling wrapp API functions
    jobs: int = 200
    tagging_jobs: int = 50
    file_transfer_jobs: int = 10

    def __post_init__(self) -> None:
        """Validate that all job parameters are positive integers."""
        _validate_positive_int(self.jobs, "jobs")
        _validate_positive_int(self.tagging_jobs, "tagging_jobs")
        _validate_positive_int(self.file_transfer_jobs, "file_transfer_jobs")


@dataclass
@wrapp_public_api
class SchedulingParameters(SelfFillingParameterClass):
    """
    Class storing all parameters for constructing schedulers and rate limiting for wrapp commands.

    Construct this class with SchedulingParameters(<keyword params>).
    Hand this into the run_scheduler() method of the SchedulerContext class to construct an appropriate scheduling setup
    For a more detailed explanation for the parameters related to the job numbers, please refer to the section :ref:`generic-parameters-label`.
    """

    # Internally we also use SchedulingParameters.from_params before calling wrapp API functions

    jobs: int = 200  #: The number of concurrent roundtrip jobs. Deprecated in favour of ThrottlingParameters.
    tagging_jobs: int = 50
    file_transfer_jobs: int = 10  #: The number of concurrent file transfer jobs. Deprecated in favour of ThrottlingParameters.

    progress_report: ProgressReportVerbosity = ProgressReportVerbosity.off
    job_name: Optional[
        str
    ] = None  #: The job name under which all tasks for the executed operation appear in the ProgressReport. Default is "WRAPP".
    progress_report_callback: Optional[
        ProgressReportCallback
    ] = None  #: Callback which can be used to monitor the progress of the operation. See ProgressReport for more details.
    progress_report_interval: Optional[
        float
    ] = None  #: Interval in seconds between calls to the progress_report_callback. If 0, the callback is called as frequently as possible. Default 0.2.

    def __post_init__(self) -> None:
        """Validate that all job parameters are positive integers."""
        _validate_positive_int(self.jobs, "jobs")
        _validate_positive_int(self.tagging_jobs, "tagging_jobs")
        _validate_positive_int(self.file_transfer_jobs, "file_transfer_jobs")


def context_from_params() -> Tuple[CommandParameters, SchedulingParameters]:
    """
    Constructs both the CommandParameters and the SchedulingParameters object by extracting the values of parameters of the calling function
    and storing them in the fields of the two data classes.
    This can be used to easily extract the common parameters and make them available to the API functions from the command functions.
    """
    return CommandParameters(**CommandParameters.from_params(stack_depth=2)), SchedulingParameters(
        **SchedulingParameters.from_params(stack_depth=2)
    )


@wrapp_public_api
class SchedulerContext:
    """
    All WRAPP operations executed using the same SchedulerContext will be reported together by the progress reporting.
    Use this class directly if it is not convenient to use the :class:`WrappScheduler` context manager class.

    Make sure to call start_scheduler() and stop_scheduler() accordingly:

    >>> import wrapp
    >>> async def example(root_dir: str) -> wrapp.Catalog:
    >>>     scheduler_context = SchedulerContext(SchedulingParameters())
    >>>     scheduler_context.start_scheduler()
    >>>     try:
    >>>         catalog = await catalog(root_dir, scheduler=scheduler_context)
    >>>     finally:
    >>>         await scheduler_context.stop_scheduler()
    >>>     return catalog
    """

    @log_args
    def __init__(self, params: Optional[SchedulingParameters] = None) -> None:
        self._parameters = params or SchedulingParameters()
        self._root_task_node: SchedulerRootNode = SchedulerTreeNode.create_root(self._parameters.job_name)
        # The task node which is used as the parent for all newly created nodes - this is used if one main public API function calls another.
        self._parent_task_node: SchedulerTreeNode = self._root_task_node
        self._progress_reporter: Optional[ProgressReportGenerator] = None

    @log_args
    def get_parameters(self) -> SchedulingParameters:
        """
        :return: Retrieve the SchedulingParameters which were used to construct this SchedulerContext
        """
        return self._parameters

    @log_args
    def start_scheduler(self) -> None:
        """
        Initialize the  progress reporting.
        Use this in standalone usage, and make sure to call stop_scheduler in a 'finally' block.
        """

        # This is a hack to allow the old method of setting the job parameters through SchedulingParameters for each public
        # API call to still work, if the function is called in isolation, i.e. no other functions are currently running.
        # "reconfigure_throttlers" will do nothing if the provided parameters are the same as before, but if they
        # are different, it will check if the throttlers currently have open tasks, and if so log a warning.
        # If the throttlers have no open tasks, it will recreate them with the new job numbers provided by SchedulingParameters.
        # In the long run the idea is to remove the job numbers from SchedulingParameters, and only rely on the job numbers
        # provided to wrapp_initialize.
        if not Router.me().reconfigure_throttlers(
            ThrottlingParameters(
                jobs=self._parameters.jobs,
                tagging_jobs=self._parameters.tagging_jobs,
                file_transfer_jobs=self._parameters.file_transfer_jobs,
            )
        ):
            logger.warning(
                "Failed to reconfigure throttlers with new jobs parameters,"
                "which were different from the previous set of parameters. Please set the jobs parameters once in wrapp.initialize,"
                "if possible, and leave them otherwise unchanged. The jobs parameters in SchedulingParameters are deprecated and will be removed in a future version."
            )
        if self._parameters.progress_report_callback is not None and self._parameters.progress_report != ProgressReportVerbosity.off:
            self._progress_reporter = ProgressReportGenerator(
                self._root_task_node, self._parameters.progress_report, self._parameters.progress_report_interval
            )
            self._progress_reporter.start_progress_report(self._parameters.progress_report_callback)

    @log_args
    async def stop_scheduler(self, *, exc_type=None, exc_val=None, exc_tb=None) -> None:
        """
        Stops the progress reporting. Call only after start_scheduler() had been called.
        """
        if self._progress_reporter is not None:
            await self._progress_reporter.shut_down()
        await self._root_task_node.cancel_all_tasks()

    def root_task_node(self) -> SchedulerTreeNode:
        return self._root_task_node

    def parent_task_node(self) -> SchedulerTreeNode:
        return self._parent_task_node

    @log_args
    def sub_job(self, job_name: str) -> "SchedulerContext":
        """
        Create a named sub-job for progress reporting.

        Returns a new :class:`SchedulerContext` where all operations will be grouped
        under *job_name* in the :class:`ProgressReport` hierarchy. This is useful for
        labeling or grouping multiple WRAPP API calls under a single progress entry.

        Example::

            await wrapp.create(..., scheduler=scheduler.sub_job("Packaging"))
        """
        return self.create_sub_context_and_node(job_name)

    @log_args
    def create_sub_context(self, parent_task_node: SchedulerTreeNode) -> "SchedulerContext":
        new_context = copy.copy(self)
        new_context._parent_task_node = parent_task_node
        return new_context

    def create_sub_context_and_node(
        self, description: Optional[str] = None, *, expected_num_sub_jobs: Optional[int] = None
    ) -> "SchedulerContext":
        new_parent_node = self._parent_task_node.create_tree_node(description, expected_num_sub_jobs=expected_num_sub_jobs)
        return self.create_sub_context(new_parent_node)

    def __repr__(self):
        parameters = getattr(self, "_parameters", None)
        scheduler = getattr(self, "_scheduler", None)
        scheduler_shut_down_completed = getattr(self, "_scheduler_shut_down_completed", None)
        return (
            f"SchedulerContext(parameters={parameters!r}, "
            f"scheduler={scheduler!r}, "
            f"scheduler_shut_down_completed={scheduler_shut_down_completed!r})"
        )


@wrapp_public_api
class WrappScheduler:
    """
    ContextManager for wrapp to schedule its tasks. This will help throttling the number of tasks
    send simultaneously to the server, avoiding server overload and back-off responses.

    Example:
            >>> import asyncio, wrapp
            >>> async def example(test_folder_1: str, test_folder_2: str):
            >>>     tasks = []
            >>>     async with WrappScheduler(params=SchedulingParameters(jobs=10)) as scheduler_context:
            >>>         tasks.append(asyncio.create_task(wrapp.catalog(test_folder_1, scheduler=scheduler_context)))
            >>>         tasks.append(asyncio.create_task(wrapp.catalog(test_folder_2, scheduler=scheduler_context)))
            >>>     results = await asyncio.gather(*tasks)
    """

    @log_args
    def __init__(
        self,
        *,
        context: Optional[SchedulerContext] = None,
        params: Optional[SchedulingParameters] = None,
    ):
        if context is not None:
            if params is not None:
                raise RuntimeError(
                    "Provide either a context object prefilled with parameters or a params object used to construct one, not both!"
                )
            self.context = context
        elif params is not None:
            self.context = SchedulerContext(params)
        else:
            self.context = SchedulerContext(SchedulingParameters())

    @log_args
    async def __aenter__(self) -> SchedulerContext:
        self.context.start_scheduler()
        return self.context

    @log_args
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.context.stop_scheduler(exc_type=exc_type, exc_val=exc_val, exc_tb=exc_tb)

    def __repr__(self):
        context = getattr(self, "context", None)
        return f"WrappScheduler(context={context!r})"


class WrappSchedulerCLI:
    def __init__(self, params: Optional[SchedulingParameters] = None):
        params = params or SchedulingParameters()
        if params.progress_report_callback is not None:
            raise RuntimeError("WrappSchedulerCLI provides its own progress report callback")
        self._cli_progress_reporter = CLIProgressPrinter(params.progress_report)
        params.progress_report_callback = self._cli_progress_reporter.print_progress
        self._wrapp_scheduler = WrappScheduler(params=params)

    async def __aenter__(self) -> SchedulerContext:
        self._cli_progress_reporter.start_progress_report()
        return await self._wrapp_scheduler.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._wrapp_scheduler.__aexit__(exc_type, exc_val, exc_tb)
        await self._cli_progress_reporter.shut_down()

    def cli_progress_reporter(self) -> CLIProgressPrinter:
        return self._cli_progress_reporter

    def __str__(self):
        context = getattr(self, "context", None)
        return f"WrappScheduler(context={context})"


def log_exception_and_raise_exit(e: Exception, context: CommandParameters, exit_code: int = 3):
    logger.exception(e)
    if context.debug:
        raise e
    else:
        raise typer.Exit(exit_code)
