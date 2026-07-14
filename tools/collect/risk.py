"""tools/collect/risk.py — COLLECT-13: RISK_INDEX (derived audit ranking).

A worklist, not a new fact: `compute_risk_index` only *combines* facts
EPIC B/C/D already produced into one deterministic score per module, so an
auditor knows where to look first. Every ingredient is `static` or
`derived` in COLLECT-1's sense — never `llm` — so, like `test_map.py` and
`graph.py`, this module is composition, not a new source of truth:

* **LOC** — a cheap size proxy. Bigger modules have more surface area to
  audit, all else equal.
* **blast-radius** — `len(imported_by[path])` (COLLECT-8): a bug in a
  module many others import matters more than the same bug in a leaf.
* **UNGUARDED count** — `GuardedAccess.status == "UNGUARDED"` (COLLECT-7):
  the second false-positive killer's *negative* space — sites nobody has
  shown are safe.
* **undocumented fail-open count** — `FailOpenEntry`s from the
  FAIL_OPEN_REGISTRY (COLLECT-9) whose `rationale is None`: a silent
  `except` swallow with no comment explaining why is a worse risk than
  one that says `# deliberately silent, see #123`.
* **coverage** — via TEST_MAP (COLLECT-12): a module on the zero-list gets
  the largest single bonus this score has, since "nobody tests this at
  all" dominates every other signal — exactly the COLLECT-13 "coverage
  (zero -> max risk)" requirement.

The weights below are an explicit, fixed constant table, not a tuned model
— there is nothing here for an LLM (or any other adaptive process) to
learn or drift; the same inputs always produce the same score
(COLLECT-3 determinism), and `RISK_INDEX` is sorted `(-score, path)` so
ties break on path, not on iteration order.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from tools.collect.model import ModuleRecord
from tools.collect.registries import FailOpenEntry

# ── Fixed weight table (COLLECT-3: deterministic, not tuned/adaptive) ──────

LOC_WEIGHT = 1
BLAST_RADIUS_WEIGHT = 5
UNGUARDED_WEIGHT = 10
UNDOCUMENTED_FAIL_OPEN_WEIGHT = 8
#: Coverage is binary-gated, not scaled by a count: a module with *any*
#: covering test is not on the zero-list, so this bonus never applies to
#: it. This is the "coverage (zero -> max risk)" requirement — it is
#: deliberately the single largest term in the table, since no other
#: signal here should be able to outrank "nothing tests this at all"
#: unless the module is also huge/UNGUARDED/undocumented-fail-open-heavy.
ZERO_COVERAGE_BONUS = 50


@dataclass(frozen=True)
class RiskEntry:
    """One row of RISK_INDEX: a module's audit-priority score plus the
    raw ingredient counts that produced it, so a consumer can see *why*
    a module ranked where it did, not just the number.
    """

    path: str
    loc: int
    blast_radius: int
    unguarded_count: int
    undocumented_fail_open_count: int
    zero_coverage: bool
    score: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "loc": self.loc,
            "blast_radius": self.blast_radius,
            "unguarded_count": self.unguarded_count,
            "undocumented_fail_open_count": self.undocumented_fail_open_count,
            "zero_coverage": self.zero_coverage,
            "score": self.score,
        }


def _loc(root: Optional[Path], module: ModuleRecord) -> int:
    """Line count of `module`'s source, or `0` when it can't be read
    (no `root` given, a `parse_error`, or the file moved/vanished under
    `root`) — LOC is a size *proxy*, so a missing count degrades to "no
    size signal" rather than raising and taking down the whole index.
    """
    if root is None or module.parse_error:
        return 0
    try:
        source = (Path(root) / module.path).read_text(encoding="utf-8")
    except OSError:
        return 0
    return len(source.splitlines())


def _unguarded_count(module: ModuleRecord) -> int:
    return sum(1 for g in module.guarded_accesses if g.status == "UNGUARDED")


def _undocumented_fail_open_counts(
    fail_open_registry: Iterable[FailOpenEntry],
) -> Dict[str, int]:
    """`{module_path: count of that module's fail-open sites with no
    rationale comment}` — built once over the whole registry rather than
    re-scanning it per module.
    """
    counts: Dict[str, int] = {}
    for entry in fail_open_registry:
        if entry.rationale is not None:
            continue
        path = entry.location.rpartition(":")[0]
        counts[path] = counts.get(path, 0) + 1
    return counts


def _score(
    *,
    loc: int,
    blast_radius: int,
    unguarded_count: int,
    undocumented_fail_open_count: int,
    zero_coverage: bool,
) -> int:
    return (
        loc * LOC_WEIGHT
        + blast_radius * BLAST_RADIUS_WEIGHT
        + unguarded_count * UNGUARDED_WEIGHT
        + undocumented_fail_open_count * UNDOCUMENTED_FAIL_OPEN_WEIGHT
        + (ZERO_COVERAGE_BONUS if zero_coverage else 0)
    )


def compute_risk_index(
    modules: Iterable[ModuleRecord],
    *,
    imported_by: Dict[str, frozenset],
    fail_open_registry: Iterable[FailOpenEntry] = (),
    test_map: Optional[Dict[str, Tuple[str, ...]]] = None,
    root: Optional[Path] = None,
) -> List[RiskEntry]:
    """RISK_INDEX: one `RiskEntry` per module in `modules`, sorted by
    `(-score, path)` — highest risk first, path as the deterministic
    tie-break (COLLECT-3).

    `imported_by` is `graph.imported_by(graph.import_edges(modules))`
    (COLLECT-8); `fail_open_registry` is
    `registries.build_fail_open_registry(...)` (COLLECT-9); `test_map` is
    `test_map.build_test_map(...)` (COLLECT-12). All three are optional to
    build ahead of time and pass in — this function does not recompute
    them itself — because each is independently expensive (a registry scan,
    a test-file re-read pass) and callers assembling the full collect
    artifact already have them lying around from earlier stages; passing
    `test_map=None` degrades every module to `zero_coverage=False` rather
    than guessing.
    """
    modules = list(modules)
    undocumented_counts = _undocumented_fail_open_counts(fail_open_registry)

    entries: List[RiskEntry] = []
    for m in modules:
        loc = _loc(root, m)
        blast_radius = len(imported_by.get(m.path, frozenset()))
        unguarded_count = _unguarded_count(m)
        undocumented_fail_open_count = undocumented_counts.get(m.path, 0)
        zero_coverage = (
            test_map is not None and len(test_map.get(m.path, ())) == 0
        )
        score = _score(
            loc=loc,
            blast_radius=blast_radius,
            unguarded_count=unguarded_count,
            undocumented_fail_open_count=undocumented_fail_open_count,
            zero_coverage=zero_coverage,
        )
        entries.append(
            RiskEntry(
                path=m.path,
                loc=loc,
                blast_radius=blast_radius,
                unguarded_count=unguarded_count,
                undocumented_fail_open_count=undocumented_fail_open_count,
                zero_coverage=zero_coverage,
                score=score,
            )
        )

    entries.sort(key=lambda e: (-e.score, e.path))
    return entries
