"""H2 — the full boundary-stripping contract of ``_strip_wrapping_delimiters``.

H2 on the pullv3 list is the blanket ``token.strip("\"'()[],;:")`` in
``_looks_like_path``/``_normalise``: ``str.strip(chars)`` removes a *charset*
from each end independently, not a matched pair, so ``[file].py`` — whose
closing ``]`` is not the last character — lost only its leading ``[`` and
became ``file].py``, a name that is not on disk.

That defect is already fixed (FIX-2 #3, commit efe5fbf) by
:func:`tools.auto.existence_validator._strip_wrapping_delimiters`, and
:mod:`tests_bugfix.test_bugfix_fix2_bracket_filename_corruption` pins the
core case. This module pins the rest of the contract — the cases the other
models' H2 patches asserted, which had no test here yet — so a future
rewrite of the helper cannot quietly change any of them:

* a bracket pair *at the exact boundaries* is wrapping, and comes off;
* a bracket with a partner elsewhere in the token is part of the filename,
  and stays (``[file].py``, ``a[b].py``, ``[[nested]].md``);
* an unpartnered stray bracket comes off, as the old charset strip did;
* quotes and ``,;:`` still come off; a leading ``.`` never does.
"""

from __future__ import annotations

import pytest

from tools.auto.existence_validator import (
    ExistenceValidator,
    _strip_wrapping_delimiters,
)


@pytest.fixture
def validator() -> ExistenceValidator:
    return ExistenceValidator()


@pytest.mark.parametrize(
    "raw,expected",
    [
        # ── wrapping pairs at the exact boundaries: stripped ──────────────
        ("(main.py)", "main.py"),
        ("[main.py]", "main.py"),
        ("{main.py}", "main.py"),
        ("([main.py])", "main.py"),
        ('"main.py"', "main.py"),
        ("'main.py'", "main.py"),
        # ── partnered brackets: part of the name, preserved ───────────────
        ("[file].py", "[file].py"),
        ("a[b].py", "a[b].py"),
        ("data[1].csv", "data[1].csv"),
        ("path/to/[item].py", "path/to/[item].py"),
        ("[[nested]].md", "[[nested]].md"),
        # ── unpartnered strays: stripped, as the old charset strip did ────
        ("(main.py", "main.py"),
        ("main.py)", "main.py"),
        ("[file.py", "file.py"),
        ("file.py]", "file.py"),
        # ── quotes and sentence punctuation, alone and combined ───────────
        ("main.py,", "main.py"),
        ("main.py;", "main.py"),
        ("main.py:", "main.py"),
        ("[file].py,", "[file].py"),
        ("[file].py;", "[file].py"),
        ('"[file].py"', "[file].py"),
        ('"(main.py)",', "main.py"),
        # ── a leading dot is never punctuation: it makes the dotfile ──────
        (".hidden_test.py", ".hidden_test.py"),
        ("./main.py", "./main.py"),
    ],
)
def test_strip_wrapping_delimiters_contract(raw: str, expected: str) -> None:
    assert _strip_wrapping_delimiters(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("[file].py", "[file].py"),
        ("[[nested]].md", "[[nested]].md"),
        ("path/to/[item].py", "path/to/[item].py"),
        ("(main.py)", "main.py"),
        ("[main.py]", "main.py"),
        ('"[file].py"', "[file].py"),
        ("./main.py", "main.py"),
        ("../main.py", "main.py"),
        (".hidden_test.py", ".hidden_test.py"),
    ],
)
def test_normalise_agrees_with_the_helper(validator, raw, expected) -> None:
    """``_normalise`` layers ./ and ../ handling on top of the same strip —
    the two used to disagree with each other about brackets."""
    assert validator._normalise(raw) == expected


@pytest.mark.parametrize(
    "token",
    ["[file].py", "[[nested]].md", "data[1].csv", "(main.py)", '"[file].py"'],
)
def test_bracketed_tokens_are_still_recognised_as_paths(validator, token) -> None:
    """The corrupted form (``file].py``) has the suffix ``py`` too, so a
    _looks_like_path check alone never caught this bug — but the gate is
    useless if the fix makes bracketed names stop looking like paths."""
    assert validator._looks_like_path(token) is True


def test_bracketed_reference_to_a_real_file_is_approved(tmp_path) -> None:
    """End to end: the false rejection this bug caused."""
    (tmp_path / "[file].py").write_text("x\n", encoding="utf-8")
    (tmp_path / "test_smoke.py").write_text("def test_x(): pass\n", encoding="utf-8")
    verdict = ExistenceValidator().check("See `[file].py` for details.", tmp_path)
    assert verdict.approved is True
    assert verdict.missing == []


def test_bracketed_reference_to_a_missing_file_is_still_rejected(tmp_path) -> None:
    """Preserving brackets must not turn the gate off: a bracketed name that
    really is absent is still reported, and reported uncorrupted."""
    (tmp_path / "test_smoke.py").write_text("def test_x(): pass\n", encoding="utf-8")
    verdict = ExistenceValidator().check("See `[ghost].py` for details.", tmp_path)
    assert verdict.approved is False
    assert "[ghost].py" in verdict.missing
    assert "ghost].py" not in verdict.missing
