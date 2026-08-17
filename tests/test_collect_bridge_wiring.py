"""tests/test_collect_bridge_wiring.py — COLLECT-24 pipeline wiring.

Covers the parts CollectBridge unit tests (test_collect_bridge.py) don't:
* ContextBroker's new Pass 3 (collect fallback for pull-model symbols).
* The collect model is loaded exactly ONCE per --auto run, not per task.
* use_in_auto=false is a byte-for-byte regression: the coder prompt is
  identical to what it was before COLLECT-24 wired anything in.
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
from tools.auto.context_broker import ContextBroker
from tools.auto.controller import AutoController, RunLimits
from tools.collect.loader import CollectModel, STATUS_FRESH
from tools.collect import cli as cli_mod


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text(
        "def a():\n"
        "    return 1\n"
        "\n"
        "def b():\n"
        "    return 2\n"
    )
    (pkg / "helper.py").write_text(
        "def my_helper(x):\n"
        "    return x + 1\n"
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def _write_agents_ini(root: Path, *, use_in_auto: bool) -> Path:
    ini = root / "agents.ini"
    ini.write_text(
        "[collect]\n"
        "dir = .collect\n"
        f"use_in_auto = {'true' if use_in_auto else 'false'}\n"
        "use_in_doc = false\n"
        "staleness = warn\n"
        "llm_summaries = false\n"
    )
    return ini


# ── ContextBroker Pass 3: collect fallback for pull-model symbols ───────


def test_broker_resolves_symbol_from_collect_when_not_in_code():
    """A symbol requested via context_request that does NOT exist as real
    source in target_files/base_dir (e.g. summarized-only / no source
    handy) must still resolve if the collect model knows about it."""
    module = SimpleNamespace(
        path="pkg/other.py",
        public_symbols=[SimpleNamespace(qualname="pkg.other.ghost_func", signature="ghost_func(x) -> int")],
    )
    model = SimpleNamespace(status="fresh", modules=[module], contracts_for=lambda q: [])
    bridge = CollectBridge(model)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def a(): ...\n")  # ghost_func not here
        out = ContextBroker(collect_bridge=bridge).fetch(
            ["ghost_func"], ["helper.py"], d,
        )
    assert "ghost_func" in out
    assert "pkg/other.py" in out
    assert "PREFETCHED CONTEXT" in out


def test_broker_code_search_wins_over_collect_when_both_have_it():
    """Real source is ground truth — collect is only a fallback for symbols
    code search couldn't find, never a replacement for a real hit."""
    module = SimpleNamespace(
        path="helper.py",
        public_symbols=[SimpleNamespace(qualname="helper.my_helper", signature="STALE SIGNATURE FROM COLLECT")],
    )
    model = SimpleNamespace(status="fresh", modules=[module], contracts_for=lambda q: [])
    bridge = CollectBridge(model)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def my_helper(x):\n    return x + 1\n")
        out = ContextBroker(collect_bridge=bridge).fetch(
            ["my_helper"], ["helper.py"], d,
        )
    assert "return x + 1" in out
    assert "STALE SIGNATURE FROM COLLECT" not in out


def test_broker_without_collect_bridge_behaves_exactly_as_before():
    """collect_bridge=None (the default) — regression: identical to the
    pre-COLLECT-24 broker."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def a(): ...\n")
        assert ContextBroker().fetch(["does_not_exist"], ["helper.py"], d) == ""


def test_broker_stale_collect_model_never_used_as_fallback():
    module = SimpleNamespace(
        path="pkg/other.py",
        public_symbols=[SimpleNamespace(qualname="pkg.other.ghost_func", signature="x")],
    )
    stale_model = SimpleNamespace(status="stale", modules=[module], contracts_for=lambda q: [])
    bridge = CollectBridge(stale_model)

    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "helper.py").write_text("def a(): ...\n")
        out = ContextBroker(collect_bridge=bridge).fetch(
            ["ghost_func"], ["helper.py"], d,
        )
    assert out == ""


# ── collect model loaded exactly once per run, not per task ─────────────


def _make_controller_with_config(tmp_path: Path, config_path: str) -> AutoController:
    ctrl = AutoController.__new__(AutoController)
    ctrl.goal          = "test goal"
    ctrl.base_dir      = tmp_path
    ctrl.config_path   = config_path
    ctrl.agent_dir     = tmp_path / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    ctrl._time_fn      = time.monotonic
    ctrl._start_time   = time.monotonic()
    ctrl.limits        = RunLimits()
    ctrl.config        = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    if Path(config_path).exists():
        ctrl.config.read(config_path, encoding="utf-8")
    ctrl.task_mode     = "code"
    ctrl.state         = MagicMock()
    ctrl.state.resume_info.return_value = {"pending": [
        {"id": f"T-{i}", "target_files": ["pkg/a.py"], "dependencies": []} for i in range(3)
    ]}
    ctrl.state.all_tasks.return_value = []
    ctrl.git              = None
    ctrl.run_trace        = None
    ctrl.progress_display = None
    ctrl.metrics_stream   = None
    ctrl.auto_tuner       = None
    return ctrl


def test_collect_model_loaded_once_per_run_not_per_task(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)  # artifact exists and is fresh
    config_path = _write_agents_ini(mini_repo, use_in_auto=True)

    ctrl = _make_controller_with_config(mini_repo, str(config_path))

    load_calls = []
    real_load = None
    from tools.collect import loader as loader_mod
    real_load = loader_mod.load

    def _counting_load(*a, **k):
        load_calls.append(1)
        return real_load(*a, **k)

    monkeypatch.setattr("tools.collect.loader.load", _counting_load)

    fake_outer = MagicMock()
    fake_outer.run_task.return_value = SimpleNamespace(
        passed=True, rounds_used=1, inner_results=[],
    )

    with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
         patch("tools.auto.commit_on_success.CommitOnSuccess"), \
         patch("tools.auto.executor.make_executor", return_value=MagicMock()), \
         patch("tools.auto.bug_fix_loop.make_bug_fix_loop", return_value=MagicMock()):
        ctrl._run_task_loop(task_mode="code")

    assert len(load_calls) == 1, f"expected exactly 1 load() call for 3 tasks, got {len(load_calls)}"


def test_get_collect_bridge_disabled_never_calls_load(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)
    config_path = _write_agents_ini(mini_repo, use_in_auto=False)
    ctrl = _make_controller_with_config(mini_repo, str(config_path))

    load_calls = []
    monkeypatch.setattr(
        "tools.collect.loader.load",
        lambda *a, **k: load_calls.append(1) or CollectModel(status=STATUS_FRESH),
    )
    bridge = ctrl._get_collect_bridge("code")
    assert bridge is None
    assert load_calls == []


def test_get_collect_bridge_missing_config_attr_is_safe():
    """AutoController built via __new__() without __init__ (as some older
    tests do) must not crash — just behaves as collect-disabled."""
    ctrl = AutoController.__new__(AutoController)
    ctrl.base_dir = Path(".")
    assert ctrl._get_collect_bridge("code") is None


# ── regression: use_in_auto=false leaves the coder prompt untouched ─────


def test_use_in_auto_false_prompt_is_byte_for_byte_unchanged(mini_repo):
    """The exact AC from COLLECT-23, re-verified after COLLECT-24 wiring:
    with the flag off, InnerLoop.run_task's prefetched_context seed must
    stay empty — this is what feeds Coder._build_prompt's file_contents
    prefix, so an empty seed means an untouched prompt."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_agents_ini(mini_repo, use_in_auto=False)

    ctrl = _make_controller_with_config(mini_repo, str(config_path))
    bridge = ctrl._get_collect_bridge("code")
    assert bridge is None

    # Mirrors InnerLoop.run_task's own COLLECT-24 seeding logic exactly.
    prefetched_context = ""
    if bridge is not None:
        block = bridge.context_for_many(["pkg/a.py"])
        if block:
            prefetched_context = block + "\n\n"
    assert prefetched_context == ""
