"""Review of cc-epic-docs: a BOM in truth.csv or contest.local.ini is read, an ini count of 0 is refused, a run with no vote at all exits 1, claim_extract keeps short sections."""

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claim_extract as ce  # noqa: E402
import claim_vote as cv  # noqa: E402
import harvest_report as hr  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402
import truth_consensus as tc  # noqa: E402
from tools.claimcheck import truth  # noqa: E402
from tools.contest import roster  # noqa: E402

BOM = "﻿"
CSV = "finding,truth,checked_by,duplicate_of,how\nF1,REAL,a,,x\nF2,FALSE,b,,y\n"


@pytest.fixture
def bom_csv(tmp_path):
    p = tmp_path / "truth.csv"
    p.write_text(BOM + CSV, encoding="utf-8")
    return p


def test_every_reader_of_a_bom_truth_csv_keeps_every_row(bom_csv):
    assert [r["finding"] for r in truth.read_truth(bom_csv)] == ["F1", "F2"]
    assert hr.load_truth(str(bom_csv)) == {"F1": "REAL", "F2": "FALSE"}     # was {'': 'FALSE'}
    assert [r["finding"] for r in tc.load([str(bom_csv)])] == ["F1", "F2"]


def test_append_to_a_bom_truth_csv_adds_nothing_twice(bom_csv):
    again = [{"finding": "F1", "truth": "REAL", "checked_by": "a", "duplicate_of": "", "how": "x"}]
    added, conflicts = truth.write_truth(bom_csv, again, append=True)
    assert added == [] and conflicts == []
    new = [{"finding": "F3", "truth": "REAL", "checked_by": "c", "duplicate_of": "", "how": "z"}]
    truth.write_truth(bom_csv, new, append=True)
    text = bom_csv.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "finding,truth,checked_by,duplicate_of,how"
    assert [r["finding"] for r in truth.read_truth(bom_csv)] == ["F1", "F2", "F3"]


def test_a_bom_in_contest_local_ini_is_read(tmp_path, monkeypatch):
    shutil.copy(ROOT / "contest.ini", tmp_path / "contest.ini")
    for name in set(re.findall(r"\$\{(\w+)\}", (ROOT / "contest.ini").read_text())):
        monkeypatch.setenv(name, "x")                     # the ini's ${VAR} references, not under test
    (tmp_path / roster.LOCAL_FILENAME).write_text(BOM + "[claim_vote]\nbatch = 3\n", encoding="utf-8")
    roster.load_roster(tmp_path / "contest.ini")          # was RosterError: no section headers
    assert cv.read_ini(tmp_path).get("claim_vote", "batch") == "3"


@pytest.mark.parametrize("line,word", [("batch = 0", "batch = 0"), ("per_provider = 0", "per_provider = 0"),
                                       ("interval_sec = -1", "interval_sec = -1")])
def test_an_ini_count_out_of_range_is_refused(tmp_path, capsys, line, word):
    (tmp_path / "contest.ini").write_text(f"[claim_vote]\n{line}\n")
    (tmp_path / "c.json").write_text(json.dumps(["c"]))
    with pytest.raises(SystemExit) as exc:
        cv.main([str(tmp_path / "c.json"), "--models", "a/x", "--repo-root", str(tmp_path)])
    assert exc.value.code == 2 and word in capsys.readouterr().err


def test_a_run_where_no_voter_returned_a_vote_exits_1(tmp_path, monkeypatch, capsys):
    (tmp_path / "c.json").write_text(json.dumps(["`f` returns 1"]))
    monkeypatch.setattr(cv, "ask", lambda ref, claims, run, *a, **k:
                        {"model": ref, "run": run, "votes": {}, "error": "connection refused"})
    out = tmp_path / "votes.json"
    assert cv.main([str(tmp_path / "c.json"), "--models", "a/x", "b/y", "c/z", "--runs", "1",
                    "--interval", "0", "--repo-root", str(tmp_path), "--out", str(out)]) == 1
    assert out.exists() and "no voter returned a vote" in capsys.readouterr().err
    monkeypatch.setattr(cv, "ask", lambda ref, claims, run, *a, **k:
                        {"model": ref, "run": run, "votes": {0: "TRUE"}} if ref == "a/x" else
                        {"model": ref, "run": run, "votes": {}, "error": "connection refused"})
    assert cv.main([str(tmp_path / "c.json"), "--models", "a/x", "b/y", "c/z", "--runs", "1",
                    "--interval", "0", "--repo-root", str(tmp_path)]) == 0


SHORT = "# Review\n\n## Bug 1\n" + "The parser loses a vote when the reply ends early. " * 4 + \
        "\n\n## Lock\n`_Lock` never times out.\n\n## Bug 2\n" + "Two closes delete the shared tree. " * 5 + "\n"


def test_claim_extract_keeps_a_short_section_lenz_filter_still_cuts_it(tmp_path, capsys):
    (tmp_path / "r.md").write_text(SHORT)
    assert ce.main([str(tmp_path / "r.md"), "--dry-run"]) == 0
    assert capsys.readouterr().out.splitlines() == ["- ## Bug 1", "- ## Lock", "- ## Bug 2"]
    assert [s.splitlines()[0] for s in lf.split_sections(SHORT)] == ["## Bug 1", "## Bug 2"]


def test_a_tiny_report_is_one_section_for_claim_extract(tmp_path, capsys):
    (tmp_path / "r.md").write_text("`Target.close` removes a tree another run still uses.\n")
    assert ce.main([str(tmp_path / "r.md"), "--dry-run"]) == 0
    assert capsys.readouterr().out.startswith("- `Target.close`")


def test_a_heading_inside_a_code_fence_is_not_a_section():
    body = "The wrapper prints this and exits 0 although the vote failed, which hides it. "
    text = (f"## Bug 1\n{body}\n```\n$ run\n## done: votes.json\n```\n{body}\n\n## Bug 2\n{body}{body}\n")
    parts = lf.split_sections(text)
    assert [p.splitlines()[0] for p in parts] == ["## Bug 1", "## Bug 2"] and "## done" in parts[0]
