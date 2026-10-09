"""tests_bugfix/test_arena_ini_line_breaks_199.py — Bug 6: _with_key must split on \\n only."""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.arena import models
from tools.contest import roster


def _read(text):
    parser = roster._new_parser()
    parser.read_string(text)
    return parser["arena.profile.p"]


def test_unicode_line_break_u2028_does_not_split_the_value():
    text = "[arena.profile.p]\nextra = v\u2028keep = 2\n"
    out = models._with_key(text, "p", "extra", "z")
    assert _read(out)["extra"] == "z"
    assert "keep" not in _read(out)


def test_unicode_line_break_u0085_does_not_split_the_value():
    text = "[arena.profile.p]\nnote = a\x85b\nkeep = 2\n"
    out = models._with_key(text, "p", "note", "z")
    assert _read(out)["note"] == "z"
    assert _read(out)["keep"] == "2"


def test_vertical_tab_does_not_split_the_value():
    text = "[arena.profile.p]\nnote = a\x0bb\nkeep = 2\n"
    out = models._with_key(text, "p", "note", "z")
    assert _read(out)["note"] == "z"
    assert _read(out)["keep"] == "2"


def test_form_feed_does_not_split_the_value():
    text = "[arena.profile.p]\nnote = a\x0cb\nkeep = 2\n"
    out = models._with_key(text, "p", "note", "z")
    assert _read(out)["note"] == "z"
    assert _read(out)["keep"] == "2"


def test_regular_newline_still_splits():
    text = "[arena.profile.p]\nmodels = a,\n  b\nkeep = 1\n"
    out = models._with_key(text, "p", "models", "z")
    assert _read(out)["models"] == "z"
    assert _read(out)["keep"] == "1"


def test_crlf_still_splits():
    text = "[arena.profile.p]\r\nmodels = a,\r\n  b\r\nkeep = 1\r\n"
    out = models._with_key(text, "p", "models", "z")
    assert _read(out)["models"] == "z"
    assert _read(out)["keep"] == "1"
