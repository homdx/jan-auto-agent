"""tests/test_collect_manifest.py — COLLECT-2.

* file content hash changes when the file changes.
* `is_fresh` is False when a tracked file is modified, a new file is added,
  or a tracked file is removed.
* `is_fresh` is True for a clean/unchanged tree, even at the same git SHA.
"""

import subprocess
from pathlib import Path

import pytest

from tools.collect.manifest import (
    Manifest,
    build_manifest,
    discover_files,
    get_git_sha,
    hash_file,
    is_dirty,
    is_fresh,
    read_manifest,
    refresh_manifest,
    write_manifest,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (tmp_path / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    _init_repo(tmp_path)
    return tmp_path


# ── hashing ───────────────────────────────────────────────────────────────


def test_hash_file_changes_when_content_changes(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    h1 = hash_file(f)
    f.write_text("a = 2\n")
    h2 = hash_file(f)
    assert h1 != h2


def test_hash_file_stable_for_unchanged_content(tmp_path: Path):
    f = tmp_path / "x.py"
    f.write_text("a = 1\n")
    assert hash_file(f) == hash_file(f)


def test_discover_files_finds_all_py_files(mini_repo: Path):
    files = discover_files(mini_repo)
    assert files == ["pkg/a.py", "pkg/b.py"]


def test_discover_files_skips_git_dir(mini_repo: Path):
    files = discover_files(mini_repo)
    assert not any(f.startswith(".git/") for f in files)


# ── manifest build ───────────────────────────────────────────────────────


def test_build_manifest_records_git_sha_and_clean_tree(mini_repo: Path):
    manifest = build_manifest(mini_repo)
    assert manifest.git_sha == get_git_sha(mini_repo)
    assert manifest.git_sha is not None
    assert manifest.dirty is False
    assert set(manifest.file_hashes) == {"pkg/a.py", "pkg/b.py"}


def test_build_manifest_detects_dirty_tree(mini_repo: Path):
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 999\n")
    manifest = build_manifest(mini_repo)
    assert manifest.dirty is True
    assert is_dirty(mini_repo) is True


def test_manifest_json_roundtrip(mini_repo: Path, tmp_path: Path):
    manifest = build_manifest(mini_repo)
    out = tmp_path / "collect_manifest.json"
    write_manifest(manifest, out)
    restored = read_manifest(out)
    assert restored == manifest


# ── freshness ────────────────────────────────────────────────────────────


def test_is_fresh_true_for_unchanged_tree(mini_repo: Path):
    manifest = build_manifest(mini_repo)
    assert is_fresh(manifest, mini_repo) is True


def test_is_fresh_true_even_at_same_sha_after_rebuild(mini_repo: Path):
    """Rebuilding the manifest (new timestamp) on the same, unchanged tree
    must still compare fresh — freshness is a content question, not a
    timestamp question."""
    m1 = build_manifest(mini_repo)
    m2 = build_manifest(mini_repo)
    assert m1.git_sha == m2.git_sha
    assert is_fresh(m1, mini_repo) is True
    assert is_fresh(m2, mini_repo) is True


def test_is_fresh_false_when_tracked_file_modified(mini_repo: Path):
    manifest = build_manifest(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 'changed'\n")
    assert is_fresh(manifest, mini_repo) is False


def test_is_fresh_false_when_file_added(mini_repo: Path):
    manifest = build_manifest(mini_repo)
    (mini_repo / "pkg" / "c.py").write_text("def c():\n    return 3\n")
    assert is_fresh(manifest, mini_repo) is False

    # A refresh picks up the new file and is fresh again against the tree.
    refreshed = refresh_manifest(mini_repo, manifest)
    assert set(refreshed.file_hashes.keys()) == {"pkg/a.py", "pkg/b.py", "pkg/c.py"}
    assert is_fresh(refreshed, mini_repo) is True


def test_is_fresh_false_when_tracked_file_removed(mini_repo: Path):
    manifest = build_manifest(mini_repo)
    (mini_repo / "pkg" / "b.py").unlink()
    assert is_fresh(manifest, mini_repo) is False


def test_is_fresh_false_when_manifest_hash_stale_relative_to_new_build(mini_repo: Path):
    stale = build_manifest(mini_repo)
    (mini_repo / "pkg" / "a.py").write_text("def a():\n    return 'v2'\n")
    fresh_build = build_manifest(mini_repo)
    assert is_fresh(stale, mini_repo) is False
    assert is_fresh(fresh_build, mini_repo) is True
