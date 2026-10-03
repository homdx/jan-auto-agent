"""tests/test_arena_run_view.py — AR-4: `arena run view NN[.K]`."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.arena import cli, rounds
from tools.contest.roster import AgentSpec
from tools.contest.runner import AgentRun, AgentState, RoundState
from tools.contest.workspace import Workspace

INI = """[contest]
out_dir = contest-out

[contest.agent.a]
model = test/a
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ws(folder: Path, name: str, round_no: int = 134) -> Workspace:
    """A minimal Workspace for *name* under *folder*."""
    path = folder / "worktrees" / name
    path.mkdir(parents=True, exist_ok=True)
    return Workspace(
        agent=name,
        path=path,
        branch=f"contest/{round_no:02d}/{name}",
        base_sha="abc1234def56789" * 3,
        kind="worktree",
    )


def _agent_run(ws: Workspace, state: str = "READY", attempt: int = 1,
               tokens: dict | None = None, commit: str = "") -> AgentRun:
    return AgentRun(
        agent=AgentSpec(ws.agent, "test", f"{ws.agent}:free"),
        workspace=ws,
        state=AgentState(state),
        attempt=attempt,
        tokens=tokens or {},
        commit=commit,
    )


def _state(folder: Path, agent_specs: list[dict], round_no: int = 134,
           base_sha: str = "abc1234def56789") -> None:
    """Write a valid state.json to *folder* that cmd_status can parse."""
    folder.mkdir(parents=True, exist_ok=True)
    runs = []
    for spec in agent_specs:
        ws = _ws(folder, spec.get("agent", "a"), round_no)
        run = _agent_run(
            ws,
            state=spec.get("state", "READY"),
            attempt=spec.get("attempt", 1),
            tokens=spec.get("tokens") or {},
            commit=spec.get("commit", ""),
        )
        runs.append(run)
    state = RoundState(
        round_no=round_no,
        ticket=f"{round_no}-test.md",
        base_sha=base_sha,
        started_at=1700000000.0,
        agents=runs,
    )
    data = state.to_dict()
    # Merge any extra per-agent fields (like api_key) for masking tests.
    for i, spec in enumerate(agent_specs):
        for k, v in spec.items():
            if k not in ("agent", "state", "attempt", "tokens", "commit"):
                data["agents"][i][k] = v
    (folder / "state.json").write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A throw-away repo with contest.ini and patched REPO_ROOT / PROC_ROOT."""
    r = tmp_path / "repo"
    _write(r / "contest.ini", INI)
    monkeypatch.setattr(cli, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return r


# ── 1. bare NN resolves to highest leg ───────────────────────────────────────
def test_bare_nn_shows_highest_leg(repo, capsys):
    """run view 134 with two legs shows leg 2/2 in the header."""
    out = repo / "contest-out"
    agents = [{"agent": "a", "state": "READY", "attempt": 1,
               "tokens": {"input": 10, "output": 20, "reasoning": 5}, "commit": "abc123456789ef"}]
    _state(out / "134.1", agents, round_no=134)
    _state(out / "134.2", agents, round_no=134)
    rc = cli.main(["run", "view", "134"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "leg 2/2" in captured.out


# ── 2. NN.K shows the named leg ───────────────────────────────────────────────
def test_nn_dot_k_shows_named_leg(repo, capsys):
    """run view 134.1 shows leg 1/2 in the header."""
    out = repo / "contest-out"
    agents = [{"agent": "a", "state": "READY", "attempt": 1, "tokens": {}, "commit": ""}]
    _state(out / "134.1", agents, round_no=134)
    _state(out / "134.2", agents, round_no=134)
    rc = cli.main(["run", "view", "134.1"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "leg 1/2" in captured.out


# ── 3. round without legs has no leg part ─────────────────────────────────────
def test_round_without_legs_has_no_leg_in_header(repo, capsys):
    """run view 7 and run view 07 both work; no 'leg' in the header."""
    out = repo / "contest-out"
    agents = [{"agent": "a", "state": "READY", "attempt": 1, "tokens": {}, "commit": ""}]
    _state(out / "07", agents, round_no=7)

    rc1 = cli.main(["run", "view", "7"])
    out1 = capsys.readouterr().out
    assert rc1 == 0
    assert "leg" not in out1

    rc2 = cli.main(["run", "view", "07"])
    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "leg" not in out2


# ── 4. missing folder exits 1 with one stderr line naming the path ────────────
def test_missing_round_exits_1_with_path(repo, capsys):
    """No folder for round 99: exit 1, one stderr line naming the path."""
    rc = cli.main(["run", "view", "99"])
    assert rc == 1
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 1
    assert "99" in err


# ── 5. broken state.json exits 1 with one stderr line ────────────────────────
def test_broken_state_json_exits_1(repo, capsys):
    """Broken JSON in state.json: exit 1, one line on stderr, no traceback."""
    _write(repo / "contest-out" / "50" / "state.json", "{not json")
    rc = cli.main(["run", "view", "50"])
    assert rc == 1
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert "Traceback" not in err


# ── 6. -o json output ─────────────────────────────────────────────────────────
def test_json_output_tokens_are_integers_and_keys_are_masked(repo, capsys):
    """-o json: tokens is input+output+reasoning, api_key is not emitted."""
    out = repo / "contest-out"
    agents = [
        {
            "agent": "m1",
            "state": "READY",
            "attempt": 2,
            "tokens": {"input": 100, "output": 200, "reasoning": 50},
            "commit": "aabbccddeeff0011",
            "api_key": "sekrit42",
        }
    ]
    _state(out / "20", agents, round_no=20)
    rc = cli.main(["-o", "json", "run", "view", "20"])
    assert rc == 0
    raw = capsys.readouterr().out
    rows = json.loads(raw)
    assert len(rows) == 1
    row = rows[0]
    assert row["tokens"] == 350
    assert row["commit"] == "aabbccddeeff"  # 12-char prefix
    assert "sekrit42" not in raw
    assert "api_key" not in row  # only the 5 defined keys are emitted


# ── 7. running vs done ────────────────────────────────────────────────────────
def test_running_and_done_status_in_header(repo, tmp_path, monkeypatch, capsys):
    """A fake /proc with a matching process → 'running'; without it → 'done'."""
    out = repo / "contest-out"
    agents = [{"agent": "a", "state": "WAITING", "attempt": 1, "tokens": {}, "commit": ""}]
    _state(out / "134", agents, round_no=134)

    # done (no proc)
    rc = cli.main(["run", "view", "134"])
    assert rc == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "done" in header

    # running: fake /proc/1234/cmdline + /proc/1234/cwd → repo
    proc = tmp_path / "proc"
    pid_dir = proc / "1234"
    pid_dir.mkdir(parents=True)
    cmdline = b"python3\x00-m\x00tools.contest\x00run\x00--ticket\x00134\x00"
    (pid_dir / "cmdline").write_bytes(cmdline)
    cwd_link = pid_dir / "cwd"
    cwd_link.symlink_to(repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))

    rc2 = cli.main(["run", "view", "134"])
    assert rc2 == 0
    header2 = capsys.readouterr().out.splitlines()[0]
    assert "running" in header2


# ── 8. bad argument formats are refusals ─────────────────────────────────────
def test_bad_round_arguments_are_refusals(repo, capsys):
    """'abc' and '1.2.3' are exit 2 with one stderr line."""
    for bad in ("abc", "1.2.3"):
        rc = cli.main(["run", "view", bad])
        assert rc == 2, f"expected exit 2 for {bad!r}"
        err = capsys.readouterr().err
        assert len(err.splitlines()) == 1
        assert err.startswith("arena: ")


# ── 9. REPO_ROOT points at the checkout root ─────────────────────────────────
def test_repo_root_is_the_checkout_root():
    """The real, unpatched REPO_ROOT is this repo's root (keep AR-3 check green)."""
    assert (cli.REPO_ROOT / "tools" / "arena" / "cli.py").is_file()
