"""tests/test_collect_determinism.py — COLLECT-3.

* Two runs of "Pass A" on `collect_mini_repo` produce byte-identical
  structural JSON (sorted keys, stable symbol order, no timestamps).
* That output matches a golden fixture checked into the repo.
* `_determinism`'s canonicalization utilities reject clock-dependent keys
  and normalize dict/set/tuple ordering on their own.

The "Pass A" used here is `tests/_pass_a_stub.py` — a minimal placeholder
AST walk. It exists only to exercise the determinism guarantee before the
real scanner (COLLECT-4, EPIC B) is built; COLLECT-4 supersedes it.
"""

from pathlib import Path

import pytest

from _pass_a_stub import pass_a_payload
from tools.collect._determinism import (
    NonDeterministicPayload,
    canonical_bytes,
    canonical_dumps,
    load_golden_bytes,
    run_twice_and_compare,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "collect_mini_repo_golden.json"


# ── AC: rerun without code changes gives no diff ─────────────────────────


def test_pass_a_is_byte_identical_across_two_runs():
    first, second = run_twice_and_compare(lambda: pass_a_payload(FIXTURE_ROOT))
    assert first == second


def test_pass_a_matches_golden_fixture():
    actual = canonical_bytes(pass_a_payload(FIXTURE_ROOT))
    golden = load_golden_bytes(GOLDEN_PATH)
    assert actual == golden


def test_pass_a_covers_every_fixture_module():
    payload = pass_a_payload(FIXTURE_ROOT)
    paths = {m["path"] for m in payload}
    assert paths == {
        "pkg/__init__.py",
        "pkg/config_reader.py",
        "pkg/error_handling.py",
        "pkg/prompt_store.py",
        "pkg/unguarded.py",
        "pkg/view_trace.py",
    }


def test_pass_a_finds_known_symbols_in_stable_order():
    payload = pass_a_payload(FIXTURE_ROOT)
    by_path = {m["path"]: m for m in payload}
    error_module = by_path["pkg/error_handling.py"]
    names = [s["qualname"].split(":")[-1] for s in error_module["public_symbols"]]
    assert names == ["read_optional", "read_with_log", "read_strict", "scan_all"]


# ── canonicalization utilities ────────────────────────────────────────────


def test_canonical_dumps_sorts_keys_regardless_of_insertion_order():
    a = {"b": 1, "a": 2, "c": {"z": 1, "y": 2}}
    b = {"c": {"y": 2, "z": 1}, "a": 2, "b": 1}
    assert canonical_dumps(a) == canonical_dumps(b)


def test_canonical_dumps_normalizes_tuples_to_lists():
    assert canonical_dumps((1, 2, 3)) == canonical_dumps([1, 2, 3])


def test_canonical_dumps_normalizes_set_order():
    # Two sets built in a different insertion order must canonicalize
    # identically — this is exactly the "imports" case (COLLECT-4/-5),
    # which is naturally assembled via a set before being sorted.
    s1 = {"os", "sys", "json"}
    s2 = {"json", "os", "sys"}
    assert canonical_dumps(s1) == canonical_dumps(s2) == '["json","os","sys"]'


def test_canonical_dumps_rejects_clock_dependent_keys():
    with pytest.raises(NonDeterministicPayload):
        canonical_bytes({"path": "pkg/x.py", "generated_at": "2026-07-14T00:00:00Z"})


def test_canonical_dumps_rejects_clock_dependent_keys_when_nested():
    with pytest.raises(NonDeterministicPayload):
        canonical_bytes([{"path": "pkg/x.py", "summary": {"timestamp": "now"}}])


def test_canonical_dumps_check_forbidden_can_be_disabled_for_manifests():
    # The manifest itself (COLLECT-2) legitimately carries `generated_at` —
    # canonicalization must still be available to it, just without the
    # structural-payload guard.
    payload = {"generated_at": "2026-07-14T00:00:00Z", "git_sha": "abc123"}
    assert canonical_bytes(payload, check_forbidden=False) == (
        b'{"generated_at":"2026-07-14T00:00:00Z","git_sha":"abc123"}'
    )
