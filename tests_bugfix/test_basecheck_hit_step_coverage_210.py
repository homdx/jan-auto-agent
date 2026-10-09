"""tests_bugfix/test_basecheck_hit_step_coverage_210.py — ticket 210, bug 45.

``tools/arena/basecheck.py``'s ``_hit`` used to accept any cached ``ok: true``
entry whatever steps it held: a pass cached when ``STEPS`` had three entries
was a "hit" after a fourth step was added, and the table printed a green
verdict for a check that never ran the new step. Fixed: a hit now needs every
name of the current step list present among the stored rows; an entry missing
one is run for real and the cache is overwritten.
"""
from __future__ import annotations

from tools.arena import basecheck


def _entry(steps: list[dict], ok: bool = True) -> dict:
    return {"at": 1, "ok": ok, "steps": steps}


def _row(name: str, ok: bool = True) -> dict:
    return {"step": name, "ok": ok, "seconds": 0.1, "summary": "s"}


def test_a_stored_pass_missing_a_current_step_is_not_a_hit():
    """Cached with only two of today's three steps: no hit."""
    cache = {"deadbeef": _entry([_row("tests"), _row("tiers")])}
    assert basecheck._hit(cache, "deadbeef", ["tests", "tiers", "clocks"]) is None


def test_a_stored_pass_covering_every_current_step_is_still_a_hit():
    """Cached with all of today's steps (an extra stale one is harmless): a hit."""
    cache = {"deadbeef": _entry([_row("tests"), _row("tiers"), _row("clocks"), _row("old_step")])}
    hit = basecheck._hit(cache, "deadbeef", ["tests", "tiers", "clocks"])
    assert hit is not None
    assert {row["step"] for row in hit} == {"tests", "tiers", "clocks", "old_step"}


def test_default_step_names_come_from_the_real_steps_tuple():
    """Without an explicit list, `_hit` checks against `STEPS` itself."""
    names = [name for name, _ in basecheck.STEPS]
    cache = {"deadbeef": _entry([_row(n) for n in names])}
    assert basecheck._hit(cache, "deadbeef") is not None
    cache = {"deadbeef": _entry([_row(names[0])])}
    assert basecheck._hit(cache, "deadbeef") is None
