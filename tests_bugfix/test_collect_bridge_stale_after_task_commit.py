"""tests_bugfix/test_collect_bridge_stale_after_task_commit.py — V9:
freshness is checked once and then believed all run.

Bug (reproduced against the live code and the real artifact):

    at run start:                     usable=True  status=fresh
    after a task edits the tree:      usable=True  status=fresh
    does the block know the new symbol?  False
    header still reads:               "COLLECT MODEL (static facts,
                                      do not contradict):"

`collect_bridge.py`'s own docstring promised that a stale model is treated
exactly like an absent one — but `status` is computed once inside
`load()`, the bridge is cached for the lifetime of the `Controller`, and
`--auto` edits and commits source files in between. So every task after the
first edit was served pre-edit facts under a header that says they are
ground truth: a symbol list, a `guarded` count and a `risk` score describing
the file as it was before the run started.

This file pins the fix — invalidate on write, not on a timer:

  1. `CollectBridge.invalidate(paths)` marks the paths a commit changed
     dirty; `context_for` / `pull_symbol` / `module_symbols` /
     `contracts_for_symbol` return nothing for a dirty path.
  2. Clean paths are untouched: one edited file must not blind the pack for
     the other hundreds, for the rest of the run.
  3. The dirty check runs BEFORE `_shrink`, so a dirty path spends neither
     the shrink LLM call nor a shrunk block of pre-edit facts.
  4. `tools.collect.loader.load` is still called exactly once per run.
  5. `[collect] auto_refresh_between_tasks = true` repairs a dirty path via
     the existing incremental `action_module` — one module, one LLM call —
     and folds the fresh `ModuleRecord` back into the in-memory model, so a
     5-task run where tasks 1 and 3 touch the same file gives task 3 the
     post-task-1 facts.
  6. `collect_miss(reason="dirty")` is counted, and `invalidate()` fails
     open on malformed input.
"""

from __future__ import annotations

import configparser
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tools.auto.collect_bridge import CollectBridge, make_collect_bridge
from tools.auto.controller import AutoController, RunLimits
from tools.auto.git_manager import GitManager
from tools.collect import cli as cli_mod
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect.model import (
    ContractRecord,
    FunctionRecord,
    ModuleRecord,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(text)
    return cfg


def _collect_ini(**extra: str) -> str:
    """A `[collect]` section with `llm_summaries = false`, so no test in this
    file ever needs a live provider config."""
    lines = [
        "[collect]",
        "dir = .collect",
        "use_in_auto = true",
        "use_in_doc = false",
        "staleness = warn",
        "llm_summaries = false",
    ]
    lines.extend(f"{k} = {v}" for k, v in extra.items())
    return "\n".join(lines) + "\n"


def _symbol(qualname: str, signature: str = "", lineno: int = 1) -> FunctionRecord:
    module = qualname.rsplit(":", 1)[-1].rsplit(".", 1)[0] if "." in qualname else qualname
    return FunctionRecord(qualname=qualname, module=module, lineno=lineno, signature=signature)


def _module(path: str, symbols=()) -> ModuleRecord:
    return ModuleRecord(path=path, public_symbols=tuple(symbols))


def _fresh_model(modules=(), contracts=()) -> CollectModel:
    return CollectModel(status=STATUS_FRESH, modules=tuple(modules), contracts=tuple(contracts))


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """Neutralise seed contracts/gates that reference real symbols not present
    in these synthetic mini repos."""
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """Three-module repo: two modules to edit, one to stay clean."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("def a():\n    return 1\n")
    (pkg / "b.py").write_text("def b():\n    return 2\n")
    (pkg / "c.py").write_text("def c():\n    return 3\n")
    # Keep the run's own output out of the task commits.
    (tmp_path / ".gitignore").write_text(".agent/\n.collect/\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _write_ini(root: Path, **extra: str) -> Path:
    ini = root / "agents.ini"
    ini.write_text(_collect_ini(**extra))
    return ini


def _counting_load(monkeypatch, counts: list):
    """Wrap `tools.collect.loader.load` so a test can count how often the
    model is built."""
    import tools.collect.loader as loader_mod

    real_load = loader_mod.load

    def _counting(*args, **kwargs):
        counts.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr("tools.collect.loader.load", _counting)


def _make_controller(root: Path, config_path: Path, git: GitManager | None = None) -> AutoController:
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "test goal"
    ctrl.base_dir = root
    ctrl.config_path = str(config_path)
    ctrl.agent_dir = root / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits = RunLimits()
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if config_path.exists():
        cfg.read(str(config_path), encoding="utf-8")
    ctrl.config = cfg
    ctrl.task_mode = "code"
    ctrl.state = MagicMock()
    ctrl.state.all_tasks.return_value = []
    ctrl.git = git
    ctrl.run_trace = None
    ctrl.progress_display = None
    ctrl.metrics_stream = None
    ctrl.auto_tuner = None
    return ctrl


def _task(task_id: str, target_file: str) -> dict:
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "target_files": [target_file],
        "dependencies": [],
        "acceptance_check": "true",
    }


# ── 1. the reproduction inverts ────────────────────────────────────────────


def test_context_for_blinds_the_edited_path_only(mini_repo):
    """The ticket's reproduction, inverted: after a task commits an edit to
    `pkg/a.py`, `context_for("pkg/a.py")` must return `""` — the "stale is
    treated exactly like absent" contract, applied per path."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")
    assert bridge is not None and bridge.usable is True

    before = bridge.context_for("pkg/a.py")
    assert "COLLECT MODEL (static facts, do not contradict):" in before

    bridge.invalidate(["pkg/a.py"])
    assert "pkg/a.py" in bridge.dirty_paths
    assert bridge.context_for("pkg/a.py") == ""


def test_unedited_path_keeps_its_block_for_the_whole_run(mini_repo):
    """One edited file must not blind the pack for the other modules — the
    clean paths keep serving real blocks for the rest of the run."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")

    clean_before = [bridge.context_for(f"pkg/{m}.py") for m in ("b", "c")]
    assert all("COLLECT MODEL" in b for b in clean_before)

    bridge.invalidate(["pkg/a.py"])
    for _ in range(5):
        for m, expected in zip(("b", "c"), clean_before):
            assert bridge.context_for(f"pkg/{m}.py") == expected
    assert "pkg/b.py" not in bridge.dirty_paths
    assert "pkg/c.py" not in bridge.dirty_paths


def test_pull_symbol_blinds_only_the_dirty_module():
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py"])

    assert bridge.pull_symbol("a") == ""
    assert bridge.pull_symbol("b") != ""
    assert "pkg/b.py" in bridge.pull_symbol("b")


def test_pull_symbol_prefers_a_clean_duplicate_over_a_dirty_one():
    """The same symbol defined in two modules: the edited module's record
    must not answer, but a clean duplicate elsewhere still wins."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:shared", "shared(x) -> STALE")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:shared", "shared(x) -> FRESH")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py"])

    block = bridge.pull_symbol("shared")
    assert block == "module: pkg/b.py\nsymbol: pkg/b.py:shared\nsignature: shared(x) -> FRESH"
    assert "STALE" not in block


def test_module_symbols_blinds_every_spelling_of_a_dirty_path():
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py"])

    for ref in ("pkg/a.py", "pkg/a", "pkg.a"):
        assert bridge.module_symbols(ref) == ""
    assert bridge.module_symbols("pkg/b.py") != ""
    assert bridge.module_symbols("pkg.b") != ""


def test_contracts_for_symbol_blinds_the_dirty_module():
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    contract_a = ContractRecord(name="never_raises_a", known_edge="pkg/a.py:a",
                                description="never raises")
    contract_b = ContractRecord(name="never_raises_b", known_edge="pkg/b.py:b",
                                description="never raises")
    bridge = CollectBridge(_fresh_model([module_a, module_b], contracts=[contract_a, contract_b]))
    bridge.invalidate(["pkg/a.py"])

    assert bridge.contracts_for_symbol("a") == []
    assert [c.name for c in bridge.contracts_for_symbol("b")] == ["never_raises_b"]


# ── 2. the dirty check runs before `_shrink` ──────────────────────────────


def test_dirty_path_never_reaches_shrink():
    """A dirty path must not spend the shrink LLM call and must not return a
    shrunk block of pre-edit facts. V9's check is deliberately placed before
    the budget check so `_shrink` is never even reached."""
    module = _module("pkg/a.py", [_symbol(f"pkg/a.py:f{i}", f"f{i}(x, y, z) -> int") for i in range(40)])
    bridge = CollectBridge(_fresh_model([module]), max_context_chars=100)

    calls = []

    def _shrink_call(system: str, user: str) -> str:
        calls.append(user)
        return "SHOULD NOT BE CALLED"

    bridge._summarizer_call = _shrink_call
    bridge.invalidate(["pkg/a.py"])

    assert bridge.context_for("pkg/a.py") == ""
    assert calls == []
    assert bridge.shrink_calls == 0


# ── 3. collect_miss(reason="dirty") ───────────────────────────────────────


def test_collect_miss_counts_dirty():
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py"])

    bridge.context_for("pkg/a.py")
    bridge.pull_symbol("a")
    bridge.module_symbols("pkg/a.py")
    bridge.contracts_for_symbol("a")
    bridge.context_for("pkg/b.py")   # clean path: no miss
    bridge.pull_symbol("b")          # clean path: no miss

    assert bridge.collect_misses == {"dirty": 4}


# ── 4. fail open ───────────────────────────────────────────────────────────


def test_invalidate_ignores_non_module_paths():
    """A commit that only touched docs or config must not blind the pack."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]))

    bridge.invalidate(["README.md", "agents.ini", "docs/COLLECT-24-SUMMARY.md",
                       ".agent/plan.json", ".collect/artifact.json"])
    assert bridge.dirty_paths == frozenset()
    assert bridge.context_for("pkg/a.py") != ""


def test_invalidate_is_fail_open_on_malformed_input():
    """`None`, an empty list, non-strings, absolute paths, a missing file and
    a generator that raises all degrade to "nothing was written" or to the
    paths that parsed — never an exception into a run."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]))

    for paths in (None, [], ("README.md", "agents.ini"),
                  [None, None], [123, b"bytes", object()],
                  ["pkg/never_seen.py"]):
        bridge.invalidate(paths)
    assert bridge.context_for("pkg/a.py") != ""

    def _exploding():
        yield "pkg/a.py"
        raise RuntimeError("boom")

    bridge.invalidate(_exploding())
    assert "pkg/a.py" in bridge.dirty_paths   # the one path that parsed
    assert bridge.context_for("pkg/a.py") == ""


def test_invalidate_normalises_task_and_git_path_spelling():
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]))
    bridge.invalidate(["./pkg/a.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for("pkg/a.py") == ""


# ── 5. [collect] auto_refresh_between_tasks ────────────────────────────────


def test_auto_refresh_defaults_to_false(mini_repo):
    """The feature is opt-in: with no key in `[collect]`, an edited path stays
    blinded for the rest of the run rather than spending an LLM call."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")

    assert bridge._auto_refresh is False
    bridge.invalidate(["pkg/a.py"])
    assert bridge.context_for("pkg/a.py") == ""
    assert "pkg/a.py" in bridge.dirty_paths


def test_auto_refresh_malformed_value_falls_back_to_false(mini_repo):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="not-a-boolean")
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")

    assert bridge is not None
    assert bridge._auto_refresh is False


def test_auto_refresh_repairs_the_path_and_folds_the_fresh_record(mini_repo, monkeypatch):
    """With the flag on, a dirty path is repaired via the existing incremental
    `action_module` and the fresh `ModuleRecord` is folded back into the
    in-memory model — `context_for` then returns the POST-edit facts, not
    `""`."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    counts: list = []
    _counting_load(monkeypatch, counts)
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")

    assert bridge.context_for("pkg/a.py").endswith("public_symbols: pkg/a.py:a")

    # Task 1 edits pkg/a.py and commits it.
    (mini_repo / "pkg" / "a.py").write_text(
        "def a():\n    return 1\n\n\ndef helper_from_task1():\n    return 42\n"
    )
    sha = GitManager(mini_repo).commit("auto(T1): add helper_from_task1")
    assert sha
    bridge.invalidate(GitManager(mini_repo).paths_changed_in(sha))

    block = bridge.context_for("pkg/a.py")
    assert "helper_from_task1" in block, "expected post-edit facts, got: %r" % block
    assert "pkg/a.py:a" in block
    assert "pkg/a.py" not in bridge.dirty_paths
    assert bridge.collect_misses == {}          # repaired, so nothing was withheld
    assert counts == [1], "load() must still be called exactly once per run"

    # A clean path is untouched by the repair.
    assert bridge.context_for("pkg/b.py").endswith("public_symbols: pkg/b.py:b")


def test_auto_refresh_unknown_path_stays_dirty(mini_repo):
    """A path the model has no record for cannot be repaired — inventing a
    record would be worse than a blind."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")

    bridge.invalidate(["pkg/never_collected.py"])
    assert bridge.context_for("pkg/never_collected.py") == ""
    assert "pkg/never_collected.py" in bridge.dirty_paths


def test_auto_refresh_failure_keeps_the_path_dirty():
    """A failing repair degrades to the blind — never to stale facts."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]), auto_refresh=True)

    def _broken(path: str):
        raise RuntimeError("provider down")

    bridge._module_refresh_fn = _broken
    bridge.invalidate(["pkg/a.py"])
    assert bridge.context_for("pkg/a.py") == ""
    assert "pkg/a.py" in bridge.dirty_paths
    assert "dirty" in bridge.collect_misses


def test_auto_refresh_rejects_a_record_from_another_module():
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    other = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module]), auto_refresh=True)
    bridge._module_refresh_fn = lambda path: other   # wrong module back

    bridge.invalidate(["pkg/a.py"])
    assert bridge.context_for("pkg/a.py") == ""
    assert "pkg/a.py" in bridge.dirty_paths


def test_auto_refresh_never_repairs_a_stale_model():
    """`usable` is False for a stale model anyway, so a repair would spend an
    LLM call that no block would ever be served."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(CollectModel(status="stale", modules=(module,)), auto_refresh=True)

    calls = []
    bridge._module_refresh_fn = lambda path: calls.append(path) or module
    bridge.invalidate(["pkg/a.py"])
    assert bridge.context_for("pkg/a.py") == ""
    assert calls == []


def test_auto_refresh_failure_is_attempted_once_per_path():
    """A repair that failed is not retried on every later lookup: each retry
    would re-run `action_module` — and re-pay its LLM call — for a path that
    is going to stay dirty anyway."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]), auto_refresh=True)
    attempts = []

    def _broken(path: str):
        attempts.append(path)
        raise RuntimeError("provider down")

    bridge._module_refresh_fn = _broken
    bridge.invalidate(["pkg/a.py"])
    for _ in range(4):
        assert bridge.context_for("pkg/a.py") == ""
        assert bridge.pull_symbol("a") == ""
    assert attempts == ["pkg/a.py"]


def test_auto_refresh_repairs_for_pull_symbol_and_contracts_too():
    """The repair is not `context_for`-only: Gate-1 grounding notes and the
    ArchProbe read through `pull_symbol` / `contracts_for_symbol`, and with
    the flag on they must see the post-edit record as well."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:a", "a() -> STALE")])
    fresh = _module("pkg/a.py", [_symbol("pkg/a.py:a", "a(x) -> FRESH")])
    contract = ContractRecord(name="never_raises_a", known_edge="pkg/a.py:a",
                              description="never raises")
    bridge = CollectBridge(_fresh_model([stale], contracts=[contract]), auto_refresh=True)
    bridge._module_refresh_fn = lambda path: fresh
    bridge.invalidate(["pkg/a.py"])

    assert "a(x) -> FRESH" in bridge.pull_symbol("a")
    assert "pkg/a.py" not in bridge.dirty_paths
    assert bridge.collect_misses == {}

    bridge.invalidate(["pkg/a.py"])
    assert [c.name for c in bridge.contracts_for_symbol("a")] == ["never_raises_a"]
    assert "pkg/a.py" not in bridge.dirty_paths


def test_invalidate_relativises_an_absolute_path_under_base_dir(tmp_path):
    """A coder that reports the absolute path it wrote must still blind the
    model's relative key; an absolute path outside the tree blinds nothing."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]), base_dir=tmp_path)
    bridge.invalidate([str(tmp_path / "pkg" / "a.py"), "/somewhere/else/pkg/b.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for(str(tmp_path / "pkg" / "a.py")) == ""


# ── 6. the controller wiring ───────────────────────────────────────────────


def _run_loop(ctrl, fake_outer):
    with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
         patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
         patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
        return ctrl._run_task_loop(task_mode="code")


def test_controller_invalidates_after_commit_and_loads_once(mini_repo, monkeypatch):
    """The controller knows which files each task touched — it commits them —
    and must mark exactly those dirty, with `load()` still called once."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    counts: list = []
    _counting_load(monkeypatch, counts)

    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [
        _task("T1", "pkg/a.py"),
        _task("T2", "pkg/b.py"),
        _task("T3", "pkg/c.py"),
    ]}

    fake_outer = MagicMock()
    fake_outer.run_task.return_value = SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    def _edit_then_pass(task, base_dir):
        (base_dir / task["target_files"][0]).write_text(
            f"def changed_by_{task['id']}():\n    return 0\n"
        )
        return fake_outer.run_task.return_value

    fake_outer.run_task.side_effect = _edit_then_pass

    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert len(counts) == 1, f"expected exactly 1 load() call for 3 tasks, got {len(counts)}"
    assert bridge.dirty_paths == frozenset({"pkg/a.py", "pkg/b.py", "pkg/c.py"})
    assert bridge.context_for("pkg/a.py") == ""
    assert bridge.context_for("pkg/b.py") == ""


def test_controller_invalidation_fails_open_without_git(mini_repo, monkeypatch):
    """No git (setup failed) means no commit hash and no `paths_changed_in` —
    the controller must still try the task's declared `target_files` and never
    abort the run."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    ctrl = _make_controller(mini_repo, config_path, git=None)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    fake_outer = MagicMock()
    fake_outer.run_task.return_value = SimpleNamespace(passed=True, rounds_used=1, inner_results=[])
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for("pkg/b.py") != ""


def test_controller_commit_that_touches_no_source_blinds_nothing(mini_repo, monkeypatch):
    """A commit that only touched a doc is not a source edit, so the pack
    must keep serving real blocks for every module."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "README.md")]}

    fake_outer = MagicMock()
    fake_outer.run_task.return_value = SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    def _write_doc(task, base_dir):
        (base_dir / "README.md").write_text("# docs only\n")
        return fake_outer.run_task.return_value

    fake_outer.run_task.side_effect = _write_doc
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset()
    assert bridge.context_for("pkg/a.py") != ""


def test_controller_git_failure_falls_back_to_target_files(mini_repo, monkeypatch):
    """git cannot be asked (index.lock, a hung hook) — the declared
    `target_files` are the fallback rather than "blind nothing"."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    from tools.auto.git_manager import GitError

    gm.paths_changed_in = MagicMock(side_effect=GitError("git diff-tree failed"))

    fake_outer = MagicMock()

    def _edit(task, base_dir):
        (base_dir / "pkg" / "a.py").write_text("def a():\n    return 9\n")
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _edit
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})


def test_controller_auto_refresh_gives_task3_the_post_task1_facts(mini_repo, monkeypatch):
    """The ticket's headline acceptance criterion: with
    `auto_refresh_between_tasks = true`, a 5-task run where tasks 1 and 3
    touch the same file gives task 3 the post-task-1 facts — and the model is
    still built only once for the whole run."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    counts: list = []
    _counting_load(monkeypatch, counts)

    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    targets = ["pkg/a.py", "pkg/b.py", "pkg/a.py", "pkg/c.py", "pkg/a.py"]
    ctrl.state.resume_info.return_value = {"pending": [
        _task(f"T{i}", f) for i, f in enumerate(targets, start=1)
    ]}

    seen: dict[str, str] = {}

    def _read_then_edit(task, base_dir):
        # The real pipeline builds the coder prompt (and therefore reads the
        # collect block) BEFORE the task writes anything — mirror that order.
        seen[task["id"]] = ctrl._get_collect_bridge("code").context_for(
            task["target_files"][0])
        new_symbol = f"helper_from_{task['id']}"
        (base_dir / task["target_files"][0]).write_text(
            f"def original():\n    return 0\n\n\ndef {new_symbol}():\n    return 1\n"
        )
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer = MagicMock()
    fake_outer.run_task.side_effect = _read_then_edit

    _run_loop(ctrl, fake_outer)

    assert "helper_from_T1" in seen["T3"], (
        "task 3 must see task 1's post-edit facts; it saw: %r" % seen["T3"])
    assert "helper_from_T3" not in seen["T3"]
    # Task 1 was served the pre-edit facts, as it must be — its own edit had
    # not happened yet.
    assert "helper_from_T1" not in seen["T1"]
    # Task 3's own commit must have been visible to task 5's read.
    assert "helper_from_T3" in seen["T5"]
    # A task that never touches pkg/a.py keeps reading real blocks the whole
    # time, and the build-once contract survives the repairs.
    assert "COLLECT MODEL" in seen["T2"]
    assert "COLLECT MODEL" in seen["T4"]
    assert len(counts) == 1, f"load() called {len(counts)} times for 5 tasks"


# ── 7. git_manager: the path list the controller invalidates from ──────────


def test_paths_changed_in_lists_only_the_paths_in_that_commit(mini_repo):
    gm = GitManager(mini_repo)
    gm.configure_identity()
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n")
    sha = gm.commit("auto(T1): edit a")
    assert sha
    assert gm.paths_changed_in(sha) == ["pkg/a.py"]
    assert gm.paths_changed_in("") == []
    assert gm.paths_changed_in("does-not-exist") == []


def test_paths_changed_between_covers_the_range(mini_repo):
    gm = GitManager(mini_repo)
    gm.configure_identity()
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n")
    base = gm.commit("auto(T1): edit a")
    (mini_repo / "pkg" / "b.py").write_text("def b():\n    return 99\n")
    tip = gm.commit("auto(BUG-FIX-T1): edit b")

    assert gm.paths_changed_between(base, tip) == ["pkg/b.py"]
    assert gm.paths_changed_between(base, base) == []
    assert gm.paths_changed_between("", tip) == []
    assert gm.paths_changed_between(base, "") == []


def test_paths_changed_between_covers_regressions_after_a_task_commit(mini_repo):
    """The controller invalidates after the post-commit regression loop, so a
    bug-fix commit on top of the task's own commit must be dirty too."""
    gm = GitManager(mini_repo)
    gm.configure_identity()
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n")
    base = gm.commit("auto(T1): edit a")
    (mini_repo / "pkg" / "c.py").write_text("def c():\n    return 99\n")
    tip = gm.commit("auto(BUG-FIX-T1): edit c")

    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    other = _module("pkg/c.py", [_symbol("pkg/c.py:c")])
    bridge = CollectBridge(_fresh_model([module, other]))

    paths = list(gm.paths_changed_in(base))
    if tip != base:
        paths = sorted(set(paths) | set(gm.paths_changed_between(base, tip)))
    bridge.invalidate(paths)
    assert bridge.dirty_paths == frozenset({"pkg/a.py", "pkg/c.py"})
