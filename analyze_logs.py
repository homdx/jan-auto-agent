#!/usr/bin/env python3
"""
analyze_logs.py — Human-readable analytics for agent trace (.jsonl) logs.

Usage:
    python analyze_logs.py <trace_file.jsonl>
    python analyze_logs.py <trace_file.jsonl> --run-id abc123
    python analyze_logs.py .agent/              # auto-finds newest trace
    python analyze_logs.py .agent/ --all-runs   # show all runs in dir

What it shows:
    • Summary: total tasks, iterations, approve/reject counts, prompt changes
    • Applied tasks: every completed task with commit hash and iteration count
    • Per-task breakdown: status, iteration count, approve/reject per task
    • Prompt changes: when, which agent, old→new diff
    • Timeline: human-readable event flow
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── ANSI colours ─────────────────────────────────────────────────────────────

USE_COLOR = sys.stdout.isatty()

COLORS = {
    "reset":   "\033[0m",
    "bold":    "\033[1m",
    "dim":     "\033[2m",
    "red":     "\033[31m",
    "green":   "\033[32m",
    "yellow":  "\033[33m",
    "blue":    "\033[34m",
    "magenta": "\033[35m",
    "cyan":    "\033[36m",
    "white":   "\033[37m",
}

def c(name: str) -> str:
    return COLORS.get(name, "") if USE_COLOR else ""

def bold(s: str) -> str:    return f"{c('bold')}{s}{c('reset')}"
def dim(s: str) -> str:     return f"{c('dim')}{s}{c('reset')}"
def green(s: str) -> str:   return f"{c('green')}{s}{c('reset')}"
def red(s: str) -> str:     return f"{c('red')}{s}{c('reset')}"
def yellow(s: str) -> str:  return f"{c('yellow')}{s}{c('reset')}"
def cyan(s: str) -> str:    return f"{c('cyan')}{s}{c('reset')}"
def magenta(s: str) -> str: return f"{c('magenta')}{s}{c('reset')}"


# ── Loading ───────────────────────────────────────────────────────────────────

def find_trace_files(path: str) -> list[Path]:
    """Return trace .jsonl files from a path (file or directory)."""
    p = Path(path)
    if p.is_file():
        return [p]
    if p.is_dir():
        files = sorted(p.glob("trace_*.jsonl"), key=lambda f: f.stat().st_mtime)
        if not files:
            # also try .agent/ subdir
            agent = p / ".agent"
            if agent.is_dir():
                files = sorted(agent.glob("trace_*.jsonl"), key=lambda f: f.stat().st_mtime)
        return files
    print(f"  [!] Path not found: {path}", file=sys.stderr)
    return []


def load_events(path: Path) -> list[dict]:
    events = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  [!] Line {lineno}: JSON parse error — {e}", file=sys.stderr)
    return events


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def fmt_ts(ts: str) -> str:
    """Format ISO timestamp to human-readable short form."""
    if not ts:
        return "?"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ts[:19]


def elapsed(ts_start: str, ts_end: str) -> str:
    """Return human-readable elapsed time between two ISO timestamps."""
    if not ts_start or not ts_end:
        return ""
    try:
        a = datetime.fromisoformat(ts_start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ts_end.replace("Z", "+00:00"))
        secs = int((b - a).total_seconds())
        if secs < 60:
            return f"{secs}s"
        m, s = divmod(secs, 60)
        return f"{m}m{s:02d}s"
    except ValueError:
        return ""


# ── Validator verdict helper ──────────────────────────────────────────────────

def _parse_validator_verdict(content) -> Optional[bool]:
    """
    Parse a validator result event's content and return:
        True  — approved
        False — rejected
        None  — could not determine (treat as rejected, don't count)

    validator_agent emits kind="result" with content = a dict (serialised to a
    JSON string by the tracer).  The dict is either:
        {"approved": true|false, "feedback": "...", ...}   ← autonomous mode
        {"status": "approved"|"needs_fix", ...}            ← interactive mode
        {"status": "APPROVED"|"REJECTED", ...}             ← plain-text verdict
    """
    if content is None:
        return None

    # Tracer._truncate converts dicts to JSON strings before writing to JSONL.
    # json.loads() on the event record gives us the string back, so we re-parse it.
    data: dict | None = None
    if isinstance(content, dict):
        data = content
    elif isinstance(content, str):
        # Skip truncation marker — can't parse a cut-off JSON blob
        if "…[+" in content:
            return None
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                data = parsed
            else:
                # Bare string verdict e.g. "APPROVED" / "REJECTED"
                upper = str(parsed).strip().upper()
                return upper in ("APPROVED", "PASS", "OK", "YES")
        except (json.JSONDecodeError, ValueError):
            # Plain text fallback: "APPROVED" / "REJECTED: ..."
            upper = content.strip().upper()
            if upper.startswith("APPROVED") or upper in ("PASS", "OK", "YES"):
                return True
            if upper.startswith("REJECTED") or upper in ("FAIL", "NO", "BLOCKED"):
                return False
            return None

    if data is None:
        return None

    # Auto mode: {"approved": true, ...}
    if "approved" in data:
        return bool(data["approved"])

    # Interactive mode: {"status": "approved" | "needs_fix" | ...}
    status = str(data.get("status", "")).strip().lower()
    if status in ("approved", "pass", "ok"):
        return True
    if status in ("needs_fix", "rejected", "fail", "blocked"):
        return False

    return None


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyze(events: list[dict], run_id_filter: Optional[str] = None) -> dict:
    """
    Walk events and build an analytics structure:
    {
        runs: {run_id: { tasks, events, prompt_changes, start_ts, end_ts }},
    }
    """
    if run_id_filter:
        events = [e for e in events if e.get("run_id") == run_id_filter]

    runs: dict[str, dict] = {}

    def get_run(rid: str) -> dict:
        if rid not in runs:
            runs[rid] = {
                "run_id":         rid,
                "start_ts":       None,
                "end_ts":         None,
                "stop_reason":    None,
                "goal":           None,
                "tasks":          {},        # task_id -> task_info
                "prompt_changes": [],
                "events":         [],
                "llm_calls":      0,
                "total_events":   0,
                # Internal tracking — not rendered directly
                "_current_task":  None,      # task_id of the task currently in the loop
            }
        return runs[rid]

    for evt in events:
        rid     = evt.get("run_id") or "__ungrouped__"
        run     = get_run(rid)
        kind    = evt.get("kind", "")
        src     = evt.get("source", "")
        tgt     = evt.get("target", "")
        ts      = evt.get("ts", "")
        params  = evt.get("params") or {}
        content = evt.get("content", "")

        run["events"].append(evt)
        run["total_events"] += 1

        # ── run lifecycle ──────────────────────────────────────────────────
        if kind == "run_start":
            # FIX: outer_loop.py also emits kind="run_start" once per task with
            # params like {"task": task_id, "start_round": ...} — no "goal" key.
            # Only treat this as a real run-level start when the params carry a
            # "goal" or "prompt" key, so per-task run_start events don't clobber
            # the run's goal or timestamp.
            has_goal = bool(params.get("goal") or params.get("prompt"))
            if has_goal or run["start_ts"] is None:
                if run["start_ts"] is None:
                    run["start_ts"] = ts
                if has_goal and not run["goal"]:
                    run["goal"] = params.get("goal") or params.get("prompt", "")

        elif kind in ("run_finished", "run_capped"):
            run["end_ts"] = ts
            run["stop_reason"] = params.get("stop_reason")

        # ── task lifecycle ─────────────────────────────────────────────────
        elif kind == "call" and tgt == "outer_loop":
            # run_trace.log_task_start() → source="controller", target="outer_loop",
            # kind="call", params={"task_id": ..., "title": ...}
            task_id = params.get("task_id", "?")
            title   = params.get("title", "")
            task = run["tasks"].setdefault(task_id, {
                "task_id":    task_id,
                "title":      title,
                "start_ts":   ts,
                "end_ts":     None,
                "status":     "in_progress",
                "iterations": 0,
                "approved":   0,
                "rejected":   0,
                "commit":     None,
            })
            task["start_ts"] = ts
            task["title"]    = task["title"] or title
            task["status"]   = "in_progress"
            # Track which task is currently in the loop for validator association.
            run["_current_task"] = task_id

        elif kind == "result" and src == "outer_loop":
            # run_trace.log_task_done() → params={"task_id": ..., "commit": ...}, content="DONE"
            # outer_loop.py result    → params={"task": ..., "passed": True/False, ...}
            # Handle both param key names.
            task_id = params.get("task_id") or params.get("task", "?")
            if task_id == "?":
                # Skip malformed / internal outer_loop events with no identifiable task.
                continue
            task = run["tasks"].setdefault(task_id, {"task_id": task_id,
                                                      "status": "in_progress",
                                                      "iterations": 0,
                                                      "approved": 0,
                                                      "rejected": 0,
                                                      "commit": None})
            task["end_ts"] = ts
            # Determine status: prefer content string, fall back to params["passed"].
            if content:
                task["status"] = str(content).strip().upper()
            elif "passed" in params:
                task["status"] = "DONE" if params["passed"] else "BLOCKED"
            else:
                task["status"] = "DONE"

            if params.get("commit"):
                task["commit"] = params["commit"]
            # Clear current task tracker when done.
            if run["_current_task"] == task_id:
                run["_current_task"] = None

        elif kind == "decision" and src == "outer_loop":
            # run_trace.log_task_blocked() → params={"task_id": ..., "reason": ...}, content="BLOCKED"
            task_id = params.get("task_id") or params.get("task", "?")
            if task_id == "?":
                continue
            task = run["tasks"].setdefault(task_id, {"task_id": task_id,
                                                      "status": "in_progress",
                                                      "iterations": 0,
                                                      "approved": 0,
                                                      "rejected": 0,
                                                      "commit": None})
            task["end_ts"] = ts
            task["status"] = str(content).strip().upper() if content else "BLOCKED"
            if run["_current_task"] == task_id:
                run["_current_task"] = None

        # Gate-1 rejection (pre-execution filter)
        elif kind == "rejected":
            g1_key = f"gate1:{params.get('title', '?')}"
            run["tasks"].setdefault(g1_key, {
                "task_id":    g1_key,
                "title":      params.get("title", "?"),
                "start_ts":   ts,
                "end_ts":     ts,
                "status":     "GATE1_REJECTED",
                "iterations": 0,
                "approved":   0,
                "rejected":   1,
                "reason":     params.get("reason", ""),
                "commit":     None,
            })

        # ── validator decisions ────────────────────────────────────────────
        #
        # FIX: validator_agent emits kind="result" (NOT kind="decision").
        # The content is a JSON-encoded dict, not a plain "APPROVED"/"REJECTED"
        # string, so it must be parsed via _parse_validator_verdict().
        elif kind == "result" and "validator" in src:
            # Associate with the tracked current task first; fall back to
            # heuristic scan of in-progress tasks if the tracker is empty.
            current_id = run.get("_current_task")
            if current_id and current_id in run["tasks"]:
                target_task = run["tasks"][current_id]
            else:
                active = [t for t in run["tasks"].values()
                          if t.get("status") == "in_progress"]
                target_task = active[-1] if active else None

            if target_task:
                target_task["iterations"] = target_task.get("iterations", 0) + 1
                verdict = _parse_validator_verdict(content)
                if verdict is True:
                    target_task["approved"] = target_task.get("approved", 0) + 1
                elif verdict is False:
                    target_task["rejected"] = target_task.get("rejected", 0) + 1
                # verdict is None → count the iteration but don't skew either bucket

        # ── llm calls ─────────────────────────────────────────────────────
        elif kind == "llm_request":
            run["llm_calls"] += 1

        # ── prompt changes ─────────────────────────────────────────────────
        elif kind in ("prompt_updated", "prompt_push", "prompt_change"):
            agent_name = params.get("agent") or params.get("agent_name") or src
            old_prompt = params.get("old_prompt") or params.get("before", "")
            new_prompt = params.get("new_prompt") or params.get("after") or str(content or "")
            run["prompt_changes"].append({
                "ts":         ts,
                "agent":      agent_name,
                "old_prompt": old_prompt,
                "new_prompt": new_prompt,
                "run_id":     rid,
            })

    return runs


# ── Prompt diff renderer ───────────────────────────────────────────────────────

def render_prompt_diff(old: str, new: str, context: int = 3) -> str:
    """Render a unified-diff style comparison between old and new prompt."""
    if not old and not new:
        return dim("  (no content)")
    if not old:
        return green("  (new prompt — no previous version)")
    if not new:
        return red("  (prompt removed)")
    if old == new:
        return dim("  (identical — no change)")

    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="before", tofile="after",
        n=context,
    ))
    if not diff:
        return dim("  (no textual difference)")

    output = []
    for line in diff:
        line = line.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            output.append(bold(dim(line)))
        elif line.startswith("@@"):
            output.append(cyan(line))
        elif line.startswith("+"):
            output.append(green(line))
        elif line.startswith("-"):
            output.append(red(line))
        else:
            output.append(dim(line))
    return "\n".join(output)


def truncate(s: str, n: int = 120) -> str:
    s = str(s or "").strip()
    if len(s) <= n:
        return s
    return s[:n] + dim(f"…[+{len(s)-n}]")


# ── Renderers ─────────────────────────────────────────────────────────────────

def print_header(title: str) -> None:
    width = 72
    print()
    print(bold(cyan("═" * width)))
    print(bold(cyan(f"  {title}")))
    print(bold(cyan("═" * width)))


def print_section(title: str) -> None:
    print()
    print(bold(f"── {title} " + "─" * max(0, 60 - len(title))))


def render_run_summary(run: dict) -> None:
    tasks  = run["tasks"]
    real   = {k: v for k, v in tasks.items() if not k.startswith("gate1:")}
    gate1r = {k: v for k, v in tasks.items() if k.startswith("gate1:")}

    done_tasks    = [t for t in real.values() if t.get("status") in ("DONE", "APPROVED", "PASS")]
    blocked_tasks = [t for t in real.values() if t.get("status") in ("BLOCKED", "FAIL")]
    other_tasks   = [t for t in real.values() if t not in done_tasks and t not in blocked_tasks]

    total_iters    = sum(t.get("iterations", 0) for t in real.values())
    total_approved = sum(t.get("approved", 0) for t in real.values())
    total_rejected = sum(t.get("rejected", 0) for t in real.values())
    prompt_changes = len(run["prompt_changes"])

    dur = elapsed(run.get("start_ts", ""), run.get("end_ts", ""))

    print_section("RUN SUMMARY")

    print(f"  {bold('Run ID')}:          {cyan(run['run_id'])}")
    if run.get("goal"):
        print(f"  {bold('Goal')}:           {truncate(run['goal'], 80)}")
    if run.get("start_ts"):
        ts_str = fmt_ts(run["start_ts"])
        print(f"  {bold('Started')}:        {ts_str}" + (f"  (duration: {dur})" if dur else ""))
    if run.get("stop_reason"):
        print(f"  {bold('Stop reason')}:    {yellow(run['stop_reason'])}")

    print()
    print(f"  {bold('Tasks')}:           total={bold(str(len(real)))}  "
          f"done={green(str(len(done_tasks)))}  "
          f"blocked={red(str(len(blocked_tasks)))}  "
          f"other={str(len(other_tasks))}")
    if gate1r:
        print(f"  {bold('Gate-1 rejected')}: {yellow(str(len(gate1r)))} tasks filtered before execution")
    print(f"  {bold('Iterations')}:      total={bold(str(total_iters))}  "
          f"approved={green(str(total_approved))}  "
          f"rejected={red(str(total_rejected))}")
    print(f"  {bold('LLM calls')}:       {run['llm_calls']}")
    print(f"  {bold('Prompt changes')}: {magenta(str(prompt_changes))}")
    print(f"  {bold('Total events')}:    {run['total_events']}")


def render_applied_tasks(run: dict) -> None:
    """Show every completed task — the things that were actually applied/done."""
    tasks = run["tasks"]
    done = [
        v for k, v in tasks.items()
        if not k.startswith("gate1:")
        and v.get("status") in ("DONE", "APPROVED", "PASS")
    ]

    print_section(f"APPLIED / COMPLETED TASKS  ({len(done)} total)")

    if not done:
        print(dim("  (no completed tasks recorded in this run)"))
        return

    for t in done:
        title    = t.get("title") or t.get("task_id", "?")
        task_id  = t.get("task_id", "?")
        commit   = t.get("commit") or ""
        iters    = t.get("iterations", 0)
        approved = t.get("approved", 0)
        rejected = t.get("rejected", 0)
        dur      = elapsed(t.get("start_ts", ""), t.get("end_ts", ""))

        print()
        print(f"  {green('✓')} {bold(truncate(title, 65))}")
        print(f"    {dim('id:')}       {dim(task_id)}")
        if commit:
            print(f"    {dim('commit:')}   {cyan(commit[:12])}")
        if dur:
            print(f"    {dim('duration:')} {dur}")
        if iters:
            attempt_str = (
                f"  ({green(str(approved))} approved"
                f" / {red(str(rejected))} rejected)"
            )
            print(f"    {dim('attempts:')} {iters}{attempt_str}")


def render_tasks(run: dict) -> None:
    tasks = run["tasks"]
    if not tasks:
        print(dim("  (no tasks recorded)"))
        return

    print_section("ALL TASKS")

    real  = [(k, v) for k, v in tasks.items() if not k.startswith("gate1:")]
    gate1 = [(k, v) for k, v in tasks.items() if k.startswith("gate1:")]

    for _, t in real:
        status = t.get("status", "?")
        if status in ("DONE", "APPROVED", "PASS"):
            status_str = green(f"✓ {status}")
        elif status in ("BLOCKED", "FAIL"):
            status_str = red(f"✗ {status}")
        elif status == "in_progress":
            status_str = yellow("◌ IN_PROGRESS")
        else:
            status_str = yellow(f"● {status}")

        iters    = t.get("iterations", 0)
        approved = t.get("approved", 0)
        rejected = t.get("rejected", 0)
        title    = truncate(t.get("title", t.get("task_id", "?")), 55)
        dur      = elapsed(t.get("start_ts", ""), t.get("end_ts", ""))
        dur_str  = f"  {dim(dur)}" if dur else ""

        iter_str = ""
        if iters or approved or rejected:
            iter_str = (f"  [{dim('iterations:')} {iters}  "
                        f"{green('✓')} {approved}  "
                        f"{red('✗')} {rejected}]")

        commit_str = ""
        if t.get("commit"):
            commit_str = f"  {dim('commit:')} {dim(t['commit'][:8])}"

        task_id_str = dim(f"[{t.get('task_id', '?')}]")
        print(f"  {status_str:<30} {title}{dur_str}{iter_str}{commit_str}")
        print(f"    {task_id_str}  started: {fmt_ts(t.get('start_ts',''))}")

    if gate1:
        print()
        print(f"  {bold(yellow('Gate-1 Rejections'))} (filtered before execution):")
        for _, t in gate1:
            reason = truncate(t.get("reason", ""), 70)
            print(f"    {red('✗')} {t.get('title', '?')}  —  {dim(reason)}")


def render_prompt_changes(run: dict) -> None:
    changes = run["prompt_changes"]
    if not changes:
        print_section("PROMPT CHANGES")
        print(dim("  (no prompt changes recorded in this run)"))
        return

    print_section(f"PROMPT CHANGES  ({len(changes)} total)")

    for i, ch in enumerate(changes, 1):
        print()
        print(f"  {bold(magenta(f'Change #{i}'))}  —  agent: {cyan(ch.get('agent', '?'))}  —  {fmt_ts(ch.get('ts', ''))}")
        diff_text = render_prompt_diff(ch.get("old_prompt", ""), ch.get("new_prompt", ""))
        for line in diff_text.splitlines():
            print(f"    {line}")


def render_timeline(run: dict, max_events: int = 40) -> None:
    events = run["events"]
    if not events:
        return

    INTERESTING = {
        "run_start", "run_finished", "run_capped",
        "call", "result", "decision", "error",
        "prompt_updated", "prompt_push", "prompt_change",
        "rejected", "phase_transition",
    }
    shown = [e for e in events if e.get("kind") in INTERESTING]

    if not shown:
        return

    print_section(f"TIMELINE  (showing {min(len(shown), max_events)} of {len(shown)} key events)")

    for evt in shown[-max_events:]:
        kind    = evt.get("kind", "?")
        src     = evt.get("source", "?")
        tgt     = evt.get("target", "?")
        ts      = fmt_ts(evt.get("ts", ""))
        params  = evt.get("params") or {}
        content = str(evt.get("content") or "").strip()

        if kind == "run_start" and (params.get("goal") or params.get("prompt")):
            goal = truncate(params.get("goal") or params.get("prompt", ""), 60)
            print(f"  {dim(ts)}  {bold(cyan('▶ RUN START'))}  {dim(goal)}")

        elif kind in ("run_finished", "run_capped"):
            reason = params.get("stop_reason", "")
            label  = "■ RUN CAPPED" if kind == "run_capped" else "■ RUN FINISHED"
            col    = yellow(bold(label)) if kind == "run_capped" else green(bold(label))
            print(f"  {dim(ts)}  {col}" + (f"  {yellow(reason)}" if reason else ""))

        elif kind == "call" and tgt == "outer_loop":
            title = truncate(params.get("title", params.get("task_id", "")), 55)
            print(f"  {dim(ts)}  {bold('→ task start')}  {title}")

        elif kind == "result" and src == "outer_loop":
            verdict  = content or params.get("status", "DONE")
            task_id  = params.get("task_id") or params.get("task", "")
            col      = green if verdict in ("DONE", "APPROVED") else red
            commit   = params.get("commit", "")
            extra    = f"  {dim('commit:')} {dim(commit[:8])}" if commit else ""
            print(f"  {dim(ts)}  {col(bold(f'✓ task {verdict}'))}  {dim(task_id)}{extra}")

        # FIX: validator now correctly emits kind="result" — show it in the timeline
        elif kind == "result" and "validator" in src:
            verdict_bool = _parse_validator_verdict(evt.get("content"))
            if verdict_bool is True:
                verdict_str = green(bold("validator: APPROVED"))
            elif verdict_bool is False:
                verdict_str = red(bold("validator: REJECTED"))
            else:
                verdict_str = yellow(bold("validator: ?"))
            print(f"  {dim(ts)}  {verdict_str}  {dim(src)}")

        elif kind == "decision":
            verdict   = content or ""
            is_reject = verdict.upper() in ("REJECTED", "BLOCKED", "FAIL", "NO")
            col       = red if is_reject else green
            task_id   = params.get("task_id", "")
            extra     = truncate(params.get("reason", ""), 50)
            print(f"  {dim(ts)}  {col(bold(f'decision: {verdict}'))}  {dim(src)} → {dim(tgt)}  {dim(extra)}")

        elif kind == "rejected":
            title  = params.get("title", "?")
            reason = truncate(params.get("reason", ""), 50)
            print(f"  {dim(ts)}  {red('✗ gate-1 reject')}  {title}  {dim(reason)}")

        elif kind in ("prompt_updated", "prompt_push", "prompt_change"):
            agent = params.get("agent") or params.get("agent_name") or src
            print(f"  {dim(ts)}  {magenta(bold('↺ prompt change'))}  agent={cyan(agent)}")

        elif kind == "phase_transition":
            phase  = params.get("phase", "?")
            status = params.get("status", "?")
            print(f"  {dim(ts)}  {bold('phase')} {cyan(phase)} → {status}")

        elif kind == "error":
            msg = truncate(content or str(params), 70)
            print(f"  {dim(ts)}  {red(bold('ERROR'))}  {msg}")


# ── Main render ───────────────────────────────────────────────────────────────

def render_run(run: dict, show_timeline: bool = True, show_diff: bool = True) -> None:
    print_header(f"Run Analysis  [{run['run_id']}]")
    render_run_summary(run)
    render_applied_tasks(run)   # NEW: show what was actually applied/done
    render_tasks(run)
    if show_diff:
        render_prompt_changes(run)
    if show_timeline:
        render_timeline(run)


def render_multi_run_overview(runs: dict) -> None:
    """Show a compact table when multiple runs are present."""
    print_header("Multi-Run Overview")
    print()

    rows = []
    for run in runs.values():
        tasks   = {k: v for k, v in run["tasks"].items() if not k.startswith("gate1:")}
        done    = sum(1 for t in tasks.values() if t.get("status") in ("DONE","APPROVED","PASS"))
        blocked = sum(1 for t in tasks.values() if t.get("status") in ("BLOCKED","FAIL"))
        iters   = sum(t.get("iterations", 0) for t in tasks.values())
        rows.append((run["run_id"], run.get("start_ts",""), len(tasks), done, blocked, iters,
                     len(run["prompt_changes"]), run.get("stop_reason",""), run.get("goal","")))

    hdr = (f"  {'run_id':14}  {'started':19}  "
           f"{'tasks':>5}  {'done':>4}  {'blk':>4}  {'iters':>5}  {'prompts':>7}  goal")
    print(bold(hdr))
    print(dim("  " + "─" * 90))

    for run_id, ts, ntasks, done, blocked, iters, npc, stop, goal in rows:
        done_s    = green(str(done).rjust(4))
        blocked_s = red(str(blocked).rjust(4))
        stop_s    = yellow(f" [{stop}]") if stop else ""
        goal_s    = truncate(goal, 30)
        print(f"  {cyan(run_id):14}  {fmt_ts(ts)}  "
              f"{str(ntasks).rjust(5)}  {done_s}  {blocked_s}  "
              f"{str(iters).rjust(5)}  {magenta(str(npc).rjust(7))}"
              f"{stop_s}  {dim(goal_s)}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Human-readable analytics for agent trace .jsonl logs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("path",
                   help="Path to a .jsonl trace file or a directory containing trace_*.jsonl files")
    p.add_argument("--run-id",
                   metavar="HEX",
                   help="Show only this run_id (12-char hex)")
    p.add_argument("--all-runs",
                   action="store_true",
                   help="When given a directory, analyze ALL trace files (not just the newest)")
    p.add_argument("--no-timeline",
                   action="store_true",
                   help="Skip the event timeline section")
    p.add_argument("--no-diff",
                   action="store_true",
                   help="Skip the prompt diff section")
    p.add_argument("--no-color",
                   action="store_true",
                   help="Disable ANSI colours")
    return p


def main() -> int:
    global USE_COLOR

    parser = build_parser()
    args   = parser.parse_args()

    if args.no_color:
        USE_COLOR = False

    trace_files = find_trace_files(args.path)
    if not trace_files:
        print(f"No trace files found at: {args.path}", file=sys.stderr)
        return 1

    if not args.all_runs:
        trace_files = trace_files[-1:]  # newest only unless --all-runs

    all_events: list[dict] = []
    for tf in trace_files:
        print(dim(f"  Loading: {tf}"), file=sys.stderr)
        all_events.extend(load_events(tf))

    if not all_events:
        print("No events found.", file=sys.stderr)
        return 1

    runs = analyze(all_events, run_id_filter=args.run_id)

    if not runs:
        print("No matching runs found.", file=sys.stderr)
        return 1

    if len(runs) > 1 and not args.run_id:
        render_multi_run_overview(runs)
        print()
        ans = input(bold("Show details for each run? [Y/n] ")).strip().lower()
        if ans in ("n", "no"):
            return 0

    for run in runs.values():
        render_run(
            run,
            show_timeline=not args.no_timeline,
            show_diff=not args.no_diff,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
