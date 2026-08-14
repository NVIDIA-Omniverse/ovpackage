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

def wrapp_public_api(func):
    func._wrapp_public_api = True
    return func


def is_wrapp_public_api(obj) -> bool:
    """Return whether ``obj`` itself carries the ``@wrapp_public_api`` marker.

    Uses the object's OWN namespace (``vars(obj)``, i.e. its ``__dict__`` for a
    class) so a marker inherited from a decorated base class does NOT count -- only
    objects that were individually decorated are considered public. Works for both
    classes and functions.
    """
    return bool(vars(obj).get("_wrapp_public_api", False))
