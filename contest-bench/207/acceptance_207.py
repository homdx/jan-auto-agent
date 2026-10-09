"""Round 207 black box: written from ticket 207 alone, never from an entry's code.

Run from a checkout root:  python3 -m pytest contest-bench/207/acceptance_207.py -n0 -q
Bugs 35 (context_memory), 36 (export), 37 (gates), 38 (basecheck), 39 (next_task).
"""
import csv
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.arena import basecheck
from tools.contest import context_memory as cm
from tools.contest import export as ex
from tools.contest import gates

ROOT = Path(__file__).resolve().parents[2]


# ── 35: context_memory ──────────────────────────────────────────────────────
@pytest.mark.parametrize("v", [float("nan"), float("inf"), float("-inf")])
def test_35_number_non_finite_is_none(v):
    assert cm._number(v) is None


def test_35_number_finite_still_works():
    assert cm._number(262144.9) == 262144
    assert cm._number("262,144") == 262144
    assert cm._number(None) is None and cm._number("") is None
    assert cm._number(True) is None


def _rec(i, **kw):
    d = {"at": 1.8e9 + i, "round": "r", "agent": f"a{i}", "provider": "p",
         "model": f"m{i}", "limit": 1000, "last_ok": 900, "prompt": 950}
    d.update(kw)
    return d


def test_35_load_keeps_good_records_around_one_non_finite(tmp_path):
    p = tmp_path / "mem.json"
    body = json.dumps([_rec(1), _rec(2, limit=0), _rec(3)]).replace(
        '"limit": 0', '"limit": Infinity')
    assert "Infinity" in body
    p.write_text(body, encoding="utf-8")
    recs = cm.load(p, days=36500, now=1.8e9 + 10)
    models = [r.model for r in recs]
    assert "m1" in models and "m3" in models


@pytest.mark.parametrize("word", ["NaN", "Infinity", "-Infinity"])
def test_35_load_never_raises_on_bare_words(tmp_path, word):
    p = tmp_path / "mem.json"
    body = json.dumps([_rec(1), _rec(2, prompt=0, last_ok=0)]).replace(
        '"prompt": 0', f'"prompt": {word}').replace('"last_ok": 0', f'"last_ok": {word}')
    p.write_text(body, encoding="utf-8")
    recs = cm.load(p, days=36500, now=1.8e9 + 10)
    assert isinstance(recs, list)
    assert any(r.model == "m1" for r in recs)


# ── 36: export ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan")])
def test_36_number_non_finite_is_zero(v):
    assert ex._number(v) == 0


def test_36_number_ordinary():
    assert ex._number("7") == 7 and ex._number(3.9) == 3
    assert ex._number(None) == 0 and ex._number("x") == 0


class _State:
    def __init__(self, rows):
        self._rows = rows

    def table_rows(self):
        return self._rows


def test_36_infinite_tokens_still_get_their_row():
    rows = [
        {"name": "good", "model": "p/m", "state": "READY", "tokens": {"input": 5, "output": 2}},
        {"name": "inf", "model": "p/m", "state": "READY",
         "tokens": {"input": float("inf"), "output": float("nan"), "reasoning": float("inf"),
                    "cache": {"read": float("inf")}},
         "permissions": {"asked": float("inf")}, "sessions": float("nan"),
         "compactions": float("inf")},
    ]
    lines = ex.render_table(_State(rows), [])
    assert any("| inf |" in l for l in lines)
    assert any("| good |" in l for l in lines)


# ── 37: gates ───────────────────────────────────────────────────────────────
def _g(cwd, *a, env=None):
    e = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
             GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", **(env or {}))
    return subprocess.run(["git", *a], cwd=cwd, env=e, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "wt"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    (r / "README").write_text("x\n")
    _g(r, "add", "-A")
    _g(r, "commit", "-q", "-m", "base")
    return r


def _row(repo):
    return gates.judge_worktree("a", str(repo), "main", [], False)


def _commit_on_branch(repo, files, message=b"work"):
    _g(repo, "checkout", "-q", "-b", "agent")
    for name, data in files.items():
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    _g(repo, "add", "-A")
    subprocess.run(["git", "commit", "-q", "-F", "-"], cwd=repo, input=message, check=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"))


def test_37_binary_fixture_under_tests(repo):
    _commit_on_branch(repo, {"tests/fixtures/sample.png": b"\x89PNG\r\n\x1a\n\xff\xfe\x00\xc0",
                             "tests/test_a.py": b"def test_one():\n    pass\n"})
    row = _row(repo)
    assert row["files"] == 2
    assert row["test_files"] == 2
    assert row["test_funcs"] == 1


def test_37_latin1_byte_in_test_file_still_counts(repo):
    _commit_on_branch(repo, {"tests/test_b.py": b"# caf\xe9\ndef test_a():\n    pass\n\ndef test_b():\n    pass\n"})
    row = _row(repo)
    assert row["test_funcs"] == 2


def test_37_latin1_commit_message_is_a_row(repo):
    _commit_on_branch(repo, {"tests/test_c.py": b"def test_x():\n    pass\n"},
                      message=b"caf\xe9 fix")
    row = _row(repo)
    assert row["commits"] == 1
    assert row["sha"] != "—"


def test_37_git_helper_survives_latin1_log(repo):
    _commit_on_branch(repo, {"f.txt": b"x\n"}, message=b"caf\xe9")
    out = gates.git(str(repo), "log", "--format=%s", "main..HEAD")
    assert isinstance(out, str) and out.startswith("caf")


def test_37_git_helper_survives_latin1_diff(repo):
    _commit_on_branch(repo, {"f.txt": b"caf\xe9 \xff\n"})
    out = gates.git(str(repo), "diff", "main..HEAD")
    assert isinstance(out, str) and "f.txt" in out


def test_37_shrink_reads_bridge_with_latin1(repo):
    bridge = (b"# caf\xe9\ndef _shrink(x):\n    return x\n")
    _commit_on_branch(repo, {"tools/auto/collect_bridge.py": bridge})
    assert gates.extract_shrink(str(repo), "HEAD") is not None
    row = _row(repo)
    assert row["shrink"] in ("?", "same", "CHANGED", "GONE")


def test_37_absent_bridge_is_none(repo):
    assert gates.extract_shrink(str(repo), "HEAD") is None


def test_37_git_helper_unchanged_for_plain_output(repo):
    assert gates.git(str(repo), "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert gates.git(str(repo), "log", "--format=%s") == "base"


# ── 38: basecheck ───────────────────────────────────────────────────────────
def _py(code):
    return [sys.executable, "-c", code]


def test_38_red_step_with_undecodable_output_is_a_row(tmp_path):
    steps = [basecheck.Step("tests", _py(
        "import sys; sys.stdout.buffer.write(b'1 passed\\n\\xff\\xfe bad\\n'); "
        "sys.stderr.buffer.write(b'warn \\xe9\\n'); sys.exit(1)"))]
    rows = basecheck.run_steps(tmp_path, steps)
    assert len(rows) == 1 and rows[0]["ok"] is False
    assert isinstance(rows[0]["summary"], str) and rows[0]["summary"]


def test_38_green_step_with_undecodable_output_is_ok(tmp_path):
    steps = [basecheck.Step("lint", _py("import sys; sys.stdout.buffer.write(b'fine \\xff\\n')"))]
    rows = basecheck.run_steps(tmp_path, steps)
    assert rows[0]["ok"] is True


def test_38_later_steps_still_run(tmp_path):
    steps = [
        basecheck.Step("a", _py("import sys; sys.stdout.buffer.write(b'\\xff'); sys.exit(1)")),
        basecheck.Step("b", _py("print('ok')")),
    ]
    rows = basecheck.run_steps(tmp_path, steps)
    assert [r["step"] for r in rows] == ["a", "b"]
    assert [r["ok"] for r in rows] == [False, True]


def test_38_stderr_only_undecodable(tmp_path):
    steps = [basecheck.Step("x", _py(
        "import sys; sys.stderr.buffer.write(b'\\xff\\xff'); sys.exit(3)"))]
    rows = basecheck.run_steps(tmp_path, steps)
    assert rows[0]["ok"] is False


def test_38_pytest_failed_line_latin1_name(tmp_path):
    steps = [basecheck.Step("tests", _py(
        "import sys; sys.stdout.buffer.write(b'FAILED tests/test_caf\\xe9.py::t - x\\n"
        "1 failed in 1s\\n'); sys.exit(1)"), pytest=True)]
    rows = basecheck.run_steps(tmp_path, steps)
    assert rows[0]["ok"] is False and "1 failed" in rows[0]["summary"]


def test_38_unstartable_step_is_still_a_failed_row(tmp_path):
    rows = basecheck.run_steps(tmp_path, [basecheck.Step("nope", ["/no/such/binary-207"])])
    assert rows[0]["ok"] is False


def test_38_plain_output_unchanged(tmp_path):
    rows = basecheck.run_steps(tmp_path, [basecheck.Step("t", _py("print('3 passed in 0.1s')"))])
    assert rows[0]["ok"] is True and rows[0]["summary"] == "3 passed in 0.1s"


# ── 39: next_task ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def nt():
    spec = importlib.util.spec_from_file_location("next_task_207", ROOT / "scripts" / "next_task.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("data", [
    b"\xff\xfeticket,outcome\n01,done\n",
    b"ticket,outcome\n01,\x00done\n",
    b"ticket,outcome\n01," + b"x" * 200000 + b"\n",
    b"\x80\x81\x82",
])
def test_39_unreadable_progress_is_empty(nt, tmp_path, data):
    p = tmp_path / "PROGRESS.csv"
    p.write_bytes(data)
    assert nt.recorded(str(p)) == {}


def test_39_readable_file_still_read(nt, tmp_path):
    p = tmp_path / "PROGRESS.csv"
    p.write_text("ticket,outcome\n01,done\n02,failed\n", encoding="utf-8")
    assert nt.recorded(str(p)) == {"01": "done", "02": "failed"}


def test_39_missing_and_empty(nt, tmp_path):
    assert nt.recorded(str(tmp_path / "none.csv")) == {}
    (tmp_path / "e.csv").write_text("")
    assert nt.recorded(str(tmp_path / "e.csv")) == {}


def test_39_script_hands_out_a_ticket_with_unreadable_progress(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "01-first.md").write_text("# First\n\nbody\n")
    (tasks / "PROGRESS.csv").write_bytes(b"\xff\xfe\x00junk")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "next_task.py"), "--tasks", str(tasks)],
                       capture_output=True, text=True)
    assert "Traceback" not in r.stderr
    assert "UnicodeDecodeError" not in r.stderr and "csv.Error" not in r.stderr
    assert "First" in r.stdout or "01" in r.stdout
