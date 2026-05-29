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

import atexit
import logging
import os
import pathlib
import platform
import subprocess
import tempfile
import time
import traceback
from typing import (
    Optional,
    Tuple,
    Type,
)

logger = logging.getLogger("omni.wrapp")

try:
    import httpx

    g_httpx_available = True
except ImportError as e:
    logger.info(f"DISABLED: verify feature. Failed to import {e.name}. Exception: {e}")
    g_httpx_available = False

from wrapp.utils.utils import (
    FailedCommand,
    MicroServiceOperationError,
)


class MicroserviceManager:
    # TODO - this is using classmethods and class variables
    # in other parts of the software we use singletons, which have somewhat simpler syntax. We should probably agree which way to go
    kit_process = None
    wrapp_service_port: Optional[int] = None

    @classmethod
    def service_host(cls: Type["MicroserviceManager"]) -> str:
        return f"http://localhost:{cls.wrapp_service_port}"

    @classmethod
    def find_kit_exe(cls: Type["MicroserviceManager"]) -> Tuple[str, str]:
        # this will only work when running from source, users using the deployed version have to specify kit_exe_path.
        # this could be improved to find kit in the usual install locations, or by asking launcher?
        kit_root_path = ""
        kit_exe_path: Optional[str] = None
        try:
            project_root = pathlib.Path(__file__).parent.parent.parent.parent
            kit_exe_path_object = project_root / "_deps" / "kit-sdk"
            if platform.system() == "Windows":
                kit_exe_path = str(kit_exe_path_object / "kit.exe")
            else:
                kit_exe_path = str(kit_exe_path_object / "kit")
            kit_root_path = str(project_root / "_kitroot")
        except:
            pass
        if not kit_exe_path or not os.path.exists(kit_exe_path):
            raise FailedCommand(f"Verify is currently only supported for source builds. No path for kit or not found at '{kit_exe_path}'")
        else:
            return kit_exe_path, kit_root_path

    @classmethod
    async def wait_for_microservice(cls: Type["MicroserviceManager"], timeout: float):
        if not g_httpx_available:
            # Should not be called - verify should be disabled.
            raise RuntimeError("Missing dependencies for the MicroserviceManager!")
        async with httpx.AsyncClient() as client:
            start_time = time.time()
            while True:
                try:
                    response = await client.get(f"{cls.service_host()}/docs", timeout=timeout - (time.time() - start_time))
                    if not response.is_success:
                        raise MicroServiceOperationError(
                            f"Failed to connect to '{cls.service_host()}/docs'. Kit should have started up and make the microservice on this port available, but it did not?"
                        )
                    else:
                        return response.text
                except httpx.RequestError:
                    time.sleep(0.5)
                except Exception as e:
                    raise MicroServiceOperationError(
                        f"Exception '{e}' when trying to connect to '{cls.service_host()}/docs'. Kit should have started up and make the microservice on this port available, but it did not?"
                    ) from e
                if time.time() - start_time > timeout:
                    raise MicroServiceOperationError(
                        f"Timeout when trying to connect to '{cls.service_host()}/docs'. Kit should have started up and make the microservice on this port available, but it did not?"
                    )

    # Start Kit using the given executable and root path providing the microservice
    # on the given port which can be overwritten with the given environment variable.
    # Returns True if a fresh process was started up, returns False if an already existing process
    # was used. Throws an exception in error cases.
    @classmethod
    async def start(
        cls: Type["MicroserviceManager"],
        kit_exe_path: Optional[str],
        kit_root_path: Optional[str],
        port: int,
        auto_shutdown=True,
        timeout=10,
        debug_service=False,
    ):
        cls.stop()
        cls.wrapp_service_port = port

        logger.info(f"Starting microservice using {kit_exe_path}...")
        try:
            t = await cls.wait_for_microservice(1)
            logger.info(f"Using the already running microservice on {cls.service_host()}")
            return False
        except:
            pass

        cls.kit_process = None

        if not kit_exe_path:
            # TODO: this will only work on developer machines.
            try:
                project_root = pathlib.Path(__file__).parent.parent.parent.parent
                kit_exe_path_object = project_root / "_deps" / "kit-sdk"
                if platform.system() == "Windows":
                    kit_exe_path = str(kit_exe_path_object / "kit.exe")
                else:
                    kit_exe_path = str(kit_exe_path_object / "kit")
                kit_root_path = str(project_root / "_kitroot")
            except:
                pass
            if not kit_exe_path or not os.path.exists(kit_exe_path):
                raise FailedCommand(
                    "Please specify the 'kit_exe_path' parameter with a valid path to a Kit "
                    "executable. Check in the settings of apps like Code or USD Composer in the "
                    "Omniverse Launcher for suitable paths."
                )

        if not kit_root_path:
            kit_root_path = tempfile.TemporaryDirectory("kit_root").name
        cmd = [
            kit_exe_path,
            "--portable",
            "--portable-root",
            kit_root_path,
            "--ext-folder",
            "exts",
            "--enable",
            "omni.kit.wrapp_service",
            "--enable",
            "omni.services.core",
            "--enable",
            "omni.services.transport.server.http",
            "--info",
            f"--/exts/omni.services.transport.server.http/port={cls.wrapp_service_port}",
        ]
        if debug_service:
            cmd += ["--enable", "omni.kit.debug.pycharm"]
        if not os.path.exists(kit_root_path) or (len(os.listdir(kit_root_path)) < 5 and timeout < 60):
            timeout = 60  # Start up takes considerably longer if Kit is started for the first time and needs to pull extensions

        # TODO: this will only work on developer machines (unless kitext_service is also deployed)
        project_root = pathlib.Path(__file__).parent.parent.parent.parent
        cwd = str(project_root / "src" / "kitext_service" / "wrapp_service")
        logger.info(f"Starting Kit: '{' '.join(cmd)}' in working directory '{cwd}'")
        try:
            if platform.system() == "Windows":
                # Prevent the shell from stealing focus all the time
                cmd = ["cmd", "/c", "start", "/min"] + cmd
                cls.kit_process = subprocess.Popen(cmd, text=True, shell=True, cwd=cwd)  # type: ignore
            else:
                # No easy way to make the console show up in Linux, we can live without it for now
                cls.kit_process = subprocess.Popen(cmd, text=True, cwd=cwd)
            if auto_shutdown:
                atexit.register(cls.stop)
        except Exception as e:
            exception_str: str = traceback.format_exc()
            logger.error(f"Starting Kit FAILED: exception {exception_str}")
            raise

        # Wait for the microservice to be accessible
        await cls.wait_for_microservice(timeout)
        return True

    @classmethod
    def stop(cls: Type["MicroserviceManager"]):
        if cls.kit_process and cls.kit_process.poll() is None:
            cls.kit_process.kill()
            cls.kit_process = None
