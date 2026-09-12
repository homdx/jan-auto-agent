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
`Path.mkdir` anywhere in this module's five actions — that is what makes
"collect physically cannot modify a file outside `[collect] dir`" true by
construction rather than by convention, and what `tests/test_collect_cli.py`
checks by hashing the whole source tree before/after every action.

Actions
-------
``check``   — freshness check only (`manifest.is_fresh`). Never writes
              anything, anywhere — matches `--check`'s brief exactly.
``collect`` — one-shot (`--collect` / `/collect`): freshness-gated. A
              fresh tree is a no-op (no write of any kind). A stale tree
              delegates to `action_refresh`, so only the modules whose
              content hash changed since the last manifest are re-summarized
              — never the whole tree. This is what running `collect` "just
              in case" should cost: nothing once fresh, and one Pass B call
              per *changed* module when something moved.
``refresh`` — diff-driven incremental rebuild (`--refresh`), regardless of
              current freshness: Pass A always re-runs (cheap, no LLM),
              but Pass B (`llm_call`) only runs for modules whose content
              hash changed since the last manifest — every unchanged
              module's record, summary included, is reused verbatim
              (COLLECT-24). Falls back to a full build when there is no
              prior artifact to diff against.
``rebuild`` — unconditional full rebuild (`--collect --rebuild` /
              `/collect --rebuild`): `action_rebuild` → `_full_build`,
              every module re-scanned and re-summarized regardless of
              freshness, of which files changed, and of any prior
              artifact. The deliberate escape hatch (after a
              `collector_version` bump, or to discard a suspect artifact),
              not the default — `collect` and `refresh` are both
              incremental.
``module``  — incremental (`--module <path>`): re-scan *only* that file,
              patch its record into the existing artifact (every other
              module's `ModuleRecord` is reused, not re-parsed), and patch
              only that file's manifest entry. Falls back to a full
              `refresh` when there is no existing artifact to patch into.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

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
from tools.collect.scanner import language_for, scan_file, scan_repo
from tools.collect.summarizer import LlmCall, collect_max_retries, summarize_repo


def _print_summarize_progress(done: int, total: int, module_path: str) -> None:
    """Simple `[done/total]` progress line printed to stdout after each
    module's LLM call finishes (COLLECT Pass B)."""
    print(f"[{done}/{total}] summarized {module_path}", flush=True)


def _print_summarize_error(module_path: str, exc: Exception, error_count: int) -> None:
    """Printed once per *outer* retry (a module whose LLM call raised),
    on top of the per-HTTP-request retries `request_completion` already
    logs itself — so a module that's about to be retried shows up in the
    same progress stream instead of just going quiet for a while."""
    print(f"  ! {module_path}: {exc} (retry {error_count})", flush=True)

DEFAULT_COLLECT_DIR = ".collect"
ARTIFACT_FILENAME = "artifact.json"
MANIFEST_FILENAME = "collect_manifest.json"
VERIFICATION_REPORT_FILENAME = "verification_report.json"

VALID_ACTIONS = frozenset({"check", "collect", "refresh", "rebuild", "module"})


class CollectCliError(RuntimeError):
    """Raised for a usage error in this CLI (bad action, missing --module
    path, etc.) — never for anything the underlying builders themselves
    raise (a stale seed's `ContractCitationError`/`GateCitationError`
    propagates as-is, since silently swallowing a hard-failure citation
    check here would undo exactly the guarantee COLLECT-10/15 exist for)."""


@dataclass
class CollectResult:
    """What every action returns: whether anything was written, why (or
    why not), and — for `check`/`collect`/`refresh`/`rebuild` — the
    freshness verdict that drove the decision."""

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
    is built from its return value.

    AUTO-FIX (high-priority audit, DeepSeek-plan finding): a misconfigured
    `[collect] dir` (e.g. `../../etc`, or an absolute path pointing
    elsewhere entirely) used to resolve without any containment check —
    and since this function's own docstring says every write in the
    module derives from its return value, that meant a config typo could
    make `_write_artifact` write files outside the project entirely.
    `root` itself may not exist yet on a fresh checkout, so containment is
    checked with `os.path.normpath` on the unresolved path rather than
    `Path.resolve()`/`is_relative_to()` (which would require the path to
    already exist to be meaningful, and can behave surprisingly around
    symlinks) — this is a config-sanity check, not a symlink-attack
    defense.
    """
    settings = read_collect_settings(config)
    path = Path(settings.dir)
    if not path.is_absolute():
        path = Path(root) / path
    normalized_path = Path(os.path.normpath(str(path)))
    normalized_root = Path(os.path.normpath(str(root)))
    try:
        normalized_path.relative_to(normalized_root)
    except ValueError:
        raise CollectCliError(
            f"[collect] dir resolves to {normalized_path}, which is "
            f"outside the project root {normalized_root} — refusing to "
            f"write there. Check the `[collect] dir` setting in your "
            f"config for a stray absolute path or `..` traversal."
        ) from None
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

    # Built before Pass B/C on purpose: Pass C (V11) needs the import graph
    # to know what a test file may cite, and a summary changes no import.
    edges = graph_mod.import_edges(modules)

    verification_report: Optional[Dict[str, Any]] = None
    if llm_call is not None:
        summarized = summarize_repo(
            modules, sources, llm_call,
            max_retries=collect_max_retries(config),
            progress_fn=_print_summarize_progress,
            on_error=_print_summarize_error,
        )
        modules, verification_report = verifier_mod.verify_repo(
            summarized, sources, root=root, import_edges=edges,
        )

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
    only function in the module that ever opens a path for writing.

    AUTO-FIX (medium-priority audit, NVIDIA-plan finding): every write
    below (`mkdir` + 9+ `write_text` calls) used to be completely
    unguarded. A mid-batch failure (disk full, permission change) left a
    partially-written `.collect/` directory — some pages from this run,
    some stale from the last one — with only a bare, file-less stdlib
    OSError to explain it. Every write is now wrapped so a failure names
    exactly which file it was on before re-raising; this doesn't change
    what happens on failure (still raises — a partial collect artifact is
    a genuine build failure, not something to silently skip past), just
    what the caller sees when it does.

    BUGFIX: `verification_report.json` is the one derived file whose write
    is conditional (`ctx.verification_report is not None` — Pass B/C only
    ran when there is something to verify), while every rendered page is
    rewritten unconditionally. A run where Pass B/C skipped — `--no-llm`,
    `[collect] llm_summaries = false`, a summarizer that could not be
    built, or an incremental refresh whose only change was a deletion —
    therefore left the *previous* run's report sitting next to an
    `artifact.json` that no longer carries any summary for it to describe:
    a stale artifact whose presence looks like a claim about the current
    build. Remove it in that case so the directory contents always describe
    exactly one build.
    """
    try:
        collect_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OSError(f"could not create {collect_dir}: {exc}") from exc
    written: List[str] = []

    def _write(path: Path, content: str) -> None:
        try:
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise OSError(f"could not write {path}: {exc}") from exc

    artifact_path = collect_dir / ARTIFACT_FILENAME
    _write(artifact_path, canonical_dumps(_artifact_dict(ctx), check_forbidden=False) + "\n")
    written.append(ARTIFACT_FILENAME)

    report_path = collect_dir / VERIFICATION_REPORT_FILENAME
    if ctx.verification_report is not None:
        _write(
            report_path,
            json.dumps(ctx.verification_report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        written.append(VERIFICATION_REPORT_FILENAME)
    elif report_path.exists():
        # A failed delete is a nuisance, not a build failure: the run's
        # own artifact is already written and correct, and one stale derived
        # report should not turn a cosmetic leftover into an aborted collect.
        try:
            report_path.unlink()
        except OSError as exc:
            logger.warning("could not remove stale %s: %s", report_path, exc)

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
        _write(collect_dir / name, content)
        written.append(name)

    return sorted(written)


def _write_manifest(
    root: Path,
    collect_dir: Path,
    modules: List[ModuleRecord],
    *,
    provenance: Optional[Tuple[Optional[str], bool]] = None,
    file_hashes: Optional[Dict[str, str]] = None,
) -> None:
    """`provenance`, if given, must be a `(git_sha, dirty)` pair captured
    via `manifest_mod.capture_provenance(root)` *before* `_write_artifact`
    ran — see that function's docstring for why the ordering matters.

    `file_hashes`, if given, is the `{path: sha256}` map the caller already
    computed for this same set of modules, reused instead of re-hashing the
    tree here (see `manifest.build_manifest`)."""
    files = sorted(m.path for m in modules)
    manifest = manifest_mod.build_manifest(
        root, files, provenance=provenance, file_hashes=file_hashes,
    )
    manifest_mod.write_manifest(manifest, collect_dir / MANIFEST_FILENAME)


# ── the five actions ─────────────────────────────────────────────────────────


def _read_previous_manifest(collect_dir: Path) -> Tuple[Optional[manifest_mod.Manifest], Optional[str]]:
    """The previous run's manifest, or why there isn't one.

    Returns `(manifest, None)` when it read, `(None, None)` when the file
    simply isn't there yet (collect has never run), and `(None, reason)`
    when it is there but can't be used — that last case is the one that
    used to be indistinguishable from "never run" to every caller, so a
    build that had to fall back to a full rebuild reported "no prior
    artifact to diff against" for a manifest that was sitting right there,
    unreadable.

    `Manifest.from_dict` converts every malformed shape it can hit (missing
    key, a non-dict JSON root) into `ValueError`, so `(OSError, ValueError)`
    is the complete guard — matching the four existing call sites.
    """
    path = collect_dir / MANIFEST_FILENAME
    if not path.exists():
        return None, None
    try:
        return manifest_mod.read_manifest(path), None
    except (OSError, ValueError) as exc:
        return None, f"{path} is unreadable ({exc})"


def _freshness(
    root: Path, config: Optional[configparser.ConfigParser],
) -> Tuple[CollectResult, Optional[List[ModuleRecord]], Optional[Dict[str, str]]]:
    """The one freshness verdict — what `--check` reports and what
    `--collect` gates on — plus the Pass A scan and `{path: sha256}` map it
    cost, so a stale `--collect` hands them to `action_refresh` instead of
    scanning and hashing the same tree a second time. Both are `None` when
    the verdict needed neither (no manifest, an unreadable one, a manifest
    whose artifact is gone): the refresh that follows scans for itself.

    One function for both callers on purpose: `action_collect` used to ask
    `action_check` for the verdict and then `action_refresh` re-derived the
    same scan, and a later fix that inlined the gate into `action_collect`
    would have left two copies of the rules to drift apart — which is how
    the artifact test below was missing from one of them.

    BUGFIX: the manifest records the tree's hashes; the artifact is the model
    the hashes point at. The loader (`_load_model`) and `action_refresh` both
    treat the pair as a unit, but the verdict read only the manifest — so a
    manifest whose `artifact.json` had been deleted (hand-cleaned
    `.collect/`, a build that died mid-write) reported "up to date" against
    an unchanged tree, and `action_collect`, trusting that, printed "already
    up to date — nothing to do" and wrote nothing: a state nothing else
    would ever repair, with the consumer handed no artifact at all. A
    manifest with no artifact beside it is not fresh; it is absent.
    """
    collect_dir = resolve_collect_dir(root, config)
    manifest_path = collect_dir / MANIFEST_FILENAME
    artifact_path = collect_dir / ARTIFACT_FILENAME

    def _verdict(fresh: bool, message: str) -> CollectResult:
        return CollectResult(action="check", wrote=False, fresh=fresh, message=message, collect_dir=collect_dir)

    existing, manifest_problem = _read_previous_manifest(collect_dir)
    if manifest_problem is not None:
        return _verdict(False, f"manifest {manifest_problem} — treat as stale"), None, None
    if existing is None:
        return _verdict(False, f"no manifest at {manifest_path} — collect has never run"), None, None
    if not artifact_path.exists():
        return _verdict(
            False,
            f"manifest present at {manifest_path} but the artifact at "
            f"{artifact_path} is missing — needs a full rebuild",
        ), None, None

    modules = scan_repo(root, config=config)
    hashes = manifest_mod.hash_tree(root, [m.path for m in modules])
    fresh = manifest_mod.is_fresh(existing, root, files=[m.path for m in modules], hashes=hashes)
    if fresh:
        reason = "up to date"
    elif existing.collector_version != manifest_mod.COLLECTOR_VERSION:
        # No file changed at all — the reason is the schema, and the old
        # blanket message claimed the opposite of what was true.
        reason = (
            f"stale — manifest was built by collector_version="
            f"{existing.collector_version!r}, current is {manifest_mod.COLLECTOR_VERSION!r}"
        )
    else:
        reason = "stale — a tracked file changed since the last collect run"
    return _verdict(fresh, reason), modules, hashes


def action_check(root: Path, *, config: Optional[configparser.ConfigParser] = None) -> CollectResult:
    """`--check`: freshness only. Never writes — not the manifest, not the
    artifact, nothing — regardless of what it finds. The verdict is
    `_freshness`'s, the same one `--collect` gates on."""
    return _freshness(Path(root), config)[0]


def _full_build(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser],
    config_path: Optional[str],
    llm_call: Optional[LlmCall],
    modules: Optional[List[ModuleRecord]] = None,
    hashes: Optional[Dict[str, str]] = None,
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
    # `modules`/`hashes`, if given, are the caller's own scan and hash of
    # this same tree (`action_refresh` has both before it decides to fall
    # back here), reused rather than re-run — otherwise a `--collect` that
    # falls back to a full build would still cost two Pass A scans.
    if modules is None:
        modules = scan_repo(root, config=config)
    if hashes is None:
        # Hashed once, here, and reused by `_write_manifest` below instead of
        # being recomputed after `_write_artifact` has written to disk.
        hashes = manifest_mod.hash_tree(root, [m.path for m in modules])
    ctx = build_context(root, modules, config=config, config_path=config_path, llm_call=llm_call)
    written = _write_artifact(collect_dir, ctx)
    _write_manifest(root, collect_dir, ctx.modules, provenance=provenance, file_hashes=hashes)
    written = sorted(set(written) | {MANIFEST_FILENAME})
    return collect_dir, written, ctx


def _full_build_message(why: str, ctx: CollectContext, written: List[str], collect_dir: Path) -> str:
    """The one line every full build reports, whichever path reached it
    (`--rebuild`, a first-ever run, a `collector_version` mismatch). It
    leads with `why` so the reader sees which path ran, and it counts what
    Pass B actually did, in both directions:

    - `--no-llm` / `[collect] llm_summaries = false` / a summarizer that
      could not be built never run Pass B (`build_context` leaves
      `verification_report` None), and then the honest verb is
      "re-scanned (Pass B skipped)", not "re-summarized";
    - a module with a parse error is never summarized, and a summarizer
      that exhausts its retries leaves `summary` empty too, so "N
      module(s) re-summarized" for a tree where only some were was a
      literal overclaim on the one number V7 made this line report. A
      partial batch names both halves; a batch that ran and produced
      nothing says so rather than claiming Pass B was skipped."""
    total = len(ctx.modules)
    summarized = sum(1 for m in ctx.modules if m.summary is not None)
    pass_b_ran = ctx.verification_report is not None
    if summarized == total:
        what = f"{total} module(s) re-summarized"
    elif not pass_b_ran:
        what = f"{total} module(s) re-scanned (Pass B skipped)"
    elif summarized:
        what = f"{total} module(s) re-scanned, {summarized} re-summarized"
    else:
        what = f"{total} module(s) re-scanned, none re-summarized (Pass B produced no summary)"
    return f"{why}{what}; wrote {len(written)} file(s) in {collect_dir}"


def action_rebuild(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectResult:
    """`--collect --rebuild` / `/collect --rebuild`: unconditional full
    rebuild.

    This is `_full_build`'s path as a first-class action rather than a
    fallback only: every module in the tree is re-scanned and re-summarized
    regardless of freshness, regardless of which files changed, and
    regardless of whether a prior artifact exists. That is what you want
    after a `collector_version` bump, when the artifact is suspect, or
    when a summarizer prompt changed and every module's `purpose` should be
    re-derived — previously the only way to get that was to delete
    `[collect] dir` (or point `--base` at an empty artifact dir) first.

    One Pass B call per module, which is exactly why this is a flag you
    opt into rather than what `--collect` does by default.
    """
    root = Path(root)
    collect_dir, written, ctx = _full_build(root, config=config, config_path=config_path, llm_call=llm_call)
    return CollectResult(
        action="rebuild", wrote=True, fresh=True,
        message=_full_build_message("", ctx, written, collect_dir),
        collect_dir=collect_dir, written_files=tuple(written),
    )


def action_refresh(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
    modules: Optional[List[ModuleRecord]] = None,
    hashes: Optional[Dict[str, str]] = None,
) -> CollectResult:
    """`--refresh`: diff-driven incremental rebuild (COLLECT-24).

    This is the function `action_collect` delegates a stale tree to, so
    `--collect` and `--refresh` share one implementation for the
    "something changed" case and differ only in that `collect` is
    freshness-gated (a fresh tree is a no-op) and never reaches here.

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

    `modules`, if given, is a `scan_repo` result the caller already has
    (`action_collect` needs one for its own freshness gate anyway): Pass A
    is run exactly once across the two instead of twice over the same tree,
    which is what the stale-tree path used to do — one scan to decide
    whether anything changed, a second to do something about it. `hashes`,
    if given, is the `hash_tree` result over that same file set, so the
    tree is hashed once too rather than once here and once in the caller's
    freshness gate.

    Pass C (`verifier.verify_repo`) still runs over the full merged
    module list on every call, because a citation check needs the
    *current* whole-repo symbol table to be correct — but Pass C is pure
    computation (no LLM call), so this costs nothing extra by the
    `--refresh`-costs-zero-LLM-calls-on-an-unchanged-tree measure
    (COLLECT-24 AC).

    Falls back to an unconditional full build (`_full_build`) when there is
    no existing manifest+artifact pair to diff against — nothing to be
    "incremental" relative to — and also when the existing manifest was
    written by a different `collector_version`. `action_rebuild` reaches
    the same path directly, without either condition.
    """
    root = Path(root)
    collect_dir = resolve_collect_dir(root, config)
    artifact_path = collect_dir / ARTIFACT_FILENAME

    previous_manifest, manifest_problem = _read_previous_manifest(collect_dir)
    previous_by_path: Dict[str, ModuleRecord] = {}
    artifact_problem: Optional[str] = None
    if previous_manifest is not None:
        try:
            if artifact_path.exists():
                payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                previous_by_path = {d["path"]: ModuleRecord.from_dict(d) for d in payload.get("modules", [])}
            else:
                artifact_problem = f"{artifact_path} is absent (the manifest has nothing to diff against)"
        # BUGFIX: ConfigRead/ExceptSite/GuardedAccess are frozen dataclasses
        # with required (no-default) fields, so a stored artifact.json missing
        # one of those keys (schema drift, hand-edited file, partial/corrupted
        # write) raises TypeError, not OSError/ValueError/KeyError. Matches
        # loader.py::_load_from_dir's existing (correct) guard so a corrupt
        # artifact degrades to "no previous manifest" instead of crashing.
        # AttributeError added: a valid-JSON-but-non-dict artifact (bare
        # list/number/string/null) makes payload.get(...) raise it.
        except (AttributeError, OSError, ValueError, KeyError, TypeError) as exc:
            artifact_problem = f"{artifact_path} is unreadable or malformed ({exc})"
        if artifact_problem is None and not previous_by_path:
            # A `modules: []` artifact alongside a manifest that lists files
            # is the "partial write" hazard the guard above already covers
            # for a corrupt record — the same thing that used to be absorbed
            # silently, here as an empty list instead of a missing key.
            artifact_problem = f"{artifact_path} carries no module records to reuse"
        if artifact_problem is not None:
            # A usable manifest with an unusable artifact is not "incremental
            # against nothing" — it is "nothing to diff against", so the
            # manifest stops counting too. This is the coupling the original
            # combined try/except had, preserved while keeping the reason.
            previous_manifest = None

    # BUGFIX: manifest.py's own docstring documents `collector_version`
    # as existing "so a format change can be detected" — but nothing
    # anywhere in this codebase ever actually COMPARED it. Every field in
    # ModuleRecord.from_dict() (and its nested records) degrades
    # gracefully via `.get(key, default)` rather than raising on a
    # missing key, which is the right behaviour for a genuinely corrupt
    # or hand-edited file — but it also means a SCHEMA CHANGE (a new field
    # added to ModuleRecord after `previous_manifest` was written) was
    # silently absorbed here: an unchanged file's reused `previous_by_path`
    # record would be missing/defaulted for the new field while a
    # genuinely-changed file's freshly-scanned record has it populated,
    # producing an internally inconsistent `merged` list with no warning
    # that it happened. Treating a version mismatch exactly like the
    # existing "no previous manifest" case — fall back to a full build —
    # closes it the same way that case is already closed, and is the
    # literal mechanism the docstring already promised existed.
    # BUGFIX: `manifest_problem`/`artifact_problem` keep the honest reason
    # here. Both fallbacks used to read "no prior artifact to diff against",
    # which is false when a manifest is sitting right there unreadable, or
    # when the artifact exists but is corrupt.
    why = "no prior artifact to diff against — full build: "
    if manifest_problem is not None:
        why = f"{manifest_problem} — full build: "
    elif artifact_problem is not None:
        why = f"{artifact_problem} — full build: "
    if previous_manifest is not None and previous_manifest.collector_version != manifest_mod.COLLECTOR_VERSION:
        why = (
            f"manifest was built by collector_version={previous_manifest.collector_version!r}, "
            f"current is {manifest_mod.COLLECTOR_VERSION!r} — full build: "
        )
        logger.info(
            "collect --refresh: manifest was built by collector_version=%r, "
            "current is %r — falling back to a full build instead of "
            "reusing possibly schema-mismatched records",
            previous_manifest.collector_version, manifest_mod.COLLECTOR_VERSION,
        )
        previous_manifest = None
        previous_by_path = {}

    # Scanned and hashed once, before either branch: the incremental path
    # diffs this against the previous manifest, and the full-build fallback
    # below needs the same scan — so it is shared rather than the fallback
    # re-running Pass A and the hash pass for the same tree.
    current_modules = modules if modules is not None else scan_repo(root, config=config)
    current_hashes = hashes if hashes is not None else manifest_mod.hash_tree(
        root, [m.path for m in current_modules],
    )

    if previous_manifest is None:
        collect_dir, written, ctx = _full_build(
            root, config=config, config_path=config_path, llm_call=llm_call,
            modules=current_modules, hashes=current_hashes,
        )
        return CollectResult(
            action="refresh", wrote=True, fresh=True,
            message=_full_build_message(why, ctx, written, collect_dir),
            collect_dir=collect_dir, written_files=tuple(written),
        )

    # Same ordering requirement as `_full_build`: capture provenance now,
    # before `_write_artifact` below writes anything under `.collect/` —
    # and exclude the collect dir by path, since the *previous* run's
    # output is already untracked before this run writes anything.
    provenance = manifest_mod.capture_provenance(root, collect_dir=collect_dir)

    changes = manifest_mod.diff_files(previous_manifest.file_hashes, current_hashes)

    to_summarize = [m for m in current_modules if m.path in changes.changed]

    settings = read_collect_settings(config)
    # "Skipped" is only honest when there was something to summarize: a
    # deletion-only refresh has no changed module, so Pass B had no work,
    # not a reason to stay silent about.
    pass_b_skipped = bool(to_summarize) and (llm_call is None or not settings.llm_summaries)
    summarized_by_path: Dict[str, ModuleRecord] = {}
    if to_summarize and not pass_b_skipped:
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
        # BUGFIX: the loop above skips a module it cannot re-read, but
        # `to_summarize` still contained it — and `summarize_repo` falls
        # back to `sources.get(path, "")`, so that module was summarized
        # from an EMPTY source: Pass A facts plus no code at all. The reply
        # still parsed into a plausible-looking `purpose`, which then
        # landed in artifact.json as grounded prose about a file the
        # summarizer never read. Summarize only the modules that actually
        # have a source this round; the rest stay structural-only for this
        # run and get picked up next time.
        batch = [m for m in to_summarize if m.path in sources_for_summary]
        summarized_by_path = {
            m.path: m for m in summarize_repo(
                batch, sources_for_summary, llm_call,
                max_retries=collect_max_retries(config),
                progress_fn=_print_summarize_progress,
                on_error=_print_summarize_error,
            )
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
        verified_modules, report = verifier_mod.verify_repo(
            merged, sources, root=root, import_edges=ctx.import_edges,
        )
        ctx = replace(ctx, modules=verified_modules, verification_report=report)

    written = _write_artifact(collect_dir, ctx)
    # Reuse the hashes already computed for the diff above: the manifest
    # tracks exactly this same set of paths, so a second tree-wide hash
    # pass here (after `_write_artifact` has written to disk) was pure
    # duplication of the one done at the top of this function.
    _write_manifest(root, collect_dir, ctx.modules, provenance=provenance, file_hashes=current_hashes)
    written = sorted(set(written) | {MANIFEST_FILENAME})

    if changes.is_empty():
        message = f"tree unchanged — recomputed derived artifacts only, wrote {len(written)} file(s) in {collect_dir}"
    else:
        # Zero counts are noise in both directions: "and 0 removed" on the
        # common path (one edited file) and a leading "0 changed" when the
        # only change was a deletion — the count that's zero says nothing,
        # the one that isn't is the whole message.
        scope = f"{len(changes.changed)} changed" if changes.changed else f"{len(changes.removed)} removed"
        if changes.changed and changes.removed:
            scope += f" and {len(changes.removed)} removed"
        # V7's "the line says which path ran" applies to Pass B too:
        # `--no-llm` / `[collect] llm_summaries = false` / a summarizer that
        # could not be built all make this path run with zero LLM calls, so
        # the honest verb is "re-scanned", never "refreshed" in a way that
        # implies a summary was re-derived.
        suffix = " (Pass B skipped)" if pass_b_skipped else ""
        message = f"incrementally refreshed {scope} module(s){suffix}; wrote {len(written)} file(s) in {collect_dir}"

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
    """`--collect` / `/collect`: one-shot, freshness-gated.

    A fresh tree is a no-op — no write of any kind, same as `check` would
    report. A stale tree delegates to `action_refresh`, so only the modules
    whose content hash changed since the last manifest are re-summarized and
    every unchanged module keeps its previous record (summary included)
    verbatim. The previous behaviour was to call `_full_build` here, which
    re-ran Pass B over *every* module in the tree for every single changed
    file — one changed file cost the same as a from-scratch build.

    The delegated result's own message is passed through unchanged, so the
    printed line always says which path actually ran ("incrementally
    refreshed N changed module(s)" vs "no prior artifact ... full build")
    while `action` stays "collect" — the flag the user typed. The
    unconditional full rebuild lives in `action_rebuild` (`--rebuild`); a
    `collector_version` mismatch still forces one, because `action_refresh`
    drops a version-mismatched previous manifest and falls back to
    `_full_build`.

    Cost: one Pass A scan and one hash pass. `_freshness` — the same verdict
    `action_check` reports — returns the scan and the hash map it computed,
    and they are handed to `action_refresh` (`modules=`/`hashes=`), which
    passes the map on to `_write_manifest`. This used to scan the tree twice
    and hash it three times (freshness gate, the diff, then again inside
    `manifest.build_manifest`); Pass B was already down to one call per
    changed module, so Pass A and the hash passes were the next largest
    things on the receipt.
    """
    verdict, modules, hashes = _freshness(Path(root), config)
    if verdict.fresh:
        return CollectResult(
            action="collect", wrote=False, fresh=True,
            message="already up to date — nothing to do",
            collect_dir=verdict.collect_dir,
        )
    result = action_refresh(
        root, config=config, config_path=config_path, llm_call=llm_call,
        modules=modules, hashes=hashes,
    )
    return CollectResult(
        action="collect", wrote=result.wrote, fresh=result.fresh,
        message=result.message,
        collect_dir=result.collect_dir, written_files=result.written_files,
    )


def _normalize_module_path(
    root: Path, module_path: str, config: Optional[configparser.ConfigParser]
) -> str:
    """Turn a `--module <path>` argument into the one form the rest of
    `action_module` (and every other part of the collector) speaks.

    BUGFIX: the raw argument used to be threaded through unchanged as both
    the module's `path` and its manifest key, so `--module ./pkg/a.py`
    produced an artifact with *two* records for the same file (`pkg/a.py`,
    reused from the previous artifact, and `./pkg/a.py`, freshly scanned)
    and a manifest carrying the key `./pkg/a.py`. The manifest's keys are
    exactly what `is_fresh` compares against the scanner's own path list,
    and the scanner never yields a `./`-prefixed path — so that phantom
    key stayed in the manifest forever, `is_fresh` returned False forever,
    and `--collect` could never take V7's no-op path again: one mistyped
    `--module` made the incremental fast path permanently unreachable.
    Normalizing here removes the duplicate and keeps the manifest's keys in
    the same shape `scan_repo` produces.

    The same rule applies to any path whose extension no language in this
    collector recognizes at all — a `README.md`, a config file, anything
    without an extension: `scan_repo` would never record it, so `is_fresh`
    could never see it either. `language_for` is the scanner's own test
    (recognition, not `[collect] languages` enablement — `--module
    Foo.java` on a Python-only repo stays the documented COLLECT-28 escape
    hatch), so there is one place that decides what counts as a source
    file.
    """
    raw = (module_path or "").strip()
    if not raw:
        raise CollectCliError("--module requires a path argument")
    if Path(raw).is_absolute():
        raise CollectCliError(
            f"--module path must be relative to {root}, got an absolute path: {raw!r}"
        )
    normalized = os.path.normpath(raw)
    if Path(normalized).parts[:1] == ("..",):
        raise CollectCliError(
            f"--module path escapes the project root {root}: {raw!r}"
        )
    rel = normalized.replace(os.sep, "/")
    if language_for(rel, config) is None:
        raise CollectCliError(
            f"--module path {raw!r} (normalized: {rel!r}) has an extension "
            f"this collector does not recognize — refusing to patch it into "
            f"the artifact, where it would also mark the tree stale forever"
        )
    return rel


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
    module_path = _normalize_module_path(root, module_path, config)
    collect_dir = resolve_collect_dir(root, config)
    artifact_path = collect_dir / ARTIFACT_FILENAME
    manifest_path = collect_dir / MANIFEST_FILENAME
    if not artifact_path.exists():
        result = action_refresh(root, config=config, config_path=config_path, llm_call=llm_call)
        return CollectResult(
            action="module", wrote=result.wrote, fresh=result.fresh,
            message=f"no existing artifact — ran a full refresh instead ({result.message})",
            collect_dir=result.collect_dir, written_files=result.written_files,
        )

    # BUGFIX: same collector_version gap fixed for manifest.is_fresh and
    # action_refresh — this function reuses every OTHER module's record
    # from the existing artifact verbatim and never calls is_fresh at all,
    # so it needs its own check. Without it, patching one module under an
    # artifact built by an older collector_version would silently merge
    # the freshly-scanned module (current schema) with every reused record
    # (old schema) into one inconsistent artifact.
    if manifest_path.exists():
        try:
            existing_manifest = manifest_mod.read_manifest(manifest_path)
        except (OSError, ValueError):
            existing_manifest = None
        if existing_manifest is not None and existing_manifest.collector_version != manifest_mod.COLLECTOR_VERSION:
            logger.info(
                "collect --module: existing artifact was built by "
                "collector_version=%r, current is %r — running a full "
                "refresh instead of patching one module into a "
                "possibly schema-mismatched artifact",
                existing_manifest.collector_version, manifest_mod.COLLECTOR_VERSION,
            )
            result = action_refresh(root, config=config, config_path=config_path, llm_call=llm_call)
            return CollectResult(
                action="module", wrote=result.wrote, fresh=result.fresh,
                message=f"collector_version mismatch — ran a full refresh instead ({result.message})",
                collect_dir=result.collect_dir, written_files=result.written_files,
            )

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectCliError(f"existing artifact at {artifact_path} is unreadable: {exc}") from exc

    # BUGFIX: ConfigRead/ExceptSite/GuardedAccess are frozen dataclasses with
    # required (no-default) fields, so a stored artifact.json missing one of
    # those keys (schema drift, hand-edited file, partial/corrupted write)
    # raised TypeError/KeyError straight out of this list comprehension —
    # previously completely unguarded, unlike every other from_dict() call
    # site in this file (action_refresh above, loader.py::_load_from_dir).
    # `--module`'s whole point is patching into an *existing* artifact (see
    # the `if not artifact_path.exists()` full-refresh fallback above), so
    # there's no sensible silent-degrade here — raise the same clean
    # CollectCliError the JSON-read guard just above already uses for other
    # forms of a broken artifact.
    try:
        modules = [ModuleRecord.from_dict(d) for d in payload.get("modules", [])]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # Bugfix: AttributeError added — a valid-JSON-but-non-dict payload
        # makes payload.get(...) raise it, and that fell through uncaught.
        raise CollectCliError(f"existing artifact at {artifact_path} is malformed: {exc}") from exc
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

    # BUGFIX: `action_module`'s whole point is to be incremental — patch
    # ONE file, reuse every other module's record verbatim.  But the
    # previous `build_context(..., llm_call=llm_call)` call below handed
    # the entire `modules` list to `build_context`, which forwarded it to
    # `summarize_repo`.  `summarize_repo` has no guard for a module that
    # already carries a `summary` (it only skips parse-error modules and
    # checkpoint hits), so it called the LLM once per module in the whole
    # codebase — O(N) LLM calls for what should be exactly 1.
    #
    # Fix: mirror `action_refresh`'s own incremental pattern.  Run Pass B
    # (the LLM summarizer) only on `patched` — the one freshly-scanned
    # module (summary=None) — using the `source` already in scope, then
    # splice the result back into `modules`.  Pass `llm_call=None` to
    # `build_context` so it never calls `summarize_repo` again.  Pass C
    # (verifier) still runs inside `build_context` the same way it does in
    # `action_refresh` (via the `any(m.summary is not None …)` branch
    # there), so verification of the newly-updated summary is not skipped.
    settings = read_collect_settings(config)
    if llm_call is not None and settings.llm_summaries and patched.parse_error is None:
        summarized = summarize_repo(
            [patched], {module_path: source}, llm_call,
            max_retries=collect_max_retries(config),
            progress_fn=_print_summarize_progress,
            on_error=_print_summarize_error,
        )
        if summarized and summarized[0].summary is not None:
            patched = summarized[0]
            modules = [patched if m.path == module_path else m for m in modules]

    # Captured before `_write_artifact` below writes anything under
    # `.collect/` — see `capture_provenance`'s docstring. Same ordering
    # bug as `_full_build` otherwise: `.collect/` isn't git-ignored, so
    # computing this after the write would see the write's own untracked
    # output and report `dirty=True` regardless of the tracked tree.
    # `collect_dir` is passed so prior runs' output is excluded by path.
    git_sha, dirty = manifest_mod.capture_provenance(root, collect_dir=collect_dir)

    ctx = build_context(root, modules, config=config, config_path=config_path, llm_call=None)

    # BUGFIX: `build_context` only runs Pass C (`verifier.verify_repo`) in
    # the branch guarded by `if llm_call is not None`, and this call passes
    # `llm_call=None` (Pass B was already run above, directly, against just
    # `patched`) — so without this block, `--module` never verified
    # anything: a fabricated citation in the freshly-summarized module's
    # `purpose`/`notes` (or a stale fabrication in any reused module) would
    # survive straight into artifact.json, silently. `action_refresh` avoids
    # exactly this by re-running `verify_repo` itself right after its own
    # `build_context(..., llm_call=None)` call, gated on
    # `any(m.summary is not None for m in modules)` — mirrored here so
    # `--module` gets the same guarantee.
    if any(m.summary is not None for m in modules):
        sources = _sources_for(root, modules)
        verified_modules, report = verifier_mod.verify_repo(
            modules, sources, root=root, import_edges=ctx.import_edges,
        )
        modules = verified_modules
        ctx = replace(ctx, modules=verified_modules, verification_report=report)

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

    if hashes:
        # A usable previous manifest: patch just this one entry. `hash_tree`
        # (rather than the unguarded `hash_file` this used to call) so a file
        # that vanished or went unreadable in the window since it was read
        # above is dropped from the map instead of raising — `is_fresh` treats
        # a missing key as "stale", which is the safe direction.
        fresh_hashes = manifest_mod.hash_tree(root, [module_path])
        if module_path in fresh_hashes:
            hashes[module_path] = fresh_hashes[module_path]
        else:
            hashes.pop(module_path, None)
    else:
        # BUGFIX: nothing usable to patch into (no previous manifest, or one
        # that would not parse). Writing a manifest that tracked ONLY this one
        # file made the next `--collect`/`--refresh` diff an N-file tree
        # against a 1-entry map — every other module looked "added" and got
        # re-summarized by the LLM. That is V7's exact cost bug (one changed
        # file == one call per module in the tree), reached through the
        # incremental `--module` path instead of `--collect`. Record the whole
        # merged set so the manifest stays whole and the next run is
        # incremental for real.
        hashes = manifest_mod.hash_tree(root, sorted(m.path for m in modules))

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
    """Single dispatch point for all five actions — what `main.py`'s
    `/collect` command / `--collect`/`--check`/`--refresh`/`--rebuild`/
    `--module` flags call."""
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
    if action == "rebuild":
        return action_rebuild(root, config=config, config_path=config_path, llm_call=llm_call)
    if action == "module":
        if not module_path:
            raise CollectCliError("action='module' requires module_path")
        return action_module(root, module_path, config=config, config_path=config_path, llm_call=llm_call)
    return action_collect(root, config=config, config_path=config_path, llm_call=llm_call)


# ── argparse-level entry point (mirrors --auto / --faq in main.py) ─────────


def action_from_flags(
    *,
    check: bool = False,
    module_path: Optional[str] = None,
    rebuild: bool = False,
    refresh: bool = False,
) -> Tuple[str, Optional[str]]:
    """The one precedence table that maps a set of collect flags to an
    action, so both entry points agree: `parse_collect_args` (the `/collect`
    command) and `main.py`'s one-shot `--collect` if-chain.

    Before this existed each entry point had its own ordering and they
    disagreed on `--module` + `--rebuild` — `/collect --module pkg/a.py
    --rebuild` ran a full rebuild while `--collect --module pkg/a.py
    --rebuild` ran the single-file patch — and on `--check` + `--module`,
    where the interactive path honoured `--check` and the one-shot path
    ignored it and *wrote* anyway, breaking `--check`'s "writes nothing,
    anywhere" promise on one of the two entry points.

    Precedence, most to least specific:
      check   — read-only; a freshness report must never silently become
                a write, whatever else was also asked for.
      module  — the most specific write request: one named file.
      rebuild — the explicit whole-tree request; must beat `refresh`, or
                asking for both silently downgrades to the cheap path.
      refresh — the cheapest write request; the fallback.
    None of them → `collect`, the freshness-gated one-shot default.
    """
    if check:
        return "check", None
    if module_path:
        return "module", module_path
    if rebuild:
        return "rebuild", None
    if refresh:
        return "refresh", None
    return "collect", None


def parse_collect_args(argv: List[str]) -> Dict[str, Any]:
    """Parse the collect-specific slice of argv into `run()` kwargs. Kept
    separate from stdlib `argparse` so `main.py` can add `--collect`,
    `--check`, `--refresh`, `--rebuild`, and `--module` to its existing
    parser and just forward here — see that module's own `_parse_args` for
    the actual flag definitions. Flag precedence is `action_from_flags`,
    shared with `main.py` so the two entry points dispatch identically."""
    module_path = None
    if "--module" in argv or any(a.startswith("--module=") for a in argv):
        # BUGFIX: only the space form was recognised. argparse (main.py's
        # parser) accepts both `--module <path>` and `--module=<path>`, so
        # `main.py --collect --module=pkg/a.py` patched one module while
        # `/collect --module=pkg/a.py` silently ran a full-tree collect —
        # the same "the two entry points disagree" defect, one flag later.
        module_path = _module_path_from(argv)

    action, _ = action_from_flags(
        check="--check" in argv,
        module_path=module_path,
        rebuild="--rebuild" in argv,
        refresh="--refresh" in argv,
    )
    return {"action": action, "module_path": module_path}


def _module_path_from(argv: List[str]) -> str:
    """The path `--module` names, from either `--module <path>` or
    `--module=<path>`. Raises `CollectCliError` instead of returning
    something unusable: with no value at all, with an empty value, or with
    a value that is really another flag (which would otherwise be handed to
    `action_module` as a path and reported as "does not exist"). argparse
    rejects `--module --no-llm` with "expected one argument" — this mirrors
    that rather than guessing at what was meant."""
    equals_values = [a.split("=", 1)[1] for a in argv if a.startswith("--module=")]
    if equals_values:
        value = equals_values[-1]
    else:
        idx = argv.index("--module")
        if idx + 1 >= len(argv):
            raise CollectCliError("--module requires a path argument")
        value = argv[idx + 1]
    if not value:
        raise CollectCliError("--module requires a path argument")
    if value.startswith("-"):
        raise CollectCliError(
            f"--module got {value!r}, which looks like a flag rather than a path — "
            "pass the path after the flag: --module <path>"
        )
    return value


def main(argv: List[str], root: Optional[str] = None, config_path: str = "agents.ini") -> int:
    """`python main.py --collect ...` entry point. Prints a one-line
    summary and returns a process exit code: 0 on success, 1 on any
    `CollectCliError`/citation failure so the caller can `sys.exit` it."""
    base = Path(root or ".").resolve()
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    if Path(config_path).exists():
        # AUTO-FIX: this sat OUTSIDE the try/except below, so a non-UTF-8
        # agents.ini crashed the whole CLI entry point with an uncaught
        # UnicodeDecodeError before it could report errors cleanly.
        try:
            config.read(config_path, encoding="utf-8")
        except UnicodeDecodeError as exc:
            print(f"collect: {config_path} is not valid UTF-8 — {exc}")
            return 1
        except configparser.Error as exc:
            print(f"collect: {config_path} is not a valid .ini file — {exc}")
            return 1

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
