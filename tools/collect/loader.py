"""tools/collect/loader.py — COLLECT-21: consumer-side loader.

The thin read side of `collect` mode (EPIC G), sitting opposite `cli.py`'s
producer side. Everything in `tools/collect/*` up through EPIC F only ever
*writes* `.collect/`; this module is the only place in the package that
reads that artifact back for a consumer (auto/doc context injection —
COLLECT-23, bughunt-suppression — COLLECT-22).

Contract (COLLECT-21's brief)
------------------------------
* **No manifest** -> "no model": `load()` returns a `CollectModel` whose
  `status` is `"absent"` and whose query methods all answer with an empty/
  `None`/`unknown` result. Nothing downstream needs to special-case this —
  every query method is safe to call on an absent model, so a caller who
  forgets to check `.available` still gets today's behavior (no collect
  data) rather than an exception.
* **Present but stale** -> handled per `[collect] staleness`:
    - `"warn"`    — load the (stale) artifact anyway, `status="stale"`.
    - `"refresh"` — rebuild via `cli.action_refresh`, then load the fresh
      result, `status="fresh"`.
    - `"ignore"`  — treated exactly like absent.
    - anything else (typo, unknown value) — falls back to `"warn"`, same
      as `cli.py`'s own config-reading convention elsewhere in this repo.
* **Fresh** -> `status="fresh"`, full query API available.
* **Broken/partial artifact** (unreadable JSON, missing keys) is treated as
  absent, *not* as an error — a half-written or corrupted `.collect/` must
  never crash a caller; it just means "no model" the same as if collect had
  never run.

Query-API antihallucination guarantee
--------------------------------------
`CollectModel.is_safe()` delegates to `registries.AlreadySafeIndex`
(COLLECT-11), which is built only from `guarded_accesses` (static),
`FAIL_OPEN_REGISTRY` (static) and `CONTRACTS` (static/derived) — never from
an `LLMSummary`. So "safe ли X?" answering only by static facts
(COLLECT-21's own AC) holds by construction, the same way COLLECT-1's
provenance isolation holds by construction rather than by convention.

V1 — the import graph
----------------------
`import_edges` / `imported_by` / `entry_points` (COLLECT-8) are written by
the producer on every run but, before this ticket, thrown away on read —
9 of the artifact's 13 keys made it onto `CollectModel`, and "who calls
X?" / "what does X call?" meant re-running the scan even though the
answer sat in `.collect/artifact.json` already. They are now kept
alongside everything else above: same guarded-try normalisation, same
"missing/null key -> empty container, wrong-shaped key -> absent model"
contract. `callers_of()` and `calls_into()` are the read-only query pair
built on top; see their docstrings below for the ordering and
test-exclusion rules.
"""

from __future__ import annotations

import configparser
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from tools.collect import cli as cli_mod
from tools.collect import manifest as manifest_mod
from tools.collect import registries as registries_mod
from tools.collect.model import ContractRecord, ModuleRecord
from tools.collect.registries import FailOpenEntry, SafetyAnswer
from tools.collect.gates import GateEntry
from tools.collect.risk import RiskEntry
from tools.collect.config_map import ConfigMapEntry
from tools.collect.scanner import scan_repo

VALID_STALENESS = frozenset({"warn", "refresh", "ignore"})
DEFAULT_STALENESS = "warn"

STATUS_ABSENT = "absent"
STATUS_STALE = "stale"
STATUS_FRESH = "fresh"

#: Directory prefixes and file names that mark a module as test code rather
#: than shipped code. `callers_of` uses these alongside the test paths the
#: artifact itself recorded in `test_map`'s values: the artifact's own list is
#: authoritative but indexes coverage rather than the test tree, and a stale
#: artifact can omit whole test directories (this repo's `tests_bugfix/` has no
#: entry at all), so a path fallback is needed for anything it did not record.
#: `tools/collect/test_map.py` deliberately matches neither signal: it is
#: shipped code whose name merely contains "test".
_TEST_PATH_PREFIXES = (
    "tests/", "tests_bugfix/", "tests_slow/", "stub-test/",
    ".smoke_tests/", ".regression_tests/",
)
_TEST_FILE_NAMES = ("conftest.py",)


def _staleness_policy(config: Optional[configparser.ConfigParser]) -> str:
    """`[collect] staleness` (default `warn`); any unrecognised value also
    falls back to `warn` — same "don't let a typo silently misbehave"
    stance the rest of `[collect]`'s config reading takes."""
    if config is None:
        return DEFAULT_STALENESS
    raw = config.get("collect", "staleness", fallback=DEFAULT_STALENESS).strip().lower()
    return raw if raw in VALID_STALENESS else DEFAULT_STALENESS


@dataclass(frozen=True)
class CollectModel:
    """The consumer-facing handle on a (possibly absent) collect artifact.

    Every field below is empty on an absent/ignored model, so every query
    method degrades to "nothing known" rather than raising — a caller that
    never checks `.available` still behaves exactly like collect never ran.
    """

    status: str  # "absent" | "stale" | "fresh"
    collect_dir: Optional[Path] = None
    modules: Tuple[ModuleRecord, ...] = ()
    contracts: Tuple[ContractRecord, ...] = ()
    fail_open_registry: Tuple[FailOpenEntry, ...] = ()
    gates: Tuple[GateEntry, ...] = ()
    test_map: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    zero_coverage_list: Tuple[str, ...] = ()
    thin_coverage_list: Tuple[str, ...] = ()
    risk_index: Tuple[RiskEntry, ...] = ()
    config_map: Tuple[ConfigMapEntry, ...] = ()
    # V1: the import graph the producer has always written (13 artifact keys,
    # this was the first batch dropped on read). Both tables are keyed by
    # relative module path: `import_edges` is first-party only — `graph.
    # import_edges` resolves names against the scanned module set and drops
    # anything outside the repo — and `imported_by` is its reverse index.
    # `entry_points` is a denormalised copy of "keys of `imported_by` with an
    # empty value"; kept because the artifact carries it, so a caller does not
    # have to rebuild it. Empty containers, never `None`.
    import_edges: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    imported_by: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    entry_points: Tuple[str, ...] = ()
    reason: str = ""

    # ── availability ────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        """True iff there is real data behind this model (`fresh` or
        `stale`) — false for `absent`."""
        return self.status != STATUS_ABSENT

    @property
    def is_stale(self) -> bool:
        return self.status == STATUS_STALE

    # ── lazily-built indexes over the tuples above ──────────────────────

    def _modules_by_path(self) -> Dict[str, ModuleRecord]:
        return {m.path: m for m in self.modules}

    def _already_safe_index(self, root: Optional[Path] = None) -> registries_mod.AlreadySafeIndex:
        return registries_mod.build_already_safe_index(
            self.modules, self.fail_open_registry, self.contracts, root=root or self.collect_dir,
        )

    # ── "запись модуля X" ────────────────────────────────────────────────

    def module(self, path: str) -> Optional[ModuleRecord]:
        """The `ModuleRecord` for `path`, or `None` if unknown / model
        absent."""
        return self._modules_by_path().get(path)

    # ── "контракты по X" ─────────────────────────────────────────────────

    def contracts_for(self, path_or_qualname: str) -> List[ContractRecord]:
        """Every `ContractRecord` whose `known_edge` names `path_or_qualname`
        (a module path or a `path:Qualname` symbol reference). Empty list
        (not `None`) when nothing matches, or the model is absent."""
        return [c for c in self.contracts if c.known_edge == path_or_qualname]

    # ── "fail-open по X" ─────────────────────────────────────────────────

    def fail_open_for(self, path: str) -> List[FailOpenEntry]:
        """Every `FailOpenEntry` whose `location` falls under module
        `path` (`"path:line"` prefix match)."""
        prefix = f"{path}:"
        return [e for e in self.fail_open_registry if e.location == path or e.location.startswith(prefix)]

    # ── "safe ли X?" — static facts only, per COLLECT-21's AC ───────────

    def is_safe(self, location: str, access: Optional[str] = None, *, root: Optional[Path] = None) -> SafetyAnswer:
        """Delegates to `registries.AlreadySafeIndex.query`, which only
        ever consults `guarded_accesses`/`FAIL_OPEN_REGISTRY`/`CONTRACTS`
        (static/derived) — never an `LLMSummary`. On an absent model this
        answers `unknown` (`safe=False`), same as an unrecognised
        location on a real model."""
        if not self.available:
            return SafetyAnswer(False, "unknown")
        return self._already_safe_index(root=root).query(location, access=access)

    # ── coverage worklists ───────────────────────────────────────────────

    def zero_coverage(self) -> List[str]:
        return list(self.zero_coverage_list)

    def thin_coverage(self) -> List[str]:
        return list(self.thin_coverage_list)

    # ── the rest of the producer's tables, read-only ────────────────────

    def gates_for(self, name: Optional[str] = None) -> List[GateEntry]:
        if name is None:
            return list(self.gates)
        return [g for g in self.gates if g.name == name]

    def risk_for(self, path: str) -> Optional[RiskEntry]:
        for r in self.risk_index:
            if r.path == path:
                return r
        return None

    def config_map_for(self, section: Optional[str] = None) -> List[ConfigMapEntry]:
        if section is None:
            return list(self.config_map)
        return [c for c in self.config_map if c.section == section]

    # ── V1: the import graph, read-only ─────────────────────────────────

    def _test_paths(self) -> frozenset:
        """Test-file paths the artifact itself recorded: `test_map`'s values,
        keyed by the module they cover. Built per call — it is small (hundreds
        of paths) and that keeps this frozen dataclass free of a mutable cache.
        Only strings survive, so a partially written `test_map` cannot turn a
        query into an exception."""
        return frozenset(
            test
            for entries in self.test_map.values()
            for test in entries
            if isinstance(test, str)
        )

    @staticmethod
    def _is_test_path(path: str, test_paths: frozenset) -> bool:
        """Is `path` test code rather than shipped code?

        Two signals, in order: a path the artifact itself recorded as a test
        file (`test_map`'s values — authoritative for whatever directory they
        name), and the well-known test locations. The second is needed because
        `test_map` indexes coverage, not the test tree, and a stale artifact can
        omit whole test directories (this repo's `tests_bugfix/` has no entry
        at all).

        A bare `test_*.py` filename is deliberately **not** a signal: shipped
        code can be named that (`tools/collect/test_map.py` is a shipped module
        in this repo), and filtering it out of a caller list would hide a real
        importer under a plausible-looking name.
        """
        if not path:
            return False
        if path.startswith(_TEST_PATH_PREFIXES):
            return True
        if path.rsplit("/", 1)[-1] in _TEST_FILE_NAMES:
            return True
        return path in test_paths

    def callers_of(self, path: str, *, exclude_tests: bool = True, limit: int = 0) -> List[str]:
        """Every module that imports `path`, heaviest blast radius first.

        `exclude_tests=True` is the default and is not decoration: an importer
        list is usually dominated by a module's own test files, and those are
        already the `tests` row a caller is looking at. Ordering is by the
        caller's own `risk_index.blast_radius` descending, then path, so a
        truncated list keeps the callers that matter and two `load()` calls
        agree (COLLECT-3 determinism). `limit <= 0` means no limit.

        Read-only: the tables are never mutated. `[]` when the model is absent,
        `path` is not in the graph, or `exclude_tests` filters every importer
        out — never an exception.
        """
        if not self.available or not path:
            return []
        test_paths = self._test_paths() if exclude_tests else frozenset()
        blast_radius = {r.path: r.blast_radius for r in self.risk_index}
        out = [
            caller
            for caller in self.imported_by.get(path, ())
            if not (exclude_tests and self._is_test_path(caller, test_paths))
        ]
        out.sort(key=lambda caller: (-blast_radius.get(caller, 0), caller))
        return out if limit <= 0 else out[:limit]

    def calls_into(self, path: str, *, limit: int = 0) -> List[str]:
        """Every first-party module `path` imports, sorted by path.

        `graph.import_edges` already resolves names against the scanned module
        set and drops stdlib/third-party, so the result is first-party by
        construction; the extra filter only drops a target the artifact's
        `modules` table does not know about, which a partial artifact can carry
        and which must not surface as a phantom dependency. `limit <= 0` means
        no limit. `[]` on an absent model or an unknown `path`.
        """
        if not self.available or not path:
            return []
        known = {m.path for m in self.modules} if self.modules else None
        out = sorted(
            target
            for target in self.import_edges.get(path, ())
            if known is None or target in known
        )
        return out if limit <= 0 else out[:limit]


def _graph_table(value: object) -> Dict[str, Tuple[str, ...]]:
    """Normalise one of the two graph tables to `{path: (path, ...)}`.

    `None` means the producer never wrote the key and yields an empty dict, so
    an older artifact still loads with the data it does have. Anything else with
    the wrong shape raises `TypeError`, which `_load_from_dir`'s guarded block
    already turns into an absent model — a table that is present *and* garbled
    means the whole artifact is untrustworthy, the same stance as the malformed
    `modules` entry `test_partial_artifact_missing_keys_treated_as_absent` pins.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise TypeError(f"graph table must be an object, got {type(value).__name__}")
    out: Dict[str, Tuple[str, ...]] = {}
    for path, neighbours in value.items():
        if not isinstance(path, str) or not isinstance(neighbours, (list, tuple)):
            raise TypeError(f"graph table entry {path!r} is not path -> [path]")
        if not all(isinstance(neighbour, str) for neighbour in neighbours):
            raise TypeError(f"graph table entry {path!r} has a non-string neighbour")
        out[path] = tuple(neighbours)
    return out


def _string_tuple(value: object, name: str) -> Tuple[str, ...]:
    """Normalise a `[path]` artifact key to a tuple of paths.

    Same split as `_graph_table`: `None` is "the producer did not write this",
    so an empty tuple keeps the rest of the artifact alive; a wrong type raises
    into `_load_from_dir`'s guarded block and the model becomes absent.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{name} must be a list, got {type(value).__name__}")
    if not all(isinstance(path, str) for path in value):
        raise TypeError(f"{name} has a non-string entry")
    return tuple(value)


def _absent(collect_dir: Optional[Path], reason: str) -> CollectModel:
    return CollectModel(status=STATUS_ABSENT, collect_dir=collect_dir, reason=reason)


def _load_from_dir(collect_dir: Path, *, status: str, reason: str = "") -> CollectModel:
    """Read `artifact.json` out of `collect_dir` into a `CollectModel`.
    Any missing file, unreadable JSON, or missing/malformed key is treated
    as "no model" — a half-written or corrupted artifact must never raise
    out of the loader; it degrades to absent, same as if collect had never
    run at all."""
    artifact_path = collect_dir / cli_mod.ARTIFACT_FILENAME
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _absent(collect_dir, "artifact missing or unreadable — treated as absent")

    try:
        modules = tuple(ModuleRecord.from_dict(d) for d in payload.get("modules", []))
        contracts = tuple(
            ContractRecord(
                name=c["name"], kind=c.get("kind", "seed"), known_edge=c.get("known_edge"),
                description=c.get("description", ""), provenance=c.get("provenance", "static"),
            )
            for c in payload.get("contracts", [])
        )
        fail_open = tuple(
            FailOpenEntry(
                location=e["location"], exception_type=e["exception_type"],
                rationale=e.get("rationale"), provenance=e.get("provenance", "static"),
            )
            for e in payload.get("fail_open_registry", [])
        )
        gates = tuple(
            GateEntry(
                name=g["name"], module=g["module"], parser=g["parser"], protocol=g["protocol"],
                fail_mode=g["fail_mode"], extra_llm_call=g["extra_llm_call"],
                config_switch=g["config_switch"], config_default=g["config_default"],
                provenance=g.get("provenance", "static"),
            )
            for g in payload.get("gates", [])
        )
        test_map = {k: tuple(v) for k, v in payload.get("test_map", {}).items()}
        # V1: the three graph tables the producer has always written but this
        # reader dropped. Each is normalised through a helper that raises
        # TypeError on a wrong shape — the guard below already turns that into
        # an absent model — while a missing or null key yields an empty
        # container, so an older artifact still loads with the rest of its data.
        import_edges = _graph_table(payload.get("import_edges"))
        imported_by = _graph_table(payload.get("imported_by"))
        entry_points = _string_tuple(payload.get("entry_points"), "entry_points")
        risk_index = tuple(
            RiskEntry(
                path=r["path"], loc=r["loc"], blast_radius=r["blast_radius"],
                unguarded_count=r["unguarded_count"],
                undocumented_fail_open_count=r["undocumented_fail_open_count"],
                zero_coverage=r["zero_coverage"], score=r["score"],
            )
            for r in payload.get("risk_index", [])
        )
        config_map = tuple(
            ConfigMapEntry(
                section=c["section"], key_template=c["key_template"], readers=tuple(c["readers"]),
                fallbacks=tuple(c["fallbacks"]), has_mode_override=c["has_mode_override"],
                concrete_keys=tuple(c["concrete_keys"]), provenance=c.get("provenance", "derived"),
            )
            for c in payload.get("config_map", [])
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        # Bugfix: a valid-JSON-but-non-dict artifact.json made
        # payload.get(...) raise AttributeError, which this tuple missed —
        # breaking the docstring's "must never raise out of the loader".
        return _absent(collect_dir, "artifact has an unexpected shape — treated as absent")

    return CollectModel(
        status=status,
        collect_dir=collect_dir,
        modules=modules,
        contracts=contracts,
        fail_open_registry=fail_open,
        gates=gates,
        test_map=test_map,
        zero_coverage_list=tuple(payload.get("zero_coverage", [])),
        thin_coverage_list=tuple(payload.get("thin_coverage", [])),
        risk_index=risk_index,
        config_map=config_map,
        import_edges=import_edges,
        imported_by=imported_by,
        entry_points=entry_points,
        reason=reason,
    )


def load(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
) -> CollectModel:
    """Load the collect model for `root`, applying `[collect] staleness`
    when the on-disk artifact is out of date. Never raises for anything
    to do with the artifact itself — a `CollectCliError`/citation error
    from an actual `--refresh` rebuild (bad seed data etc.) still
    propagates, since that's the same hard-failure guarantee COLLECT-10/15
    give the producer side and swallowing it here would undo that."""
    root = Path(root)
    collect_dir = cli_mod.resolve_collect_dir(root, config)
    manifest_path = collect_dir / cli_mod.MANIFEST_FILENAME
    artifact_path = collect_dir / cli_mod.ARTIFACT_FILENAME

    if not manifest_path.exists() or not artifact_path.exists():
        return _absent(collect_dir, "no manifest/artifact — collect has never run")

    try:
        existing_manifest = manifest_mod.read_manifest(manifest_path)
    except (OSError, ValueError):
        return _absent(collect_dir, "manifest is unreadable — treated as absent")

    current_paths = [m.path for m in scan_repo(root, config=config)]
    fresh = manifest_mod.is_fresh(existing_manifest, root, files=current_paths)
    if fresh:
        return _load_from_dir(collect_dir, status=STATUS_FRESH)

    policy = _staleness_policy(config)
    if policy == "ignore":
        return _absent(collect_dir, "stale artifact, staleness=ignore — treated as absent")
    if policy == "refresh":
        cli_mod.action_refresh(root, config=config, config_path=config_path)
        return _load_from_dir(collect_dir, status=STATUS_FRESH)
    # policy == "warn" (default / fallback)
    return _load_from_dir(collect_dir, status=STATUS_STALE, reason="artifact is stale (a tracked file changed)")
