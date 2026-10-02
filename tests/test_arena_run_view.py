"""tests/test_arena_run_view.py — AR-4: `arena run view NN[.K]` over the roster's out_dir."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.arena import cli, rounds

INI = """[contest]
out_dir = contest-out

[contest.agent.a]
model = test/a
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _state(folder: Path, names: list[str], *, base="abcdef0123456789", **extra) -> None:
    """A `state.json` `cmd_status` can read: the shape `RoundState.to_dict` writes."""
    agents = []
    for i, name in enumerate(names):
        agents.append({
            "agent": {"name": name, "provider_id": "p", "model_id": "m"},
            "workspace": {"agent": name, "path": str(folder / name), "branch": f"contest/1/{name}",
                          "base_sha": base, "kind": "worktree"},
            "state": "READY",
            "attempt": i + 1,
            "commit": "0123456789abcdef0123",
            "tokens": {"input": 100, "output": 20, "reasoning": 3, "cache": {"read": 5, "write": 7}},
            **extra,
        })
    _write(folder / "state.json", json.dumps({
        "round_no": 1, "ticket": "t", "base_sha": base, "started_at": 1.0, "agents": agents}))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "contest.ini", INI)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def _view(capsys, *argv):
    code = cli.main(["run", "view", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_bare_round_shows_its_last_leg(repo, capsys):
    _state(repo / "contest-out" / "134.1", ["one-a"])
    _state(repo / "contest-out" / "134.2", ["two-a", "two-b"])
    code, out, _ = _view(capsys, "134")
    assert code == 0
    assert out.splitlines()[0] == "round 134 · leg 2/2 · done · base abcdef0"
    assert "two-a" in out and "two-b" in out and "one-a" not in out


def test_leg_argument_shows_that_leg(repo, capsys):
    _state(repo / "contest-out" / "134.1", ["one-a"])
    _state(repo / "contest-out" / "134.2", ["two-a"])
    code, out, _ = _view(capsys, "134.1")
    assert code == 0
    assert out.splitlines()[0].startswith("round 134 · leg 1/2 · ")
    assert "one-a" in out and "two-a" not in out


def test_leg_count_is_the_last_leg_number(repo, capsys):
    # round 139: with only `07.2` left the header read `leg 2/1`
    _state(repo / "contest-out" / "07.2", ["a7"])
    code, out, _ = _view(capsys, "07.2")
    assert code == 0 and out.splitlines()[0].startswith("round 7 · leg 2/2 · ")


def test_round_without_legs_has_no_leg_part(repo, capsys):
    _state(repo / "contest-out" / "07", ["solo"])
    code, out, _ = _view(capsys, "7")
    assert code == 0
    assert out.splitlines()[0] == "round 7 · done · base abcdef0"
    assert "solo" in out
    assert _view(capsys, "07")[1] == out


def test_missing_base_sha_prints_a_question_mark(repo, capsys):
    _state(repo / "contest-out" / "07", ["solo"], base="")
    assert _view(capsys, "7")[1].splitlines()[0] == "round 7 · done · base ?"


def test_no_folder_is_exit_1_with_the_path(repo, capsys):
    code, out, err = _view(capsys, "9")
    assert code == 1 and out == ""
    lines = err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("arena: no round 9 (looked in ")
    assert str(repo / "contest-out" / "09") in lines[0]


@pytest.mark.parametrize("text", ["{not json", "[]", '{"agents": 3}', '{"round_no": 1}'])
def test_broken_state_is_exit_1_one_line(repo, capsys, text):
    _write(repo / "contest-out" / "05" / "state.json", text)
    for extra in ([], ["-o", "json"]):
        code = cli.main([*extra, "run", "view", "5"])
        cap = capsys.readouterr()
        assert code == 1 and cap.out == ""
        assert len(cap.err.splitlines()) == 1 and "state.json" in cap.err
        assert "Traceback" not in cap.err


def test_json_rows_are_masked_numbers_stay_numbers(repo, capsys):
    _state(repo / "contest-out" / "07", ["a-one", "a-two"], api_key="sk-secret-value")
    assert cli.main(["-o", "json", "run", "view", "7"]) == 0
    raw = capsys.readouterr().out
    rows = json.loads(raw)
    assert [r["agent"] for r in rows] == ["a-one", "a-two"]
    assert [r["attempt"] for r in rows] == [1, 2]
    assert all(r["tokens"] == 123 and isinstance(r["tokens"], int) for r in rows)
    assert all(r["commit"] == "0123456789ab" for r in rows)
    assert "***" not in raw and "sk-secret-value" not in raw
    assert set(rows[0]) == {"agent", "state", "attempt", "tokens", "commit"}


def test_missing_tokens_count_as_zero(repo, capsys):
    _state(repo / "contest-out" / "07", ["a"])
    data = json.loads((repo / "contest-out" / "07" / "state.json").read_text())
    del data["agents"][0]["tokens"]
    _write(repo / "contest-out" / "07" / "state.json", json.dumps(data))
    cli.main(["-o", "json", "run", "view", "7"])
    assert json.loads(capsys.readouterr().out)[0]["tokens"] == 0


def _fake_proc(root: Path, pid: str, cwd: Path, words: list[str]) -> None:
    (root / pid).mkdir(parents=True)
    (root / pid / "cmdline").write_bytes(b"\0".join(w.encode() for w in words) + b"\0")
    os.symlink(cwd, root / pid / "cwd")


def test_running_follows_a_live_contest_run(repo, tmp_path, monkeypatch, capsys):
    _state(repo / "contest-out" / "134", ["a"])
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    assert " · done · " in _view(capsys, "134")[1].splitlines()[0]
    _fake_proc(proc, "4242", repo, ["python3", "-m", "tools.contest", "run", "--ticket", "134"])
    assert " · running · " in _view(capsys, "134")[1].splitlines()[0]


@pytest.mark.parametrize("arg", ["abc", "1.2.3", "", "-1", "1."])
def test_bad_argument_is_a_refusal(repo, capsys, arg):
    code, out, err = _view(capsys, arg)
    assert code == 2 and out == ""
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")


def test_view_writes_nothing(repo, capsys):
    _state(repo / "contest-out" / "07", ["a"])
    before = sorted(p for p in repo.rglob("*"))
    _view(capsys, "7")
    assert sorted(p for p in repo.rglob("*")) == before


def test_real_repo_root_is_the_checkout():
    import importlib
    fresh = importlib.reload(cli)
    assert (fresh.REPO_ROOT / "tools" / "arena" / "rounds.py").is_file()
