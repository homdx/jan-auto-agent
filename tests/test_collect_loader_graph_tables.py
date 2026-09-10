"""tests/test_collect_loader_graph_tables.py — V1: loader keeps the import graph.

`_load_from_dir` read 9 of the artifact's 13 keys. `import_edges`,
`imported_by` and `entry_points` were written by the producer on every run and
silently dropped on read, so "who calls X?" and "what does X call?" were
unanswerable from a loaded model even though the answer was sitting in the
artifact. This ticket keeps the three tables and puts three read-only queries
on top:

  V1-1  The three tables are populated from the artifact.
  V1-2  Defaults are empty containers, never `None`.
  V1-3  `callers_of` excludes test files by default and includes them when
        asked; ordering is the caller's own blast radius descending, then path,
        so `limit` truncates to the callers that matter.
  V1-4  `calls_into` is first-party only.
  V1-5  An artifact missing the keys loads with empty containers, not an
        absent model — the rest of the artifact stays usable.
  V1-6  An artifact whose keys are present but garbled still degrades to an
        absent model. Both schema directions.
  V1-7  Absent model -> `[]` / `()` / `None`, no exception.
  V1-8  The queries are read-only: they never mutate the model's tables.
  V1-9  Two `load()` calls agree (COLLECT-3 determinism).
  V1-10 Test-vs-shipped classification does not misfire on shipped modules whose
        names merely contain "test".

`risk_for` (the ticket's third query) predates this ticket and is unchanged;
V1-3's ordering leans on it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as cli_mod
from tools.collect import loader as loader_mod
from tools.collect.loader import (
    STATUS_ABSENT,
    STATUS_FRESH,
    CollectModel,
)
from tools.collect.risk import RiskEntry


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    # Seed contracts and the gate map are COLLECT-10/11 content, not under test
    # here; neutralising them keeps the graph deterministic.
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """A repo whose import graph is known by hand.

    Edges:  core <- mid <- {leaf1, leaf2};  core <- leaf3
            leaf1 <- tests/test_leaf1.py;  leaf2 <- tests/test_leaf2.py

    `mid` and `leaf3` each import a stdlib module (`json`, `os`) that must be
    dropped, and every test importer lives under `tests/` so `exclude_tests`
    has something to exclude.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "core.py").write_text(
        "import os\n"
        "\n"
        "def core_fn():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (pkg / "mid.py").write_text(
        "import json\n"
        "from pkg.core import core_fn\n"
        "\n"
        "def mid_fn():\n"
        "    return core_fn()\n",
        encoding="utf-8",
    )
    (pkg / "leaf1.py").write_text(
        "from pkg.mid import mid_fn\n"
        "\n"
        "def leaf1_fn():\n"
        "    return mid_fn()\n",
        encoding="utf-8",
    )
    (pkg / "leaf2.py").write_text(
        "import pkg.mid\n"
        "\n"
        "def leaf2_fn():\n"
        "    return pkg.mid.mid_fn()\n",
        encoding="utf-8",
    )
    (pkg / "leaf3.py").write_text(
        "import pkg.core\n"
        "\n"
        "def leaf3_fn():\n"
        "    return pkg.core.core_fn()\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_leaf1.py").write_text(
        "from pkg.leaf1 import leaf1_fn\n"
        "\n"
        "def test_leaf1():\n"
        "    assert leaf1_fn() == 1\n",
        encoding="utf-8",
    )
    (tests / "test_leaf2.py").write_text(
        "from pkg.leaf2 import leaf2_fn\n"
        "\n"
        "def test_leaf2():\n"
        "    assert leaf2_fn() == 1\n",
        encoding="utf-8",
    )

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _collect_and_load(mini_repo: Path) -> CollectModel:
    cli_mod.action_collect(mini_repo)
    return loader_mod.load(mini_repo)


# ─────────────────────────────────────────────────────────────────────────────
# V1-1 / V1-3 / V1-4 / V1-9 — a real artifact, loaded
# ─────────────────────────────────────────────────────────────────────────────

class TestGraphTablesLoaded:

    def test_three_tables_populated(self, mini_repo):
        """V1-1"""
        model = _collect_and_load(mini_repo)
        assert model.status == loader_mod.STATUS_FRESH
        assert set(model.import_edges) == set(model.imported_by) == {m.path for m in model.modules}
        assert model.import_edges["pkg/leaf1.py"] == ("pkg/mid.py",)
        assert model.imported_by["pkg/core.py"] == ("pkg/leaf3.py", "pkg/mid.py")
        assert model.entry_points == (
            "pkg/__init__.py",
            "pkg/leaf3.py",
            "tests/__init__.py",
            "tests/test_leaf1.py",
            "tests/test_leaf2.py",
        )

    def test_callers_of_excludes_tests_by_default(self, mini_repo):
        """V1-3: `leaf1` is imported only by its own test file."""
        model = _collect_and_load(mini_repo)
        assert model.callers_of("pkg/leaf1.py") == []
        assert model.callers_of("pkg/leaf1.py", exclude_tests=False) == ["tests/test_leaf1.py"]
        assert model.callers_of("pkg/leaf2.py", exclude_tests=False) == ["tests/test_leaf2.py"]

    def test_callers_of_orders_heaviest_caller_first(self, mini_repo):
        """V1-3: `mid`'s own blast radius is 2 (`leaf1` and `leaf2` depend on
        it) while `leaf3`'s is 0, so `mid` sorts first and `limit` keeps the
        caller that would carry a change furthest — not the first path found."""
        model = _collect_and_load(mini_repo)
        assert model.callers_of("pkg/core.py") == ["pkg/mid.py", "pkg/leaf3.py"]
        assert model.callers_of("pkg/core.py", limit=1) == ["pkg/mid.py"]
        assert model.callers_of("pkg/leaf3.py", exclude_tests=False) == []

    def test_callers_of_path_tiebreak_is_stable(self, mini_repo):
        """V1-3 / V1-9: `mid`'s two shipped importers tie at blast radius 1, so
        the path tiebreak decides the order."""
        model = _collect_and_load(mini_repo)
        assert model.callers_of("pkg/mid.py", exclude_tests=False) == [
            "pkg/leaf1.py", "pkg/leaf2.py",
        ]
        assert model.callers_of("pkg/mid.py", exclude_tests=False, limit=1) == ["pkg/leaf1.py"]

    def test_calls_into_is_first_party_only(self, mini_repo):
        """V1-4: `json` and `os` are in the import lists, and neither may leak
        into the answer."""
        model = _collect_and_load(mini_repo)
        assert model.calls_into("pkg/mid.py") == ["pkg/core.py"]
        assert model.calls_into("pkg/leaf3.py") == ["pkg/core.py"]
        assert model.calls_into("pkg/core.py") == []

    def test_unknown_path_is_empty(self, mini_repo):
        model = _collect_and_load(mini_repo)
        assert model.callers_of("pkg/does_not_exist.py") == []
        assert model.calls_into("pkg/does_not_exist.py") == []
        assert model.callers_of("") == []
        assert model.calls_into("") == []

    def test_two_loads_agree(self, mini_repo):
        """V1-9"""
        cli_mod.action_collect(mini_repo)
        first = loader_mod.load(mini_repo)
        second = loader_mod.load(mini_repo)
        for path in ("pkg/core.py", "pkg/mid.py", "pkg/leaf1.py", "pkg/leaf2.py", "pkg/leaf3.py"):
            assert first.callers_of(path) == second.callers_of(path)
            assert first.calls_into(path) == second.calls_into(path)
        assert first.entry_points == second.entry_points


# ─────────────────────────────────────────────────────────────────────────────
# V1-5 / V1-6 — both schema directions, over a real artifact
# ─────────────────────────────────────────────────────────────────────────────

def _rewrite_artifact(mini_repo: Path, mutate) -> CollectModel:
    """Collect for real, then apply `mutate` to the on-disk artifact and reload.

    The manifest and the source tree are untouched, so the artifact is still
    judged fresh — only its key set changes.
    """
    cli_mod.action_collect(mini_repo)
    collect_dir = cli_mod.resolve_collect_dir(mini_repo, None)
    path = collect_dir / cli_mod.ARTIFACT_FILENAME
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return loader_mod.load(mini_repo)


class TestGraphTablesSchema:

    def test_missing_graph_keys_load_with_empty_containers(self, mini_repo):
        """V1-5: an older producer's artifact is not an absent model."""
        def _drop(payload):
            for key in ("import_edges", "imported_by", "entry_points"):
                payload.pop(key, None)

        model = _rewrite_artifact(mini_repo, _drop)
        assert model.status == loader_mod.STATUS_FRESH
        assert model.import_edges == {}
        assert model.imported_by == {}
        assert model.entry_points == ()
        # The rest of the artifact survived, so callers still get real data.
        assert model.module("pkg/core.py") is not None
        assert model.callers_of("pkg/core.py") == []
        assert model.calls_into("pkg/mid.py") == []

    def test_null_graph_keys_load_with_empty_containers(self, mini_repo):
        """V1-5: a present-but-null key is "the producer did not write this"."""
        model = _rewrite_artifact(mini_repo, lambda p: p.update({
            "import_edges": None, "imported_by": None, "entry_points": None,
        }))
        assert model.status == loader_mod.STATUS_FRESH
        assert (model.import_edges, model.imported_by, model.entry_points) == ({}, {}, ())

    def test_malformed_graph_table_yields_absent(self, mini_repo):
        """V1-6: present-and-garbled is a corrupt artifact, not empty data."""
        model = _rewrite_artifact(mini_repo, lambda p: p.update({"import_edges": "garbage"}))
        assert model.status == loader_mod.STATUS_ABSENT
        assert model.module("pkg/core.py") is None

    def test_malformed_imported_by_yields_absent(self, mini_repo):
        model = _rewrite_artifact(mini_repo, lambda p: p.update({"imported_by": [{"not": "a dict"}]}))
        assert model.status == loader_mod.STATUS_ABSENT

    def test_malformed_entry_points_yields_absent(self, mini_repo):
        model = _rewrite_artifact(mini_repo, lambda p: p.update({"entry_points": "not-a-list"}))
        assert model.status == loader_mod.STATUS_ABSENT

    def test_malformed_graph_neighbour_yields_absent(self, mini_repo):
        model = _rewrite_artifact(mini_repo, lambda p: p.update({
            "import_edges": {"pkg/core.py": ["pkg/mid.py", 7]},
        }))
        assert model.status == loader_mod.STATUS_ABSENT


# ─────────────────────────────────────────────────────────────────────────────
# V1-2 / V1-7 / V1-8 / V1-10 — model-level, no collect run needed
# ─────────────────────────────────────────────────────────────────────────────

def _entry(path: str, blast: int) -> RiskEntry:
    return RiskEntry(
        path=path, loc=0, blast_radius=blast, unguarded_count=0,
        undocumented_fail_open_count=0, zero_coverage=False, score=0,
    )


def _model(**kw) -> CollectModel:
    return CollectModel(status=STATUS_FRESH, **kw)


class TestModelLevel:

    def test_defaults_are_empty_containers_not_none(self):
        """V1-2"""
        model = CollectModel(status=STATUS_FRESH)
        assert model.import_edges == {}
        assert model.imported_by == {}
        assert model.entry_points == ()

    def test_absent_model_returns_empty_everywhere(self):
        """V1-7"""
        model = CollectModel(status=STATUS_ABSENT)
        assert model.callers_of("pkg/core.py") == []
        assert model.callers_of("pkg/core.py", exclude_tests=False) == []
        assert model.calls_into("pkg/core.py") == []
        assert model.risk_for("pkg/core.py") is None
        assert model.entry_points == ()

    def test_risk_for_is_unchanged(self):
        """The ticket's third query predates this one; pin it."""
        model = _model(risk_index=(_entry("pkg/core.py", 2),))
        assert model.risk_for("pkg/core.py").blast_radius == 2
        assert model.risk_for("pkg/unknown.py") is None

    def test_callers_of_orders_by_blast_radius_then_path(self):
        """V1-3: `b` and `c` tie on blast radius 9, so path decides; `z` has no
        risk entry and sorts last; `limit` keeps the head."""
        model = _model(
            imported_by={"x/target.py": ("tools/b.py", "tools/a.py", "tools/z.py", "tools/c.py")},
            risk_index=(_entry("tools/a.py", 5), _entry("tools/b.py", 9), _entry("tools/c.py", 9)),
        )
        assert model.callers_of("x/target.py") == [
            "tools/b.py", "tools/c.py", "tools/a.py", "tools/z.py",
        ]
        assert model.callers_of("x/target.py", limit=2) == ["tools/b.py", "tools/c.py"]
        assert model.callers_of("x/target.py", limit=0) == [
            "tools/b.py", "tools/c.py", "tools/a.py", "tools/z.py",
        ]
        assert model.callers_of("x/target.py", limit=-1) == [
            "tools/b.py", "tools/c.py", "tools/a.py", "tools/z.py",
        ]

    def test_queries_do_not_mutate_the_model(self):
        """V1-8"""
        model = _model(
            import_edges={"pkg/core.py": ("pkg/mid.py",)},
            imported_by={"pkg/core.py": ("pkg/leaf3.py", "pkg/mid.py")},
            entry_points=("pkg/core.py",),
            risk_index=(_entry("pkg/mid.py", 2),),
        )
        assert model.callers_of("pkg/core.py") == ["pkg/mid.py", "pkg/leaf3.py"]
        assert model.calls_into("pkg/core.py") == ["pkg/mid.py"]
        assert model.import_edges["pkg/core.py"] == ("pkg/mid.py",)
        assert model.imported_by["pkg/core.py"] == ("pkg/leaf3.py", "pkg/mid.py")
        assert model.entry_points == ("pkg/core.py",)

    def test_test_path_classification_boundaries(self):
        """V1-10: shipped modules whose names merely contain "test" must not be
        filtered out of a caller list, and a test file the artifact recorded
        outside the well-known directories must still be recognised."""
        recorded = frozenset({"examples/hello-world/test_hello.py"})
        shipped = [
            "tools/collect/test_map.py",
            "scripts/sync_test_tiers.py",
            "tools/auto/inner_loop.py",
            "tools/testing.py",
            "testmap.py",
        ]
        tests = [
            "tests/test_x.py",
            "tests_bugfix/test_y.py",
            "tests_slow/test_z.py",
            "stub-test/stub_codeapp_server.py",
            ".smoke_tests/test_q.py",
            ".regression_tests/test_r.py",
            "tests/fixtures/collect_mini_repo/pkg/__init__.py",
            "conftest.py",
            "tools/conftest.py",
        ]
        for path in shipped:
            assert not CollectModel._is_test_path(path, recorded), path
        for path in tests:
            assert CollectModel._is_test_path(path, recorded), path
        # The recorded signal is what rescues a test file that sits outside the
        # known test directories — and nothing else.
        assert CollectModel._is_test_path("examples/hello-world/test_hello.py", recorded)
        assert not CollectModel._is_test_path(
            "examples/hello-world/test_hello.py", frozenset()
        )
        assert not CollectModel._is_test_path("", recorded)

    def test_excluding_tests_uses_the_artifacts_own_record(self):
        """`test_map` values are authoritative for whatever directory they name,
        so a test importer recorded there is excluded even outside the known
        test directories."""
        model = _model(
            imported_by={"tools/auto/coder.py": (
                "tools/auto/inner_loop.py",
                "examples/hello-world/test_hello.py",
            )},
            test_map={"tools/auto/coder.py": ("examples/hello-world/test_hello.py",)},
        )
        assert model.callers_of("tools/auto/coder.py") == ["tools/auto/inner_loop.py"]
        assert model.callers_of("tools/auto/coder.py", exclude_tests=False) == [
            "examples/hello-world/test_hello.py", "tools/auto/inner_loop.py",
        ]

    def test_excluding_tests_can_empty_the_list(self):
        """The ticket's stated reason for the default: a module imported only by
        its own tests has no shipped callers."""
        model = _model(imported_by={
            "tools/auto/coder.py": ("tests/test_coder.py", "tests_bugfix/test_coder_b.py"),
        })
        assert model.callers_of("tools/auto/coder.py") == []
        assert model.callers_of("tools/auto/coder.py", exclude_tests=False) == [
            "tests/test_coder.py", "tests_bugfix/test_coder_b.py",
        ]
