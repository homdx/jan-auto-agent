"""tests_bugfix/test_claimcheck_non_utf8_symbols_265.py — found while building CC-3 (ticket 265).

A ``.py`` file with a byte that is not UTF-8 reaches the resolver as text with a lone surrogate
(``read_utf8`` keeps the bytes). ``ast.parse`` refuses it, so every definition of the file was
skipped and a claim naming ``f_one`` there resolved as *not found* — a symbol that exists, reported
as missing. The resolver now parses the text with U+FFFD for such bytes; line numbers are the same.
"""
from __future__ import annotations

from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors


def test_symbol_in_a_file_with_a_non_utf8_byte_is_found(tmp_path):
    (tmp_path / "bad.py").write_bytes(b"import os\n\ndef f_one():\n    return '\xff'\n")
    view = PathRepoView(tmp_path)
    (r,) = resolve_anchors(extract_anchors("`f_one` returns a byte."), view)
    assert (r.found, r.path, r.qualname, r.lines) == (True, "bad.py", "f_one", (3, 4))


def test_a_file_that_does_not_parse_is_still_skipped(tmp_path):
    (tmp_path / "bad.py").write_bytes(b"def f_one(:\n    pass\n")
    view = PathRepoView(tmp_path)
    (r,) = resolve_anchors(extract_anchors("`f_one` returns a byte."), view)
    assert r.found is False
