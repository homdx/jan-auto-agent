"""Judge's acceptance suite for round 143 (AR-5), written from the ticket alone.

`arena run rerun NN[.K] (--failed | --agent NAME) [--dead] [--dry-run]` through
`tools.arena.cli.main`, plus `scripts/revive_round.py`'s `revive_agents` and its
`--dead` / `--agent`. A throw-away git repo with `contest.ini` (`out_dir = out`),
`REPO_ROOT` patched, `rounds.PROC_ROOT` a fake `/proc`, `rounds.SPAWN` a stub
that records argv. Nothing runs a round, a kilo or a model.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/143/acceptance_143.py -n 8 -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402
from tools.arena import rounds  # noqa: E402
from scripts import revive_round  # noqa: E402

BASE = "abcdef0123456789abcdef0123456789abcdef01"


def agent(name: str, state: str = "READY") -> dict:
    return {"agent": {"name": name, "provider_id": "prov", "model_id": f"{name}-m"},
            "workspace": {"agent": name, "path": f"/nowhere/{name}", "base_sha": BASE},
            "state": state, "attempt": 2, "last_error": "boom" if state != "READY" else None,
            "commit": None, "tokens": {"input": 1, "output": 1, "reasoning": 0}}


MIX = [("r", "READY"), ("s", "STALLED"), ("g", "GAVE_UP"), ("e", "ERROR"), ("d", "DEAD")]


def write_state(folder: Path, nn: int, agents: list[dict], base: str | None = BASE) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    st = {"round_no": nn, "ticket": f"{nn:02d}-x.md", "started_at": 1790000000.0, "agents": agents}
    if base is not None:
        st["base_sha"] = base
    p = folder / "state.json"
    p.write_text(json.dumps(st))
    return p


def mix_state(repo: Path, nn: int = 7, folder: str | None = None) -> Path:
    return write_state(repo / "out" / (folder or f"{nn:02d}"), nn, [agent(n, s) for n, s in MIX])


class Spawn:
    """`rounds.SPAWN` stub: records argv; `wait` returns *code*, after rewriting
    *touch* (a state.json) when given."""

    def __init__(self, code: int = 0, touch: Path | None = None):
        self.calls: list[list[str]] = []
        self.code, self.touch = code, touch

    def __call__(self, argv, *a, **k):
        self.calls.append(list(argv))
        outer = self

        class Child:
            pid = 999999

            def wait(self, *a, **k):
                if outer.touch is not None:
                    # the runner's rewrite, stamped clearly after the start (a
                    # real runner writes minutes later; file clocks are coarse)
                    outer.touch.write_text(outer.touch.read_text())
                    later = time.time() + 5
                    os.utime(outer.touch, (later, later))
                return outer.code

            def poll(self):
                return outer.code

        return Child()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "contest.ini").write_text("[contest]\nout_dir = out\n\n[contest.agent.a]\nmodel = p/m-a\n")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "add", "contest.ini"], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "init"], check=True, env=env)
    proc = tmp_path / "proc"
    proc.mkdir()
    for mod in (cli, rounds):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    monkeypatch.chdir(tmp_path)  # cwd must not matter
    return r


@pytest.fixture
def spawn(monkeypatch):
    s = Spawn()
    monkeypatch.setattr(rounds, "SPAWN", s)
    return s


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


def states(path: Path) -> dict:
    return {a["agent"]["name"]: a["state"] for a in json.loads(path.read_text())["agents"]}


def line_of(spawn: Spawn) -> list[str]:
    assert len(spawn.calls) == 1, spawn.calls
    return spawn.calls[0]


def flag_value(argv: list[str], flag: str) -> list[str]:
    vals = []
    for i, w in enumerate(argv):
        if w == flag and i + 1 < len(argv):
            vals.append(argv[i + 1])
        elif w.startswith(flag + "="):
            vals.append(w.split("=", 1)[1])
    return vals


def set_profile(repo: Path, body: str) -> None:
    (repo / "contest.local.ini").write_text(f"[arena.profile.p]\n{body}")


# 1
def test_live_round_refused(repo, capsys, spawn, tmp_path):
    p = mix_state(repo)
    before = p.read_bytes()
    fake_proc(tmp_path / "proc", 4242, ["python3", "-m", "tools.contest", "run", "--ticket", "7"], repo)
    rc, out, err = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 2, (out, err)
    assert p.read_bytes() == before and spawn.calls == []
    assert not (p.parent / "state.before-revive.json").exists()


# 2
def test_dry_run(repo, capsys, spawn):
    p = mix_state(repo)
    before = p.read_bytes()
    rc, out, err = run(capsys, "run", "rerun", "7", "--failed", "--dry-run")
    assert rc == 0, err
    for n in ("s", "g", "e"):
        assert any(l.startswith(f"{n}:") and "WAITING" in l for l in out.splitlines()), out
    assert any(l.startswith("r:") and "kept" in l for l in out.splitlines()), out
    assert any(l.startswith("d:") and "kept" in l for l in out.splitlines()), out
    assert p.read_bytes() == before and spawn.calls == []
    assert not (p.parent / "state.before-revive.json").exists()


# 3
def test_failed_revives(repo, capsys, spawn):
    p = mix_state(repo)
    before = p.read_bytes()
    rc, out, err = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 0, err
    st = states(p)
    assert st == {"r": "READY", "s": "WAITING", "g": "WAITING", "e": "WAITING", "d": "DEAD"}
    assert (p.parent / "state.before-revive.json").read_bytes() == before
    by = {a["agent"]["name"]: a for a in json.loads(p.read_text())["agents"]}
    assert by["s"]["revived_from"]["state"] == "STALLED"
    assert by["g"]["revived_from"]["state"] == "GAVE_UP"
    assert "revived_from" not in by["r"] and "revived_from" not in by["d"]
    assert len(spawn.calls) == 1


# 4
def test_failed_dead(repo, capsys, spawn):
    p = mix_state(repo)
    rc, _, err = run(capsys, "run", "rerun", "7", "--failed", "--dead")
    assert rc == 0, err
    by = {a["agent"]["name"]: a for a in json.loads(p.read_text())["agents"]}
    assert by["d"]["state"] == "WAITING" and by["d"]["revived_from"]["state"] == "DEAD"
    assert by["r"]["state"] == "READY"


# 5
def test_agent_only(repo, capsys, spawn):
    p = mix_state(repo)
    rc, _, err = run(capsys, "run", "rerun", "7", "--agent", "g")
    assert rc == 0, err
    assert states(p) == {"r": "READY", "s": "STALLED", "g": "WAITING", "e": "ERROR", "d": "DEAD"}


def test_agent_ready_refused(repo, capsys, spawn):
    p = mix_state(repo)
    before = p.read_bytes()
    rc, _, err = run(capsys, "run", "rerun", "7", "--agent", "r")
    assert rc == 2 and "READY" in err and len(err.strip().splitlines()) == 1
    assert p.read_bytes() == before and spawn.calls == []


def test_agent_dead_needs_flag(repo, capsys, spawn):
    p = mix_state(repo)
    before = p.read_bytes()
    rc, _, err = run(capsys, "run", "rerun", "7", "--agent", "d")
    assert rc == 2 and "--dead" in err
    assert p.read_bytes() == before and spawn.calls == []
    rc, _, err = run(capsys, "run", "rerun", "7", "--agent", "d", "--dead")
    assert rc == 0, err
    assert states(p)["d"] == "WAITING"


def test_agent_unknown_lists_names(repo, capsys, spawn):
    p = mix_state(repo)
    before = p.read_bytes()
    rc, _, err = run(capsys, "run", "rerun", "7", "--agent", "nosuch")
    assert rc == 2
    assert all(n in err for n in ("r", "s", "g", "e", "d")) and "nosuch" in err
    assert p.read_bytes() == before and spawn.calls == []


def test_mode_required_and_exclusive(repo, capsys, spawn):
    p = mix_state(repo)
    before = p.read_bytes()
    rc, _, _ = run(capsys, "run", "rerun", "7")
    assert rc == 2
    rc, _, _ = run(capsys, "run", "rerun", "7", "--failed", "--agent", "s")
    assert rc == 2
    assert p.read_bytes() == before and spawn.calls == []


def test_no_state_exit_1(repo, capsys, spawn):
    rc, _, err = run(capsys, "run", "rerun", "55", "--failed")
    assert rc == 1 and len(err.strip().splitlines()) == 1 and "55" in err
    assert spawn.calls == []


# 6
def test_nothing_to_revive(repo, capsys, spawn):
    p = write_state(repo / "out" / "07", 7, [agent("r"), agent("d", "DEAD")])
    before = p.read_bytes()
    rc, _, err = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 3, err
    assert p.read_bytes() == before and spawn.calls == []
    assert not (p.parent / "state.before-revive.json").exists()


# 7
def test_base_ref_and_resume(repo, capsys, spawn):
    mix_state(repo)
    subprocess.run(["git", "-C", str(repo), "update-ref", "refs/heads/arena-round/7", "HEAD"], check=True)
    rc, _, err = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 0, err
    argv = line_of(spawn)
    assert "--resume" in argv
    assert flag_value(argv, "--ticket") == ["7"]
    assert flag_value(argv, "--base") == ["arena-round/7"]


def test_base_sha_fallback(repo, capsys, spawn):
    mix_state(repo)
    rc, _, err = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 0, err
    argv = line_of(spawn)
    assert flag_value(argv, "--base") == [BASE] and "--resume" in argv


def test_no_base_refused(repo, capsys, spawn):
    p = write_state(repo / "out" / "07", 7, [agent("s", "STALLED")], base=None)
    before = p.read_bytes()
    rc, _, _ = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 2 and p.read_bytes() == before and spawn.calls == []


def test_no_legs_no_out(repo, capsys, spawn):
    mix_state(repo)
    rc, _, err = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 0, err
    argv = line_of(spawn)
    assert "--out" not in " ".join(argv) and "--legs" not in " ".join(argv)


# 8
@pytest.mark.parametrize("prof,extra", [
    ("fresh = yes\n", []),
    ("extra = --fresh\n", []),
    ("", ["--", "--fresh"]),
])
def test_never_fresh(repo, capsys, spawn, prof, extra):
    set_profile(repo, "max_parallel = 3\n" + prof)
    mix_state(repo)
    rc, _, err = run(capsys, "-p", "p", "run", "rerun", "7", "--failed", *extra)
    assert rc == 0, err
    argv = line_of(spawn)
    assert "--fresh" not in argv
    assert flag_value(argv, "--max-parallel") == ["3"]


def test_passthrough_wins(repo, capsys, spawn):
    set_profile(repo, "max_parallel = 3\n")
    mix_state(repo)
    rc, _, err = run(capsys, "-p", "p", "run", "rerun", "7", "--failed", "--", "--max-parallel", "8")
    assert rc == 0, err
    assert flag_value(line_of(spawn), "--max-parallel") == ["8"]


# 9
def legs65(repo: Path) -> tuple[Path, Path]:
    return mix_state(repo, 65, "65.1"), mix_state(repo, 65, "65.2")


def test_legs_last(repo, capsys, spawn):
    set_profile(repo, "legs = 3\n")
    p1, p2 = legs65(repo)
    rc, _, err = run(capsys, "-p", "p", "run", "rerun", "65", "--failed")
    assert rc == 0, err
    argv = line_of(spawn)
    assert flag_value(argv, "--out") == [str(repo / "out" / "65.2")]
    assert flag_value(argv, "--legs") == ["1"]
    assert states(p2)["s"] == "WAITING" and states(p1)["s"] == "STALLED"


def test_legs_earlier_needs_yes(repo, capsys, spawn):
    set_profile(repo, "legs = 3\n")
    p1, _ = legs65(repo)
    before = p1.read_bytes()
    rc, _, err = run(capsys, "-p", "p", "run", "rerun", "65.1", "--failed")
    assert rc == 2 and p1.read_bytes() == before and spawn.calls == []
    rc, _, err = run(capsys, "-p", "p", "run", "rerun", "65.1", "--failed", "-y")
    assert rc == 0, err
    argv = line_of(spawn)
    assert flag_value(argv, "--out") == [str(repo / "out" / "65.1")]
    assert flag_value(argv, "--legs") == ["1"]


def test_legs_out_dir_from_roster(repo, capsys, spawn):
    (repo / "contest.ini").write_text("[contest]\nout_dir = elsewhere\n\n[contest.agent.a]\nmodel = p/m-a\n")
    mix_state(repo, 65, "../elsewhere/65.1")
    mix_state(repo, 65, "../elsewhere/65.2")
    rc, _, err = run(capsys, "run", "rerun", "65", "--failed")
    assert rc == 0, err
    out = flag_value(line_of(spawn), "--out")
    assert len(out) == 1 and Path(out[0]).resolve() == (repo / "elsewhere" / "65.2").resolve()


# 10
def test_exit_2_rewritten_is_4(repo, capsys, monkeypatch):
    p = mix_state(repo)
    s = Spawn(code=2, touch=p)
    monkeypatch.setattr(rounds, "SPAWN", s)
    rc, _, _ = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 4


def test_exit_2_untouched_is_1(repo, capsys, monkeypatch):
    # the rerun writes state.json itself just before the start; only the
    # runner's own rewrite after the start makes an exit 2 "no agent ready"
    mix_state(repo)
    monkeypatch.setattr(rounds, "SPAWN", Spawn(code=2))
    rc, _, _ = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 1


def test_lock_removed(repo, capsys, spawn):
    mix_state(repo)
    rc, _, _ = run(capsys, "run", "rerun", "7", "--failed")
    assert rc == 0
    assert not (repo / ".arena" / "locks" / "7.pid").exists()


# 11 — scripts/revive_round.py
def test_revive_agents_function():
    agents = [agent(n, s) for n, s in MIX]
    names = revive_round.revive_agents(agents)
    assert sorted(names) == ["e", "g", "s"]
    assert {a["agent"]["name"]: a["state"] for a in agents}["d"] == "DEAD"
    agents = [agent(n, s) for n, s in MIX]
    assert sorted(revive_round.revive_agents(agents, dead=True)) == ["d", "e", "g", "s"]
    agents = [agent(n, s) for n, s in MIX]
    assert revive_round.revive_agents(agents, only="g") == ["g"]
    assert [a["state"] for a in agents] == ["READY", "STALLED", "WAITING", "ERROR", "DEAD"]


EXPECTED_DRY = """r: READY (kept)
s: STALLED -> WAITING
g: GAVE_UP -> WAITING
e: ERROR -> WAITING
d: DEAD (kept)
3 to revive (dry run, nothing written)
"""


def test_script_output_unchanged(tmp_path, capsys):
    p = write_state(tmp_path / "out" / "07", 7, [agent(n, s) for n, s in MIX])
    before = p.read_bytes()
    rc = revive_round.main([str(p.parent), "--dry-run"])
    assert rc == 0 and capsys.readouterr().out == EXPECTED_DRY
    assert p.read_bytes() == before


def test_script_dead_dry(tmp_path, capsys):
    p = write_state(tmp_path / "out" / "07", 7, [agent(n, s) for n, s in MIX])
    rc = revive_round.main([str(p.parent), "--dead", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "d: DEAD -> WAITING" in out and "4 to revive" in out


def test_script_write_unchanged(tmp_path, capsys):
    p = write_state(tmp_path / "out" / "07", 7, [agent(n, s) for n, s in MIX])
    before = p.read_bytes()
    rc = revive_round.main([str(p.parent)])
    assert rc == 0
    assert (p.parent / "state.before-revive.json").read_bytes() == before
    assert states(p)["d"] == "DEAD" and states(p)["s"] == "WAITING"


def test_other_verbs_unchanged(repo, capsys):
    rc, _, err = run(capsys, "issue", "create")
    assert rc != 0 and "not implemented" in err
