#!/usr/bin/env python3
"""
analyze_logs.py — Human-readable analytics for agent trace (.jsonl) logs.

Usage:
    python analyze_logs.py <trace_file.jsonl>
    python analyze_logs.py <trace_file.jsonl> --run-id abc123
    python analyze_logs.py .agent/              # auto-finds newest trace
    python analyze_logs.py .agent/ --all-runs   # show all runs in dir
    python analyze_logs.py .agent/ --rewrites       # + prompt rewrite report
    python analyze_logs.py .agent/ --rewrites-only  # rewrite report only
    python analyze_logs.py .agent/ --mode creative  # story-progress layout
    python analyze_logs.py .agent/ --mode docs      # documentation-run layout

What it shows:
    • Summary: total tasks, iterations, approve/reject counts, prompt changes
    • Applied tasks: every completed task with commit hash and iteration count
    • Per-task breakdown: status, iteration count, approve/reject per task
    • Prompt changes: when, which agent, old→new diff
    • Prompt rewrite attempts (--rewrites): every auto-tuner candidate, its
      score, and whether it was promoted or denied; promoted attempts show
      the old → new prompt diff
    • Timeline: human-readable event flow

Task modes mirror ``agents.ini [auto] task_mode`` (code | docs | creative —
see AUTO-CR-10 / tools/auto/utils.py normalize_task_mode) so a run analyzed
here always lines up with the mode it was actually generated under:

Creative mode (--mode creative or --mode auto on a creative run):
    • Story Progress: chapters in narrative order with revision counts
    • Per-gate breakdown (Gate-2 LLM / Gate-3 fact / prosody / continuity)
      when those validators emit tracer events
    • Non-chapter tasks shown separately

Docs mode (--mode docs, or --mode auto on a task_mode=docs run) (AUTO-CR-35):
    • Documentation tasks are rendered like code tasks (docs runs use the
      same coder → Gate-2 pipeline, with no Gate-3 prose gates), but the
      section headers and run banner are relabelled so a docs run doesn't
      read like a plain code run.
    • Auto-detection infers "docs" when a majority of a run's tasks touch
      only prose/doc files (.md, .rst, .txt, .adoc, .mdx) and don't look
      like chapter-writing tasks.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


# ── Creative-mode helpers ─────────────────────────────────────────────────────

# Matches "chapter 1", "chapter_02", "Chapter-3", etc. in task titles / IDs.
_CHAPTER_RE = re.compile(r"chapter[\s_\-]?(\d+)", re.IGNORECASE)

# Creative gate sources — these validators run in inner_loop.py but do NOT emit
# tracer events themselves (rejections surface only as extra coder iterations).
# Tracked here for documentation and future tracer integration.
_CREATIVE_GATE_LABELS = {
    "validator_agent":      "Gate-2 (LLM)",
    "fact_validator":       "Gate-3 fact",
    "prosody":              "Gate-3 prosody",
    "continuity_validator": "Gate-3 continuity",
    "story_bible":          "Story bible",
}

def _chapter_num(task: dict) -> int:
    """Extract chapter number from task title or id, or return 9999 for non-chapters."""
    for field in ("title", "task_id"):
        m = _CHAPTER_RE.search(task.get(field, "") or "")
        if m:
            return int(m.group(1))
    return 9999


# ── Docs-mode helpers (AUTO-CR-35) ────────────────────────────────────────────
# Keeps analyze_logs.py's mode support in sync with agents.ini [auto] task_mode
# (code | docs | creative — tools/auto/utils.py normalize_task_mode). Unlike
# creative mode, docs tasks don't emit a distinguishing structural marker like
# a chapter number: the architect/gate1/validator prompts differ (see
# docs/Readme.MD §4 "Task Modes"), but the trace shape is identical to plain
# code tasks (coder → Gate-2, no Gate-3 prose gates). So detection here is a
# best-effort heuristic based on which files a task actually touched.
_DOC_EXTENSIONS = (".md", ".mdx", ".rst", ".txt", ".adoc")


def _is_doc_file(filename: str) -> bool:
    """True if *filename* looks like a prose/documentation file."""
    return filename.lower().endswith(_DOC_EXTENSIONS)


def _task_files_map(run: dict) -> dict:
    """Build ``task_id -> [files touched]`` from files_preparing trace records.

    Uses the most recent record per task (whichever file list was last
    reported for that task's workspace setup).
    """
    files_by_task: dict = {}
    for rec in run.get("files_preparing", []):
        task_id = rec.get("task")
        files = rec.get("files")
        if task_id and files:
            files_by_task[task_id] = files
    return files_by_task


def _detect_task_mode(run: dict) -> str:
    """Infer task mode from trace patterns.

    Returns ``"creative"`` when the majority of tasks look like chapter-writing
    tasks (title or task_id contains a chapter number); ``"docs"`` when the
    majority of the remaining tasks touched only prose/doc files (AUTO-CR-35);
    otherwise falls back to ``"code"``. Requires at least two file-carrying
    tasks before ever returning "docs", so a single incidental README tweak
    inside an otherwise ordinary code run isn't misread as a docs run.
    """
    real_tasks = [v for k, v in run["tasks"].items() if not k.startswith("gate1:")]
    if not real_tasks:
        return "code"

    chapter_tasks = sum(1 for t in real_tasks if _chapter_num(t) < 9999)
    if chapter_tasks > len(real_tasks) / 2:
        return "creative"

    files_by_task = _task_files_map(run)
    doc_tasks = 0
    considered = 0
    for t in real_tasks:
        files = files_by_task.get(t.get("task_id", ""))
        if not files:
            continue
        considered += 1
        if all(_is_doc_file(f) for f in files):
            doc_tasks += 1
    if considered >= 2 and doc_tasks > considered / 2 and doc_tasks > len(real_tasks) / 2:
        return "docs"

    return "code"


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

def _extract_context_signals(src, content) -> list:
    """Pull-model: return the symbol names a coder/validator asked for in an
    ``llm_response``.  The coder embeds a top-level ``context_request`` array and
    the validator a ``missing_context`` array in its JSON response.  Returns an
    empty list when absent or unparseable (so callers can treat it as a flag)."""
    if not content or not isinstance(content, str):
        return []
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    brace = text.find("{")
    if brace > 0:                      # tolerate <think>… preambles
        text = text[brace:]
    try:
        data = json.loads(text)
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    req = data.get("context_request")
    if not isinstance(req, list):
        req = data.get("missing_context")
    if not isinstance(req, list):
        return []
    return [str(x).strip() for x in req if str(x).strip()]


def _extract_current_prompt_from_meta(meta_prompt: str) -> str:
    """
    Pull the old/active prompt text out of PromptOptimizer's meta-prompt.

    tools/prompt_optimizer.py renders OPTIMIZER_META_PROMPT with the agent's
    current prompt embedded between a "CURRENT PROMPT:" and a "FAILURE
    SUMMARY:" marker. The llm_request event traced for that call carries the
    full rendered meta-prompt as its content; this pulls just the embedded
    prompt back out so a rewrite report can show what the optimizer started
    from. Returns "" if the markers aren't found (e.g. unrelated llm_request).
    """
    if not meta_prompt:
        return ""
    start_marker = "CURRENT PROMPT:\n"
    end_marker = "\nFAILURE SUMMARY:"
    if start_marker not in meta_prompt:
        return ""
    after = meta_prompt.split(start_marker, 1)[1]
    if end_marker in after:
        after = after.split(end_marker, 1)[0]
    return after.rstrip("\n")


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
                "rewrite_attempts": [],      # auto-tuner prompt rewrites: score + promoted/denied + old/new prompt
                "events":         [],
                "llm_calls":      0,
                "context_requests": [],
                "total_events":   0,
                "plan_total":     0,         # filled by plan_ready event — total tasks in plan
                "files_preparing": [],       # [{ts, task, file_count, files_copied, files_missing, files}]
                # Internal tracking — not rendered directly
                "_current_task":  None,      # task_id of the task currently in the loop
                "_pending_old_prompt": None, # most recent prompt_optimizer llm_request "CURRENT PROMPT" text
                "_pending_new_prompt": None, # most recent prompt_optimizer llm_response candidate text
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

        # ── plan size (emitted by pipeline.py after _run_plan_phase) ──────────
        elif kind == "plan_ready":
            total = params.get("total_tasks", 0)
            if total:
                run["plan_total"] = int(total)

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
                "stages":     {},   # AUTO-CR-27: per-stage gate counts
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

        # ── AUTO-CR-27: inner-loop per-stage gate decisions ────────────────
        #
        # _trace_stage() in inner_loop.py emits:
        #   kind="decision", target="inner_loop", source=<stage>,
        #   params={"task": id, "attempt": N, "stage": <stage>, ...},
        #   content=REJECTED|APPROVED|EXHAUSTED|ACCEPTED_AT_CAP|ERROR
        #
        # Stages (all modes):  coder, executor, gate2, overall
        # Stages (creative):   canon, fact, continuity, prosody
        #
        # "overall APPROVED"  → the attempt fully passed every gate
        # "overall EXHAUSTED" → all attempts used up, task fails
        # All other stages    → a single gate rejected this attempt
        elif kind == "decision" and tgt == "inner_loop":
            task_id = params.get("task") or params.get("task_id", "?")
            stage   = params.get("stage", src or "?")
            verdict = str(content or "").strip().upper()
            attempt = params.get("attempt", 0)

            # Associate with the current task; fall back to in-progress scan.
            current_id = run.get("_current_task")
            if task_id != "?" and task_id in run["tasks"]:
                target_task = run["tasks"][task_id]
            elif current_id and current_id in run["tasks"]:
                target_task = run["tasks"][current_id]
            else:
                active = [t for t in run["tasks"].values()
                          if t.get("status") == "in_progress"]
                target_task = active[-1] if active else None

            if target_task is None and task_id != "?":
                target_task = run["tasks"].setdefault(
                    task_id, {"task_id": task_id, "status": "in_progress",
                              "iterations": 0, "approved": 0, "rejected": 0,
                              "commit": None, "stages": {}}
                )

            if target_task is not None:
                target_task.setdefault("stages", {})
                stage_counter = target_task["stages"].setdefault(
                    stage, {"REJECTED": 0, "ACCEPTED_AT_CAP": 0,
                            "ERROR": 0, "APPROVED": 0, "EXHAUSTED": 0}
                )
                stage_counter[verdict] = stage_counter.get(verdict, 0) + 1

                # "overall" is the authoritative attempt-level outcome.
                if stage == "overall":
                    target_task["iterations"] = max(
                        target_task.get("iterations", 0), int(attempt)
                    )
                    if verdict == "APPROVED":
                        target_task["approved"] = target_task.get("approved", 0) + 1
                    elif verdict == "EXHAUSTED":
                        target_task["rejected"] = target_task.get("rejected", 0) + 1

        # ── validator decisions ────────────────────────────────────────────
        #
        # FIX: validator_agent emits kind="result" (NOT kind="decision").
        # The content is a JSON-encoded dict, not a plain "APPROVED"/"REJECTED"
        # string, so it must be parsed via _parse_validator_verdict().
        #
        # NOTE: creative-mode gates (fact, prosody, continuity) now ALSO emit
        # kind="decision" target="inner_loop" stage=<gate> via _trace_stage()
        # (AUTO-CR-27).  The block above handles those.  This block remains for
        # the Gate-2 LLM validator (validator_agent) which still emits
        # kind="result" with a JSON payload, and for backward-compat with older
        # traces that pre-date AUTO-CR-27.
        elif kind == "result" and (
            "validator" in src
            or src in ("prosody", "story_bible")  # creative-mode gate sources
        ):
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
                # Per-gate breakdown (useful for creative-mode reports).
                gate_label = _CREATIVE_GATE_LABELS.get(src, "Gate-2 (LLM)")
                gates = target_task.setdefault("gates", {})
                g = gates.setdefault(gate_label, {"approved": 0, "rejected": 0})
                if verdict is True:
                    target_task["approved"] = target_task.get("approved", 0) + 1
                    g["approved"] += 1
                elif verdict is False:
                    target_task["rejected"] = target_task.get("rejected", 0) + 1
                    g["rejected"] += 1
                # verdict is None → count the iteration but don't skew either bucket

        # ── creative-mode detection ────────────────────────────────────────
        # Standalone check (outside the elif chain above) so it always fires
        # regardless of which branch matched.  If any creative-specific gate
        # source appears in this run, tag it as creative.
        if src in _CREATIVE_GATE_LABELS and src != "validator_agent":
            run.setdefault("detected_mode", "creative")

        # ── llm calls ─────────────────────────────────────────────────────
        if kind == "llm_request":
            run["llm_calls"] += 1
            # prompt_optimizer's meta-prompt embeds the CURRENT PROMPT text —
            # stash it so a later prompt_denied/prompt_promoted event (which
            # only carries score/reason) can be paired with the old prompt.
            if src == "prompt_optimizer":
                run["_pending_old_prompt"] = _extract_current_prompt_from_meta(str(content or ""))

        # ── pull-model context requests (coder context_request / validator missing_context) ──
        elif kind == "llm_response":
            _syms = _extract_context_signals(src, content)
            if _syms:
                run["context_requests"].append({"ts": ts, "src": src, "symbols": _syms})
            # The candidate prompt the optimizer's LLM call returned — this is
            # the "new prompt" half of a rewrite attempt.
            if src == "llm" and tgt == "prompt_optimizer":
                run["_pending_new_prompt"] = str(content or "")

        # ── auto-tuner prompt rewrite outcomes (score, promoted or denied) ──
        elif kind in ("prompt_denied", "prompt_promoted"):
            promoted = (kind == "prompt_promoted")
            run["rewrite_attempts"].append({
                "ts":         ts,
                "agent":      params.get("agent") or params.get("agent_name") or "?",
                "score":      params.get("score", 0.0),
                "promoted":   promoted,
                "reason":     str(content or ""),
                "old_prompt": run.get("_pending_old_prompt") or "",
                "new_prompt": run.get("_pending_new_prompt") or "",
                "run_id":     rid,
            })
            # Consumed — don't let a stale pair leak into an unrelated event.
            run["_pending_old_prompt"] = None
            run["_pending_new_prompt"] = None

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

        # ── file preparation phase (executor workspace setup) ──────────────
        elif kind == "phase_transition" and params.get("phase") == "files_preparing":
            status = params.get("status", "")
            task_id_fp = params.get("task", "")
            if status == "started":
                run["files_preparing"].append({
                    "ts":            ts,
                    "task":          task_id_fp,
                    "file_count":    params.get("file_count", 0),
                    "files":         params.get("files", []),
                    "files_copied":  None,   # filled in on "done"
                    "files_missing": None,
                    "status":        "started",
                })
                # Annotate the task dict so per-task view shows it.
                if task_id_fp in run["tasks"]:
                    t = run["tasks"][task_id_fp]
                    t.setdefault("files_prep_count", 0)
                    t["files_prep_count"] += 1
            elif status == "done":
                # Update the most recent "started" record for this task.
                for rec in reversed(run["files_preparing"]):
                    if rec["task"] == task_id_fp and rec["status"] == "started":
                        rec["files_copied"]  = params.get("files_copied", 0)
                        rec["files_missing"] = params.get("files_missing", 0)
                        rec["copied"]        = params.get("copied", [])
                        rec["missing"]       = params.get("missing", [])
                        rec["status"]        = "done"
                        break

    return runs


# ── Stage-breakdown renderer (AUTO-CR-27) ─────────────────────────────────────

# Pipeline order for display; unknown stages sort last.
_STAGE_ORDER  = ["coder", "executor", "gate2", "canon", "fact",
                 "continuity", "prosody", "overall"]
_STAGE_LABELS = {
    "coder":       "Coder",
    "executor":    "Executor",
    "gate2":       "Gate-2 (LLM)",
    "canon":       "Canon",
    "fact":        "Fact",
    "continuity":  "Continuity",
    "prosody":     "Prosody",
    "overall":     "Overall",
}


def _fmt_stage_counts(stage: str, counts: dict) -> str:
    """One-line summary for one stage — e.g. 'Gate-2 (LLM):  2✗  1✓'."""
    label    = _STAGE_LABELS.get(stage, stage)
    rejected = counts.get("REJECTED", 0)
    approved = counts.get("APPROVED", 0)
    cap      = counts.get("ACCEPTED_AT_CAP", 0)
    errors   = counts.get("ERROR", 0)
    exhaust  = counts.get("EXHAUSTED", 0)

    parts: list[str] = []
    if rejected:
        parts.append(red(f"{rejected}✗"))
    if approved:
        parts.append(green(f"{approved}✓"))
    if cap:
        parts.append(yellow(f"{cap}⚠ cap"))
    if errors:
        parts.append(red(f"{errors} err"))
    if exhaust:
        parts.append(red("EXHAUSTED"))
    return f"{dim(label + ':')}  {'  '.join(parts) if parts else dim('—')}"


def render_stage_breakdown(task: dict, indent: str = "    ") -> None:
    """Print per-stage gate counts for a task when AUTO-CR-27 stage data exists.

    Stages are displayed in pipeline order:
      coder → executor → gate2 → (canon → fact → continuity → prosody) → overall
    Only stages that actually fired are shown; creative-only stages appear only
    for creative-mode tasks.
    """
    stages = task.get("stages", {})
    if not stages:
        return

    ordered = sorted(
        stages.items(),
        key=lambda kv: (
            _STAGE_ORDER.index(kv[0]) if kv[0] in _STAGE_ORDER else 99,
            kv[0],
        ),
    )
    print(f"{indent}{dim('gate log:')}")
    for stage, counts in ordered:
        print(f"{indent}  {_fmt_stage_counts(stage, counts)}")


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
    plan_total = run.get("plan_total") or len(real)
    print(f"  {bold('Tasks')}:           plan={bold(str(plan_total))}  "
          f"seen={str(len(real))}  "
          f"done={green(str(len(done_tasks)))}  "
          f"blocked={red(str(len(blocked_tasks)))}  "
          f"other={str(len(other_tasks))}")
    if gate1r:
        print(f"  {bold('Gate-1 rejected')}: {yellow(str(len(gate1r)))} tasks filtered before execution")
    print(f"  {bold('Iterations')}:      total={bold(str(total_iters))}  "
          f"approved={green(str(total_approved))}  "
          f"rejected={red(str(total_rejected))}")
    print(f"  {bold('LLM calls')}:       {run['llm_calls']}")
    _ctx_reqs = run.get("context_requests", [])
    if _ctx_reqs:
        _total_syms = sum(len(r["symbols"]) for r in _ctx_reqs)
        print(f"  {bold('Context pulls')}:   {cyan(str(len(_ctx_reqs)))} "
              f"request(s), {_total_syms} symbol(s)")
    print(f"  {bold('Prompt changes')}: {magenta(str(prompt_changes))}")
    _rewrites = run.get("rewrite_attempts", [])
    if _rewrites:
        _n_promoted = sum(1 for a in _rewrites if a.get("promoted"))
        _n_denied = len(_rewrites) - _n_promoted
        print(
            f"  {bold('Rewrite attempts')}: {len(_rewrites)}  "
            f"({green(f'{_n_promoted} promoted')} / {red(f'{_n_denied} denied')})  "
            f"{dim('— see --rewrites for full report')}"
        )
    _fp_recs = run.get("files_preparing", [])
    _fp_done = [r for r in _fp_recs if r.get("status") == "done"]
    if _fp_done:
        _fp_copied  = sum(int(r.get("files_copied",  0)) for r in _fp_done)
        _fp_missing = sum(int(r.get("files_missing", 0)) for r in _fp_done)
        _fp_miss_str = f"  {yellow(f'({_fp_missing} missing)')}" if _fp_missing else ""
        print(
            f"  {bold('Files prepared')}: "
            f"{cyan(str(len(_fp_done)))} workspace setup(s)  "
            f"{_fp_copied} file(s) copied{_fp_miss_str}"
        )
    print(f"  {bold('Total events')}:    {run['total_events']}")


def render_applied_tasks(run: dict, mode: str = "code") -> None:
    """Show every completed task — the things that were actually applied/done.

    ``mode`` (AUTO-CR-35) only changes the section header and per-task file
    annotation — docs runs share the exact same task shape as code runs, just
    with prose files instead of source files (see docs/Readme.MD §4).
    """
    tasks = run["tasks"]
    done = [
        v for k, v in tasks.items()
        if not k.startswith("gate1:")
        and v.get("status") in ("DONE", "APPROVED", "PASS")
    ]

    header = "DOCUMENTATION CHANGES APPLIED" if mode == "docs" else "APPLIED / COMPLETED TASKS"
    print_section(f"{header}  ({len(done)} total)")

    if not done:
        no_tasks_msg = (
            "  (no completed documentation tasks recorded in this run)"
            if mode == "docs" else
            "  (no completed tasks recorded in this run)"
        )
        print(dim(no_tasks_msg))
        return

    files_by_task = _task_files_map(run) if mode == "docs" else {}

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
        if mode == "docs":
            doc_files = files_by_task.get(task_id, [])
            if doc_files:
                preview = ", ".join(doc_files[:3])
                if len(doc_files) > 3:
                    preview += dim(f" … +{len(doc_files) - 3}")
                print(f"    {dim('file:')}     {dim(preview)}")
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
        render_stage_breakdown(t, indent="    ")


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
        render_stage_breakdown(t, indent="    ")

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


def render_rewrite_report(run: dict) -> None:
    """
    New mode: every auto-tuner rewrite attempt — score, agent, and whether it
    was promoted or denied. Successful (promoted) attempts also show the
    old→new prompt diff, recovered from the optimizer's llm_request/response
    pair that produced the candidate.
    """
    attempts = run.get("rewrite_attempts", [])

    if not attempts:
        print_section("PROMPT REWRITE ATTEMPTS")
        print(dim("  (no auto-tuner rewrite attempts recorded in this run)"))
        return

    n_promoted = sum(1 for a in attempts if a.get("promoted"))
    n_denied = len(attempts) - n_promoted

    print_section(f"PROMPT REWRITE ATTEMPTS  ({len(attempts)} total)")
    print(
        f"  {bold('Outcomes')}: "
        f"{green(f'{n_promoted} promoted')}  /  {red(f'{n_denied} denied')}"
    )

    for i, a in enumerate(attempts, 1):
        promoted = a.get("promoted", False)
        score    = a.get("score", 0.0)
        status   = green(bold("✓ PROMOTED")) if promoted else red(bold("✗ DENIED"))

        print()
        print(
            f"  {bold(f'Attempt #{i}')}  agent={cyan(a.get('agent', '?'))}  "
            f"score={bold(f'{score:.4f}')}  {status}  {dim(fmt_ts(a.get('ts', '')))}"
        )
        reason = a.get("reason", "")
        if reason:
            print(f"    {dim('reason:')} {truncate(reason, 100)}")

        if promoted:
            old_p = a.get("old_prompt", "")
            new_p = a.get("new_prompt", "")
            if old_p or new_p:
                print(f"    {dim('— old → new prompt —')}")
                diff_text = render_prompt_diff(old_p, new_p)
                for line in diff_text.splitlines():
                    print(f"    {line}")
            else:
                print(dim(
                    "    (old/new prompt text not found — optimizer "
                    "llm_request/llm_response missing from this trace)"
                ))


def render_timeline(run: dict, max_events: int = 40) -> None:
    events = run["events"]
    if not events:
        return

    INTERESTING = {
        "run_start", "run_finished", "run_capped",
        "call", "result", "decision", "error",
        "prompt_updated", "prompt_push", "prompt_change",
        "prompt_denied", "prompt_promoted",
        "rejected", "phase_transition",
    }
    # Include only files_preparing "started" transitions to avoid double-entries.
    def _is_interesting_phase(e: dict) -> bool:
        if e.get("kind") != "phase_transition":
            return False
        p = (e.get("params") or {})
        return p.get("phase") == "files_preparing" and p.get("status") == "started"
    shown = [
        e for e in events
        if e.get("kind") in INTERESTING
        or _is_interesting_phase(e)
        or (e.get("kind") == "llm_response"
            and _extract_context_signals(e.get("source", ""), str(e.get("content") or "")))
    ]

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
        elif kind == "result" and (
            "validator" in src
            or src in ("prosody", "story_bible")
        ):
            verdict_bool = _parse_validator_verdict(evt.get("content"))
            gate_label   = _CREATIVE_GATE_LABELS.get(src, "validator")
            if verdict_bool is True:
                verdict_str = green(bold(f"{gate_label}: APPROVED"))
            elif verdict_bool is False:
                verdict_str = red(bold(f"{gate_label}: REJECTED"))
            else:
                verdict_str = yellow(bold(f"{gate_label}: ?"))
            print(f"  {dim(ts)}  {verdict_str}  {dim(src)}")

        elif kind == "decision" and tgt == "inner_loop":
            # AUTO-CR-27 stage event — show compact one-liner per stage decision.
            stage   = params.get("stage", src or "?")
            verdict = content or "?"
            attempt = params.get("attempt", "")
            task_id = params.get("task") or params.get("task_id", "")
            label   = _STAGE_LABELS.get(stage, stage)
            if verdict in ("APPROVED", "ACCEPTED_AT_CAP"):
                col = green if verdict == "APPROVED" else yellow
            elif verdict in ("REJECTED", "EXHAUSTED", "ERROR"):
                col = red
            else:
                col = dim
            attempt_str = f"  {dim(f'attempt {attempt}')}" if attempt else ""
            print(f"  {dim(ts)}  {col(bold(f'{label}: {verdict}'))}"
                  f"  {dim(task_id)}{attempt_str}")

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

        elif kind in ("prompt_denied", "prompt_promoted"):
            agent = params.get("agent") or params.get("agent_name") or "?"
            score = params.get("score", 0.0)
            if kind == "prompt_promoted":
                label = green(bold("✓ rewrite promoted"))
            else:
                label = red(bold("✗ rewrite denied"))
            print(f"  {dim(ts)}  {label}  agent={cyan(agent)}  score={score:.4f}")

        elif kind == "phase_transition" and params.get("phase") == "files_preparing":
            task_fp    = params.get("task", "")
            file_count = params.get("file_count", 0)
            files      = params.get("files", [])
            preview    = ", ".join(files[:3])
            if len(files) > 3:
                preview += dim(f" … +{len(files)-3}")
            label = cyan(bold("⧉ files preparing"))
            print(
                f"  {dim(ts)}  {label}  "
                f"{dim(task_fp)}  "
                f"{bold(str(file_count))} file(s)"
                + (f"  {dim(preview)}" if preview else "")
            )

        elif kind == "phase_transition":
            phase  = params.get("phase", "?")
            status = params.get("status", "?")
            print(f"  {dim(ts)}  {bold('phase')} {cyan(phase)} → {status}")

        elif kind == "llm_response":
            _syms = _extract_context_signals(src, content)
            if not _syms:
                continue   # only surface responses that carry a context pull
            _arrow = chr(0x2192)
            _pull  = chr(0x27f3)
            print(f"  {dim(ts)}  {cyan(bold(_pull + ' context pull'))}  "
                  f"{dim(src)} {_arrow} {', '.join(_syms)}")

        elif kind == "error":
            msg = truncate(content or str(params), 70)
            print(f"  {dim(ts)}  {red(bold('ERROR'))}  {msg}")


# ── Files-preparing renderer ──────────────────────────────────────────────────

def render_files_preparing(run: dict) -> None:
    """Show a summary of all workspace file-preparation events for this run."""
    records = run.get("files_preparing", [])
    if not records:
        return

    # Group by task and count preparation rounds.
    from collections import defaultdict
    by_task: dict = defaultdict(list)
    for r in records:
        by_task[r["task"]].append(r)

    done_recs = [r for r in records if r.get("status") == "done"]
    total_copied  = sum(int(r.get("files_copied",  0)) for r in done_recs)
    total_missing = sum(int(r.get("files_missing", 0)) for r in done_recs)

    print_section(
        f"FILES PREPARING  ({len(done_recs)} workspace setup(s)  "
        f"copied={total_copied}  missing={total_missing})"
    )

    for task_id, recs in by_task.items():
        done = [r for r in recs if r.get("status") == "done"]
        if not done:
            continue
        last = done[-1]
        ts_str  = fmt_ts(last["ts"])
        n_preps = len(done)
        copied  = last.get("files_copied",  0)
        missing = last.get("files_missing", 0)
        files   = last.get("files", [])

        miss_str = f"  {yellow(f'({missing} missing)')}" if missing else ""
        print(
            f"  {cyan(task_id):30} "
            f"preps={bold(str(n_preps))}  "
            f"files={bold(str(copied))}{miss_str}  "
            f"{dim(ts_str)}"
        )
        # Show the file list (up to 6, then summarise).
        shown = files[:6]
        for f_name in shown:
            is_missing = f_name in (last.get("missing") or [])
            marker = red("✗") if is_missing else dim("·")
            print(f"    {marker} {dim(f_name)}")
        if len(files) > 6:
            print(f"    {dim(f'… and {len(files) - 6} more file(s)')}")


# ── Story-progress renderer (creative mode) ───────────────────────────────────

def render_story_progress(run: dict) -> None:
    """Chapter-by-chapter story progress for ``--auto`` creative-mode runs.

    Chapters are sorted by their numeric suffix (``chapter_01``, ``chapter_2``,
    …) so the output reads in narrative order regardless of trace order.
    Non-chapter tasks are shown after all chapters under "Other tasks".

    Per-chapter it shows:
    • Status (✓ done / ✗ blocked / ◌ in_progress / …)
    • Duration
    • Validator iteration breakdown (approved / rejected per gate when available)
    • Commit hash if present
    """
    tasks = {k: v for k, v in run["tasks"].items() if not k.startswith("gate1:")}
    if not tasks:
        print_section("STORY PROGRESS")
        print(dim("  (no chapter tasks recorded in this run)"))
        return

    chapters   = sorted(
        [t for t in tasks.values() if _chapter_num(t) < 9999],
        key=lambda t: (_chapter_num(t), t.get("start_ts", "")),
    )
    other_tasks = [t for t in tasks.values() if _chapter_num(t) == 9999]

    done_ch    = sum(1 for t in chapters if t.get("status") in ("DONE","APPROVED","PASS"))
    blocked_ch = sum(1 for t in chapters if t.get("status") in ("BLOCKED","FAIL"))

    print_section(
        f"STORY PROGRESS  "
        f"({len(chapters)} chapter(s)  "
        f"{green(str(done_ch))} done  "
        f"{red(str(blocked_ch))} blocked)"
    )

    for t in chapters:
        ch_num   = _chapter_num(t)
        ch_label = f"Chapter {ch_num:02d}"
        title    = t.get("title", "").strip()
        # Strip leading "chapter N" prefix if title duplicates the label.
        m = _CHAPTER_RE.match(title)
        if m:
            title = title[m.end():].lstrip(" :—-")

        status = t.get("status", "?")
        if status in ("DONE", "APPROVED", "PASS"):
            status_icon = green("✓")
            status_col  = green
        elif status in ("BLOCKED", "FAIL"):
            status_icon = red("✗")
            status_col  = red
        elif status == "in_progress":
            status_icon = yellow("◌")
            status_col  = yellow
        else:
            status_icon = yellow("●")
            status_col  = yellow

        iters    = t.get("iterations", 0)
        approved = t.get("approved", 0)
        rejected = t.get("rejected", 0)
        dur      = elapsed(t.get("start_ts", ""), t.get("end_ts", ""))
        commit   = t.get("commit", "")

        header_parts = [f"  {status_icon} {status_col(bold(ch_label))}"]
        if title:
            header_parts.append(f"  {truncate(title, 50)}")
        print("".join(header_parts))

        detail_parts: list[str] = []
        if dur:
            detail_parts.append(f"{dim('duration:')} {dur}")
        if iters:
            detail_parts.append(
                f"{dim('revisions:')} {iters}  "
                f"({green(str(approved))} approved / {red(str(rejected))} rejected)"
            )
        if commit:
            detail_parts.append(f"{dim('commit:')} {cyan(commit[:10])}")
        if detail_parts:
            print("    " + "   ".join(detail_parts))

        # Per-stage gate log (AUTO-CR-27: populated whenever _trace_stage fires).
        render_stage_breakdown(t, indent="    ")

    if other_tasks:
        print()
        print(f"  {bold(dim('Non-chapter tasks:'))}")
        for t in other_tasks:
            status = t.get("status", "?")
            col = green if status in ("DONE","APPROVED","PASS") else (
                  red   if status in ("BLOCKED","FAIL") else yellow)
            print(f"    {col('●')} {truncate(t.get('title', t.get('task_id','?')), 60)}  "
                  f"{dim(status)}")


# ── Main render ───────────────────────────────────────────────────────────────

def render_run(
    run: dict,
    show_timeline: bool = True,
    show_diff: bool = True,
    show_rewrites: bool = False,
    rewrites_only: bool = False,
    mode: str = "auto",
) -> None:
    """Render a full run report.

    Parameters
    ----------
    mode:
        ``"auto"``     — detect from trace (default; uses :func:`_detect_task_mode`)
        ``"code"``     — standard code-pipeline layout
        ``"creative"`` — story-progress layout with chapter ordering
        ``"docs"``     — standard code-pipeline layout, relabelled for
                         documentation runs (AUTO-CR-35; see docs/Readme.MD
                         §4 "Task Modes" — docs runs share the code-mode
                         trace shape, just with different architect/gate1/
                         validator prompts, so no separate renderer is needed)
    """
    effective_mode = mode if mode != "auto" else _detect_task_mode(run)

    print_header(f"Run Analysis  [{run['run_id']}]"
                 + (f"  [{cyan(effective_mode)} mode]" if effective_mode != "code" else ""))
    if rewrites_only:
        render_rewrite_report(run)
        return
    render_run_summary(run)

    if effective_mode == "creative":
        render_story_progress(run)         # chapter ordering + gate breakdown
    else:
        render_applied_tasks(run, mode=effective_mode)   # code / docs pipeline view

    render_files_preparing(run)            # workspace file-preparation stages
    render_tasks(run)
    if show_diff:
        render_prompt_changes(run)
    if show_rewrites:
        render_rewrite_report(run)
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
    p.add_argument("--rewrites",
                   action="store_true",
                   help=(
                       "New mode: also show the PROMPT REWRITE ATTEMPTS report — "
                       "every auto-tuner candidate with its score and whether it "
                       "was promoted or denied; promoted attempts include the "
                       "old → new prompt diff"
                   ))
    p.add_argument("--rewrites-only",
                   action="store_true",
                   help="Like --rewrites, but skip every other report section")
    p.add_argument("--no-color",
                   action="store_true",
                   help="Disable ANSI colours")
    p.add_argument("--mode",
                   choices=["auto", "code", "docs", "creative"],
                   default="auto",
                   metavar="MODE",
                   help=(
                       "Display mode: auto (default), code, docs, or creative. "
                       "auto detects from trace patterns. Use creative when "
                       "the run used task_mode=creative in agents.ini to get "
                       "chapter-ordered story-progress output. Use docs when "
                       "the run used task_mode=docs to get documentation-run "
                       "section labels."
                   ))
    return p


def render_auto_prompts(search_path: str) -> None:
    """If auto_prompts.json exists near search_path, print its contents."""
    p = Path(search_path)
    candidates = [
        p.parent / "auto_prompts.json",          # next to trace file
        p / "auto_prompts.json",                 # inside given dir
        p / ".agent" / "auto_prompts.json",      # .agent/ subdir
    ]
    found = next((c for c in candidates if c.exists()), None)
    if not found:
        return
    try:
        data = json.loads(found.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  [!] Could not read {found}: {exc}", file=sys.stderr)
        return
    print_section(f"AUTO-TUNED PROMPTS  ({found})")
    print(json.dumps(data, indent=2, ensure_ascii=False))



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

    if len(runs) > 1 and not args.run_id and not args.rewrites_only:
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
            show_rewrites=args.rewrites or args.rewrites_only,
            rewrites_only=args.rewrites_only,
            mode=args.mode,
        )

    if not args.rewrites_only:
        render_auto_prompts(args.path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
