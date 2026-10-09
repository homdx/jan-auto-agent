"""tests_bugfix/test_claim_vote_quorum_warning_210.py — bug 46: fewer voters than the quorum says so on stderr and still runs.

With fewer than `MIN_COMMITTED` voters no claim can be accepted, and the run
printed a table of UNSURE with no word why. Ticket 210: one stderr line before
the run — "N voters, quorum is 3: no claim can be accepted" — and the run goes
on with the exit code unchanged; three voters print nothing of the kind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


def _run(tmp_path, monkeypatch, capsys, profiles):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps(["the sky is blue"]), encoding="utf-8")
    asked = []

    def fake_ask(ref, texts, run, *a, **k):
        asked.append(ref)
        return {"model": ref, "run": run, "votes": {0: "TRUE"}}

    monkeypatch.setattr(cv, "ask", fake_ask)
    monkeypatch.setattr(cv.lf, "_repo_symbols", lambda root: set())
    code = cv.main([str(claims), "--profiles", *profiles, "--runs", "1",
                    "--repo-root", str(tmp_path)])
    cap = capsys.readouterr()
    return code, cap.err, asked


def test_two_voters_print_the_quorum_line_once_and_still_run(tmp_path, monkeypatch, capsys):
    code, err, asked = _run(tmp_path, monkeypatch, capsys, ["a", "b"])
    line = "claim_vote: 2 voters, quorum is 3: no claim can be accepted"
    assert code == 0 and err.count(line) == 1
    assert sorted(asked) == ["a", "b"]


def test_three_voters_print_nothing(tmp_path, monkeypatch, capsys):
    code, err, asked = _run(tmp_path, monkeypatch, capsys, ["a", "b", "c"])
    assert code == 0 and "quorum" not in err
    assert sorted(asked) == ["a", "b", "c"]


def test_one_voter_says_so_too(tmp_path, monkeypatch, capsys):
    code, err, asked = _run(tmp_path, monkeypatch, capsys, ["a"])
    assert code == 0 and err.count("claim_vote: 1 voters, quorum is 3") == 1
    assert asked == ["a"]
