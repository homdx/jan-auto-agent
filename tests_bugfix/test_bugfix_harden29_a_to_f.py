"""tests_bugfix/test_bugfix_harden29_a_to_f.py

Pinned regression guards for HARDEN-29 (ticket 327), the bugs an outside review
of 2026-11-01 reproduced after HARDEN-28, and the two the round's judging found:

* A  workspace retention deleted the source repository and an unrelated sibling
     when ``workspace_root`` was shared (`Executor._prune_old_workspaces`).
* B  context retrieval read a file a symlink points to outside the repository.
* C  context retrieval read a stale ``.agent/workspace`` mirror before the source.
* D  a constant edit in a file shown in part was written next to the old line,
     which then won (`Coder._splice_partial_reply`).
* E  two base checks of one commit deleted each other's checkout; two checks of
     two commits finishing together lost one pass from the cache (judging).
* F  the test cache answered ``FEATURE_X=1 pytest`` with a plain run; and, found
     in judging, ``export X=1; X=$(date); pytest`` with the ``X=1`` run, and
     ``PYTHONPATH=src pytest`` with a plain run.

Each test fails on ad0ea09 (the round's base) and passes after the fix, but the two
guards (a link inside the repository, the plain key) which pass on both.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.arena.basecheck import Step, run_base_check
from tools.auto.coder import Coder
from tools.auto.context_broker import ContextBroker
from tools.auto.executor import Executor
from tools.contest.testcache import PytestRun, Summary, parse_pytest
from tools.contest.testcache_glue import TestCacheGate, _run_from_key, run_key
from tools.contest.testcache_store import tree_fingerprint


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "tracked.txt").write_text("source\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "base")
    return root


# ── A ─────────────────────────────────────────────────────────────────────────


def test_a_shared_root_keeps_source_and_sibling(tmp_path):
    """The review's case: a workspace root that is the source's parent."""
    src = _repo(tmp_path / "source")
    sibling = tmp_path / "unrelated-project"
    sibling.mkdir()
    (sibling / "precious.txt").write_text("user data")
    for task in ("AUTO-T1", "AUTO-T2"):
        assert Executor(src, workspace_root=tmp_path, max_retained_workspaces=1).run(
            {"id": task, "acceptance_check": "true"}).passed
    assert (src / "tracked.txt").exists()
    assert (sibling / "precious.txt").read_text() == "user data"


def test_a_ancestor_of_source_survives(tmp_path):
    """A directory that contains the source is never pruned."""
    src = _repo(tmp_path / "outer" / "source")
    for task in ("AUTO-T1", "AUTO-T2"):
        Executor(src, workspace_root=tmp_path, max_retained_workspaces=1).run(
            {"id": task, "acceptance_check": "true"})
    assert (src / "tracked.txt").exists()


def test_a_own_workspaces_are_still_trimmed(tmp_path):
    """Retention still trims the executor's own workspaces, and leaves a foreign one."""
    src = _repo(tmp_path / "source")
    ws = tmp_path / "ws"
    (ws / "notes").mkdir(parents=True)
    (ws / "notes" / "keep.txt").write_text("mine")
    for task in ("AUTO-T1", "AUTO-T2", "AUTO-T3"):
        Executor(src, workspace_root=ws, max_retained_workspaces=1).run(
            {"id": task, "acceptance_check": "true"})
        time.sleep(0.02)
    assert sorted(p.name for p in ws.iterdir() if p.is_dir()) == ["AUTO-T3", "notes"]


# ── B / C ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("link,symbol,targets", [
    ("chapter_9.md", "chapter_9", ["chapter_1.md"]),
    ("prologue.md", "prologue", ["chapter_1.md"]),
])
def test_b_prose_link_out_of_the_repo_is_not_read(tmp_path, link, symbol, targets):
    """The chapter and stem fallbacks check the resolved path before reading."""
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "private.md").write_text("PRIVATE OUTSIDE CONTENT")
    (root / link).symlink_to(tmp_path / "private.md")
    result = ContextBroker().resolve([symbol], targets, root)
    assert "PRIVATE" not in "".join(result.values())


@pytest.mark.parametrize("where", ["project", "target"])
def test_b_code_link_out_of_the_repo_is_not_read(tmp_path, where):
    """The project fallback and the target-file read check the resolved path too."""
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "helper.py").write_text('def secret_helper():\n    return "OUTSIDE_SECRET"\n')
    name = "helper.py" if where == "project" else "main.py"
    (root / name).symlink_to(tmp_path / "helper.py")
    result = ContextBroker().resolve(["secret_helper"], ["main.py"], root)
    assert "OUTSIDE_SECRET" not in "".join(result.values())


def test_b_link_inside_the_repo_still_resolves(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.md").write_text("# Prologue\nINSIDE TEXT\n")
    (root / "prologue.md").symlink_to(root / "docs" / "notes.md")
    assert "INSIDE TEXT" in ContextBroker().resolve(["prologue"], ["chapter_1.md"], root)["prologue"]


@pytest.mark.parametrize("name,symbol,targets,text", [
    ("helper.py", "helper", ["main.py"], 'def helper():\n    return "{}"\n'),
    ("chapter_9.md", "chapter_9", ["chapter_1.md"], "{} CHAPTER\n"),
    ("prologue.md", "prologue", ["chapter_1.md"], "{} PROLOGUE\n"),
])
def test_c_agent_mirror_is_never_a_source(tmp_path, name, symbol, targets, text):
    """The live file wins, and a file only the mirror has is not found at all."""
    root = tmp_path / "repo"
    stale = root / ".agent" / "workspace" / "AUTO-T1"
    stale.mkdir(parents=True)
    (stale / name).write_text(text.format("STALE"))
    assert "STALE" not in "".join(ContextBroker().resolve([symbol], targets, root).values())
    (root / name).write_text(text.format("CURRENT"))
    got = ContextBroker().resolve([symbol], targets, root)[symbol]
    assert "CURRENT" in got and "STALE" not in got


# ── D ─────────────────────────────────────────────────────────────────────────

_ORIGINAL = (
    "import os\n\nLIMIT = 10\n\ndef first():\n    return LIMIT\n\n"
    + "".join(f"def hidden{i}():\n    return {i} # " + "pad " * 20 + "\n\n" for i in range(8))
)


def _write(tmp_path, reply, original=_ORIGINAL):
    (tmp_path / "m.py").write_text(original)
    coder = Coder(configparser.ConfigParser(), "http://localhost:1", "test", "m")
    coder._max_file_chars = 140
    assert "not included" in coder._read_file_contents(["m.py"], tmp_path)
    written, error = coder._write_files([{"path": "m.py", "content": reply}], tmp_path, "T",
                                        allowed_paths=frozenset({"m.py"}))
    text = (tmp_path / "m.py").read_text()
    namespace: dict = {}
    exec(compile(text, "m.py", "exec"), namespace)
    return written, error, text, namespace


def test_d_complete_reply_constant_edit_lands(tmp_path):
    """The review's case: LIMIT = 10 -> 20 in a complete reply evaluates to 20."""
    written, error, text, ns = _write(tmp_path, _ORIGINAL.replace("LIMIT = 10", "LIMIT = 20"))
    assert (written, error) == (["m.py"], "")
    assert ns["LIMIT"] == 20 and "LIMIT = 10" not in text


def test_d_partial_reply_constant_edit_lands_and_hidden_code_stays(tmp_path):
    reply = "import os\n\nLIMIT = 20\n\ndef first():\n    return LIMIT\n"
    _w, error, text, ns = _write(tmp_path, reply)
    assert error == "" and ns["LIMIT"] == 20 and "LIMIT = 10" not in text
    assert all(f"hidden{i}" in ns for i in range(8))


def test_d_complete_reply_removing_the_assignment_removes_it(tmp_path):
    reply = _ORIGINAL.replace("LIMIT = 10\n\n", "").replace("return LIMIT", "return 5")
    _w, error, _text, ns = _write(tmp_path, reply)
    assert error == "" and "LIMIT" not in ns and ns["first"]() == 5


# ── E ─────────────────────────────────────────────────────────────────────────


def _args(ref="HEAD"):
    return SimpleNamespace(ref=ref, force=True, keep=False, dry_run=False, output="json")


def test_e_overlapping_checks_of_one_commit_both_pass(tmp_path, capsys):
    """The review's case: the second check no longer deletes the first one's checkout."""
    root = _repo(tmp_path / "repo")
    started, release = tmp_path / "started", tmp_path / "release"
    code = (f"from pathlib import Path\nimport time\nstarted = Path({str(started)!r})\n"
            f"release = Path({str(release)!r})\nif not started.exists():\n    started.touch()\n"
            "    deadline = time.monotonic() + 15\n"
            "    while not release.exists() and time.monotonic() < deadline:\n"
            "        time.sleep(0.01)\n"
            "assert Path('tracked.txt').exists(), 'active checkout was deleted'\n")
    steps = [Step("check", [sys.executable, "-c", code])]
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_base_check, root, _args(), None, steps)
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        second = pool.submit(run_base_check, root, _args(), None, steps)
        time.sleep(0.3)
        release.touch()
        assert (first.result(timeout=60), second.result(timeout=60)) == (0, 0)


def test_e_two_commits_finishing_together_both_reach_the_cache(tmp_path, capsys):
    """Found in judging: the per-commit lock left the shared cache's
    read-modify-write open, and one of two passes was lost."""
    for _ in range(4):
        root = _repo(tmp_path / f"repo{_}")
        first = _git(root, "rev-parse", "HEAD").stdout.strip()
        (root / "tracked.txt").write_text("two\n")
        _git(root, "commit", "-qam", "two")
        steps = [Step("check", [sys.executable, "-c", "import time; time.sleep(0.5)"])]
        with ThreadPoolExecutor(max_workers=2) as pool:
            rcs = [f.result(timeout=60) for f in (
                pool.submit(run_base_check, root, _args(first), None, steps),
                pool.submit(run_base_check, root, _args("HEAD"), None, steps))]
        assert rcs == [0, 0]
        cache = json.loads((root / ".arena" / "base-check.json").read_text())
        assert len(cache) == 2, list(cache)


# ── F ─────────────────────────────────────────────────────────────────────────


def _seeded(tmp_path, command):
    root = _repo(tmp_path / "repo")
    gate = TestCacheGate(tmp_path / "cache.jsonl", agent="t")
    gate.cache.record(run_key(parse_pytest(command)[0]), tree_fingerprint(root),
                      dataclasses.asdict(Summary(passed=1, raw_line="1 passed in 0.01s")),
                      0, 0.01, "t")
    return gate, root


def _ask(gate, root, command):
    return gate.ask({"permission": "bash", "metadata": {"command": command},
                     "tool": {"callID": "q"}}, root)


@pytest.mark.parametrize("command", [
    "FEATURE_X=1 pytest tests -q",
    "env FEATURE_X=1 pytest tests -q",
    "export FEATURE_X=1 && pytest tests -q",
    "export FEATURE_X=1; pytest tests -q",
    "PYTHONPATH=src pytest tests -q",
])
def test_f_assignment_is_not_answered_by_a_plain_run(tmp_path, command):
    gate, root = _seeded(tmp_path, "pytest tests -q")
    assert _ask(gate, root, command) is None


def test_f_other_value_misses_and_same_value_hits(tmp_path):
    gate, root = _seeded(tmp_path, "FEATURE_X=1 pytest tests -q")
    assert _ask(gate, root, "FEATURE_X=2 pytest tests -q") is None
    assert "1 passed" in (_ask(gate, root, "FEATURE_X=1 pytest tests -q") or "")


@pytest.mark.parametrize("command", [
    "FEATURE_X=$(date) pytest tests -q",
    "FEATURE_X=$HOME pytest tests -q",
    "unset FEATURE_X && pytest tests -q",
    "export FEATURE_X=1; FEATURE_X=$(date); pytest tests -q",
    "env -i pytest tests -q",
])
def test_f_unreadable_environment_is_not_cached(command):
    assert parse_pytest(command) == []


def test_f_key_of_a_plain_run_is_unchanged_and_an_env_key_round_trips():
    """No assignment: the key it always had. With one: read back as the same run
    (the env no longer lands in the ``cwd`` slot)."""
    assert run_key(parse_pytest("pytest tests -q")[0]) == '[["tests"], ["-q"]]'
    run = parse_pytest("FEATURE_X=1 pytest tests -q")[0]
    back = _run_from_key(run_key(run))
    assert back == run and back.cwd == ""
    assert isinstance(PytestRun().cwd, str)
