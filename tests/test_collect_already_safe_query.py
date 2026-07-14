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

from tools.collect.model import ContractRecord, GuardedAccess, ModuleRecord, Provenance
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
    return build_already_safe_index(modules, fail_open, contracts)


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
