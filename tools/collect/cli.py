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
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
from tools.collect.model import LLMSummary, ModuleRecord, Provenance
from tools.collect.scanner import language_for, scan_file, scan_repo, scan_repo_ex
from tools.collect.summarizer import LlmCall, collect_max_retries, summarize_repo


def _modules(n: int) -> str:
    """`1 module` / `N modules`."""
    return f"{n} module" + ("" if n == 1 else "s")


def _print_summarize_progress(done: int, total: int, module_path: str, succeeded: bool = True,
                              last_error: str = "") -> None:
    """Simple `[done/total]` progress line printed to stdout after each
    module's LLM call finishes (COLLECT Pass B). When `succeeded` is
    False the module ran out of retries and got no summary — say so
    rather than mislabeling it `summarized`."""
    if succeeded:
        print(f"[{done}/{total}] summarized {module_path}", flush=True)
    else:
        print(f"[{done}/{total}] no summary {module_path}" + (f" ({last_error})" if last_error else ""), flush=True)


def _print_summarize_error(module_path: str, exc: Exception, error_count: int) -> None:
    """Printed once per *outer* retry (a module whose LLM call raised),
    on top of the per-HTTP-request retries `request_completion` already
    logs itself — so a module that's about to be retried shows up in the
    same progress stream instead of just going quiet for a while."""
    print(f"  ! {module_path}: {exc} (retry {error_count})", flush=True)


def _print_summarize_resume(kept: int, dropped: int) -> None:
    """RUN-11: one line for a Pass B batch that resumed from the checkpoint
    an interrupted run left behind. `kept` modules attached without an LLM
    call, `dropped` re-summarised because their source moved (the entry's
    sha no longer matched). Printed once per run — `summarize_repo` calls
    the callback before the loop, not per module — so the operator sees the
    recovery before the `[k/N]` stream, where `N` is what is still owed."""
    line = (
        f"[collect] Pass B resumes: {kept} module(s) kept from the interrupted run, "
        f"{dropped} re-summarised (source changed)"
    )
    print(line, flush=True)
    logger.info("%s", line)

DEFAULT_COLLECT_DIR = ".collect"
ARTIFACT_FILENAME = "artifact.json"
MANIFEST_FILENAME = "collect_manifest.json"
VERIFICATION_REPORT_FILENAME = "verification_report.json"
# Pass B checkpoint file name: every landed summary, cleared when the batch completes; it does not make a build dirty (RUN-11).
SUMMARIZE_CHECKPOINT_FILENAME = "collect_summarize_state.json"

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
    # pass_b_ran is the honest flag for whether summaries were produced; the counters say what a no-LLM build kept (V8).
    pass_b_ran: bool = False
    summaries_carried: int = 0
    summaries_stale: int = 0
    summaries_dropped: int = 0


def _previous_summaries(collect_dir: Path) -> Tuple[Dict[str, LLMSummary], Dict[str, str]]:
    """V8: every summary the previous artifact holds, by module path, and
    the previous manifest's file hashes. Read-only, fail-open: an absent,
    unreadable or malformed artifact/manifest yields empty maps — "nothing
    to carry forward" is the same outcome as a first-ever build, never an
    error out of a build that does not need the previous one."""
    summaries: Dict[str, LLMSummary] = {}
    hashes: Dict[str, str] = {}
    artifact_path = collect_dir / ARTIFACT_FILENAME
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        for d in payload.get("modules", []):
            rec = ModuleRecord.from_dict(d)
            if rec.summary is not None:
                summaries[rec.path] = rec.summary
    except (AttributeError, OSError, ValueError, KeyError, TypeError):
        return {}, {}
    previous_manifest, _problem = _read_previous_manifest(collect_dir)
    if previous_manifest is not None:
        hashes = dict(previous_manifest.file_hashes)
    return summaries, hashes


def _carry_summaries(
    modules: List[ModuleRecord],
    previous: Dict[str, LLMSummary],
    previous_hashes: Dict[str, str],
    current_hashes: Dict[str, str],
) -> Tuple[List[ModuleRecord], int, int]:
    """V8: attach the previous artifact's summary to every module of
    `modules` that has none. A module whose content hash differs from the
    one the previous manifest recorded (or that the manifest did not
    record at all) gets the prose tagged `llm-stale`; an unchanged module
    keeps it as it was. Returns `(modules, carried, stale)`."""
    out: List[ModuleRecord] = []
    carried = stale = 0
    for m in modules:
        prev = previous.get(m.path)
        if m.summary is not None or prev is None:
            out.append(m)
            continue
        old_hash = previous_hashes.get(m.path)
        if old_hash is None or old_hash != current_hashes.get(m.path):
            prev = prev.as_stale()
        if prev.provenance == Provenance.LLM_STALE:
            stale += 1
        carried += 1
        out.append(m.with_llm_summary(prev))
    return out, carried, stale


def _pass_b_suffix(ctx: "CollectContext") -> str:
    """The parenthesised tail of a build message when Pass B was skipped:
    what a `--no-llm` build did with the summaries it did not re-derive."""
    if ctx.summaries_dropped:
        return f"; {ctx.summaries_dropped} summaries dropped (--drop-summaries), no {VERIFICATION_REPORT_FILENAME}"
    if ctx.summaries_carried:
        return (
            f"; {ctx.summaries_carried} summaries carried forward, "
            f"{ctx.summaries_stale} marked llm-stale"
        )
    return ""


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
            sources[m.path] = (Path(root) / m.path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            # A file that becomes invalid UTF-8 between the scan and the re-read is skipped, not a crash (BUGFIX).
            continue
    return sources


def _primary_config_path(root: Path, config_path: str) -> Path:
    """The primary profile `.ini`, as one file. `--config` may name it cwd-relative
    (the default `agents.ini`) or be handed to `run()` root-relative by an in-process
    caller, and neither resolution may silently mean another file: `_sibling_gaps`
    diffs the primary against, and `_profile_paths` hashes, exactly this path.
    """
    primary = Path(config_path)
    if primary.is_absolute() or primary.exists():
        return primary
    return Path(root) / primary


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
    return config_map_mod.diff_sibling_profiles(_primary_config_path(root, config_path), siblings)


def _profile_paths(
    root: Path, config: Optional[configparser.ConfigParser], config_path: Optional[str]
) -> List[str]:
    """The `.ini` files `CONFIG_MAP.md` depends on, as repo-relative POSIX paths: the
    primary config file, plus every `[collect] sibling_profiles` entry — `_sibling_gaps`
    diffs exactly that set, so an edit to any of it changes the artifact.

    HARDEN-21 C: `file_hashes` held modules only, so after fixing a sibling profile
    `--check` and `--collect` answered "already up to date" and `CONFIG_MAP.md` kept the
    gap until some `.py` file was touched. These paths are hashed into the manifest's
    `profile_hashes` instead, so `is_fresh` sees them without touching the module diff.

    `[]` when there is no primary config file — the case for a tree without a profile —
    so such a tree hashes nothing extra and its manifest keeps its previous shape. Fail
    open: a missing or unreadable entry is simply absent from the result (`hash_tree`
    skips it), never an exception into a build.
    """
    if not config_path:
        return []
    # `CONFIG_MAP.md` diffs the primary against its siblings; a tree that names none has nothing
    # the profiles could change, so an edit of its `agents.ini` must not make the model stale.
    if config is None:
        return []
    try:
        if not config.get("collect", "sibling_profiles", fallback="").strip():
            return []
    except (AttributeError, TypeError, ValueError, configparser.Error):
        return []

    def _key(p: Path) -> str:
        # A `config_path` is cwd-relative, so it is resolved before it is made
        # root-relative: `os.path.relpath` would otherwise mean
        # `cwd / config_path`, and the manifest key would track the wrong file.
        resolved = p.resolve()
        try:
            return resolved.relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            # Outside `root`: keep the absolute path, so the key is still one file.
            return resolved.as_posix()

    out: List[str] = [_key(_primary_config_path(root, config_path))]
    if config is not None:
        try:
            raw = config.get("collect", "sibling_profiles", fallback="")
        except (AttributeError, TypeError, ValueError, configparser.Error):
            raw = ""
        if isinstance(raw, str):
            for name in raw.split(","):
                name = name.strip()
                if name and _key(Path(root) / name) not in out:
                    out.append(_key(Path(root) / name))
    return out


def _hash_modules_and_profiles(
    root: Path,
    modules: List[ModuleRecord],
    config: Optional[configparser.ConfigParser],
    config_path: Optional[str],
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """`({module: sha256}, {profile ini: sha256})` from ONE `hash_tree` pass.

    The modules and the `_profile_paths` of this run are hashed in the same
    call and split afterwards, so the tree is read once, not once for the
    modules and again for the `.ini` files — `--collect`, `--refresh` and
    `--rebuild` each pay for exactly one hash pass either way (the counts the
    stale-tree receipt pins). Splitting on membership rather than by two
    separate calls keeps the two maps disjoint, which is what lets
    `--refresh`'s module diff stay blind to a profile edit.

    A missing or unreadable file is simply absent from its map
    (`hash_tree`'s own rule), so a typo'd `sibling_profiles` entry costs
    nothing and never fails the build.
    """
    module_paths = [m.path for m in modules]
    profiles = _profile_paths(root, config, config_path)
    profile_set = set(profiles)
    hashed = manifest_mod.hash_tree(root, module_paths + profiles)
    return (
        {p: h for p, h in hashed.items() if p not in profile_set},
        {p: h for p, h in hashed.items() if p in profile_set},
    )


def build_context(
    root: Path,
    modules: List[ModuleRecord],
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
    checkpoint_path: Optional[Path] = None,
    hashes: Optional[Dict[str, str]] = None,
) -> CollectContext:
    """Run Pass B (only if `llm_call` is given) → Pass C → every EPIC C/D
    builder, over an already-scanned `modules` list. Kept separate from
    `scan_repo` so `--module`'s incremental path can call this over a
    patched module list without re-scanning the whole tree.

    RUN-11: `checkpoint_path` forwards to `summarize_repo`, so a full build
    whose Pass B dies at module *k* (Ctrl-C, `kill`, an OOM, a lost
    terminal) leaves the *k − 1* summaries the provider already returned
    in `<collect dir>/collect_summarize_state.json` instead of nowhere —
    `_write_artifact` is the only thing that touches `.collect/`, and it
    runs after Pass C and every builder. The same path is reused by
    `action_refresh`'s incremental batch, so the whole `.collect/`
    vocabulary for interrupted Pass B work is one file. `hashes` tags each
    saved entry with the source hash it was summarized from, so a resume
    re-derives a summary whose file moved. `None` (the default, and what
    `action_module` passes by not passing it) means no checkpoint at all —
    a one-module batch has nothing to resume, and `summarize_repo`'s
    `clear_state` at the end would otherwise wipe an interrupted full
    build's checkpoint.
    """
    sources = _sources_for(root, modules)

    # Built before Pass B and C, since Pass C needs the import graph and a summary changes no import (V11).
    edges = graph_mod.import_edges(modules)

    verification_report: Optional[Dict[str, Any]] = None
    if llm_call is not None:
        summarized = summarize_repo(
            modules, sources, llm_call,
            max_retries=collect_max_retries(config),
            progress_fn=_print_summarize_progress,
            on_error=_print_summarize_error,
            checkpoint_path=checkpoint_path,
            hashes=hashes,
            on_resume=_print_summarize_resume,
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
            from tools.auto.utils import atomic_write_text
            atomic_write_text(path, content)
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
        # A failed delete of a derived report is a nuisance, not a build failure (the artifact is already written).
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


def _log_skipped_files(skipped_files: Optional[Iterable[Tuple[str, str]]]) -> None:
    """One log line per file the collect walk skipped, with its reason (HARDEN-24 B).

    Before this a depth or a size limit dropped a file from the structural model with no
    word of it — the manifest never named it either. Each file is named once, here and on
    the manifest; nothing raises, so a walk that skipped a thousand files cannot make a
    run fail for the log.
    """
    for path, reason in (skipped_files or ()):
        logger.warning("collect: skipping %s (%s)", path, reason)


def _write_manifest(
    root: Path,
    collect_dir: Path,
    modules: List[ModuleRecord],
    *,
    provenance: Optional[Tuple[Optional[str], bool]] = None,
    file_hashes: Optional[Dict[str, str]] = None,
    config: Optional[configparser.ConfigParser] = None,
    unsummarized: Optional[Tuple[str, ...]] = None,
    profile_hashes: Optional[Dict[str, str]] = None,
    skipped_files: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> None:
    """`provenance`, if given, must be a `(git_sha, dirty)` pair captured
    via `manifest_mod.capture_provenance(root)` *before* `_write_artifact`
    ran — see that function's docstring for why the ordering matters.

    `file_hashes`, if given, is the `{path: sha256}` map the caller already
    computed for this same set of modules, reused instead of re-hashing the
    tree here (see `manifest.build_manifest`).
    `profile_hashes`, if given, is the caller's `{ini: sha256}` map over the
    same `_profile_paths` set — recorded verbatim, never recomputed here, so
    this write stays inside the caller's single hash pass (HARDEN-21 C).
    `None` records an empty map: the caller tracked no profile, which is what
    `is_fresh` compares it to.
    `unsummarized`, if given, is the sorted list of module paths whose Pass B
    produced no summary — the manifest records them so the next refresh can
    retry just those instead of treating the whole artifact as fresh.
    `skipped_files`, if given, is the `(path, reason)` pairs the walk left out
    on a depth or a size limit (HARDEN-24 B) — the manifest records them so a
    skipped file is named instead of missing with no word of it."""
    files = sorted(m.path for m in modules)
    manifest = manifest_mod.build_manifest(
        root, files, provenance=provenance, file_hashes=file_hashes, config=config,
        unsummarized=unsummarized, profile_hashes=profile_hashes,
        skipped_files=skipped_files,
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
    root: Path, config: Optional[configparser.ConfigParser], config_path: Optional[str] = None,
) -> Tuple[CollectResult, Optional[List[ModuleRecord]], Optional[Dict[str, str]], Dict[str, str],
           Tuple[Tuple[str, str], ...]]:
    """The one freshness verdict — what `--check` reports and what
    `--collect` gates on — plus the Pass A scan and the two `{path: sha256}`
    maps it cost (modules, profile `.ini` files), so a stale `--collect`
    hands them to `action_refresh` instead of scanning and hashing the same
    tree a second time. The first two are `None` when the verdict needed
    neither (no manifest, an unreadable one, a manifest whose artifact is
    gone): the refresh that follows scans for itself. `profile_hashes` is
    always a map — `{}` when there was nothing to hash. The last item is the
    `(path, reason)` pairs that scan skipped (HARDEN-24 B), handed on with the
    modules so the refresh records them — `()` when there was no scan.

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
        return _verdict(False, f"manifest {manifest_problem} — treat as stale"), None, None, {}, ()
    if existing is None:
        return _verdict(False, f"no manifest at {manifest_path} — collect has never run"), None, None, {}, ()
    if not artifact_path.exists():
        return _verdict(
            False,
            f"manifest present at {manifest_path} but the artifact at "
            f"{artifact_path} is missing — needs a full rebuild",
        ), None, None, {}, ()

    # CC review fixes 45: the scan names what it skipped, and the list goes to the refresh with
    # the modules. A plain scan_repo here dropped it, so the second --collect of a tree rewrote
    # the manifest with no skipped file and logged none (the first one had both).
    modules, skipped = scan_repo_ex(root, config=config)
    # One hash pass for both maps: the modules and the profile `.ini` files
    # CONFIG_MAP.md is built from, split afterwards. Hashing them separately
    # would be a second pass over the same tree, which the stale-tree receipt pins against.
    hashes, profile_hashes = _hash_modules_and_profiles(root, modules, config, config_path)
    fresh = manifest_mod.is_fresh(existing, root, files=[m.path for m in modules],
                                  hashes=hashes, skip_dirs=manifest_mod.skip_dirs_from_config(config),
                                  profile_hashes=profile_hashes)
    if fresh:
        reason = "up to date"
    elif existing.unsummarized:
        n = len(existing.unsummarized)
        reason = f"stale — {_modules(n)} without a summary"
    elif existing.collector_version != manifest_mod.COLLECTOR_VERSION:
        # No file changed, so the reason is the schema; the old message said the opposite.
        reason = (
            f"stale — manifest was built by collector_version="
            f"{existing.collector_version!r}, current is {manifest_mod.COLLECTOR_VERSION!r}"
        )
    elif (dict(existing.file_hashes) == dict(hashes)
          and dict(profile_hashes) != dict(existing.profile_hashes)):
        # HARDEN-21 C: no tracked module moved — only the `.ini` files CONFIG_MAP.md is built
        # from did, so the verdict says that instead of blaming a module. Comparing the manifest's
        # own recorded hashes with `hashes` costs nothing: both maps were already computed here.
        # The word "tracked file" is kept out of this sentence on purpose: the stale-message guard
        # pins that it appears only when a module really changed.
        reason = ("stale — a collect profile config changed, not a tracked module — "
                  "CONFIG_MAP.md needs a rebuild")
    else:
        reason = "stale — a tracked file changed since the last collect run"
    return _verdict(fresh, reason), modules, hashes, profile_hashes, tuple(skipped)


def action_check(root: Path, *, config: Optional[configparser.ConfigParser] = None,
                 config_path: Optional[str] = None) -> CollectResult:
    """`--check`: freshness only. Never writes — not the manifest, not the
    artifact, nothing — regardless of what it finds. The verdict is
    `_freshness`'s, the same one `--collect` gates on. `config_path` names
    the profile `.ini` files that verdict hashes (HARDEN-21 C)."""
    return _freshness(Path(root), config, config_path)[0]


def _full_build(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser],
    config_path: Optional[str],
    llm_call: Optional[LlmCall],
    modules: Optional[List[ModuleRecord]] = None,
    hashes: Optional[Dict[str, str]] = None,
    profile_hashes: Optional[Dict[str, str]] = None,
    drop_summaries: bool = False,
    skipped_files: Optional[Tuple[Tuple[str, str], ...]] = None,
) -> Tuple[Path, List[str], CollectContext]:
    # Provenance is captured before the output directory is written, so the build's own output does not make the tree dirty.
    collect_dir = resolve_collect_dir(root, config)
    provenance = manifest_mod.capture_provenance(root, collect_dir=collect_dir)
    # The caller's scan and hash of this tree are reused, so a fallback build does not scan twice.
    if modules is None:
        # HARDEN-24 B: the collect stage's own limits, and the files they left out — named here
        # and on the manifest instead of dropped silently.
        modules, skipped_files = scan_repo_ex(root, config=config)
        _log_skipped_files(skipped_files)
    if hashes is None:
        # Hashed once here — modules and profile ini files in the same pass — and reused by
        # _write_manifest, before _write_artifact writes to disk.
        hashes, profile_hashes = _hash_modules_and_profiles(root, modules, config, config_path)
    # A build without Pass B carries previous summaries forward, tagged stale where the file changed, instead of erasing them (V8).
    pass_b_ran = llm_call is not None
    carried = stale = dropped = 0
    if not pass_b_ran:
        previous, previous_hashes = _previous_summaries(collect_dir)
        if drop_summaries:
            dropped = sum(1 for m in modules if m.path in previous)
        elif previous:
            modules, carried, stale = _carry_summaries(modules, previous, previous_hashes, hashes)
    ctx = build_context(
        root, modules, config=config, config_path=config_path, llm_call=llm_call,
        # Pass B checkpoints each landed summary, so an interrupt costs only the module in flight (RUN-11).
        checkpoint_path=collect_dir / SUMMARIZE_CHECKPOINT_FILENAME, hashes=hashes,
    )
    if carried and any(m.summary is not None for m in ctx.modules):
        sources = _sources_for(root, ctx.modules)
        verified_modules, report = verifier_mod.verify_repo(
            ctx.modules, sources, root=root, import_edges=ctx.import_edges,
        )
        ctx = replace(ctx, modules=verified_modules, verification_report=report)
    ctx = replace(
        ctx, pass_b_ran=pass_b_ran,
        summaries_carried=carried, summaries_stale=stale, summaries_dropped=dropped,
    )
    written = _write_artifact(collect_dir, ctx)
    # Compute the set of modules Pass B left without a summary so the manifest
    # can list them and the next refresh retries just those (CC-15).
    # A --no-llm build never attempted Pass B, so unsummarized stays empty.
    unsummarized = tuple(sorted(
        m.path for m in ctx.modules
        if m.summary is None and m.parse_error is None and pass_b_ran
    ))
    _write_manifest(root, collect_dir, ctx.modules, provenance=provenance, file_hashes=hashes,
                    config=config, unsummarized=unsummarized, profile_hashes=profile_hashes,
                    skipped_files=skipped_files)
    written = sorted(set(written) | {MANIFEST_FILENAME})
    return collect_dir, written, ctx, unsummarized


def _full_build_message(why: str, ctx: CollectContext, written: List[str], collect_dir: Path, unsummarized: Tuple[str, ...] = ()) -> str:
    """The one line every full build reports, whichever path reached it
    (`--rebuild`, a first-ever run, a `collector_version` mismatch). It
    leads with `why` so the reader sees which path ran, and it counts what
    Pass B actually did, in both directions:

    - `--no-llm` / `[collect] llm_summaries = false` / a summarizer that
      could not be built never ran Pass B (`build_context` leaves
      `verification_report` None), and then the honest verb is
      "re-scanned (Pass B skipped)", not "re-summarized";
    - a module with a parse error is never summarized, and a summarizer
      that exhausts its retries leaves `summary` empty too, so "N
      module(s) re-summarized" for a tree where only some were was a
      literal overclaim on the one number V7 made this line report. A
      partial batch names both halves; a batch that ran and produced
      nothing says so rather than claiming Pass B was skipped.
    - CC-15: when some modules ran out of retries and are listed in
      `unsummarized`, they are named so the operator knows which ones
      will be retried at the next --collect instead of staying silently
      without a summary."""
    total = len(ctx.modules)
    summarized = sum(1 for m in ctx.modules if m.summary is not None)
    n_unsummarized = len(unsummarized)
    pass_b_ran = ctx.pass_b_ran
    if not pass_b_ran:
        what = f"{total} module(s) re-scanned (Pass B skipped{_pass_b_suffix(ctx)})"
    elif summarized == total:
        what = f"{total} module(s) re-summarized"
    elif summarized:
        what = f"{total} module(s) re-scanned, {summarized} re-summarized"
    else:
        what = f"{total} module(s) re-scanned, none re-summarized (Pass B produced no summary)"
    if n_unsummarized:
        what += f", {n_unsummarized} without a summary (tried again at the next --collect)"
    return f"{why}{what}; wrote {len(written)} file(s) in {collect_dir}"


def action_rebuild(
    root: Path,
    *,
    config: Optional[configparser.ConfigParser] = None,
    config_path: Optional[str] = None,
    llm_call: Optional[LlmCall] = None,
    drop_summaries: bool = False,
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

    With `--no-llm` (V8) the previous artifact's summaries are carried
    forward — `llm-stale` where the file changed — unless
    `drop_summaries` asks for the structural-only artifact explicitly.
    """
    root = Path(root)
    collect_dir, written, ctx, unsummarized = _full_build(
        root, config=config, config_path=config_path, llm_call=llm_call, drop_summaries=drop_summaries,
    )
    return CollectResult(
        action="rebuild", wrote=True, fresh=True,
        message=_full_build_message("", ctx, written, collect_dir, unsummarized=unsummarized),
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
    profile_hashes: Optional[Dict[str, str]] = None,
    drop_summaries: bool = False,
    skipped_files: Optional[Iterable[Tuple[str, str]]] = None,
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
    `skipped_files` goes with `modules`: the `(path, reason)` pairs that
    same scan skipped, recorded on the manifest and logged once here (HARDEN-24
    B). Left out with `modules` given, the manifest records none.

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
        # A stored artifact with the wrong shape is treated as no previous manifest, not a crash (BUGFIX).
        except (AttributeError, OSError, ValueError, KeyError, TypeError) as exc:
            artifact_problem = f"{artifact_path} is unreadable or malformed ({exc})"
        if artifact_problem is None and not previous_by_path:
            # A modules-empty artifact next to a manifest is a partial write: reported as a problem, not read as no modules (BUGFIX).
            artifact_problem = f"{artifact_path} carries no module records to reuse"
        if artifact_problem is not None:
            # An unusable artifact also drops the manifest, so the build is a full one, not an incremental diff against nothing.
            previous_manifest = None

    # A changed collector version forces a full build, so a schema change cannot leave an inconsistent merged list (BUGFIX).
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

    # The tree is scanned and hashed once, shared by the incremental path and the full-build fallback.
    # Modules and profile ini files go through the same pass, so a stale `--refresh` hashes once too.
    # HARDEN-24 B: the scan also returns the files the collect stage's own limits skipped, so a
    # refresh records them on the manifest exactly as a full build does.
    # `skipped_files` goes with `modules`: the files the caller's own scan skipped (CC review fixes 45).
    current_modules, current_skipped = (
        (modules, tuple(skipped_files or ())) if modules is not None
        else scan_repo_ex(root, config=config)
    )
    _log_skipped_files(current_skipped)
    if hashes is not None:
        current_hashes = hashes
        current_profile_hashes = profile_hashes if profile_hashes is not None else {}
    else:
        current_hashes, current_profile_hashes = _hash_modules_and_profiles(
            root, current_modules, config, config_path,
        )

    if previous_manifest is None:
        collect_dir, written, ctx, unsummarized = _full_build(
            root, config=config, config_path=config_path, llm_call=llm_call,
            modules=current_modules, hashes=current_hashes, profile_hashes=current_profile_hashes,
            drop_summaries=drop_summaries, skipped_files=tuple(current_skipped),
        )
        return CollectResult(
            action="refresh", wrote=True, fresh=True,
            message=_full_build_message(why, ctx, written, collect_dir, unsummarized=unsummarized),
            collect_dir=collect_dir, written_files=tuple(written),
        )

    # Provenance is captured before anything is written under the collect directory, excluding its previous output (as in _full_build).
    provenance = manifest_mod.capture_provenance(root, collect_dir=collect_dir)

    changes = manifest_mod.diff_files(previous_manifest.file_hashes, current_hashes)

    to_summarize = [m for m in current_modules if m.path in changes.changed]
    # Modules whose carried-forward summary is stale are re-summarised with the rest of the changed ones (V8).
    stale_paths = {
        p for p, prev in previous_by_path.items()
        if p not in changes.changed and prev.summary is not None
        and prev.summary.provenance == Provenance.LLM_STALE
    }
    to_summarize += [m for m in current_modules if m.path in stale_paths]
    # CC-15: modules that were left without a summary last time are retried too,
    # alongside the changed ones; unchanged modules that are already summarized
    # are not touched.
    previous_unsummarized = set(previous_manifest.unsummarized)
    queued = {m.path for m in to_summarize}
    to_summarize += [m for m in current_modules if m.path in previous_unsummarized and m.path not in queued]

    settings = read_collect_settings(config)
    # "Skipped" is reported only when there was something to summarise, not for a deletion-only refresh.
    pass_b_skipped = bool(to_summarize) and (llm_call is None or not settings.llm_summaries)
    summarized_by_path: Dict[str, ModuleRecord] = {}
    # Tracks every module path that was actually handed to summarize_repo this run;
    # unchanged / carried-forward modules are excluded, as are modules skipped by
    # pass_b_skipped (they never reached the LLM at all).
    summarized_paths: set = set()
    if to_summarize and not pass_b_skipped:
        sources_for_summary: Dict[str, str] = {}
        for m in to_summarize:
            if m.parse_error is not None:
                continue
            try:
                sources_for_summary[m.path] = (root / m.path).read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                # A file that changed or vanished before the re-read is left out of this summary batch, not a crash (BUGFIX).
                continue
        # A module that could not be re-read is not summarised from an empty source; it stays structural-only until the next run (BUGFIX).
        batch = [m for m in to_summarize if m.path in sources_for_summary]
        summarized_by_path = {
            m.path: m for m in summarize_repo(
                batch, sources_for_summary, llm_call,
                max_retries=collect_max_retries(config),
                progress_fn=_print_summarize_progress,
                on_error=_print_summarize_error,
                # The checkpoint covers this batch too, since an interrupted collect most often dies here (RUN-11).
                checkpoint_path=collect_dir / SUMMARIZE_CHECKPOINT_FILENAME,
                hashes=current_hashes,
                on_resume=_print_summarize_resume,
            )
        }
        summarized_paths = {m.path for m in batch}

    # With Pass B skipped, a changed module keeps its old summary, tagged stale, unless --drop-summaries removes it (V8).
    carried = stale = dropped = 0
    merged: List[ModuleRecord] = []
    for m in current_modules:
        if m.path in summarized_by_path:
            merged.append(summarized_by_path[m.path])
        elif m.path in changes.changed:
            prev = previous_by_path.get(m.path)
            if pass_b_skipped and prev is not None and prev.summary is not None:
                if drop_summaries:
                    dropped += 1
                    merged.append(m)
                else:
                    carried += 1
                    stale += 1
                    merged.append(m.with_llm_summary(prev.summary.as_stale()))
            else:
                merged.append(m)
        else:
            # An unchanged module keeps its previous record verbatim, so its purpose stays byte-identical across refreshes.
            merged.append(previous_by_path.get(m.path, m))
    merged.sort(key=lambda m: m.path)

    ctx = build_context(root, merged, config=config, config_path=config_path, llm_call=None)
    if any(m.summary is not None for m in merged):
        sources = _sources_for(root, merged)
        verified_modules, report = verifier_mod.verify_repo(
            merged, sources, root=root, import_edges=ctx.import_edges,
        )
        ctx = replace(ctx, modules=verified_modules, verification_report=report)
    ctx = replace(
        ctx, pass_b_ran=bool(to_summarize) and not pass_b_skipped,
        summaries_carried=carried, summaries_stale=stale, summaries_dropped=dropped,
    )

    written = _write_artifact(collect_dir, ctx)
    # The hashes computed for the diff are reused for the manifest, so the tree is not hashed twice.
    # CC-15: record which modules Pass B left without a summary so the next refresh retries them.
    # Only modules actually sent to Pass B this run count — unchanged modules carried forward
    # from a previous build keep whatever summary state they already had, and skipped batches
    # (no llm_call, llm_summaries=false) never attempted anything.
    #
    # CC-16 (Part A): a refresh that does not run Pass B hands nothing to it, so the rule above
    # alone would rebuild the list from an empty batch and *erase* the previous one — the list
    # is the only record of which modules still have no summary, and the manifest is the only
    # place it lives, so a `--no-llm` / `llm_summaries = false` refresh (or any caller that
    # passes no `llm_call`: `loader.load` with `staleness = refresh`, claimcheck's target and
    # contest's draft) silently re-marked the tree clean and the module was never retried.
    # Such a refresh adds nothing — it attempted nothing — but it must *keep* the previous
    # list, restricted to what is still true of the artifact it just wrote: the path is still
    # in the tree (a deleted or moved module drops out) and its record still carries no
    # summary. A refresh that does run Pass B keeps its own rule for the modules it handed
    # over and takes the same restriction for the listed ones it did not (none, since CC-15
    # hands them all) — one list either way. A full build is untouched: `_full_build` rebuilds
    # the list from the modules it handed to Pass B, so a `--no-llm` full build records `[]`.
    summarized_unsummarized = {
        m.path for m in ctx.modules
        if m.summary is None and m.parse_error is None and m.path in summarized_paths
    }
    carried_unsummarized = {
        p for p in previous_unsummarized
        if p not in summarized_paths
        and any(m.path == p and m.summary is None and m.parse_error is None for m in ctx.modules)
    }
    unsummarized = tuple(sorted(summarized_unsummarized | carried_unsummarized))
    _write_manifest(root, collect_dir, ctx.modules, provenance=provenance,
                    file_hashes=current_hashes, config=config, unsummarized=unsummarized,
                    profile_hashes=current_profile_hashes,
                    skipped_files=tuple(current_skipped))
    written = sorted(set(written) | {MANIFEST_FILENAME})

    if changes.is_empty():
        message = f"tree unchanged — recomputed derived artifacts only, wrote {len(written)} file(s) in {collect_dir}"
    else:
        # Zero counts are left out of the message: only the non-zero part is shown.
        scope = f"{len(changes.changed)} changed" if changes.changed else f"{len(changes.removed)} removed"
        if changes.changed and changes.removed:
            scope += f" and {len(changes.removed)} removed"
        # Pass B skipped or every changed module failed: say so, not "refreshed" (BUGFIX).
        if pass_b_skipped:
            suffix = f" (Pass B skipped{_pass_b_suffix(ctx)})"
        elif to_summarize and not summarized_by_path:
            suffix = " (Pass B produced no summary)"
        else:
            suffix = ""
        # CC-15: name modules that still lack a summary after the batch. CC-16 (Part A): the
        # count is the whole list the manifest now records, carried-over modules included, so
        # the line and `--check` cannot disagree about what is still owed.
        n_unsummarized = len(unsummarized)
        if n_unsummarized:
            suffix += f", {n_unsummarized} without a summary (tried again at the next --collect)"
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
    drop_summaries: bool = False,
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

    Cost: one Pass A scan and one hash pass — that one pass covers both the
    modules and the profile `.ini` files `CONFIG_MAP.md` is built from,
    hashed together and split afterwards rather than twice (HARDEN-21 C).
    `_freshness` — the same verdict `action_check` reports — returns the scan
    and the maps it computed, and they are handed to `action_refresh`
    (`modules=`/`hashes=`/`profile_hashes=`), which passes them on to
    `_write_manifest`. This used to scan the tree twice
    and hash it three times (freshness gate, the diff, then again inside
    `manifest.build_manifest`); Pass B was already down to one call per
    changed module, so Pass A and the hash passes were the next largest
    things on the receipt.
    """
    verdict, modules, hashes, profile_hashes, skipped = _freshness(Path(root), config, config_path)
    if verdict.fresh:
        return CollectResult(
            action="collect", wrote=False, fresh=True,
            message="already up to date — nothing to do",
            collect_dir=verdict.collect_dir,
        )
    result = action_refresh(
        root, config=config, config_path=config_path, llm_call=llm_call,
        modules=modules, hashes=hashes, profile_hashes=profile_hashes,
        drop_summaries=drop_summaries, skipped_files=skipped,
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
    drop_summaries: bool = False,
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

    # Reject an artifact from an older collector_version before reusing its records (BUGFIX).
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

    # A broken stored artifact raises a clean CollectCliError, not a raw TypeError/KeyError (BUGFIX).
    try:
        modules = [ModuleRecord.from_dict(d) for d in payload.get("modules", [])]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # A valid JSON non-object raises AttributeError, which is now caught too (bugfix).
        raise CollectCliError(f"existing artifact at {artifact_path} is malformed: {exc}") from exc
    by_path = {m.path: m for m in modules}

    abs_module = root / module_path
    if not abs_module.is_file():
        raise CollectCliError(f"--module path does not exist under {root}: {module_path}")
    try:
        source = abs_module.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        # A read failure (permissions, bad UTF-8) becomes a clean CollectCliError (BUGFIX).
        raise CollectCliError(f"--module path is unreadable: {module_path}: {exc}") from exc
    patched = scan_file(source, module_path, config=config)

    previous_record = by_path.get(module_path)
    if module_path not in by_path:
        modules.append(patched)
    else:
        modules = [patched if m.path == module_path else m for m in modules]
    modules.sort(key=lambda m: m.path)

    # Pass B runs only on the re-scanned module and Pass C still verifies it (BUGFIX).
    settings = read_collect_settings(config)
    # Pass B is skipped by config or no LLM; a parse error means nothing to summarise (BUGFIX).
    pass_b_skipped = llm_call is None or not settings.llm_summaries
    if not pass_b_skipped and patched.parse_error is None:
        # No checkpoint for a one-module batch, so it cannot delete an interrupted build's checkpoint (RUN-11).
        summarized = summarize_repo(
            [patched], {module_path: source}, llm_call,
            max_retries=collect_max_retries(config),
            progress_fn=_print_summarize_progress,
            on_error=_print_summarize_error,
        )
        if summarized and summarized[0].summary is not None:
            patched = summarized[0]
            modules = [patched if m.path == module_path else m for m in modules]
    elif pass_b_skipped and not drop_summaries and previous_record is not None and previous_record.summary is not None:
        # V8: a skipped Pass B keeps the re-scanned module's old summary, tagged stale.
        patched = patched.with_llm_summary(previous_record.summary.as_stale())
        modules = [patched if m.path == module_path else m for m in modules]

    # Provenance is read before `.collect/` is written, so its own output does not mark the tree dirty.
    git_sha, dirty = manifest_mod.capture_provenance(root, collect_dir=collect_dir)

    ctx = build_context(root, modules, config=config, config_path=config_path, llm_call=None)

    # Pass C is not run by build_context here, so verify the patched module explicitly (BUGFIX).
    if any(m.summary is not None for m in modules):
        sources = _sources_for(root, modules)
        verified_modules, report = verifier_mod.verify_repo(
            modules, sources, root=root, import_edges=ctx.import_edges,
        )
        modules = verified_modules
        ctx = replace(ctx, modules=verified_modules, verification_report=report)

    written = _write_artifact(collect_dir, ctx)

    # Patch only this file's manifest entry instead of rehashing the whole tree.
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
        # Use hash_tree so a file that vanished since it was read is dropped, not a crash (BUGFIX).
        fresh_hashes = manifest_mod.hash_tree(root, [module_path])
        if module_path in fresh_hashes:
            hashes[module_path] = fresh_hashes[module_path]
        else:
            hashes.pop(module_path, None)
    else:
        # No usable previous manifest: record the whole merged set, not one file (BUGFIX).
        hashes = manifest_mod.hash_tree(root, sorted(m.path for m in modules))

    patched_manifest = manifest_mod.Manifest(
        collector_version=manifest_mod.COLLECTOR_VERSION,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        git_sha=git_sha,
        dirty=dirty,
        file_hashes=hashes,
        # The skip_dirs list is recorded too, so a patched manifest is not read as stale (CC-13).
        skip_dirs=tuple(sorted(manifest_mod.skip_dirs_from_config(config))),
    )
    manifest_mod.write_manifest(patched_manifest, manifest_path)
    written = sorted(set(written) | {MANIFEST_FILENAME})

    # The message names Pass B's outcome: skipped, or ran but produced no summary (BUGFIX).
    if pass_b_skipped and patched.summary is not None:
        suffix = " (Pass B skipped; summary carried forward, marked llm-stale)"
    elif pass_b_skipped:
        suffix = " (Pass B skipped)"
    elif patched.summary is None:
        suffix = " (Pass B produced no summary)"
    else:
        suffix = ""
    return CollectResult(
        action="module", wrote=True, fresh=True,
        message=f"patched {module_path} and refreshed {len(written)} file(s) in {collect_dir}{suffix}",
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
    drop_summaries: bool = False,
) -> CollectResult:
    """Single dispatch point for all five actions — what `main.py`'s
    `/collect` command / `--collect`/`--check`/`--refresh`/`--rebuild`/
    `--module` flags call."""
    if action not in VALID_ACTIONS:
        raise CollectCliError(f"unknown collect action {action!r}; must be one of {sorted(VALID_ACTIONS)}")
    root = Path(root)
    if action == "check":
        return action_check(root, config=config, config_path=config_path)

    settings = read_collect_settings(config)
    if not settings.enabled and action != "check":
        return CollectResult(
            action=action, wrote=False, fresh=None,
            message="collect is disabled ([collect] enabled = false) — nothing done",
            collect_dir=resolve_collect_dir(root, config),
        )
    if action == "refresh":
        return action_refresh(
            root, config=config, config_path=config_path, llm_call=llm_call, drop_summaries=drop_summaries,
        )
    if action == "rebuild":
        return action_rebuild(
            root, config=config, config_path=config_path, llm_call=llm_call, drop_summaries=drop_summaries,
        )
    if action == "module":
        if not module_path:
            raise CollectCliError("action='module' requires module_path")
        return action_module(
            root, module_path, config=config, config_path=config_path, llm_call=llm_call,
            drop_summaries=drop_summaries,
        )
    return action_collect(
        root, config=config, config_path=config_path, llm_call=llm_call, drop_summaries=drop_summaries,
    )


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
    check = "--check" in argv
    module_path = None
    # --check wins over --module, so a malformed --module is resolved only when --check is absent (BUGFIX).
    if not check and ("--module" in argv or any(a.startswith("--module=") for a in argv)):
        # Both "--module <path>" and "--module=<path>" are accepted, as argparse does (BUGFIX).
        module_path = _module_path_from(argv)

    action, _ = action_from_flags(
        check=check,
        module_path=module_path,
        rebuild="--rebuild" in argv,
        refresh="--refresh" in argv,
    )
    return {"action": action, "module_path": module_path, "drop_summaries": "--drop-summaries" in argv}


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
        # Read agents.ini inside the try, so a non-UTF-8 file is reported, not a crash (AUTO-FIX).
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
