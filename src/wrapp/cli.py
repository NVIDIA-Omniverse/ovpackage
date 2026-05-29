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
import sys

import wrapp.commands.apply_patch
import wrapp.commands.build_manifest
import wrapp.commands.cat
import wrapp.commands.catalog
import wrapp.commands.count
import wrapp.commands.create
import wrapp.commands.create_metapackage
import wrapp.commands.create_patch
import wrapp.commands.delete_packages
import wrapp.commands.diff
import wrapp.commands.export
import wrapp.commands.extract_catalog
import wrapp.commands.find_orphans
import wrapp.commands.freeze
import wrapp.commands.get
import wrapp.commands.help
import wrapp.commands.import_
import wrapp.commands.install
import wrapp.commands.list_repo
import wrapp.commands.ls
import wrapp.commands.mirror
import wrapp.commands.new
import wrapp.commands.package_list
import wrapp.commands.publish_redirect
import wrapp.commands.shell
import wrapp.commands.status
import wrapp.commands.traceback
import wrapp.commands.uninstall
import wrapp.commands.verify
import wrapp.commands.version
from wrapp.app import app
from wrapp.utils.storage import wrapp_shutdown


def run() -> None:
    try:
        app()
    finally:
        # We need to call shutdown to prevent assert errors in Windows - see HUB-777.
        # omni.client.initialize is called in the clientlib_logging wrapper for the command
        # functions - we cannot call shutdown in those, because they are called several times
        # from the tests via CliRunner, and we can only call shutdown once.
        try:
            wrapp_shutdown(*sys.exc_info(), stop_timeout=30.0)
        except RuntimeError as e:
            logging.getLogger("omni.wrapp").warning("Error during shutdown: %s", e)


if __name__ == "__main__":
    run()
