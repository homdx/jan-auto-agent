"""tools/collect/render.py — COLLECT-18: JSON → Markdown renderers.

The human-readable layer of `collect` mode: nine markdown pages
(`ARCHITECTURE.md`, `MODULE_MAP.md`, `CONTRACTS.md`, `FAIL_OPEN_REGISTRY.md`,
`GATES.md`, `TEST_MAP.md`, `RISK_INDEX.md`, `CONFIG_MAP.md`, `GLOSSARY.md`)
generated strictly from the JSON-shaped facts EPIC A–D already built —
`ModuleRecord`/`ContractRecord` (COLLECT-1/4), `FailOpenEntry` (COLLECT-9),
`GateEntry` (COLLECT-15), the import graph (COLLECT-8), `TEST_MAP`
(COLLECT-12), `RISK_INDEX` (COLLECT-13), `CONFIG_MAP` (COLLECT-14).

No renderer in this module discovers a new fact, computes anything, or
calls an LLM — every `render_*` function is a pure, deterministic
formatting step over data its caller already assembled. That is the whole
COLLECT-18 guarantee:

    MD and JSON cannot diverge, because MD is derived — every string
    a renderer emits is either a literal field off an input record, or
    fixed scaffolding text (headers, table syntax) that carries no fact
    of its own.

Concretely: delete a module from the input list and its whole section
disappears from `MODULE_MAP.md`; nothing here has a second, independent
notion of "what modules exist" to fall back on. `tests/test_collect_render.py`
checks exactly this (an ablation test), plus that two renders of the same
input are byte-identical (COLLECT-3) and that every fact string that ends
up in a page can be found in the canonical JSON of the input it came from.

This module does no file I/O — `render_all` returns an in-memory
`{filename: markdown_text}` dict; writing it to disk is the CLI's job
(COLLECT-19).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from tools.collect.config_map import ConfigMapEntry, SiblingGap
from tools.collect.gates import GateEntry
from tools.collect.graph import Graph
from tools.collect.model import ContractRecord, ModuleRecord
from tools.collect.registries import FailOpenEntry
from tools.collect.risk import RiskEntry

#: The exact nine pages COLLECT-18's brief names, in the order `render_all`
#: assembles them.
PAGE_NAMES: Tuple[str, ...] = (
    "ARCHITECTURE.md",
    "MODULE_MAP.md",
    "CONTRACTS.md",
    "FAIL_OPEN_REGISTRY.md",
    "GATES.md",
    "TEST_MAP.md",
    "RISK_INDEX.md",
    "CONFIG_MAP.md",
    "GLOSSARY.md",
)


# ── tiny markdown-formatting helpers (scaffolding, not facts) ──────────────


def _h(level: int, text: str) -> str:
    return f"{'#' * level} {text}"


def _escape_cell(value: object) -> str:
    """Neutralize characters that would break a pipe-table cell. This never
    changes what the value *says* — just makes it safe to sit inside `| |`
    — so it doesn't introduce or drop any fact."""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _table(headers: Tuple[str, ...], rows: List[Tuple[object, ...]]) -> str:
    """A minimal, deterministic GitHub-flavored markdown table."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(_escape_cell(c) for c in row) + " |")
    return "\n".join(lines)


# ── ARCHITECTURE.md ─────────────────────────────────────────────────────────


def render_architecture(
    modules: Iterable[ModuleRecord],
    *,
    import_edges: Graph,
    imported_by: Graph,
    entry_points: List[str],
) -> str:
    """Module count, entry points (COLLECT-8), and each module's local
    imports + blast radius — every number here reads straight off
    `import_edges`/`imported_by`, nothing recomputed."""
    modules = sorted(modules, key=lambda m: m.path)
    lines = [_h(1, "ARCHITECTURE"), ""]
    lines.append(
        f"{len(modules)} module(s) scanned. {len(entry_points)} entry point(s) "
        "(modules nothing else in this repo imports)."
    )
    lines.append("")
    lines.append(_h(2, "Entry points"))
    if entry_points:
        for ep in sorted(entry_points):
            lines.append(f"- `{ep}`")
    else:
        lines.append("_None._")
    lines.append("")
    lines.append(_h(2, "Import graph"))
    rows: List[Tuple[object, ...]] = []
    for m in modules:
        imports = sorted(import_edges.get(m.path, ()))
        importers = sorted(imported_by.get(m.path, ()))
        rows.append(
            (
                f"`{m.path}`",
                len(importers),
                ", ".join(f"`{p}`" for p in imports) or "_none_",
            )
        )
    lines.append(_table(("module", "blast radius", "imports (local)"), rows))
    lines.append("")
    return "\n".join(lines) + "\n"


# ── MODULE_MAP.md ────────────────────────────────────────────────────────────


def render_module_map(modules: Iterable[ModuleRecord]) -> str:
    """One section per module: its `summary` (if Pass B ran, clearly
    labeled `llm`), its imports, and a table of its `public_symbols`
    (COLLECT-4) — signature, docstring first line, and `is_private`."""
    modules = sorted(modules, key=lambda m: m.path)
    lines = [_h(1, "MODULE_MAP"), ""]
    for m in modules:
        lines.append(_h(2, f"`{m.path}`"))
        if m.parse_error:
            lines.append(f"**parse error:** {m.parse_error}")
            lines.append("")
            continue
        if m.summary is not None and (m.summary.purpose or m.summary.notes):
            if m.summary.purpose:
                lines.append(f"_{m.summary.purpose}_ (provenance: `{m.summary.provenance}`)")
            if m.summary.notes:
                lines.append(f"Notes: {m.summary.notes}")
            lines.append("")
        if m.imports:
            lines.append("Imports: " + ", ".join(f"`{i}`" for i in m.imports))
            lines.append("")
        if m.public_symbols:
            rows = [
                (
                    f"`{s.qualname}`",
                    f"`{s.signature}`",
                    "yes" if s.is_private else "no",
                    s.docstring_first_line,
                )
                for s in m.public_symbols
            ]
            lines.append(_table(("symbol", "signature", "private", "docstring"), rows))
        else:
            lines.append("_No top-level symbols._")
        lines.append("")
    return "\n".join(lines) + "\n"


# ── CONTRACTS.md ─────────────────────────────────────────────────────────────


def render_contracts(contracts: Iterable[ContractRecord]) -> str:
    """One row per `ContractRecord` (COLLECT-10): name, kind
    (seed/derived), the symbol it cites, and its description — verbatim."""
    contracts = sorted(contracts, key=lambda c: c.name)
    lines = [_h(1, "CONTRACTS"), ""]
    if not contracts:
        lines.append("_No contracts recorded._")
        lines.append("")
        return "\n".join(lines) + "\n"
    rows = [
        (c.name, c.kind, f"`{c.known_edge}`" if c.known_edge else "", c.description)
        for c in contracts
    ]
    lines.append(_table(("name", "kind", "known_edge", "description"), rows))
    lines.append("")
    return "\n".join(lines) + "\n"


# ── FAIL_OPEN_REGISTRY.md ────────────────────────────────────────────────────


def render_fail_open_registry(entries: Iterable[FailOpenEntry]) -> str:
    """One row per `FailOpenEntry` (COLLECT-9): location, exception type,
    and the literal source-comment rationale, if any."""
    entries = sorted(entries, key=lambda e: e.location)
    lines = [_h(1, "FAIL_OPEN_REGISTRY"), ""]
    if not entries:
        lines.append("_No fail-open sites recorded._")
        lines.append("")
        return "\n".join(lines) + "\n"
    rows = [
        (f"`{e.location}`", e.exception_type, e.rationale if e.rationale else "_none_")
        for e in entries
    ]
    lines.append(_table(("location", "exception_type", "rationale"), rows))
    lines.append("")
    return "\n".join(lines) + "\n"


# ── GATES.md ─────────────────────────────────────────────────────────────────


def render_gates(gates: Iterable[GateEntry]) -> str:
    """One row per `GateEntry` (COLLECT-15): its module/parser citation,
    reply protocol, fail-mode, and config switch."""
    gates = sorted(gates, key=lambda g: g.name)
    lines = [_h(1, "GATES"), ""]
    if not gates:
        lines.append("_No gates recorded._")
        lines.append("")
        return "\n".join(lines) + "\n"
    rows = [
        (
            g.name,
            f"`{g.module}`",
            f"`{g.parser}`",
            g.protocol,
            g.fail_mode,
            "yes" if g.extra_llm_call else "no",
            g.config_switch if g.config_switch else "_always on_",
            g.config_default,
        )
        for g in gates
    ]
    lines.append(
        _table(
            ("gate", "module", "parser", "protocol", "fail_mode", "extra_llm_call", "config_switch", "default"),
            rows,
        )
    )
    lines.append("")
    return "\n".join(lines) + "\n"


# ── TEST_MAP.md ──────────────────────────────────────────────────────────────


def render_test_map(
    test_map: Dict[str, Tuple[str, ...]],
    *,
    zero_coverage: Iterable[str] = (),
    thin_coverage: Iterable[str] = (),
) -> str:
    """The zero-list and thin-list worklists (COLLECT-12), plus the full
    module -> covering-tests map they were both computed from."""
    lines = [_h(1, "TEST_MAP"), ""]

    lines.append(_h(2, "Zero coverage"))
    zero_coverage = sorted(zero_coverage)
    if zero_coverage:
        for path in zero_coverage:
            lines.append(f"- `{path}`")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(_h(2, "Thin coverage"))
    thin_coverage = sorted(thin_coverage)
    if thin_coverage:
        for path in thin_coverage:
            covering = ", ".join(f"`{t}`" for t in test_map.get(path, ()))
            lines.append(f"- `{path}` (covered by {covering})")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(_h(2, "Full map"))
    rows = [
        (f"`{path}`", ", ".join(f"`{t}`" for t in tests) or "_none_")
        for path, tests in sorted(test_map.items())
    ]
    lines.append(_table(("module", "covering tests"), rows))
    lines.append("")
    return "\n".join(lines) + "\n"


# ── RISK_INDEX.md ────────────────────────────────────────────────────────────


def render_risk_index(entries: Iterable[RiskEntry]) -> str:
    """One row per `RiskEntry` (COLLECT-13). Row order is `entries`'s own
    order (already `(-score, path)`-sorted by `risk.compute_risk_index`) —
    this renderer never re-sorts, since re-sorting here would be a second,
    independent notion of "risk order" that could quietly diverge from the
    one COLLECT-13 actually computed."""
    entries = list(entries)
    lines = [_h(1, "RISK_INDEX"), ""]
    if not entries:
        lines.append("_No modules scored._")
        lines.append("")
        return "\n".join(lines) + "\n"
    rows = [
        (
            f"`{e.path}`",
            e.score,
            e.loc,
            e.blast_radius,
            e.unguarded_count,
            e.undocumented_fail_open_count,
            "yes" if e.zero_coverage else "no",
        )
        for e in entries
    ]
    lines.append(
        _table(
            ("module", "score", "loc", "blast_radius", "unguarded", "undocumented_fail_open", "zero_coverage"),
            rows,
        )
    )
    lines.append("")
    return "\n".join(lines) + "\n"


# ── CONFIG_MAP.md ────────────────────────────────────────────────────────────


def render_config_map(
    entries: Iterable[ConfigMapEntry],
    *,
    sibling_gaps: Iterable[SiblingGap] = (),
) -> str:
    """CONFIG_MAP (COLLECT-14): the `{key}_{task_mode}` matrix, plus
    `diff_sibling_profiles`'s cross-check of every sibling `.ini` profile
    against the primary."""
    entries = sorted(entries, key=lambda e: (e.section, e.key_template))
    lines = [_h(1, "CONFIG_MAP"), ""]
    if entries:
        rows = [
            (
                f"`{e.section}.{e.key_template}`",
                ", ".join(f"`{k}`" for k in e.concrete_keys) if e.has_mode_override else "_n/a_",
                ", ".join(f"`{r}`" for r in e.readers),
                ", ".join(str(f) for f in e.fallbacks),
            )
            for e in entries
        ]
        lines.append(_table(("key", "concrete keys (mode override)", "readers", "fallbacks"), rows))
    else:
        lines.append("_No config reads recorded._")
    lines.append("")

    lines.append(_h(2, "Sibling-profile gaps"))
    sibling_gaps = sorted(sibling_gaps, key=lambda g: (g.section, g.key))
    if sibling_gaps:
        rows = [
            (f"`{g.section}.{g.key}`", g.present_in, ", ".join(g.missing_in))
            for g in sibling_gaps
        ]
        lines.append(_table(("key", "present_in", "missing_in"), rows))
    else:
        lines.append("_No gaps detected._")
    lines.append("")
    return "\n".join(lines) + "\n"


# ── GLOSSARY.md ──────────────────────────────────────────────────────────────
#
# The one page that isn't a projection of a scanned repo's facts — there is
# no "glossary fact" for Pass A to have extracted. It defines the fixed
# vocabulary the other eight pages use, so it's fixed, deterministic text
# that ships with this module rather than being computed per-run; two
# renders are trivially byte-identical since there's no input to vary.

_GLOSSARY_TERMS: Tuple[Tuple[str, str], ...] = (
    ("static", "A fact produced directly by Pass A's AST walk (EPIC B); ground truth, never LLM prose (COLLECT-1)."),
    ("llm", "Prose written by Pass B's summarizer (COLLECT-16); always unverified until Pass C (COLLECT-17) checks it."),
    ("derived", "A fact computed by a pure function of other static facts (e.g. RISK_INDEX, a contract inferred from the call graph); not raw AST, not LLM, but still trustworthy."),
    ("GUARDED / UNGUARDED", "Whether a `GuardedAccess` (an indexed access like `stack[-1]`) has a dataflow-provable guard above it (COLLECT-7)."),
    ("fail-open", "An `except` block whose body silently swallows the exception (e.g. `pass`) rather than logging, re-raising, or altering control flow (COLLECT-6)."),
    ("blast radius", "The number of modules that import a given module (COLLECT-8); a proxy for how much a bug there could affect."),
    ("zero coverage / thin coverage", "A source module with no covering test file at all, or covered by only a handful (COLLECT-12)."),
    ("contract", "A cross-module invariant, either hand-seeded or derived from other static facts (COLLECT-10); never LLM prose."),
    ("gate", "One of this pipeline's quality gates (`gate1`/`verdict`/`continuity`/`theme`/`fact`/`canon`/`language`), its response protocol, and its fail-mode (COLLECT-15)."),
    ("risk score", "A deterministic, weighted combination of LOC, blast radius, UNGUARDED count, undocumented fail-open count, and zero-coverage for a module (COLLECT-13)."),
)


def render_glossary() -> str:
    lines = [_h(1, "GLOSSARY"), ""]
    for term, definition in _GLOSSARY_TERMS:
        lines.append(f"- **{term}** — {definition}")
    lines.append("")
    return "\n".join(lines) + "\n"


# ── render_all: every page in one call ──────────────────────────────────────


def render_all(
    *,
    modules: Iterable[ModuleRecord],
    import_edges: Graph,
    imported_by: Graph,
    entry_points: List[str],
    contracts: Iterable[ContractRecord] = (),
    fail_open_registry: Iterable[FailOpenEntry] = (),
    gates: Iterable[GateEntry] = (),
    test_map: Optional[Dict[str, Tuple[str, ...]]] = None,
    zero_coverage: Iterable[str] = (),
    thin_coverage: Iterable[str] = (),
    risk_index: Iterable[RiskEntry] = (),
    config_map: Iterable[ConfigMapEntry] = (),
    sibling_gaps: Iterable[SiblingGap] = (),
) -> Dict[str, str]:
    """Every one of `PAGE_NAMES`, rendered from the caller's already-built
    facts. This function does no computation of its own beyond dispatching
    to the `render_*` functions above — the CLI (COLLECT-19) is expected to
    have already run Pass A/B/C and the EPIC C/D builders and simply hand
    the results straight through.
    """
    modules = list(modules)
    test_map = test_map or {}
    pages = {
        "ARCHITECTURE.md": render_architecture(
            modules, import_edges=import_edges, imported_by=imported_by, entry_points=entry_points
        ),
        "MODULE_MAP.md": render_module_map(modules),
        "CONTRACTS.md": render_contracts(contracts),
        "FAIL_OPEN_REGISTRY.md": render_fail_open_registry(fail_open_registry),
        "GATES.md": render_gates(gates),
        "TEST_MAP.md": render_test_map(test_map, zero_coverage=zero_coverage, thin_coverage=thin_coverage),
        "RISK_INDEX.md": render_risk_index(risk_index),
        "CONFIG_MAP.md": render_config_map(config_map, sibling_gaps=sibling_gaps),
        "GLOSSARY.md": render_glossary(),
    }
    assert set(pages) == set(PAGE_NAMES)  # keep the dict and the manifest in lockstep
    return pages
