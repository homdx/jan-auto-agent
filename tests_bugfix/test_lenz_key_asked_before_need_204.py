"""Bug 28 (round 204): the scripts asked for LENZ_API_KEY before anything had to be sent.

claim_extract and lenz_claim_filter read the key first and exited with no
console, even when every section was cached; and --json wrote no file when
nothing was left to send.  The key is asked for only for a non-empty send and
the output file is always written.
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_extract as ce  # noqa: E402
import lenz_claim_filter as lf  # noqa: E402

SECTION = "## Section\n\n" + "words " * 40


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.delenv(lf.ENV_KEY, raising=False)
    monkeypatch.setattr(lf, "call", lambda *a, **k: pytest.fail("a request went to the network"))
    monkeypatch.setattr(lf, "api_key", lambda: pytest.fail("the key was asked for"))
    monkeypatch.setattr(lf, "_repo_symbols", lambda root: set())


def _report(tmp_path):
    report = tmp_path / "r.md"
    report.write_text(SECTION + "\n" + SECTION.replace("Section", "Other"), encoding="utf-8")
    return report


def _cache(tmp_path, report, claims):
    cache = tmp_path / "c.json"
    c = lf.Cache(cache)
    for sec in lf.split_sections(report.read_text(encoding="utf-8")):
        c.put("extract", sec, claims)
    return cache


def test_extract_with_every_section_cached_needs_no_key(tmp_path, offline):
    report = _report(tmp_path)
    cache = _cache(tmp_path, report, ["A cached fact about pytest exit codes."])
    out = tmp_path / "claims.json"
    assert ce.main([str(report), "--cache", str(cache), "--out", str(out)]) == 0
    assert [c["claim"] for c in json.loads(out.read_text())] == [
        "A cached fact about pytest exit codes."]


def test_filter_fully_cached_needs_no_key(tmp_path, offline):
    report = _report(tmp_path)
    claim = "Pytest exits with code 4 on a usage error."
    cache = _cache(tmp_path, report, [claim])
    lf.Cache(cache).put("assess", claim, {"claim": claim, "verdict": "True"})
    out = tmp_path / "out.json"
    assert lf.main([str(report), "--cache", str(cache), "--json", str(out)]) == 0
    assert json.loads(out.read_text())["rows"][0]["cached"] is True


def test_filter_with_nothing_to_send_writes_an_empty_list(tmp_path, offline):
    report = _report(tmp_path)
    cache = _cache(tmp_path, report, ["See tools/contest/cli.py for pytest flags."])
    out = tmp_path / "out.json"
    out.write_text('{"stale": true}', encoding="utf-8")
    assert lf.main([str(report), "--cache", str(cache), "--json", str(out)]) == 0
    assert json.loads(out.read_text()) == []


def test_an_uncached_section_still_asks_for_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(lf, "api_key", lambda: "k")
    sent = []
    monkeypatch.setattr(lf, "call", lambda m, path, key, body=None, **k:
                        sent.append(key) or {"claim": "A fresh fact.", "identified_claims": []})
    report = _report(tmp_path)
    out = tmp_path / "claims.json"
    assert ce.main([str(report), "--cache", str(tmp_path / "c.json"), "--out", str(out)]) == 0
    assert sent == ["k", "k"]
