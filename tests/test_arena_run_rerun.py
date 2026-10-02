"""tests/test_arena_run_rerun.py — AR-5: `arena run rerun NN[.K]` revives agents, then resumes."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts import revive_round
from tools.arena import cli, rounds

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

INI = """[contest]
out_dir = {out}

[contest.agent.a]
model = test/a
"""

SHA = "0123456789abcdef0123456789abcdef01234567"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _state(folder: Path, states: dict[str, str], base: str = SHA) -> Path:
    agents = [{"agent": {"name": name, "provider_id": "p", "model_id": "m"},
               "state": state, "attempt": 2, "last_error": f"{state} happened"}
              for name, state in states.items()]
    path = folder / "state.json"
    _write(path, json.dumps({"round_no": 1, "ticket": "t", "base_sha": base, "agents": agents}))
    return path


class _Stub:
    """`rounds.SPAWN`: records the argv, returns a child that exits with *code*."""

    def __init__(self, code: int = 0, rewrite: Path | None = None):
        self.seen: list[list[str]] = []
        self.code, self.rewrite = code, rewrite

    def __call__(self, line, cwd):
        self.seen.append(list(line))
        stub = self

        class Child:
            pid = 4242

            def wait(self):
                if stub.rewrite is not None:
                    stub.rewrite.write_text(stub.rewrite.read_text(encoding="utf-8"),
                                            encoding="utf-8")
                    os.utime(stub.rewrite, (1e10, 1e10))
                return stub.code
        return Child()


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "contest.ini", INI.format(out="out"))
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


@pytest.fixture
def spawn(monkeypatch) -> _Stub:
    stub = _Stub()
    monkeypatch.setattr(rounds, "SPAWN", stub)
    return stub


def _rerun(capsys, *argv):
    code = cli.main(["run", "rerun", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _on_disk(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# 1 ─ a live round
def test_a_live_round_is_refused_and_untouched(repo, spawn, tmp_path, capsys, monkeypatch):
    state = _state(repo / "out" / "07", {"a": "STALLED"})
    before = state.read_bytes()
    proc = tmp_path / "proc" / "99"
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(
        w.encode() for w in ["python", "-m", "tools.contest", "run", "--ticket", "7"]) + b"\0")
    os.symlink(repo, proc / "cwd")
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "proc"))
    code, out, err = _rerun(capsys, "7", "--failed")
    assert code == 2 and len(err.splitlines()) == 1 and "running" in err
    assert state.read_bytes() == before and spawn.seen == []
    assert not (state.parent / revive_round.BACKUP_NAME).exists()


# 2 ─ --dry-run
def test_dry_run_lists_every_agent_and_writes_nothing(repo, spawn, capsys):
    state = _state(repo / "out" / "07", {"a": "STALLED", "b": "READY", "c": "DEAD"})
    before = state.read_bytes()
    code, out, _ = _rerun(capsys, "7", "--failed", "--dry-run")
    lines = out.splitlines()
    assert code == 0
    assert lines[:3] == ["a: STALLED -> WAITING", "b: READY (kept)", "c: DEAD (kept)"]
    assert state.read_bytes() == before and spawn.seen == []


# 3, 4 ─ what comes back
def test_failed_revives_the_failed_and_keeps_ready_and_dead(repo, spawn, capsys):
    state = _state(repo / "out" / "07", {"s": "STALLED", "g": "GAVE_UP", "e": "ERROR",
                                         "r": "READY", "d": "DEAD"})
    old = state.read_bytes()
    code, _, _ = _rerun(capsys, "7", "--failed")
    assert code == 0 and len(spawn.seen) == 1
    got = {a["agent"]["name"]: a for a in _on_disk(state)["agents"]}
    assert [got[n]["state"] for n in "sgerd"] == ["WAITING"] * 3 + ["READY", "DEAD"]
    assert got["s"]["revived_from"]["state"] == "STALLED"
    assert got["s"]["revived_from"]["last_error"] == "STALLED happened"
    assert "revived_from" not in got["r"] and "revived_from" not in got["d"]
    assert (state.parent / revive_round.BACKUP_NAME).read_bytes() == old


def test_failed_with_dead_revives_the_dead_too(repo, spawn, capsys):
    state = _state(repo / "out" / "07", {"d": "DEAD", "r": "READY"})
    assert _rerun(capsys, "7", "--failed", "--dead")[0] == 0
    got = {a["agent"]["name"]: a for a in _on_disk(state)["agents"]}
    assert got["d"]["state"] == "WAITING" and got["d"]["revived_from"]["state"] == "DEAD"
    assert got["r"]["state"] == "READY"


# 5 ─ --agent
def test_agent_revives_that_one_only(repo, spawn, capsys):
    state = _state(repo / "out" / "07", {"x": "STALLED", "y": "ERROR"})
    assert _rerun(capsys, "7", "--agent", "x")[0] == 0
    got = {a["agent"]["name"]: a["state"] for a in _on_disk(state)["agents"]}
    assert got == {"x": "WAITING", "y": "ERROR"}


@pytest.mark.parametrize("argv, words", [
    (["--agent", "r"], ["READY"]),
    (["--agent", "w"], ["WAITING"]),
    (["--agent", "d"], ["DEAD", "--dead"]),
    (["--agent", "zz"], ["zz", "r", "d"]),
    (["--failed", "--agent", "x"], ["--failed", "--agent"]),
    ([], ["--failed", "--agent"]),
])
def test_agent_refusals_name_the_reason(repo, spawn, capsys, argv, words):
    state = _state(repo / "out" / "07", {"x": "STALLED", "r": "READY", "w": "WAITING",
                                         "d": "DEAD"})
    state_bytes = state.read_bytes()
    code, _, err = _rerun(capsys, "7", *argv)
    assert code == 2 and len(err.splitlines()) == 1
    assert all(word in err for word in words)
    assert state.read_bytes() == state_bytes and spawn.seen == []


# 6 ─ nothing to revive
def test_nothing_to_revive_is_exit_3(repo, spawn, capsys):
    state = _state(repo / "out" / "07", {"a": "READY", "b": "DEAD"})
    before = state.read_bytes()
    code, out, err = _rerun(capsys, "7", "--failed")
    assert code == 3 and out == "" and len(err.splitlines()) == 1
    assert state.read_bytes() == before and spawn.seen == []
    assert not (state.parent / revive_round.BACKUP_NAME).exists()


def test_no_state_is_exit_1_with_the_path(repo, spawn, capsys):
    code, _, err = _rerun(capsys, "9", "--failed")
    assert code == 1 and len(err.splitlines()) == 1 and str(repo / "out" / "09") in err


# 7 ─ the base
def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_run_line_carries_resume_and_the_base_sha(repo, spawn, capsys):
    _state(repo / "out" / "07", {"a": "ERROR"})
    assert _rerun(capsys, "7", "--failed")[0] == 0
    line = spawn.seen[0]
    assert line[1:5] == ["-m", "tools.contest", "run", "--ticket"] and line[5] == "7"
    assert line[line.index("--base") + 1] == SHA
    assert line[-1] == "--resume" and line.count("--resume") == 1


def test_run_line_prefers_the_round_ref_when_it_exists(repo, spawn, capsys):
    _state(repo / "out" / "07", {"a": "ERROR"})
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "contest.ini")
    _git(repo, "commit", "-q", "-m", "x")
    _git(repo, "update-ref", "refs/heads/arena-round/7", "HEAD")
    assert _rerun(capsys, "7", "--failed")[0] == 0
    line = spawn.seen[0]
    assert line[line.index("--base") + 1] == "arena-round/7"


def test_no_ref_and_no_base_sha_is_a_refusal(repo, spawn, capsys):
    state = _state(repo / "out" / "07", {"a": "ERROR"}, base="")
    before = state.read_bytes()
    code, _, err = _rerun(capsys, "7", "--failed")
    assert code == 2 and len(err.splitlines()) == 1
    assert state.read_bytes() == before and spawn.seen == []


# 8 ─ --fresh never reaches a rerun
def test_fresh_never_reaches_the_runner(repo, spawn, capsys):
    _write(repo / "contest.local.ini",
           "[arena.profile.default]\nfresh = yes\nextra = --fresh --no-gate\n")
    _state(repo / "out" / "07", {"a": "ERROR"})
    assert _rerun(capsys, "7", "--failed", "--", "--fresh", "--max-parallel", "3")[0] == 0
    line = spawn.seen[0]
    assert "--fresh" not in line and "--no-gate" in line
    assert line[line.index("--max-parallel") + 1] == "3"


# 9 ─ a round of legs
def test_legs_round_resumes_its_last_leg_alone(repo, spawn, capsys):
    _write(repo / "contest.local.ini", "[arena.profile.default]\nlegs = 3\n")
    _state(repo / "out" / "65.1", {"a": "ERROR"})
    _state(repo / "out" / "65.2", {"a": "ERROR"})
    assert _rerun(capsys, "65", "--failed")[0] == 0
    line = spawn.seen[0]
    assert line[line.index("--out") + 1] == str(repo / "out" / "65.2")
    assert line.count("--legs") == 1 and line[line.index("--legs") + 1] == "1"


def test_an_earlier_leg_needs_yes(repo, spawn, capsys):
    first = _state(repo / "out" / "65.1", {"a": "ERROR"})
    _state(repo / "out" / "65.2", {"a": "ERROR"})
    before = first.read_bytes()
    code, _, err = _rerun(capsys, "65.1", "--failed")
    assert code == 2 and len(err.splitlines()) == 1 and "65.2" in err
    assert first.read_bytes() == before and spawn.seen == []
    assert _rerun(capsys, "65.1", "--failed", "-y")[0] == 0
    line = spawn.seen[0]
    assert line[line.index("--out") + 1] == str(repo / "out" / "65.1")


def test_a_round_without_legs_gets_no_out_and_no_legs(repo, spawn, capsys):
    _write(repo / "contest.local.ini", "[arena.profile.default]\nlegs = 3\n")
    _state(repo / "out" / "07", {"a": "ERROR"})
    assert _rerun(capsys, "7", "--failed")[0] == 0
    assert "--out" not in spawn.seen[0] and "--legs" not in spawn.seen[0]


# 10 ─ the exit code
def test_exit_2_is_no_ready_only_when_the_runner_rewrote_state(repo, monkeypatch, capsys):
    state = _state(repo / "out" / "07", {"a": "ERROR"})
    monkeypatch.setattr(rounds, "SPAWN", _Stub(code=2, rewrite=state))
    assert _rerun(capsys, "7", "--failed")[0] == 4
    state = _state(repo / "out" / "08", {"a": "ERROR"})
    monkeypatch.setattr(rounds, "SPAWN", _Stub(code=2))
    assert _rerun(capsys, "8", "--failed")[0] == 1


# 11 ─ scripts/revive_round.py
def test_revive_round_script_output_without_the_new_flags(tmp_path, capsys):
    path = _state(tmp_path / "out" / "07", {"a": "STALLED", "d": "DEAD", "r": "READY"})
    before = path.read_bytes()
    assert revive_round.main([str(path), "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "a: STALLED -> WAITING", "d: DEAD (kept)", "r: READY (kept)",
        "1 to revive (dry run, nothing written)"]
    assert revive_round.main([str(path), "--dead", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines()[:3] == [
        "a: STALLED -> WAITING", "d: DEAD -> WAITING", "r: READY (kept)"]
    assert revive_round.main([str(path), "--agent", "d", "--dead", "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines()[:3] == [
        "a: STALLED (kept)", "d: DEAD -> WAITING", "r: READY (kept)"]
    assert path.read_bytes() == before
