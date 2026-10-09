"""tests/test_arena_run_list.py — AR-3: `arena run list` over the roster's out_dir."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tools.arena import cli, rounds

INI = """[contest]
out_dir = {out}

[contest.agent.agent-a]
model = test/agent-a
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _state(folder: Path, states: list[str], mtime: float, **extra) -> None:
    path = folder / "state.json"
    _write(path, json.dumps({"agents": [{"state": s} for s in states], **extra}))
    os.utime(path, (mtime, mtime))


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    _write(repo / "contest.ini", INI.format(out="contest-out"))
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def _fill(out: Path) -> None:
    _state(out / "05", ["READY", "READY", "GAVE_UP"], 1000)
    _state(out / "06.1", ["STALLED"], 3000, legs=2)
    _state(out / "06.2", ["READY"], 4000, legs=2)
    _write(out / "08" / "state.json", "{not json")
    os.utime(out / "08" / "state.json", (2000, 2000))
    _write(out / "probe-memory.json", "{}")
    (out / "notes").mkdir()


def test_rows_collapse_legs_and_tolerate_a_broken_state(repo, capsys):
    _fill(repo / "contest-out")
    assert cli.main(["run", "list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["RUN", "LEGS", "STATE", "AGE", "READY/TOTAL"]
    rows = [line.split() for line in lines[1:]]
    assert [r[0] for r in rows] == ["6", "8", "5"]
    assert rows[0][1] == "2/2" and rows[0][2] == "done" and rows[0][-1] == "1/1"
    assert rows[1][1] == "?"          # no LEGS, STATE ?, no READY/TOTAL
    assert rows[2][-1] == "2/3" and rows[2][1] == "done"


def test_json_output_names_the_legs(repo, capsys):
    _fill(repo / "contest-out")
    assert cli.main(["-o", "json", "run", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    six = next(r for r in rows if r["RUN"] == 6)
    assert six["LEGS"] == "2/2"
    eight = next(r for r in rows if r["RUN"] == 8)
    assert eight["STATE"] == "?" and eight["READY/TOTAL"] == "" and eight["LEGS"] == ""


def test_no_round_folder_exits_3_with_one_line(repo, capsys):
    assert cli.main(["run", "list"]) == 3
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and err.startswith("arena: ")


def test_the_rosters_out_dir_is_listed(repo, capsys):
    _write(repo / "contest.ini", INI.format(out="elsewhere"))
    _state(repo / "elsewhere" / "09", ["READY"], 1000)
    _state(repo / "contest-out" / "03", ["READY"], 1000)
    assert cli.main(["run", "list"]) == 0
    rows = [line.split()[0] for line in capsys.readouterr().out.splitlines()[1:]]
    assert rows == ["9"]


def test_age_is_compact():
    assert [rounds._age(s) for s in (45, 720, 3 * 3600, 2 * 86400)] == ["45s", "12m", "3h", "2d"]
