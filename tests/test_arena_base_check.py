"""tests/test_arena_base_check.py — AR-25: `arena base check`, the base's own checks on a clean checkout."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.arena import basecheck, cli


# ── helpers ──────────────────────────────────────────────────────────────────
def _git(cwd: Path, *words: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *words],
        capture_output=True, text=True, check=True,
    )
    return done.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


#: The step that proves a run sees the clean checkout, not the operator's tree:
#: `marker.txt` is committed as "dirty" at `HEAD` and uncommitted as
#: "uncommitted" in the working tree, and `untracked.txt` exists only there.
MARKER = (
    "import pathlib; "
    "print('marker=%s; untracked=%s' % (pathlib.Path('marker.txt').read_text().strip(), "
    "pathlib.Path('untracked.txt').exists()))"
)

#: Nine failed tests and a counts line, pytest-shaped. A step carries `pytest=True`
#: to have them listed; the command line itself is not read for it.
FAILED_OUT = ("import sys; " + "; ".join("print('FAILED tests/t.py::test_%d')" % d
                                         for d in range(9))
              + "; print('9 failed in 0.01s'); sys.exit(1)")

INI_HEAD = """[contest]
out_dir = contest-out

[arena]
profile = base

[arena.profile.base]
branch = HEAD
"""

INI_BRANCH = """[contest]
out_dir = contest-out

[arena]
profile = base

[arena.profile.base]
branch = base
"""

INI_NO_BRANCH = """[contest]
out_dir = contest-out

[arena]
profile = base

[arena.profile.base]
"""


@dataclass
class Throwaway:
    root: Path
    first: str  # the commit the profile's `base` branch points at
    head: str   # the tip, `HEAD` — the fixture's default ref


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Throwaway:
    """A throwaway repository: two commits, a `base` branch on the first, `HEAD` as ref."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _write(root / "marker.txt", "committed\n")
    _write(root / "contest.ini", INI_HEAD)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    first = _git(root, "rev-parse", "HEAD")
    _git(root, "branch", "base")
    _write(root / "marker.txt", "dirty\n")
    _git(root, "commit", "-qam", "two")
    head = _git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(cli, "REPO_ROOT", root)
    return Throwaway(root, first, head)


def good_steps() -> list[basecheck.Step]:
    return [
        basecheck.Step("tests", [sys.executable, "-c", "print('3 passed in 0.01s')"]),
        basecheck.Step("tests_bugfix", [sys.executable, "-c", "print('2 passed in 0.02s')"]),
        basecheck.Step("tiers", [sys.executable, "-c", "print('tiers ok')"]),
        basecheck.Step("clocks", [sys.executable, "-c", "print('clocks ok')"]),
    ]


def red_steps() -> list[basecheck.Step]:
    """`tests_bugfix` is red; the later steps still run."""
    return [
        basecheck.Step("tests", [sys.executable, "-c", "print('3 passed in 0.01s')"]),
        basecheck.Step("tests_bugfix", [sys.executable, "-c",
                              "import sys; print('FAILED tests/test_a.py::test_one'); sys.exit(1)"]),
        basecheck.Step("tiers", [sys.executable, "-c", "print('tiers ok')"]),
        basecheck.Step("clocks", [sys.executable, "-c", "print('clocks ok')"]),
    ]


def marker_steps() -> list[basecheck.Step]:
    return [basecheck.Step("tests", [sys.executable, "-c", MARKER])] + red_steps()[1:]


class Recorder:
    """Stands in for `basecheck.RUN`: records the line and the cwd, never runs it."""

    def __init__(self, codes=None):
        self.calls: list[dict] = []
        self.codes = list(codes or [])

    def __call__(self, line, **kwargs):
        self.calls.append({"line": list(line), "cwd": kwargs.get("cwd")})
        code = self.codes.pop(0) if self.codes else 0
        return subprocess.CompletedProcess(list(line), code, stdout="", stderr="")


def _install(steps, monkeypatch) -> list[basecheck.Step]:
    """Swap the four real steps for tiny `python3 -c` ones, so no suite runs."""
    monkeypatch.setattr(basecheck, "STEPS",
                        tuple((s.name, list(s.command)) for s in steps))
    return steps


def _check(capsys, *argv):
    code = cli.main(["base", "check", *argv])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _cache(repo: Path) -> dict:
    return json.loads((repo.root / ".arena" / "base-check.json").read_text(encoding="utf-8"))


def _pass_entry() -> dict:
    """A stored pass of the shape `run_base_check` writes."""
    return {"at": 1, "ok": True,
            "steps": [{"step": "tests", "ok": True, "seconds": 0.1, "summary": "s"},
                       {"step": "tiers", "ok": True, "seconds": 0.0, "summary": ""}]}


def _detached(tree: Path) -> bool:
    """True when *tree* has no symbolic HEAD: a detached checkout."""
    done = subprocess.run(["git", "-C", str(tree), "symbolic-ref", "-q", "HEAD"],
                          capture_output=True, text=True)
    return done.returncode != 0


def _worktrees(root: Path) -> list[str]:
    return _git(root, "worktree", "list", "--porcelain").splitlines()


# ── the steps ────────────────────────────────────────────────────────────────
def test_build_steps_is_the_four_checks_in_order_without_any_parallelism_flag():
    steps = basecheck.build_steps()
    assert [s.name for s in steps] == ["tests", "tests_bugfix", "tiers", "clocks"]
    assert [s.command for s in steps] == [
        ["python3", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
        ["python3", "-m", "pytest", "tests_bugfix", "-q", "-p", "no:cacheprovider"],
        ["python3", "scripts/sync_test_tiers.py", "--check"],
        ["python3", "scripts/check_test_clocks.py", "--check"],
    ]
    for step in steps:
        assert "-n" not in step.command and "--dist" not in step.command


# ── a clean checkout ─────────────────────────────────────────────────────────
def test_the_steps_run_in_order_in_the_worktree(repo, monkeypatch, capsys):
    steps = _install(good_steps(), monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, _, _ = _check(capsys)
    assert code == 0
    tree = str(repo.root / ".arena" / "base-check" / repo.head[:12])
    assert [c["cwd"] for c in rec.calls] == [tree] * 4
    assert [c["line"] for c in rec.calls] == [s.command for s in steps]


def test_a_step_sees_the_clean_checkout_not_the_operators_tree(repo, monkeypatch, capsys):
    _install(marker_steps(), monkeypatch)
    _write(repo.root / "marker.txt", "uncommitted\n")
    _write(repo.root / "untracked.txt", "only in the working tree\n")
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 1
    rows = json.loads(out)
    assert rows[0]["summary"] == "marker=dirty; untracked=False"


def test_the_worktree_is_checked_out_detached_at_the_sha(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / "marker.txt", "uncommitted\n")
    code, out, _ = _check(capsys, "--keep")
    tree = repo.root / ".arena" / "base-check" / repo.head[:12]
    assert code == 0 and tree.is_dir()
    assert _git(tree, "rev-parse", "HEAD") == repo.head
    assert _detached(tree)
    assert tree.joinpath("marker.txt").read_text(encoding="utf-8").strip() == "dirty"
    assert not tree.joinpath("untracked.txt").exists()


# ── the rows and the verdict ─────────────────────────────────────────────────
def test_all_green_exits_zero_and_prints_four_columns_and_the_verdict(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    code, out, err = _check(capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0].split() == ["step", "ok", "seconds", "summary"]
    assert len(lines) == 6
    assert lines[-1] == f"base {repo.head[:12]}: ok"
    assert [line.split(None, 1)[0] for line in lines[1:5]] == [
        "tests", "tests_bugfix", "tiers", "clocks"]


def test_json_is_the_rows_alone_with_sha_and_cached(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 0 and len(out.splitlines()) == 1
    rows = json.loads(out)
    assert len(rows) == 4
    for row in rows:
        assert set(row) == {"step", "ok", "seconds", "summary", "sha", "cached"}
        assert row["sha"] == repo.head and row["cached"] is False
        assert isinstance(row["ok"], bool) and isinstance(row["seconds"], (int, float))
    assert rows[0]["summary"] == "3 passed in 0.01s"


def test_a_red_step_does_not_stop_the_later_steps_and_exits_one(repo, monkeypatch, capsys):
    _install(red_steps(), monkeypatch)
    rec = Recorder(codes=[0, 1, 0, 0])
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys)
    assert code == 1
    assert [c["line"] for c in rec.calls] == [s.command for s in red_steps()]
    assert out.splitlines()[-1] == f"base {repo.head[:12]}: FAILED (tests_bugfix)"


def test_the_failed_test_names_are_in_the_row_and_in_the_table(repo, monkeypatch, capsys):
    _install(red_steps(), monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 1
    rows = json.loads(out)
    assert [r["ok"] for r in rows] == [True, False, True, True]
    assert rows[1]["summary"] == "FAILED tests/test_a.py::test_one"
    code, out, _ = _check(capsys, "-o", "table")
    assert code == 1 and "FAILED tests/test_a.py::test_one" in out


def test_the_summary_is_the_last_non_empty_line_cut_to_120(repo, monkeypatch, capsys):
    _install([basecheck.Step("tests",
                             [sys.executable, "-c", "print('   '); print('x' * 300)"])],
             monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 0 and json.loads(out)[0]["summary"] == "x" * 120


def test_a_failed_pytest_step_lists_up_to_five_of_its_failed_tests(repo, monkeypatch, capsys):
    _install([basecheck.Step("tests_bugfix", [sys.executable, "-c", FAILED_OUT], pytest=True)],
             monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 1
    parts = json.loads(out)[0]["summary"].split("; ")
    assert parts[0] == "9 failed in 0.01s"
    assert len(parts) == 6
    assert parts[1] == "FAILED tests/t.py::test_0" and parts[5] == "FAILED tests/t.py::test_4"


def test_a_failed_line_that_is_the_last_line_is_said_once(repo, monkeypatch, capsys):
    body = ("import sys; print('FAILED tests/t.py::test_one'); "
            "print('FAILED tests/t.py::test_two'); sys.exit(1)")
    _install([basecheck.Step("tests", [sys.executable, "-c", body], pytest=True)], monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 1
    assert json.loads(out)[0]["summary"] == "FAILED tests/t.py::test_two; FAILED tests/t.py::test_one"


def test_only_the_two_pytest_steps_list_failed_tests(repo, monkeypatch, capsys):
    """A stand-in command on a `tests` step is still a pytest step; `tiers` never is."""
    assert [(step.name, step.pytest) for step in basecheck.build_steps()] == [
        ("tests", True), ("tests_bugfix", True), ("tiers", False), ("clocks", False)]
    _install([basecheck.Step("tiers", [sys.executable, "-c", FAILED_OUT])], monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 1 and json.loads(out)[0]["summary"] == "9 failed in 0.01s"


# ── the cache ────────────────────────────────────────────────────────────────
def test_a_green_base_stores_only_its_own_pass(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    code, _, _ = _check(capsys)
    assert code == 0
    cache = _cache(repo)
    assert list(cache) == [repo.head]
    entry = cache[repo.head]
    assert entry["ok"] is True and isinstance(entry["at"], int)
    assert [s["step"] for s in entry["steps"]] == ["tests", "tests_bugfix", "tiers", "clocks"]
    assert all(set(s) == {"step", "ok", "seconds", "summary"} and s["ok"] for s in entry["steps"])


def test_a_second_run_prints_cached_runs_no_step_and_exits_zero(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    assert _check(capsys)[0] == 0
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys)
    assert code == 0 and rec.calls == []
    assert "cached" in out and f"base {repo.head[:12]}: ok" in out


def test_json_on_a_cache_hit_carries_cached_true(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _check(capsys)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 0 and rec.calls == []
    rows = json.loads(out)
    assert len(rows) == 4 and all(r["cached"] is True and r["sha"] == repo.head for r in rows)


def test_a_cached_row_without_a_summary_is_just_cached(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / ".arena" / "base-check.json", json.dumps({repo.head: _pass_entry()}))
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 0 and rec.calls == []
    assert [r["summary"] for r in json.loads(out)] == ["s (cached)", "(cached)"]


def test_force_ignores_the_cache(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _check(capsys)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys, "--force")
    assert code == 0 and len(rec.calls) == 4 and "cached" not in out


def test_a_red_base_is_never_cached(repo, monkeypatch, capsys):
    _install(red_steps(), monkeypatch)
    code, _, _ = _check(capsys)
    assert code == 1 and not (repo.root / ".arena" / "base-check.json").exists()


def test_a_cache_entry_for_another_sha_is_not_a_hit(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / ".arena" / "base-check.json", json.dumps({"0" * 40: _pass_entry()}))
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys)
    assert code == 0 and len(rec.calls) == 4 and "cached" not in out


def test_a_stored_red_entry_is_no_hit(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / ".arena" / "base-check.json",
           json.dumps({repo.head: {"at": 1, "ok": False, "steps": []}}))
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys)
    assert code == 0 and len(rec.calls) == 4 and "cached" not in out


def test_a_mangled_entry_is_no_hit(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / ".arena" / "base-check.json",
           json.dumps({repo.head: {"at": 1, "ok": True, "steps": "not a list"}}))
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, _ = _check(capsys)
    assert code == 0 and len(rec.calls) == 4 and "cached" not in out


@pytest.mark.parametrize("text", ["", "not json {", "[]", "{}", '{"sha": []}', '{"sha": "no"}'])
def test_a_missing_empty_or_garbage_cache_is_an_empty_cache(tmp_path, text):
    if text:
        _write(tmp_path / ".arena" / "base-check.json", text)
    assert basecheck.read_cache(tmp_path) == {}


def test_a_garbage_cache_is_rewritten_on_the_next_pass_and_leaves_no_tmp(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / ".arena" / "base-check.json", "not json {")
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, _, _ = _check(capsys)
    assert code == 0 and len(rec.calls) == 4
    assert list(_cache(repo)) == [repo.head]
    assert not list((repo.root / ".arena").glob("*.tmp"))


def test_write_cache_is_atomic_and_leaves_no_tmp(tmp_path):
    assert basecheck.write_cache(tmp_path, {"abc": {}}) is True
    assert basecheck.read_cache(tmp_path) == {"abc": {}}
    assert not list((tmp_path / ".arena").glob("*.tmp"))


def test_a_cache_that_cannot_be_written_is_fail_open(tmp_path):
    (tmp_path / ".arena").write_text("a file, not a directory\n", encoding="utf-8")
    assert basecheck.write_cache(tmp_path, {"abc": {}}) is False
    assert not list(tmp_path.rglob("*.tmp"))


# ── refusals ─────────────────────────────────────────────────────────────────
def test_an_unresolvable_ref_is_a_refusal_that_writes_nothing(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, err = _check(capsys, "--ref", "no-such-ref")
    assert code == 2 and out == "" and rec.calls == []
    lines = err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("arena:") and "no-such-ref" in lines[0]
    assert not (repo.root / ".arena").exists()


def test_a_directory_that_is_not_a_git_checkout_is_a_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, err = _check(capsys)
    assert code == 2 and out == "" and rec.calls == []
    lines = err.splitlines()
    assert len(lines) == 1 and lines[0].startswith("arena:") and "not a git checkout" in lines[0]
    assert not (tmp_path / ".arena").exists()


def test_a_failed_worktree_add_is_a_refusal_that_caches_nothing(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    real = basecheck.gitref.git

    def broken(*args, **kwargs):
        if "worktree" in args and "add" in args:
            raise basecheck.gitref.GitRefError("git worktree add: boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(basecheck.gitref, "git", broken)
    code, out, err = _check(capsys)
    assert code == 2 and out == ""
    assert len(err.splitlines()) == 1 and "cannot check out" in err
    assert "git worktree add" in err
    assert not (repo.root / ".arena" / "base-check.json").exists()


def test_a_bad_argument_is_exit_two_and_one_line(repo, capsys):
    code, out, err = _check(capsys, "--ref")
    assert code == 2 and out == ""
    assert len(err.splitlines()) == 1 and err.startswith("arena: ")


# ── the default ref ──────────────────────────────────────────────────────────
def test_the_default_ref_is_the_profiles_branch_else_head(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    code, out, _ = _check(capsys, "--dry-run")
    assert code == 0 and out.splitlines()[0] == repo.head  # the fixture's `branch = HEAD`
    (repo.root / "contest.ini").write_text(INI_BRANCH, encoding="utf-8")
    code, out, _ = _check(capsys, "--dry-run")
    assert code == 0 and out.splitlines()[0] == repo.first  # the profile's `branch`
    (repo.root / "contest.ini").write_text(INI_NO_BRANCH, encoding="utf-8")
    code, out, _ = _check(capsys, "--dry-run")
    assert code == 0 and out.splitlines()[0] == repo.head  # no `branch` key: HEAD
    code, out, _ = _check(capsys, "--dry-run", "--ref", repo.first)
    assert code == 0 and out.splitlines()[0] == repo.first  # --ref wins


# ── the worktree's life ──────────────────────────────────────────────────────
def test_the_worktree_is_gone_after_a_green_and_after_a_red_run(repo, monkeypatch, capsys):
    tree = repo.root / ".arena" / "base-check" / repo.head[:12]
    _install(good_steps(), monkeypatch)
    code, _, _ = _check(capsys)
    assert code == 0 and not tree.exists()
    assert _worktrees(repo.root)[0] == f"worktree {repo.root}"
    assert not any("base-check" in line for line in _worktrees(repo.root))
    _install(red_steps(), monkeypatch)
    code, _, _ = _check(capsys, "--force")
    assert code == 1 and not tree.exists()
    assert _worktrees(repo.root)[0] == f"worktree {repo.root}"
    assert not any("base-check" in line for line in _worktrees(repo.root))


def test_keep_leaves_the_worktree_and_names_it(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    code, out, _ = _check(capsys, "--keep")
    tree = repo.root / ".arena" / "base-check" / repo.head[:12]
    assert code == 0 and str(tree) in out and tree.is_dir()


def test_a_leftover_tree_of_the_same_sha_is_replaced(repo, monkeypatch, capsys):
    tree = repo.root / ".arena" / "base-check" / repo.head[:12]
    tree.mkdir(parents=True)
    _write(tree / "junk.txt", "left by a crash\n")
    _write(repo.root / "marker.txt", "uncommitted\n")
    _install(marker_steps(), monkeypatch)
    code, out, _ = _check(capsys, "-o", "json")
    assert code == 1 and json.loads(out)[0]["summary"] == "marker=dirty; untracked=False"
    assert not tree.joinpath("junk.txt").exists()
    assert not any("base-check" in line for line in _worktrees(repo.root))


def test_a_registered_leftover_tree_is_replaced_too(repo, monkeypatch, capsys):
    tree = repo.root / ".arena" / "base-check" / repo.head[:12]
    _git(repo.root, "worktree", "add", "-q", "--detach", str(tree), repo.head)
    _install(good_steps(), monkeypatch)
    code, _, _ = _check(capsys)
    assert code == 0 and not tree.exists()
    assert _worktrees(repo.root)[0] == f"worktree {repo.root}"
    assert not any("base-check" in line for line in _worktrees(repo.root))


# ── --dry-run ────────────────────────────────────────────────────────────────
def test_dry_run_prints_the_sha_the_worktree_and_the_four_commands_and_runs_nothing(
        repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, out, err = _check(capsys, "--dry-run")
    assert code == 0 and err == "" and rec.calls == []
    lines = out.splitlines()
    assert len(lines) == 6
    assert lines[0] == repo.head
    assert lines[1] == str(repo.root / ".arena" / "base-check" / repo.head[:12])
    assert lines[2:] == [" ".join(s.command) for s in good_steps()]
    assert not (repo.root / ".arena").exists()


def test_dry_run_neither_reads_nor_writes_the_cache(repo, monkeypatch, capsys):
    _install(good_steps(), monkeypatch)
    _write(repo.root / ".arena" / "base-check.json", json.dumps({repo.head: _pass_entry()}))
    before = (repo.root / ".arena" / "base-check.json").read_text(encoding="utf-8")
    code, out, _ = _check(capsys, "--dry-run")
    assert code == 0 and len(out.splitlines()) == 6 and "cached" not in out
    assert (repo.root / ".arena" / "base-check.json").read_text(encoding="utf-8") == before


# ── the seams ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("profile,want", [
    ({"branch": "integration"}, "integration"),
    ({"branch": "  main  "}, "main"),
    ({"branch": ""}, "HEAD"),
    ({}, "HEAD"),
    (None, "HEAD"),
    ({"branch": 7}, "HEAD"),
])
def test_default_ref_takes_the_branch_else_head_and_is_never_empty(profile, want):
    assert basecheck.default_ref(profile) == want


def test_verdict_names_every_red_step():
    sha = "a" * 40
    assert basecheck.verdict(sha, [{"step": "tests", "ok": True}]) == f"base {'a' * 12}: ok"
    assert basecheck.verdict(
        sha, [{"step": "tests", "ok": True}, {"step": "tests_bugfix", "ok": False},
              {"step": "tiers", "ok": True}, {"step": "clocks", "ok": False}]
    ) == f"base {'a' * 12}: FAILED (tests_bugfix, clocks)"


def test_a_step_that_cannot_be_started_is_a_failed_row(tmp_path):
    def boom(line, **kwargs):
        raise OSError("no python3 on this box")

    rows = basecheck.run_steps(tmp_path,
                               [basecheck.Step("tests", ["python3", "-c", "x"])],
                               runner=boom)
    assert rows[0]["step"] == "tests" and rows[0]["ok"] is False
    assert rows[0]["summary"] == "no python3 on this box"
    assert isinstance(rows[0]["seconds"], float)


def test_the_two_pytest_steps_are_separate_subprocesses_without_a_parallelism_flag(
        repo, monkeypatch, capsys):
    steps = basecheck.build_steps()          # the real four command lines
    _install(steps, monkeypatch)
    rec = Recorder()
    monkeypatch.setattr(basecheck, "RUN", rec)
    code, _, _ = _check(capsys)
    assert code == 0 and len(rec.calls) == 4
    pytest_runs = [c["line"] for c in rec.calls if "pytest" in c["line"]]
    assert len(pytest_runs) == 2
    assert all("-p" in line and "no:cacheprovider" in line for line in pytest_runs)
    assert all("-n" not in c["line"] and "--dist" not in c["line"] for c in rec.calls)
