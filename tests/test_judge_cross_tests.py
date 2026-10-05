"""tests/test_judge_cross_tests.py — 158: the judge's cross phase.

`scripts/judge_epic_round.py` gained `cross_tests`, which runs every entry's own
new or changed tests against every other entry's code, the base, and an optional
candidate ideal, then prints one matrix and writes `cross.json` / `cross.md`
next to `SUMMARY.md`. Before this the judge made that table by hand, with a
shell loop.

Every case runs against a temp repo and temp worktrees only, never this
checkout. Three entries are enough to hold all four cases the ticket asks for:
`a` adds a name only it has (an `api` cell), `b` breaks something the base has
(a `behaviour` cell) while also testing something no one returns (a `base`
cell), and the base column is what separates the last two.

Knowledge label: 158 regression test.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import subprocess
import sys
import tempfile

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

JUDGE = REPO_ROOT / "scripts" / "judge_epic_round.py"

_BRIDGE = """\
class CollectBridge:
    def _shrink(self, text):
        return text[:10]
"""

_TICKET = """\
# 158 — cross

**File:** `pkg/mod.py`

**Also touches:** `tests/test_a.py`
"""

#: The base: two facts every implementation starts from.
_MOD_BASE = """\
def shared():
    return 1


def other():
    return 10
"""

#: `a` adds a name only it has.
_MOD_A = """\
def shared():
    return 1


def other():
    return 10


def special():
    return 2
"""

#: `b` keeps both base names but moves one of them.
_MOD_B = """\
def shared():
    return 2


def other():
    return 10
"""

_TEST_BASE = """\
import pkg.mod as mod


def test_base():
    assert mod.shared() == 1
"""

_TEST_A = """\
import pkg.mod as mod


def test_other():
    assert mod.other() == 10


def test_special():
    assert mod.special() == 2
"""

_TEST_B = """\
import pkg.mod as mod


def test_shared_is_one():
    assert mod.shared() == 1


def test_nobody_returns_99():
    assert mod.shared() == 99
"""


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {cwd} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _load_judge():
    """`scripts/judge_epic_round.py` as a module, the way the CLI loads it."""
    spec = importlib.util.spec_from_file_location("judge_epic_round_158", JUDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


judge = _load_judge()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _repo(root: Path) -> tuple[Path, str]:
    """A temp repo whose base commit carries the bridge, a ticket and a test."""
    _write(root / "tools" / "auto" / "collect_bridge.py", _BRIDGE)
    _write(root / "epic-tasks" / "158-cross.md", _TICKET)
    _write(root / "pkg" / "__init__.py", "")
    _write(root / "pkg" / "mod.py", _MOD_BASE)
    _write(root / "tests" / "test_base.py", _TEST_BASE)
    # An empty root conftest puts the repo root on sys.path, so a copied test
    # can `import pkg.mod` the way an entry's own test does.
    _write(root / "conftest.py", "")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "cross@example.com")
    _git(root, "config", "user.name", "Cross")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _entry(repo: Path, base: str, root: Path, name: str) -> Path:
    """A worktree at *base* on its own branch, the way the round hands one out."""
    wt = root / f"wt-{name}"
    _git(repo, "worktree", "add", "-q", str(wt), base)
    _git(wt, "checkout", "-q", "-b", f"contest/158/{name}", base)
    return wt


def _commit(wt: Path, message: str) -> None:
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", message)


@pytest.fixture(scope="module")
def trees(tmp_path_factory):
    """`(base_sha, repo, [(name, worktree)])` — two entries, each with its own tests."""
    root = tmp_path_factory.mktemp("cross")
    repo, base = _repo(root / "repo")
    a = _entry(repo, base, root, "a")
    _write(a / "pkg" / "mod.py", _MOD_A)
    _write(a / "tests" / "test_a.py", _TEST_A)
    _commit(a, "a: add special() and its test")
    b = _entry(repo, base, root, "b")
    _write(b / "pkg" / "mod.py", _MOD_B)
    _write(b / "tests" / "test_b.py", _TEST_B)
    _commit(b, "b: move shared() and its test")
    return base, repo, [("a", a), ("b", b)]


@pytest.fixture(scope="module")
def matrix(trees, tmp_path_factory):
    """`cross_tests` run once over the two entries, artifacts in a temp dir."""
    base, repo, ts = trees
    out = tmp_path_factory.mktemp("matrix") / "contest-out" / "158"
    data = judge.cross_tests(ts, base, jobs=1, out_dir=str(out),
                             round_no=158, ticket="158-cross.md", log=lambda *a: None)
    data["_out"] = out
    return data


@pytest.fixture
def scratch_tmp(tmp_path, monkeypatch):
    """A private temp dir for one test: `tempfile` caches its first answer in the pytest process, so
    the env var alone sends the judge's scratch dirs to the real /tmp and every "nothing left in
    TMPDIR" check passes without looking."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setenv("TMPDIR", str(d))
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


def _cell(data, tests: str, code: str) -> dict:
    for c in data["cells"]:
        if c["tests"] == tests and c["code"] == code:
            return c
    raise AssertionError(f"no cell for {tests} on {code}: {data['cells']}")


def _kinds(data, tests: str, code: str) -> list[str]:
    return sorted(f["kind"] for f in _cell(data, tests, code)["failures"])


def test_the_matrix_counts_and_the_second_cell_is_api(matrix):
    """Rows are whose tests, columns whose code; a name only P has is `api`."""
    assert matrix["tests"] == ["a", "b"]
    assert matrix["impls"] == ["a", "b", "base"]
    assert len(matrix["cells"]) == 6

    assert (_cell(matrix, "a", "a")["passed"], _cell(matrix, "a", "a")["total"]) == (2, 2)
    # `a` on `b`: `other()` still 10, `special()` is a name only `a` has.
    assert (_cell(matrix, "a", "b")["passed"], _cell(matrix, "a", "b")["total"]) == (1, 2)
    assert _kinds(matrix, "a", "b") == ["api"]
    assert (_cell(matrix, "a", "base")["passed"], _cell(matrix, "a", "base")["total"]) == (1, 2)
    assert _kinds(matrix, "a", "base") == ["api"]

    node = _cell(matrix, "a", "b")["failures"][0]["node"]
    assert node.endswith("test_a.py::test_special")
    assert "AttributeError" in _cell(matrix, "a", "b")["failures"][0]["line"]


def test_a_test_that_fails_on_the_base_too_is_base(matrix):
    """`no one returns 99`: the base fails it too, so it is not a finding."""
    for code in ("a", "b", "base"):
        fails = _cell(matrix, "b", code)["failures"]
        n99 = [f for f in fails if f["node"].endswith("::test_nobody_returns_99")]
        assert n99, fails
        assert n99[0]["kind"] == "base"


def test_a_behaviour_failure_carries_its_E_line(matrix):
    """`b` moved `shared()`: a test the base satisfies is a lead, not a verdict."""
    cell = _cell(matrix, "b", "b")
    assert (cell["passed"], cell["total"]) == (0, 2)
    assert _kinds(matrix, "b", "b") == ["base", "behaviour"]

    behav = [f for f in cell["failures"] if f["kind"] == "behaviour"]
    assert len(behav) == 1
    assert behav[0]["node"].endswith("::test_shared_is_one")
    assert behav[0]["line"].startswith("E   assert 2 == 1")

    md = (matrix["_out"] / "cross.md").read_text(encoding="utf-8")
    assert "## Failures" in md
    assert "- **b on b: 0/2**" in md
    assert "E   assert 2 == 1" in md
    # the matrix itself, as a table, with `passed/total` in every cell
    for line in ("| tests \\ code | a | b | base |", "| a | 2/2 | 1/2 | 1/2 |"):
        assert line in md


def test_no_file_is_left_in_a_worktree_or_as_a_basetemp(trees, tmp_path, scratch_tmp):
    """Every cell's scratch copy and basetemp are gone when the phase returns."""
    base, repo, ts = trees

    data = judge.cross_tests(ts, base, jobs=1, out_dir=str(tmp_path / "out"),
                             log=lambda *a: None)

    assert data["cells"] and all(c["total"] is not None for c in data["cells"])
    assert sorted(p.name for p in scratch_tmp.iterdir()) == []
    assert not glob.glob(os.path.join(str(tmp_path), "**", ".pytest_basetemp"),
                         recursive=True)
    for _, wt in ts:
        assert _git(wt, "status", "--porcelain") == "", f"{wt} was written to"
    assert _git(repo, "status", "--porcelain") == "", "the base repo was written to"

    (tmp_path / "out" / "cross.json").read_text(encoding="utf-8")
    (tmp_path / "out" / "cross.md").read_text(encoding="utf-8")


def test_a_broken_tree_is_a_cell_that_says_so(trees, tmp_path):
    """A ref git cannot archive and a path that is not a repo never raise."""
    base, repo, ts = trees
    out = tmp_path / "out"
    data = judge.cross_tests([(ts[0][0], ts[0][1]), ("ghost", str(tmp_path / "nope"))],
                             base, ideal="no-such-ref", out_dir=str(out),
                             log=lambda *a: None)

    assert data["ideal"] == "no-such-ref"
    assert "ideal" not in data["impls"], "an unresolvable ideal is not a column"
    ghost = _cell(data, ts[0][0], "ghost")
    assert ghost["total"] is None and ghost["failures"] == []
    assert ghost["note"]
    assert judge._cell_str(ghost) == "n/a"
    assert json.loads((out / "cross.json").read_text(encoding="utf-8"))["cells"]


def test_the_cli_prints_the_table_and_writes_both_artifacts(trees, tmp_path):
    """`--cross` is the one command: the table on stdout, the artifacts next to SUMMARY."""
    base, repo, ts = trees
    out = tmp_path / "contest-out" / "158"
    cmd = [sys.executable, str(JUDGE), "--round", "158", "--tasks", str(repo / "epic-tasks"),
           "--base", base, "--cross", "--jobs", "4", "--cell-timeout", "120",
           "--cross-out", str(out)]
    for n, p in ts:
        cmd += ["--worktree", f"{n}={p}"]
    proc = subprocess.run(cmd, cwd=str(repo), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    out_txt = proc.stdout
    assert "Cross — every entry's own tests, run on every implementation" in out_txt
    assert "tests \\ code" in out_txt
    assert "1/2" in out_txt and "0/2" in out_txt
    assert "behaviour" in out_txt and "api" in out_txt and "base" in out_txt
    assert "E   assert 2 == 1" in out_txt
    assert "cross.json" in out_txt

    data = json.loads((out / "cross.json").read_text(encoding="utf-8"))
    assert data["tests"] == ["a", "b"] and data["impls"] == ["a", "b", "base"]
    assert len(data["cells"]) == 6
    assert (out / "cross.md").read_text(encoding="utf-8").startswith("# Cross")

    # the score table is still printed first, before the cross phase
    assert out_txt.index("Round 158:") < out_txt.index("Cross —")


def test_the_cross_phase_never_raises(trees):
    """Fail-open: an absent tree list and an unreadable base both degrade."""
    base, repo, ts = trees
    assert judge.cross_tests([], base, log=lambda *a: None)["cells"] == []
    assert judge.cross_tests(ts, "definitely-not-a-ref", log=lambda *a: None)["impls"] == \
        [n for n, _ in ts]


# ── what the other entries of round 158 and the bench found ────────────────────────────────────
_TEST_C = """\
from pkg.mod import special


def test_c_uses_special():
    assert special() == 2
"""

_TEST_HANG = """\
import time


def test_h_never_ends():
    time.sleep(600)
"""


def _extra(trees, tmp_path, name: str, test_text: str, mod_text: str = _MOD_BASE) -> tuple[str, Path]:
    """One more entry on the module's repo: its own worktree, one test file, committed."""
    base, repo, _ts = trees
    wt = _entry(repo, base, tmp_path, name)
    _write(wt / "pkg" / "mod.py", mod_text)
    _write(wt / "tests" / f"test_{name.replace('.', '_').replace('-', '_')}.py", test_text)
    _commit(wt, f"{name}: a test")
    return name, wt


def test_a_module_that_cannot_import_on_the_other_code_is_api(trees, tmp_path):
    """A collection error (ImportError at module level) is the commonest `api` there is: the
    cell keeps its E line, and it is `api`, never `base` (the base cannot import it either)."""
    base, repo, ts = trees
    c = _extra(trees, tmp_path, "c", _TEST_C)
    data = judge.cross_tests([*ts, c], base, workers=0, log=lambda *a: None)
    for code in ("b", "base", "c"):
        cell = _cell(data, "c", code)
        assert [f["kind"] for f in cell["failures"]] == ["api"], (code, cell)
        assert "ImportError" in cell["failures"][0]["line"], cell["failures"]
    assert _cell(data, "c", "a")["failures"] == []


def test_an_entry_named_like_a_model_is_still_importable(trees, tmp_path):
    """`agnes-2.5-flash` and `apertus-v1-5-8b.GAVE_UP` are names of this very round: a `.` in the
    copied file's name made every cell of such an entry `ModuleNotFoundError`, i.e. a false `api`."""
    base, repo, ts = trees
    dotted = _extra(trees, tmp_path, "agnes-2.5-flash.GAVE_UP", _TEST_B, _MOD_B)
    data = judge.cross_tests([dotted], base, workers=0, log=lambda *a: None)
    own = _cell(data, "agnes-2.5-flash.GAVE_UP", "agnes-2.5-flash.GAVE_UP")
    assert (own["passed"], own["total"]) == (0, 2), own          # its own wrong test, not an import
    assert not [f for f in own["failures"] if f["kind"] == "api"], own["failures"]
    assert [f["node"].split("::")[1] for f in own["failures"]
            if f["kind"] == "behaviour"] == ["test_shared_is_one"]
    names = [f["node"] for f in own["failures"]]
    assert all("." not in n.split("::")[0].rsplit("/", 1)[-1].rsplit(".", 1)[0] for n in names), names


def test_a_tree_named_base_or_ideal_is_that_column_not_a_second_one(trees, tmp_path):
    """`setup_worktrees.py` creates a `base` and an `ideal` worktree of its own; handed to the
    judge they are the columns, and `--base` / `--ideal` must not archive a second one beside them."""
    base, repo, ts = trees
    wt_base = tmp_path / "wt-base"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt_base), base)
    wt_ideal = tmp_path / "wt-ideal"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt_ideal), _git(ts[0][1], "rev-parse", "HEAD"))
    data = judge.cross_tests([*ts, ("base", wt_base), ("ideal", wt_ideal)], base, ideal="HEAD",
                             workers=0, log=lambda *a: None)
    assert data["impls"] == ["a", "b", "base", "ideal"], data["impls"]
    # the `ideal` tree carries a's commit, tests included: it is a row too (its tests on every code)
    assert data["tests"] == ["a", "b", "ideal"]
    assert len(data["cells"]) == 3 * 4
    assert (_cell(data, "a", "ideal")["passed"], _cell(data, "a", "ideal")["total"]) == (2, 2)


def test_the_ideal_ref_is_a_column(trees):
    base, repo, ts = trees
    a_sha = _git(ts[0][1], "rev-parse", "HEAD")
    data = judge.cross_tests(ts, base, ideal=a_sha, workers=0, log=lambda *a: None)
    assert data["impls"] == ["a", "b", "base", "ideal"]
    cell = _cell(data, "a", "ideal")
    assert (cell["passed"], cell["total"]) == (2, 2)
    assert [f["kind"] for f in _cell(data, "b", "ideal")["failures"]] == ["base"]


def test_an_entry_without_a_test_change_is_a_column_and_no_row(trees, tmp_path):
    base, repo, ts = trees
    quiet = _entry(repo, base, tmp_path, "quiet")
    _write(quiet / "pkg" / "mod.py", _MOD_A)
    _commit(quiet, "quiet: code only")
    data = judge.cross_tests([*ts, ("quiet", quiet)], base, workers=0, log=lambda *a: None)
    assert data["tests"] == ["a", "b"]
    assert data["impls"] == ["a", "b", "quiet", "base"]
    assert (_cell(data, "a", "quiet")["passed"], _cell(data, "a", "quiet")["total"]) == (2, 2)


def test_no_entry_with_tests_is_an_empty_matrix_and_no_error(trees, tmp_path):
    base, repo, ts = trees
    quiet = _entry(repo, base, tmp_path, "quiet2")
    out = tmp_path / "out"
    data = judge.cross_tests([("quiet2", quiet)], base, out_dir=str(out), log=lambda *a: None)
    assert data["tests"] == [] and data["cells"] == []
    assert (out / "cross.json").is_file() and (out / "cross.md").is_file()


def test_the_classes_are_ordered_api_then_base_then_behaviour():
    classify = judge._classify
    assert classify("t::x", "E   AttributeError: module 'm' has no attribute 'n'", {"t::x"}) == "api"
    assert classify("t::x", "E   assert 1 == 2", {"t::x"}) == "base"
    assert classify("t::x", "E   assert 1 == 2", set()) == "behaviour"
    assert classify("t::x", "", {"t::x"}) == "base", "an empty error is never `api`"
    assert classify("t::x", "", set()) == "behaviour"
    assert classify("t::x", "E   TypeError: f() got an unexpected keyword argument 'k'", set()) == "api"
    # every TypeError is the ticket's `api`: another implementation's argument shape
    assert classify("t::x", "E   TypeError: 'int' object is not iterable", set()) == "api"
    assert classify("t::x", "E   KeyError: 'k'", set()) == "behaviour"
    # another entry's CLI flags: the test asserts rc == 0 and gets argparse's banner
    assert classify("t::x", "E   AssertionError: usage: judge_epic_round.py [-h] --round ROUND", {"t::x"}) == "api"


def test_two_jobs_leave_the_same_matrix_and_nothing_behind(trees, tmp_path, scratch_tmp):
    base, repo, ts = trees
    one = judge.cross_tests(ts, base, jobs=1, workers=0, log=lambda *a: None)
    two = judge.cross_tests(ts, base, jobs=2, workers=0, log=lambda *a: None)
    shape = lambda d: [(c["tests"], c["code"], c["passed"], c["total"], c["kinds"]) for c in d["cells"]]
    assert shape(one) == shape(two)
    assert sorted(p.name for p in scratch_tmp.iterdir()) == []


def test_a_test_that_never_ends_is_ended_at_the_cell_timeout_and_leaves_no_process(trees, tmp_path, scratch_tmp):
    base, repo, ts = trees
    # a name of its own: three suites at once (the load run) must not see each other's cells
    name = "hang" + os.urandom(4).hex()
    hang = _extra(trees, tmp_path, name, _TEST_HANG)
    import time
    started = time.monotonic()
    data = judge.cross_tests([hang], base, cell_timeout=3, workers=2, log=lambda *a: None)
    assert time.monotonic() - started < 60, "the cell timeout did not end the hanging test"
    cell = _cell(data, name, name)
    assert cell["total"] is None and "ran past 3 s" in cell["note"], cell
    assert judge._cell_str(cell) == "n/a"
    alive = subprocess.run(["pgrep", "-f", f"_xcross_{name}_"], capture_output=True, text=True).stdout
    assert alive.split() == [], "the hanging test's pytest or one of its workers survived its cell"
    assert sorted(p.name for p in scratch_tmp.iterdir()) == []


def test_a_ref_only_one_entry_repo_has_is_found(tmp_path):
    """Round checkouts can be separate clones: `--ideal` is tried in each one."""
    one, two = tmp_path / "one", tmp_path / "two"
    for repo, text in ((one, "1"), (two, "2")):
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "config", "user.email", "x@example.com")
        _git(repo, "config", "user.name", "x")
        (repo / "f.txt").write_text(text, encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", text)
    sha = _git(two, "rev-parse", "HEAD")
    assert judge._resolve([str(one), str(two)], sha) == str(two)
    assert judge._resolve([str(one)], sha) is None


def test_the_artifacts_carry_the_classes_the_lines_and_the_ideal(trees, tmp_path):
    base, repo, ts = trees
    out = tmp_path / "out"
    a_sha = _git(ts[0][1], "rev-parse", "HEAD")
    judge.cross_tests(ts, base, ideal=a_sha, out_dir=str(out), workers=0, round_no=158,
                      ticket="158-cross.md", log=lambda *a: None)
    data = json.loads((out / "cross.json").read_text(encoding="utf-8"))
    assert data["impls"][-1] == "ideal" and data["ideal"] == a_sha and data["round"] == 158
    kinds = {f["kind"] for c in data["cells"] for f in c["failures"]}
    assert kinds == {"api", "base", "behaviour"}
    assert all(f["line"] for c in data["cells"] for f in c["failures"])
    md = (out / "cross.md").read_text(encoding="utf-8")
    assert "- ticket: 158-cross.md" in md and f"- ideal: `{a_sha}`" in md and "`api`" in md


def test_the_cross_phase_is_off_by_default(trees, tmp_path):
    base, repo, ts = trees
    cmd = [sys.executable, str(JUDGE), "--round", "158", "--tasks", str(repo / "epic-tasks"), "--base", base]
    for n, p in ts:
        cmd += ["--worktree", f"{n}={p}"]
    proc = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True, timeout=300)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Cross" not in proc.stdout and not (tmp_path / "contest-out").exists()


def test_the_default_out_dir_is_zero_padded_like_the_runners(trees, tmp_path):
    """`contest-out/07`, not `contest-out/7`: cross.md goes next to the round's SUMMARY.md."""
    base, repo, ts = trees
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "07-cross.md").write_text(_TICKET, encoding="utf-8")
    cmd = [sys.executable, str(JUDGE), "--round", "7", "--tasks", str(tasks), "--base", base, "--cross"]
    for n, p in ts:
        cmd += ["--worktree", f"{n}={p}"]
    proc = subprocess.run(cmd, cwd=str(tmp_path), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "contest-out" / "07" / "cross.md").is_file()
    assert not (tmp_path / "contest-out" / "7").exists()


_OUT_TWO_SECTIONS = """\
============================= test session starts ==============================
=================================== ERRORS ====================================
_______________ ERROR at setup of test_fixture_boom _______________
tests/_xcross_x.py:3: in boom
    raise AttributeError("nope")
E   AttributeError: nope
=================================== FAILURES ===================================
_______________________ test_plain_assert _______________________
tests/_xcross_x.py:9: in test_plain_assert
    assert 1 == 2
E   assert 1 == 2
_______________________ TestK.test_in_a_class _______________________
E   KeyError: 'k'
___________________ ERROR collecting tests/_xcross_y.py ___________________
ImportError while importing test module
E   ImportError: cannot import name 'special' from 'pkg.mod'
=========================== short test summary info ============================
FAILED tests/_xcross_x.py::test_plain_assert - assert 1 == 2
FAILED tests/_xcross_x.py::TestK::test_in_a_class - KeyError: 'k'
ERROR tests/_xcross_x.py::test_fixture_boom - AttributeError: nope
ERROR tests/_xcross_y.py
=================== 2 failed, 2 errors in 0.31s ===================
"""


def test_a_section_is_matched_to_its_test_by_name_not_by_position():
    """xdist prints the sections in the order its workers finish, not in the order of the short
    summary; a fixture error (`ERROR at setup of ...`) borrowed the next test's E line and a failure
    with no line cannot be `api` — so a mismatch made a false `behaviour` lead out of a non-lead."""
    ids = judge._fails(_OUT_TWO_SECTIONS)
    assert ids == ["tests/_xcross_x.py::test_plain_assert", "tests/_xcross_x.py::TestK::test_in_a_class",
                   "tests/_xcross_x.py::test_fixture_boom", "tests/_xcross_y.py"]
    got = dict(judge._pair(ids, _OUT_TWO_SECTIONS))
    assert got["tests/_xcross_x.py::test_plain_assert"] == "E   assert 1 == 2"
    assert got["tests/_xcross_x.py::TestK::test_in_a_class"] == "E   KeyError: 'k'"
    assert got["tests/_xcross_x.py::test_fixture_boom"] == "E   AttributeError: nope"
    assert got["tests/_xcross_y.py"].startswith("E   ImportError: cannot import name 'special'")


def test_a_failure_with_no_matching_section_has_no_line_and_is_not_guessed():
    out = _OUT_TWO_SECTIONS.replace("test_plain_assert ____", "test_other_name ____", 1)
    got = dict(judge._pair(judge._fails(out), out))
    # one node and one section left over: they are the same failure
    assert got["tests/_xcross_x.py::test_plain_assert"] == "E   assert 1 == 2"
    out2 = out.replace("TestK.test_in_a_class", "TestK.test_renamed")
    got2 = dict(judge._pair(judge._fails(out2), out2))
    assert "" in got2.values(), "two unmatched nodes and two unmatched sections are never paired by position"


def test_a_section_header_of_a_very_long_name_is_still_a_header():
    """pytest shrinks the rule around a long name to one `_` on each side; the section was then glued
    to the one before it and its test was left with no E line, i.e. a false `behaviour`."""
    name = "test_a_behaviour_failure_is_behaviour_with_its_e_line_in_cross_md_and_then_some_more_words"
    out = ("=================================== ERRORS ====================================\n"
           f"_ ERROR at setup of {name} _\n"
           "tests/_xcross_x.py:146: in result\n"
           "E   TypeError: cross_tests() got an unexpected keyword argument 'cell_timeout'\n"
           "_______________ ERROR at setup of test_short _______________\n"
           "E   AttributeError: nope\n"
           "=========================== short test summary info ============================\n"
           f"ERROR tests/_xcross_x.py::{name} - TypeError: cross_tests()\n"
           "ERROR tests/_xcross_x.py::test_short - AttributeError: nope\n")
    got = dict(judge._pair(judge._fails(out), out))
    assert got[f"tests/_xcross_x.py::{name}"].startswith("E   TypeError: cross_tests()")
    assert got["tests/_xcross_x.py::test_short"] == "E   AttributeError: nope"


def test_every_finished_cell_is_reported_as_it_finishes(trees):
    """A round is ninety cells and minutes long: one `[k/n]` line per cell, before the matrix."""
    base, repo, ts = trees
    lines: list[str] = []
    judge.cross_tests(ts, base, workers=0, log=lambda *a: lines.append(" ".join(str(x) for x in a)))
    progress = [l for l in lines if l.lstrip().startswith("[")]
    assert [l.split("]")[0].strip() for l in progress] == [f"[{k}/6" for k in range(1, 7)]
    assert "a on b: 1/2" in " ".join(progress)
    first_matrix = next(i for i, l in enumerate(lines) if "tests \\ code" in l)
    assert lines.index(progress[-1]) < first_matrix


def test_a_cell_with_no_verdict_is_run_once_more_before_it_is_n_a(trees, monkeypatch):
    """`no tests ran` (rc 5) once, from a worker that died under load: the second run stands."""
    base, repo, ts = trees
    real = judge._run_pytest
    calls = {"n": 0}

    def flaky(code_root, files, basetemp, timeout, workers=judge.CROSS_WORKERS):
        calls["n"] += 1
        if calls["n"] == 1:
            return 5, "============ no tests ran in 1.58s ============\n", ""
        return real(code_root, files, basetemp, timeout, workers)

    monkeypatch.setattr(judge, "_run_pytest", flaky)
    cell = judge._cross_cell("a", str(ts[0][1]), ["tests/test_a.py"], "a", "HEAD", str(ts[0][1]), 120, 0)
    assert calls["n"] == 2
    assert (cell["passed"], cell["total"]) == (2, 2) and cell["note"] == ""


def test_a_cell_that_never_gives_a_verdict_is_n_a_with_the_reason(trees, monkeypatch):
    base, repo, ts = trees
    monkeypatch.setattr(judge, "_run_pytest", lambda *a, **k: (5, "no tests ran in 0.1s\n", ""))
    cell = judge._cross_cell("a", str(ts[0][1]), ["tests/test_a.py"], "a", "HEAD", str(ts[0][1]), 120, 0)
    assert cell["total"] is None and "rc 5" in cell["note"]


_TEST_SWEEPS_TMP = """\
import glob
import os
import shutil
import tempfile

import pkg.mod as mod


def test_sweeps_the_tmp_dir_like_a_careless_neighbour():
    left = tempfile.mkdtemp(prefix="xcross-left-")      # a dir the test itself leaves behind
    assert os.path.isdir(left)
    for d in glob.glob(os.path.join(tempfile.gettempdir(), "xcross-*")):
        shutil.rmtree(d, ignore_errors=True)            # and a sweep of everything called xcross-*
    assert mod.shared() in (1, 2)
"""


_TEST_SLEEPS = """\
import os
import time


def test_sleeps_in_its_cell():
    time.sleep(3)
    assert os.path.isdir(os.getcwd())      # the cell's scratch dir is still there
"""


def test_a_test_that_sweeps_the_tmp_dir_cannot_take_another_cell_with_it(trees, tmp_path, scratch_tmp):
    """Round 158: agnes-2-5-flash's own test removes every new `<tmp>/xcross-*`; under `--jobs` that was
    another cell's scratch copy (`rc 127`, `no tests ran`). The sleeper holds its cells open for three
    seconds while the sweeper's cells run beside them: the cell's temp dir and name are its own now."""
    base, repo, ts = trees
    sleeper = _extra(trees, tmp_path, "sleeper", _TEST_SLEEPS)
    sweeper = _extra(trees, tmp_path, "sweeper", _TEST_SWEEPS_TMP)
    data = judge.cross_tests([sleeper, sweeper], base, jobs=6, workers=0, log=lambda *a: None)
    assert all(c["total"] is not None for c in data["cells"]), [c for c in data["cells"] if c["total"] is None]
    for code in ("sleeper", "sweeper", "base"):
        cell = _cell(data, "sleeper", code)
        assert (cell["passed"], cell["total"]) == (1, 1), (code, cell)
    own = _cell(data, "sweeper", "sweeper")
    assert (own["passed"], own["total"]) == (1, 1), own
    assert sorted(p.name for p in scratch_tmp.iterdir()) == []


_OUT_SYNTAX_ERROR = """\
=================================== ERRORS ====================================
___________________ ERROR collecting tests/_xcross_s.py ___________________
ImportError while importing test module
E   File "/tmp/judgecell-x/tools/arena/rounds.py", line 12
E     def broken(:
E                ^
E   SyntaxError: invalid syntax
=========================== short test summary info ============================
ERROR tests/_xcross_s.py
"""


def test_the_e_line_of_code_that_does_not_import_is_the_error_not_its_first_frame():
    """A SyntaxError in the code under test opens with `E   File "…", line 12`; that is a frame."""
    got = dict(judge._pair(judge._fails(_OUT_SYNTAX_ERROR), _OUT_SYNTAX_ERROR))
    assert got["tests/_xcross_s.py"] == "E   SyntaxError: invalid syntax"


def _fake_cells(spec):
    """`by` for `_leads`: {(tests, code): (total, [(node, kind), ...])}."""
    by = {}
    for (tests, code), (total, fails) in spec.items():
        by[(tests, code)] = {"tests": tests, "code": code, "passed": None if total is None else total - len(fails),
                             "total": total, "note": "",
                             "failures": [{"node": n, "kind": k, "line": f"E   {n}"} for n, k in fails]}
    return by


def test_the_leads_are_the_behaviour_failures_and_the_tests_that_tell_the_entries_apart():
    node = "tests/_xcross_p_t.py::test_asks_for_the_fix"
    imp = "tests/_xcross_p_t.py"
    spec = {
        ("p", "base"): (2, [(node, "base")]),
        ("p", "p"): (2, []),                              # did what the test asks
        ("p", "q"): (2, [(node, "base")]),                # did not
        ("p", "r"): (2, [(node, "base")]),                # did not
        ("p", "broken"): (1, [(imp, "api")]),             # the module never imported: neither
        ("p", "gone"): (None, []),                        # the cell never ran: neither
        ("p", "buggy"): (2, [("tests/_xcross_p_t.py::test_other", "behaviour")]),
    }
    impls = [(n, "HEAD", "/x") for n in ("p", "q", "r", "broken", "gone", "buggy", "base")]
    leads = judge._leads([("p", "/p", [])], impls, _fake_cells(spec))
    assert [(b["code"], b["node"].split("::")[1]) for b in leads["behaviour"]] == [("buggy", "test_other")]
    (d,) = leads["discriminating"]
    assert d["node"] == node and d["fails_on"] == ["q", "r"], d
    assert d["passes_on"] == ["p", "buggy"], d


def test_a_test_nobody_passes_or_everybody_passes_is_not_a_discriminating_lead():
    node = "tests/_xcross_p_t.py::test_x"
    impls = [(n, "HEAD", "/x") for n in ("p", "q", "base")]
    nobody = _fake_cells({("p", "base"): (1, [(node, "base")]), ("p", "p"): (1, [(node, "base")]),
                          ("p", "q"): (1, [(node, "base")])})
    everybody = _fake_cells({("p", "base"): (1, [(node, "base")]), ("p", "p"): (1, []), ("p", "q"): (1, [])})
    assert judge._leads([("p", "/p", [])], impls, nobody)["discriminating"] == []
    assert judge._leads([("p", "/p", [])], impls, everybody)["discriminating"] == []


_MOD_E = """\
def shared():
    return 2


def other():
    return 10
"""

_TEST_E = """\
import pkg.mod as mod


def test_wants_shared_to_be_two():
    assert mod.shared() == 2
"""


def test_the_matrix_names_the_test_that_only_some_entries_pass(trees, tmp_path, scratch_tmp):
    """`e` asks for `shared() == 2`: the base fails it (class `base`), `b` moved shared() and passes,
    `a` and `e`'s own base-code column do not — the tell-apart a `base` label alone would hide."""
    base, repo, ts = trees
    e = _entry(repo, base, tmp_path, "e")
    _write(e / "pkg" / "mod.py", _MOD_E)
    _write(e / "tests" / "test_e.py", _TEST_E)
    _commit(e, "e: shared() is two, and a test for it")
    out = tmp_path / "out"
    lines: list[str] = []
    data = judge.cross_tests([*ts, ("e", e)], base, workers=0, out_dir=str(out),
                             log=lambda *a: lines.append(" ".join(str(x) for x in a)))
    disc = [d for d in data["leads"]["discriminating"] if d["tests"] == "e"]
    assert [d["node"].split("::")[1] for d in disc] == ["test_wants_shared_to_be_two"]
    assert disc[0]["passes_on"] == ["b", "e"] and disc[0]["fails_on"] == ["a"]
    text = "\n".join(lines)
    assert "Leads — read these first:" in text and "discriminating" in text
    i_matrix = next(i for i, l in enumerate(lines) if "tests \\ code" in l)
    i_leads = next(i for i, l in enumerate(lines) if "Leads — read these first:" in l)
    assert i_matrix < i_leads, "the leads come right after the matrix"
    md = (out / "cross.md").read_text(encoding="utf-8")
    assert "## Leads" in md and md.index("## Leads") < md.index("## Failures")
    assert "passes on: b, e" in md and "fails on: a" in md
