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

"""Temporary pytest-bdd 9.0 compatibility with pytest 9.2 fixture registries.

Remove when pytest-bdd supports the node-based registry itself:
https://github.com/pytest-dev/pytest-bdd/blob/master/src/pytest_bdd/scenario.py
This plugin is loaded by both source and copied wheel test suites.
"""

import importlib
from contextlib import contextmanager
from importlib.metadata import version


def pytest_configure(config):
    if version("pytest-bdd") != "9.0.0":
        return
    scenario = importlib.import_module("pytest_bdd.scenario")
    original = scenario.inject_fixturedefs_for_step

    @contextmanager
    def inject_fixturedefs_for_step(step, fixturemanager, node):
        if hasattr(fixturemanager, "_arg2fixturedefs"):
            with original(step, fixturemanager, node):
                yield
            return

        # Query visibility through pytest, including legacy nodeid registrations.
        names = dict.fromkeys(fixturemanager._arg2node2fixturedefs)
        names.update(dict.fromkeys(fixturemanager._arg2nodeid2fixturedefs))
        by_node = {}
        by_nodeid = {}
        for name in names:
            for definition in fixturemanager.getfixturedefs(name, node) or ():
                context = scenario.step_function_context_registry.get(definition.func)
                if context is None or context.type not in (None, step.type):
                    continue
                if not context.parser.is_matching(step.name):
                    continue
                # pytest stores definitions at each node in most-specific-first
                # order, then reverses them when resolving a requested fixture.
                if definition.node is None:
                    by_nodeid.setdefault(definition.baseid, []).insert(0, definition)
                else:
                    by_node.setdefault(definition.node, []).insert(0, definition)

        if not by_node and not by_nodeid:
            yield
            return

        name = scenario.get_step_fixture_name(step=step)
        registries = (
            (fixturemanager._arg2node2fixturedefs, by_node),
            (fixturemanager._arg2nodeid2fixturedefs, by_nodeid),
        )
        previous = [(registry, registry.get(name)) for registry, _ in registries]
        for registry, definitions in registries:
            registry[name] = definitions
        try:
            yield
        finally:
            for registry, definitions in previous:
                if definitions is None:
                    registry.pop(name, None)
                else:
                    registry[name] = definitions

    scenario.inject_fixturedefs_for_step = inject_fixturedefs_for_step
    config.add_cleanup(lambda: setattr(scenario, "inject_fixturedefs_for_step", original))
