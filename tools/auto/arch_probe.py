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
import re
from dataclasses import dataclass
from pathlib import Path
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

# AUTO-P7: lines returned by a `read` with no explicit range. Enough to see a
# function and its neighbours, small enough that a whole-file read cannot
# spend the batch's entire digest budget on one op.
_READ_MAX_LINES = 120

# AUTO-P8: what to do when a probe round is cut short by the digest cap.
# Each entry is (digest-cap multiplier, temperature) for one escalated
# re-ask, applied in order. Modelled on AUTO-H5-ESCALATE-1, which already
# escalates max_tokens/temperature for an unsalvageable response — this is
# the same idea for a response that was fine but could not be *answered* in
# full.
#
# The order is deliberate. Step 1 gives more room at temperature 0, because
# what we need is the SAME request re-issued so the larger budget serves the
# ops that were dropped; a re-roll at higher temperature would ask something
# else and waste the room. Step 2 keeps the room and raises temperature,
# because if step 1 produced a byte-identical loop the determinism is the
# problem, not the budget. Step 3 goes back to temperature 0 with more room
# still, for the case where step 2 broke the loop into a genuinely bigger
# request.
_BUDGET_ESCALATION_LADDER = (
    (2.0, 0.0),
    (2.0, 0.7),
    (4.0, 0.0),
)

# Ops the Phase 0 executor can actually answer. Anything else parses to
# nothing, so an unrecognised op degrades to "not a probe" rather than to a
# probe the executor will silently no-op on.
DEFAULT_ALLOWED_OPS: tuple[str, ...] = ("facts",)

# Appended to the Architect's user message when probing is available. Kept
# short on purpose: the surrounding prompt is already long, and every token
# here competes with the file contents the model is meant to be reviewing.
# AUTO-P4b: the shape rules below are not decoration. In the first two real
# runs the model asked for 19 distinct names and only 2 existed: the rest were
# config keys (`max_attempts_per_task`), module paths (`tools.llm_stream`),
# methods (`InnerLoop.run_task`), and plain guesses at what a helper "should"
# be called (`backoff`, `retry`, `_retry`, `attempt`, `retry_loop`). Collect
# indexes TOP-LEVEL functions and classes only — no methods, no config keys —
# so every one of those was unanswerable by construction, and the model had
# never been told what kind of name it could ask for.
#
# AUTO-P5 then found that telling it was not enough for one whole class of
# request: 7 of the 9 unresolved lookups across two later runs were
# `facts backoff` or `facts retry`, where the model wanted a FILE
# (tools/backoff.py) or a concept. Naming the restriction does not help when
# the thing you need cannot be expressed at all, so `module <path>` was added
# to make that question askable rather than merely forbidden.
PROBE_INSTRUCTIONS = (
    "\nIF — and only if — you cannot ground a task because you are missing a "
    "fact about a symbol that is NOT shown above, you may reply with a single "
    "line instead of the JSON array:\n"
    "\n"
    "    ARCH_PROBE: facts <symbol>, module <path>, read <path>:<a>-<b>\n"
    "\n"
    "`facts <symbol>` — <symbol> must be a top-level function or class name, "
    "spelled exactly as it is defined in the source, for example "
    "`request_completion` or `InnerLoop`. Returns its signature and "
    "contracts.\n"
    "`module <path>` — <path> is a file path as it appears in the repository, "
    "for example `tools/backoff.py`. Returns every top-level name defined "
    "there with its line number and first docstring line. Use this when you "
    "know WHICH FILE you need but not what is in it.\n"
    "`read <path>:<start>-<end>` — the actual source lines. Use this ONLY to "
    "verify a claim you are about to make about code you cannot see — for "
    "example before writing \"this function has a retry loop\". ALWAYS give a "
    "range; `module <path>` hands you the line numbers to ask for, and a "
    "whole-file read spends most of your budget on one op.\n"
    "\n"
    "The following CANNOT be looked up and will come back empty:\n"
    "  - a method or attribute (`InnerLoop.run_task`) — ask for `InnerLoop`\n"
    "  - a config key or setting (`max_attempts_per_task`)\n"
    "  - a concept rather than a real name (`retry`, `retry_loop`) — if you "
    "mean a file, use `module`; if you mean a symbol, you must know its "
    "actual name\n"
    "\n"
    "You will be re-asked with whatever resolves. Ask only for what you "
    "genuinely need and cannot see above; a probe costs a full round and you "
    "get a limited number of them. Re-asking for something that already came "
    "back empty will end your probing, not retry it. If you can plan from "
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
        base_dir=None,
        batch_files=(),
    ) -> None:
        self._bridge = collect_bridge
        # AUTO-P7: `read` is the first op that touches the filesystem rather
        # than the collect artifact, so it needs a root to contain paths
        # against. None disables the op entirely — an unconstrained read is
        # not a degraded feature, it is a path-traversal hole.
        self._base_dir = Path(base_dir).resolve() if base_dir else None
        # AUTO-P7: the files this batch was actually shown. Results sourced
        # from anywhere else get a marker, because Gate-1 rejects a
        # cited_location outside the batch as a hallucinated path — two such
        # rejections in the last measured run, and the count doubled when
        # AUTO-P5 widened out-of-batch knowledge.
        self._batch_files = frozenset(str(f) for f in (batch_files or ()))
        self._max_chars = max(200, int(max_chars))
        self._max_total_chars = max(self._max_chars, int(max_total_chars))
        # AUTO-P4a: per-BATCH, reset by reset() at the top of every batch.
        # Before AUTO-P4a this counter was created once with the instance and
        # never cleared, while the instance itself is memoized on the
        # ClusterReviewer for the whole run — so `max_total_chars` silently
        # behaved as a per-RUN cap. On a long run the probe stopped answering
        # partway through and stayed off, which in the logs is indistinguishable
        # from "the model stopped asking". The digest is appended to ONE batch's
        # prompt, so the context-window budget it protects is per batch.
        self._chars_used = 0
        # AUTO-P8: the configured cap, preserved so raise_budget() can multiply
        # it rather than the current (possibly already-raised) value, and so
        # reset() can restore it between batches.
        self._base_total_chars = self._max_total_chars
        # AUTO-P4b: per-round outcome of the LAST execute() call, so callers
        # can report "symbols found" instead of "digests produced" — the
        # distinction that hid a 60/60 miss rate behind "22 resolved".
        self._last_hits = 0
        self._last_misses = 0
        # AUTO-P5: {op_name: [hits, misses]} for the last execute(). Aggregate
        # hit/miss cannot answer "which op is pulling its weight" once there is
        # more than one op, and that is precisely the question the next
        # scope decision (refs? read?) turns on. Recording it per type costs
        # one dict and makes the decision measurable instead of arguable.
        self._last_by_op: dict = {}
        # AUTO-P8: ops the last execute() never ran because the digest cap was
        # already spent. Before this they were dropped with only a log line —
        # the model was never told, so it planned as though it had been
        # answered. A real run lost `read main.py:100-200` exactly this way.
        self._last_dropped: list = []
        # Never reset — reported only, so a run can still be judged on total
        # probe cost without that total being able to switch the feature off.
        self._run_chars_used = 0

    def set_batch_files(self, files) -> None:
        """AUTO-P7: the files the current batch was shown.

        Separate from the constructor because the ArchProbe instance is
        memoized for the whole run (one CollectBridge) while the batch
        changes on every call — the same asymmetry that made AUTO-P4a's
        per-batch `reset()` necessary.
        """
        self._batch_files = frozenset(str(f) for f in (files or ()))

    def raise_budget(self, factor: float) -> int:
        """AUTO-P8: widen the per-batch digest cap for an escalated re-ask.

        Multiplies the CONFIGURED cap, not the current one, so the ladder's
        multipliers compose predictably (2x then 4x, not 2x then 8x).
        Returns the new cap.
        """
        self._max_total_chars = max(
            self._max_chars, int(self._base_total_chars * float(factor))
        )
        return self._max_total_chars

    @property
    def last_dropped(self) -> list:
        """Ops the last :meth:`execute` did not run — budget already spent."""
        return list(self._last_dropped)

    def reset(self) -> None:
        """Clear the per-batch digest budget and any escalation. Called once
        per batch.

        Deliberately does NOT rebuild the underlying CollectBridge:
        ``make_collect_bridge``'s contract is build-once-per-run, and the
        artifact cannot change mid-run.
        """
        self._chars_used = 0
        # AUTO-P8: an escalation is scoped to one batch. Carrying a raised cap
        # into the next batch would silently re-tune the profile.
        self._max_total_chars = self._base_total_chars
        self._last_dropped = []

    @property
    def usable(self) -> bool:
        """``True`` only when there is a FRESH collect artifact to answer from.

        Mirrors ``CollectBridge.usable`` — a stale artifact counts as absent,
        exactly as it does everywhere else in the ``--auto`` path.
        """
        # AUTO-P7: `read` answers from the working tree, not the artifact, so
        # a base_dir alone makes this probe useful even with no collect model.
        # Before this the whole probe went dark when the artifact was missing,
        # which would have silently disabled the one op that never needed it.
        # Each op still guards its own backing source independently.
        return bool(self._bridge_usable or self._base_dir is not None)

    @property
    def _bridge_usable(self) -> bool:
        return bool(self._bridge is not None and getattr(self._bridge, "usable", False))

    @property
    def chars_used(self) -> int:
        """Digest characters produced for the CURRENT batch."""
        return self._chars_used

    @property
    def last_hits(self) -> int:
        """Ops the last :meth:`execute` actually resolved."""
        return self._last_hits

    @property
    def last_misses(self) -> int:
        """Ops the last :meth:`execute` could not resolve."""
        return self._last_misses

    @property
    def last_by_op(self) -> dict:
        """AUTO-P5: ``{op_name: [hits, misses]}`` for the last execute()."""
        return dict(self._last_by_op)

    def last_by_op_str(self) -> str:
        """`last_by_op` as a trace-friendly string: ``facts=3/1 module=1/0``
        (hits/misses). Flat text because agent_trace stringifies params
        anyway, and a parseable one-liner beats a repr of a dict."""
        return " ".join(
            f"{op}={h}/{m}" for op, (h, m) in sorted(self._last_by_op.items())
        )

    @property
    def run_chars_used(self) -> int:
        """Digest characters produced across the whole run. Observability
        only — never gates anything, unlike :attr:`chars_used`."""
        return self._run_chars_used

    @property
    def budget_exhausted(self) -> bool:
        return self._chars_used >= self._max_total_chars

    def execute(self, ops: Iterable[ProbeOp]) -> str:
        """Run *ops* and return a prompt-ready digest, or ``""``.

        ``""`` means "nothing usable came back" and the caller must treat it
        as budget exhaustion — re-asking with an empty digest would produce
        the identical response and the identical probe, forever.

        AUTO-P4b: a round in which EVERY op missed now returns ``""`` as
        well.  It previously returned a digest made entirely of
        ``(not found)`` lines, on the theory (AC-P1E-3) that the model would
        read them, learn its guesses were wrong, and stop asking.  It does
        not.  A real run showed one batch asking ``facts backoff`` five
        rounds running, receiving ``(not found)`` each time, and only
        stopping at the round cap — five LLM calls spent re-asking a question
        whose answer had not changed.  An all-miss round carries no
        information the next round can act on, so it is treated as the
        nothing it is.

        A round with even one hit still reports the misses alongside it: in
        that case the digest does carry new information, and naming what was
        not found stops the model re-requesting it.

        Fail-open throughout: an op that raises is logged and skipped, and
        nothing here aborts a batch.
        """
        if not self.usable:
            return ""
        blocks: list[str] = []
        _done: list = []
        _hits = 0
        self._last_dropped = []
        self._last_hits = 0
        self._last_misses = 0
        self._last_by_op = {}
        for op in ops or ():
            if self.budget_exhausted:
                # AUTO-P8: remember which ops went unanswered so execute() can
                # SAY SO in the digest. Logging alone left the model believing
                # its whole request had been served.
                self._last_dropped = [o for o in (ops or ()) if o not in _done]
                logger.info(
                    "ArchProbe: total digest budget (%d chars) reached — "
                    "dropping %d remaining op(s) starting at %s.",
                    self._max_total_chars, len(self._last_dropped), op,
                )
                break
            _done.append(op)
            try:
                body = self._run_one(op)
            except Exception as exc:  # noqa: BLE001 — never abort a batch
                logger.warning("ArchProbe: op %s raised: %s", op, exc)
                continue
            _tally = self._last_by_op.setdefault(op.op, [0, 0])
            if not body:
                body = "(not found)"
                self._last_misses += 1
                _tally[1] += 1
            else:
                _hits += 1
                self._last_hits += 1
                _tally[0] += 1
            body = self._cap(body)
            block = f"### {op}\n{self._out_of_batch_note(body)}{body}"
            blocks.append(block)
            self._chars_used += len(block)
            self._run_chars_used += len(block)
        if not blocks:
            return ""
        if _hits == 0:
            # AUTO-P4b: all-miss round — see the docstring. The characters are
            # still charged against the batch budget: the lookups happened, and
            # a model that burns its digest allowance on names that do not
            # exist should not get an unlimited supply of retries for free.
            logger.info(
                "ArchProbe: all %d op(s) missed (%s) — returning an empty "
                "digest so the caller stops re-asking.",
                len(blocks), ", ".join(str(op) for op in ops or ()),
            )
            return ""
        out = "## Probe results\n" + "\n\n".join(blocks)
        if self._last_dropped:
            # The model must be able to see the gap. Otherwise it plans as if
            # the dropped op had come back empty, which is a different and
            # much more misleading fact than "not answered yet".
            out += (
                "\n\n[NOT ANSWERED — the digest budget for this batch ran out "
                "before these: "
                + ", ".join(str(o) for o in self._last_dropped)
                + ". They were not looked up and are NOT known to be absent. "
                "Ask again for the one you most need.]"
            )
        return out

    # ── op implementations ───────────────────────────────────────────────

    def _run_one(self, op: ProbeOp) -> str:
        if op.op == "facts":
            return self._facts(op.arg)
        if op.op == "module":
            return self._module(op.arg)
        if op.op == "read":
            return self._read(op.arg)
        # Unreachable while extract_probe_request enforces the allow-list;
        # defensive so a future op added to the parser but not here degrades
        # to "(not found)" rather than to an AttributeError mid-batch.
        logger.warning("ArchProbe: no executor for op %r — skipping.", op.op)
        return ""

    def _facts(self, symbol: str) -> str:
        """Signature + contracts for *symbol*, from the collect model."""
        if not self._bridge_usable:
            return ""
        return self._bridge.pull_symbol(symbol) or ""

    def _module(self, module_ref: str) -> str:
        """AUTO-P5: top-level symbol inventory for *module_ref*.

        Answers "what is in this file", which `facts` structurally cannot.
        Same budget, same truncation, same miss accounting — the only new
        thing is the question it can answer.
        """
        if not self._bridge_usable:
            return ""
        return self._bridge.module_symbols(module_ref) or ""

    def _read(self, arg: str) -> str:
        """AUTO-P7: actual source lines for ``<path>`` or ``<path>:<a>-<b>``.

        The op the measurements asked for. Across six runs the largest
        Gate-1 rejection bucket has been "hallucinated the premise" — the
        Architect asserting a retry loop exists in a file that has none, 34
        of 52 rejections in the last run. `facts` returns a signature,
        `module` returns a name list; neither returns a single line of code,
        so nothing in the protocol let the model check a claim before making
        it.

        Unlike the other two ops this reads the WORKING TREE, not the collect
        artifact, which brings two obligations the others never had:

        * **Containment.** Anything resolving outside ``base_dir`` is a miss,
          never a read. Same shape ``gate1_filter`` uses for cited paths.
        * **Freshness.** When this disagrees with `facts`/`module`, this is
          right — the artifact can lag the tree.

        Misses (absent, outside root, a directory, binary, undecodable)
        return ``""`` and are counted as misses like any other. Never raises.
        """
        if self._base_dir is None:
            logger.debug("ArchProbe: read requested but no base_dir — skipping.")
            return ""
        raw = (arg or "").strip().strip("`\"'")
        if not raw:
            return ""

        rel, start, end = self._parse_read_arg(raw)
        if not rel:
            return ""

        try:
            target = (self._base_dir / rel).resolve()
            # Containment BEFORE any I/O. `..` segments, absolute paths and
            # symlinks out of the tree all collapse here.
            target.relative_to(self._base_dir)
        except (ValueError, OSError) as exc:
            logger.warning(
                "ArchProbe: read %r rejected — resolves outside the repo (%s).",
                raw, exc,
            )
            return ""

        try:
            if not target.is_file():
                return ""
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("ArchProbe: read %r unreadable: %s", raw, exc)
            return ""

        lines = text.splitlines()
        if not lines:
            return f"file: {rel}\n(empty file)"

        if start is None:
            lo, hi = 1, min(len(lines), _READ_MAX_LINES)
        else:
            lo = max(1, start)
            hi = min(len(lines), end if end is not None else start)
            if hi < lo:
                return ""

        body = [f"{n:>5}  {lines[n - 1]}" for n in range(lo, hi + 1)]
        header = f"file: {rel}  lines {lo}-{hi} of {len(lines)}"
        omitted = len(lines) - hi
        if omitted > 0 and start is None:
            # Say so. A silently head-truncated file reads as a complete one,
            # which is precisely the kind of confident-but-wrong input this
            # op exists to prevent.
            body.append(
                f"      … {omitted} more line(s) not shown — ask for a range, "
                f"e.g. read {rel}:{hi + 1}-{min(len(lines), hi + _READ_MAX_LINES)}"
            )
        return header + "\n" + "\n".join(body)

    @staticmethod
    def _parse_read_arg(raw: str) -> tuple:
        """Split ``path`` / ``path:12-40`` / ``path:12`` into parts.

        Returns ``(rel, start, end)`` with ``rel=""`` when unparseable. A
        Windows-style ``C:`` or any colon that is not followed by digits is
        treated as part of the path, not as a range separator.
        """
        rel, start, end = raw, None, None
        if ":" in raw:
            head, _, tail = raw.rpartition(":")
            m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", tail.strip())
            if head and m:
                rel = head
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else start
        return rel.strip(), start, end

    def _out_of_batch_note(self, block: str) -> str:
        """A one-line warning when a result names a file outside this batch.

        AUTO-P7. The probe hands the Architect knowledge of files it was
        never shown; Gate-1 rejects any `cited_location.file` or
        `target_files` entry outside the batch as a hallucinated path. The
        two rules contradict each other, and the rejection count doubled
        between the last two runs as `module` widened out-of-batch
        knowledge. Marking the result is the cheap half of the fix — it does
        not teach Gate-1 anything, it stops the model citing what it cannot
        cite.
        """
        if not self._batch_files:
            return ""
        m = re.search(r"^(?:file|module): (\S+)", block, re.M)
        if not m:
            return ""
        path = m.group(1).split(":", 1)[0]
        if path in self._batch_files:
            return ""
        return (
            "[NOT IN YOUR BATCH — read-only context. Do NOT put this path in "
            "target_files or cited_location; Gate-1 will reject it.]\n"
        )

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        # Same truncation style ContextBroker._cap and CollectBridge._shrink
        # use, so a truncated block reads consistently to a human in the
        # prompt log regardless of which component produced it.
        keep = self._max_chars
        return text[:keep] + f"\n… [truncated, {len(text) - keep} more chars]"
