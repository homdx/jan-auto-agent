"""tests/test_claimcheck_lenz_errors_review.py — the Lenz and stage-3 scripts after the self-review of 2026-10-12:
a failed /extract is one line in both scripts, the claims file keeps a claim's leading backtick, and truth_consensus
opens a file whose name looks like a pattern.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
import claim_extract  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402
from tools.claimcheck import lenz_hint as lh  # noqa: E402

REPORT = "## Facts\n\n" + "POSIX requires that rename() replaces an existing target atomically on one filesystem.\n" * 3


# ── №55: a failed /extract ───────────────────────────────────────────────────────────────────

def _http_429(*a, **k):
    raise urllib.error.HTTPError("https://api.lenz/extract", 429, "Too Many Requests", {}, io.BytesIO(b"slow down"))


def test_claim_extract_reports_a_failed_extract_in_one_line(tmp_path, monkeypatch, capsys):
    """Step 1 of run_claim_check.sh: an HTTP 429 or a bad key was one line until the LenzError, then a
    traceback in extract.log."""
    report = tmp_path / "r.md"
    report.write_text(REPORT, encoding="utf-8")
    monkeypatch.setenv(lf.ENV_KEY, "k")
    monkeypatch.setattr(lf.urllib.request, "urlopen", _http_429)
    rc = claim_extract.main([str(report), "--out", str(tmp_path / "claims.json"), "--cache", str(tmp_path / "c.json")])
    err = capsys.readouterr().err
    assert rc == 1 and "HTTP 429" in err and "Traceback" not in err and len(err.strip().splitlines()) == 1
    assert not (tmp_path / "claims.json").exists()


def test_lenz_claim_filter_reports_a_failed_extract_in_one_line(tmp_path, monkeypatch, capsys):
    report = tmp_path / "r.md"
    report.write_text(REPORT, encoding="utf-8")
    monkeypatch.setenv(lf.ENV_KEY, "k")
    monkeypatch.setattr(lf.urllib.request, "urlopen", _http_429)
    rc = lf.main([str(report), "--cache", str(tmp_path / "c.json"), "--repo-root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 1 and "HTTP 429" in err and "a re-run does not send them again" in err


# ── №58: the claims file keeps a leading backtick ────────────────────────────────────────────

@pytest.mark.parametrize("claim, body", [
    ("`requests` 2.32 dropped support for Python 3.7", "`requests` 2.32 dropped support for Python 3.7"),
    ("```python x = sorted(y)``` keeps equal keys in order", "python x = sorted(y)``` keeps equal keys in order"),
    ("## POSIX requires an atomic rename", "POSIX requires an atomic rename"),
    ("~~~ a tilde fence first", "a tilde fence first"),
    ("### ```py both at once", "py both at once"),
])
def test_only_a_fence_or_a_heading_comes_off_the_body(claim, body):
    heading, blank, got, *_ = lh.section_of(claim).split("\n")
    assert (heading, got) == (f"### {claim}", body)


# ── №59: a file whose name looks like a pattern ──────────────────────────────────────────────

def test_truth_consensus_opens_a_file_named_like_a_pattern(tmp_path):
    with (tmp_path / "truth[1].csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["finding", "truth", "how"])
        w.writerow(["a.py::f", "REAL", "read it"])
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "truth_consensus.py"), str(tmp_path / "truth[1].csv"),
                        "--truth", str(tmp_path / "truth.csv")], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert "no file matches" not in r.stderr
    [row] = list(csv.DictReader(io.StringIO((tmp_path / "truth.csv").read_text(encoding="utf-8"))))
    assert row["truth"] == "REAL"
