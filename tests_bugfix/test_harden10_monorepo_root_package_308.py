"""tests_bugfix/test_harden10_monorepo_root_package_308.py — HARDEN-10 D: a package at the
repository root keeps its imports in a monorepo.

HARDEN-6 D counts an alias claimed by two modules as contested and resolves it for nobody. In
a layout with `app/` at the repository root beside `svc_a/app/` and `svc_b/app/`, the root
package's own `app.models` is among the claimants only by accident of the alias — it is the
module's *own* dotted name, not a contested alias. Counting it made `app.models` contested for
nobody, so `app/views.py` and the root's tests looked like they import nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.collect.graph import build_module_index, import_edges, resolve_import  # noqa: E402
from tools.collect.model import ModuleRecord  # noqa: E402


def _module(path: str, imports=()) -> ModuleRecord:
    return ModuleRecord(path=path, imports=tuple(imports))


def _root_package_monorepo():
    """`app/` at the repository root, beside two sub-projects that carry their own `app/`."""
    return [
        _module("app/__init__.py"),
        _module("app/models.py"),
        _module("app/views.py", ("app.models",)),
        _module("svc_a/app/__init__.py"),
        _module("svc_a/app/models.py"),
        _module("svc_a/app/views.py", ("app.models",)),
        _module("svc_b/app/__init__.py"),
        _module("svc_b/app/models.py"),
        _module("svc_b/app/views.py", ("app.models",)),
        _module("tests/test_root.py", ("app.models",)),
    ]


def _two_services():
    """HARDEN-6 D's layout: two sub-projects and no package at the repository root."""
    return [
        _module("svc_a/app/__init__.py"),
        _module("svc_a/app/models.py"),
        _module("svc_b/app/__init__.py"),
        _module("svc_b/app/models.py"),
        _module("svc_b/app/views.py", ("app.models",)),
    ]


class TestTheRootPackageKeepsItsImports:
    """HARDEN-10 D: the ticket's table."""

    def test_the_root_package_views_resolve_to_the_root_models(self):
        index = build_module_index(_root_package_monorepo())
        assert resolve_import("app.models", index, "app/views.py") == "app/models.py"

    def test_the_roots_tests_resolve_to_the_root_models(self):
        index = build_module_index(_root_package_monorepo())
        assert resolve_import("app.models", index, "tests/test_root.py") == "app/models.py"

    def test_the_sub_projects_still_resolve_inside_themselves(self):
        # HARDEN-6 D: a sub-project's import names its own module, not the root's.
        index = build_module_index(_root_package_monorepo())
        assert resolve_import("app.models", index, "svc_a/app/models.py") == "svc_a/app/models.py"
        assert resolve_import("app.models", index, "svc_a/app/views.py") == "svc_a/app/models.py"
        assert resolve_import("app.models", index, "svc_b/app/views.py") == "svc_b/app/models.py"

    def test_the_root_alias_is_not_contested(self):
        index = build_module_index(_root_package_monorepo())
        # `app.models` is the root module's own dotted name, not an alias two modules claim.
        assert "app.models" not in index.ambiguous_aliases


class TestTheGraphShowsTheEdges:
    """The root package's views and its tests are no longer entry points with no importers."""

    def _edges(self):
        return import_edges(_root_package_monorepo())

    def test_the_root_package_views_have_their_import(self):
        assert self._edges()["app/views.py"] == frozenset({"app/models.py"})

    def test_the_roots_tests_have_their_import(self):
        assert self._edges()["tests/test_root.py"] == frozenset({"app/models.py"})

    @pytest.mark.parametrize("importer, target", [
        ("svc_a/app/views.py", "svc_a/app/models.py"),
        ("svc_b/app/views.py", "svc_b/app/models.py"),
    ])
    def test_the_sub_project_edges_are_unchanged(self, importer, target):
        assert self._edges()[importer] == frozenset({target})

    def test_the_root_models_has_importers_from_outside_the_sub_projects(self):
        importers = [
            p for p, targets in self._edges().items() if "app/models.py" in targets
        ]
        assert sorted(importers) == ["app/views.py", "tests/test_root.py"]


class TestTheHARDEN6DLayoutIsUnchanged:
    """Two services and no root package: the alias stays contested for nobody."""

    def test_the_alias_stays_contested(self):
        index = build_module_index(_two_services())
        assert "app.models" in index.ambiguous_aliases

    def test_an_importer_outside_both_services_gets_nothing(self):
        index = build_module_index(_two_services())
        assert resolve_import("app.models", index, "main.py") is None
        assert resolve_import("app.models", index, None) is None
        assert import_edges(_two_services() + [_module("main.py", ("app.models",))])["main.py"] \
            == frozenset()

    def test_each_service_still_resolves_inside_itself(self):
        index = build_module_index(_two_services())
        assert resolve_import("app.models", index, "svc_a/app/models.py") == "svc_a/app/models.py"
        assert resolve_import("app.models", index, "svc_b/app/views.py") == "svc_b/app/models.py"
        assert import_edges(_two_services())["svc_b/app/views.py"] == frozenset({"svc_b/app/models.py"})


class TestTheSubProjectsStillDoNotSeeTheRootPackage:
    """The root package is an answer only for an importer outside every sub-project's root."""

    def test_a_sub_project_importer_never_gets_the_root_module(self):
        index = build_module_index(_root_package_monorepo())
        for importer in ("svc_a/app/views.py", "svc_b/app/views.py",
                         "svc_a/app/__init__.py", "svc_b/app/__init__.py"):
            assert resolve_import("app.models", index, importer) != "app/models.py", importer
