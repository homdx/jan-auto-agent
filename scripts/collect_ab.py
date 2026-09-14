#!/usr/bin/env python3
"""scripts/collect_ab.py — M5: one goal, two runs, one diff.

The only way to claim "the pack helps" is to run the same thing twice. This
harness runs one goal on one base tree twice — arm A with
``[collect] pack_enabled = false`` (the pre-V3 rows), arm B with
``pack_enabled = true`` (the full pack) — and diffs the Tier-2 counters.
Everything else is identical: the goal, the tree, the plan, the stub, the
config file (copied to a scratch path and patched there; the source is never
written — its bytes are re-hashed after every copy).

What "identical" costs, and how each cost is paid
-------------------------------------------------
* **Both arms execute tasks.** ``--dry-run`` never reaches ``Coder``, and the
  collect block is built only in the coder prompt — a dry-run A/B measures
  nothing. Both arms run ``main.py --auto`` for real, against a local stub.
* **One plan.** The architect + gate-1 phase is upstream of the pack and, on a
  live model, non-deterministic. The plan is built **once** (``--auto
  --dry-run`` on its own clone of the base), then every arm's ``.agent/`` is
  seeded from it — ``plan.json``, ``progress.json``, ``tickets/`` — so
  ``--auto`` resumes straight into the coding phase. Consequently the gate-1
  rows are a sanity check (must be equal), not a measurement.
* **Non-``.py`` tasks are dropped from the seeded plan** — they get no block by
  construction (L1 is open) and the coder loops on them. The same tasks are
  dropped in both arms and the count is reported.
* **Determinism.** There is no seed flag; the stub is the seed. It answers
  every role and replays recorded answers keyed by a hash of the prompt with
  the ``COLLECT MODEL`` block cut out — the coder prompt differs between the
  arms by exactly that block, so a reply recorded in arm A serves arm B. A
  prompt with no recording gets a canned, role-aware answer (the same one
  every time), so a run always completes. Arm A runs twice; if its two
  counter sets differ the diff is reported as not reproducible.
* **Never a live provider.** Every ``base_url`` in the patched copy is pointed
  at the stub, every ``api_key`` (commented lines included) is scrubbed, and
  a stub URL off loopback is refused before anything runs.

Counters — one function each, read from **every** ``trace_*.jsonl`` of the
arm's tree (the plan phase's trace is seeded along with the plan, so the
gate-1 rows come out equal by construction and any difference is a seeding
bug):

    probe requests / misses         probe_request, probe_result(misses),
                                    probe_declined(unresolved → ops)
    context re-requests             llm_response bodies carrying a
                                    context_request / missing_context list
    gate1 rejected: existence /     kind=rejected — params.stage (M4) when
      presence                      present, else the filter's own reason
                                    vocabulary
    gate2 attempts per task         stage=gate2 decisions + overall APPROVED
                                    (an approval passes gate 2 without its
                                    own event) / tasks executed
    tasks done / blocked            outer_loop result passed=True /
                                    decision BLOCKED
    coder rounds per task           coder llm_request / tasks executed
    prompt chars per coder call     mean len(llm_request.content), coder
    collect blocks / coder calls    blocks in coder prompts — M4's
                                    collect_block events when the trace has
                                    them, the block header otherwise — and
                                    the pack rows (callers / calls_into /
                                    tests / neighbours) inside them; arm A
                                    must show 0 pack rows, arm B > 0
    LLM calls, whole run            every llm_request

Pass condition, stated up front and deliberately weak: **probe misses and
gate-2 attempts do not increase, and the coder prompt grows by less than the
pack's own budget** (``[collect] max_context_chars_auto``). A pack that makes
the model ask more questions is a pack that added noise.

Commands
--------
    configs   copy + patch the two arm configs, run nothing
    serve     run the stub on its own (for a run driven by hand)
    plan      build the seeded plan once (dry-run + non-.py drop), run nothing
    run       the whole thing: configs → stub → plan → A ×2 → B → diff
    report    diff two arm trees that already exist (no subprocess)
    counters  print the counters of one tree

    scripts/collect_ab.sh run --goal "<goal>" --base ../tree \\
        --config agents_128k.ini --workdir /tmp/ab \\
        --out docs/collect-epics/ab-<date>.json
"""

from __future__ import annotations

import argparse
import configparser
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── the block under test ──────────────────────────────────────────────────────
# Must match tools/auto/context_assembler._COLLECT_HEADER byte for byte; the
# assembler ends the block with one empty line, which is where the cut stops.
COLLECT_HEADER = "COLLECT MODEL (static facts, do not contradict):"
_COLLECT_BLOCK_RE = re.compile(
    re.escape(COLLECT_HEADER) + r"\n(?:[^\n]*\n)*?\n"
)
# The rows V3–V5 added on top of the V2 baseline — the rows `pack_enabled`
# switches. Arm A must show none of them, arm B at least one.
PACK_ROW_PREFIXES = ("callers: ", "calls_into: ", "tests: ", "neighbours: ")

DEFAULT_BUDGET = 1200            # [collect] max_context_chars_auto default
_TRACE_MAX_FIELD_CHARS = 400000  # keep coder prompts untruncated in the trace
_SCRUBBED_KEY = "collect-ab-scrubbed"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def cut_collect_block(text: str) -> str:
    """*text* with every COLLECT block removed — the only part of a coder
    prompt the two arms differ by, so it is the part the replay key ignores."""
    if not text or COLLECT_HEADER not in text:
        return text or ""
    return _COLLECT_BLOCK_RE.sub("", text)


def replay_key(system: str, user: str) -> str:
    """sha256 of the prompt with the block cut out — the stub's replay key."""
    payload = (system or "") + "\x00" + cut_collect_block(user or "")
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()


def count_collect_blocks(content: str) -> Tuple[int, int]:
    """``(blocks, pack_rows)`` in one coder prompt.

    The one place block detection lives. Today it greps the header; when M4's
    ``collect_block`` event lands, ``counters_from_events`` prefers the event
    and this stays as the fallback for traces written before it.
    """
    if not content:
        return 0, 0
    blocks = content.count(COLLECT_HEADER)
    rows = 0
    for match in _COLLECT_BLOCK_RE.finditer(content):
        for line in match.group(0).splitlines():
            if line.startswith(PACK_ROW_PREFIXES):
                rows += 1
    return blocks, rows


# ── config: copy, patch, never edit ───────────────────────────────────────────

class LiveProviderRefused(RuntimeError):
    """The stub URL does not resolve to loopback — refusing to measure."""


def guard_stub_url(url: str) -> None:
    host = (urlparse(url or "").hostname or "").lower()
    if not url or host not in LOOPBACK_HOSTS:
        raise LiveProviderRefused(
            f"--stub-url {url!r} is not loopback — a measurement run never "
            "points at a live provider"
        )


_HEADER_RE = re.compile(r"^\[([^\]]+)\]\s*(?:[;#].*)?$")
_KEY_RE = re.compile(r"^(\s*)([A-Za-z0-9_]+)(\s*)=(.*)$")
_COMMENTED_KEY_RE = re.compile(r"^(\s*[#;]+\s*)api_key(\s*)=.*$")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_config(src: os.PathLike, dst: os.PathLike, *, pack_enabled: bool,
                 stub_url: str, max_field_chars: int = _TRACE_MAX_FIELD_CHARS) -> Dict[str, Any]:
    """Copy *src* to *dst* and patch the copy line by line. Never writes *src*.

    Patches: every ``base_url`` (any section) → *stub_url*; every ``api_key``
    line, commented or not → scrubbed; ``[collect] pack_enabled`` set (inserted
    when absent), ``[collect] use_in_auto = true`` (the pack is wired through
    it — flipping it would remove the architect probe too); ``[trace]
    max_field_chars`` raised so prompt lengths are measured, not truncated;
    the active ``[api_<active>]`` section gets a ``base_url`` when its own is
    commented out (the 128k template ships it that way).

    Line-based rather than a ConfigParser round-trip so the copy stays
    byte-identical to the template everywhere else and the two arm copies
    differ by exactly one line. Returns a summary of what was patched.
    """
    guard_stub_url(stub_url)
    src, dst = Path(src), Path(dst)
    if not src.is_file():
        raise FileNotFoundError(f"config not found: {src}")
    if src.resolve() == dst.resolve():
        raise ValueError(f"refusing to patch {src} in place")
    before = _sha256(src)
    lines = src.read_text(encoding="utf-8").splitlines(keepends=True)

    summary: Dict[str, Any] = {"base_url": 0, "api_key": 0, "inserted": []}
    wanted = {  # (section, key) -> value the copy must carry
        ("collect", "pack_enabled"): "true" if pack_enabled else "false",
        ("collect", "use_in_auto"): "true",
        ("trace", "max_field_chars"): str(int(max_field_chars)),
    }
    seen: set = set()
    base_url_sections: set = set()
    active = "local"
    section = ""
    for line in lines:
        m = _HEADER_RE.match(line.strip())
        if m:
            section = m.group(1).strip()
            continue
        km = _KEY_RE.match(line.rstrip("\n"))
        if km and section == "api" and km.group(2) == "active":
            active = km.group(4).split("#")[0].split(";")[0].strip()
        if km and km.group(2) == "base_url":
            base_url_sections.add(section)

    out: List[str] = []
    section = ""

    def flush(sec: str) -> None:
        for (s, k), v in wanted.items():
            if s == sec and (s, k) not in seen:
                out.append(f"{k} = {v}\n")
                seen.add((s, k))
                summary["inserted"].append(f"[{s}] {k}")
        if sec == f"api_{active}" and sec not in base_url_sections:
            out.append(f"base_url = {stub_url}\n")
            base_url_sections.add(sec)
            summary["inserted"].append(f"[{sec}] base_url")

    for line in lines:
        m = _HEADER_RE.match(line.strip())
        if m:
            flush(section)
            section = m.group(1).strip()
            out.append(line)
            continue
        cm = _COMMENTED_KEY_RE.match(line.rstrip("\n"))
        if cm:
            out.append(f"{cm.group(1)}api_key{cm.group(2)}= {_SCRUBBED_KEY}\n")
            summary["api_key"] += 1
            continue
        km = _KEY_RE.match(line.rstrip("\n"))
        if km:
            indent, key, gap = km.group(1), km.group(2), km.group(3)
            if key == "base_url":
                out.append(f"{indent}{key}{gap}= {stub_url}\n")
                summary["base_url"] += 1
                continue
            if key == "api_key":
                out.append(f"{indent}{key}{gap}= {_SCRUBBED_KEY}\n")
                summary["api_key"] += 1
                continue
            if (section, key) in wanted:
                out.append(f"{indent}{key}{gap}= {wanted[(section, key)]}\n")
                seen.add((section, key))
                continue
        out.append(line)
    flush(section)
    for sec in dict.fromkeys(s for s, _ in wanted):
        missing = [(k, v) for (s, k), v in wanted.items() if s == sec and (s, k) not in seen]
        if missing:  # the section itself is absent
            out.append(f"\n[{sec}]\n" + "".join(f"{k} = {v}\n" for k, v in missing))
            seen.update((sec, k) for k, _ in missing)
            summary["inserted"].extend(f"[{sec}] {k} (new section)" for k, _ in missing)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("".join(out), encoding="utf-8")
    if _sha256(src) != before:
        raise RuntimeError(f"{src} changed while it was being copied — aborting")
    summary["pack_enabled"] = pack_enabled
    return summary


def read_pack_budget(config: os.PathLike) -> int:
    parser = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    parser.read(str(config), encoding="utf-8")
    try:
        return parser.getint("collect", "max_context_chars_auto", fallback=DEFAULT_BUDGET)
    except ValueError:
        return DEFAULT_BUDGET


# ── the stub: every role, replay first, canned second ─────────────────────────

def role_of(system: str) -> str:
    """Which agent is asking, from the system prompt the run actually sends
    (each role's opener is pinned in its module)."""
    s = (system or "")[:400]
    if "plan reviewer" in s:
        return "plan_reviewer"
    if "senior software architect" in s or "senior technical writer" in s \
            or "creative-writing planner" in s:
        return "architect"
    if "reviewed failed implementation attempts" in s:
        return "task_rewriter"
    if "false-positive check" in s or "quality check" in s:
        return "gate1"
    if "senior software engineer implementing" in s:
        return "coder"
    if "code-change validator" in s or "documentation change validator" in s \
            or "creative writing editor validating" in s:
        return "gate2"
    if "code completeness validator" in s:
        return "validator"
    if "noise filter" in s:
        return "search_filter"
    if "prompt engineering agent" in s:
        return "prompt_optimizer"
    return "other"


_TARGET_FILES_RE = re.compile(r"^TARGET FILES TO MODIFY:\n((?:\s+- .+\n)+)", re.M)
_TASK_ID_RE = re.compile(r"^TASK ID:\s*(\S+)", re.M)
_LISTED_PY_RE = re.compile(r"^### (\S+\.py)$", re.M)


# Where a "### <path>" file section ends in a coder / architect prompt: the
# next section, or the coder template's feedback / closing text.
_SECTION_END_MARKERS = ("\n### ", "\nPRIOR ROUND FEEDBACK", "\nProduce the corrected files now",
                        "\nNow output the COMPLETE")
_STUB_TOUCH = "# collect_ab stub touch\n"


def _file_section(user: str, rel: str) -> Optional[str]:
    marker = f"### {rel}\n"
    i = user.find(marker)
    if i < 0:
        return None
    body = user[i + len(marker):]
    ends = [k for k in (body.find(m) for m in _SECTION_END_MARKERS) if k >= 0]
    return body[:min(ends)] if ends else body


def canned_answer(role: str, system: str, user: str) -> str:
    """A deterministic, schema-valid answer for *role* — enough for a run to
    complete when nothing was recorded for this prompt."""
    if role == "plan_reviewer":
        return "APPROVED"
    if role == "architect":
        # one task per prompt, on the first non-test module that defines
        # something — gate-1's existence check wants a real symbol to anchor to
        paths = _LISTED_PY_RE.findall(user)
        code = [p for p in paths if not Path(p).name.startswith("test_")
                and "/tests/" not in p and not p.startswith("tests/")]
        target, symbol = None, None
        for p in code or paths:
            m = re.search(r"^(?:def|class)\s+([A-Za-z_]\w*)", _file_section(user, p) or "", re.M)
            if m:
                target, symbol = p, m.group(1)
                break
        if target is None:
            return "[]"
        # the coder may only write target_files, and the tests mandate wants a
        # test among them — an existing test module from the listing, if any
        tests = [p for p in paths if p not in code]
        return json.dumps([{
            "title": f"Touch {Path(target).name} (collect_ab stub task)",
            "instruction": f"Append a trailing comment to {target} and its test.",
            "target_files": [target] + tests[:1],
            "acceptance_check": f"python3 -m py_compile {target}",
            "cited_location": {"file": target, "symbol": symbol,
                               "line_start": None, "line_end": None, "new_file": False},
        }])
    if role == "gate1":
        m = re.search(r"```[^\n]*\n(.*?)\n```", user, re.S)
        first = next((ln for ln in (m.group(1) if m else "").splitlines() if ln.strip()), "")
        return json.dumps({"verdict": "confirmed", "evidence": first,
                           "reason": "collect_ab stub: confirmed"})
    if role == "coder":
        files = []
        tm = _TARGET_FILES_RE.search(user)
        targets = [ln.strip()[2:].strip() for ln in (tm.group(1) if tm else "").splitlines()
                   if ln.strip().startswith("- ")]
        for rel in targets:
            body = _file_section(user, rel)
            if body is None or body.startswith("[new file") or body.startswith("[unreadable"):
                body = ""
            if body and not body.endswith("\n"):
                body += "\n"
            if _STUB_TOUCH not in body:
                body += _STUB_TOUCH
            files.append({"path": rel, "content": body})
        if not files:
            files.append({"path": "collect_ab_stub.txt", "content": "stub\n"})
        return json.dumps({"files": files})
    if role in ("gate2", "validator"):
        return json.dumps({"approved": True, "feedback": "collect_ab stub: approved", "hints": []})
    if role == "search_filter":
        return "[]"
    if role == "task_rewriter":
        return user  # nothing to rewrite deterministically — echo
    return "APPROVED"


class Recordings:
    """``replay_key -> reply`` loaded from / appended to a JSONL file."""

    def __init__(self, path: Optional[os.PathLike] = None) -> None:
        self.path = Path(path) if path else None
        self._map: Dict[str, str] = {}
        self._lock = threading.Lock()
        if self.path and self.path.is_file():
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    rec = json.loads(line)
                    self._map[rec["key"]] = rec["reply"]
                except (ValueError, KeyError, TypeError):
                    continue

    def __len__(self) -> int:
        return len(self._map)

    def get(self, key: str) -> Optional[str]:
        return self._map.get(key)

    def put(self, key: str, role: str, reply: str) -> None:
        with self._lock:
            self._map[key] = reply
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"key": key, "role": role, "reply": reply},
                                        ensure_ascii=False) + "\n")


def shape_reply(content: str, *, api_format: str, stream: bool) -> Tuple[bytes, str]:
    """``(body, content_type)`` in the exact shape ``tools.llm_stream`` parses."""
    if api_format == "ollama":
        body = json.dumps({"message": {"role": "assistant", "content": content},
                           "done": True}) + "\n"
        return body.encode("utf-8"), "application/x-ndjson"
    if stream:
        chunk = json.dumps({"choices": [{"delta": {"role": "assistant", "content": content}}]})
        return f"data: {chunk}\n\ndata: [DONE]\n\n".encode("utf-8"), "text/event-stream"
    body = json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]})
    return body.encode("utf-8"), "application/json"


class _StubHandler(BaseHTTPRequestHandler):
    server_version = "collect_ab_stub/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # noqa: D401 — silence per-request noise
        pass

    def do_POST(self) -> None:  # noqa: N802
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            self.send_error(400, "bad json")
            return
        messages = body.get("messages") or []
        system = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") == "system")
        user = "\n".join(str(m.get("content") or "") for m in messages if m.get("role") != "system")
        api_format = "ollama" if self.path.rstrip("/").endswith("/api/chat") else "openai"
        stream = bool(body.get("stream", False))
        role = role_of(system)
        key = replay_key(system, user)

        reply = srv.recordings.get(key)  # type: ignore[attr-defined]
        source = "replay"
        if reply is None:
            reply = canned_answer(role, system, user)
            source = "canned"
            srv.recordings.put(key, role, reply)  # type: ignore[attr-defined]
        tid = _TASK_ID_RE.search(user)
        srv.record_hit(  # type: ignore[attr-defined]
            {"role": role, "source": source, "key": key[:16],
             "task_id": tid.group(1) if tid else None,
             "prompt_chars": len(system) + len(user), "api_format": api_format,
             "stream": stream},
        )
        payload, ctype = shape_reply(reply, api_format=api_format, stream=stream)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, host: str, port: int, *, recordings: Recordings,
                 log_path: Optional[os.PathLike] = None) -> None:
        super().__init__((host, port), _StubHandler)
        self.recordings = recordings
        self.log_path = Path(log_path) if log_path else None
        self.hits: List[dict] = []
        self._lock = threading.Lock()

    def record_hit(self, rec: dict) -> None:
        with self._lock:
            self.hits.append(rec)
            if self.log_path:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> "StubServer":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self


def start_stub(host: str = "127.0.0.1", port: int = 0, *, recordings: Optional[Recordings] = None,
               log_path: Optional[os.PathLike] = None) -> StubServer:
    """Start the stub on a background thread; ``port=0`` picks a free one."""
    # `is not None`, not `or`: an empty Recordings is falsy through __len__
    return StubServer(host, port, recordings=recordings if recordings is not None else Recordings(),
                      log_path=log_path).start()


# ── the seeded plan ───────────────────────────────────────────────────────────

def drop_non_py_tasks(plan: dict) -> Tuple[dict, List[str], List[str]]:
    """Remove tasks whose ``target_files`` are all non-``.py`` (no block by
    construction until L1) and every ``dependencies`` entry pointing at them.

    Returns ``(plan, dropped_ids, dangling)`` where *dangling* lists the
    ``"task -> dep"`` edges that were cut so the report can say so.
    """
    tasks = list(plan.get("tasks") or [])
    keep, dropped = [], []
    for t in tasks:
        targets = [str(p) for p in (t.get("target_files") or [])]
        if targets and not any(p.endswith(".py") for p in targets):
            dropped.append(str(t.get("id")))
        else:
            keep.append(t)
    dropped_set = set(dropped)
    dangling: List[str] = []
    for t in keep:
        deps = [d for d in (t.get("dependencies") or [])]
        cut = [d for d in deps if str(d) in dropped_set]
        if cut:
            dangling.extend(f"{t.get('id')} -> {d}" for d in cut)
            t["dependencies"] = [d for d in deps if str(d) not in dropped_set]
    plan = dict(plan)
    plan["tasks"] = keep
    return plan, dropped, dangling


_SEED_FILES = ("plan.json", "progress.json")
_SEED_DIRS = ("tickets", "tasks")


def seed_agent_dir(plan_tree: os.PathLike, arm_tree: os.PathLike) -> None:
    """Copy the plan's ``.agent/`` state into the arm — plan, progress,
    tickets, and the plan phase's own trace (so every arm's counters carry the
    same gate-1 / probe history) — and re-point ``base_dir``."""
    src = Path(plan_tree) / ".agent"
    dst = Path(arm_tree) / ".agent"
    dst.mkdir(parents=True, exist_ok=True)
    for name in _SEED_FILES:
        if (src / name).is_file():
            shutil.copy2(src / name, dst / name)
    for trace in src.glob("trace_*.jsonl"):
        shutil.copy2(trace, dst / trace.name)
    for name in _SEED_DIRS:
        if (src / name).is_dir():
            shutil.copytree(src / name, dst / name, dirs_exist_ok=True)
    plan_path = dst / "plan.json"
    if plan_path.is_file():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["base_dir"] = str(Path(arm_tree).resolve())
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ── counters ──────────────────────────────────────────────────────────────────

ROWS: Tuple[Tuple[str, str, str], ...] = (
    # key, label, want
    ("probe_requests",            "probe requests",              ""),
    ("probe_misses",              "probe misses",                "down"),
    ("context_re_requests",       "context re-requests",         "down"),
    ("gate1_rejected_existence",  "gate1 rejected: existence",   "equal"),
    ("gate1_rejected_presence",   "gate1 rejected: presence",    "equal"),
    ("gate2_attempts_per_task",   "gate2 attempts per task",     "down"),
    ("tasks_blocked",             "tasks blocked",               ""),
    ("coder_rounds_per_task",     "coder rounds per task",       "down"),
    ("tasks_done",                "tasks done",                  ""),
    ("prompt_chars_per_coder_call", "prompt chars per coder call", "up-little"),
    ("collect_blocks",            "collect blocks",              ""),
    ("collect_pack_rows",         "  pack rows in them",         "A=0,B>0"),
    ("coder_calls",               "coder calls",                 ""),
    ("llm_calls",                 "LLM calls, whole run",        ""),
)

_EXISTENCE_REASONS = (  # gate1_filter._check_existence's own vocabulary
    "new_file path escapes base_dir", "new_file path is an existing directory",
    "but this path already exists", "was not in the ingested file list",
    "cited path escapes base_dir", "cited file not found", "cannot read ",
)
_DUPLICATE_REASONS = ("duplicate of an earlier candidate", "superseded by a later candidate")


def gate1_stage(params: dict) -> str:
    """``existence`` / ``presence`` / ``duplicate`` for one ``rejected`` event —
    M4's ``stage`` param when present, else the reason vocabulary."""
    stage = str(params.get("stage") or "").strip().lower()
    if stage in ("existence", "presence", "duplicate", "already_safe"):
        return stage
    reason = str(params.get("reason") or "")
    if any(m in reason for m in _EXISTENCE_REASONS):
        return "existence"
    if any(m in reason for m in _DUPLICATE_REASONS):
        return "duplicate"
    return "presence"


def _context_signals(content: Any) -> bool:
    """True when an ``llm_response`` body carries a non-empty
    ``context_request`` / ``missing_context`` list (the pull-model re-ask)."""
    if not isinstance(content, str) or not content.strip():
        return False
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rstrip().removesuffix("```")
    brace = text.find("{")
    if brace > 0:
        text = text[brace:]
    try:
        data = json.loads(text)
    except ValueError:
        return bool(re.search(r"CONTEXT_REQUEST\s*[::]\s*\S", content))
    if not isinstance(data, dict):
        return False
    return any(isinstance(data.get(k), list) and data.get(k)
               for k in ("context_request", "missing_context"))


def iter_trace_events(paths: Iterable[os.PathLike]) -> Iterable[dict]:
    """Every parseable event of every trace, in file order — a malformed line
    is skipped, a missing file is skipped (fail open)."""
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            continue


def trace_files(tree: os.PathLike) -> List[Path]:
    agent = Path(tree) / ".agent"
    return sorted(agent.glob("trace_*.jsonl")) if agent.is_dir() else []


def counters_from_events(events: Iterable[dict]) -> Dict[str, Any]:
    c: Dict[str, Any] = {k: 0 for k, _, _ in ROWS}
    raw = {"gate2_attempts": 0, "tasks_executed": 0, "prompt_chars": 0,
           "blocks_from_events": 0, "has_block_events": False,
           "gate1_rejected_duplicate": 0}
    for ev in events:
        kind = ev.get("kind") or ""
        src = ev.get("source") or ""
        tgt = ev.get("target") or ""
        params = ev.get("params") or {}
        content = ev.get("content") or ""
        if kind == "llm_request":
            c["llm_calls"] += 1
            if src == "coder":
                c["coder_calls"] += 1
                raw["prompt_chars"] += len(content) if isinstance(content, str) else 0
                blocks, rows = count_collect_blocks(content if isinstance(content, str) else "")
                c["collect_blocks"] += blocks
                c["collect_pack_rows"] += rows
        elif kind == "collect_block":  # M4
            # One event per coder request that received a block (memo hits
            # included). `rows_kept` counts the whole selected row set — the
            # three V2 rows too — so it cannot stand in for "pack rows": that
            # stays a grep of the prompt itself, which is where the A=0 / B>0
            # check needs it anyway.
            raw["has_block_events"] = True
            raw["blocks_from_events"] += 1
        elif kind == "llm_response":
            if _context_signals(content):
                c["context_re_requests"] += 1
        elif kind == "probe_request":
            c["probe_requests"] += 1
        elif kind == "probe_result":
            try:
                c["probe_misses"] += int(params.get("misses", 0) or 0)
            except (TypeError, ValueError):
                pass
        elif kind == "probe_declined":
            if "unresolved" in str(params.get("reason") or ""):
                try:
                    c["probe_misses"] += int(params.get("ops", 0) or 0)
                except (TypeError, ValueError):
                    pass
        elif kind == "rejected":
            stage = gate1_stage(params)
            if stage == "existence":
                c["gate1_rejected_existence"] += 1
            elif stage == "duplicate":
                raw["gate1_rejected_duplicate"] += 1
            else:
                c["gate1_rejected_presence"] += 1
        elif kind == "call" and tgt == "outer_loop":
            raw["tasks_executed"] += 1
        elif kind == "decision":
            stage = params.get("stage")
            verdict = str(content).strip().upper()
            if tgt == "inner_loop" and (stage == "gate2" or (stage == "overall" and verdict == "APPROVED")):
                raw["gate2_attempts"] += 1
            if src == "outer_loop" and verdict == "BLOCKED":
                c["tasks_blocked"] += 1
        elif kind == "result" and src == "outer_loop" and str(params.get("passed")).lower() == "true":
            c["tasks_done"] += 1
    if raw["has_block_events"]:  # M4 events win over the header grep
        c["collect_blocks"] = raw["blocks_from_events"]
    tasks = raw["tasks_executed"]
    c["tasks_executed"] = tasks
    c["gate2_attempts"] = raw["gate2_attempts"]
    c["gate2_attempts_per_task"] = round(raw["gate2_attempts"] / tasks, 3) if tasks else 0
    c["coder_rounds_per_task"] = round(c["coder_calls"] / tasks, 3) if tasks else 0
    c["prompt_chars_per_coder_call"] = round(raw["prompt_chars"] / c["coder_calls"], 1) if c["coder_calls"] else 0
    c["gate1_rejected_duplicate"] = raw["gate1_rejected_duplicate"]
    c["collect_source"] = "events" if raw["has_block_events"] else "header"
    return c


def counters_for_tree(tree: os.PathLike) -> Dict[str, Any]:
    """Counters over **every** trace of *tree* — never the newest alone."""
    paths = trace_files(tree)
    c = counters_from_events(iter_trace_events(paths))
    c["traces"] = [p.name for p in paths]
    return c


# ── the diff and the verdict ──────────────────────────────────────────────────

def diff_arms(off: Dict[str, Any], on: Dict[str, Any], *, budget: int) -> Dict[str, Any]:
    reasons: List[str] = []
    d = {k: (on.get(k, 0) or 0) - (off.get(k, 0) or 0) for k, _, _ in ROWS}
    if (off.get("coder_calls") or 0) == 0 or (on.get("coder_calls") or 0) == 0:
        verdict = "no_data"
        reasons.append("an arm has no coder llm_request — the coder was never reached "
                       "(a --dry-run, a plan with no .py task, or a stub that refused)")
    else:
        verdict = "pass"
        if d["probe_misses"] > 0:
            verdict = "fail"
            reasons.append(f"probe misses rose by {d['probe_misses']}")
        if d["gate2_attempts_per_task"] > 0:
            verdict = "fail"
            reasons.append(f"gate2 attempts per task rose by {d['gate2_attempts_per_task']}")
        if d["prompt_chars_per_coder_call"] >= budget:
            verdict = "fail"
            reasons.append(f"coder prompt grew by {d['prompt_chars_per_coder_call']:.0f} chars, "
                           f"not under the pack budget {budget}")
        if (off.get("collect_pack_rows") or 0) != 0:
            verdict = "fail"
            reasons.append(f"arm A shows {off['collect_pack_rows']} pack row(s) — pack_enabled=false did not take")
        if (on.get("collect_pack_rows") or 0) == 0:
            verdict = "fail"
            reasons.append("arm B shows no pack row — pack_enabled=true did not take "
                           "(absent/stale artifact, or no .py task)")
    sanity = [k for k in ("gate1_rejected_existence", "gate1_rejected_presence") if d[k] != 0]
    for k in sanity:
        reasons.append(f"{k} differs between arms — the plan was not seeded identically")
    return {"off": off, "on": on, "delta": d, "budget": int(budget),
            "verdict": "fail" if sanity and verdict != "no_data" else verdict,
            "reasons": reasons}


def render_table(diff: Dict[str, Any]) -> str:
    w = max(len(label) for _, label, _ in ROWS) + 2
    lines = [f"{'':<{w}} {'pack off':>10} {'pack on':>10} {'delta':>9}"]
    for key, label, want in ROWS:
        a, b, d = diff["off"].get(key, 0), diff["on"].get(key, 0), diff["delta"][key]
        d_str = f"{d:+.3f}" if isinstance(d, float) and not float(d).is_integer() else f"{int(d):+d}"
        note = {"down": "  ← want down", "equal": "  ← must be equal", "up-little": "  ← want up a little",
                "A=0,B>0": "  ← A must be 0, B > 0"}.get(want, "")
        lines.append(f"{label:<{w}} {a:>10} {b:>10} {d_str:>9}{note}")
    lines.append(f"verdict: {diff['verdict']}  (pack budget {diff['budget']} chars)")
    for r in diff["reasons"]:
        lines.append(f"  - {r}")
    return "\n".join(lines)


# ── running things ────────────────────────────────────────────────────────────

def clone_tree(src: os.PathLike, dst: os.PathLike) -> None:
    """A fresh copy of the base for one run: ``git clone --shared`` when the
    base is a repo (committed state only), a byte copy otherwise. ``.agent``
    is never carried over."""
    src, dst = Path(src), Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    if (src / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", "--shared", str(src), str(dst)],
                       check=True, capture_output=True, text=True)
        if (src / ".collect").is_dir() and not (dst / ".collect").is_dir():
            shutil.copytree(src / ".collect", dst / ".collect")
    else:
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".agent"))
    shutil.rmtree(dst / ".agent", ignore_errors=True)


def run_main(args: Sequence[str], *, cwd: os.PathLike = REPO_ROOT, timeout: int = 3600,
             log_to: Optional[os.PathLike] = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(REPO_ROOT / "main.py"), *args]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    if log_to:
        Path(log_to).write_text(
            "$ " + " ".join(cmd) + f"\n[exit {proc.returncode}]\n--- stdout ---\n"
            + proc.stdout[-20000:] + "\n--- stderr ---\n" + proc.stderr[-20000:],
            encoding="utf-8", errors="replace")
    return proc


def build_plan(goal: str, base: os.PathLike, plan_tree: os.PathLike, config: os.PathLike, *,
               timeout: int, log_to: Optional[os.PathLike] = None) -> Dict[str, Any]:
    """Clone *base* to *plan_tree*, build the plan once (``--auto --dry-run``),
    drop the non-``.py`` tasks. Returns what happened."""
    clone_tree(base, plan_tree)
    ensure_collect(plan_tree, config, timeout=timeout)
    proc = run_main(["--auto", goal, "--base", str(plan_tree), "--config", str(config), "--dry-run"],
                    timeout=timeout, log_to=log_to)
    plan_path = Path(plan_tree) / ".agent" / "plan.json"
    info: Dict[str, Any] = {"returncode": proc.returncode, "tasks": 0, "dropped": [], "dangling": []}
    if not plan_path.is_file():
        info["error"] = "no plan.json after --dry-run"
        return info
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    plan, dropped, dangling = drop_non_py_tasks(plan)
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    info.update(tasks=len(plan.get("tasks") or []), dropped=dropped, dangling=dangling)
    return info


def ensure_collect(tree: os.PathLike, config: os.PathLike, *, timeout: int,
                   log_to: Optional[os.PathLike] = None) -> bool:
    """A ``.collect/`` artifact for *tree* — built with ``--collect --no-llm``
    when absent (static passes only: deterministic, no stub involved)."""
    if (Path(tree) / ".collect" / "artifact.json").is_file():
        return True
    proc = run_main(["--collect", "--no-llm", "--base", str(tree), "--config", str(config)],
                    timeout=timeout, log_to=log_to)
    return proc.returncode == 0 and (Path(tree) / ".collect" / "artifact.json").is_file()


def run_arm(goal: str, base: os.PathLike, plan_tree: os.PathLike, arm_tree: os.PathLike,
            config: os.PathLike, *, timeout: int, log_to: Optional[os.PathLike] = None) -> Dict[str, Any]:
    clone_tree(base, arm_tree)
    ensure_collect(arm_tree, config, timeout=timeout)
    seed_agent_dir(plan_tree, arm_tree)
    proc = run_main(["--auto", goal, "--base", str(arm_tree), "--config", str(config)],
                    timeout=timeout, log_to=log_to)
    counters = counters_for_tree(arm_tree)
    return {"tree": str(arm_tree), "returncode": proc.returncode, "counters": counters}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _comparable(counters: Dict[str, Any]) -> Dict[str, Any]:
    """Counters minus the per-run trace names — what two repeats must agree on."""
    return {k: v for k, v in counters.items() if k != "traces"}


def _write_json(path: os.PathLike, payload: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def cmd_configs(a: argparse.Namespace) -> int:
    out = Path(a.out_dir)
    for arm, pack in (("off", False), ("on", True)):
        s = patch_config(a.config, out / f"config_{arm}.ini", pack_enabled=pack, stub_url=a.stub_url)
        print(f"config_{arm}.ini: base_url→stub ×{s['base_url']}, api_key scrubbed ×{s['api_key']}, "
              f"inserted {s['inserted'] or '-'}")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    srv = start_stub(a.host, a.port, recordings=Recordings(a.recordings), log_path=a.log)
    print(f"collect_ab stub at {srv.url} (recordings: {a.recordings or 'memory only'})", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
    return 0


def _stub_for(a: argparse.Namespace, workdir: Path) -> Tuple[str, Optional[StubServer]]:
    if a.stub_url:
        guard_stub_url(a.stub_url)
        return a.stub_url, None
    srv = start_stub(recordings=Recordings(a.recordings or workdir / "recordings.jsonl"),
                     log_path=workdir / "stub_requests.jsonl")
    return srv.url, srv


def cmd_plan(a: argparse.Namespace) -> int:
    workdir = Path(a.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    stub_url, srv = _stub_for(a, workdir)
    cfg = workdir / "config_off.ini"
    patch_config(a.config, cfg, pack_enabled=False, stub_url=stub_url)
    info = build_plan(a.goal, a.base, workdir / "plan", cfg, timeout=a.timeout, log_to=workdir / "plan.log")
    print(json.dumps(info, indent=1))
    if srv:
        srv.shutdown()
    return 0 if info.get("tasks") else 1


def cmd_counters(a: argparse.Namespace) -> int:
    print(json.dumps(counters_for_tree(a.tree), indent=1, sort_keys=True))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    off, on = counters_for_tree(a.a), counters_for_tree(a.b)
    diff = diff_arms(off, on, budget=a.budget)
    print(render_table(diff))
    if a.out:
        _write_json(a.out, {"schema": "collect-ab-1", "taken_at": _now(), "mode": "report",
                            "arms": {"off": str(a.a), "on": str(a.b)}, **diff})
        print(f"\n-> {a.out}")
    return {"pass": 0, "fail": 1, "no_data": 2}[diff["verdict"]]


def cmd_run(a: argparse.Namespace) -> int:
    goal = (a.goal or "").strip()
    if not goal:
        print("collect_ab: --goal must be non-empty", file=sys.stderr)
        return 2
    base = Path(a.base).resolve()
    if not base.is_dir():
        print(f"collect_ab: base tree not found: {base}", file=sys.stderr)
        return 2
    workdir = Path(a.workdir or REPO_ROOT / "runs" / f"ab-{_dt.datetime.now():%Y%m%d-%H%M%S}").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    budget = a.budget if a.budget is not None else read_pack_budget(a.config)

    stub_url, srv = _stub_for(a, workdir)
    configs = {}
    for arm, pack in (("off", False), ("on", True)):
        configs[arm] = workdir / f"config_{arm}.ini"
        patch_config(a.config, configs[arm], pack_enabled=pack, stub_url=stub_url)

    plan_tree = workdir / "plan"
    plan = build_plan(goal, base, plan_tree, configs["off"], timeout=a.timeout, log_to=workdir / "plan.log")
    print(f"plan: {plan['tasks']} task(s), dropped {len(plan['dropped'])} non-.py "
          f"{plan['dropped'] or ''}, cut {len(plan['dangling'])} dependency edge(s)")
    if not plan.get("tasks"):
        print("collect_ab: the plan has no .py task — nothing to measure (see plan.log)", file=sys.stderr)
        if srv:
            srv.shutdown()
        return 2

    runs: Dict[str, List[dict]] = {"off": [], "on": []}
    for arm, repeats in (("off", a.repeats), ("on", 1)):
        for i in range(repeats):
            tree = workdir / f"arm_{arm}_{i}"
            rec = run_arm(goal, base, plan_tree, tree, configs[arm], timeout=a.timeout,
                          log_to=workdir / f"arm_{arm}_{i}.log")
            rec["config"] = str(configs[arm])
            runs[arm].append(rec)
            c = rec["counters"]
            print(f"arm {arm} #{i}: exit {rec['returncode']}, coder calls {c['coder_calls']}, "
                  f"blocks {c['collect_blocks']}, pack rows {c['collect_pack_rows']}, "
                  f"done {c['tasks_done']}, blocked {c['tasks_blocked']}")
    if srv:
        srv.shutdown()

    off, on = runs["off"][-1]["counters"], runs["on"][0]["counters"]
    reproducible = all(_comparable(r["counters"]) == _comparable(off) for r in runs["off"])
    diff = diff_arms(off, on, budget=budget)
    if not reproducible:
        diff["reasons"].insert(0, "arm A repeats disagree — the stub was not deterministic; "
                               "do not read the deltas")
        if diff["verdict"] == "pass":
            diff["verdict"] = "fail"
    print()
    print(render_table(diff))
    print(f"reproducible arm A: {reproducible} ({a.repeats} repeat(s))")

    payload = {"schema": "collect-ab-1", "taken_at": _now(), "mode": "run", "goal": goal,
               "base": str(base), "config": str(Path(a.config).resolve()), "stub_url": stub_url,
               "workdir": str(workdir), "plan": plan, "reproducible_arm_a": reproducible,
               "runs": runs, "pass_condition": ("probe misses and gate-2 attempts do not increase, "
                                                "and the coder prompt grows by less than the pack budget"),
               **diff}
    _write_json(workdir / "ab.json", payload)
    if a.out:
        _write_json(a.out, payload)
        print(f"-> {a.out}")
    return {"pass": 0, "fail": 1, "no_data": 2}[diff["verdict"]]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="collect_ab.py",
                                 description="M5 A/B harness: one goal, two runs, one diff.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_stub(p: argparse.ArgumentParser) -> None:
        p.add_argument("--stub-url", default=None,
                       help="an already-running stub (loopback only); default: start the built-in one")
        p.add_argument("--recordings", default=None,
                       help="JSONL of recorded replies for the built-in stub (default: <workdir>/recordings.jsonl)")

    p = sub.add_parser("configs", help="copy + patch the two arm configs, run nothing")
    p.add_argument("--config", default="agents_128k.ini")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--stub-url", default="http://127.0.0.1:14141/v1")
    p.set_defaults(func=cmd_configs)

    p = sub.add_parser("serve", help="run the stub on its own")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=14141)
    p.add_argument("--recordings", default=None)
    p.add_argument("--log", default=None, help="JSONL request log")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("plan", help="build the seeded plan once, run nothing else")
    p.add_argument("--goal", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--config", default="agents_128k.ini")
    p.add_argument("--workdir", required=True)
    p.add_argument("--timeout", type=int, default=3600)
    common_stub(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="configs → stub → plan → arm A ×N → arm B → diff")
    p.add_argument("--goal", required=True, help="the --auto goal, byte-identical for both arms")
    p.add_argument("--base", required=True, help="base tree (committed state is what gets cloned)")
    p.add_argument("--config", default="agents_128k.ini", help="template config — copied, never edited")
    p.add_argument("--workdir", default=None, help="scratch dir for configs, clones, logs (default runs/ab-<ts>)")
    p.add_argument("--out", default=None, help="commit-ready JSON (e.g. docs/collect-epics/ab-<date>.json)")
    p.add_argument("--budget", type=int, default=None, help="pack budget (default: [collect] max_context_chars_auto)")
    p.add_argument("--repeats", type=int, default=2, help="arm A runs (reproducibility check)")
    p.add_argument("--timeout", type=int, default=3600, help="per main.py call, seconds")
    common_stub(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("report", help="diff two arm trees that already exist")
    p.add_argument("--a", required=True, help="pack-off tree")
    p.add_argument("--b", required=True, help="pack-on tree")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("counters", help="counters of one tree, every trace")
    p.add_argument("tree")
    p.set_defaults(func=cmd_counters)
    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.func(a)
    except LiveProviderRefused as exc:
        print(f"collect_ab: refused — {exc}", file=sys.stderr)
        return 3
    except (FileNotFoundError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"collect_ab: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
