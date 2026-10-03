"""Judge's acceptance suite for round 139 (AR-4), written from the ticket alone.

`arena run view NN[.K]` through `tools.arena.cli.main`: a throw-away repo with
`contest.ini` (`out_dir = out`), round folders holding a `state.json` shaped
like a real round's, `REPO_ROOT` patched, `rounds.PROC_ROOT` pointed at a fake
`/proc`. Nothing runs a round, a kilo or a model.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/139/acceptance_139.py -n 8 -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402
from tools.arena import rounds  # noqa: E402

BASE = "abcdef0123456789abcdef0123456789abcdef01"


def agent(name: str, state: str = "READY", tokens=None, commit: str | None = "1234567890abcdef1234",
          **extra) -> dict:
    a = {
        "agent": {"name": name, "provider_id": "prov", "model_id": f"{name}-m", "kilo_agent": None,
                  "variant": None, "context_limit": 100000},
        "workspace": {"agent": name, "path": f"/nowhere/{name}", "branch": f"contest/x/{name}",
                      "base_sha": BASE, "kind": "clone"},
        "session_id": "ses_x", "state": state, "attempt": 1, "turns": [],
        "permissions": {"asked": 3, "allowed": 3, "rejected": 0, "gated": 0, "gate_failed": 0},
        "questions": 0, "last_error": None, "resumable": False, "commit": commit,
        "deadline_commit": False, "cost": 0.0,
        "tokens": tokens if tokens is not None else {"input": 100, "output": 20, "reasoning": 3,
                                                      "cache": {"read": 900, "write": 0}},
        "continues": 0, "sessions": 1, "sessions_this_attempt": 1, "last_diff_signature": "",
        "first_touch_nudges_used": 0, "first_touch_resets": 0, "compactions": 0,
        "summary": "", "summaries": [], "summary_attempted_at_attempt": -1,
        "summary_session_id": "",
    }
    a.update(extra)
    return a


def write_state(folder: Path, nn: int, agents: list[dict], base: str | None = BASE) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    st = {"round_no": nn, "ticket": f"{nn:02d}-x.md", "started_at": 1790000000.0, "agents": agents}
    if base is not None:
        st["base_sha"] = base
    (folder / "state.json").write_text(json.dumps(st))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "contest.ini").write_text("[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    proc = tmp_path / "proc"
    proc.mkdir()
    for mod in (cli, rounds):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    return r


def fake_proc(root: Path, pid: int, argv: list[str], cwd: Path) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    os.symlink(cwd, d / "cwd")


def run(capsys, *argv):
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    c = capsys.readouterr()
    return rc, c.out, c.err


def header(out: str) -> str:
    lines = [l for l in out.splitlines() if l.strip()]
    assert lines, out
    return lines[0]


def legs(repo: Path) -> None:
    write_state(repo / "out" / "134.1", 134, [agent("leg-one-agent")])
    write_state(repo / "out" / "134.2", 134, [agent("leg-two-agent"), agent("leg-two-b", "STALLED")])


# 1
def test_bare_nn_is_last_leg(repo, capsys):
    legs(repo)
    rc, out, err = run(capsys, "run", "view", "134")
    assert rc == 0, err
    h = header(out)
    assert "round 134" in h and "leg 2/2" in h
    assert "leg-two-agent" in out and "leg-two-b" in out and "leg-one-agent" not in out


# 2
def test_explicit_leg(repo, capsys):
    legs(repo)
    rc, out, err = run(capsys, "run", "view", "134.1")
    assert rc == 0, err
    assert "leg 1/2" in header(out)
    assert "leg-one-agent" in out and "leg-two-agent" not in out


# 3
def test_no_legs_and_leading_zero(repo, capsys):
    write_state(repo / "out" / "07", 7, [agent("solo")])
    rc, out7, err = run(capsys, "run", "view", "7")
    assert rc == 0, err
    h = header(out7)
    assert "leg" not in h and "round 7" in h and "done" in h and "abcdef0" in h
    assert "abcdef01" not in h  # sha7
    rc, out07, _ = run(capsys, "run", "view", "07")
    assert rc == 0 and out07 == out7
    assert "solo" in out7


def test_header_shape_exact(repo, capsys):
    write_state(repo / "out" / "07", 7, [agent("solo")])
    rc, out, _ = run(capsys, "run", "view", "7")
    assert header(out).strip() == "round 7 · done · base abcdef0"


def test_base_missing_is_question_mark(repo, capsys):
    write_state(repo / "out" / "07", 7, [agent("solo")], base=None)
    rc, out, err = run(capsys, "run", "view", "7")
    # cmd_status itself cannot read a state.json without base_sha; its exit
    # code is run view's (the ticket), so only the header is arena's to get right
    assert header(out).rstrip().endswith("base ?")
    assert "Traceback" not in err


# 4
def test_no_round(repo, capsys):
    rc, out, err = run(capsys, "run", "view", "55")
    assert rc == 1
    lines = err.strip().splitlines()
    assert len(lines) == 1 and "no round 55" in lines[0]
    assert str(repo / "out") in lines[0] or "out/55" in lines[0]
    assert "Traceback" not in err


def test_folder_without_state(repo, capsys):
    (repo / "out" / "56").mkdir(parents=True)
    rc, _, err = run(capsys, "run", "view", "56")
    assert rc == 1 and len(err.strip().splitlines()) == 1 and "no round 56" in err


# 5
@pytest.mark.parametrize("body", ["{broken", "[1, 2]", '{"round_no": 8}', '{"agents": 5}'])
def test_broken_state(repo, capsys, body):
    f = repo / "out" / "08"
    f.mkdir(parents=True)
    (f / "state.json").write_text(body)
    rc, out, err = run(capsys, "run", "view", "8")
    assert rc == 1, (out, err)
    lines = err.strip().splitlines()
    assert len(lines) == 1 and "state.json" in lines[0]
    assert "Traceback" not in err + out


# 6
def test_json(repo, capsys):
    write_state(repo / "out" / "09", 9, [
        agent("first", tokens={"input": 1000, "output": 200, "reasoning": 30, "cache": {"read": 5}}),
        agent("second", "STALLED", tokens={"input": 7}, commit=None, api_key="sk-LEAKME-123456"),
        agent("third", tokens={}),
    ])
    rc, out, err = run(capsys, "run", "view", "9", "-o", "json")
    if rc == 2 and "unrecognized" in err:
        rc, out, err = run(capsys, "-o", "json", "run", "view", "9")
    assert rc == 0, err
    data = json.loads(out)
    assert [r["agent"] for r in data] == ["first", "second", "third"]
    assert all({"agent", "state", "attempt", "tokens", "commit"} <= set(r) for r in data)
    assert data[0]["tokens"] == 1230 and isinstance(data[0]["tokens"], int)
    assert data[1]["tokens"] == 7 and data[2]["tokens"] == 0
    assert data[0]["commit"] == "1234567890ab" and data[1]["commit"] in ("", None)
    assert data[1]["state"] == "STALLED"
    assert "***" not in out
    assert "sk-LEAKME-123456" not in out + err


def test_json_has_no_header(repo, capsys):
    legs(repo)
    rc, out, _ = run(capsys, "-o", "json", "run", "view", "134")
    assert rc == 0
    assert [r["agent"] for r in json.loads(out)] == ["leg-two-agent", "leg-two-b"]


# 7
def test_running_vs_done(repo, capsys, tmp_path):
    write_state(repo / "out" / "134", 134, [agent("a1")])
    rc, out, _ = run(capsys, "run", "view", "134")
    assert "done" in header(out) and "running" not in header(out)
    fake_proc(tmp_path / "proc", 4242, ["python3", "-m", "tools.contest", "run", "--ticket", "134"], repo)
    rc, out, _ = run(capsys, "run", "view", "134")
    assert rc == 0 and "running" in header(out)


def test_other_repo_round_is_not_running(repo, capsys, tmp_path):
    write_state(repo / "out" / "134", 134, [agent("a1")])
    other = tmp_path / "elsewhere"
    other.mkdir()
    fake_proc(tmp_path / "proc", 4243, ["python3", "-m", "tools.contest", "run", "--ticket", "134"], other)
    rc, out, _ = run(capsys, "run", "view", "134")
    assert "done" in header(out)


# 8
@pytest.mark.parametrize("bad", ["abc", "1.2.3", "-3", "1.", ".2", "1.x"])
def test_bad_spec_refused(repo, capsys, bad):
    rc, out, err = run(capsys, "run", "view", bad)
    assert rc == 2, (bad, out, err)
    assert "Traceback" not in err


# 9
def test_repo_root_real_value():
    import importlib
    fresh = importlib.reload(importlib.import_module("tools.arena.cli"))
    assert Path(fresh.REPO_ROOT).resolve() == ROOT.resolve()


# extras from the ticket
def test_writes_nothing_starts_nothing(repo, capsys, monkeypatch):
    legs(repo)
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))

    def boom(*a, **k):
        raise AssertionError("run view started a process")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    rc, _, err = run(capsys, "run", "view", "134")
    assert rc == 0, err
    assert sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*")) == before


def test_bad_ini_is_refusal(repo, capsys):
    (repo / "contest.ini").write_text("[contest\nbroken")
    rc, _, err = run(capsys, "run", "view", "7")
    assert rc == 2 and len(err.strip().splitlines()) == 1


def test_other_verbs_unchanged(repo, capsys):
    # `issue list` landed in round 144 (AR-6); another unimplemented verb
    rc, _, err = run(capsys, "issue", "land")
    assert rc != 0 and "not implemented" in err
