"""tests/test_claimcheck_anchors_review.py — the anchor resolver after the review of 2026-10-12: a path
with a space before its first `/` or with no `/` at all, the files a live working directory lists
(no virtualenv, no node_modules, no symlinked mirror of tests/), and the named file's definition
over a same-named decoy.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tools.claimcheck.anchors import (PathRepoView, classify, extract_anchors, is_dangling,
                                      resolve_anchors)
from tools.claimcheck.model import Anchor


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def tree(tmp_path):
    _write(tmp_path, "my file.py", "def keep_two():\n    return 2\n")
    _write(tmp_path, "my dir/mod.py", "def mydir_fn():\n    return 1\n")
    _write(tmp_path, "renamed é.py", "def keep_three():\n    return 3\n")
    _write(tmp_path, "pkg/file.py", "def other_fn():\n    return 0\n")
    return tmp_path


def _resolved(claim, root):
    return resolve_anchors(extract_anchors(claim), PathRepoView(root))


# ── №11, №12: a space in the name ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("claim, path", [
    ("`my file.py` defines `keep_two`", "my file.py"),
    ('"my file.py" defines `keep_two`', "my file.py"),
    ("`renamed é.py` defines `keep_three`", "renamed é.py"),
    ("`my dir/mod.py` defines `mydir_fn`", "my dir/mod.py"),
    ('"my dir/mod.py" defines `mydir_fn`', "my dir/mod.py"),
    ("the module my dir/mod.py defines `mydir_fn`", "my dir/mod.py"),
])
def test_a_path_with_a_space_resolves_to_the_file_that_is_there(tree, claim, path):
    got = _resolved(claim, tree)
    paths = [r for r in got if r.anchor.kind == "path"]
    assert [(r.found, r.path) for r in paths] == [(True, path)]
    assert claim[paths[0].anchor.start:paths[0].anchor.end] == paths[0].anchor.text == path
    assert classify(claim, got) == "code" and not is_dangling(got)


def test_a_bare_name_with_a_space_is_found_when_the_plain_reading_is_not_a_file(tree):
    got = _resolved("my file.py defines keep_two", tree)
    assert [(r.found, r.path) for r in got if r.anchor.kind == "path"] == [(True, "my file.py")]


def test_an_unquoted_word_before_an_existing_file_is_not_taken(tree):
    """`see pkg/file.py`: the plain reading is a file, so the sentence's word stays a word."""
    got = _resolved("see pkg/file.py for `other_fn`", tree)
    assert [(r.anchor.text, r.path) for r in got if r.anchor.kind == "path"] == [("pkg/file.py", "pkg/file.py")]


def test_a_quoted_span_that_is_no_file_falls_back_to_the_plain_rule(tree):
    """CC-1's golden: `python3 scripts/run.sh` is a command and the path in it."""
    _write(tree, "scripts/run.sh", "echo\n")
    got = _resolved("`python3 scripts/run.sh` prints it", tree)
    assert [(r.anchor.text, r.found) for r in got if r.anchor.kind == "path"] == [("scripts/run.sh", True)]


def test_extraction_and_equality_are_what_they_were():
    """The longer readings ride along, outside equality: an anchor compares as it always did."""
    [a] = extract_anchors("`my file.py` defines it")
    assert (a.kind, a.text) == ("path", "file.py")
    assert a == Anchor("path", "file.py", a.start, a.end)
    assert a.wider == ((1, "my file.py", True),)


def test_a_missing_spaced_file_is_still_dangling_as_before(tree):
    got = _resolved("`gone dir/none.py` defines it", tree)
    assert not any(r.found for r in got if r.anchor.kind == "path")


# ── №10: the files of a live working directory ───────────────────────────────────────────────

def _decoys(root: Path) -> None:
    _write(root, "tools/llm.py", "def request_completion(url):\n    return url\n")
    _write(root, "tests/test_llm.py", "def request_completion(url):\n    return 'stub'\n")
    os.makedirs(root / ".smoke_tests", exist_ok=True)
    os.symlink("../tests/test_llm.py", root / ".smoke_tests" / "test_llm.py")
    _write(root, ".venv/pyvenv.cfg", "home = /usr\n")
    _write(root, ".venv/lib/site-packages/pkg/mod.py", "class Settings:\n    def load_config(self):\n        pass\n")
    _write(root, "node_modules/x/index.js", "module.exports = 1\n")
    _write(root, "src/app.py", "class Settings:\n    def load_config(self):\n        return {}\n")


@pytest.mark.parametrize("git", [False, True])
def test_vendored_trees_and_symlinked_mirrors_are_not_the_projects_files(tmp_path, git):
    _decoys(tmp_path)
    if git:
        run = lambda *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                        cwd=tmp_path, check=True, capture_output=True)
        run("init", "-q")
        (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        run("add", "-A")              # node_modules committed: still not ours
        run("commit", "-qm", "x")
        _write(tmp_path, "src/new.py", "x = 1\n")    # untracked, not ignored: listed
    files = PathRepoView(tmp_path).files()
    assert "src/app.py" in files and "tools/llm.py" in files and "tests/test_llm.py" in files
    assert not [f for f in files if f.startswith((".venv/", "node_modules/", ".smoke_tests/"))]
    if git:
        assert "src/new.py" in files


def test_the_cited_symbol_is_the_named_files_not_a_decoys(tmp_path):
    _decoys(tmp_path)
    got = _resolved("`request_completion` in `tools/llm.py` never times out", tmp_path)
    sym = next(r for r in got if r.anchor.kind == "symbol")
    assert (sym.path, sym.qualname) == ("tools/llm.py", "request_completion")
    got = _resolved("`Settings.load_config` in `src/app.py` reads nothing", tmp_path)
    sym = next(r for r in got if r.anchor.kind == "symbol")
    assert sym.path == "src/app.py"


def test_a_nested_stub_of_the_same_name_loses_to_the_named_files_function(tmp_path):
    """The decoy of this repository: a test's nested `def request_completion` sorted first."""
    _write(tmp_path, "tests/test_cli.py", "def test_x():\n    def request_completion(*a):\n        return ''\n")
    _write(tmp_path, "tools/llm.py", "def request_completion(url):\n    return url\n")
    got = _resolved("`request_completion` in `tools/llm.py` has the problem: it never times out", tmp_path)
    sym = next(r for r in got if r.anchor.kind == "symbol")
    assert (sym.path, sym.qualname) == ("tools/llm.py", "request_completion")
