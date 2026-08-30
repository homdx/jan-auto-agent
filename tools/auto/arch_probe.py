"""tools/auto/arch_probe.py — AUTO-P: architect context probe.

The Architect plans against a fixed slice of the repository
(``[architect] max_files_per_review`` files × ``max_file_chars`` each) and,
before AUTO-P, had no channel to say *"I need to see X before I can plan
this"*.  It had to guess, and the rest of the pipeline (Gate-1 grounding,
``existence_validator``, ``fact_validator``, ``delta_validator``) spent a
full plan→gate cycle catching the consequences.

AUTO-P adds the channel: the Architect may reply with a single line

    ARCH_PROBE: facts <name>, facts <name>

instead of the JSON task array.  The harness resolves those **read-only**
lookups, appends a digest to the same prompt, and re-asks.  Bounded by
``probe_max_rounds`` iterations and ``probe_max_total_chars`` of digest,
after which a *forced* final call tells the model to plan with what it has.
The give-up decision belongs to the harness, by counter — never to the
model, which is unreliable about when to stop asking.

Two pieces live here:

``extract_probe_request``
    Parses the protocol line.  Deliberately strict: an unknown op, an empty
    argument or a missing prefix all yield ``[]``, which the caller must
    treat as "not a probe" and hand to its normal unparseable-response
    handling.  Never raises.

``ArchProbe``
    Executes the ops against :class:`tools.auto.collect_bridge.CollectBridge`
    and formats a prompt-ready digest.  Fail-open in the same way the bridge
    is: a miss contributes a ``(not found)`` line, never an exception, so a
    broken or absent collect artifact can never abort a batch.

Why ``facts`` first (and, in Phase 0, only)
-------------------------------------------
``CollectBridge`` answers from the collect model's AST facts — signature
plus contracts — which is denser per token than any source excerpt and
works even for a symbol in a file the model was never shown.  It is also
already built once per ``--auto`` run, so Phase 0 adds no new I/O path.
``symbol`` / ``refs`` / ``read`` (source-level, via ``block_extractor`` and
``SearchAgent``) follow in Phase 1, gated by the same
``probe_allowed_ops`` allow-list this module already enforces.

Why a text protocol rather than native tool-calling
----------------------------------------------------
``tools/llm_stream.py`` has no ``tools=``/``tool_call`` support, and adding
it would mean a migration across ``api_format = ollama``, the stub servers
and seven ``agents_*.ini`` window profiles.  ``coder.py`` already ships the
same shape as ``CONTEXT_REQUEST:``; this reuses that convention rather than
introducing a second one.

Gated by ``[architect] probe_enabled`` (absent ⇒ ``False`` ⇒ every prompt
is byte-identical to pre-AUTO-P).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

# Recognised on its own line, at the end of an Architect response. Mirrors
# coder.py's existing ``CONTEXT_REQUEST:`` convention rather than introducing a
# second protocol shape.
PROBE_PREFIX = "ARCH_PROBE:"

# Upper bound on ops honoured from a single request — matches the cap
# coder.py::_extract_context_request_prose applies to CONTEXT_REQUEST names.
# A model that asks for 40 symbols has not understood the question; answering
# the first 8 and letting it re-ask is both cheaper and more informative than
# resolving all 40.
_MAX_OPS = 8

# Ops the Phase 0 executor can actually answer. Anything else parses to
# nothing, so an unrecognised op degrades to "not a probe" rather than to a
# probe the executor will silently no-op on.
DEFAULT_ALLOWED_OPS: tuple[str, ...] = ("facts",)

# Appended to the Architect's user message when probing is available. Kept
# short on purpose: the surrounding prompt is already long, and every token
# here competes with the file contents the model is meant to be reviewing.
PROBE_INSTRUCTIONS = (
    "\nIF — and only if — you cannot ground a task because you are missing a "
    "fact about a symbol that is NOT shown above, you may reply with a single "
    "line instead of the JSON array:\n"
    "\n"
    "    ARCH_PROBE: facts <symbol>, facts <other_symbol>\n"
    "\n"
    "You will be re-asked with those facts (signature and contracts) added. "
    "Ask only for symbols you genuinely need and cannot see; a probe costs a "
    "full round, and you get a limited number of them. If you can plan from "
    "what is already above, return the JSON array and do not probe."
)

# Appended instead of PROBE_INSTRUCTIONS on the final call, once the probe
# budget is spent. Without this the model can keep probing forever; with it,
# the harness — not the model — decides that asking is over.
FORCED_SUFFIX = (
    "\nNOTE: the context-probe budget for this batch is spent. No further "
    "ARCH_PROBE requests will be answered. Produce the JSON array now using "
    "only the information above; if some detail is still unknown, propose the "
    "task you CAN ground and leave the rest out rather than guessing."
)


@dataclass(frozen=True)
class ProbeOp:
    """One read-only lookup the Architect asked for."""

    op: str
    arg: str

    def __str__(self) -> str:  # readable in logs and trace params
        return f"{self.op} {self.arg}".strip()


def extract_probe_request(
    text: str,
    *,
    allowed_ops: Sequence[str] = DEFAULT_ALLOWED_OPS,
) -> list[ProbeOp]:
    """Parse a trailing ``ARCH_PROBE:`` line out of *text*.

    Returns the requested ops in order, deduplicated and capped at
    ``_MAX_OPS``, or ``[]`` when *text* carries no well-formed probe request.

    Strictness is the point: a request that names an op outside *allowed_ops*,
    or carries no parseable ``<op> <arg>`` pair at all, returns ``[]`` — which
    the caller reads as "this is not a probe" and routes to its normal
    unparseable-response handling.  Silently honouring half a malformed
    request would spend a round on a lookup the model did not ask for.

    Never raises.  A response containing several ``ARCH_PROBE:`` lines (a
    model that restated itself) contributes all of them, in order, subject to
    the same dedup and cap.
    """
    if not text or not isinstance(text, str):
        return []

    allowed = {str(op).strip().lower() for op in (allowed_ops or ()) if str(op).strip()}
    if not allowed:
        return []

    ops: list[ProbeOp] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.upper().startswith(PROBE_PREFIX):
            continue
        payload = line.split(":", 1)[1]
        for part in payload.split(","):
            item = part.strip().strip(".\"'`")
            if not item:
                continue
            # "<op> <arg>" — split on the FIRST run of whitespace only, so an
            # arg that itself contains a space (a future "read path 10:20"
            # form) survives intact for the executor to reject or handle.
            bits = item.split(None, 1)
            if len(bits) != 2:
                continue
            op = bits[0].strip().lower()
            arg = bits[1].strip().strip(".\"'`")
            if not arg or op not in allowed:
                continue
            ops.append(ProbeOp(op, arg))

    # Deduplicate, preserve order, cap — matches
    # coder.py::_extract_context_request_prose.
    deduped: list[ProbeOp] = list(dict.fromkeys(ops))
    return deduped[:_MAX_OPS]


class ArchProbe:
    """Read-only executor for :class:`ProbeOp` requests.

    Parameters
    ----------
    collect_bridge:
        A :class:`tools.auto.collect_bridge.CollectBridge`, or ``None`` when
        the collect artifact is off/absent.  ``None`` makes this instance
        unusable, which the caller checks via :attr:`usable` before it ever
        offers probing to the model.
    max_chars:
        Cap on a single op's result.  Overflow is hard-truncated with a
        visible notice — no LLM shrink at this stage, which would spend a
        summariser call to save a summariser call.
    max_total_chars:
        Cap on everything this instance has produced across all rounds of one
        batch.  Reached before ``probe_max_rounds`` in practice, and the
        reason a request for eight hot symbols cannot blow the context window.
    """

    def __init__(
        self,
        collect_bridge,
        *,
        max_chars: int = 2000,
        max_total_chars: int = 6000,
    ) -> None:
        self._bridge = collect_bridge
        self._max_chars = max(200, int(max_chars))
        self._max_total_chars = max(self._max_chars, int(max_total_chars))
        self._chars_used = 0

    @property
    def usable(self) -> bool:
        """``True`` only when there is a FRESH collect artifact to answer from.

        Mirrors ``CollectBridge.usable`` — a stale artifact counts as absent,
        exactly as it does everywhere else in the ``--auto`` path.
        """
        return bool(self._bridge is not None and getattr(self._bridge, "usable", False))

    @property
    def chars_used(self) -> int:
        return self._chars_used

    @property
    def budget_exhausted(self) -> bool:
        return self._chars_used >= self._max_total_chars

    def execute(self, ops: Iterable[ProbeOp]) -> str:
        """Run *ops* and return a prompt-ready digest, or ``""``.

        ``""`` means "nothing usable came back" and the caller must treat it
        as budget exhaustion — re-asking with an empty digest would produce
        the identical response and the identical probe, forever.

        Fail-open throughout: an op that misses contributes a ``(not found)``
        line so the model can see its guess was wrong and stop asking for it,
        and an op that raises is logged and skipped.  Neither aborts a batch.
        """
        if not self.usable:
            return ""
        blocks: list[str] = []
        for op in ops or ():
            if self.budget_exhausted:
                logger.info(
                    "ArchProbe: total digest budget (%d chars) reached — "
                    "dropping remaining op(s) starting at %s.",
                    self._max_total_chars, op,
                )
                break
            try:
                body = self._run_one(op)
            except Exception as exc:  # noqa: BLE001 — never abort a batch
                logger.warning("ArchProbe: op %s raised: %s", op, exc)
                continue
            if not body:
                body = "(not found)"
            body = self._cap(body)
            block = f"### {op}\n{body}"
            blocks.append(block)
            self._chars_used += len(block)
        if not blocks:
            return ""
        return "## Probe results\n" + "\n\n".join(blocks)

    # ── op implementations ───────────────────────────────────────────────

    def _run_one(self, op: ProbeOp) -> str:
        if op.op == "facts":
            return self._facts(op.arg)
        # Unreachable while extract_probe_request enforces the allow-list;
        # defensive so a future op added to the parser but not here degrades
        # to "(not found)" rather than to an AttributeError mid-batch.
        logger.warning("ArchProbe: no executor for op %r — skipping.", op.op)
        return ""

    def _facts(self, symbol: str) -> str:
        """Signature + contracts for *symbol*, from the collect model."""
        return self._bridge.pull_symbol(symbol) or ""

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        # Same truncation style ContextBroker._cap and CollectBridge._shrink
        # use, so a truncated block reads consistently to a human in the
        # prompt log regardless of which component produced it.
        keep = self._max_chars
        return text[:keep] + f"\n… [truncated, {len(text) - keep} more chars]"
