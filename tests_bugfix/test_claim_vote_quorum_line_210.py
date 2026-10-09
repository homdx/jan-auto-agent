"""tests_bugfix/test_claim_vote_quorum_line_210.py — ticket 210, bug 46.

``scripts/claim_vote.py``'s quorum is ``MIN_COMMITTED = 3``: with fewer voters
than that, no claim can ever reach it and every verdict comes back UNSURE —
but ``main`` said nothing, so a run with ``--profiles a b`` looked broken
instead of under-quorate. Fixed: one line to stderr before the run names the
voter count and the quorum; the run itself, and its exit code, are unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote  # noqa: E402


@pytest.fixture
def claims_file(tmp_path: Path) -> Path:
    path = tmp_path / "claims.json"
    path.write_text(json.dumps(["claim one"]), encoding="utf-8")
    return path


def _fake_ask(model, texts, run, seed, parser, timeout, batch, fixed_prompt):
    return {"model": model, "run": run, "votes": {}, "error": None}


def test_fewer_than_quorum_voters_prints_one_warning_line(monkeypatch, tmp_path, claims_file, capsys):
    monkeypatch.setattr(claim_vote, "ask", _fake_ask)
    code = claim_vote.main([str(claims_file), "--profiles", "a", "b", "--repo-root", str(tmp_path)])
    assert code == 0
    err = capsys.readouterr().err
    assert err.count("quorum is 3") == 1
    assert "2 voters, quorum is 3: no claim can be accepted" in err


def test_exactly_three_voters_prints_no_quorum_warning(monkeypatch, tmp_path, claims_file, capsys):
    monkeypatch.setattr(claim_vote, "ask", _fake_ask)
    code = claim_vote.main([str(claims_file), "--profiles", "a", "b", "c", "--repo-root", str(tmp_path)])
    assert code == 0
    err = capsys.readouterr().err
    assert "quorum" not in err


def test_the_run_still_happens_under_quorum(monkeypatch, tmp_path, claims_file):
    """The warning does not stop the run: votes are still asked for and tallied."""
    calls = []

    def recording_ask(model, texts, run, seed, parser, timeout, batch, fixed_prompt):
        calls.append(model)
        return {"model": model, "run": run, "votes": {}, "error": None}

    monkeypatch.setattr(claim_vote, "ask", recording_ask)
    code = claim_vote.main([str(claims_file), "--profiles", "a", "b", "--repo-root", str(tmp_path)])
    assert code == 0
    assert set(calls) == {"a", "b"}
