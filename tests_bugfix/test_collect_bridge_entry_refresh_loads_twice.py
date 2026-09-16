"""tests_bugfix/test_collect_bridge_entry_refresh_loads_twice.py — RUN-6
follow-up: a doc/coverage gap, no behavior change.

`make_collect_bridge`'s docstring called itself "the ONLY place
`tools.collect.loader.load()` is called per run", and `CollectBridge`'s
module docstring said "`load()` is never called a second time". Both
claims are too broad. RUN-6's stale-artifact entry refresh deliberately
calls `load()` a second time — `_refresh_on_entry` runs the incremental
`action_refresh` over the paths that moved and then reloads the model it
just repaired, so the bridge is built over a fresh artifact. That second
call is the mechanism that puts the pack back on for a resumed session,
not a defect; the original AUTO-METRIC invariant (never `load()` per
task) is untouched.

`test_collect_model_loaded_once_per_run_not_per_task` only exercises the
fresh-artifact path, so the stale+auto_refresh path's second call was
pinned in neither direction. This file pins the call count on all three
build paths:

  1. fresh artifact                  → exactly 1 `load()`.
  2. stale + auto_refresh = true     → exactly 2 `load()` (the initial
                                       load of the stale model + the
                                       reload after the entry repair).
  3. stale + auto_refresh = false    → exactly 1 `load()`, bridge
                                       unusable.

plus 4: exercising the bridge afterwards (the thing tasks do) adds no
further `load()` on either path — that is the invariant the two narrowed
docstrings now state precisely.
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

import pytest

from tools.auto.collect_bridge import make_collect_bridge
from tools.collect import cli as cli_mod
from tools.collect.loader import STATUS_FRESH, STATUS_STALE


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(text)
    return cfg


def _collect_ini(**extra: str) -> str:
    keys = {
        "dir": ".collect",
        "use_in_auto": "true",
        "use_in_doc": "false",
        "staleness": "warn",
        "llm_summaries": "false",
    }
    keys.update(extra)
    return "[collect]\n" + "".join(f"{k} = {v}\n" for k, v in keys.items())


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    monkeypatch.setattr(cli_mod.registries_mod, "build_seed_contracts", lambda modules, root=None: [])
    monkeypatch.setattr(cli_mod.gates_mod, "build_gates_map", lambda modules, root: [])


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("def a():\n    return 1\n")
    (pkg / "b.py").write_text("def b():\n    return 2\n")
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


def _counting_load(monkeypatch) -> list:
    import tools.collect.loader as loader_mod

    real_load = loader_mod.load
    counts: list = []

    def _counting(*args, **kwargs):
        counts.append(1)
        return real_load(*args, **kwargs)

    monkeypatch.setattr("tools.collect.loader.load", _counting)
    return counts


def _make_stale(root: Path) -> None:
    """One tracked module edited: `git_sha` unchanged but its hash moved, so
    the artifact loads as `stale` — the shape a resumed run starts with."""
    (root / "pkg" / "a.py").write_text("def a():\n    return 99\n")


def _bridge(root: Path, config_path: Path):
    return make_collect_bridge(
        root, _cfg(config_path.read_text(encoding="utf-8")), str(config_path), task_mode="code",
    )


def test_fresh_artifact_loads_exactly_once(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    counts = _counting_load(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None and bridge.usable is True
    assert len(counts) == 1, f"expected 1 load() on the fresh path, got {len(counts)}"


def test_stale_auto_refresh_loads_twice_and_never_per_use(mini_repo, monkeypatch):
    """The second `load()` is the entry refresh reloading the model it just
    repaired — and it stays at two however many times the bridge is then
    used, which is the invariant that actually matters."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    counts = _counting_load(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None and bridge.usable is True
    assert bridge.status == STATUS_FRESH
    assert len(counts) == 2, (
        f"expected 2 load() calls on the stale+auto_refresh path "
        f"(initial load + entry-refresh reload), got {len(counts)}"
    )

    # Tasks use the already-built bridge: no load() may follow.
    assert bridge.context_for("pkg/b.py")
    assert bridge.context_for("pkg/a.py")
    assert len(counts) == 2, f"load() must never run per task, got {len(counts)}"


def test_stale_without_auto_refresh_loads_once(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)  # auto_refresh_between_tasks absent
    _make_stale(mini_repo)
    counts = _counting_load(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None
    assert bridge.usable is False
    assert bridge.status == STATUS_STALE
    assert len(counts) == 1, f"expected 1 load() without the flag, got {len(counts)}"
