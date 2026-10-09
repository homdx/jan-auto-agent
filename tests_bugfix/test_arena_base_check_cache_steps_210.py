"""tests_bugfix/test_arena_base_check_cache_steps_210.py — bug 45: a cached pass is a hit only for the current step list.

`_hit` took any stored entry with `ok: true` as a hit whatever steps it held,
so a pass cached when there were three steps was still a green verdict after a
fourth step was added — a check that never ran the fourth. Ticket 210: every
step name of the current list must be among the stored rows; otherwise the
check runs for real and overwrites the entry. The cache keeps its format.
"""

from __future__ import annotations

from tools.arena import basecheck

SHA = "a" * 40
NAMES = [name for name, _ in basecheck.STEPS]


def _entry(names) -> dict:
    return {"at": 1, "ok": True,
            "steps": [{"step": n, "ok": True, "seconds": 0.1, "summary": f"{n} ok"}
                      for n in names]}


def test_a_stored_pass_missing_a_current_step_is_not_a_hit():
    cache = {SHA: _entry(NAMES[:-1])}
    assert basecheck._hit(cache, SHA) is None
    assert basecheck._hit(cache, SHA, NAMES) is None


def test_a_complete_stored_pass_is_still_a_hit():
    rows = basecheck._hit({SHA: _entry(NAMES)}, SHA, NAMES)
    assert rows is not None and [r["step"] for r in rows] == NAMES
    assert all(r["cached"] is True and r["ok"] for r in rows)


def test_an_extra_stored_step_does_not_spoil_a_hit():
    rows = basecheck._hit({SHA: _entry(NAMES + ["retired"])}, SHA, NAMES)
    assert rows is not None and len(rows) == len(NAMES) + 1


def test_the_step_list_checked_is_the_one_the_run_would_run():
    cache = {SHA: _entry(["tests", "tiers"])}
    assert basecheck._hit(cache, SHA, ["tests", "tiers"]) is not None
    assert basecheck._hit(cache, SHA, ["tests", "tiers", "lint"]) is None


def test_no_step_list_means_the_current_steps():
    """Without a list the current `STEPS` decide — never the old "any pass" answer."""
    assert basecheck._hit({SHA: _entry(["tests"])}, SHA) is None
    assert basecheck._hit({SHA: _entry(NAMES)}, SHA) is not None


def test_run_base_check_asks_for_the_steps_it_is_about_to_run(monkeypatch, tmp_path):
    """From the Sonnet 5.5 entry: the call site passes its own step list, not `STEPS`."""
    seen = []
    monkeypatch.setattr(basecheck, "_hit", lambda cache, sha, names=None: seen.append(names))
    monkeypatch.setattr(basecheck, "resolve_sha", lambda repo, ref: SHA)
    monkeypatch.setattr(basecheck, "prepare_tree",
                        lambda *a, **k: (_ for _ in ()).throw(basecheck.BaseCheckError("stop")))
    steps = [basecheck.Step("only", ["true"])]

    class A:
        ref, force, dry_run, output = "HEAD", False, False, "table"

    basecheck.run_base_check(tmp_path, A(), None, steps)
    assert seen == [["only"]]
