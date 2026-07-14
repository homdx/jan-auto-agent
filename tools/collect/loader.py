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
    except (KeyError, TypeError, ValueError):
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
