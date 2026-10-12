"""tests/test_claimcheck_truth_review.py — the truth step and the Lenz hint after the review of 2026-10-12:
the voters' models recorded in votes.json and read wherever the script runs, the Lenz hint counting
families as pending.md does, a BOM in contest.local.ini, claim_diff's side files in claim_vote's shape,
a stale lenz-claims.md, a claim that starts with a fence or a heading, and a hand-edited votes.json.

The votes files come from the real `claim_vote.tally`; the script is the real `claimcheck_truth.main`.
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.claimcheck import lenz_hint as lh
from tools.claimcheck.pack import PackBudget

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_diff as cd  # noqa: E402
import claim_vote as cv  # noqa: E402
import claimcheck_truth as ct  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402

WORLD = ("pytest runs the tests of one module in the order they are written in the file, unless a plugin "
         "such as pytest-randomly reorders them.")
AGNES = {"vote_a": "kenary/agnes-3-0-flash:free", "vote_b": "kenary/agnes-3-5-flash:free",
         "vote_c": "kenary/agnes-2-5-pro:free"}
VENDORS = {"vote_a": "kenary/agnes-3-0-flash:free", "vote_b": "openai/gpt-5-mini", "vote_c": "meta/llama-4-scout"}
SHA = "a" * 40


def _world_votes(models, *, record=True, verdict="TRUE") -> dict:
    """votes.json of one world claim, every voter `verdict`, tallied by the real tally."""
    rows = [{"claim": WORLD, "truth": None}]
    results = [{"model": ref, "run": 0, "votes": {0: verdict}, "accepted": {}, "rejected": []} for ref in models]
    table = cv.tally(rows, results, meta=[{"kind": "world", "dangling": False, "sha": SHA}])
    out = {"target": {"repo": "/nonexistent", "ref": SHA, "sha": SHA, "base": None, "warnings": []},
           "results": results, "claims": table}
    if record:
        out["models"] = dict(models)
    return json.loads(json.dumps(out))


def _write(path: Path, data) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _main(tmp_path, votes_file, *extra, repo_root=None):
    out = tmp_path / "out" / "truth.csv"
    rc = ct.main([str(votes_file), "--out", str(out), "--no-rebuild",
                  "--repo-root", str(repo_root or tmp_path), *map(str, extra)])
    return rc, out, out.with_name("pending.md"), out.with_name("lenz-claims.md")


# ── №13: the Lenz hint counts the families as pending.md does ────────────────────────────────

def test_three_sections_of_one_family_are_offered_to_lenz_like_pending_lists_them(tmp_path):
    vf = _write(tmp_path / "v.json", _world_votes(AGNES))
    rc, out, pending, claims_file = _main(tmp_path, vf)
    assert rc == 0
    assert out.read_text(encoding="utf-8").count("\n") == 1               # no row: one family
    text = pending.read_text(encoding="utf-8")
    assert "Nothing worth sending to Lenz" not in text
    assert "unanimous, too few families" in text
    assert claims_file.is_file() and WORLD in claims_file.read_text(encoding="utf-8")


def test_three_families_settle_and_nothing_goes_to_lenz(tmp_path):
    vf = _write(tmp_path / "v.json", _world_votes(VENDORS))
    rc, out, pending, claims_file = _main(tmp_path, vf)
    assert rc == 0 and out.read_text(encoding="utf-8").count("\n") == 2   # the header and one row
    assert "Nothing worth sending to Lenz" in pending.read_text(encoding="utf-8")
    assert not claims_file.exists()


def test_lenz_group_reads_the_model_map():
    row = _world_votes(AGNES)["claims"][0]
    assert lh.lenz_group(row) is None                     # without the map: three "families"
    assert lh.lenz_group(row, AGNES) == "DISSENT"
    assert lh.pick_for_lenz([row], models=AGNES) == [row]


# ── №14: the models recorded in votes.json, wherever the script runs ─────────────────────────

def test_claim_vote_records_each_voters_model(tmp_path, monkeypatch):
    claims = tmp_path / "claims.json"
    claims.write_text(json.dumps([WORLD]), encoding="utf-8")
    monkeypatch.setattr(cv, "ask", lambda ref, texts, run, *a, **k: {"model": ref, "run": run, "votes": {0: "TRUE"}})
    monkeypatch.setattr(cv, "voter_settings",
                        lambda ref, parser: SimpleNamespace(model={"a": "kenary/agnes-1", "b": "openai/gpt-5"}[ref]))
    out = tmp_path / "votes.json"
    assert cv.main([str(claims), "--profiles", "a", "b", "dead", "--out", str(out), "--repo-root", str(tmp_path)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["models"] == {"a": "kenary/agnes-1", "b": "openai/gpt-5"}


def test_the_same_votes_file_gives_the_same_rows_from_any_directory(tmp_path):
    """It read contest.ini of the current directory: no row from the project, a REAL row elsewhere."""
    vf = _write(tmp_path / "v.json", _world_votes(AGNES))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    runs = []
    for cwd in (ROOT, elsewhere):
        out = tmp_path / f"out-{len(runs)}" / "truth.csv"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "claimcheck_truth.py"), str(vf), "--out", str(out),
                            "--no-rebuild", "--no-lenz-hint"], cwd=cwd, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        runs.append(out.read_text(encoding="utf-8"))
    assert runs[0] == runs[1] and runs[0].count("\n") == 1


def test_a_section_voter_with_no_known_model_is_named(tmp_path, capsys):
    vf = _write(tmp_path / "v.json", _world_votes(AGNES, record=False))
    rc, out, _, _ = _main(tmp_path, vf)
    assert rc == 0
    assert "no model known for vote_a, vote_b, vote_c" in capsys.readouterr().err


def test_contest_local_ini_with_a_bom_is_read(tmp_path):
    (tmp_path / "contest.local.ini").write_text(
        "\ufeff" + "".join(f"[{ref}]\nmodel = {m}\n" for ref, m in AGNES.items()), encoding="utf-8")
    vf = _write(tmp_path / "v.json", _world_votes(AGNES, record=False))
    rc, out, pending, _ = _main(tmp_path, vf)
    assert rc == 0 and out.read_text(encoding="utf-8").count("\n") == 1    # read: one family, no row
    assert "agnes" in pending.read_text(encoding="utf-8")


# ── №16: claim_diff's side files are claim_vote's shape ──────────────────────────────────────

def test_claim_diffs_side_file_carries_base_sha_collect_budget_and_models(tmp_path, capsys):
    target = SimpleNamespace(repo=str(tmp_path / "gone"), ref="HEAD", sha=SHA, warnings=[])
    side = {"results": _world_votes(VENDORS)["results"], "table": _world_votes(VENDORS)["claims"]}
    budget = PackBudget(chars=9000, chunks=5, per_chunk=1500)
    rep = cd.side_report(target, side, "b" * 40, "all", budget=budget, collect=True, models=VENDORS)
    assert rep["target"]["base_sha"] == rep["target"]["base"] == "b" * 40
    assert rep["target"]["collect"] is True
    assert rep["target"]["budget"] == {"chars": 9000, "chunks": 5, "per_chunk": 1500}
    assert rep["models"] == VENDORS
    vf = _write(tmp_path / "votes_head.json", json.loads(json.dumps(rep)))
    rc, out, _, _ = _main(tmp_path, vf)
    assert rc == 0 and "as a name, not a sha" not in capsys.readouterr().err
    assert out.read_text(encoding="utf-8").count("\n") == 2


def test_side_report_without_the_new_arguments_is_as_before_plus_base_sha(tmp_path):
    target = SimpleNamespace(repo="r", ref="HEAD", sha=SHA, warnings=[])
    rep = cd.side_report(target, {"results": [], "table": []}, None)
    assert set(rep) == {"target", "accept", "results", "claims"}
    assert rep["target"]["base_sha"] is None and "budget" not in rep["target"]


# ── №17: a stale lenz-claims.md ──────────────────────────────────────────────────────────────

def test_a_rerun_that_picks_nothing_removes_the_old_claims_file(tmp_path):
    first = _write(tmp_path / "v1.json", _world_votes(AGNES))
    rc, _, _, claims_file = _main(tmp_path, first)
    assert rc == 0 and claims_file.is_file()
    second = _write(tmp_path / "v2.json", _world_votes(VENDORS))
    rc, _, pending, claims_file = _main(tmp_path, second)
    assert rc == 0 and not claims_file.exists()
    assert "Nothing worth sending to Lenz" in pending.read_text(encoding="utf-8")


def test_no_lenz_hint_leaves_the_claims_file_alone(tmp_path):
    rc, _, _, claims_file = _main(tmp_path, _write(tmp_path / "v1.json", _world_votes(AGNES)))
    assert claims_file.is_file()
    rc, _, _, claims_file = _main(tmp_path, _write(tmp_path / "v2.json", _world_votes(VENDORS)), "--no-lenz-hint")
    assert rc == 0 and claims_file.is_file()


# ── №18: a claim that starts with a fence or a heading ───────────────────────────────────────

def test_a_claim_that_starts_with_a_fence_or_a_heading_is_one_section_of_the_claims_file():
    claims = [{"claim": "```python x = sorted(y)``` keeps equal keys in input order in the output list, "
                        "as the pytest docs and the language reference both say."},
              {"claim": "## POSIX requires that rename() replaces an existing target file atomically on "
                        "one filesystem, whatever pytest does."},
              {"claim": WORLD}]
    text = lh.claims_file_text(claims, hints=None)
    sections = lf.split_sections(text)
    assert len(sections) == 3
    assert all(s.startswith("### ") for s in sections)


# ── №19: a hand-edited votes.json is a refusal, not a traceback ──────────────────────────────

@pytest.mark.parametrize("edit, why", [
    (lambda v: v["claims"][0].__setitem__("evidence", [1, 2]), "`evidence` is not a list of strings"),
    (lambda v: v["claims"][0].__setitem__("quotes", [3]), "`quotes` is not a list of strings"),
    (lambda v: v["claims"][0].__setitem__("id", [7]), "`id` is not a string or a number"),
    (lambda v: v["claims"][0]["by_model"].__setitem__("vote_a", 1), "a `by_model` verdict is not a string"),
    (lambda v: v["target"].__setitem__("sha", 12345), "`target.sha` is not a string"),
    (lambda v: v.__setitem__("models", ["vote_a"]), "`models` is not an object"),
])
def test_a_malformed_votes_file_is_refused_with_a_reason(tmp_path, capsys, edit, why):
    votes = _world_votes(VENDORS)
    edit(votes)
    rc, *_ = _main(tmp_path, _write(tmp_path / "v.json", votes))
    err = capsys.readouterr().err
    assert rc == 2 and why in err and "Traceback" not in err


def test_an_integer_claim_id_is_still_read(tmp_path):
    votes = _world_votes(VENDORS)
    votes["claims"][0]["id"] = 7
    rc, out, _, _ = _main(tmp_path, _write(tmp_path / "v.json", votes))
    assert rc == 0 and out.read_text(encoding="utf-8").count("\n") == 2
