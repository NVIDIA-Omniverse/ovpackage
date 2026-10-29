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

import bisect
import json
import logging
import traceback
from abc import (
    ABC,
    abstractmethod,
)
from enum import (
    Enum,
    IntEnum,
)
from typing import (
    Annotated,
    Any,
    Dict,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    Field,
)

from wrapp.datastructures.command_context import (
    CommandParameters,
    SchedulerContext,
)
from wrapp.datastructures.previous_versions.patch_old_versions import PatchOpList_V2_0
from wrapp.datastructures.wrapp_file_model import (
    WRAPPFileModel,
    WRAPPModelVersion,
)
from wrapp.tagging_service import tagging
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.storage import (
    Result,
    wrapp_copy_address_optimized,
    wrapp_copy_resource_identity_optimized,
    wrapp_delete_item,
)
from wrapp.utils.storage_interface import (
    ResourceAddress,
    SerializedResourceIdentity,
)
from wrapp.utils.utils import (
    StorageOperationError,
    escape_legacy_storage_address,
    get_path_from_url,
)

logger = logging.getLogger("omni.wrapp")


class PatchOperationType(str, Enum):
    """Enum for the different patch operations. Each key has its name as a string value."""

    create_folder = "create_folder"  #: Enum value indicating :class:`PatchOpCreateFolder`
    delete = "delete"  #: Enum value indicating :class:`PatchOpDelete`
    copy = "copy"  #: Enum value indicating :class:`PatchOpCopy`
    add_tag = "add_tag"  #: Enum value indicating :class:`PatchOpAddTag`
    delete_tag = "delete_tag"  #: Enum value indicating :class:`PatchOpDeleteTag`


class ItemType(str, Enum):
    file = "file"
    folder = "folder"


# Note that the operations are executed in ascending order of the enum values.
class _ConcurrencyGroup(IntEnum):
    normal_ops = 1
    add_tag_ops = 2
    delete_ops = 3


@wrapp_public_api
class PatchOpBase(BaseModel, ABC):
    """
    Abstract base class for patch operations. Each concrete class has a field "operation_type"
    of type :class:`PatchOperationType`.
    """

    # We could also lose this string for compactness of the Patch if the patch is only kept
    # in memory, and generate it in the same way as ThreeWayDiffContext.diff_explanation.
    # The written out files need to add user information (but arguably those files also might
    # need to contain the full output from ThreeWayDiffContext.diff_explanation?).
    explanation: Optional[str] = None

    def __repr__(self):
        return self._description() + (f" (because {self.explanation})" if self.explanation is not None else "")

    @abstractmethod
    async def execute(self, patch: "Patch", context: CommandParameters, verbose: bool = False) -> None:
        ...

    @abstractmethod
    def stable_order_sort_key(self) -> str:
        ...

    # Operations with lower concurrency group number are executed first.
    # Operations with the same concurrency group number are executed concurrently.
    def concurrency_group(self) -> Tuple[_ConcurrencyGroup, int]:
        return _ConcurrencyGroup.normal_ops, 0

    # The name of task used for the scheduling
    @abstractmethod
    def taskname(self) -> str:
        ...

    # Description of the patch operation if not provided by self.explanation
    @abstractmethod
    def _description(self) -> str:
        ...

    def explanation_txt(self) -> str:
        """Description of the patch operation."""
        return self.explanation or self._description()

    def __lt__(self, other: Any) -> bool:
        """
        The patches are sorted in execution order, and such that the order is stable.
        If the function returns True, "self" is executed before "other".
        """
        if not isinstance(other, PatchOpBase):
            return NotImplemented
        self_concurrency_group = self.concurrency_group()
        other_concurrency_group = other.concurrency_group()
        if self_concurrency_group != other_concurrency_group:
            return self_concurrency_group < other_concurrency_group
        # Make sure we have a stable order
        return self.stable_order_sort_key() < other.stable_order_sort_key()


class PatchOpCopy(PatchOpBase):
    """Patch operation copying a file."""

    operation_type: Literal[PatchOperationType.copy] = PatchOperationType.copy
    src: str  #: The source file.
    dst: str  #: The target file path.
    hash: Optional[str] = None  #: If available, the hash of the source file for optimization
    resource_identity_v2: Optional[str] = Field(default=None, min_length=1)  #: Read token for the exact source bytes.
    item_type: ItemType  #: Deprecated - WRAPP only copies files which implicitly creates folders.

    def _description(self) -> str:
        hash_txt = f"with hash {self.hash}" if hash is not None else ""
        return f"Copy {self.src} to {self.dst} {hash_txt}"

    async def execute(self, patch: "Patch", context: CommandParameters, verbose: bool = False) -> None:
        if self.item_type == ItemType.folder:
            # Create folder
            logger.warning(f"Old patch file: Ignoring to create folder {self.dst}")
        else:
            logger.info(repr(self))
            try:
                if self.resource_identity_v2 is None:
                    # Legacy patches and backends without durable identities
                    # retain the address/hash copy path.
                    result = await wrapp_copy_address_optimized(
                        context,
                        ResourceAddress(self.src),
                        ResourceAddress(self.dst),
                        self.hash,
                        overwrite=True,
                        force_upload=False,
                    )
                else:
                    result = await wrapp_copy_resource_identity_optimized(
                        context,
                        SerializedResourceIdentity(self.resource_identity_v2),
                        ResourceAddress(self.dst),
                        self.hash,
                        overwrite=True,
                        force_upload=False,
                        source_description=self.src,
                    )
                if result != Result.OK:
                    patch.add_error_msg(self, f"Error: could not copy {self.src} to {self.dst}: {result}")
            except StorageOperationError as e:
                patch.add_error_msg(self, f"Error: could not copy {self.src} to {self.dst}: {str(e)}")

    def stable_order_sort_key(self) -> str:
        return self.dst + "#copyto"

    def taskname(self) -> str:
        return "Copy file"


class PatchOpDelete(PatchOpBase):
    """Patch operation deleting a file."""

    operation_type: Literal[PatchOperationType.delete] = PatchOperationType.delete
    url: str  #: The URL of the deleted file.

    def _description(self):
        return f"Delete {self.url}"

    async def execute(self, patch: "Patch", context: CommandParameters, verbose: bool = False) -> None:
        logger.info(repr(self))
        result = await wrapp_delete_item(context, self.url)
        if result != Result.OK:
            patch.add_error_msg(self, f"Error: could not delete {self.url}: {result}")

    def stable_order_sort_key(self) -> str:
        return self.url + "#delete"

    def concurrency_group(self) -> Tuple[_ConcurrencyGroup, int]:
        # Delete operations are executed in descending order of number of slashes in the path.
        num_slashes: int = get_path_from_url(self.url).count("/")
        return _ConcurrencyGroup.delete_ops, -num_slashes

    def taskname(self) -> str:
        return "Delete"


class PatchOpCreateFolder(PatchOpBase):
    """
    Patch operation to create a folder. Only for backwards compatibility.

    .. deprecated:: 1.2.0
    """

    operation_type: Literal[PatchOperationType.create_folder] = PatchOperationType.create_folder
    url: str

    def _description(self):
        return f"Create folder {self.url}"

    async def execute(self, patch: "Patch", context: CommandParameters, verbose: bool = False) -> None:
        logger.warning(f"Old patch file, ignoring to create folder {repr(self)}")

    def stable_order_sort_key(self) -> str:
        return self.url + "#create"

    def taskname(self) -> str:
        return "Create Folder"


class PatchOpAddTag(PatchOpBase):
    """Patch operation to add a tag to a file."""

    operation_type: Literal[PatchOperationType.add_tag] = PatchOperationType.add_tag
    url: str  #: URL of the file.
    tag: str  #: JSON data for the tag added (object of type Tag serialized to JSON).

    def _description(self):
        return f"Add tag {self.tag} to {self.url}"

    async def execute(self, patch: "Patch", context: CommandParameters, verbose: bool = False) -> None:
        try:
            tag = json.loads(self.tag)
            parsed = urlparse(self.url)
            ts = await patch.get_ts(context, self.url)
            logger.info(repr(self))
            r = await ts.set_tag(parsed.path, tag["name"], tag["value"], tag["ns"])
            if (
                r is None
                or len(r.path_result) != 1
                or len(r.path_result[0].status) != 1
                or r.path_result[0].status[0] != tagging.StatusCode.Ok
            ):
                patch.add_error_msg(self, f"Failed to add tag {self.tag} to {self.url}: {r}")
        except Exception:
            patch.add_error_msg(self, f"Failed to add tag {self.tag} to {self.url}. Exception:  {traceback.format_exc()}")

    def stable_order_sort_key(self) -> str:
        return self.url + "#" + self.tag + "#add"

    def concurrency_group(self) -> Tuple[_ConcurrencyGroup, int]:
        return _ConcurrencyGroup.add_tag_ops, 0

    def taskname(self) -> str:
        return "Add Tag"


class PatchOpDeleteTag(PatchOpBase):
    """Patch operation to remove a tag from a file."""

    operation_type: Literal[PatchOperationType.delete_tag] = PatchOperationType.delete_tag
    url: str  #: URL of the file.
    tag: str  #: JSON data for the tag added (object of type Tag serialized to JSON).

    def _description(self):
        return f"Delete tag {self.tag} from {self.url}"

    async def execute(self, patch: "Patch", context: CommandParameters, verbose: bool = False) -> None:
        try:
            tag = json.loads(self.tag)
            parsed = urlparse(self.url)
            ts = await patch.get_ts(context, self.url)
            logger.info(repr(self))
            r = await ts.delete_tag(parsed.path, tag["name"], tag["ns"])
            if (
                r is None
                or len(r.path_result) != 1
                or len(r.path_result[0].status) != 1
                or r.path_result[0].status[0] not in [tagging.StatusCode.Ok, tagging.StatusCode.DoesNotExist]
            ):
                patch.add_error_msg(self, f"Failed to delete tag {self.tag} to {self.url}: {r}")
        except Exception as e:
            patch.add_error_msg(self, f"Failed to delete tag {self.tag} to {self.url}: {e}")

    def stable_order_sort_key(self) -> str:
        return self.url + "#" + self.tag + "#delete"

    def taskname(self) -> str:
        return "Delete Tag"


# Doing polymorphic serialization and deserialization with Pydantic 2.8.2 without an explicit
# list of all types is surprisingly hard and in this context unnecessary.
# This is the way the Pydantic authors think it should work.
PatchOpUnion = Annotated[
    Union[PatchOpCopy, PatchOpDelete, PatchOpCreateFolder, PatchOpAddTag, PatchOpDeleteTag], Field(discriminator="operation_type")
]


class PatchOpList(WRAPPFileModel):
    format_version: WRAPPModelVersion = WRAPPModelVersion(version_string="3.0")
    operations: list[PatchOpUnion]

    @classmethod
    def migrate_major_version(cls, read_data: PatchOpList_V2_0) -> "PatchOpList":
        # Without format_version: original format.
        # v1: add_tag operations are guaranteed to come before delete operations, but after all other operations.
        # v2: introduction of WRAPPFileModel/WRAPPModelVersion
        # v3: preserve resource_identity_v2 on copy operations. This is a major
        #     version because old nested PatchOpCopy models cannot preserve an
        #     unknown field when a newer patch is loaded and saved.
        data_dict = read_data.model_dump()
        data_dict.pop("format_version")  # we do not want to override format_version
        for operation in data_dict["operations"]:
            for key in ("src", "dst", "url"):
                if key in operation:
                    operation[key] = escape_legacy_storage_address(operation[key])
        return PatchOpList(**data_dict)


@wrapp_public_api
class Patch:
    """
    Class to execute a set of patch operations returned by "install" efficiently and in the correct order.
    To execute all patch operations in a list, create an object of this class,
    use  :func:`Patch.add` to add all operations, and run :func:`Patch.execute`.

    The correct order of operations is determined by the '<' operator for PatchOpBase.
    """

    def __init__(self) -> None:
        self.ops: PatchOpList = PatchOpList(operations=[])
        self._errors: List[Tuple[PatchOpBase, str]] = []  # List is sorted according to PatchOpBase.__lt__
        self.ts_cache: Dict[str, tagging.Tagging] = {}

    async def disconnect_tagging_service(self):
        for ts in self.ts_cache.values():
            await ts.disconnect()
        self.ts_cache = {}

    def add(self, operation: PatchOpBase) -> None:
        """
        Add a patch operation to this patch.
        All operations are executed in the order they are added to the Patch object,
        with the exception of adding tag and deleting files: all other operations
        are executed first, followed by all operations adding tags, and then all operations
        deleting files.
        """
        bisect.insort(self.ops.operations, operation)

    async def save(self, context: CommandParameters, filename: str) -> None:
        await self.ops.save(context, filename)

    async def load(self, filename: str) -> None:
        self.ops = await self.ops.load(source=filename)
        if "format_version" not in self.ops.model_fields_set:
            # Version 0 would not have the add_tag operations at the right place.
            self.ops.operations.sort()
            pass

    async def get_ts(self, context: CommandParameters, url: str) -> tagging.Tagging:
        parsed = urlparse(url)
        server = parsed.netloc
        # Note that we might get a concurrent call into this function while ts.connect is waiting -
        # to avoid returning tag services not yet completely intialized, all calls need to wait
        # for ts.connect to complete.
        self.ts_cache.setdefault(server, tagging.Tagging(context, parsed.netloc))
        await self.ts_cache[server].connect(url)

        return self.ts_cache[server]

    async def execute(self, context: CommandParameters, scheduler: SchedulerContext) -> bool:
        """
        Execute all operations in the patch. Return true if all operations succeed or
        if the Patch is empty.

        Errors in all patch operations are gathered. If any error occurred, False
        is returned and the errors can be retrieved through :func:`Patch.get_errors()`
        """
        # If a patch operation reports an error through Patch.operation_failed, the
        # execution continues, and False is returned.
        # If a patch operation throws an exception (which should only occur in exceptional cases), the execution
        # is immediately stopped, and the exception is raised.
        # The operations are already sorted by concurrency group. So we can just iterate
        # and make a scheduler group for each block.
        if not self.ops.operations:
            return True
        done = False
        it = iter(self.ops.operations)
        op = next(it)
        async with scheduler.parent_task_node().create_task_group_node(f"Running {len(self.ops.operations)} patch operations") as tree_node:
            while not done:
                async with tree_node.create_task_group_node(
                    f"Execute {len(self.ops.operations)} patch ops: {op.concurrency_group()[0].name}"
                ) as task_node:
                    while True:
                        task_node.create_task(
                            op.execute(self, context, context.verbose),
                            name=op.taskname(),
                        )
                        try:
                            next_op = next(it)
                        except StopIteration:
                            # The iteration completed normally
                            done = True
                            break
                        if next_op.concurrency_group() != op.concurrency_group():
                            # Make a new task_node for each concurrency group before executing next_op
                            op = next_op
                            break
                        else:
                            # next op to be executed concurrently with the last one
                            op = next_op
        for error_op, error_msg in self._errors:
            logger.error(f"{error_op!r} failed: {error_msg}")
        return not self._errors

    def add_error_msg(self, operation: PatchOpBase, error_msg: str) -> None:
        # Report the failure of a patch operation without stopping the remaining operations.
        # Insert the error message as the last one for this operation
        bisect.insort(self._errors, (operation, error_msg), key=lambda t: t[0])

    def get_error_msg(self, operation: PatchOpBase) -> Optional[str]:
        """
        Check if the given operation has failed and return the error message for the operation if it failed,
        or None if it succeeded.
        """
        pos = bisect.bisect_left(self._errors, (operation, ""))  # Find the first element containing "operation"
        if pos != len(self._errors) and self._errors[pos][0] == operation:
            return self._errors[pos][1]
        return None

    def get_errors(self) -> list[tuple[PatchOpBase, str]]:
        """
        Return all errors encountered during :func:`Patch.execute()` as a list of tuples,
        first the failed patch operation as :class:`PatchOpBase`, secondly the error message.
        """
        return self._errors
