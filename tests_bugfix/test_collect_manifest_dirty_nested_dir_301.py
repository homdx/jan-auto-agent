"""tests_bugfix/test_collect_manifest_dirty_nested_dir_301.py

Bug: `manifest.is_dirty` excluded the collector's own output directory by
comparing each `git status --porcelain` path against that directory's
prefix. With a collect dir nested under an untracked parent (`out/collect`
and `out/` untracked) git collapses the whole untracked *directory* into one
`?? out/` entry — a path that is not under `out/collect/` — so the filter
kept it and `dirty` was True on every build. `--untracked-files=all` makes
git list the files instead, so the exclusion works for any layout.

Field report: HARDEN-4 D7, reproduced on a nested layout before the fix
(the effect is cosmetic today: the flag is only written into the manifest).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.collect.manifest import is_dirty


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "out" / "collect").mkdir(parents=True)
    (tmp_path / "out" / "collect" / "artifact.json").write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    return tmp_path


def test_a_nested_collect_dir_is_not_dirty(tmp_path):
    root = _repo(tmp_path)
    assert is_dirty(root, exclude_dir=root / "out" / "collect") is False


def test_a_really_untracked_source_file_is_still_dirty(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "b.py").write_text("y = 2\n", encoding="utf-8")
    assert is_dirty(root, exclude_dir=root / "out" / "collect") is True


def test_a_modified_tracked_file_is_still_dirty(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert is_dirty(root, exclude_dir=root / "out" / "collect") is True


def test_a_plain_collect_dir_layout_is_unchanged(tmp_path):
    root = _repo(tmp_path)
    (root / ".collect").mkdir()
    (root / ".collect" / "artifact.json").write_text("{}\n", encoding="utf-8")
    assert is_dirty(root, exclude_dir=root / ".collect") is False


def test_a_non_repo_directory_is_not_dirty(tmp_path):
    # git unavailable / not a repo: "nothing to report", not an error.
    assert is_dirty(tmp_path) is False
