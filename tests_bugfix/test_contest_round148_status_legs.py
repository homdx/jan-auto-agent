"""Round 148: `status --ticket NN` reads the last leg of a round of legs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.contest import cli


def _state(folder: Path, name: str) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps({
        "round_no": 148, "ticket": "t", "base_sha": "abcdef0123456789", "started_at": 1.0,
        "agents": [{
            "agent": {"name": name, "provider_id": "p", "model_id": "m"},
            "workspace": {"agent": name, "path": str(folder / name), "branch": f"contest/148/{name}",
                          "base_sha": "abcdef0123456789", "kind": "worktree"},
            "state": "WAITING", "attempt": 1,
        }]}), encoding="utf-8")


def _status(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / "contest.ini").write_text(
        "[contest]\nout_dir = contest-out\n\n[contest.agent.a]\nmodel = test/a\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(ticket=148, out=None, roster=cli.DEFAULT_ROSTER)
    code = cli.cmd_status(args)
    return code, capsys.readouterr()


def test_bare_round_number_reads_the_highest_leg(tmp_path, monkeypatch, capsys):
    _state(tmp_path / "contest-out" / "148.1", "early-leg")
    _state(tmp_path / "contest-out" / "148.2", "last-leg")
    code, cap = _status(tmp_path, monkeypatch, capsys)
    assert code == 0
    assert "last-leg" in cap.out and "early-leg" not in cap.out
    assert "148.2" in cap.err


def test_no_round_at_all_is_still_exit_1(tmp_path, monkeypatch, capsys):
    code, cap = _status(tmp_path, monkeypatch, capsys)
    assert code == 1
    assert "nothing to report" in cap.err
