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

import logging
import os.path
import pathlib
import warnings
from typing import (
    Callable,
    List,
    Optional,
    Union,
)

from gitignore_parser import (
    parse_gitignore_str,
)

from wrapp.datastructures.catalog import Catalog
from wrapp.utils.api_marker import wrapp_public_api
from wrapp.utils.storage import (
    wrapp_read_text_file,
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    StorageOperationError,
    is_relative_path,
    join_paths,
    make_relative_url,
    normalize_url,
)

from ..utils.logging import log_args

logger = logging.getLogger("omni.wrapp")

# Type alias for ignore predicate functions - documented in docs/api.rst, "#:" does not work for Sphinx for a type alias?
IgnorePredicate = Callable[..., bool]
IgnorePredicate.wrapp_public_api = True  # type: ignore[attr-defined]


@wrapp_public_api
class IgnoreFilePredicate:
    """
    A predicate that evaluates ignore rules loaded from a file using gitignore syntax.

    This class parses ignore rules in the same format as ``.gitignore`` files and provides
    a callable interface to check if a given path should be ignored. It is typically used
    as the predicate for an :class:`IgnoreEvaluator`.

    For full documentation of the ignore file syntax, see :ref:`ignore-file-format-label`.
    """

    def __init__(self, ignore_text: str, normalized_base_url: str):
        """
        Create an IgnoreFilePredicate from ignore rules text.

        :param ignore_text: The content of the ignore file (gitignore-format rules).
                           See :ref:`ignore-file-format-label` for syntax details.
        :param normalized_base_url: The normalized base URL against which paths will be matched.
        """
        # The ignore parser works with absolute paths, and only works well with paths which look like they are
        # from a file system. It does not like URLs. So we make a fake root for it, and use that for all files.
        # The parser expects absolute paths to be checked, and it will raise a ValueError exception
        # if the checked path is not under the base_path set by either the ignore file path, or manually provided.
        # Unfortunately it uses internally os.path.abspath(p).as_posix() to normalize a path before checking the
        # rules, so we will end up with a path like "C:/something/other" on Windows, i.e. our fake root
        # needs to match that.
        self._fake_root = pathlib.Path(os.path.abspath("/FakeIgnoreRoot/"))
        self._normalized_base_url = normalized_base_url
        self._ignore_lambda = parse_gitignore_str(ignore_text, self._fake_root)

    def is_ignored(self, normalized_abs_url: str) -> bool:
        """
        Check if a path should be ignored according to the loaded rules.

        :param normalized_abs_url: The normalized absolute URL to check.
        :return: True if the path should be ignored, False otherwise.
        """
        # We need to add the fake root to make things work for the ignore_lambda (which expects an absolute URL).
        # We essentially swap the base url in the IgnoreEvaluator with the fake root directory to get from
        # an absolute URL to an absolute os-like path.
        rel_path = make_relative_url(self._normalized_base_url, normalized_abs_url)
        abs_path = join_paths(self._fake_root, rel_path)
        return self._ignore_lambda(abs_path)


@wrapp_public_api
class IgnoreEvaluator:
    """
    Evaluates whether paths should be ignored based on configurable predicate functions.

    This class is used throughout WRAPP to filter files during operations like ``create``,
    ``catalog``, ``install``, and ``freeze``. It supports combining multiple predicates
    and can be created from ignore files using :meth:`create_from_file`.

    Example usage with a custom predicate::

        evaluator = IgnoreEvaluator(
            root_path="/my/project",
            predicate=lambda path: path.endswith(".tmp")
        )
        if evaluator.is_ignored("file.tmp"):
            print("File is ignored")

    Example usage with an ignore file::

        evaluator = await IgnoreEvaluator.create_from_file(
            "/path/to/.wrappignore",
            "omniverse://server/my/project"
        )
    """

    @log_args
    def __init__(self, root_path: Optional[str], predicate: Union[List[IgnorePredicate], IgnorePredicate]):
        """
        Create an IgnoreEvaluator with a root path and one or more predicates.

        :param root_path: The root path used to resolve relative paths when checking.
                          Can be None if predicates work with absolute paths only.
        :param predicate: A single predicate function or a list of predicate functions.
                          Each predicate takes a path string and returns True if ignored.
        """
        self.root_path = root_path
        self.predicate: Union[List[IgnorePredicate], IgnorePredicate] = predicate

    @log_args
    def add_evaluator(self, predicate: Union[List[IgnorePredicate], IgnorePredicate]):
        """
        Add one or more additional predicates to this evaluator.

        A path is considered ignored if any predicate returns True.

        :param predicate: A single predicate function or a list of predicate functions to add.
        """
        if isinstance(self.predicate, list):
            if isinstance(predicate, list):
                self.predicate.extend(predicate)
            else:
                self.predicate.append(predicate)
        else:
            if isinstance(predicate, list):
                self.predicate = [self.predicate] + predicate
            else:
                self.predicate = [self.predicate, predicate]

    def is_ignored(self, path: str) -> bool:
        """
        Check if a path should be ignored.

        Files ending with ``.auto.png`` (thumbnails) are always ignored regardless of predicates.

        :param path: The path to check. Can be relative (resolved against root_path) or absolute.
        :return: True if the path should be ignored, False otherwise.
        """
        # First check if it is a thumbnail generated by the thumb-nailing service, those are always ignored
        if path.endswith(".auto.png"):
            return True

        if not isinstance(self.predicate, list):
            predicates = [self.predicate]
        else:
            predicates = self.predicate

        for predicate in predicates:
            if not is_relative_path(path) or self.root_path is None:
                if predicate(normalize_url(path)):
                    return True
            else:
                if predicate(normalize_url(join_paths(self.root_path, path))):
                    return True
        return False

    def __repr__(self) -> str:
        root_path = getattr(self, "root_path", None)
        predicate = getattr(self, "predicate", None)
        return f"IgnoreEvaluator(root_path={root_path!r}, predicate={predicate!r})"

    @staticmethod
    async def create_from_file(ignore_file: Optional[str], base_url: str) -> Optional["IgnoreEvaluator"]:
        """
        Create an IgnoreEvaluator from an ignore file.

        The ignore file uses the same format as ``.gitignore`` files. For full documentation
        of the supported syntax, see :ref:`ignore-file-format-label`.

        :param ignore_file: Path or URL to the ignore file. If None, looks for ``.wrappignore``
                            in base_url. If the file does not exist, returns None.
        :param base_url: The base URL for resolving patterns in the ignore file. Patterns are
                         always resolved relative to this base_url, regardless of where the
                         ignore file is located.
        :return: An IgnoreEvaluator configured with the rules from the file, or None if no
                 ignore file was found (only when ignore_file is None).
        :raises FailedCommand: If the ignore file cannot be read.

        **Path resolution behavior:**

        When ``ignore_file`` is provided, it is used as-is without joining with ``base_url``.
        This means local paths without a URL scheme (e.g., ``"myignore.txt"``) are resolved
        relative to the current working directory, NOT relative to ``base_url``.

        Only when ``ignore_file`` is None does this method look for default ignore files in ``base_url``.
        """
        if ignore_file is None:
            default_ignore_name = ".wrappignore"
            candidate = join_paths(base_url, default_ignore_name)
            try:
                if await wrapp_url_exists(candidate):
                    ignore_file = candidate
            except StorageOperationError as e:
                # Can't determine if the file exists (e.g., CloudFront returns access denied
                # for stat operations on non-existent files). Proceed without ignore file.
                logger.info(f"Could not check for default {default_ignore_name} at '{candidate}': {e}")
                return None
            if ignore_file is None:
                return None
        try:
            ignore_text = await wrapp_read_text_file(ignore_file)
        except Exception as e:
            raise FailedCommand(f"Failed to read ignore file '{ignore_file}'.") from e
        normalized_base_url = normalize_url(base_url)
        predicate = IgnoreFilePredicate(ignore_text, normalized_base_url)
        return IgnoreEvaluator(normalized_base_url, predicate.is_ignored)


async def create_ignore_eval_from_params(
    ignores: Optional[Union[str, IgnoreEvaluator]],
    base_url: str,
    *,
    ignore_file: Optional[str] = None,
    stacklevel: int = 4,
) -> Optional[IgnoreEvaluator]:
    """
    Internal helper to resolve ignores parameter to an IgnoreEvaluator.

    This function handles the deprecated ignore_file parameter for backwards compatibility.

    :param ignores: The ignores parameter (str path, IgnoreEvaluator, or None)
    :param base_url: The base URL for resolving relative paths and loading default ignore files
    :param ignore_file: Deprecated, use ignores instead. Specify the name of an ignore file.
    :param stacklevel: Stack level for the deprecation warning (default 4 for typical usage)
    :return: An IgnoreEvaluator object, or None if no ignores are specified
    :raises FailedCommand: If both ignores and ignore_file are specified
    """
    # Handle deprecated ignore_file parameter
    if ignore_file is not None:
        warnings.warn(
            "The 'ignore_file' parameter is deprecated and will be removed in a future version. " "Use 'ignores' instead.",
            DeprecationWarning,
            stacklevel=stacklevel,
        )
        if ignores is None:
            ignores = ignore_file
        else:
            raise FailedCommand("Cannot specify both 'ignores' and 'ignore_file'. Use 'ignores' only.")

    # Now resolve to IgnoreEvaluator
    if ignores is None or isinstance(ignores, str):
        return await IgnoreEvaluator.create_from_file(ignores, base_url)
    else:
        return ignores


def item_is_ignored(parent_dir: str, relative_path: str, ignore_rules: Optional[IgnoreEvaluator]):
    # First check if it is a thumbnail generated by the thumb-nailing service, those are always ignored
    # We do not check for the user 'thumbnails_service' anymore, but rely solely on the extension .auto.png
    if relative_path.endswith(".auto.png"):
        return True
    return ignore_rules and ignore_rules.is_ignored(join_paths(parent_dir, relative_path))


def filter_catalog_with_ignores(catalog: Catalog, ignore_rules: Optional[IgnoreEvaluator]) -> Catalog:
    if ignore_rules:
        filtered_items = list(filter(lambda x: not item_is_ignored(catalog.root, x.relative_path, ignore_rules), catalog.items))
        return Catalog(root=catalog.root, items=filtered_items)
    else:
        return catalog
