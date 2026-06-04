#!/usr/bin/env python3
"""view_trace.py — AUTO-F2: Inspect and replay an autonomous run trace.

Usage
-----
    python view_trace.py .agent/trace_<run_id>.jsonl
    python view_trace.py .agent/trace_<run_id>.jsonl --run-id <hex>
    python view_trace.py .agent/trace_<run_id>.jsonl --filter llm_request llm_response
    python view_trace.py .agent/trace_<run_id>.jsonl --sources architect coder
    python view_trace.py .agent/trace_<run_id>.jsonl --tail 20
    python view_trace.py .agent/  # auto-discovers the newest trace file

Options
-------
--run-id <hex>          Show only events whose run_id matches.
--filter <kind> …       Show only events of these kinds (e.g. llm_request result).
--sources <name> …      Show only events from these sources.
--tail <N>              Show the last N events only.
--no-color              Disable ANSI colours.
--summary               Print a one-line-per-event summary table, then exit.

Output
------
Each event is printed with a compact header (seq · timestamp · source → target [kind])
followed by an indented content/params preview.  The output is designed to let you
reconstruct exactly what each architect / coder / validator / executor exchange
looked like — satisfying the AUTO-F2 AC "a completed run is fully reconstructable
from the trace."
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── ANSI colour helpers ───────────────────────────────────────────────────────

_COLORS = {
    "architect":        "\033[35m",   # magenta
    "coder":            "\033[36m",   # cyan
    "validator":        "\033[33m",   # yellow
    "executor":         "\033[32m",   # green
    "inner_loop":       "\033[34m",   # blue
    "outer_loop":       "\033[34m",
    "controller":       "\033[37m",   # white
    "gate2_validator":  "\033[33m",
    "llm":              "\033[90m",   # dark grey
    "reset":            "\033[0m",
    "dim":              "\033[2m",
    "bold":             "\033[1m",
    "kind_llm_request":  "\033[90m",
    "kind_llm_response": "\033[32m",
    "kind_result":       "\033[32m",
    "kind_decision":     "\033[33m",
    "kind_error":        "\033[31m",
    "kind_run_start":    "\033[36m",
    "kind_run_finished": "\033[36m",
    "kind_run_capped":   "\033[33m",
    "kind_call":         "\033[37m",
}

_NO_COLOR: dict[str, str] = {k: "" for k in _COLORS}


def _c(key: str, use_color: bool) -> str:
    return (_COLORS if use_color else _NO_COLOR).get(key, "")


# ── Event rendering ───────────────────────────────────────────────────────────

_PREVIEW_CHARS = 400


def _preview(text: Any, max_chars: int = _PREVIEW_CHARS) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    s = s.strip()
    if len(s) <= max_chars:
        return s
    half = max_chars // 2
    skipped = len(s) - max_chars
    return s[:half] + f"\n  … [{skipped} chars] …\n" + s[-half:]


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def render_event(evt: dict, use_color: bool = True) -> str:
    C   = lambda k: _c(k, use_color)   # noqa: E731
    rst = C("reset")
    dim = C("dim")
    bld = C("bold")

    seq    = evt.get("seq", "?")
    ts     = evt.get("ts", "")
    src    = evt.get("source", "?")
    tgt    = evt.get("target", "?")
    kind   = evt.get("kind", "?")
    run_id = evt.get("run_id", "")

    sc = C(src)
    tc = C(tgt)
    kc = C(f"kind_{kind}")

    run_tag = f"  {dim}[run:{run_id}]{rst}" if run_id else ""

    header = (
        f"{dim}#{seq:>4}{rst}  "
        f"{dim}{ts}{rst}  "
        f"{bld}{sc}{src}{rst}"
        f"{dim} → {rst}"
        f"{bld}{tc}{tgt}{rst}"
        f"  {kc}[{kind}]{rst}"
        f"{run_tag}"
    )
    lines = [header]

    params  = evt.get("params")
    content = evt.get("content")
    model   = evt.get("model")

    if model:
        lines.append(f"  {dim}model={model}{rst}")

    if kind in ("llm_request",) and content:
        lines.append(f"  {dim}PROMPT:{rst}")
        lines.append(_indent(_preview(content)))
    elif kind in ("llm_response", "result") and content:
        lines.append(f"  {dim}RESPONSE/RESULT:{rst}")
        lines.append(_indent(_preview(content)))
    elif params:
        skip = {"related_code", "target_block", "imports", "context_lines"}
        for k, v in params.items():
            if k in skip:
                continue
            lines.append(f"  {dim}{k}:{rst}")
            lines.append(_indent(_preview(v)))

    return "\n".join(lines)


# ── Summary table ─────────────────────────────────────────────────────────────

def render_summary(events: list[dict], use_color: bool = True) -> str:
    C   = lambda k: _c(k, use_color)   # noqa: E731
    rst = C("reset")
    dim = C("dim")

    rows = []
    for evt in events:
        seq  = str(evt.get("seq", "?")).rjust(4)
        ts   = evt.get("ts", "")[:19]
        src  = (evt.get("source") or "?")[:18].ljust(18)
        tgt  = (evt.get("target") or "?")[:18].ljust(18)
        kind = (evt.get("kind") or "?")[:18].ljust(18)
        rows.append(f"  {dim}#{seq}{rst}  {ts}  {C(evt.get('source',''))}{src}{rst}  → {tgt}  {kind}")

    header = f"  {'seq':>4}  {'timestamp':19}  {'source':18}  → {'target':18}  {'kind':18}"
    sep    = "  " + "-" * 84
    return "\n".join([header, sep] + rows)


# ── File discovery ────────────────────────────────────────────────────────────

def find_trace_file(path_arg: str) -> Path:
    p = Path(path_arg)
    if p.is_file():
        return p
    if p.is_dir():
        candidates = sorted(p.glob("trace_*.jsonl"), key=lambda f: f.stat().st_mtime)
        if not candidates:
            sys.exit(f"No trace_*.jsonl files found in {p}")
        return candidates[-1]
    sys.exit(f"Path not found: {p}")


# ── Loading ───────────────────────────────────────────────────────────────────

def load_events(trace_file: Path) -> list[dict]:
    events = []
    with trace_file.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"  [warn] line {lineno}: {exc}", file=sys.stderr)
    return events


# ── Filtering ─────────────────────────────────────────────────────────────────

def apply_filters(
    events:   list[dict],
    run_id:   Optional[str],
    kinds:    Optional[list[str]],
    sources:  Optional[list[str]],
    tail:     Optional[int],
) -> list[dict]:
    if run_id:
        events = [e for e in events if e.get("run_id") == run_id]
    if kinds:
        kinds_set = set(kinds)
        events = [e for e in events if e.get("kind") in kinds_set]
    if sources:
        src_set = set(sources)
        events = [e for e in events if e.get("source") in src_set]
    if tail and tail > 0:
        events = events[-tail:]
    return events


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Inspect and replay an AUTO-F2 run trace (.jsonl).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("path",            help="Trace file or .agent/ directory")
    p.add_argument("--run-id",        help="Filter to one run_id")
    p.add_argument("--filter",        nargs="+", metavar="KIND",   help="Event kinds to show")
    p.add_argument("--sources",       nargs="+", metavar="SOURCE", help="Sources to show")
    p.add_argument("--tail",          type=int,  metavar="N",      help="Show last N events")
    p.add_argument("--no-color",      action="store_true",         help="Disable ANSI colour")
    p.add_argument("--summary",       action="store_true",         help="One-line-per-event table")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    use_color = not args.no_color and sys.stdout.isatty()

    trace_file = find_trace_file(args.path)
    events     = load_events(trace_file)

    if not events:
        print(f"No events found in {trace_file}", file=sys.stderr)
        return 1

    events = apply_filters(
        events,
        run_id  = args.run_id,
        kinds   = args.filter,
        sources = args.sources,
        tail    = args.tail,
    )

    if not events:
        print("No events match the given filters.", file=sys.stderr)
        return 1

    C   = lambda k: _c(k, use_color)   # noqa: E731
    rst = C("reset")
    dim = C("dim")

    print(f"\n{dim}trace file : {trace_file}{rst}")
    print(f"{dim}events     : {len(events)}{rst}\n")

    if args.summary:
        print(render_summary(events, use_color=use_color))
    else:
        sep = dim + ("─" * 80) + rst
        for evt in events:
            print(sep)
            print(render_event(evt, use_color=use_color))
        print(sep)

    print(f"\n{dim}({len(events)} event(s)){rst}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
