"""tools/collect/_determinism.py — COLLECT-3: canonicalization + golden-fixture harness.

Pass A (the AST-only structural pass, EPIC B) is meant to be re-run freely —
by a developer checking staleness, by CI, by `--refresh` (COLLECT-24) — and
each of those re-runs must produce byte-identical JSON for an unchanged
tree. Determinism is what makes the model *checkable*: a diff between two
runs on the same code is either a real code change or a bug in the
collector, never noise, because there is no third source of variation left.

Three sources of nondeterminism this module closes off:

1. **Key order** — Python dict iteration order follows insertion order, and
   different code paths (or Python versions) could insert fields in a
   different sequence. `canonical_dumps` always sorts keys.
2. **Set/tuple derived ordering** — anywhere a value was ever assembled via
   a `set` (e.g. deduplicated imports), iteration order is not guaranteed
   across runs/processes. `canonical_dumps` normalizes sets to sorted lists
   before serializing, and tuples to lists (JSON has no tuple type anyway).
3. **Clock-dependent fields** — a structural record must never carry a
   timestamp; only `collect_manifest.json` (COLLECT-2) does. If a
   `generated_at`/`timestamp`/`collected_at` key ever leaks into a
   structural payload, two runs one second apart would differ for a reason
   that has nothing to do with the code — `canonical_dumps` raises instead
   of silently serializing it.

The golden-fixture harness (`write_golden` / `load_golden_bytes`) lets a
test check a Pass A run against a checked-in reference file, and
`run_twice_and_compare` is the direct expression of the COLLECT-3 AC: run
the same builder twice, canonicalize both, and let the caller assert they's
equal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Tuple

#: Keys that must never appear inside a *structural* payload — they belong
#: only in the manifest (COLLECT-2), which is explicitly clock-carrying so
#: that structural JSON doesn't have to be.
FORBIDDEN_STRUCTURAL_KEYS = frozenset({"generated_at", "timestamp", "collected_at"})


class NonDeterministicPayload(RuntimeError):
    """Raised when a structural payload contains a clock-dependent field,
    or anything else that would make two honest runs disagree."""


def _check_no_forbidden_keys(obj: Any, path: str = "$") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in FORBIDDEN_STRUCTURAL_KEYS:
                raise NonDeterministicPayload(
                    f"structural payload at {path}.{k!s} carries a clock-dependent "
                    f"field {k!r}; timestamps belong only in collect_manifest.json "
                    f"(COLLECT-2), never in Pass A's structural JSON"
                )
            _check_no_forbidden_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _check_no_forbidden_keys(v, f"{path}[{i}]")


def _to_jsonable(obj: Any) -> Any:
    """Recursively normalize dataclass-adjacent containers into the plain
    JSON-native shapes `canonical_dumps` can serialize deterministically:
    tuples become lists, sets/frozensets become *sorted* lists (their
    iteration order is otherwise run-dependent)."""
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted(_to_jsonable(v) for v in obj)
    return obj


def canonical_dumps(obj: Any, *, check_forbidden: bool = True) -> str:
    """The one canonical JSON serialization structural (Pass A) output must
    always use: sorted keys, normalized (non-set-order-dependent)
    containers, compact/reproducible separators, and — by default — a
    guard against clock-dependent fields. Two calls on equal-but-freshly-
    constructed inputs always produce the identical string."""
    normalized = _to_jsonable(obj)
    if check_forbidden:
        _check_no_forbidden_keys(normalized)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(obj: Any, *, check_forbidden: bool = True) -> bytes:
    return canonical_dumps(obj, check_forbidden=check_forbidden).encode("utf-8")


def write_golden(path: Path, obj: Any) -> None:
    """Write `obj`'s canonical serialization to `path` as a golden fixture."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(obj) + b"\n")


def load_golden_bytes(path: Path) -> bytes:
    """Read back a golden fixture written by `write_golden` (trailing
    newline stripped, so it compares equal to a fresh `canonical_bytes` call)."""
    return Path(path).read_bytes().rstrip(b"\n")


def run_twice_and_compare(build_fn: Callable[[], Any]) -> Tuple[bytes, bytes]:
    """Call `build_fn` twice — fresh, from scratch each time — and
    canonicalize both results. Returns `(first, second)` so the caller gets
    a readable diff on assertion failure instead of just a bool."""
    first = canonical_bytes(build_fn())
    second = canonical_bytes(build_fn())
    return first, second
