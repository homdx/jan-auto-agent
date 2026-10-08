"""tests_bugfix/test_claim_vote_unresolved_voter_203.py — bug 21: a voter that cannot be resolved aborts the whole pool.

Bug: `claim_vote.ask` called `voter_settings(ref, parser)` outside any `try`,
and `main` runs it inside a thread pool. A misspelt profile name raises
`ValueError: [claim_vote] _pick = 'p_typo' but the config has no [p_typo]
section`, and a `provider/model` whose Kilo files are missing raises
`FileNotFoundError` — both out of the pool, so one bad voter threw away the
completed work of the other voters. The docstring and the runbook say "a dead
model is a result, not a crash".

Fix: catch `(ValueError, OSError, KeyError)` around the resolution and return
the usual result row with `error` set and no votes; `main` then prints one line
per such voter like any other dead model.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402

CLAIMS = ["A plain world fact."]


def _parser(tmp_path):
    parser = cv.read_ini(tmp_path)
    parser.add_section("good")
    parser.set("good", "base_url", "https://ok.example/v1")
    parser.set("good", "api_key", "key-good")
    parser.set("good", "model", "model-good")
    return parser


def _no_kilo_files(ref):
    raise FileNotFoundError(f"No such file or directory: {ref}")


@pytest.fixture
def stubbed(monkeypatch):
    cv.PACER.__init__(2, 0.0)          # no pacing, no sleeps
    monkeypatch.setattr(cv, "_kilo_settings", _no_kilo_files)
    monkeypatch.setattr(cv, "request_completion", lambda *a, **k: '[{"id":1,"verdict":"TRUE"}]')


def test_a_misspelt_profile_is_a_row_with_no_votes(tmp_path, stubbed):
    row = cv.ask("p_typo", CLAIMS, 0, 1, _parser(tmp_path), 5)
    assert row["model"] == "p_typo" and row["run"] == 0 and row["votes"] == {}
    assert "p_typo" in row["error"]


def test_a_provider_model_whose_kilo_files_are_missing_is_a_row_with_no_votes(tmp_path, stubbed):
    row = cv.ask("nope/model", CLAIMS, 0, 1, _parser(tmp_path), 5)
    assert row["votes"] == {} and "nope/model" in row["error"]


def test_the_other_voters_still_vote(tmp_path, stubbed):
    parser = _parser(tmp_path)
    rows = [cv.ask(ref, CLAIMS, 0, 1, parser, 5) for ref in ("p_typo", "nope/model", "good")]
    assert [r["votes"] for r in rows] == [{}, {}, {0: "TRUE"}]
    assert [r["model"] for r in rows] == ["p_typo", "nope/model", "good"]
    assert "error" not in rows[2]


def test_main_prints_a_line_per_unresolvable_voter(tmp_path, stubbed, capsys):
    root = tmp_path
    (root / "claims.json").write_text(json.dumps(CLAIMS))
    (root / "contest.local.ini").write_text(
        "[claim_vote]\nllm_profiles = good\n\n[good]\nbase_url = https://ok.example/v1\n"
        "api_key = key-good\nmodel = model-good\n", encoding="utf-8")

    rc = cv.main([str(root / "claims.json"), "--profiles", "p_typo", "nope/model", "good",
                  "--runs", "1", "--parallel", "2", "--per-provider", "2", "--interval", "0",
                  "--repo-root", str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert sum(1 for line in out.splitlines() if "ERROR" in line) == 2
    assert "votes=0/1" in out and "votes=1/1" in out
