"""tools/collect/cli.py — COLLECT-19: `/collect` command / `--collect` flag.

The Producer side of `collect` mode (EPIC F): orchestrates Pass A (scan) →
Pass B (LLM summarizer, optional) → Pass C (verification gate) → the EPIC
C/D builders (fail-open registry, contracts, gates, import graph, TEST_MAP,
RISK_INDEX, CONFIG_MAP) → COLLECT-18's renderers, and writes the result.

Read-only / write-only split (COLLECT-19 AC)
---------------------------------------------
Every read this module (or anything it calls) performs against the scanned
project is a `read_text`/`open(..., "r")`/`stat()` — scanner.py,
registries.py, gates.py, test_map.py, risk.py, config_map.py are all
read-only by construction (see each module's own docstring). This module
adds exactly one write surface: `_write_artifact`/`_write_manifest` below,
and every path either writes is built from `resolve_collect_dir`, which
always returns a path under `root / [collect] dir` (default
`root/.collect`). There is no other `open(..., "w")` / `Path.write_text` /
`Path.mkdir` anywhere in this module's four actions — that is what makes
"collect physically cannot modify a file outside `[collect] dir`" true by
construction rather than by convention, and what `tests/test_collect_cli.py`
checks by hashing the whole source tree before/after every action.

Actions
-------
``check``   — freshness check only (`manifest.is_fresh`). Never writes
              anything, anywhere — matches `--check`'s brief exactly.
``collect`` — one-shot (`--collect` / `/collect`): build if `.collect/` is
              missing or stale, otherwise a no-op. This is what running
              `collect` "just in case" should cost: nothing, once fresh.
``refresh`` — diff-driven incremental rebuild (`--refresh`), regardless of
              current freshness: Pass A always re-runs (cheap, no LLM),
              but Pass B (`llm_call`) only runs for modules whose content
              hash changed since the last manifest — every unchanged
              module's record, summary included, is reused verbatim
              (COLLECT-24). Falls back to a full build when there is no
              prior artifact to diff against.
``module``  — incremental (`--module <path>`): re-scan *only* that file,
              patch its record into the existing artifact (every other
              module's `ModuleRecord` is reused, not re-parsed), and patch
              only that file's manifest entry. Falls back to a full
              `refresh` when there is no existing artifact to patch into.
"""

from __future__ import annotations

import configparser
import json
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.collect import config_map as config_map_mod
from tools.collect import gates as gates_mod
from tools.collect import graph as graph_mod
from tools.collect import manifest as manifest_mod
from tools.collect import registries as registries_mod
from tools.collect import render as render_mod
from tools.collect import risk as risk_mod
from tools.collect import test_map as test_map_mod
from tools.collect import verifier as verifier_mod
from tools.collect._determinism import canonical_dumps
from tools.collect.model import ModuleRecord
from tools.collect.scanner import scan_file, scan_repo
from tools.collect.summarizer import LlmCall, summarize_repo

DEFAULT_COLLECT_DIR = ".collect"
ARTIFACT_FILENAME = "artifact.json"
MANIFEST_FILENAME = "collect_manifest.json"
VERIFICATION_REPORT_FILENAME = "verification_report.json"

VALID_ACTIONS = frozenset({"check", "collect", "refresh", "module"})


class CollectCliError(RuntimeError):
    """Raised for a usage error in this CLI (bad action, missing --module
    path, etc.) — never for anything the underlying builders themselves
    raise (a stale seed's `ContractCitationError`/`GateCitationError`
    propagates as-is, since silently swallowing a hard-failure citation
    check here would undo exactly the guarantee COLLECT-10/15 exist for)."""


@dataclass
class CollectResult:
    """What every action returns: whether anything was written, why (or
    why not), and — for `check`/`collect`/`refresh` — the freshness verdict
    that drove the decision."""

    action: str
    wrote: bool
    fresh: Optional[bool]
    message: str
    collect_dir: Optional[Path] = None
    written_files: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "wrote": self.wrote,
            "fresh": self.fresh,
            "message": self.message,
            "collect_dir": str(self.collect_dir) if self.collect_dir else None,
            "written_files": list(self.written_files),
        }


# ── config plumbing (COLLECT-20: `[collect]` section) ───────────────────────

VALID_STALENESS = frozenset({"warn", "refresh", "ignore"})
DEFAULT_STALENESS = "warn"


@dataclass(frozen=True)
class CollectSettings:
    """Everything `[collect]` in `agents.ini` can configure, each with a
    safe default equal to what happens when the section is absent
    entirely. Read via `read_collect_settings` — nothing else in this
    module or `main.py` should call `config.get("collect", ...)` directly,
    so there is exactly one place that has to know the key names and
    defaults."""

    enabled: bool = True
    dir: str = DEFAULT_COLLECT_DIR
    use_in_auto: bool = False
    use_in_doc: bool = False
    use_in_bughunt: bool = False
    staleness: str = DEFAULT_STALENESS
    llm_summaries: bool = True
    think: bool = False


def _get_bool(config: configparser.ConfigParser, key: str, default: bool) -> bool:
    """`ConfigParser.getboolean` raises `ValueError` on an unparseable
    value (e.g. `enabled = maybe`) — COLLECT-20's AC is that a bad value
    falls back to the default rather than crashing the whole run, same
    posture as the `staleness` fallback below."""
    try:
        return config.getboolean("collect", key, fallback=default)
    except ValueError:
        return default


def read_collect_settings(config: Optional[configparser.ConfigParser]) -> CollectSettings:
    """`[collect]` section reader. A missing section — or a missing
    `agents.ini` entirely (``config=None``) — returns every default
    unchanged, which by construction is "today's behavior": nothing in
    this module or `main.py` treats collect mode as active unless a
    human explicitly configured it (or accepted the ``enabled=true``
    default) *and* ran `--collect`/`/collect`. An invalid `staleness`
    (typo, stale value) falls back to `"warn"` rather than raising —
    config authoring mistakes should degrade to the safest mode, not
    take down the run."""
    if config is None or not config.has_section("collect"):
        return CollectSettings()

    dir_value = config.get("collect", "dir", fallback=DEFAULT_COLLECT_DIR).strip() or DEFAULT_COLLECT_DIR

    staleness = config.get("collect", "staleness", fallback=DEFAULT_STALENESS).strip().lower()
    if staleness not in VALID_STALENESS:
        staleness = DEFAULT_STALENESS

    return CollectSettings(
        enabled=_get_bool(config, "enabled", True),
        dir=dir_value,
        use_in_auto=_get_bool(config, "use_in_auto", False),
        use_in_doc=_get_bool(config, "use_in_doc", False),
        use_in_bughunt=_get_bool(config, "use_in_bughunt", False),
        staleness=staleness,
        llm_summaries=_get_bool(config, "llm_summaries", True),
        think=_get_bool(config, "think", False),
    )


def resolve_collect_dir(root: Path, config: Optional[configparser.ConfigParser]) -> Path:
    """`[collect] dir` (default `.collect`), resolved relative to `root` if
    given as a relative path. This is the *only* function in this module
    that decides where writes may land — every write path in this module
    is built from its return value."""
    settings = read_collect_settings(config)
    path = Path(settings.dir)
    if not path.is_absolute():
        path = Path(root) / path
    return path


# ── Pass A→D: build the full in-memory context ─────────────────────────────


@dataclass
class CollectContext:
    modules: List[ModuleRecord]
    import_edges: Dict[str, frozenset]
    imported_by: Dict[str, frozenset]
    entry_points: List[str]
    contracts: list
    fail_open_registry: list
    gates: list
    test_map: Dict[str, Tuple[str, ...]]
    zero_coverage: List[str]
    thin_coverage: List[str]
    risk_index: list
    config_map: list
    sibling_gaps: list
    verification_report: Optional[Dict[str, Any]]


def _sources_for(root: Path, modules: List[ModuleRecord]) -> Dict[str, str]:
    """Re-read every non-`parse_error` module's source once, for Pass
    C's line-count check and Pass B's prompt — the one place this module
    reads file content beyond what `scan_repo` already did, and still
    strictly read-only (`read_text`)."""
    sources: Dict[str, str] = {}
    for m in modules:
        if m.parse_error is not None:
            continue
        try:
            sources[m.path] = (Path(root) / m.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # BUGFIX: same class of bug as scanner.scan_repo/graph.
            # build_call_edges/risk._loc — only OSError was caught here,
            # so a file that changed to invalid UTF-8 on disk between the
            # scan and this re-read (or otherwise slipped through without
            # a `parse_error`) crashed Pass B/C instead of just being
            # skipped, the same "strictly read-only, best-effort" contract
            # this function's own docstring describes.
            continue
    return sources


def _sibling_gaps(root: Path, config: Optional[configparser.ConfigParser], config_path: Optional[str]):
    """`[collect] sibling_profiles = agents_32k.ini,agents_stub.ini` (comma
    list, optional) diffed against the primary config file. Empty by
    default — this module makes no assumption about which `.ini` files are
    "siblings" without being told."""
    if config is None or not config_path:
        return []
    raw = config.get("collect", "sibling_profiles", fallback="")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        return []
    siblings = {name: Path(root) / name for name in names}
    return config_map_mod.diff_sibling_profiles(Path(config_path), siblings)


def build_context(
    root: Path,
    modules: List[ModuleRecord],
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectContext:
    """Run Pass B (only if `llm_call` is given) → Pass C → every EPIC C/D
    builder, over an already-scanned `modules` list. Kept separate from
    `scan_repo` so `--module`'s incremental path can call this over a
    patched module list without re-scanning the whole tree."""
    sources = _sources_for(root, modules)

    verification_report: Optional[Dict[str, Any]] = None
    if llm_call is not None:
        summarized = summarize_repo(modules, sources, llm_call)
        modules, verification_report = verifier_mod.verify_repo(summarized, sources, root=root)

    edges = graph_mod.import_edges(modules)
    reverse = graph_mod.imported_by(edges)
    entries = graph_mod.entry_points(edges, reverse)

    fail_open = registries_mod.build_fail_open_registry(modules, root=root)
    contracts = registries_mod.build_seed_contracts(modules, root=root)
    gates = gates_mod.build_gates_map(modules, root)

    tmap = test_map_mod.build_test_map(root, modules)
    zero = test_map_mod.zero_coverage(tmap)
    thin = test_map_mod.thin_coverage(tmap)

    risk_entries = risk_mod.compute_risk_index(
        modules, imported_by=reverse, fail_open_registry=fail_open, test_map=tmap, root=root,
    )
    cmap = config_map_mod.build_config_map(modules)
    gaps = _sibling_gaps(root, config, config_path)

    return CollectContext(
        modules=modules,
        import_edges=edges,
        imported_by=reverse,
        entry_points=entries,
        contracts=contracts,
        fail_open_registry=fail_open,
        gates=gates,
        test_map=tmap,
        zero_coverage=zero,
        thin_coverage=thin,
        risk_index=risk_entries,
        config_map=cmap,
        sibling_gaps=gaps,
        verification_report=verification_report,
    )


def _artifact_dict(ctx: CollectContext) -> Dict[str, Any]:
    return {
        "modules": [m.to_dict() for m in ctx.modules],
        "import_edges": {k: sorted(v) for k, v in ctx.import_edges.items()},
        "imported_by": {k: sorted(v) for k, v in ctx.imported_by.items()},
        "entry_points": list(ctx.entry_points),
        "contracts": [
            {
                "name": c.name, "kind": c.kind, "known_edge": c.known_edge,
                "description": c.description, "provenance": c.provenance,
            }
            for c in ctx.contracts
        ],
        "fail_open_registry": [e.to_dict() for e in ctx.fail_open_registry],
        "gates": [g.to_dict() for g in ctx.gates],
        "test_map": {k: list(v) for k, v in ctx.test_map.items()},
        "zero_coverage": list(ctx.zero_coverage),
        "thin_coverage": list(ctx.thin_coverage),
        "risk_index": [r.to_dict() for r in ctx.risk_index],
        "config_map": [e.to_dict() for e in ctx.config_map],
        "sibling_gaps": [g.to_dict() for g in ctx.sibling_gaps],
    }


def _write_artifact(collect_dir: Path, ctx: CollectContext) -> List[str]:
    """Write `artifact.json`, `verification_report.json` (if Pass B ran),
    and all nine rendered markdown pages into `collect_dir`. This is the
    only function in the module that ever opens a path for writing."""
    collect_dir.mkdir(parents=True, exist_ok=True)
    written: List[str] = []

    artifact_path = collect_dir / ARTIFACT_FILENAME
    artifact_path.write_text(canonical_dumps(_artifact_dict(ctx), check_forbidden=False) + "\n", encoding="utf-8")
    written.append(ARTIFACT_FILENAME)

    if ctx.verification_report is not None:
        report_path = collect_dir / VERIFICATION_REPORT_FILENAME
        report_path.write_text(
            json.dumps(ctx.verification_report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written.append(VERIFICATION_REPORT_FILENAME)

    pages = render_mod.render_all(
        modules=ctx.modules,
        import_edges=ctx.import_edges,
        imported_by=ctx.imported_by,
        entry_points=ctx.entry_points,
        contracts=ctx.contracts,
        fail_open_registry=ctx.fail_open_registry,
        gates=ctx.gates,
        test_map=ctx.test_map,
        zero_coverage=ctx.zero_coverage,
        thin_coverage=ctx.thin_coverage,
        risk_index=ctx.risk_index,
        config_map=ctx.config_map,
        sibling_gaps=ctx.sibling_gaps,
    )
    for name, content in pages.items():
        (collect_dir / name).write_text(content, encoding="utf-8")
        written.append(name)

    return sorted(written)


def _write_manifest(
    root: Path,
    collect_dir: Path,
    modules: List[ModuleRecord],
    *,
    provenance: Optional[Tuple[Optional[str], bool]] = None,
) -> None:
    """`provenance`, if given, must be a `(git_sha, dirty)` pair captured
    via `manifest_mod.capture_provenance(root)` *before* `_write_artifact`
    ran — see that function's docstring for why the ordering matters."""
    files = sorted(m.path for m in modules)
    manifest = manifest_mod.build_manifest(root, files, provenance=provenance)
    manifest_mod.write_manifest(manifest, collect_dir / MANIFEST_FILENAME)


# ── the four actions ─────────────────────────────────────────────────────────


def action_check(root: Path, *, config: Optional[configparser.ConfigParser] = None) -> CollectResult:
    """`--check`: freshness only. Never writes — not the manifest, not the
    artifact, nothing — regardless of what it finds."""
    collect_dir = resolve_collect_dir(root, config)
    manifest_path = collect_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return CollectResult(
            action="check", wrote=False, fresh=False,
            message=f"no manifest at {manifest_path} — collect has never run",
            collect_dir=collect_dir,
        )
    try:
        existing = manifest_mod.read_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        return CollectResult(
            action="check", wrote=False, fresh=False,
            message=f"manifest at {manifest_path} is unreadable ({exc}) — treat as stale",
            collect_dir=collect_dir,
        )
    current_paths = [m.path for m in scan_repo(root, config=config)]
    fresh = manifest_mod.is_fresh(existing, root, files=current_paths)
    return CollectResult(
        action="check", wrote=False, fresh=fresh,
        message="up to date" if fresh else "stale — a tracked file changed since the last collect run",
        collect_dir=collect_dir,
    )


def _full_build(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser],
    config_path: Optional[str],
    llm_call: Optional[LlmCall],
) -> Tuple[Path, List[str], CollectContext]:
    # Captured before anything under `[collect] dir` is written: `.collect/`
    # isn't git-ignored, so if this ran *after* `_write_artifact`, the
    # collector's own new/changed output files would make `git status
    # --porcelain` non-empty and `dirty` would read True on every full
    # build regardless of whether the tracked source tree is clean.
    # The dir itself is passed so *previous* runs' untracked output is
    # excluded by path too — ordering alone only protects the very first
    # build (see `manifest.is_dirty`).
    collect_dir = resolve_collect_dir(root, config)
    provenance = manifest_mod.capture_provenance(root, collect_dir=collect_dir)
    modules = scan_repo(root, config=config)
    ctx = build_context(root, modules, config=config, config_path=config_path, llm_call=llm_call)
    written = _write_artifact(collect_dir, ctx)
    _write_manifest(root, collect_dir, ctx.modules, provenance=provenance)
    written = sorted(set(written) | {MANIFEST_FILENAME})
    return collect_dir, written, ctx


def action_refresh(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectResult:
    """`--refresh`: diff-driven incremental rebuild (COLLECT-24).

    Pass A (AST scan) is always cheap and re-runs over the whole tree —
    it does no network I/O and is byte-deterministic (COLLECT-3), so
    there's nothing to gain by trying to skip it file-by-file. What *is*
    expensive is Pass B (`llm_call`), so that's the part this function
    actually makes incremental: the fresh Pass A scan is diffed against
    the previous manifest's file hashes, and only the paths that come
    back `added`/`modified` (`manifest.diff_files`) are handed to Pass B.
    Every unchanged module — the common case on a typical re-run — keeps
    its previous `ModuleRecord` verbatim, `summary` (and thus `purpose`)
    included, so it is never re-sent to an LLM.

    Pass C (`verifier.verify_repo`) still runs over the full merged
    module list on every call, because a citation check needs the
    *current* whole-repo symbol table to be correct — but Pass C is pure
    computation (no LLM call), so this costs nothing extra by the
    `--refresh`-costs-zero-LLM-calls-on-an-unchanged-tree measure
    (COLLECT-24 AC).

    Falls back to an unconditional full build — today's previous
    behaviour — when there is no existing manifest+artifact pair to diff
    against; there is nothing to be "incremental" relative to.
    """
    root = Path(root)
    collect_dir = resolve_collect_dir(root, config)
    manifest_path = collect_dir / MANIFEST_FILENAME
    artifact_path = collect_dir / ARTIFACT_FILENAME

    previous_manifest: Optional[manifest_mod.Manifest] = None
    previous_by_path: Dict[str, ModuleRecord] = {}
    if manifest_path.exists() and artifact_path.exists():
        try:
            previous_manifest = manifest_mod.read_manifest(manifest_path)
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            previous_by_path = {d["path"]: ModuleRecord.from_dict(d) for d in payload.get("modules", [])}
        except (OSError, ValueError, KeyError):
            previous_manifest = None
            previous_by_path = {}

    if previous_manifest is None:
        collect_dir, written, _ctx = _full_build(root, config=config, config_path=config_path, llm_call=llm_call)
        return CollectResult(
            action="refresh", wrote=True, fresh=True,
            message=f"no prior artifact to diff against — full build: rebuilt {len(written)} file(s) in {collect_dir}",
            collect_dir=collect_dir, written_files=tuple(written),
        )

    # Same ordering requirement as `_full_build`: capture provenance now,
    # before `_write_artifact` below writes anything under `.collect/` —
    # and exclude the collect dir by path, since the *previous* run's
    # output is already untracked before this run writes anything.
    provenance = manifest_mod.capture_provenance(root, collect_dir=collect_dir)

    current_modules = scan_repo(root, config=config)
    current_hashes = manifest_mod.hash_tree(root, [m.path for m in current_modules])
    changes = manifest_mod.diff_files(previous_manifest.file_hashes, current_hashes)

    to_summarize = [m for m in current_modules if m.path in changes.changed]

    settings = read_collect_settings(config)
    summarized_by_path: Dict[str, ModuleRecord] = {}
    if llm_call is not None and settings.llm_summaries and to_summarize:
        sources_for_summary: Dict[str, str] = {}
        for m in to_summarize:
            if m.parse_error is not None:
                continue
            try:
                sources_for_summary[m.path] = (root / m.path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # BUGFIX: this was an unguarded dict-comprehension read —
                # a file that changed on disk (deleted, permissions,
                # became invalid UTF-8) in the window between scan_repo's
                # own read a few lines up and this re-read for the
                # summarizer prompt crashed the whole refresh instead of
                # just leaving that one module out of this round's
                # summarization batch, the same fail-open posture
                # `_sources_for` (Pass C) already gives the same situation.
                continue
        summarized_by_path = {
            m.path: m for m in summarize_repo(to_summarize, sources_for_summary, llm_call)
        }

    merged: List[ModuleRecord] = []
    for m in current_modules:
        if m.path in changes.changed:
            merged.append(summarized_by_path.get(m.path, m))
        else:
            # Unchanged since the last manifest: reuse the previous
            # record verbatim (summary included) rather than the
            # freshly re-parsed one — this is what keeps an unchanged
            # module's `purpose` byte-identical across `--refresh` runs.
            merged.append(previous_by_path.get(m.path, m))
    merged.sort(key=lambda m: m.path)

    ctx = build_context(root, merged, config=config, config_path=config_path, llm_call=None)
    if any(m.summary is not None for m in merged):
        sources = _sources_for(root, merged)
        verified_modules, report = verifier_mod.verify_repo(merged, sources, root=root)
        ctx = replace(ctx, modules=verified_modules, verification_report=report)

    written = _write_artifact(collect_dir, ctx)
    _write_manifest(root, collect_dir, ctx.modules, provenance=provenance)
    written = sorted(set(written) | {MANIFEST_FILENAME})

    if changes.is_empty():
        message = f"tree unchanged — recomputed derived artifacts only, wrote {len(written)} file(s) in {collect_dir}"
    else:
        message = (
            f"incrementally refreshed {len(changes.changed)} changed and "
            f"{len(changes.removed)} removed module(s); wrote {len(written)} file(s) in {collect_dir}"
        )

    return CollectResult(
        action="refresh", wrote=True, fresh=True,
        message=message, collect_dir=collect_dir, written_files=tuple(written),
    )


def action_collect(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectResult:
    """`--collect` / `/collect`: one-shot. Builds only if there is no
    manifest yet, or the existing one is stale; a fresh tree is a no-op —
    no write of any kind, same as `check` would report."""
    check_result = action_check(root, config=config)
    if check_result.fresh:
        return CollectResult(
            action="collect", wrote=False, fresh=True,
            message="already up to date — nothing to do",
            collect_dir=check_result.collect_dir,
        )
    collect_dir, written, _ctx = _full_build(root, config=config, config_path=config_path, llm_call=llm_call)
    return CollectResult(
        action="collect", wrote=True, fresh=True,
        message=f"built {len(written)} file(s) in {collect_dir}",
        collect_dir=collect_dir, written_files=tuple(written),
    )


def action_module(
    root: Path,
    module_path: str,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectResult:
    """`--module <path>`: incremental. Re-scans and re-parses *only*
    `module_path`; every other module's `ModuleRecord` is reused verbatim
    from the last artifact — falls back to a full `refresh` when there is
    no existing artifact to patch into (nothing to be "incremental"
    relative to)."""
    root = Path(root)
    collect_dir = resolve_collect_dir(root, config)
    artifact_path = collect_dir / ARTIFACT_FILENAME
    if not artifact_path.exists():
        result = action_refresh(root, config=config, config_path=config_path, llm_call=llm_call)
        return CollectResult(
            action="module", wrote=result.wrote, fresh=result.fresh,
            message=f"no existing artifact — ran a full refresh instead ({result.message})",
            collect_dir=result.collect_dir, written_files=result.written_files,
        )

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectCliError(f"existing artifact at {artifact_path} is unreadable: {exc}") from exc

    modules = [ModuleRecord.from_dict(d) for d in payload.get("modules", [])]
    by_path = {m.path: m for m in modules}

    abs_module = root / module_path
    if not abs_module.is_file():
        raise CollectCliError(f"--module path does not exist under {root}: {module_path}")
    try:
        source = abs_module.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # BUGFIX: this used to be a bare `read_text()` call — a permission
        # error or a file that isn't valid UTF-8 raised straight out of
        # action_module as an unhandled exception (a raw traceback) rather
        # than the clean CollectCliError every other user-facing failure
        # in this function (a missing path just above, an unreadable
        # artifact just above that) already gets.
        raise CollectCliError(f"--module path is unreadable: {module_path}: {exc}") from exc
    patched = scan_file(source, module_path, config=config)

    if module_path not in by_path:
        modules.append(patched)
    else:
        modules = [patched if m.path == module_path else m for m in modules]
    modules.sort(key=lambda m: m.path)

    # Captured before `_write_artifact` below writes anything under
    # `.collect/` — see `capture_provenance`'s docstring. Same ordering
    # bug as `_full_build` otherwise: `.collect/` isn't git-ignored, so
    # computing this after the write would see the write's own untracked
    # output and report `dirty=True` regardless of the tracked tree.
    # `collect_dir` is passed so prior runs' output is excluded by path.
    git_sha, dirty = manifest_mod.capture_provenance(root, collect_dir=collect_dir)

    ctx = build_context(root, modules, config=config, config_path=config_path, llm_call=llm_call)
    written = _write_artifact(collect_dir, ctx)

    # Patch only the changed file's manifest entry rather than rehashing
    # every tracked file — the incremental counterpart to a full rebuild's
    # `_write_manifest`.
    manifest_path = collect_dir / MANIFEST_FILENAME
    if manifest_path.exists():
        try:
            previous = manifest_mod.read_manifest(manifest_path)
            hashes = dict(previous.file_hashes)
        except (OSError, ValueError):
            hashes = {}
    else:
        hashes = {}
    hashes[module_path] = manifest_mod.hash_file(abs_module)
    patched_manifest = manifest_mod.Manifest(
        collector_version=manifest_mod.COLLECTOR_VERSION,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        git_sha=git_sha,
        dirty=dirty,
        file_hashes=hashes,
    )
    manifest_mod.write_manifest(patched_manifest, manifest_path)
    written = sorted(set(written) | {MANIFEST_FILENAME})

    return CollectResult(
        action="module", wrote=True, fresh=True,
        message=f"patched {module_path} and refreshed {len(written)} file(s) in {collect_dir}",
        collect_dir=collect_dir, written_files=tuple(written),
    )


def run(
    root: Path,
    action: str,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    module_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectResult:
    """Single dispatch point for all four actions — what `main.py`'s
    `/collect` command / `--collect`/`--check`/`--refresh`/`--module`
    flags call."""
    if action not in VALID_ACTIONS:
        raise CollectCliError(f"unknown collect action {action!r}; must be one of {sorted(VALID_ACTIONS)}")
    root = Path(root)
    if action == "check":
        return action_check(root, config=config)

    settings = read_collect_settings(config)
    if not settings.enabled and action != "check":
        return CollectResult(
            action=action, wrote=False, fresh=None,
            message="collect is disabled ([collect] enabled = false) — nothing done",
            collect_dir=resolve_collect_dir(root, config),
        )
    if action == "refresh":
        return action_refresh(root, config=config, config_path=config_path, llm_call=llm_call)
    if action == "module":
        if not module_path:
            raise CollectCliError("action='module' requires module_path")
        return action_module(root, module_path, config=config, config_path=config_path, llm_call=llm_call)
    return action_collect(root, config=config, config_path=config_path, llm_call=llm_call)


# ── argparse-level entry point (mirrors --auto / --faq in main.py) ─────────


def parse_collect_args(argv: List[str]) -> Dict[str, Any]:
    """Parse the collect-specific slice of argv into `run()` kwargs. Kept
    separate from stdlib `argparse` so `main.py` can add `--collect`,
    `--check`, `--refresh`, and `--module` to its existing parser and just
    forward here — see that module's own `_parse_args` for the actual flag
    definitions."""
    action = "collect"
    module_path = None
    if "--check" in argv:
        action = "check"
    elif "--refresh" in argv:
        action = "refresh"
    elif "--module" in argv:
        action = "module"
        idx = argv.index("--module")
        if idx + 1 >= len(argv):
            raise CollectCliError("--module requires a path argument")
        module_path = argv[idx + 1]
    return {"action": action, "module_path": module_path}


def main(argv: List[str], root: Optional[str] = None, config_path: str = "agents.ini") -> int:
    """`python main.py --collect ...` entry point. Prints a one-line
    summary and returns a process exit code: 0 on success, 1 on any
    `CollectCliError`/citation failure so the caller can `sys.exit` it."""
    base = Path(root or ".").resolve()
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if Path(config_path).exists():
        config.read(config_path, encoding="utf-8")

    try:
        kwargs = parse_collect_args(argv)
        result = run(base, config=config, config_path=config_path, **kwargs)
    except CollectCliError as exc:
        print(f"collect: {exc}")
        return 1
    except (registries_mod.ContractCitationError, gates_mod.GateCitationError) as exc:
        print(f"collect: stale seed data — {exc}")
        return 1

    print(f"collect {result.action}: {result.message}")
    return 0
