"""tests/test_collect_already_safe_query.py — COLLECT-11.

* A query for `prompt_store.py:79` (a real, GUARDED `stack[-1]` access,
  per COLLECT-7's own reference case) answers "safe: guarded".
* A query for a fabricated UNGUARDED location answers "not safe".
* A registered fail-open except site answers "safe: fail_open".
* A location covered only by a seed contract answers "safe: contract".
* A location with no data at all (not an indexed access, not an except
  site, no contract) answers "not safe: unknown" rather than guessing.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.model import ContractRecord, FunctionRecord, GuardedAccess, ModuleRecord, Provenance
from tools.collect.registries import (
    SafetyAnswer,
    build_already_safe_index,
    build_fail_open_registry,
    build_seed_contracts,
)
from tools.collect.scanner import scan_repo

REPO_ROOT = Path(__file__).parent.parent
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"


def _real_repo_index():
    modules = scan_repo(REPO_ROOT)
    fail_open = build_fail_open_registry(modules, root=REPO_ROOT)
    contracts = build_seed_contracts(modules)
    return build_already_safe_index(modules, fail_open, contracts, root=REPO_ROOT)


# ── guarded access: prompt_store.get_current's stack[-1] ───────────────────


def test_guarded_prompt_store_access_is_safe():
    index = _real_repo_index()
    answer = index.query("tools/prompt_store.py:79")

    assert isinstance(answer, SafetyAnswer)
    assert answer.safe is True
    assert answer.reason == "guarded"
    assert answer.detail is not None
    assert answer.provenance == Provenance.STATIC


# ── fabricated unguarded access is not safe ─────────────────────────────────


def test_fabricated_unguarded_access_is_not_safe():
    modules = [
        ModuleRecord(
            path="pkg/fake.py",
            guarded_accesses=(
                GuardedAccess(
                    location="pkg/fake.py:10",
                    access="items[-1]",
                    status="UNGUARDED",
                ),
            ),
        )
    ]
    index = build_already_safe_index(modules, fail_open_registry=[], contracts=[])
    answer = index.query("pkg/fake.py:10")

    assert answer.safe is False
    assert answer.reason == "unguarded"
    assert answer.detail == "items[-1]"


# ── fail-open except site is safe ───────────────────────────────────────────


def test_fail_open_site_is_safe():
    modules = scan_repo(FIXTURE_ROOT)
    fail_open = build_fail_open_registry(modules, root=FIXTURE_ROOT)
    index = build_already_safe_index(modules, fail_open, contracts=[])

    # pkg/error_handling.py:14 is read_optional's `except KeyError: pass`.
    answer = index.query("pkg/error_handling.py:14")
    assert answer.safe is True
    assert answer.reason == "fail_open"

    # read_with_log's site logs — not fail-open, not in the registry, and
    # not a guarded_access either, so it's "unknown", not "unguarded".
    answer2 = index.query("pkg/error_handling.py:22")
    assert answer2.safe is False
    assert answer2.reason == "unknown"


# ── contract coverage is safe ────────────────────────────────────────────────


def test_contract_covered_location_is_safe():
    modules = [
        ModuleRecord(path="tools/example.py", public_symbols=())
    ]
    from tools.collect.model import FunctionRecord

    modules = [
        ModuleRecord(
            path="tools/example.py",
            public_symbols=(
                FunctionRecord(
                    qualname="tools/example.py:build_thing",
                    module="tools/example.py",
                    lineno=5,
                    signature="build_thing(...)",
                ),
            ),
        )
    ]
    contracts = [
        ContractRecord(
            name="build_thing_invariant",
            description="build_thing always returns a 2-tuple",
            kind="seed",
            known_edge="tools/example.py:build_thing",
        )
    ]
    index = build_already_safe_index(modules, fail_open_registry=[], contracts=contracts)

    # Line 8 falls inside build_thing (declared at line 5, no later
    # top-level symbol in this module).
    answer = index.query("tools/example.py:8")
    assert answer.safe is True
    assert answer.reason == "contract"
    assert "build_thing_invariant" in answer.detail
    assert answer.provenance == Provenance.DERIVED


# ── truly unknown location ───────────────────────────────────────────────────


def test_unknown_location_is_not_safe_and_not_guessed():
    index = _real_repo_index()
    answer = index.query("tools/does_not_exist.py:1")

    assert answer.safe is False
    assert answer.reason == "unknown"
    assert answer.detail is None


# ── ambiguous location: multiple distinct accesses, mixed guard status ─────
#
# `AlreadySafeIndex` used to key `guarded_accesses` purely by `location`
# and answer "guarded" if *any* entry there was GUARDED — so a line with
# two different subscript expressions, one guarded and one not, would
# report the whole location "safe: guarded", which could wrongly suppress
# a real finding about the *other*, unguarded expression on that same
# line. Real in this repo: `tools/prompt_store.py:182` is
# `data[agent_name]["current_version"] = stack[-1]["version"] if stack
# else 0` — `data[agent_name]` is UNGUARDED, `stack[-1]` is GUARDED (via
# the `entry.get("stack")` alias two lines up).


def test_real_mixed_line_without_access_is_ambiguous_not_optimistically_guarded():
    index = _real_repo_index()
    answer = index.query("tools/prompt_store.py:182")
    assert answer.safe is False
    assert answer.reason == "ambiguous_location"
    assert "data[agent_name]" in answer.detail
    assert "stack[-1]" in answer.detail


def test_real_mixed_line_disambiguated_via_access_resolves_the_guarded_one():
    index = _real_repo_index()
    answer = index.query("tools/prompt_store.py:182", access="stack[-1]")
    assert answer.safe is True
    assert answer.reason == "guarded"


def test_real_mixed_line_disambiguated_via_access_does_not_falsely_clear_the_unguarded_one():
    # data[agent_name] itself isn't in guarded_accesses as UNGUARDED at
    # this exact citation in isolation from contract coverage — the
    # precise point is just that asking specifically about it must never
    # come back "guarded" (borrowing stack[-1]'s status), whatever else it
    # resolves to.
    index = _real_repo_index()
    answer = index.query("tools/prompt_store.py:182", access="data[agent_name]")
    assert answer.reason != "guarded"


def test_synthetic_mixed_line_ambiguous_without_access_disambiguated_with_it():
    modules = [
        ModuleRecord(
            path="pkg/mixed.py",
            guarded_accesses=(
                GuardedAccess(location="pkg/mixed.py:5", access="a[-1]", status="GUARDED", guard="g"),
                GuardedAccess(location="pkg/mixed.py:5", access="b[0]", status="UNGUARDED"),
            ),
        )
    ]
    index = build_already_safe_index(modules, fail_open_registry=[], contracts=[])

    ambiguous = index.query("pkg/mixed.py:5")
    assert ambiguous.safe is False
    assert ambiguous.reason == "ambiguous_location"

    guarded = index.query("pkg/mixed.py:5", access="a[-1]")
    assert guarded.safe is True
    assert guarded.reason == "guarded"

    unguarded = index.query("pkg/mixed.py:5", access="b[0]")
    assert unguarded.safe is False
    assert unguarded.reason == "unguarded"


def test_access_that_does_not_exist_at_a_real_location_is_unknown_not_unguarded():
    # Asking about an access that was never recorded at this location
    # (typo, or fabricated) must not silently borrow a different real
    # access's detail text at the same location.
    modules = [
        ModuleRecord(
            path="pkg/mixed.py",
            guarded_accesses=(
                GuardedAccess(location="pkg/mixed.py:5", access="a[-1]", status="GUARDED", guard="g"),
            ),
        )
    ]
    index = build_already_safe_index(modules, fail_open_registry=[], contracts=[])
    answer = index.query("pkg/mixed.py:5", access="nonexistent[0]")
    assert answer.reason == "unknown"


# ── class-level contract narrowing: a method-specific guarantee must not ───
# ── vouch for the whole class ───────────────────────────────────────────────
#
# COLLECT-4 only inventories top-level defs/classes, so a contract whose
# guarantee is about one method (`prompt_store_atomic_save`, about
# `PromptStore._save` specifically) is forced to cite the *class* as
# `known_edge`. Unnarrowed, `_enclosing_symbol_qualname` resolves *every*
# line anywhere in that class — every other method included — to the same
# class-wide symbol, so the contract matched (and reported "safe") for
# code that has nothing to do with atomic saving at all.


def test_real_prompt_store_unrelated_methods_no_longer_falsely_match_save_contract():
    index = _real_repo_index()
    # Lines inside get_store_summary/get_version_label/push/rollback —
    # none of them call _save or touch its atomic-write behavior.
    for line in (90, 100, 130, 150):
        answer = index.query(f"tools/prompt_store.py:{line}")
        assert answer.reason != "contract", (
            f"line {line} (not inside _save) falsely matched a contract "
            f"that only guarantees _save's behavior: {answer!r}"
        )


def test_real_prompt_store_save_body_still_matches_its_own_contract():
    index = _real_repo_index()
    # A line actually inside _save's try block (the os.replace call).
    answer = index.query("tools/prompt_store.py:222")
    assert answer.safe is True
    assert answer.reason == "contract"
    assert "prompt_store_atomic_save" in answer.detail


def test_class_level_contract_without_root_does_not_overmatch_either():
    # The conservative fallback: without `root` to verify a method's
    # range, a method-referencing class-level contract must not match
    # anywhere in the class — matching everywhere (the old behavior) is
    # exactly the false "safe" claim this fix closes; the only acceptable
    # degradation is matching nowhere.
    modules = scan_repo(REPO_ROOT)
    fail_open = build_fail_open_registry(modules, root=REPO_ROOT)
    contracts = build_seed_contracts(modules)
    index = build_already_safe_index(modules, fail_open, contracts)  # no root
    for line in (90, 100, 130, 150, 222):
        answer = index.query(f"tools/prompt_store.py:{line}")
        assert answer.reason != "contract"


def test_class_level_contract_with_no_method_reference_still_matches_class_wide():
    # A class-level contract whose description names no specific method
    # (unlike prompt_store_atomic_save) genuinely means the whole class —
    # that existing, correct behavior must be unaffected by this fix.
    modules = [
        ModuleRecord(
            path="pkg/widget.py",
            public_symbols=(
                FunctionRecord(
                    qualname="pkg/widget.py:Widget",
                    module="pkg/widget.py",
                    lineno=1,
                    signature="class Widget",
                ),
            ),
        )
    ]
    contracts = [
        ContractRecord(
            name="widget_thread_safe",
            description="Widget is safe to use from multiple threads.",
            kind="seed",
            known_edge="pkg/widget.py:Widget",
        )
    ]
    index = build_already_safe_index(modules, fail_open_registry=[], contracts=contracts)
    answer = index.query("pkg/widget.py:50")
    assert answer.safe is True
    assert answer.reason == "contract"
