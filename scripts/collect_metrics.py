#!/usr/bin/env python3
"""scripts/collect_metrics.py — EPIC M1: the static baseline.

One read-only pass over one `.collect/` directory. No repo scan, no LLM, no
network, no git: the artifact is the only input. Two outputs — a human table
on stdout, and `--json` for diffing.

    python3 scripts/collect_metrics.py --collect-dir ../jan-to-fix-pull-v2/.collect \\
        --json docs/collect-epics/baseline.json

Tier 0 and Tier 1 of `docs/collect-epics/EPIC-M-metrics.md`, measured from
`artifact.json` alone. The per-task block section is measured, not estimated:
`build_collect_context_block` is imported and run for every module, over the
same `CollectModel` the coder gets (`loader._load_from_dir`, artifact only —
never `loader.load()`, which scans the repo to judge freshness), so a change to
the pack shows up as a number rather than an argument.

Where the ticket's table and this measurement disagree, all three found by
checking the live source before implementing (ground rule 1):

1. `collector_version` is not a key of `artifact.json` — it lives in
   `collect_manifest.json` next to it. The header prints the manifest value and
   says where it came from; a manifest without the key prints `unknown`.
2. "symbols silently cut by [:20]" is 0 today, by construction: the silent
   `[:20]` slice was replaced by a budget cut that announces its remainder
   (`_SYMBOLS_CUT_NOTE`, PLAN-v2 V2.3). The metric still means something —
   symbols absent from the rendered block *without* an announcement — and it
   stays in the table as the canary for that regression. Likewise the budget
   cut is now enforced *inside* the renderer (`budget=`), so a block rendered
   with a budget never overshoots it; "blocks over max_context_chars" is
   therefore counted on the unbudgeted render, which is what decides how often
   `CollectBridge._shrink` still has to fire.
3. Three lines of the ticket's block table measure the artifact before
   PLAN-v2 V2/V3 landed (a rebuilt 483-module artifact, a silent symbol cap, a
   pack of `public_symbols` + `config_read` only):

       modules asked / non-empty blocks      477 -> 469
       median block                          544 -> 678   (the complete list)
       blocks over max_context_chars          50 -> 68     (14.5%)
       rows not derivable per block        0.008 -> 2.559

   The last is EPIC A's whole point — V3's `callers`/`calls_into`/`tests` rows
   are the pack's first facts the target file cannot show — so it is reported
   as measured, not restated as zero.

Contract
--------
* **Read-only.** The only path this script opens for writing is the one named
  by `--json`. The collect dir is read with `read_text`/`stat` and never
  created.
* **Deterministic.** `--json` writes a flat dict with stable keys, sorted
  alphabetically, and no clock-dependent value, so two consecutive runs are
  byte-identical and a later diff is a plain key-by-key comparison.
* **Fails closed for its own failure, fails open elsewhere.** A missing or
  corrupt `.collect/` is the one hard error: a clear message on stderr and
  exit 1. A single absent table inside an otherwise healthy artifact degrades
  to 0 rather than a traceback, so an older producer still yields a comparable
  baseline.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Two imports from `tools/`, both read-only. `build_collect_context_block` is
# the renderer EPIC A/B/C all argue about; `_load_from_dir` is the loader's
# artifact-only read path (`loader.load()` would `scan_repo` to decide
# freshness — a repo scan this script must never do). The renderer is fed the
# same `CollectModel` the coder gets, so "as rendered today" is the shipping
# block, not a stand-in's answer to the renderer's questions.
from tools.auto.context_assembler import build_collect_context_block  # noqa: E402
from tools.collect.loader import (  # noqa: E402
    STATUS_FRESH, CollectModel, _load_from_dir,
)

ARTIFACT_FILENAME = "artifact.json"
MANIFEST_FILENAME = "collect_manifest.json"

# `[collect] max_context_chars_auto` in agents.ini — the per-task budget
# `CollectBridge._shrink` is measured against. A CLI flag, because this script
# reads no config file: the budget is a property of the run, not of the
# artifact, and hard-coding one repo's config into a baseline would make two
# baselines differ for a reason nobody can see in the diff.
DEFAULT_MAX_CONTEXT_CHARS = 1200

# `name(…)` is the whole point of EPIC C4: every symbol's parameter list is
# unknowable from the artifact while the signature is elided. Matched on the
# tail, so a real signature like `f(a: int, *args) -> str` does not count.
_ELIDED_SUFFIX = "(...)"


# M2's classification, shipped with M1 because M1's own table carries the "rows
# per block that are NOT derivable from the target file's own source" line. One
# fixed dict on purpose: it has to be stable, not clever, so a before/after pair
# is comparable. `redundant` = visible by reading the target file, which the
# coder already has in full in the same prompt.
ROW_KIND_CLASS: "Dict[str, str]" = {
    "module": "redundant",        # the file path itself
    "parse_error": "redundant",   # a statement about this file's own source
    "public_symbols": "redundant",
    "config_read": "redundant",   # every read site is in the file
    "callers": "new",             # who breaks lives outside this file
    "calls_into": "new",          # first-party imports, resolved via the artifact
    "tests": "new",               # the covering test tree
    "contract": "new",            # lives in a registry, not in the file
}

ROW_KIND_BY_PREFIX = (
    ("module: ", "module"),
    ("parse_error: ", "parse_error"),
    ("public_symbols: ", "public_symbols"),
    ("config_read [", "config_read"),
    ("callers: ", "callers"),
    ("calls_into: ", "calls_into"),
    ("tests: ", "tests"),
    ("contract ", "contract"),
)

_SYMBOLS_CUT_NOTE_RE = re.compile(r"… \(\+(\d+) more, cut for budget\)$")


class CollectMetricsError(RuntimeError):
    """The script's one hard failure: no usable artifact to measure."""


# ── the artifact, read ───────────────────────────────────────────────────────


def read_collect_dir(collect_dir: Path) -> "tuple[dict, dict]":
    """Load `artifact.json` and return ``(payload, meta)``.

    `meta` carries the three facts a table value cannot stand in for: the
    artifact's byte size, its mtime, and the `collector_version` recorded next
    to it. Raises `CollectMetricsError` rather than returning a partial payload
    — a metrics run that "succeeds" on an unreadable artifact is worse than one
    that refuses, because its zeros would look like a regression.
    """
    collect_dir = Path(collect_dir)
    artifact_path = collect_dir / ARTIFACT_FILENAME
    if not collect_dir.is_dir():
        raise CollectMetricsError(
            f"{collect_dir}: no such directory — nothing to measure. Run "
            "`--collect` in the target repo first."
        )
    if not artifact_path.is_file():
        raise CollectMetricsError(
            f"{artifact_path}: missing — that is the only input. {collect_dir} "
            f"exists but no collect run has produced {ARTIFACT_FILENAME}."
        )

    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CollectMetricsError(f"{artifact_path}: unreadable ({exc})") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("modules"), list):
        found = "an object without `modules`" if isinstance(payload, dict) else type(payload).__name__
        raise CollectMetricsError(
            f"{artifact_path}: not an artifact — expected a JSON object with a "
            f"`modules` list, got {found}."
        )

    meta: Dict[str, Any] = {
        "collect_dir": str(collect_dir),
        "artifact_bytes": artifact_path.stat().st_size,
        "artifact_mtime": _utc_stamp(artifact_path.stat().st_mtime),
        "collector_version": "unknown",
        "collector_version_source": "absent",
    }
    manifest_path = collect_dir / MANIFEST_FILENAME
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict) and isinstance(manifest.get("collector_version"), (str, int)):
            meta["collector_version"] = str(manifest["collector_version"])
            meta["collector_version_source"] = f"{MANIFEST_FILENAME}:collector_version"
    # The ticket asks for "the artifact's `collector_version`"; that key does
    # not exist in artifact.json. Check it anyway, so an artifact that starts
    # carrying it is picked up without another edit to this script.
    if isinstance(payload.get("collector_version"), (str, int)):
        meta["collector_version"] = str(payload["collector_version"])
        meta["collector_version_source"] = f"{ARTIFACT_FILENAME}:collector_version"
    return payload, meta


def _utc_stamp(epoch_seconds: float) -> str:
    return _dt.datetime.fromtimestamp(epoch_seconds, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── metric sections ──────────────────────────────────────────────────────────


def _count_table(payload: dict, key: str) -> int:
    """Size of one top-level table: a missing or wrong-shaped key is 0, not a
    traceback. An older producer that never wrote the key still yields a
    baseline that lines up key-by-key with a newer one."""
    value = payload.get(key)
    if key in ("import_edges", "imported_by", "test_map"):
        return len(value) if isinstance(value, dict) else 0
    return len(value) if isinstance(value, (list, tuple)) else 0


def _artifact_metrics(payload: dict, meta: dict) -> "Dict[str, Any]":
    modules = [m for m in payload.get("modules") or [] if isinstance(m, dict)]
    symbols = [s for m in modules for s in (m.get("public_symbols") or []) if isinstance(s, dict)]
    elided = sum(1 for s in symbols if (s.get("signature") or "").endswith(_ELIDED_SUFFIX))
    # A method is a qualname whose module-side part is dotted: `path:Class.method`.
    # 0 today — the scanner indexes top-level names only, which is C4's scope.
    methods = sum(1 for s in symbols if "." in (s.get("qualname") or "").split(":", 1)[-1])
    return {
        "modules": len(modules),
        "artifact_bytes": meta["artifact_bytes"],
        "public_symbols": len(symbols),
        "public_symbols_unique_qualnames": len({s.get("qualname") for s in symbols if s.get("qualname")}),
        "signatures_elided": elided,
        "signatures_total": len(symbols),
        "methods_indexed": methods,
    }


def _test_path_is_test(payload: dict, path: str) -> bool:
    """`CollectModel._is_test_path`, applied to the raw payload: the summary
    tally does not go through the loader, so the test-path rule is borrowed
    rather than copied."""
    test_map = payload.get("test_map") or {}
    test_paths = frozenset(
        test
        for entries in (test_map if isinstance(test_map, dict) else {}).values()
        if isinstance(entries, (list, tuple))
        for test in entries
        if isinstance(test, str)
    )
    return CollectModel._is_test_path(path, test_paths)


def _table_metrics(payload: dict) -> "Dict[str, Any]":
    modules = [m for m in payload.get("modules") or [] if isinstance(m, dict)]
    module_paths = {m["path"] for m in modules if isinstance(m.get("path"), str)}

    guarded_by_location: Dict[str, set] = {}
    guarded_records = 0
    modules_with_guarded = set()
    for m in modules:
        for g in m.get("guarded_accesses") or []:
            if not isinstance(g, dict):
                continue
            guarded_records += 1
            location = g.get("location")
            if isinstance(location, str) and location:
                guarded_by_location.setdefault(location, set()).add(g.get("status"))
            if g.get("status") == "GUARDED":
                modules_with_guarded.add(m.get("path"))

    all_guarded = sum(1 for statuses in guarded_by_location.values() if statuses == {"GUARDED"})
    all_unguarded = sum(1 for statuses in guarded_by_location.values() if statuses == {"UNGUARDED"})
    mixed = len(guarded_by_location) - all_guarded - all_unguarded

    with_purpose = 0
    without_purpose = 0
    without_purpose_tests = 0
    for m in modules:
        summary = m.get("summary")
        purpose = summary.get("purpose") if isinstance(summary, dict) else None
        if isinstance(purpose, str) and purpose.strip():
            with_purpose += 1
        else:
            without_purpose += 1
            if _test_path_is_test(payload, m.get("path", "")):
                without_purpose_tests += 1

    test_map = payload.get("test_map")
    test_map = test_map if isinstance(test_map, dict) else {}
    # An entry can be recorded with nothing covering it — `zero_coverage` is
    # exactly those — so "covered" means an entry with at least one test file,
    # not merely a key. 301 entries, 78 of them actually covering something.
    covered = len({k for k, v in test_map.items() if isinstance(k, str) and v} & module_paths)

    fail_open = [e for e in payload.get("fail_open_registry") or [] if isinstance(e, dict)]
    return {
        "import_edges": _count_table(payload, "import_edges"),
        "imported_by": _count_table(payload, "imported_by"),
        "entry_points": _count_table(payload, "entry_points"),
        "sibling_gaps": _count_table(payload, "sibling_gaps"),
        "test_map_entries": _count_table(payload, "test_map"),
        "test_map_modules_with_entry": len({k for k in test_map if isinstance(k, str)} & module_paths),
        "test_map_modules_covered": covered,
        "zero_coverage": _count_table(payload, "zero_coverage"),
        "thin_coverage": _count_table(payload, "thin_coverage"),
        "risk_index": _count_table(payload, "risk_index"),
        "config_map": _count_table(payload, "config_map"),
        "contracts": _count_table(payload, "contracts"),
        "gates": _count_table(payload, "gates"),
        "fail_open_registry": len(fail_open),
        "fail_open_with_rationale": sum(
            1 for e in fail_open if isinstance(e.get("rationale"), str) and e["rationale"].strip()
        ),
        "guarded_accesses_records": guarded_records,
        "guarded_accesses_locations": len(guarded_by_location),
        "guarded_accesses_all_guarded": all_guarded,
        "guarded_accesses_mixed": mixed,
        "guarded_accesses_all_unguarded": all_unguarded,
        "modules_with_guarded_access": len(modules_with_guarded),
        "except_sites": sum(len(m.get("except_sites") or []) for m in modules),
        "summaries_purpose_present": with_purpose,
        "summaries_purpose_empty": without_purpose,
        "summaries_purpose_empty_tests": without_purpose_tests,
    }


def _rendered(model: CollectModel, path: str, budget: Optional[int]) -> str:
    """The real renderer, guarded: one broken module is one absent block, never
    a missing baseline."""
    try:
        return build_collect_context_block(model, path, budget=budget) or ""
    except Exception:  # noqa: BLE001
        return ""


def _kind_of(line: str) -> Optional[str]:
    for prefix, kind in ROW_KIND_BY_PREFIX:
        if line.startswith(prefix):
            return kind
    return None


def _symbols_listed(block: str) -> int:
    for line in block.split("\n"):
        if line.startswith("public_symbols: "):
            body = line[len("public_symbols: "):]
            match = _SYMBOLS_CUT_NOTE_RE.search(body)
            if match:
                body = body[: match.start()]
            return sum(1 for part in body.split(",") if part.strip())
    return 0


def _symbols_announced(block: str) -> int:
    for line in block.split("\n"):
        if line.startswith("public_symbols: "):
            match = _SYMBOLS_CUT_NOTE_RE.search(line)
            return int(match.group(1)) if match else 0
    return 0


def _config_read_lines(module) -> "list[str]":
    """`_row_config_read`'s lines, verbatim — the same format the renderer
    emits, from the same facts, so the duplicate count matches the count of
    lines the renderer's dedupe pass throws away."""
    return [
        f"config_read [{cr.section}] {cr.key}"
        f"{' (mode-override)' if cr.has_mode_override else ''}"
        f" (fallback={cr.fallback!r})"
        for cr in module.config_reads
    ]


def _block_metrics(model: CollectModel, max_context_chars: int) -> "Dict[str, Any]":
    """The per-task block, rendered once per module with no budget — i.e. the
    raw block `CollectBridge.context_for` hands to `_shrink` today.

    A budgeted render cannot report an overshoot: `build_collect_context_block`
    enforces `budget=` by skipping a row that does not fit, so with a budget
    every block fits by construction. The "over budget" line is therefore
    counted on the unbudgeted render, which is the number that decides how often
    `_shrink` fires; the budgeted pair is kept so the budget's own effect is
    visible in the same table.
    """
    paths = [m.path for m in model.modules]
    raw_blocks = [_rendered(model, path, None) for path in paths]
    budgeted_blocks = [_rendered(model, path, max_context_chars) for path in paths]

    non_empty = [b for b in raw_blocks if b]
    lengths = [len(b) for b in non_empty]
    budgeted_lengths = [len(b) for b in budgeted_blocks if b]
    over_budget = sum(1 for b in raw_blocks if len(b) > max_context_chars)

    duplicate_config_read = 0
    duplicate_config_read_modules = 0
    new_rows = 0
    unknown_rows = 0
    silently_cut = 0
    silently_cut_modules = 0
    announced_cut = 0
    announced_cut_modules = 0

    for block, module in zip(raw_blocks, model.modules):
        if not block:
            continue
        # Counted on the module's own read sites, not on the rendered block:
        # the renderer's V2.4 dedupe pass already throws these lines away, so a
        # rendered block can never show its own duplicates. 24 across 11
        # modules — the same fact, read where it is still visible.
        config_lines = _config_read_lines(module)
        dup = len(config_lines) - len(set(config_lines))
        if dup > 0:
            duplicate_config_read += dup
            duplicate_config_read_modules += 1
        for line in block.split("\n"):
            kind = _kind_of(line)
            if kind is None:
                if line.strip() and not line.startswith("COLLECT MODEL"):
                    unknown_rows += 1
                continue
            if ROW_KIND_CLASS[kind] == "new":
                new_rows += 1
            if kind == "public_symbols":
                total = len(module.public_symbols)
                listed, remainder = _symbols_listed(block), _symbols_announced(block)
                cut = total - listed - remainder
                if cut > 0:
                    silently_cut += cut
                    silently_cut_modules += 1
                if remainder > 0:
                    announced_cut += remainder
                    announced_cut_modules += 1

    return {
        "blocks_asked": len(paths),
        "blocks_non_empty": len(non_empty),
        "blocks_chars_median": int(statistics.median(lengths)) if lengths else 0,
        "blocks_over_budget": over_budget,
        "blocks_over_budget_pct": round(100.0 * over_budget / len(paths), 1) if paths else 0.0,
        "blocks_rows_new": new_rows,
        "blocks_rows_new_mean": round(new_rows / len(non_empty), 3) if non_empty else 0.0,
        "blocks_rows_unknown": unknown_rows,
        "config_read_duplicate_lines": duplicate_config_read,
        "config_read_duplicate_modules": duplicate_config_read_modules,
        "symbols_silently_cut": silently_cut,
        "symbols_silently_cut_modules": silently_cut_modules,
        "symbols_cut_announced": announced_cut,
        "symbols_cut_announced_modules": announced_cut_modules,
        "blocks_budgeted_non_empty": len(budgeted_lengths),
        "blocks_budgeted_chars_median": int(statistics.median(budgeted_lengths)) if budgeted_lengths else 0,
        "max_context_chars": int(max_context_chars),
    }


def load_model(collect_dir: Path) -> CollectModel:
    """The renderer's model, read from the artifact alone — no repo scan."""
    return _load_from_dir(Path(collect_dir), status=STATUS_FRESH)


def compute_metrics(
    payload: dict, meta: dict, *, max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS
) -> dict:
    """Every M1 metric in one flat dict — the shape `--json` writes.

    `meta["collect_dir"]` names the directory the loader re-reads for the block
    section; the table sections read `payload` directly."""
    metrics: Dict[str, Any] = {
        "schema": "collect-metrics-m1",
        "collector_version": meta["collector_version"],
    }
    metrics.update(_artifact_metrics(payload, meta))
    metrics.update(_table_metrics(payload))
    metrics.update(_block_metrics(load_model(meta["collect_dir"]), max_context_chars))
    return metrics


# ── presentation ─────────────────────────────────────────────────────────────

# The table, in the ticket's shape. Values marked with `/` are display-only
# compounds (the flat JSON keeps the two keys separate).
_ROWS = (
    ("artifact", [
        "modules", "artifact_bytes", "public_symbols", "public_symbols_unique_qualnames",
        "signatures_elided_total", "methods_indexed",
    ]),
    ("tables and their reach", [
        "import_edges_imported_by", "entry_points", "sibling_gaps", "test_map_entries",
        "test_map_modules_with_entry", "test_map_modules_covered", "zero_coverage", "thin_coverage",
        "risk_index", "config_map", "contracts", "gates", "fail_open_registry",
        "fail_open_with_rationale", "guarded_accesses_records", "guarded_accesses_locations",
        "guarded_accesses_all_guarded", "guarded_accesses_mixed",
        "guarded_accesses_all_unguarded", "modules_with_guarded_access",
        "except_sites", "summaries_purpose_present", "summaries_purpose_empty",
        "summaries_purpose_empty_tests",
    ]),
    ("per-task block, as rendered today", [
        "blocks_non_empty", "blocks_asked", "blocks_chars_median", "blocks_over_budget",
        "blocks_budgeted_non_empty", "blocks_budgeted_chars_median", "blocks_rows_new_mean",
        "blocks_rows_new", "config_read_duplicate_lines", "config_read_duplicate_modules",
        "symbols_silently_cut", "symbols_silently_cut_modules", "symbols_cut_announced",
        "symbols_cut_announced_modules",
    ]),
)

_LABEL_WIDTH = 46


def _display_values(metrics: dict) -> "Dict[str, str]":
    """Compound display values for the table only; `--json` stays flat."""
    return {
        "signatures_elided_total": f"{metrics.get('signatures_elided', 0)} / {metrics.get('signatures_total', 0)}",
        "import_edges_imported_by": f"{metrics.get('import_edges', 0)} / {metrics.get('imported_by', 0)}",
        "test_map_modules_covered": f"{metrics.get('test_map_modules_covered', 0)} / {metrics.get('modules', 0)}",
        "modules_with_guarded_access": f"{metrics.get('modules_with_guarded_access', 0)} / {metrics.get('modules', 0)}",
        "summaries_purpose_present": f"{metrics.get('summaries_purpose_present', 0)} / {metrics.get('modules', 0)}",
        "blocks_over_budget": f"{metrics.get('blocks_over_budget', 0)}  ({metrics.get('blocks_over_budget_pct', 0):g}%)",
    }


def render_table(metrics: dict, meta: dict) -> str:
    """The human table on stdout.

    The header states the collect dir, the artifact's collector version and its
    mtime — exactly what prevents the 469-vs-483 ambiguity from recurring
    silently. Labels are padded to one width so two tables read side by side.
    """
    shown = dict(metrics)
    shown.update(_display_values(metrics))
    lines = [
        f"collect dir        {meta['collect_dir']}",
        f"collector_version  {meta['collector_version']}   ({meta['collector_version_source']})",
        f"artifact           {meta['artifact_bytes']:,} bytes   mtime {meta['artifact_mtime']}",
        "",
    ]
    for title, keys in _ROWS:
        lines.append(title)
        for key in keys:
            value = shown.get(key, "—")
            if isinstance(value, int) and "bytes" in key:
                value = f"{value:,}".replace(",", " ")
            elif isinstance(value, float):
                value = f"{value:g}"
            lines.append(f"  {key:<{_LABEL_WIDTH}}{value}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_json(metrics: dict, path: str) -> None:
    """`--json`: flat dict, sorted keys, no clock value, trailing newline.

    The sort order *is* the determinism contract — two runs over the same
    collect dir produce byte-identical output, so a later epic's baseline is a
    plain key-by-key diff.
    """
    text = json.dumps(metrics, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure one .collect/ artifact: a human table on stdout, flat JSON for diffing.",
    )
    parser.add_argument("--collect-dir", required=True, help="the .collect/ directory to measure")
    parser.add_argument("--json", metavar="PATH", help="write the flat metrics dict to PATH")
    parser.add_argument(
        "--max-context-chars",
        type=int,
        default=DEFAULT_MAX_CONTEXT_CHARS,
        help="the per-task budget, [collect] max_context_chars_auto (default %(default)s)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        payload, meta = read_collect_dir(Path(args.collect_dir))
    except CollectMetricsError as exc:
        print(f"collect_metrics: {exc}", file=sys.stderr)
        return 1

    metrics = compute_metrics(payload, meta, max_context_chars=max(0, args.max_context_chars))
    sys.stdout.write(render_table(metrics, meta))

    if args.json:
        try:
            write_json(metrics, args.json)
        except OSError as exc:
            print(f"collect_metrics: could not write {args.json}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
