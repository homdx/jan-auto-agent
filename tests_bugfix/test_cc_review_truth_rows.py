"""Review of cc-epic-docs: run_claim_check.sh keeps an existing truth.csv, ends with the vote's exit code; claimcheck_truth.py refuses a wrong file."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import claimcheck_truth as ct  # noqa: E402
from tools.claimcheck import truth as cc_truth  # noqa: E402

HAND = ("finding,truth,checked_by,duplicate_of,how\n"
        "hand/one.py::f,REAL,human:op,,checked by hand\n"
        "hand/two.py::g,FALSE,human:op,,checked by hand\n")
EXTRACT = "import json, sys\njson.dump(['a claim'], open(sys.argv[sys.argv.index('--out') + 1], 'w'))\n"
VOTE = """import json, os, sys
if os.environ.get("VOTE_FAILS"):
    print("claim_vote: --target refused: no such ref", file=sys.stderr)
    sys.exit(2)
out = sys.argv[sys.argv.index("--out") + 1]
json.dump({"results": [{"model": m, "run": 0, "votes": {"0": "TRUE"}} for m in ("a/x", "b/y", "c/z")],
           "claims": [{"claim": "a claim", "verdict": "TRUE", "unanimous": True, "kind": "world",
                       "by_model": {"a/x": "TRUE", "b/y": "TRUE", "c/z": "TRUE"}}]}, open(out, "w"))
"""


@pytest.fixture
def box(tmp_path):
    """A checkout with the real wrapper and claimcheck_truth.py, stub extract and vote scripts."""
    (tmp_path / "scripts").mkdir()
    for name in ("run_claim_check.sh", "claimcheck_truth.py"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    (tmp_path / "scripts" / "claim_extract.py").write_text(EXTRACT)
    (tmp_path / "scripts" / "claim_vote.py").write_text(VOTE)
    (tmp_path / "tools").symlink_to(ROOT / "tools")
    (tmp_path / "r.md").write_text("report\n")
    (tmp_path / "v").mkdir()
    return tmp_path


def _run(box, *args, **env):
    return subprocess.run(["bash", "scripts/run_claim_check.sh", "r.md", *args], cwd=box,
                          capture_output=True, text=True, timeout=120,
                          env={**os.environ, "LENZ_API_KEY": "x", **env})


def test_existing_truth_file_keeps_its_hand_rows(box):
    (box / "v" / "truth.csv").write_text(HAND)
    for _ in range(2):          # a second run appends nothing more
        p = _run(box, "--truth", "v/truth.csv", "--no-lenz-hint")
        assert p.returncode == 0, p.stdout + p.stderr
    text = (box / "v" / "truth.csv").read_text()
    assert text.startswith(HAND)
    assert text.count("claimcheck:") == 1
    assert "appending" in p.stdout


def test_force_replaces_the_file(box):
    (box / "v" / "truth.csv").write_text(HAND)
    p = _run(box, "--truth", "v/truth.csv", "--force", "--no-lenz-hint")
    assert p.returncode == 0, p.stdout + p.stderr
    assert "hand/one.py" not in (box / "v" / "truth.csv").read_text()


def test_append_option_is_not_passed_to_claim_vote(box):
    p = _run(box, "--truth", "v/new.csv", "--append", "--dry-run")
    vote = next(ln for ln in p.stdout.splitlines() if "claim_vote.py" in ln)
    assert "--append" not in vote and "--force" not in vote


def test_failed_vote_ends_with_its_exit_code(box):
    for args in ((), ("--truth", "v/truth.csv")):
        p = _run(box, *args, VOTE_FAILS="1")
        assert p.returncode == 2, p.stdout
        assert "vote failed" in p.stdout and "== done" not in p.stdout
    assert not (box / "v" / "truth.csv").exists()


@pytest.mark.parametrize("payload, why", [
    (["a claim", "another"], "claim 1"),                                   # the extractor's claims.json
    ({"claims": [], "results": [{"votes": {}}]}, "result 1"),              # a result row without model
    ({"claims": [{"claim": "c", "by_model": []}], "results": []}, "by_model"),
    ({"claims": {}, "results": []}, "lists"),
])
def test_wrong_file_is_exit_2_with_one_line(tmp_path, capsys, payload, why):
    f = tmp_path / "votes.json"
    f.write_text(json.dumps(payload))
    assert ct.main([str(f), "--out", str(tmp_path / "t.csv"), "--no-rebuild"]) == 2
    err = capsys.readouterr().err
    assert why in err and len(err.strip().splitlines()) == 1
    assert not (tmp_path / "t.csv").exists()


def test_append_to_a_new_file_writes_the_header_as_documented(tmp_path):
    path = tmp_path / "t.csv"
    assert cc_truth.write_truth(path, [], append=True) == ([], [])
    assert path.read_text().strip() == ",".join(cc_truth.TRUTH_HEADER)
    before = path.stat().st_mtime_ns
    cc_truth.write_truth(path, [], append=True)     # an existing file is not touched
    assert path.stat().st_mtime_ns == before
