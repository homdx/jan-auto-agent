"""tests_bugfix/test_collect_pack_body_header_dedupe.py — Bug 3.

`_pack_body(head, forms, rows, budget)` initialized its dedup set as
`seen = set(head)`, which meant a fact-row line that happened to be
byte-identical to a header line was silently dropped — the header's
copy in `seen` suppressed the fact row's own version in the body.

The fix changes `seen` to start empty (`seen = set()`). Dedup still
works between fact rows themselves (each row's own lines track in
`fresh_seen`, and `seen` is updated with `fresh` after a row passes
the budget check, exactly as before), but header lines no longer
suppress fact-row content.
"""

from __future__ import annotations

import pytest

from tools.auto.context_assembler import _pack_body

_HEAD = ["COLLECT MODEL (static facts, do not contradict):", "module: pkg/a.py"]


def _make_rows(*entries: tuple[str, str]):
    return tuple((name, lambda *a, **kw: body) for name, body in entries)


# ── Bug 3: fact-row line byte-matching a header must not be dropped ────


class TestFactRowLineNotDroppedAgainstHeader:
    def test_fact_row_line_matching_header_appears_in_body(self):
        """A line in a fact row that byte-matches a header line
        must appear in the body, not be silently suppressed by
        `seen = set(head)`."""
        forms = {"f": "COLLECT MODEL (static facts, do not contradict):\nfact line"}
        body, kept = _pack_body(_HEAD, forms, _make_rows(("f", forms["f"])), None)

        assert "f" in kept
        assert body.count("COLLECT MODEL (static facts, do not contradict):") == 1
        assert "fact line" in body

    def test_module_line_matching_fact_row_not_suppressed(self):
        """A fact row may legitimately reference the module path
        verbatim — that must not be suppressed."""
        forms = {"f": "module: pkg/a.py\nfact line"}
        body, kept = _pack_body(_HEAD, forms, _make_rows(("f", forms["f"])), None)

        assert "f" in kept
        assert body.count("module: pkg/a.py") == 1
        assert "fact line" in body

    def test_parse_error_line_matching_fact_row_not_suppressed(self):
        """Same for a parse_error header line."""
        head = _HEAD + ["parse_error: boom"]
        forms = {"f": "parse_error: boom\nfact line"}
        body, kept = _pack_body(head, forms, _make_rows(("f", forms["f"])), None)

        assert "f" in kept
        assert body.count("parse_error: boom") == 1
        assert "fact line" in body

    def test_budget_none_renders_all_rows(self):
        """budget=None renders every row in full — nothing is cut."""
        forms = {"f": "line one\nline two"}
        body, kept = _pack_body(_HEAD, forms, _make_rows(("f", forms["f"])), None)

        assert "f" in kept
        assert len(body) == 2
        assert "line one" in body
        assert "line two" in body


# ── Regression guards: dedup still works between fact rows ─────────────


class TestDedupStillWorksBetweenRows:
    def test_duplicate_line_across_rows_only_once(self):
        """A line appearing in two different fact rows appears only
        once in the body — the second occurrence is still deduped."""
        rows = _make_rows(("f1", "shared line\nfirst row"), ("f2", "shared line\nsecond row"))
        body, kept = _pack_body(_HEAD, {"f1": "shared line\nfirst row", "f2": "shared line\nsecond row"}, rows, None)

        # f2 is processed after f1; "shared line" is already in seen
        # from f1, so it must be skipped in f2
        assert body.count("shared line") == 1
        assert "second row" in body
        assert "first row" in body
        assert len(kept) == 2

    def test_duplicate_within_row_only_once(self):
        """Duplicate lines within a single row are still deduped."""
        forms = {"f": "dup\ndup\nunique"}
        body, kept = _pack_body(_HEAD, forms, _make_rows(("f", forms["f"])), None)

        assert body.count("dup") == 1
        assert body.count("unique") == 1

    def test_two_rows_different_lines_both_appear(self):
        """Two rows with distinct lines both appear in full."""
        rows = _make_rows(("f1", "first row line"), ("f2", "second row line"))
        body, kept = _pack_body(
            _HEAD, {"f1": "first row line", "f2": "second row line"}, rows, None
        )

        assert "first row line" in body
        assert "second row line" in body
        assert len(kept) == 2
