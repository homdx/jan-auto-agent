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
``refresh`` — unconditional full rebuild (`--refresh`), ignoring current
              freshness.
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
from dataclasses import dataclass, field
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
from tools.collect.scanner import scan_module, scan_repo
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


# ── config plumbing ─────────────────────────────────────────────────────────


def resolve_collect_dir(root: Path, config: Optional[configparser.ConfigParser]) -> Path:
    """`[collect] dir` (default `.collect`), resolved relative to `root` if
    given as a relative path. This is the *only* function in this module
    that decides where writes may land — every write path in this module
    is built from its return value."""
    dir_value = DEFAULT_COLLECT_DIR
    if config is not None:
        dir_value = config.get("collect", "dir", fallback=DEFAULT_COLLECT_DIR).strip() or DEFAULT_COLLECT_DIR
    path = Path(dir_value)
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
        except OSError:
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
    contracts = registries_mod.build_seed_contracts(modules)
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


def _write_manifest(root: Path, collect_dir: Path, modules: List[ModuleRecord]) -> None:
    files = sorted(m.path for m in modules)
    manifest = manifest_mod.build_manifest(root, files)
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
    fresh = manifest_mod.is_fresh(existing, root)
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
    modules = scan_repo(root, config=config)
    ctx = build_context(root, modules, config=config, config_path=config_path, llm_call=llm_call)
    collect_dir = resolve_collect_dir(root, config)
    written = _write_artifact(collect_dir, ctx)
    _write_manifest(root, collect_dir, ctx.modules)
    written = sorted(set(written) | {MANIFEST_FILENAME})
    return collect_dir, written, ctx


def action_refresh(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
) -> CollectResult:
    """`--refresh`: unconditional full rebuild, regardless of freshness."""
    collect_dir, written, _ctx = _full_build(root, config=config, config_path=config_path, llm_call=llm_call)
    return CollectResult(
        action="refresh", wrote=True, fresh=True,
        message=f"rebuilt {len(written)} file(s) in {collect_dir}",
        collect_dir=collect_dir, written_files=tuple(written),
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
    source = abs_module.read_text(encoding="utf-8")
    patched = scan_module(source, module_path)

    if module_path not in by_path:
        modules.append(patched)
    else:
        modules = [patched if m.path == module_path else m for m in modules]
    modules.sort(key=lambda m: m.path)

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
        git_sha=manifest_mod.get_git_sha(root),
        dirty=manifest_mod.is_dirty(root),
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
