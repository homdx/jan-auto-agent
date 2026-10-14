"""tests/test_claimcheck_truth_writes_m_review.py — who writes the ground truth, after review batch m (№79–85).

Concurrent appends keep every row; append_verdict writes one header; truth_consensus settles only on a verdict,
counts an adjudicator that says two verdicts as UNDECIDED, and does not drop rows of truth.csv silently;
harvest_report scores a file-only key and leaves a finding with two truths unscored; a worktree whose maker was
killed is removed by its last user.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import harvest_report  # noqa: E402
from tools.claimcheck.target import Target  # noqa: E402


def _rows(path: Path) -> list:
    return list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))


# ── №80: concurrent appends ──────────────────────────────────────────────────────────────────

APPEND = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
          "from tools.claimcheck import truth as t\n"
          "for i in range(int(sys.argv[4])):\n"
          "    t.write_truth(Path(sys.argv[2]), [{'finding': f'p{sys.argv[3]}/f{i}.py::f', 'truth': 'REAL', "
          "'checked_by': 'x', 'how': 'h'}], append=True)")


def test_four_processes_appending_keep_every_row(tmp_path):
    """Read, merge and rename with no lock: 4 processes of 40 rows each left 44 of 160."""
    path = tmp_path / "truth.csv"
    procs = [subprocess.Popen([sys.executable, "-c", APPEND, str(ROOT), str(path), str(k), "20"]) for k in range(4)]
    for p in procs:
        p.wait(120)
    assert len(_rows(path)) == 80


# ── №81: append_verdict's first writers ──────────────────────────────────────────────────────

#: Eight writers start together, so they race for the file.
VERDICT = ("import os, sys, time; sys.path.insert(0, sys.argv[1]); import append_verdict as av\n"
           "while not os.path.exists(sys.argv[2]): time.sleep(0.001)\n"
           "sys.argv = ['append_verdict.py'] + sys.argv[3:]; sys.exit(av.main())")


def _writers(tmp_path, out: Path, findings: list) -> list:
    go = tmp_path / f"go-{out.stem}"
    procs = [subprocess.Popen([sys.executable, "-c", VERDICT, str(ROOT / "scripts"), str(go), "--out", str(out),
                               "--finding", f, "--truth", "REAL", "--confidence", "HIGH", "--how", "h",
                               "--evidence", "e", "--consequence", "c", "--severity", "LOW", "--fix", "f"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for f in findings]
    time.sleep(0.5)                                     # every writer past its imports
    go.touch()
    return [p.wait(60) for p in procs]


@pytest.mark.skipif(os.name == "nt", reason="fcntl")
def test_a_writer_waits_for_the_lock_of_the_file(tmp_path):
    """The check of the file, the check of the finding and the append are one step under the lock of the
    file's directory: a writer started while it is held writes nothing until it is let go (a race of eight
    writers shows the doubled header only some of the time; this shows the lock every time)."""
    import fcntl
    out = tmp_path / "truth-adj.csv"
    fd = os.open(tmp_path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "append_verdict.py"), "--out", str(out),
                                 "--finding", "a.py::f", "--truth", "REAL", "--confidence", "HIGH", "--how", "h",
                                 "--evidence", "e", "--consequence", "c", "--severity", "LOW", "--fix", "f"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        held = out.exists()
    finally:
        os.close(fd)
    assert proc.wait(60) == 0 and not held and len(_rows(out)) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["truth-adj.csv"]     # no lock file left behind


def test_concurrent_first_writers_write_one_header(tmp_path):
    out = tmp_path / "truth-adj.csv"
    assert _writers(tmp_path, out, [f"a{k}.py::f" for k in range(8)]) == [0] * 8
    lines = out.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("finding,") for line in lines) == 1 and len(_rows(out)) == 8


def test_concurrent_writers_of_one_finding_decide_it_once(tmp_path):
    """The "already decided" check ran outside the write: two writers of one finding both passed it."""
    out = tmp_path / "truth-one.csv"
    assert sorted(_writers(tmp_path, out, ["a.py::f"] * 8)) == [0] + [2] * 7
    assert [r["finding"] for r in _rows(out)] == ["a.py::f"]


# ── №82, №83, №79: truth_consensus ───────────────────────────────────────────────────────────

def _adj(tmp_path, name, rows, header="finding,truth,how"):
    p = tmp_path / f"truth-{name}.csv"
    p.write_text(header + "\n" + "".join(r + "\n" for r in rows), encoding="utf-8")
    return p


def _consensus(tmp_path, *extra):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "truth_consensus.py"),
                           str(tmp_path / "truth-*.csv"), "--report", str(tmp_path / "adj.md"), *extra],
                          capture_output=True, text=True, cwd=tmp_path, timeout=60)


def test_a_word_that_is_no_verdict_is_refused_with_its_line(tmp_path):
    _adj(tmp_path, "alice", ["src/a.py::f,REAL,read it", "src/b.py::g,MAYBE,z"])
    _adj(tmp_path, "bob", ["src/a.py::f,REAL,ran it", "src/b.py::g,MAYBE,z"])
    r = _consensus(tmp_path, "--truth", str(tmp_path / "out.csv"))
    assert r.returncode == 2 and "truth-alice.csv:3: 'MAYBE'" in r.stderr and "truth-bob.csv:3" in r.stderr
    assert not (tmp_path / "out.csv").exists()


def test_a_second_header_line_is_refused(tmp_path):
    """append_verdict's doubled header was read as a row and settled the verdict TRUTH."""
    _adj(tmp_path, "adj", ["finding,TRUTH,how", "src/a.py::f,REAL,read it"])
    assert _consensus(tmp_path).returncode == 2


def test_blank_cells_settle_nothing(tmp_path):
    _adj(tmp_path, "alice", ["src/a.py::f,,z"])
    _adj(tmp_path, "bob", ["src/a.py::f,,z"])
    r = _consensus(tmp_path, "--truth", str(tmp_path / "out.csv"))
    assert r.returncode == 4 and "settled 0" in r.stdout and _rows(tmp_path / "out.csv") == []


@pytest.mark.parametrize("order", [("REAL", "FALSE"), ("FALSE", "REAL")])
def test_an_adjudicator_that_says_two_verdicts_is_undecided(tmp_path, order):
    """claimcheck_truth writes REAL and FALSE for two claims about one place; the last row was the vote: the
    same rows in another order settled x.py::f FALSE, and y.py::g was disputed."""
    _adj(tmp_path, "claimcheck", [f"x.py::f,{order[0]},a", f"x.py::f,{order[1]},b", "y.py::g,REAL,c"])
    _adj(tmp_path, "bob", ["x.py::f,FALSE,read it", "y.py::g,REAL,read it"])
    r = _consensus(tmp_path, "--truth", str(tmp_path / "out.csv"))
    got = {row["finding"]: (row["truth"], row["checked_by"]) for row in _rows(tmp_path / "out.csv")}
    assert got == {"x.py::f": ("FALSE", "bob"), "y.py::g": ("REAL", "bob+claimcheck")}, r.stderr
    assert "Contradicting themselves (1)" in (tmp_path / "adj.md").read_text(encoding="utf-8")


def test_truth_csv_rows_no_adjudicator_settles_are_not_dropped_silently(tmp_path):
    truth = tmp_path / "truth.csv"
    truth.write_text("finding,truth,checked_by,duplicate_of,how\nsrc/machine.py::a,REAL,claimcheck:x@1,,code\n"
                     "src/human.py::b,FALSE,alice,src/other.py::c,z\n", encoding="utf-8")
    _adj(tmp_path, "alice", ["src/human.py::b,FALSE,z"])
    r = _consensus(tmp_path, "--truth", str(truth))
    assert r.returncode == 2 and "src/machine.py::a" in r.stderr
    assert "src/machine.py::a" in truth.read_text(encoding="utf-8")            # untouched
    r = _consensus(tmp_path, "--truth", str(truth), "--replace")
    assert r.returncode == 0, r.stderr
    [row] = _rows(truth)
    assert (row["finding"], row["truth"], row["duplicate_of"]) == ("src/human.py::b", "FALSE", "src/other.py::c")


# ── №84, №83: harvest_report's truth ─────────────────────────────────────────────────────────

def test_harvest_scores_a_file_only_key_and_not_a_finding_with_two_truths(tmp_path, capsys):
    truth = tmp_path / "truth.csv"
    truth.write_text("finding,truth\ntools/x.py,REAL\na.py::f,REAL\na.py::f,FALSE\n", encoding="utf-8")
    got = harvest_report.load_truth(str(truth))
    key = next(iter(harvest_report.group([{"file": "tools/x.py", "symbol": ""}])))
    assert got.get(key) == "REAL"                                   # a reviewer's finding of the file is scored
    assert "a.py::f" not in got and "a.py::f (FALSE/REAL)" in capsys.readouterr().err


# ── №85: a worktree whose maker was killed ───────────────────────────────────────────────────

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_a_worktree_whose_maker_was_killed_is_removed_by_its_last_user(tmp_path):
    repo = tmp_path / "op"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("A = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    sha, scratch = _git(repo, "rev-parse", "HEAD"), tmp_path / "s"
    code = ("import os, sys; sys.path.insert(0, sys.argv[1]); from tools.claimcheck.target import Target; "
            "Target.open(sys.argv[2], sys.argv[3], scratch=sys.argv[4]); os._exit(9)")
    subprocess.run([sys.executable, "-c", code, str(ROOT), str(repo), sha, str(scratch)], timeout=120)
    left = list((scratch / "claimcheck").glob(f"*/{sha}/tree"))
    assert len(left) == 1                                            # the kill left it behind
    t = Target.open(repo, sha, scratch=scratch)
    assert t.tree == left[0]
    t.close()
    assert not left[0].exists()
    assert str(left[0]) not in _git(repo, "worktree", "list")


# ── m2 L-2: the collect cache of a target ────────────────────────────────────────────────────

def test_the_collect_cache_is_kept_by_the_config_object_not_its_id(tmp_path, monkeypatch):
    """An id() outlives its object: a config made where a collected one was gets the same id, and got the
    other config's model. Here the stale entry is put under the new config's id, as the reuse would."""
    import configparser
    from tools.claimcheck import target as cc_target
    t = Target(repo=str(tmp_path), ref="HEAD", sha="0" * 40, tree=tmp_path, scratch=tmp_path, git_dir=tmp_path,
               created=False, warnings=[])
    fresh = object()
    monkeypatch.setattr(cc_target.collect_loader, "load", lambda tree, config=None: None)
    monkeypatch.setattr(Target, "_build_collect", lambda self, config: fresh)
    new = configparser.ConfigParser()
    t._models[id(new)] = (configparser.ConfigParser(), "the model of a config that is gone")
    assert t.collect(new) is fresh
    assert t.collect(new) is fresh                          # and kept for it
