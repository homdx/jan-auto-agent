"""tests_bugfix/test_claimcheck_named_file_265.py — found building CC-3 (ticket 265), on the real claims.

"`_commits_above` in `tools/contest/workspace.py`" resolved to the first file by path that defines
`_commits_above` (runner.py), so the evidence was the wrong function. A bare name defined in several
files is still the first by path (255's rule) — unless the claim also names a file that defines it.
"""
from __future__ import annotations

from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors


def _repo(tmp_path):
    for name in ("runner.py", "workspace.py", "zeta.py"):
        (tmp_path / name).write_text("def commits_above_x():\n    return 1\n")
    return PathRepoView(tmp_path)


def test_named_file_wins_over_the_first_by_path(tmp_path):
    view = _repo(tmp_path)
    syms = [r for r in resolve_anchors(extract_anchors("`commits_above_x()` in `workspace.py` returns 0."), view)
            if r.anchor.kind == "symbol"]
    assert [(r.found, r.path) for r in syms] == [(True, "workspace.py")]
    assert {c[0] for c in syms[0].candidates} == {"runner.py", "zeta.py"}


def test_without_a_named_file_the_first_by_path_stays(tmp_path):
    view = _repo(tmp_path)
    (r,) = resolve_anchors(extract_anchors("`commits_above_x()` returns 0."), view)
    assert r.path == "runner.py"


def test_a_named_file_that_does_not_define_it_changes_nothing(tmp_path):
    view = _repo(tmp_path)
    (tmp_path / "other.py").write_text("X = 1\n")
    syms = [r for r in resolve_anchors(extract_anchors("`commits_above_x()` in `other.py` returns 0."), PathRepoView(tmp_path))
            if r.anchor.kind == "symbol"]
    assert syms[0].path == "runner.py"
