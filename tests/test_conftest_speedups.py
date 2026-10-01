"""FL-9 — the two suite-speed helpers in the root conftest.py stay honest.

`_tree_fingerprint` is the key of the cross-worker `scan_repo(ROOT)` cache, so
the one property that matters is that ANY change to the tree changes it (else a
stale scan could hide a real failure). `pytest_xdist_auto_num_workers` sizes
`-n auto` for a suite that mostly waits, not computes.
"""

from __future__ import annotations

import conftest


def _fp(monkeypatch, root):
    monkeypatch.setattr(conftest, "ROOT", root)
    return conftest._tree_fingerprint()


def test_fingerprint_is_stable_for_an_unchanged_tree(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    assert _fp(monkeypatch, tmp_path) == _fp(monkeypatch, tmp_path)


def test_fingerprint_changes_on_edit_add_and_delete(tmp_path, monkeypatch):
    a = tmp_path / "a.py"
    a.write_text("x = 1\n")
    base = _fp(monkeypatch, tmp_path)
    a.write_text("x = 22\n")                       # edit (size changes)
    edited = _fp(monkeypatch, tmp_path)
    assert edited != base
    (tmp_path / "b.py").write_text("")             # add
    added = _fp(monkeypatch, tmp_path)
    assert added != edited
    (tmp_path / "b.py").unlink()                   # delete
    assert _fp(monkeypatch, tmp_path) != added


def test_fingerprint_ignores_git_and_caches(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    base = _fp(monkeypatch, tmp_path)
    for d in (".git", "__pycache__", ".pytest_cache"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "noise").write_text("changes every run")
    assert _fp(monkeypatch, tmp_path) == base


def test_auto_workers_floor_and_override(monkeypatch):
    monkeypatch.delenv("JAN_TEST_WORKERS", raising=False)
    monkeypatch.setattr(conftest.os, "cpu_count", lambda: 1)
    assert conftest.pytest_xdist_auto_num_workers(None) == 4
    monkeypatch.setattr(conftest.os, "cpu_count", lambda: 16)
    assert conftest.pytest_xdist_auto_num_workers(None) == 16
    monkeypatch.setenv("JAN_TEST_WORKERS", "2")
    assert conftest.pytest_xdist_auto_num_workers(None) == 2
