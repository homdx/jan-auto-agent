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
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = None  # avoid importing logging machinery here; orchestrator owns logging


class AgentTracer:
    def __init__(self) -> None:
        self.enabled: bool = False
        self.path: Optional[Path] = None
        self.max_field_chars: int = 4000
        self._seq: int = 0
        self._run_id: Optional[str] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Configuration                                                       #
    # ------------------------------------------------------------------ #

    def configure(
        self,
        enabled: bool,
        path: str = "agent_trace.jsonl",
        max_field_chars: int = 4000,
    ) -> None:
        """Enable/disable tracing and set the output path. Called once at startup."""
        self.enabled = bool(enabled)
        self.path = Path(path) if path else None
        self.max_field_chars = max(200, int(max_field_chars))

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

            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                # Tracing must never break the pipeline — swallow write errors.
                pass

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
