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

import json
from typing import TypeVar

from pydantic import (
    BaseModel,
)


class SerializedData(BaseModel):
    pass
    # load/save functions are no longer required - all data is now first loaded into a dict
    # and then passed to Pydantic into a constructor.
    # Keeping the lines in here for future reference.

    # _LoadType = TypeVar("LoadType")  # type: ignore

    # @classmethod
    # async def load(cls: type[_LoadType], source: str) -> _LoadType:  # type: ignore
    #     if await wrapp_url_exists(source):
    #         if await wrapp_url_is_folder(source):
    #             raise FailedCommand(f"File is a folder and not a catalog file, cannot read: {source}")
    #         try:
    #             content = await wrapp_read_text_file(source)
    #             parsed_content = json.loads(content)
    #             return cls(**parsed_content)  # type: ignore
    #         except json.JSONDecodeError as e:
    #             raise FailedCommand(f"Catalog specified cannot be parsed as a JSON file: {e}")
    #         except ValidationError as e:
    #             raise FailedCommand(f"Failed to load catalog from {str}, validation error: {e.errors()}")
    #         except Exception as e:
    #             raise RuntimeError(f"Unexpected error reading catalog: {e}")
    #     else:
    #         raise FailedCommand(f"Source {source} not found - please specify a valid catalog file!")
    #
    # async def save(self, context: CommandParameters, path: str) -> None:
    #     result = await wrapp_write_file_content(context, path, self.model_dump_json(indent=2))
    #     if result != Result.OK:
    #         raise StorageOperationError(f"Failed to save data to {path}: {result}")
