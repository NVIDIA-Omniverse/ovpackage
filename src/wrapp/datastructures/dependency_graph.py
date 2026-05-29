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

import asyncio
import logging
from typing import (
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)

import networkx as nx

from wrapp.datastructures.command_context import CommandParameters
from wrapp.datastructures.loaded_package import (
    LoadedPackage,
    PackageRecord,
    collect_wrapp_files_in_tree,
)
from wrapp.datastructures.package import (
    Dependency,
    PackageInfo,
    VersionConflict,
    add_dependency_to_package,
    load_package_setup,
    wrapp_file_in_repo,
)
from wrapp.repo.repository import Repository
from wrapp.utils.scheduler_task_node import (
    SchedulerTaskGroupNode,
    SchedulerTreeNode,
)
from wrapp.utils.storage import (
    wrapp_url_exists,
)
from wrapp.utils.utils import (
    FailedCommand,
    drop_trailing_slashes,
    get_base_directory_from_url,
    join_paths,
    make_relative_url,
    normalize_url,
)

logger = logging.getLogger("omni.wrapp")


class DependencyNode:
    """
    A DependencyNode represents one vertex in the DependencyGraph. The id field is used to represent a unique identifier for
    a package installed at a certain location.
    """

    def __init__(self, package_name: str, path: str = "/"):
        path = drop_trailing_slashes(path)
        self.id = f"{package_name}|{path}"
        self.package = package_name
        self.url = path

    @staticmethod
    def from_id(node_id: str) -> "DependencyNode":
        package_name, package_location = node_id.split("|")
        return DependencyNode(package_name, package_location)


class DependencyGraph:
    """
    The Dependency graph represents the directed acyclic graph of the dependencies of packages to each other.

    Each vertex in the graph represents a package installed at a certain location, given that the same package can be installed more than once,
    but not in the same location.
    """

    def __init__(self, graph: nx.DiGraph, root: str):
        self.G = graph
        self.root = root

    # def save_diagram(self, filename):
    #     plt.clf()
    #     pos = nx.spring_layout(self.G)
    #     nx.draw(self.G, pos, with_labels=True)
    #     edge_labels = nx.get_edge_attributes(self.G, 'usage_of_version')
    #     nx.draw_networkx_edge_labels(self.G, pos, edge_labels=edge_labels)
    #     plt.axis('off')
    #     plt.savefig(filename)

    @staticmethod
    def add_subgraph(G: nx.DiGraph, subtree_graph: "DependencyGraph"):
        # Copy over the new subgraph into our root.
        # We don't use the union operator, since that renames the graph nodes (?)
        for new_node, new_node_data in subtree_graph.G.nodes.items():
            G.add_node(new_node, **new_node_data)
        for u, v, data in subtree_graph.G.edges(data=True):
            G.add_edge(u, v, **data)

    def add_dependency(self, start_node: PackageInfo, position_in_tree: str, dependency: Dependency):
        start = DependencyNode(start_node.name, position_in_tree)
        end = DependencyNode(dependency.package, (position_in_tree + "/" + dependency.relative_destination).lstrip("/"))
        self.G.add_edge(start.id, end.id, usage_of_version=dependency.version)

    def remove_package(self, package_setup: PackageInfo, relative_path: str):
        node_to_remove = DependencyNode(package_setup.name, relative_path)
        self.G.remove_node(node_to_remove.id)

    @staticmethod
    async def _build_subgraph_for_dependency(
        context: CommandParameters,
        results: list[tuple[PackageInfo, "DependencyGraph"]],
        task_node: SchedulerTaskGroupNode,
        named_package_setup: LoadedPackage,
        dependency: Dependency,
        root: str,
        ignore_version_conflicts,
        fallback_repo: Optional[Repository],
        opened_dependency_files: list[str],
    ):
        # A) Determine which repo we expect this dependency to be found in
        subpackage_repo: Optional[Repository] = fallback_repo
        if dependency.repository:
            subpackage_repo = Repository(repo_url=dependency.repository)

        # B) Then try to load the file from its installed location. If it is missing, we need to query the repo to see if we can find the package
        install_destination = named_package_setup.dependency_install_path(dependency)
        dependency_package_file = join_paths(install_destination, f".{dependency.package}.wrapp")

        # Check for circular dependency - normalize paths before comparison
        normalized_dependency_file = normalize_url(dependency_package_file)
        if normalized_dependency_file in opened_dependency_files:
            raise FailedCommand(
                f"Circular dependency detected: {dependency_package_file} is already being processed in the dependency chain"
            )

        # Add current file to the list of opened files
        opened_dependency_files.append(normalized_dependency_file)
        if not await wrapp_url_exists(normalized_dependency_file):
            # File is missing, not installed (yet). Go find the source in the repo if possible
            found = False
            if subpackage_repo is not None:
                dependency_package_file = wrapp_file_in_repo(subpackage_repo, dependency.package, dependency.version)
                found = await wrapp_url_exists(dependency_package_file)

                # Check again for circular dependency with the repo file - normalize paths before comparison
                normalized_dependency_file = normalize_url(dependency_package_file)
                if normalized_dependency_file in opened_dependency_files:
                    raise FailedCommand(
                        f"Circular dependency detected: {dependency_package_file} is already being processed in the dependency chain"
                    )

                # Add repo file to the list of opened files if it exists
                if found:
                    opened_dependency_files.append(normalized_dependency_file)
            if not found:
                raise FailedCommand(
                    f"Failed to load package file of recorded dependency at {dependency_package_file} imported by '{named_package_setup.package.name}', package incomplete!"
                )
        if context.verbose:
            logger.info(f"Building dependency graph: reading {normalized_dependency_file}")
        dependency_setup = await load_package_setup(normalized_dependency_file)
        if dependency_setup is None:
            raise FailedCommand(f"Failed to parse package file at {normalized_dependency_file}")

        # Build subtree - pass the opened_dependency_files list to track the dependency chain
        subtree_graph = await DependencyGraph.build_dependency_graph(
            context,
            task_node,
            LoadedPackage(normalized_dependency_file, dependency_setup),
            join_paths(root, dependency.relative_destination),
            ignore_version_conflicts=ignore_version_conflicts,
            fallback_repo=Repository(repo_url=dependency.repository),
            opened_dependency_files=opened_dependency_files,  # Pass the list of opened files
        )
        results.append((dependency_setup, subtree_graph))

    @staticmethod
    async def build_dependency_graph(
        context: CommandParameters,
        task_node: SchedulerTreeNode,
        named_package_setup: LoadedPackage,
        root: str = "/",
        ignore_version_conflicts=False,
        fallback_repo: Optional[Repository] = None,
        opened_dependency_files: Optional[list[str]] = None,
    ) -> "DependencyGraph":
        # Initialize tracking of opened dependency files if not provided
        if opened_dependency_files is None:
            opened_dependency_files = []

        # Add current package file to the list if not already there
        if named_package_setup.wrapp_file_url not in opened_dependency_files:
            opened_dependency_files.append(named_package_setup.wrapp_file_url)
        G = nx.DiGraph()
        # Add root to graph
        root_node = DependencyNode(named_package_setup.package.name, root)
        G.add_node(
            root_node.id, **{"package": named_package_setup.package.name, "path": root, "version": named_package_setup.package.version}
        )
        if named_package_setup.package.dependencies:
            subgraphs: list[tuple[PackageInfo, "DependencyGraph"]] = []
            async with task_node.create_task_group_node(
                f"Analysing {len(named_package_setup.package.dependencies)} dependencies of '{named_package_setup.package.name}' '{named_package_setup.package.version}'",
                expected_num_sub_jobs=len(named_package_setup.package.dependencies),
            ) as sub_task_node:
                for dependency in named_package_setup.package.dependencies:
                    sub_task_node.create_task(
                        DependencyGraph._build_subgraph_for_dependency(
                            context=context,
                            results=subgraphs,
                            task_node=sub_task_node,
                            named_package_setup=named_package_setup,
                            dependency=dependency,
                            root=root,
                            ignore_version_conflicts=ignore_version_conflicts,
                            fallback_repo=fallback_repo,
                            opened_dependency_files=opened_dependency_files.copy(),  # Pass a copy to avoid shared state issues
                        ),
                        name="Building package dependency graph",
                    )
                    await asyncio.sleep(0)

            for dependency_setup, subtree_graph in subgraphs:
                # Now we need to check this subtree can be added without conflicts. For this, inspect common nodes,
                # which are the same package in the same directory
                for common_node in [node for node in G.nodes if node in subtree_graph.G.nodes]:
                    base_version = G.nodes[common_node]["version"]
                    package_version = subtree_graph.G.nodes[common_node]["version"]
                    common_node_info = DependencyNode.from_id(common_node)
                    if not ignore_version_conflicts and base_version != package_version:
                        raise FailedCommand(
                            f"Version conflict: Both version {base_version} and version {package_version} of package {common_node_info.package} want to be installed into {common_node_info.url}"
                        )

                # Add the new graph into the existing graph
                DependencyGraph.add_subgraph(G, subtree_graph)

                # Link the subgraph into the upper level
                G.add_edge(root_node.id, subtree_graph.root, usage_of_version=dependency_setup.version)
                await asyncio.sleep(0)
        else:
            # Add an empty node, so our estimate of the expected jobs is correct
            task_node.create_tree_node("Adding package with no dependencies")

        return DependencyGraph(G, root_node.id)

    def find_version_conflicts(self) -> Tuple[List[VersionConflict], List[str]]:
        """
        Evaluate the dependency tree and check that all nodes are referenced with exactly one version
        Returns a list of VersionConflict items pointing at problems in the tree, and additionally a list of extra roots, i.e. nodes that are not the
        known root but have no incoming edges
        """
        # We need to loop all nodes and check if the incoming edges have the same version.
        # If not, need to define some logic to resolve this
        extra_roots = []
        conflicts = []
        for node in self.G.nodes:
            versions = set()
            for u, v, attrs in self.G.in_edges(node, data=True):
                if "usage_of_version" not in attrs:
                    raise FailedCommand(f"Program error: could not determine version requirements for node {node} in the dependency graph")
                versions.add(attrs["usage_of_version"])
            if len(versions) == 0:
                if node != self.root:
                    logger.debug(f"Found {node} which is not {self.root}")
                    extra_roots.append(node)
            elif len(versions) > 1:
                conflicts.append(VersionConflict(node=node, versions=versions))
        return conflicts, extra_roots

    def get_package_list(self, absolute_url: str, root_package_name: str, packages: Dict[str, PackageRecord]) -> List[PackageRecord]:
        return self._get_package_list(absolute_url, DependencyNode(root_package_name, ""), packages)

    def get_root_package_list(self, root_url: str, root_package_name: str, packages: Dict[str, PackageRecord]) -> List[PackageRecord]:
        result: List[PackageRecord] = []
        root_node = DependencyNode(root_package_name, "")
        neighbors = list(self.G.successors(root_node.id))
        for dependency in neighbors:
            if dependency in packages:
                result.append(packages[dependency])
        return result

    def _get_package_list(self, absolute_url: str, root: DependencyNode, packages: Dict[str, PackageRecord]) -> List[PackageRecord]:
        """
        Traverse the graph depth first and return the list of packages it contains, leafs first
        """
        visited: Set[str] = set()
        result: List[str] = []

        def dfs(node: str) -> None:
            """Recursive DFS helper function for post-order traversal"""
            visited.add(node)

            # Get neighbors (successors)
            neighbors = list(self.G.successors(node))

            # Sort neighbors for deterministic traversal if requested
            if True:
                neighbors.sort()

            # First visit all neighbors in a depth-first manner
            for neighbor in neighbors:
                if neighbor not in visited:
                    dfs(neighbor)

            # Then add the current node to the result (post-order)
            result.append(node)

        dfs(self.root)

        package_records = []
        for node in result:
            if node in packages:
                package_records.append(packages[node])
        return package_records


async def _validate_dependency_exists(dependency: Dependency):
    """Given a dependency from a package file, raise a FailedCommand exception if the dependency is invalid"""

    # Check that the dependency still exists in the repo it was installed from
    dep_repo = Repository(repo_url=dependency.repository)
    if not await wrapp_url_exists(wrapp_file_in_repo(dep_repo, dependency.package, dependency.version)):
        raise FailedCommand(
            f"Sub-package does not exist in repository: '{dependency.package}' version '{dependency.version}' not in '{dependency.repository}'"
        )
    else:
        logger.debug(
            f"Successfully validated dependency '{dependency.package}' version '{dependency.version}' found in '{dependency.repository}'"
        )


async def _collect_recursive_dependency(
    task_node: SchedulerTaskGroupNode,
    named_package_setup: LoadedPackage,
    dependency: Dependency,
    from_repo: bool,
    dependency_upgrade_allowed: bool,
    check_deps_exist_in_repo: Optional[Repository],
    results: list[set[str]],
):
    if from_repo:
        # Load the dependent package from the repo, not from the workspace
        repo = Repository(repo_url=dependency.repository)
        dependency_install_path = repo.package_url(dependency.package, dependency.version)
        dependency_package_file = wrapp_file_in_repo(repo, dependency.package, dependency.version)
        if not await wrapp_url_exists(dependency_package_file):
            raise FailedCommand(
                f"Target repository '{repo.package_cache_url()}' is missing '{dependency.package}' version '{dependency.version}'. "
            )
    else:
        # Load the dependent package from the installation location
        dependency_install_path = named_package_setup.dependency_install_path(dependency)
        dependency_package_file = join_paths(dependency_install_path, f".{dependency.package}.wrapp")
        if not await wrapp_url_exists(dependency_package_file):
            raise FailedCommand(
                f"Failed to collect files of recorded dependency at {dependency_package_file} imported by '{named_package_setup.package.name}', package incomplete!"
            )
    if check_deps_exist_in_repo:
        if not await wrapp_url_exists(wrapp_file_in_repo(check_deps_exist_in_repo, dependency.package, dependency.version)):
            create_command_missing = f"wrapp create --package {dependency_package_file} --new-version {dependency.version} --repo {check_deps_exist_in_repo.package_cache_url()}"
            raise FailedCommand(
                f"Target repository '{check_deps_exist_in_repo.package_cache_url()}' is missing '{dependency.package}' version '{dependency.version}'. "
                f"It can be created via '{create_command_missing}'"
            )

    dependency_setup: Optional[PackageInfo] = await load_package_setup(dependency_package_file)
    if dependency_setup is None:
        raise FailedCommand(f"Failed to load catalog file {dependency_package_file}")
    else:
        if not dependency_upgrade_allowed:
            # Strict check - name and version needs to be identical for the dependency to be valid
            if dependency_setup.name != dependency.package or dependency_setup.version != dependency.version:
                raise FailedCommand(
                    f"Detected upgraded dependency at {dependency_package_file}: Expected package '{dependency.package}' version '{dependency.version}' "
                    f"but found '{dependency_setup.name}' version '{dependency_setup.version}'"
                )
        else:
            # Simple check - this would only fire if the dependency entry points to a package file with a wrong name in it
            # That's a data corruption
            if dependency_setup.name != dependency.package:
                raise FailedCommand(
                    f"Detected broken dependency at {dependency_package_file}: Expected package '{dependency.package}' "
                    f"but found '{dependency_setup.name}' at '{dependency_package_file}'"
                )
        skip_files: set[str] = set()
        skip_files.add(dependency_package_file)
        if dependency_setup.catalog is None:
            raise FailedCommand(f"Discovered sub package without catalog: {dependency_package_file} - please remove empty package")
        elif dependency_setup.repository:
            await _validate_dependency_exists(dependency)
            if dependency_setup.repository.rstrip("/") != dependency.repository.rstrip("/"):
                dep_repo = Repository(repo_url=dependency_setup.repository)
                if not await dep_repo.find_package(dependency_setup.name, dependency_setup.version):
                    raise FailedCommand(
                        f"Sub-package does not exist in repository: '{dependency.package}' version '{dependency.version}' not in '{dependency_setup.repository}'"
                    )
                else:
                    logger.debug(
                        f"Dependency '{dependency.package}' version '{dependency.version}' installed from {dependency.repository} has package referencing repository '{dependency_setup.repository}'. Both exist, but are they accessible for users?"
                    )

            for item in dependency_setup.catalog.items:
                absolute_path = join_paths(dependency_install_path, item.relative_path)
                skip_files.add(absolute_path)
            results.append(skip_files)
            more_skip_files = await collect_recursive_dependencies(
                task_node,
                LoadedPackage(dependency_package_file, dependency_setup),
                from_repo=from_repo,
                dependency_upgrade_allowed=False,
                check_deps_exist_in_repo=check_deps_exist_in_repo,
            )
            results.extend(more_skip_files)

        else:
            raise FailedCommand("Sub-package dependency has an empty repository field - please remove draft package.")


async def collect_recursive_dependencies(
    task_node: SchedulerTaskGroupNode,
    named_package_setup: LoadedPackage,
    from_repo: bool,
    dependency_upgrade_allowed: bool,
    check_deps_exist_in_repo: Optional[Repository],
) -> list[set[str]]:
    results: list[set[str]] = []
    if named_package_setup.package.dependencies:
        async with task_node.create_task_group_node(
            f"Collecting dependencies for '{named_package_setup.package.name}' '{named_package_setup.package.version}'"
        ) as sub_task_node:
            for dependency in named_package_setup.package.dependencies:
                sub_task_node.create_task(
                    _collect_recursive_dependency(
                        task_node=sub_task_node,
                        named_package_setup=named_package_setup,
                        dependency=dependency,
                        from_repo=from_repo,
                        dependency_upgrade_allowed=dependency_upgrade_allowed,
                        check_deps_exist_in_repo=check_deps_exist_in_repo,
                        results=results,
                    ),
                    name="Collect dependencies",
                )
    return results


async def collect_dependencies_in_tree(
    context: CommandParameters, named_package_setup: LoadedPackage, root: str, task_node: SchedulerTreeNode
) -> Tuple[PackageInfo, List[PackageRecord], List[PackageRecord]]:
    """Given a loaded root package, build a dependency graph from this and all wrapp files found in the subtree. Then
    return an extended package with the additional files found registered as direct dependencies, plus the list of all
    packages loaded, and the list of all packages referenced directly by the root package.

    :param context - the wrapp Context running this composite operation
    :param named_package_setup - the root package already loaded into memory, with its location
    :param root - the root path of the loaded package, must match (?) the root of the named_package_setup's catalog root
    :param task_node - the TaskGroup to use for parallelization of sub-operations.

    :returns a 3-tuple of
        1. a new PackageInfo object being a deepcopy of the named_package_setup handed in, with additional dependencies
        2. a list of all files loaded to prevent loading them again
        3. a list of all top-level dependency packages
    """
    task_node = task_node.create_tree_node(f"Finding all subpackages in '{root}'")

    real_dependencies = await collect_wrapp_files_in_tree(context, task_node, root)

    # Now build the dependency tree from the loaded package files and merge them, if possible, into the top level graph
    top_level_graph = await DependencyGraph.build_dependency_graph(context, task_node, named_package_setup, "")
    packages_added: Dict[str, PackageRecord] = {}
    for dependency in real_dependencies:
        relative_path = make_relative_url(root, dependency.install_base)
        subgraph = await DependencyGraph.build_dependency_graph(context, task_node, dependency, relative_path)
        DependencyGraph.add_subgraph(top_level_graph.G, subgraph)
        packages_added[subgraph.root] = PackageRecord(
            relative_path=relative_path, package=dependency.package, absolute_path=dependency.wrapp_file_url
        )

    # Now check the graph
    conflicts, extra_roots = top_level_graph.find_version_conflicts()
    if len(conflicts) > 0:
        raise FailedCommand(
            f'Version conflict when collecting all wrapp files in {root}: {", ".join([str(conflict) for conflict in conflicts])}'
        )

    extended_package = named_package_setup.package.model_copy(deep=True)
    for extra_root in extra_roots:
        extra: PackageInfo = packages_added[extra_root].package
        absolute_destination = get_base_directory_from_url(packages_added[extra_root].absolute_path)
        relative_destination = make_relative_url(root, absolute_destination)
        extra_url = packages_added[extra_root].absolute_path
        new_dependency = add_dependency_to_package(
            extended_package,
            package_name=extra.name,
            version=extra.version,
            repo=Repository(repo_url=extra.repository, fallback_base=extra_url),
            relative_destination=relative_destination,
        )
        top_level_graph.add_dependency(extended_package, "", new_dependency)

    packages_added[top_level_graph.root] = PackageRecord(
        relative_path="", package=named_package_setup.package, absolute_path=named_package_setup.wrapp_file_url
    )
    packages_found = top_level_graph.get_package_list(named_package_setup.install_base, named_package_setup.package.name, packages_added)
    root_packages = top_level_graph.get_root_package_list(
        named_package_setup.install_base, named_package_setup.package.name, packages_added
    )
    # top_level_graph.save_diagram(f"{named_package_setup.package.name}.png")
    return extended_package, packages_found, root_packages
