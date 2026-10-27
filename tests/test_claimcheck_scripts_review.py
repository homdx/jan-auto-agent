"""tests/test_claimcheck_scripts_review.py — the stage-3 and Lenz scripts after the review of 2026-10-12 (n6.txt):
truth_consensus credits only the adjudicators who said the verdict and refuses a pattern that matches nothing;
lenz_claim_filter turns an HTTP or network failure into one line (a /verify keeps its paid task id) and does not
cut a sentence at "e.g."; append_verdict counts CSV rows with a closed file.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import lenz_claim_filter as lf  # noqa: E402

CONSENSUS = ROOT / "scripts" / "truth_consensus.py"


def _verdicts(folder: Path, name: str, rows: list) -> None:
    with (folder / f"truth-{name}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["finding", "truth", "how"])
        w.writeheader()
        for finding, truth, how in rows:
            w.writerow({"finding": finding, "truth": truth, "how": how})


def _consensus(tmp_path, *extra):
    return subprocess.run([sys.executable, str(CONSENSUS), str(tmp_path / "truth-*.csv"), "--truth",
                           str(tmp_path / "truth.csv"), "--report", str(tmp_path / "adj.md"), *extra],
                          capture_output=True, text=True)


# ── №46: "agreed by" and checked_by name only who said the verdict ───────────────────────────

def test_an_undecided_adjudicator_is_not_credited_with_the_verdict(tmp_path):
    _verdicts(tmp_path, "alice", [("a.py::f", "REAL", "read the code: the file stays open")])
    _verdicts(tmp_path, "bob", [("a.py::f", "UNDECIDED", "a much longer note that says nothing was decided here")])
    _verdicts(tmp_path, "carol", [("a.py::f", "REAL", "ran it")])
    r = _consensus(tmp_path)
    assert r.returncode == 0, r.stderr
    report = (tmp_path / "adj.md").read_text(encoding="utf-8")
    assert "- agreed by: alice, carol" in report and "nothing was decided" not in report
    [row] = list(csv.DictReader(io.StringIO((tmp_path / "truth.csv").read_text(encoding="utf-8"))))
    assert row["checked_by"] == "alice+carol"


def test_an_arbitrated_verdict_does_not_list_the_dissenters(tmp_path):
    _verdicts(tmp_path, "renat", [("a.py::f", "REAL", "read it")])
    _verdicts(tmp_path, "bob", [("a.py::f", "FALSE", "a long and confident wrong reason " * 3)])
    r = _consensus(tmp_path, "--arbiter", "renat")
    assert r.returncode == 0, r.stderr
    [row] = list(csv.DictReader(io.StringIO((tmp_path / "truth.csv").read_text(encoding="utf-8"))))
    assert (row["truth"], row["checked_by"], row["how"]) == ("REAL", "renat", "read it")


# ── №47: a pattern that matches nothing ──────────────────────────────────────────────────────

def test_a_pattern_that_matches_nothing_is_one_line(tmp_path):
    r = _consensus(tmp_path)
    assert r.returncode == 2 and "no file matches" in r.stderr and "Traceback" not in r.stderr


# ── №48, №49: Lenz failures are one line; a /verify keeps its task id ────────────────────────

def test_an_http_error_is_a_lenz_error_not_an_exit(monkeypatch):
    def refuse(*a, **k):
        raise urllib.error.HTTPError("https://x/assess", 429, "Too Many", {}, io.BytesIO(b"slow down"))
    monkeypatch.setattr(lf.urllib.request, "urlopen", refuse)
    with pytest.raises(lf.LenzError, match="HTTP 429"):
        lf.call("POST", "/assess", "k", {"claims": ["x"]})


@pytest.mark.parametrize("exc", [urllib.error.URLError("dns failure"), TimeoutError("timed out"),
                                 ConnectionResetError("reset")])
def test_a_network_failure_is_a_lenz_error_not_a_traceback(monkeypatch, exc):
    def fail(*a, **k):
        raise exc
    monkeypatch.setattr(lf.urllib.request, "urlopen", fail)
    with pytest.raises(lf.LenzError):
        lf.call("GET", "/verify/status/1", "k")


def test_a_failed_poll_keeps_the_paid_verify_task_id(monkeypatch, tmp_path):
    calls = []

    def fake_call(method, path, key, body=None, timeout=120):
        calls.append(path)
        if path == "/verify":
            return {"task_id": "t-42"}
        raise lf.LenzError("Lenz GET /verify/status/t-42: HTTP 502")

    monkeypatch.setattr(lf, "call", fake_call)
    monkeypatch.setattr(lf.time, "sleep", lambda s: None)
    got = lf.verify("Python's sorted() is stable.", "k", lf.Cache(tmp_path / "c.json"))
    assert got["status"] == "error" and got["task_id"] == "t-42" and "HTTP 502" in got["error"]


def test_main_reports_an_assess_failure_in_one_line(monkeypatch, tmp_path, capsys):
    report = tmp_path / "r.md"
    report.write_text("## Facts\n\n" + "POSIX requires that rename() replaces an existing target atomically on "
                      "one filesystem, which pytest relies on.\n" * 3, encoding="utf-8")
    monkeypatch.setenv(lf.ENV_KEY, "k")

    def fake_call(method, path, key, body=None, timeout=120):
        if path == "/extract":
            # /extract's live shape: the claim and identified_claims
            return {"claim": None, "identified_claims": [
                {"claim": "POSIX requires that rename() replaces an existing target atomically."}]}
        raise lf.LenzError("Lenz POST /assess: URLError: dns failure")

    monkeypatch.setattr(lf, "call", fake_call)
    rc = lf.main([str(report), "--cache", str(tmp_path / "c.json"), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1 and "dns failure" in err and "a re-run does not send them again" in err


# ── №50: an abbreviation is no sentence end ──────────────────────────────────────────────────

def test_e_g_does_not_cut_a_sentence():
    got = lf.split_sentences("e.g. Python is widely used in data science for its readable syntax. "
                             "It is also popular in web work today.")
    assert got == ["e.g. Python is widely used in data science for its readable syntax.",
                   "It is also popular in web work today."]


def test_a_real_sentence_end_still_cuts():
    assert len(lf.split_sentences("POSIX says rename() is atomic on one filesystem. "
                                  "Git status lists untracked files under their own heading.")) == 2


# ── №51: append_verdict counts CSV rows ──────────────────────────────────────────────────────

def test_append_verdict_counts_rows_not_lines(tmp_path):
    out = tmp_path / "truth.csv"
    import append_verdict as av
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=av.COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerow({"finding": "a.py::f", "truth": "REAL", "checked_by": "me", "how": "line one\nline two"})
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "append_verdict.py"), "--out", str(out),
                        "--finding", "b.py::g", "--truth", "FALSE", "--checked-by", "me", "--how", "read it"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "decided #2:" in r.stdout
