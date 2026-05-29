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

import os
import shutil
import subprocess
import time

import pytest

_NANOIDP_CONTAINER_NAME = "wrapp-test-nanoidp"
_NANOIDP_HOST_PORT = 18000
_NANOIDP_IMAGE = "nanoidp-wrapp-test"
_FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "nanoidp")
_NANOIDP_DOCKERFILE = os.path.join(_FIXTURES_DIR, "Dockerfile")


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _build_nanoidp_image() -> bool:
    import tempfile

    with tempfile.TemporaryDirectory() as build_ctx:
        if not os.path.isdir(_FIXTURES_DIR):
            return False
        config_dst = os.path.join(build_ctx, "config")
        shutil.copytree(_FIXTURES_DIR, config_dst)
        shutil.copy2(_NANOIDP_DOCKERFILE, os.path.join(build_ctx, "Dockerfile"))
        result = subprocess.run(
            ["docker", "build", "-t", _NANOIDP_IMAGE, "."],
            cwd=build_ctx,
            capture_output=True,
            timeout=300,
        )
        return result.returncode == 0


def _start_nanoidp_container() -> bool:
    subprocess.run(
        ["docker", "rm", "-f", _NANOIDP_CONTAINER_NAME],
        capture_output=True,
        timeout=30,
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            _NANOIDP_CONTAINER_NAME,
            "-p",
            f"{_NANOIDP_HOST_PORT}:8000",
            _NANOIDP_IMAGE,
        ],
        capture_output=True,
        timeout=30,
    )
    return result.returncode == 0


def _wait_for_health(timeout_seconds: int = 30) -> bool:
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{_NANOIDP_HOST_PORT}/api/health"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    return False


def _check_url_reachable(base_url: str, timeout_seconds: int = 5) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url}/api/health", timeout=timeout_seconds) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _stop_nanoidp_container():
    subprocess.run(
        ["docker", "rm", "-f", _NANOIDP_CONTAINER_NAME],
        capture_output=True,
        timeout=30,
    )


@pytest.fixture(scope="session")
def nanoidp_url():
    """Return the NanoIDP base URL.

    In CI, set NANOIDP_URL to use a pre-existing service (e.g. a GitLab CI service).
    Locally, builds and starts a Docker container automatically, skipping if Docker is unavailable.
    """
    preconfigured = os.environ.get("NANOIDP_URL", "").strip()
    if preconfigured:
        if not _check_url_reachable(preconfigured):
            pytest.skip(f"NANOIDP_URL is set to {preconfigured} but the service is not reachable")
        yield preconfigured
        return

    if not _docker_available():
        pytest.skip("Docker is not available, skipping NanoIDP integration tests")

    if not _build_nanoidp_image():
        pytest.skip("Failed to build NanoIDP Docker image")

    if not _start_nanoidp_container():
        _stop_nanoidp_container()
        pytest.skip("Failed to start NanoIDP Docker container")

    if not _wait_for_health():
        _stop_nanoidp_container()
        pytest.skip("NanoIDP container did not become healthy in time")

    url = f"http://127.0.0.1:{_NANOIDP_HOST_PORT}"
    try:
        yield url
    finally:
        _stop_nanoidp_container()
