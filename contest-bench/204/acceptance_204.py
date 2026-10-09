"""Round 204 black-box bench: scripts/lenz_claim_filter.py + claim_extract.py, written from the ticket only.

Run from a checkout root:  python3 -m pytest contest-bench/204/acceptance_204.py -q -p no:cacheprovider -n0
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT / "scripts"))
import lenz_claim_filter as lf  # noqa: E402
import claim_extract as ce  # noqa: E402


def _git_repo(tmp_path, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "a@b"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "a"], cwd=tmp_path, check=True)
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=tmp_path, check=True)
    return tmp_path


def _pick(claims, limit=5, symbols=frozenset()):
    try:
        return lf.pick_extracted(claims, limit, set(symbols))
    except TypeError:
        return lf.pick_extracted(claims, limit)


# ---- 23
def test_23_internal_path_claim_dropped():
    a = "The function tools/contest/gates.py judge_worktree returns a scorecard when pytest passes."
    b = "Python raises ValueError when int() gets bad input via pytest."
    assert _pick([a, b]) == [b]


def test_23_symbol_of_repo_dropped(tmp_path):
    repo = _git_repo(tmp_path, {"m.py": "def frobnicate_widget():\n    pass\n"})
    syms = lf._repo_symbols(repo)
    assert "frobnicate_widget" in syms
    c = "Calling `frobnicate_widget` makes pytest exit with code 2 on a bad input."
    assert _pick([c], 5, syms) == []


def test_23_cli_passes_symbols(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path, {"m.py": "def frobnicate_widget():\n    pass\n"})
    rep = tmp_path / "r.md"
    rep.write_text("## A\n" + "x " * 80 + "\n## B\n" + "y " * 80 + "\n")
    cache = tmp_path / "c.json"
    claim = "Calling `frobnicate_widget` makes pytest exit with code 2 on a bad input."
    ok = "Python raises ValueError when int() gets bad input via pytest."
    sent = []
    monkeypatch.setattr(lf, "extract_claims", lambda s, k, c: [claim, ok])
    monkeypatch.setattr(lf, "assess", lambda cl, k, c: (sent.extend(cl), [{"claim": x} for x in cl])[1])
    monkeypatch.setattr(lf, "api_key", lambda: "k")
    monkeypatch.setattr(lf, "render", lambda r: "")
    lf.main([str(rep), "--cache", str(cache), "--repo-root", str(repo)])
    assert sent == [ok]


# ---- 24
@pytest.mark.parametrize("s", [
    "Use read/write access.", "Either and/or works.", "It runs over TCP/IP.",
    "Pass input/output through the client/server pair.", "Answer yes/no or true/false.",
    "Either/or and w/o a key.",
])
def test_24_slash_words_public(s):
    assert lf.is_internal(s, set()) is False


@pytest.mark.parametrize("s", [
    "See tools/contest/cli.py.", "Run scripts/x.sh first.", "Look in tests/ for it.",
    "The file tools/contest/cli.py defines it.",
])
def test_24_real_paths_internal(s):
    assert lf.is_internal(s, set()) is True


# ---- 25
@pytest.mark.parametrize("body", ["[]", "null", '"str"', "3"])
def test_25_cache_not_object(tmp_path, body):
    p = tmp_path / "c.json"
    p.write_text(body)
    c = lf.Cache(p)
    assert c.get("assess", "some claim") is None
    c.put("assess", "some claim", {"v": 1})
    assert isinstance(json.loads(p.read_text()), dict)
    assert lf.Cache(p).get("assess", "some claim") == {"v": 1}


# ---- 26
@pytest.mark.parametrize("s", [
    "404 is returned when a page is missing.",
    "3.5 seconds is the default timeout.",
    "1.2.3 is the version that shipped it.",
    "2 ** 10 equals 1024 in Python for sure.",
    "-1 is returned when the value is not found.",
    "200 is returned when the request is fine.",
])
def test_26_numbers_kept(s):
    assert lf.split_sentences(s) == [s]


@pytest.mark.parametrize("marker", ["-", "*", "+", "1.", "12)", "1.2."])
def test_26_markers_stripped_once(marker):
    body = "Python raises ValueError when int gets bad input."
    assert lf.split_sentences(f"{marker} {body}") == [body]


def test_26_marker_only_once():
    body = "Python raises ValueError when int gets bad input."
    assert lf.split_sentences(f"- 1. {body}") == [f"1. {body}"] or \
        lf.split_sentences(f"- 1. {body}") == [body]


# ---- 27
def test_27_names_with_space_and_cyrillic(tmp_path):
    repo = _git_repo(tmp_path, {
        "my module.py": "def foo_bar_baz():\n    pass\n",
        "ф.py": "def cyr_fn_name():\n    pass\n",
    })
    syms = lf._repo_symbols(repo)
    assert "foo_bar_baz" in syms and "cyr_fn_name" in syms


# ---- 28
def _no_net(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network call")
    monkeypatch.setattr(lf, "call", boom)

    def nokey(*a, **k):
        raise AssertionError("key asked")
    monkeypatch.setattr(lf, "api_key", nokey)


REPORT = "## A\n" + "Python raises ValueError when int() gets bad input via pytest. " * 4 + \
         "\n## B\n" + "Git status exits with code 128 outside a repository for sure. " * 4 + "\n"


def _prime(cache_path):
    c = lf.Cache(cache_path)
    for sec in lf.split_sections(REPORT):
        c.put("extract", sec, ["Python raises ValueError when int() gets bad input via pytest."])
    c.put("assess", "Python raises ValueError when int() gets bad input via pytest.", {"verdict": "ok"})


def test_28_extract_all_cached_needs_no_key(tmp_path, monkeypatch):
    rep = tmp_path / "r.md"; rep.write_text(REPORT)
    cache = tmp_path / "c.json"; _prime(cache)
    _no_net(monkeypatch)
    out = tmp_path / "claims.json"
    assert ce.main([str(rep), "--out", str(out), "--cache", str(cache)]) == 0
    assert json.loads(out.read_text())


def test_28_filter_all_cached_needs_no_key(tmp_path, monkeypatch):
    rep = tmp_path / "r.md"; rep.write_text(REPORT)
    cache = tmp_path / "c.json"; _prime(cache)
    _no_net(monkeypatch)
    out = tmp_path / "o.json"
    monkeypatch.setattr(lf, "render", lambda r: "")
    rc = lf.main([str(rep), "--cache", str(cache), "--json", str(out), "--repo-root", str(tmp_path)])
    assert rc == 0 and out.is_file()


def test_28_json_written_when_nothing_to_send(tmp_path, monkeypatch):
    rep = tmp_path / "r.md"; rep.write_text(REPORT)
    cache = tmp_path / "c.json"
    c = lf.Cache(cache)
    for sec in lf.split_sections(REPORT):
        c.put("extract", sec, ["See tools/contest/cli.py for it."])   # all internal
    _no_net(monkeypatch)
    out = tmp_path / "o.json"; out.write_text('{"stale": 1}')
    monkeypatch.setattr(lf, "render", lambda r: "")
    rc = lf.main([str(rep), "--cache", str(cache), "--json", str(out), "--repo-root", str(tmp_path)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert data in ([], {}) or (isinstance(data, dict) and not data.get("rows")), data


def test_28_key_still_required_when_something_to_send(tmp_path, monkeypatch):
    rep = tmp_path / "r.md"; rep.write_text(REPORT)
    monkeypatch.delenv("LENZ_API_KEY", raising=False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit):
        ce.main([str(rep), "--out", str(tmp_path / "o.json"), "--cache", str(tmp_path / "c.json")])


def test_28_uncached_sends_with_key(tmp_path, monkeypatch):
    rep = tmp_path / "r.md"; rep.write_text(REPORT)
    monkeypatch.setenv("LENZ_API_KEY", "k")
    sent = []

    def fake(m, p, k, body=None, timeout=120):
        sent.append(p)
        return {"claim": "Python raises ValueError when int() gets bad input via pytest.", "identified_claims": []}
    monkeypatch.setattr(lf, "call", fake)
    out = tmp_path / "claims.json"
    ce.main([str(rep), "--out", str(out), "--cache", str(tmp_path / "c.json")])
    assert sent and json.loads(out.read_text())
