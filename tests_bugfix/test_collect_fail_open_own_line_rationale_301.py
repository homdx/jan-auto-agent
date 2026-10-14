"""tests_bugfix/test_collect_fail_open_own_line_rationale_301.py

Bug: `tools.collect.registries._handler_rationale` looked only at the
`except` line and at the first line of the handler's body, so the shape most
of this repo's own documented handlers use — a comment on its own line
directly under the `except` — was never seen and the FAIL_OPEN_REGISTRY row
came out with `rationale=None`. 7 of the 178 fail-open sites in `tools/` use
that shape (e.g. `tools/agent_trace.py:192`, `tools/backoff.py:221`), so the
"why" column of the registry was systematically empty for them.

Field report: HARDEN-4 D5, reproduced before the fix.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.registries import build_fail_open_registry
from tools.collect.scanner import scan_repo

SOURCE = '''def inline(path):
    try:
        open(path).read()
    except OSError:  # inline why
        pass


def own_line(path):
    try:
        open(path).read()
    except OSError:
        # own-line why
        pass


def no_comment(path):
    try:
        open(path).read()
    except OSError:
        pass


def far_away(path):
    try:
        open(path).read()
    except OSError:
        pass
        # far away, not about this site
'''


def _registry(tmp_path: Path) -> dict:
    (tmp_path / "m.py").write_text(SOURCE, encoding="utf-8")
    modules = scan_repo(tmp_path)
    return {
        entry.location.rpartition(":")[-1]: entry.rationale
        for entry in build_fail_open_registry(modules, root=tmp_path)
    }


def test_own_line_comment_is_seen(tmp_path):
    by_line = _registry(tmp_path)
    assert by_line["11"] == "own-line why"


def test_inline_comment_is_unchanged(tmp_path):
    by_line = _registry(tmp_path)
    assert by_line["4"] == "inline why"


def test_a_handler_with_no_comment_still_has_none(tmp_path):
    by_line = _registry(tmp_path)
    assert by_line["19"] is None


def test_a_comment_several_lines_down_is_not_claimed(tmp_path):
    by_line = _registry(tmp_path)
    assert by_line["26"] is None
