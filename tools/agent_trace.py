"""
tools/agent_trace.py

Inter-agent communication trace.

Records, as newline-delimited JSON (one event per line), every message that
passes between the orchestrator and the agents, and between each agent and the
LLM backend.  Each event captures:

  - who sent it (source) and who it was for (target)
  - the kind of event (call / llm_request / llm_response / result / decision)
  - the *parameters* the agent was invoked with
  - the actual content (e.g. the full rendered prompt the agent sent, or the
    raw text the model returned)
  - model + sampling parameters (model, temperature, max_tokens) where relevant
  - a monotonically increasing sequence number and an ISO-8601 UTC timestamp
  - a run_id so all events from one `prompt>` line group together

The tracer is a module-level singleton so any agent can `from tools.agent_trace
import tracer` and emit events without being explicitly handed a reference.
It is a silent no-op until `tracer.configure(enabled=True, ...)` is called
(done once by the Orchestrator from `[trace]` in agents.ini), so importing it
never causes file I/O or overhead on its own.

Long fields (prompts, model responses) are truncated to `max_field_chars`
(default 4000) so the trace file stays readable; truncation is marked with a
"…[+N chars]" suffix.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = None  # avoid importing logging machinery here; orchestrator owns logging

# ANSI colours for the console echo lines (degrade gracefully on dumb terminals)
_C = {
    "orchestrator": "\033[36m",   # cyan
    "validator_agent": "\033[33m", # yellow
    "improvement_agent": "\033[35m",  # magenta
    "prompt_optimizer": "\033[34m",   # blue
    "prompt_evaluator": "\033[34m",
    "search_agent": "\033[32m",   # green
    "llm": "\033[90m",            # dark grey
    "user": "\033[37m",
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


class AgentTracer:
    def __init__(self) -> None:
        self.enabled: bool = False
        self.console_echo: bool = False   # print inter-agent calls live to stdout
        self.console_preview_chars: int = 600  # how many prompt/response chars to show
        self.path: Optional[Path] = None
        self.max_field_chars: int = 4000
        self._seq: int = 0
        self._run_id: Optional[str] = None
        self._lock = threading.Lock()
        self._fh: Optional[Any] = None   # persistent file handle opened at configure()

    # ------------------------------------------------------------------ #
    # Configuration                                                       #
    # ------------------------------------------------------------------ #

    def configure(
        self,
        enabled: bool,
        path: str = "agent_trace.jsonl",
        max_field_chars: int = 4000,
        console_echo: bool = False,
        console_preview_chars: int = 600,
    ) -> None:
        """Enable/disable tracing and set the output path. Called once at startup."""
        self.enabled = bool(enabled)
        self.console_echo = bool(console_echo)
        self.console_preview_chars = max(80, int(console_preview_chars))
        self.path = Path(path) if path else None
        self.max_field_chars = max(200, int(max_field_chars))
        # Close any previously open handle before opening the new one.
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self.enabled and self.path is not None:
            try:
                self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115
            except OSError:
                self._fh = None

    # ------------------------------------------------------------------ #
    # Run grouping                                                         #
    # ------------------------------------------------------------------ #

    def start_run(self, user_input: str) -> str:
        """Open a new run group. All subsequent events share this run_id."""
        self._run_id = uuid.uuid4().hex[:12]
        self.event(
            source="user",
            target="orchestrator",
            kind="run_start",
            params={"prompt": user_input},
        )
        return self._run_id

    # ------------------------------------------------------------------ #
    # Event emission                                                       #
    # ------------------------------------------------------------------ #

    def event(
        self,
        source: str,
        target: str,
        kind: str,
        params: Optional[dict] = None,
        content: Any = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> None:
        """
        Append one trace event.

        source / target : agent names, e.g. "orchestrator" -> "validator_agent",
                           or "validator_agent" -> "llm".
        kind            : call | llm_request | llm_response | result | decision |
                          run_start | error
        params          : the parameters the call was made with (dict).
        content         : free-form payload (prompt text, raw response, etc.).
        model/temperature/max_tokens : sampling parameters, when an LLM is involved.
        """
        if not self.enabled or self.path is None:
            return

        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(),
                "run_id": self._run_id,
                "source": source,
                "target": target,
                "kind": kind,
            }
            if model is not None:
                record["model"] = model
            if temperature is not None:
                record["temperature"] = temperature
            if max_tokens is not None:
                record["max_tokens"] = max_tokens
            if params is not None:
                record["params"] = self._sanitize(params)
            if content is not None:
                record["content"] = self._truncate(content)

            if self.console_echo:
                self._console_line(source, target, kind, model, temperature, max_tokens, params, content)

            try:
                if self._fh is not None:
                    self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    self._fh.flush()
            except OSError:
                # Tracing must never break the pipeline — swallow write errors.
                pass

    def _console_line(
        self,
        source: str,
        target: str,
        kind: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        params: Optional[dict] = None,
        content: Any = None,
    ) -> None:
        """Print a timestamped inter-agent event with prompt/response preview."""
        if kind == "run_start":
            return

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        sc  = _C.get(source, "")
        tc  = _C.get(target, "")
        rst = _C["reset"]
        dim = _C["dim"]
        bold = _C["bold"]

        # ── header line ────────────────────────────────────────────────────
        meta = ""
        if model:
            meta += f"  {dim}model={model}{rst}"
        if temperature is not None:
            meta += f"  {dim}temp={temperature}{rst}"
        if max_tokens is not None:
            meta += f"  {dim}max_tokens={max_tokens}{rst}"

        header = (
            f"{dim}{ts}{rst}  "
            f"{bold}{sc}{source}{rst}"
            f"{dim} → {rst}"
            f"{bold}{tc}{target}{rst}"
            f"  {dim}[{kind}]{rst}"
            f"{meta}"
        )
        sys.stdout.write(header + "\n")

        # ── content preview ─────────────────────────────────────────────────
        PREVIEW_CHARS = self.console_preview_chars

        def _show(label: str, text: str) -> None:
            """Print the tail (and a tiny header) of text, indented, with a label."""
            if not text:
                return
            text = text.replace("\r\n", "\n").replace("\r", "\n")
            HEAD = 120          # always show first N chars so you know which prompt it is
            TAIL = PREVIEW_CHARS  # show last N chars — the actual task/question lives here

            if len(text) <= HEAD + TAIL:
                snippet = text
                mid = ""
            else:
                head_part = text[:HEAD].rstrip()
                tail_part = text[-(TAIL):].lstrip("\n")
                skipped   = len(text) - HEAD - TAIL
                mid       = f"\n    {dim}  … [{skipped} chars skipped] …{rst}"
                snippet   = None

            def _indent(s: str) -> str:
                return "\n".join(f"    {l}" for l in s.splitlines())

            sys.stdout.write(f"  {dim}{label}:{rst}\n")
            if snippet is not None:
                sys.stdout.write(_indent(snippet) + "\n")
            else:
                sys.stdout.write(_indent(head_part) + mid + "\n" + _indent(tail_part) + "\n")

        if kind == "llm_request" and content is not None:
            # content is the full rendered prompt string
            raw = content if isinstance(content, str) else str(content)
            _show("PROMPT", raw)

        elif kind == "llm_response" and content is not None:
            raw = content if isinstance(content, str) else str(content)
            _show("RESPONSE", raw)

        elif kind == "call" and params:
            # Show the most useful keys; skip large blobs
            SKIP = {"related_code", "target_block", "imports", "context_lines"}
            for k, v in params.items():
                if k in SKIP:
                    continue
                v_str = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
                _show(k, v_str)

        elif kind in ("result", "decision", "error") and content is not None:
            raw = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
            _show("RESULT", raw)

        sys.stdout.flush()

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _truncate(self, value: Any) -> Any:
        """Stringify (if needed) and cap long fields."""
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                value = str(value)
        elif not isinstance(value, str):
            value = str(value)

        if len(value) > self.max_field_chars:
            cut = len(value) - self.max_field_chars
            return value[: self.max_field_chars] + f"…[+{cut} chars]"
        return value

    def _sanitize(self, params: dict) -> dict:
        """Truncate each value in a params dict so nested prompts don't bloat the line."""
        out = {}
        for k, v in params.items():
            out[k] = self._truncate(v)
        return out


# Module-level singleton shared by every agent.
tracer = AgentTracer()
