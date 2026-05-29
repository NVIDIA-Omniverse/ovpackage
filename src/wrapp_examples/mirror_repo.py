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

"""
Script to mirror all packages from one repository to another.

Usage:
    python -m wrapp_examples.mirror_repo <source_repo> <destination_repo> [options]

Example:
    python -m wrapp_examples.mirror_repo omniverse://source-server/repo omniverse://dest-server/repo
"""

import argparse
import asyncio
import logging
import os
import sys

import wrapp

logger = logging.getLogger("omni.wrapp")


async def mirror_repository(
    source_repo: str,
    destination_repo: str,
    auth_infos: list[wrapp.StorageAuthInfo],
    tags: bool = False,
    resume: bool = False,
    verbose: bool = False,
    interactive_auth: bool = False,
    max_concurrent: int = 5,
) -> tuple[int, int]:
    """
    Mirror all packages from source_repo to destination_repo.

    Args:
        source_repo: URL of the source repository (excluding .packages directory)
        destination_repo: URL of the destination repository (excluding .packages directory)
        auth_infos: List of StorageAuthInfo objects for authentication
        tags: Whether to copy tags as well
        resume: Whether to use differential copy (resume interrupted transfers)
        verbose: Enable verbose logging
        interactive_auth: Allow interactive browser-based authentication
        max_concurrent: Maximum number of concurrent mirror operations

    Returns:
        Tuple of (successful_count, failed_count)
    """
    successful = 0
    failed = 0
    lock = asyncio.Lock()

    context = wrapp.CommandParameters(verbose=verbose)

    async with wrapp.ContextManager(auth_infos, interactive_login_allowed=interactive_auth) as wrapp_context:
        # List all packages in the source repository
        logger.info(f"Listing packages in source repository: {source_repo}")
        packages = await wrapp.list_repo(source_repo, scheduler=wrapp_context)

        if not packages:
            logger.warning(f"No packages found in source repository: {source_repo}")
            return (0, 0)

        # Build flat list of (package_name, version) pairs
        mirror_tasks: list[tuple[str, str]] = []
        for package_path, versions in packages:
            # Extract package name from the full path (e.g., "repo/.packages/pkg_name" -> "pkg_name")
            package_name = package_path.rstrip("/").split("/")[-1]
            for version in versions:
                mirror_tasks.append((package_name, version))

        logger.info(f"Found {len(packages)} packages with {len(mirror_tasks)} total versions to mirror")
        logger.info(f"Using max {max_concurrent} concurrent mirror operations")

        # Semaphore to limit concurrent mirror operations
        semaphore = asyncio.Semaphore(max_concurrent)

        async def mirror_one(package_name: str, version: str) -> bool:
            """Mirror a single package version. Returns True on success, False on failure."""
            nonlocal successful, failed
            async with semaphore:
                logger.info(f"Mirroring {package_name}@{version}")
                try:
                    await wrapp.mirror(
                        package_name=package_name,
                        version=version,
                        source_repo=source_repo,
                        destination_repo=destination_repo,
                        tags=tags,
                        resume=resume,
                        context=context,
                        scheduler=wrapp_context,
                    )
                    async with lock:
                        successful += 1
                    logger.info(f"Successfully mirrored {package_name}@{version}")
                    return True
                except wrapp.FailedCommand as e:
                    async with lock:
                        failed += 1
                    logger.error(f"Failed to mirror {package_name}@{version}: {e}")
                    return False
                except wrapp.StorageOperationError as e:
                    async with lock:
                        failed += 1
                    logger.error(f"Storage error mirroring {package_name}@{version}: {e}")
                    return False

        # Run all mirror operations concurrently (limited by semaphore)
        await asyncio.gather(*[mirror_one(pkg, ver) for pkg, ver in mirror_tasks])

    return (successful, failed)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mirror all packages from one repository to another",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Mirror between repositories with interactive authentication (recommended)
    python -m wrapp_examples.mirror_repo omniverse://source/repo omniverse://dest/repo -I

    # Mirror with higher parallelism (10 concurrent operations)
    python -m wrapp_examples.mirror_repo omniverse://source/repo omniverse://dest/repo -I -j 10

    # Mirror with explicit credentials from environment
    export SOURCE_USER=user1 SOURCE_PASS=pass1
    export DEST_USER=user2 DEST_PASS=pass2
    python -m wrapp_examples.mirror_repo omniverse://source/repo omniverse://dest/repo

    # Mirror with resume (differential copy) and tags
    python -m wrapp_examples.mirror_repo source dest --resume --tags -I
        """,
    )
    parser.add_argument("source_repo", help="Source repository URL (excluding .packages directory)")
    parser.add_argument("destination_repo", help="Destination repository URL (excluding .packages directory)")
    parser.add_argument("--tags", action="store_true", help="Also copy package tags")
    parser.add_argument("--resume", action="store_true", help="Use differential copy (resume interrupted transfers)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose output")
    parser.add_argument(
        "--interactive-auth", "-I", action="store_true", help="Allow interactive browser-based authentication"
    )
    parser.add_argument("--max-concurrent", "-j", type=int, default=5, help="Maximum number of concurrent mirror operations (default: 5)")

    # Authentication options
    parser.add_argument("--source-user", default=os.getenv("SOURCE_USER"), help="Username for source server (or set SOURCE_USER env var)")
    parser.add_argument(
        "--source-pass", default=os.getenv("SOURCE_PASS"), help="Password/token for source server (or set SOURCE_PASS env var)"
    )
    parser.add_argument("--dest-user", default=os.getenv("DEST_USER"), help="Username for destination server (or set DEST_USER env var)")
    parser.add_argument(
        "--dest-pass", default=os.getenv("DEST_PASS"), help="Password/token for destination server (or set DEST_PASS env var)"
    )

    return parser.parse_args()


def extract_server_url(repo_url: str) -> str:
    """Extract server URL from repository URL for authentication."""
    # Handle omniverse:// URLs
    if repo_url.startswith("omniverse://"):
        parts = repo_url.split("/")
        if len(parts) >= 3:
            return f"omniverse://{parts[2]}"
    # Handle other URL schemes (s3://, https://, etc.)
    elif "://" in repo_url:
        parts = repo_url.split("/")
        if len(parts) >= 3:
            return f"{parts[0]}//{parts[2]}"
    return repo_url


def main():
    args = parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Build authentication info list
    auth_infos: list[wrapp.StorageAuthInfo] = []

    source_server = extract_server_url(args.source_repo)
    dest_server = extract_server_url(args.destination_repo)

    # Add source authentication if credentials provided
    if args.source_user and args.source_pass:
        auth_infos.append(
            wrapp.StorageAuthInfo.nucleus(
                server_url=source_server,
                username=args.source_user,
                password_or_token=args.source_pass,
            )
        )
        logger.info(f"Using provided credentials for source server: {source_server}")

    # Add destination authentication if credentials provided (and different server)
    if args.dest_user and args.dest_pass:
        auth_infos.append(
            wrapp.StorageAuthInfo.nucleus(
                server_url=dest_server,
                username=args.dest_user,
                password_or_token=args.dest_pass,
            )
        )
        logger.info(f"Using provided credentials for destination server: {dest_server}")

    logger.info(f"Starting repository mirror: {args.source_repo} -> {args.destination_repo}")

    try:
        successful, failed = asyncio.run(
            mirror_repository(
                source_repo=args.source_repo,
                destination_repo=args.destination_repo,
                auth_infos=auth_infos,
                tags=args.tags,
                resume=args.resume,
                verbose=args.verbose,
                interactive_auth=args.interactive_auth,
                max_concurrent=args.max_concurrent,
            )
        )

        logger.info(f"Mirror complete: {successful} successful, {failed} failed")

        if failed > 0:
            sys.exit(1)
    except wrapp.FailedCommand as e:
        logger.error(f"Mirror failed: {e}")
        sys.exit(1)
    except wrapp.StorageOperationError as e:
        logger.error(f"Storage operation failed: {e}")
        sys.exit(2)
    except KeyboardInterrupt:
        logger.warning("Mirror interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
