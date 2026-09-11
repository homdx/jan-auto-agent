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
   7. Follow-up: a task that staged nothing does not blind its declared
      `target_files` (git is available and said the tree is unchanged), a
      repair that failed once is re-attempted when a later commit dirties
      the same path again, and the run reports how many blocks it withheld.
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


def test_invalidate_is_idempotent_and_dedupes():
    """`invalidate` is called once per task commit, and the controller can
    feed it the same path from both a commit's `diff-tree` and a later
    `target_files` fallback. A path dirtied twice is still just one dirty
    path — and marking it twice must not, by itself, change anything."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]))
    bridge.invalidate(["pkg/a.py", "./pkg/a.py", Path("pkg/a.py")])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    bridge.invalidate(["pkg/a.py"])
    bridge.invalidate(["pkg/a.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for("pkg/a.py") == ""


def test_bridge_built_without_init_reads_dirty_as_empty():
    """Some callers build a bridge via `__new__()` and never run `__init__`.
    A missing dirt-set must read as "nothing was written" — the pre-V9
    behaviour — and a later `invalidate` must still be able to create it."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge.__new__(CollectBridge)
    bridge._model = _fresh_model([module])
    bridge._summarizer_call = None
    bridge._max_context_chars = 1200
    bridge._task_mode = "code"

    assert bridge.dirty_paths == frozenset()
    assert bridge.has_dirty is False
    assert bridge.pull_symbol("a") != ""

    bridge.invalidate(["pkg/a.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.pull_symbol("a") == ""


def test_context_for_many_blinds_the_dirty_file_and_keeps_the_clean_ones(mini_repo):
    """`context_for_many` is the multi-target entry point: a dirty file in the
    list must not drag the clean ones down with it, and the withheld block
    must be counted once, not once per file in the batch."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    bridge = make_collect_bridge(mini_repo, _cfg(config_path.read_text(encoding="utf-8")),
                                 str(config_path), task_mode="code")

    clean_b = bridge.context_for("pkg/b.py")
    clean_c = bridge.context_for("pkg/c.py")
    assert clean_b and clean_c
    assert bridge.collect_misses == {}

    bridge.invalidate(["pkg/a.py"])
    block = bridge.context_for_many(["pkg/a.py", "pkg/b.py", "pkg/c.py"])

    assert block.count("COLLECT MODEL") == 2
    assert clean_b in block
    assert clean_c in block
    assert "pkg/a.py" in bridge.dirty_paths
    assert bridge.collect_misses == {"dirty": 1}


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


def test_auto_refresh_retries_after_a_later_commit_redirties_the_path():
    """The "once per path" cap is per *edit*, not per run: a repair that
    failed is not retried on every later lookup, but a later commit that
    dirties the same path again is a new attempt. Without it a transient
    provider outage at task 1 keeps blinding a file that task 3 edited
    again for the whole rest of the run — one LLM call per edit is the
    cost the flag promises, and it stays bounded by the commits."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:a", "a() -> STALE")])
    fresh = _module("pkg/a.py", [_symbol("pkg/a.py:a", "a(x) -> FRESH")])
    bridge = CollectBridge(_fresh_model([stale]), auto_refresh=True)
    attempts = []

    def _flaky(path: str):
        attempts.append(path)
        if len(attempts) == 1:
            raise RuntimeError("provider down")
        return fresh

    bridge._module_refresh_fn = _flaky
    bridge.invalidate(["pkg/a.py"])

    assert bridge.context_for("pkg/a.py") == ""      # first attempt fails
    for _ in range(3):
        assert bridge.context_for("pkg/a.py") == ""  # not retried
    assert attempts == ["pkg/a.py"]

    # Task 3 commits an edit to the same file: a new attempt is warranted.
    bridge.invalidate(["pkg/a.py"])
    assert "a(x) -> FRESH" in bridge.pull_symbol("a")
    assert "pkg/a.py" not in bridge.dirty_paths
    assert attempts == ["pkg/a.py", "pkg/a.py"]


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


def test_controller_nothing_staged_blinds_nothing(mini_repo, monkeypatch):
    """Git is configured but the task staged nothing: the tree is unchanged,
    so every fact in the artifact is still exactly right. `target_files` is
    a fallback for a tree we cannot ask, not for one that says it did not
    change — falling back here would suppress a block that predates no edit
    at all, and would break "an unedited path keeps its block the whole
    run" on the run's own no-op tasks."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    fake_outer = MagicMock()
    fake_outer.run_task.return_value = SimpleNamespace(passed=True, rounds_used=1, inner_results=[])
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset()
    assert bridge.context_for("pkg/a.py") != ""


def test_controller_git_unusable_blinds_the_declared_files(mini_repo, monkeypatch):
    """The mirror of the test above: git refused the commit AND cannot be
    asked whether the tree changed (index.lock, a hung hook). The edit may
    really be sitting in the tree, so the declared `target_files` are still
    the safer guess — assume a write happened rather than serving stale
    facts."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    from tools.auto.git_manager import GitError

    gm.paths_changed_in = MagicMock(side_effect=GitError("index.lock held"))
    gm.has_staged_changes = MagicMock(side_effect=GitError("index.lock held"))

    fake_outer = MagicMock()

    def _edit(task, base_dir):
        (base_dir / "pkg" / "a.py").write_text("def a():\n    return 9\n")
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _edit
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for("pkg/b.py") != ""


def test_controller_reports_how_many_blocks_it_withheld(mini_repo, monkeypatch):
    """Item 4 is only half done when the tally is counted and never shown:
    a run must report how many collect blocks V9 suppressed, so an operator
    can tell "the pack was thin" from "the pack was withheld"."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.limits = RunLimits(max_tasks_per_run=2)
    logs: list[str] = []
    ctrl.state.log = logs.append
    # Two tasks on the same file: T1 commits an edit, T2 reads the block
    # for it (and so meets the dirty path), then the task cap stops the run.
    ctrl.state.resume_info.return_value = {"pending": [
        _task("T1", "pkg/a.py"),
        _task("T2", "pkg/a.py"),
        _task("T3", "pkg/b.py"),
    ]}

    fake_outer = MagicMock()

    def _read_then_edit(task, base_dir):
        ctrl._get_collect_bridge("code").context_for(task["target_files"][0])
        (base_dir / task["target_files"][0]).write_text(
            f"def changed_by_{task['id']}():\n    return 0\n"
        )
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _read_then_edit

    reason, done = _run_loop(ctrl, fake_outer)

    assert reason == "task_cap" and done == 2
    assert any("withheld 1 block(s)" in line and "dirty=1" in line for line in logs), logs


def test_controller_reports_nothing_when_nothing_was_withheld(mini_repo, monkeypatch):
    """The summary line is only for a run that actually suppressed a block —
    a clean run stays silent, like every other per-run log line here."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    ctrl = _make_controller(mini_repo, config_path, git=None)
    ctrl.limits = RunLimits(max_tasks_per_run=1)
    logs: list[str] = []
    ctrl.state.log = logs.append
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    fake_outer = MagicMock()
    fake_outer.run_task.return_value = SimpleNamespace(passed=True, rounds_used=1, inner_results=[])
    _run_loop(ctrl, fake_outer)

    # The path T1 wrote is still dirty, so the run says so — but it never
    # withheld a block, and the summary must not claim it did.
    assert not any("withheld" in line for line in logs), logs
    assert any("still unrefreshed" in line and "pkg/a.py" in line for line in logs), logs


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


# ── 8. follow-up: what the first landing did not cover ──────────────────────


def test_invalidate_ignores_a_parent_relative_path():
    """`../pkg/a.py` is above the tree the model was built from; git never
    reports such a path and the model holds nothing there. Before the fix it
    survived normalisation verbatim (it ends in `.py`), sat in `dirty_paths`
    as noise, and matched nothing. Now it is dropped like an absolute path
    outside `base_dir` is."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]))
    bridge.invalidate(["../pkg/a.py", "../../x.py", ".."])
    assert bridge.dirty_paths == frozenset()
    assert bridge.context_for("pkg/a.py") != ""
    assert bridge.collect_misses == {}


def test_invalidate_keeps_a_non_py_parent_relative_path_out():
    """A parent-relative doc or config path is dropped like any other."""
    module = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module]))

    bridge.invalidate(["../README.md", "../", "", "./../"])
    assert bridge.dirty_paths == frozenset()
    assert bridge.context_for("pkg/a.py") != ""


def test_context_for_many_blinds_only_the_dirty_files():
    """`context_for_many` joins one block per file, so a single dirty entry
    must drop only its own block rather than empty the whole join — the
    "one edited file must not blind the pack" contract, at the joiner."""
    bridge = CollectBridge(_fresh_model([
        _module("pkg/a.py", [_symbol("pkg/a.py:a")]),
        _module("pkg/b.py", [_symbol("pkg/b.py:b")]),
    ]))
    bridge.invalidate(["pkg/a.py"])

    block = bridge.context_for_many(["pkg/a.py", "pkg/b.py"])
    assert "public_symbols: pkg/a.py:a" not in block
    assert "public_symbols: pkg/b.py:b" in block
    assert bridge.collect_misses == {"dirty": 1}


def test_pull_symbol_counts_no_miss_when_a_clean_duplicate_answers():
    """A dirty module that loses to a clean duplicate is not a miss: the caller
    got real facts. Pinning this so the dirty-hit flag can never be promoted
    from "any dirty module matched" to "a miss was counted"."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:shared", "shared(x) -> STALE")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:shared", "shared(x) -> FRESH")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py"])

    assert "FRESH" in bridge.pull_symbol("shared")
    assert bridge.collect_misses == {}


def test_summary_reports_the_withheld_blocks_and_the_still_dirty_paths():
    """V9 item 4 asked for `collect_miss(reason="dirty")` so a run can report
    how many blocks the rule suppressed. The tally was already counted but
    nothing read it, so an operator could not tell "withheld for two files"
    from "collect was never used". This is the reader."""
    bridge = CollectBridge(_fresh_model([
        _module("pkg/a.py", [_symbol("pkg/a.py:a")]),
        _module("pkg/b.py", [_symbol("pkg/b.py:b")]),
    ]))
    assert bridge.summary() == ""       # nothing withheld, nothing dirty

    bridge.invalidate(["pkg/a.py", "pkg/b.py"])
    bridge.context_for("pkg/a.py")
    bridge.pull_symbol("a")
    bridge.module_symbols("pkg/b.py")

    line = bridge.summary()
    assert "withheld 3 block(s)" in line
    assert "dirty=3" in line
    assert "2 path(s) still unrefreshed" in line
    assert "pkg/a.py" in line and "pkg/b.py" in line


def test_summary_is_empty_when_a_repair_removed_all_the_dirt():
    """A run that withheld nothing and repaired everything reports nothing, so
    run.log stays silent about a run collect had no say in."""
    bridge = CollectBridge(_fresh_model([
        _module("pkg/a.py", [_symbol("pkg/a.py:a")]),
    ]))
    assert bridge.summary() == ""


def test_summary_survives_a_broken_tally():
    """The tally is counted on a plain dict, but a bridge built without
    `__init__` has no attribute at all — the summary reads as empty rather
    than raising into a run."""
    bridge = CollectBridge.__new__(CollectBridge)
    bridge._model = None
    assert bridge.summary() == ""


def test_repair_is_rearmed_by_a_new_invalidate():
    """Before the fix, a path whose repair failed was remembered in
    `_repair_failed` for the whole run: a later task that rewrote the same
    file and committed could never get post-edit facts, even if the provider
    was back. A new `invalidate()` is a new attempt — while repeated lookups
    of the SAME dirty state still pay at most one call each."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:a", "a() -> STALE")])
    fresh = _module("pkg/a.py", [_symbol("pkg/a.py:a", "a(x) -> FRESH")])
    bridge = CollectBridge(_fresh_model([stale]), auto_refresh=True)

    attempts = []

    def _refresh(path: str):
        attempts.append(path)
        if len(attempts) == 1:
            raise RuntimeError("provider down")
        return fresh

    bridge._module_refresh_fn = _refresh

    bridge.invalidate(["pkg/a.py"])          # commit 1
    for _ in range(3):                       # repeated lookups: one attempt
        assert bridge.context_for("pkg/a.py") == ""
    assert attempts == ["pkg/a.py"]
    assert bridge.collect_misses == {"dirty": 3}

    bridge.invalidate(["pkg/a.py"])          # commit 2 re-arms it
    assert bridge.context_for("pkg/a.py") != ""   # the blind is gone
    assert "a(x) -> FRESH" in bridge.pull_symbol("a")
    assert "STALE" not in bridge.pull_symbol("a")
    assert attempts == ["pkg/a.py", "pkg/a.py"]
    assert "pkg/a.py" not in bridge.dirty_paths


def test_controller_git_empty_answer_invalidates_nothing(mini_repo, monkeypatch):
    """git answering `[]` is not "no git". Before the fix the `if not paths`
    fallback also fired on an empty answer, so a merge commit or an
    `--allow-empty` commit blinded the task's declared `target_files` — files
    the task never touched."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    gm.paths_changed_in = MagicMock(return_value=[])

    fake_outer = MagicMock()

    def _write_doc(task, base_dir):
        (base_dir / "README.md").write_text("# docs only\n")
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _write_doc
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset()
    assert bridge.context_for("pkg/a.py") != ""


def test_controller_falls_back_to_target_files_only_when_git_fails(mini_repo, monkeypatch):
    """The contrast of the test above: git raising (index.lock, a hung hook)
    still falls back to the declared `target_files` — over-blinding a path
    that turned out clean costs a block, serving stale facts costs a wrong
    one."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "pkg/a.py")]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    from tools.auto.git_manager import GitError

    gm.paths_changed_in = MagicMock(side_effect=GitError("index.lock"))

    fake_outer = MagicMock()

    def _edit(task, base_dir):
        (base_dir / "pkg" / "a.py").write_text("def a():\n    return 9\n")
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _edit
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})


def test_controller_logs_the_collect_summary_at_run_end(mini_repo, monkeypatch):
    """V9 item 4, wired: one run.log line for the whole run, on the normal
    exit path."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [
        _task("T1", "pkg/a.py"), _task("T2", "pkg/a.py"),
    ]}
    ctrl.state.get_task.side_effect = lambda task_id: _task(task_id, "pkg/a.py")

    fake_outer = MagicMock()

    def _read_then_edit(task, base_dir):
        withheld = ctrl._get_collect_bridge("code").context_for(task["target_files"][0])
        if withheld:   # T1 still gets a block; T2 gets "" and so counts a miss
            (base_dir / "pkg" / "a.py").write_text("def a():\n    return 7\n")
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _read_then_edit
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.collect_misses == {"dirty": 1}

    lines = [str(c) for c in ctrl.state.log.call_args_list]
    summary = [l for l in lines if "collect summary (code):" in l]
    assert len(summary) == 1
    assert "withheld 1 block(s)" in summary[0]
    assert "dirty=1" in summary[0]
    assert "pkg/a.py" in summary[0]


def test_controller_logs_no_collect_summary_when_collect_had_no_say(mini_repo, monkeypatch):
    """Nothing withheld and nothing dirty means no line — a run with no source
    edits must not add collect chatter to run.log."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [_task("T1", "README.md")]}

    fake_outer = MagicMock()

    def _write_doc(task, base_dir):
        (base_dir / "README.md").write_text("# docs only\n")
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    fake_outer.run_task.side_effect = _write_doc
    _run_loop(ctrl, fake_outer)

    bridge = ctrl._get_collect_bridge("code")
    assert bridge.dirty_paths == frozenset()
    assert bridge.summary() == ""
    assert not [str(c) for c in ctrl.state.log.call_args_list
                if "collect summary" in str(c)]


def test_controller_rearm_gives_a_later_task_post_edit_facts(mini_repo, monkeypatch):
    """End to end: task 1's edit cannot be repaired (provider down), task 3
    rewrites the same file and commits, and task 4 — not task 3 — is the one
    that reads post-edit facts. Without the re-arm, task 4 would have read
    nothing for the rest of the run."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")

    gm = GitManager(mini_repo)
    gm.configure_identity()
    ctrl = _make_controller(mini_repo, config_path, git=gm)
    ctrl.state.resume_info.return_value = {"pending": [
        _task(f"T{i}", "pkg/a.py") for i in (1, 2, 3, 4)
    ]}

    # Build the bridge before the loop so the loop reuses this same instance.
    bridge = ctrl._get_collect_bridge("code")

    attempts = []

    def _flaky_refresh(path):
        attempts.append(path)
        if len(attempts) == 1:
            raise RuntimeError("provider down")
        return ModuleRecord(
            path=path,
            public_symbols=(FunctionRecord(
                qualname=f"{path}:helper_from_T3", module=path, lineno=1,
                signature="helper_from_T3()",
            ),),
        )

    bridge._module_refresh_fn = _flaky_refresh

    fake_outer = MagicMock()

    def _read_then_edit(task, base_dir):
        seen[task["id"]] = (
            bridge.context_for(task["target_files"][0]),
            bridge.pull_symbol("helper_from_T3"),
        )
        if task["id"] in ("T1", "T3"):
            (base_dir / "pkg" / "a.py").write_text(
                f"def a():\n    return 1\n\n\ndef helper_from_{task['id']}():\n    return 2\n"
            )
        return SimpleNamespace(passed=True, rounds_used=1, inner_results=[])

    seen: dict = {}
    fake_outer.run_task.side_effect = _read_then_edit
    _run_loop(ctrl, fake_outer)

    assert seen["T2"][0] == ""                      # repair failed, so a blind
    assert "helper_from_T3" in seen["T4"][0]        # the re-arm made it fresh
    assert seen["T4"][1] != ""
    assert len(attempts) == 2
    assert "pkg/a.py" not in bridge.dirty_paths


# ── 8. V9 bugfix: pull_symbol / contracts_for_symbol with auto_refresh ───


def test_pull_symbol_finds_newly_added_symbol_with_auto_refresh():
    """V9 bugfix: the OLD module has no match for the newly added symbol, so
    the pre-edit `any(...)` check fails and `_is_dirty` is never called —
    the repair is never attempted. The fix defers the dirty path and repairs
    it once nothing clean answered, so the post-edit record is reachable."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:old_func", "old_func() -> int")])
    fresh = _module("pkg/a.py", [
        _symbol("pkg/a.py:old_func", "old_func() -> int"),
        _symbol("pkg/a.py:new_func", "new_func(x: str) -> str"),
    ])
    bridge = CollectBridge(_fresh_model([stale]), auto_refresh=True)
    bridge._module_refresh_fn = lambda path: fresh
    bridge.invalidate(["pkg/a.py"])

    assert bridge.pull_symbol("new_func") != ""
    assert "new_func(x: str) -> str" in bridge.pull_symbol("new_func")
    assert "pkg/a.py" not in bridge.dirty_paths
    assert bridge.collect_misses == {}


def test_contracts_for_symbol_finds_newly_added_contract_with_auto_refresh():
    """Same bugfix as above, for contracts_for_symbol: a contract attached to
    a symbol the OLD record does not know about must be reachable after repair."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:old_func")])
    fresh = _module("pkg/a.py", [
        _symbol("pkg/a.py:old_func"),
        _symbol("pkg/a.py:new_func"),
    ])
    contract_new = ContractRecord(name="never_raises_new", known_edge="pkg/a.py:new_func",
                                  description="never raises")
    bridge = CollectBridge(_fresh_model([stale], contracts=[contract_new]), auto_refresh=True)
    bridge._module_refresh_fn = lambda path: fresh
    bridge.invalidate(["pkg/a.py"])

    contracts = bridge.contracts_for_symbol("new_func")
    assert len(contracts) == 1
    assert contracts[0].name == "never_raises_new"
    assert "pkg/a.py" not in bridge.dirty_paths


def test_pull_symbol_miss_only_when_symbol_was_in_dirty_module():
    """`collect_miss(reason='dirty')` must count only when the symbol was
    actually in the OLD record (and therefore withheld). A dirty module that
    never had the symbol is not a miss — it is simply not the right module.
    Before the fix, the `any(...)` gate also prevented `_miss` from being
    called for the newly-added-symbol case, which was correct but for the
    wrong reason."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py"])

    # Symbol IS in the dirty module — this is a miss.
    bridge.pull_symbol("a")
    # Symbol is NOT in the dirty module — not a miss.
    bridge.pull_symbol("b")
    # Symbol is in no module at all — not a miss.
    bridge.pull_symbol("zzz")
    assert bridge.collect_misses == {"dirty": 1}


def test_pull_symbol_auto_refresh_failure_no_miss_when_not_in_old():
    """With auto_refresh on, a repair that fails for a module that never had
    the symbol must not count as a collect_miss — the symbol was not withheld,
    it simply was not there."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module_a]), auto_refresh=True)

    def _broken(path: str):
        raise RuntimeError("provider down")

    bridge._module_refresh_fn = _broken
    bridge.invalidate(["pkg/a.py"])

    # 'a' was in the old record — miss.
    bridge.pull_symbol("a")
    # 'zzz' was never in any record — not a miss.
    bridge.pull_symbol("zzz")
    assert bridge.collect_misses == {"dirty": 1}


# ── 9. tests_covering is NOT blinded by invalidate ─────────────────────────


def test_tests_covering_is_not_blinded_by_invalidate():
    """`tests_covering` records which test files import which modules — an edit
    to a source module does not change that mapping. The docstring says it is
    'Deliberately NOT blinded by invalidate()'. Pin that decision: invalidating
    a source module must not suppress its test-coverage entry."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    model = CollectModel(
        status=STATUS_FRESH,
        modules=(module_a,),
        test_map={"pkg/a.py": ("tests/test_a.py", "tests/test_a_extra.py")},
    )
    bridge = CollectBridge(model)
    assert bridge.tests_covering("pkg/a.py") == ("tests/test_a.py", "tests/test_a_extra.py")

    bridge.invalidate(["pkg/a.py"])
    assert bridge.tests_covering("pkg/a.py") == ("tests/test_a.py", "tests/test_a_extra.py")


# ── 10. context_for_many skips dirty paths ─────────────────────────────────


def test_context_for_many_skips_dirty_paths():
    """`context_for_many` joins blocks for several files. A dirty path must
    contribute nothing (not even a partial or truncated block), while clean
    paths keep their full block."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]), max_context_chars=5000)
    bridge.invalidate(["pkg/a.py"])

    joined = bridge.context_for_many(["pkg/a.py", "pkg/b.py"])
    assert "pkg/a.py" not in joined
    assert "pkg/b.py" in joined
    assert joined.count("COLLECT MODEL") == 1


def test_context_for_many_all_dirty_returns_empty():
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg/a.py", "pkg/b.py"])
    assert bridge.context_for_many(["pkg/a.py", "pkg/b.py"]) == ""


# ── 11. invalidate accumulates across calls ────────────────────────────────


def test_invalidate_accumulates_across_calls():
    """Multiple `invalidate` calls must accumulate dirty paths, not replace
    them. Each task commit adds its own paths."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("pkg/b.py", [_symbol("pkg/b.py:b")])
    module_c = _module("pkg/c.py", [_symbol("pkg/c.py:c")])
    bridge = CollectBridge(_fresh_model([module_a, module_b, module_c]))

    bridge.invalidate(["pkg/a.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for("pkg/b.py") != ""

    bridge.invalidate(["pkg/b.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py", "pkg/b.py"})
    assert bridge.context_for("pkg/c.py") != ""

    bridge.invalidate(["pkg/c.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py", "pkg/b.py", "pkg/c.py"})


def test_invalidate_idempotent():
    """Calling `invalidate` twice with the same path must not change the dirty
    set — `frozenset` semantics."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module_a]))
    bridge.invalidate(["pkg/a.py"])
    bridge.invalidate(["pkg/a.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})


# ── 12. _normalize_path edge cases ─────────────────────────────────────────


def test_normalize_path_handles_backslashes():
    """Windows-style paths (backslash separators) must normalise to the
    forward-slash key the model uses."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    module_b = _module("sub/pkg/b.py", [_symbol("sub/pkg/b.py:b")])
    bridge = CollectBridge(_fresh_model([module_a, module_b]))
    bridge.invalidate(["pkg\\a.py", "sub\\pkg\\b.py"])
    assert bridge.dirty_paths == frozenset({"pkg/a.py", "sub/pkg/b.py"})
    assert bridge.context_for("pkg/a.py") == ""


def test_normalize_path_handles_path_objects():
    """`invalidate` accepts `pathlib.Path` objects (a `GitManager` might
    return them). They must normalise to the same key as strings."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module_a]))
    bridge.invalidate([Path("pkg/a.py")])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})
    assert bridge.context_for("pkg/a.py") == ""


def test_normalize_path_strips_quotes_and_backticks():
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module_a]))
    for spelling in ('"pkg/a.py"', "'pkg/a.py'", "`pkg/a.py`"):
        bridge.invalidate([spelling])
    assert bridge.dirty_paths == frozenset({"pkg/a.py"})


# ── 13. pull_symbol: repair removes the symbol — not a dirty miss ────────


def test_pull_symbol_repair_removes_symbol_no_miss():
    """If the repair succeeds but the NEW module no longer has the symbol
    (the task removed it), no collect_miss is counted — the symbol genuinely
    doesn't exist anymore, it wasn't withheld due to dirtiness."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:removed")])
    fresh = _module("pkg/a.py", [_symbol("pkg/a.py:kept")])
    bridge = CollectBridge(_fresh_model([stale]), auto_refresh=True)
    bridge._module_refresh_fn = lambda path: fresh
    bridge.invalidate(["pkg/a.py"])

    assert bridge.pull_symbol("removed") == ""
    assert bridge.collect_misses == {}


# ── 14. contracts_for_symbol: auto_refresh failure, not-in-old ───────────


def test_contracts_for_symbol_auto_refresh_failure_no_miss_when_not_in_old():
    """With auto_refresh on, a repair that fails for a module that never had
    the symbol must not count as a collect_miss."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module_a]), auto_refresh=True)

    def _broken(path: str):
        raise RuntimeError("provider down")

    bridge._module_refresh_fn = _broken
    bridge.invalidate(["pkg/a.py"])

    bridge.contracts_for_symbol("a")
    bridge.contracts_for_symbol("zzz")
    assert bridge.collect_misses == {"dirty": 1}


# ── 15. module_symbols with auto_refresh ─────────────────────────────────


def test_module_symbols_with_auto_refresh_returns_post_edit_facts():
    """With auto_refresh on, `module_symbols` returns the post-edit
    inventory after repairing the dirty module."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:old_func")])
    fresh = _module("pkg/a.py", [
        _symbol("pkg/a.py:old_func"),
        _symbol("pkg/a.py:new_func"),
    ])
    bridge = CollectBridge(_fresh_model([stale]), auto_refresh=True)
    bridge._module_refresh_fn = lambda path: fresh
    bridge.invalidate(["pkg/a.py"])

    block = bridge.module_symbols("pkg/a.py")
    assert "new_func" in block
    assert "old_func" in block
    assert "pkg/a.py" not in bridge.dirty_paths


def test_module_symbols_with_auto_refresh_failure_returns_empty():
    """With auto_refresh on, a failed repair returns `""` for the dirty
    module and counts a collect_miss."""
    module_a = _module("pkg/a.py", [_symbol("pkg/a.py:a")])
    bridge = CollectBridge(_fresh_model([module_a]), auto_refresh=True)

    def _broken(path: str):
        raise RuntimeError("provider down")

    bridge._module_refresh_fn = _broken
    bridge.invalidate(["pkg/a.py"])

    assert bridge.module_symbols("pkg/a.py") == ""
    assert "pkg/a.py" in bridge.dirty_paths
    assert "dirty" in bridge.collect_misses


def test_pull_symbol_does_not_repair_a_dirty_path_a_clean_module_answers_for():
    """The added-symbol repair is paid only on a miss. A pull for a name a
    clean module knows must not spend an `action_module` call on every
    dirty path first — that would be one LLM call per dirty path per pull,
    for a fact the clean record already has."""
    stale = _module("pkg/a.py", [_symbol("pkg/a.py:old_func")])
    clean = _module("pkg/b.py", [_symbol("pkg/b.py:helper", "helper() -> None")])
    calls: list[str] = []

    def _refresh(path: str):
        calls.append(path)
        return stale

    bridge = CollectBridge(_fresh_model([stale, clean]), auto_refresh=True)
    bridge._module_refresh_fn = _refresh
    bridge.invalidate(["pkg/a.py"])

    assert "helper() -> None" in bridge.pull_symbol("helper")
    assert calls == []                       # clean record answered, no repair
    assert bridge.contracts_for_symbol("helper") == []   # known, no contracts
    assert calls == []
    assert bridge.pull_symbol("nowhere") == ""
    assert calls == ["pkg/a.py"]             # a miss pays exactly once
    assert bridge.collect_misses == {}       # the old record never claimed it
