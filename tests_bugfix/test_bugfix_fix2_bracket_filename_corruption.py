"""FIX-2 #3 — bracket stripping corrupted valid filenames.

``ExistenceValidator._looks_like_path`` / ``_normalise`` used
``token.strip("\"'()[],;:")``. ``str.strip(chars)`` removes a *charset*
independently from each end, not a matched delimiter pair, so a reference
like ``[file].py`` — whose closing ``]`` is not the token's last character —
lost only its leading ``[`` and became ``file].py``: a name that isn't on
disk. A document that correctly referenced an existing bracketed file was
therefore rejected by GATES-3 as if the file were missing.

This module already treats embedded brackets as legitimate filename
characters elsewhere (``check()`` calls ``glob.escape()`` specifically so
``handler[old].py`` is matched literally, not as a glob pattern), so the fix
must preserve them, not merely relocate the bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.auto.existence_validator import ExistenceValidator, _strip_wrapping_delimiters


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "[file].py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "handler[old].py").write_text("print(2)\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print(3)\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def validator() -> ExistenceValidator:
    return ExistenceValidator()


# ── the reported defect, end to end through check() ────────────────────────

def test_bracket_wrapped_filename_is_not_reported_missing(validator, repo):
    """The bug report's exact example: a real `[file].py` must be approved."""
    text = "See `[file].py` for the entry point.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert verdict.approved is True
    assert verdict.missing == []


def test_embedded_bracket_filename_is_not_reported_missing(validator, repo):
    """Brackets in the middle of a name were already glob-escaped in check();
    the reference list itself must not mangle them either."""
    text = "Run `handler[old].py` to reproduce.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert verdict.approved is True
    assert verdict.missing == []


def test_bracket_wrapped_missing_filename_is_still_reported_missing(validator, repo):
    """The fix must not become a blanket "ignore brackets" that fails closed
    the other way — a genuinely absent bracketed reference is still caught."""
    text = "See `[ghost].py` for details.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert verdict.approved is False
    assert "[ghost].py" in verdict.missing


# ── unit-level: _looks_like_path and _normalise must agree ─────────────────

def test_looks_like_path_and_normalise_do_not_corrupt_brackets(validator):
    assert validator._looks_like_path("[file].py") is True
    assert validator._normalise("[file].py") == "[file].py"


def test_whole_token_parenthesis_wrap_is_still_cleaned(validator, repo):
    """A single matched wrapping pair (not embedded) is prose punctuation,
    not part of the filename, and should still be stripped."""
    assert validator._normalise("(main.py)") == "main.py"
    text = "See (main.py) for the entry point.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert verdict.approved is True


def test_stray_trailing_bracket_is_stripped_not_corrupted():
    """A lone, unpaired trailing bracket is prose noise, not a filename
    character, and normalising must not leave it attached."""
    assert _strip_wrapping_delimiters("file.py]") == "file.py"


def test_stray_leading_bracket_is_stripped_not_corrupted():
    assert _strip_wrapping_delimiters("[file.py") == "file.py"


def test_leading_dot_of_a_dotfile_is_preserved():
    """The delimiter set must not include "." — stripping it would corrupt
    ".hidden_test.py" into "hidden_test.py", which is exactly the AUTO-FIX
    documented inside _normalise() and the same class of bug as the bracket
    corruption this module fixes."""
    assert _strip_wrapping_delimiters(".hidden_test.py") == ".hidden_test.py"
    assert _strip_wrapping_delimiters("./main.py") == "./main.py"


def test_normalise_still_preserves_dotfile_and_strips_dot_slash(validator):
    assert validator._normalise(".hidden_test.py") == ".hidden_test.py"
    assert validator._normalise("./main.py") == "main.py"
    assert validator._looks_like_path(".hidden_test.py") is True
