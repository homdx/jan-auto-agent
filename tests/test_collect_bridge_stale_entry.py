"""RUN-6 — a stale artifact at session start is refreshed on entry when
`[collect] auto_refresh_between_tasks = true`.

End-to-end over a real mini git repo and the real collector (Pass B off via
`llm_summaries = false`, so no LLM is ever called): `--collect` once, edit a
tracked module so the artifact is stale, then build the bridge the way
`--auto` does and look at what came back.

Base: Sensenova 6-8 var2 (round-34 competition); the "refreshed facts are
the new facts" case is from Laguna-S 2-1; the changed-module count and the
`staleness = refresh` interplay are the reviewer's.
"""

from __future__ import annotations

import configparser
import logging
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


def _make_stale(mini_repo: Path) -> None:
    """One tracked module edited → `git_sha` unchanged but the hash moved:
    exactly the "previous session committed" shape, minus the commit."""
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 99\n\n\ndef a_new():\n    return 'new'\n")


def _bridge(mini_repo: Path, config_path: Path):
    return make_collect_bridge(
        mini_repo, _cfg(config_path.read_text(encoding="utf-8")), str(config_path), task_mode="code",
    )


def _count_refresh(monkeypatch):
    calls = []
    real = cli_mod.action_refresh

    def _counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr("tools.collect.cli.action_refresh", _counting)
    return calls


def _write_version1_manifest(mini_repo: Path) -> None:
    """Rewrite the manifest as if it had been built before V10: the live
    clones all carried version "1" while the code was on "2", which is what
    made every `--auto` start take the full-build fallback."""
    import json as _json

    from tools.collect import manifest as manifest_mod

    path = cli_mod.resolve_collect_dir(mini_repo, None) / cli_mod.MANIFEST_FILENAME
    payload = _json.loads(path.read_text(encoding="utf-8"))
    assert payload["collector_version"] != "1"
    payload["collector_version"] = "1"
    manifest_mod.write_manifest(manifest_mod.Manifest.from_dict(payload), path)


def _n_modules(mini_repo: Path) -> int:
    from tools.collect.scanner import scan_repo

    return len(scan_repo(mini_repo))


# ── 1. auto_refresh=true → refresh once, reload, usable ────────────────


def test_stale_auto_refresh_true_calls_refresh_and_reloads(mini_repo, monkeypatch, capsys):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    calls = _count_refresh(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None
    assert len(calls) == 1
    assert Path(calls[0][0][0]) == mini_repo
    # The operator's config and its path reach action_refresh unchanged —
    # Pass B (off here) and the collect dir are resolved from them.
    assert calls[0][1]["config"].get("collect", "use_in_auto") == "true"
    assert calls[0][1]["config_path"] == str(config_path)
    assert bridge.usable is True
    assert bridge.status == STATUS_FRESH

    out = capsys.readouterr().out
    assert "collect: artifact stale (git_sha " in out
    assert "HEAD " in out
    # Only pkg/a.py moved — the count is the Pass B bill, not the model size.
    assert "refreshing 1 module(s)" in out
    assert "pack OFF" not in out


def test_refreshed_bridge_serves_the_new_facts(mini_repo, monkeypatch):
    """The point of the refresh: the block the coder sees describes the
    tree as it is now, not as it was when `--collect` last ran."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)

    bridge = _bridge(mini_repo, config_path)
    assert bridge is not None and bridge.usable is True
    names = {f.qualname for m in bridge._model.modules if m.path == "pkg/a.py" for f in m.public_symbols}
    assert "pkg/a.py:a_new" in names


def test_second_bridge_in_the_same_run_does_not_refresh_again(mini_repo, monkeypatch):
    """Architect and controller each build a bridge; the first refresh
    leaves the artifact fresh so the second is a plain load."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    calls = _count_refresh(monkeypatch)

    assert _bridge(mini_repo, config_path).usable is True
    assert _bridge(mini_repo, config_path).usable is True
    assert len(calls) == 1


def test_entry_refresh_takes_the_bridge_pass_b_summarizer(mini_repo, monkeypatch):
    """The entry refresh is the V9 repair over every path that moved, so it
    re-summarises the changed modules with the same Pass B call a per-path
    repair uses (Hy3's point) — and none at all under `llm_summaries = false`,
    where the refresh stays structural-only."""
    cli_mod.action_collect(mini_repo)
    _make_stale(mini_repo)
    calls = _count_refresh(monkeypatch)

    def _pass_b(prompt: str, source: str) -> str:
        return "summary"

    monkeypatch.setattr("tools.collect.summarizer.make_summarizer_call", lambda config, task_mode="code": _pass_b)

    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true", llm_summaries="true")
    assert _bridge(mini_repo, config_path).usable is True
    assert calls[-1][1]["llm_call"] is _pass_b

    _make_stale(mini_repo)
    (mini_repo / "pkg" / "b.py").write_text("def b():\n    return 3\n")
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true", llm_summaries="false")
    assert _bridge(mini_repo, config_path).usable is True
    assert calls[-1][1]["llm_call"] is None


# ── 2. auto_refresh=false → today's behaviour, but said out loud ────────


@pytest.mark.parametrize("flag", [{}, {"auto_refresh_between_tasks": "false"}], ids=["absent", "false"])
def test_stale_auto_refresh_false_no_refresh(mini_repo, monkeypatch, capsys, caplog, flag):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, **flag)
    _make_stale(mini_repo)
    calls = _count_refresh(monkeypatch)

    with caplog.at_level(logging.WARNING):
        bridge = _bridge(mini_repo, config_path)

    assert bridge is not None
    assert calls == []
    assert bridge.usable is False
    assert bridge.status == STATUS_STALE

    out = capsys.readouterr().out
    assert "pack OFF for this session" in out
    assert "staleness = refresh" in out
    assert any("staleness = refresh" in r.message for r in caplog.records if r.levelno == logging.WARNING)


def test_fresh_artifact_prints_nothing_and_never_refreshes(mini_repo, monkeypatch, capsys):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    calls = _count_refresh(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge.usable is True
    assert calls == []
    assert capsys.readouterr().out == ""


def test_staleness_refresh_policy_still_refreshes_inside_load(mini_repo, monkeypatch, capsys):
    """`staleness = refresh` is handled by `loader.load` itself; the bridge
    sees a fresh model and RUN-6 has nothing to do — one refresh, not two."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, staleness="refresh", auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    calls = _count_refresh(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge.usable is True
    assert len(calls) == 1
    assert "collect: artifact stale" not in capsys.readouterr().out


# ── 3. action_refresh raises → fail open, no exception ──────────────────


def test_stale_auto_refresh_failure_falls_back_to_stale(mini_repo, monkeypatch, caplog, capsys):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)

    def _raise(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("tools.collect.cli.action_refresh", _raise)

    with caplog.at_level(logging.WARNING):
        bridge = _bridge(mini_repo, config_path)

    assert bridge is not None
    assert bridge.usable is False
    assert bridge.status == STATUS_STALE

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "refresh on entry failed" in r.message]
    assert len(warnings) == 1
    assert "provider down" in warnings[0].message

    out = capsys.readouterr().out
    assert "pack OFF for this session" in out
    assert "staleness = refresh" in out


# ── 4. collect_refresh event ────────────────────────────────────────────


def _capture_events(monkeypatch):
    events = []
    monkeypatch.setattr("tools.agent_trace.tracer.event", lambda **kw: events.append(kw))
    return events


def test_stale_auto_refresh_emits_collect_refresh_event(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    events = _capture_events(monkeypatch)

    bridge = _bridge(mini_repo, config_path)
    assert bridge.usable is True

    refresh = [e for e in events if e.get("kind") == "collect_refresh"]
    assert len(refresh) == 1
    params = refresh[0]["params"]
    assert params["modules"] == 1
    assert params["ok"] is True
    assert isinstance(params["seconds"], float) and params["seconds"] >= 0


def test_failed_refresh_emits_collect_refresh_event_with_ok_false(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    monkeypatch.setattr("tools.collect.cli.action_refresh", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    events = _capture_events(monkeypatch)

    _bridge(mini_repo, config_path)

    refresh = [e for e in events if e.get("kind") == "collect_refresh"]
    assert len(refresh) == 1
    assert refresh[0]["params"]["ok"] is False
    assert refresh[0]["params"]["modules"] == 1


def test_no_event_without_the_flag(mini_repo, monkeypatch):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo)
    _make_stale(mini_repo)
    events = _capture_events(monkeypatch)

    _bridge(mini_repo, config_path)

    assert [e for e in events if e.get("kind") == "collect_refresh"] == []


# ── RUN-11: the line and the event say what Pass B is about to do ───────────
#
# The count was the hash diff, which understates the bill three orders of
# magnitude on a collector_version bump: the live run read "refreshing 1
# module(s)" while Pass B was about to work over 543 modules, and
# collect_refresh.modules recorded the same 1.


def test_entry_refresh_reports_a_version_bump_as_a_full_rebuild(mini_repo, monkeypatch, capsys):
    cli_mod.action_collect(mini_repo)
    _write_version1_manifest(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    events = _capture_events(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None and bridge.usable is True

    n = _n_modules(mini_repo)
    out = capsys.readouterr().out
    # The reason is named, and the count is the number Pass B is asked for.
    assert f"collector_version '1' → '2') — full rebuild, {n} module(s)" in out
    assert "refreshing" not in out

    refresh = [e for e in events if e.get("kind") == "collect_refresh"]
    assert len(refresh) == 1
    params = refresh[0]["params"]
    assert params["modules"] == n
    assert params["reason"] == "version"
    assert params["ok"] is True


def test_entry_refresh_reports_the_hash_diff_reason_when_the_version_matches(mini_repo, monkeypatch, capsys):
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    # All three modules of this repo move: the count is the diff.
    (mini_repo / "pkg" / "__init__.py").write_text("# regenerated\n")
    _make_stale(mini_repo)
    (mini_repo / "pkg" / "b.py").write_text("def b():\n    return 3\n")
    events = _capture_events(monkeypatch)

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None and bridge.usable is True

    out = capsys.readouterr().out
    assert "refreshing 3 module(s)" in out
    assert "collector_version" not in out

    refresh = [e for e in events if e.get("kind") == "collect_refresh"]
    assert len(refresh) == 1
    params = refresh[0]["params"]
    assert params["modules"] == 3
    assert params["reason"] == "sha"
    assert params["ok"] is True


def test_entry_refresh_no_count_when_the_pre_flight_fails(mini_repo, monkeypatch, capsys):
    """Fail-open: the version check must not turn a failed pre-flight into
    anything other than what it always was — "?" on the line, no reason in
    the event, and the refresh still runs. Only the bridge's own lazy
    `scan_repo` import is patched: `cli_mod.scan_repo` is bound at import,
    so `action_refresh` rescans for itself and the refresh completes."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    events = _capture_events(monkeypatch)
    monkeypatch.setattr(
        "tools.collect.scanner.scan_repo",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("walk failed")),
    )

    bridge = _bridge(mini_repo, config_path)

    assert bridge is not None and bridge.usable is True
    out = capsys.readouterr().out
    assert "collect: artifact stale" in out
    assert "refreshing ? module(s)" in out
    assert "collector_version" not in out
    assert "pack OFF" not in out

    refresh = [e for e in events if e.get("kind") == "collect_refresh"]
    assert len(refresh) == 1
    assert refresh[0]["params"]["modules"] is None
    assert refresh[0]["params"]["reason"] is None
    assert refresh[0]["params"]["ok"] is True


# ── RUN-11: Ctrl-C is the cheap way out ─────────────────────────────────────


def test_keyboard_interrupt_from_action_refresh_propagates(mini_repo, monkeypatch, capsys):
    """Ctrl-C is not a failure of the collect pack: it is the operator
    leaving. It must not read as one — no "pack OFF" line, no
    `collect_refresh` event (the run is gone, there is nothing to report)."""
    cli_mod.action_collect(mini_repo)
    config_path = _write_ini(mini_repo, auto_refresh_between_tasks="true")
    _make_stale(mini_repo)
    events = _capture_events(monkeypatch)

    def _interrupt(*args, **kwargs):
        raise KeyboardInterrupt("the operator hit Ctrl-C")

    monkeypatch.setattr("tools.collect.cli.action_refresh", _interrupt)

    with pytest.raises(KeyboardInterrupt):
        _bridge(mini_repo, config_path)

    out = capsys.readouterr().out
    assert "collect: artifact stale" in out  # the entry line was printed
    assert "pack OFF" not in out
    assert [e for e in events if e.get("kind") == "collect_refresh"] == []
