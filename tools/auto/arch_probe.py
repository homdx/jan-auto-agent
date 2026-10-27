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
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

logger = logging.getLogger(__name__)

# The probe line, on its own line at the end of an Architect response (same convention as coder.py's CONTEXT_REQUEST).
PROBE_PREFIX = "ARCH_PROBE:"

# Max ops honoured per request; a model asking for dozens has not understood the question.
_MAX_OPS = 8

# Lines returned by a read with no range: enough for a function and its neighbours, not a whole file (AUTO-P7).
_READ_MAX_LINES = 120

# Digest-cap escalation: each rung is (digest-cap multiplier, temperature); rung 1 keeps temperature 0 so the same request gets a larger budget (AUTO-P8).
_BUDGET_ESCALATION_LADDER = (
    (2.0, 0.0),
    (2.0, 0.7),
    (4.0, 0.0),
)

# Budget-only ladder, used once a working cap has been measured (AUTO-P9).
_BUDGET_ONLY_LADDER = (
    (1.5, 0.0),
    (2.5, 0.0),
    (4.0, 0.0),
)

# Ops the Phase 0 executor can answer; an unknown op parses to nothing rather than a probe that silently no-ops.
DEFAULT_ALLOWED_OPS: tuple[str, ...] = ("facts",)

# Prompt text added to the Architect when probing is on: short, with the shape rules that stopped unanswerable names (AUTO-P4b, P5, F1-followup).
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
    "get a limited number of them. Re-asking for anything already shown "
    "above — empty OR answered — will end your probing, not repeat it; an "
    "answer outside your batch cannot become citable no matter how many "
    "times you ask for it. If you can plan from what is already above, "
    "return the JSON array and do not probe.\n"
    "\n"
    "If you do not KNOW a symbol's exact name, do not guess it. Ask "
    "`module <path>` first — it lists every top-level name in that file "
    "with its line number — then ask `facts` for one of the names it "
    "returned."
)

# Used instead of PROBE_INSTRUCTIONS once the probe budget is spent, so the harness, not the model, ends the probing.
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


@dataclass(frozen=True)
class ProbeOpTally:
    """One op's hit/miss tally from the last :meth:`ArchProbe.execute` call.

    AUTO-P5-v2: ``last_by_op`` used to hand out ``dict(self._last_by_op)`` —
    a shallow copy whose values were the exact ``[hits, misses]`` lists the
    tally itself mutates via ``setdefault``. Copying the *dict* left every
    *value* aliased, so ``probe.last_by_op["facts"][0] += 1`` silently
    corrupted the probe's own bookkeeping (the same shape of bug fixed for
    ``StateStore`` in commit ``4796e0a``). A frozen row cannot be written to
    at all, so there is nothing left to alias.
    """

    op: str
    hits: int
    misses: int

    def __str__(self) -> str:  # mirrors ProbeOp; used by last_by_op_str
        return f"{self.op}={self.hits}/{self.misses}"


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
            # Split on the first whitespace only, so an argument that contains a space survives for the executor to judge.
            bits = item.split(None, 1)
            if len(bits) != 2:
                continue
            op = bits[0].strip().lower()
            arg = bits[1].strip().strip(".\"'`")
            if not arg or op not in allowed:
                continue
            ops.append(ProbeOp(op, arg))

    # Deduplicates and caps the ops in order, as coder.py does.
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
    memo_max_entries:
        AUTO-F1: bound on the run-level miss memo (below).  Oldest entry is
        evicted first once the cap is reached, so a pathological run cannot
        grow it without limit.
    """

    def __init__(
        self,
        collect_bridge,
        *,
        max_chars: int = 2000,
        max_total_chars: int = 6000,
        base_dir=None,
        batch_files=(),
        memo_max_entries: int = 200,
    ) -> None:
        self._bridge = collect_bridge
        # read is the first op that touches the filesystem, so it needs a root; without one the op is disabled, not unconstrained (AUTO-P7).
        self._base_dir = Path(base_dir).resolve() if base_dir else None
        # Results from outside the batch get a marker, since Gate 1 rejects citations outside the batch (AUTO-P7).
        self._batch_files = frozenset(str(f) for f in (batch_files or ()))
        self._max_chars = max(200, int(max_chars))
        self._max_total_chars = max(self._max_chars, int(max_total_chars))
        # Per-batch digest budget: reset() clears it, since the digest is appended to one batch's prompt (AUTO-P4a).
        self._chars_used = 0
        # The configured cap, kept so raise_budget() multiplies the base and reset() can restore it (AUTO-P8).
        self._base_total_chars = self._max_total_chars
        # Symbols found by the last execute(), not digests produced (AUTO-P4b).
        self._last_hits = 0
        self._last_misses = 0
        # Per-op hits and misses for the last execute(), so each op's value is measurable (AUTO-P5).
        self._last_by_op: dict = {}
        # Ops the last execute() skipped because the cap was spent; reported to the model so it does not plan as if answered (AUTO-P8).
        self._last_dropped: list = []
        # Cost of each complete round across the run, to seed later batches; truncated rounds are excluded (AUTO-P9).
        self._round_costs: list = []
        self._batch_floor = self._max_total_chars
        # AUTO-P11: hard ceiling from num_ctx; 0 = unset.
        self._window_budget = 0
        self._window_warned = False
        self._warmup = 3
        self._headroom = 2.0
        self._seed_ceiling = 0
        # Never reset, so a run is judged on total probe cost without that total switching the feature off.
        self._run_chars_used = 0
        # Run-level miss memo, kept across batches because the artifact is immutable for the run (AUTO-F1).
        self._miss_memo: "OrderedDict[tuple[str, str], int]" = OrderedDict()
        self._memo_max_entries = max(1, int(memo_max_entries))
        # Memo hits per execute() and per run, reported so the saving is visible (AUTO-F1).
        self._last_memo_hits = 0
        self._run_memo_hits = 0
        # Whether a facts ask was informed by an earlier module hit in this batch, per batch (AUTO-F4).
        self._informed_symbols: set = set()
        # [hits, asks] for facts; a memo hit is never a hit (AUTO-F4).
        self._informed_facts = [0, 0]
        self._blind_facts = [0, 0]

    def set_batch_files(self, files) -> None:
        """AUTO-P7: the files the current batch was shown.

        Separate from the constructor because the ArchProbe instance is
        memoized for the whole run (one CollectBridge) while the batch
        changes on every call — the same asymmetry that made AUTO-P4a's
        per-batch `reset()` necessary.
        """
        self._batch_files = frozenset(str(f) for f in (files or ()))

    def configure_learning(
        self, *, warmup: int, headroom: float, ceiling: int,
    ) -> None:
        """AUTO-P9 tuning, passed down from ``[architect]`` config."""
        self._warmup = max(0, int(warmup))
        self._headroom = max(1.0, float(headroom))
        # Zero means no explicit ceiling; it falls back to 4x the configured cap.
        self._seed_ceiling = int(ceiling) or (self._base_total_chars * 4)

    def set_window_budget(self, max_chars: int) -> None:
        """AUTO-P11: absolute ceiling in digest chars, derived from num_ctx.

        Everything before this validated the CONFIGURED cap and nothing else.
        `test_probe_budget_fits_the_window` checks that
        `probe_max_total_chars` fits the window — but AUTO-P9 can reseed it to
        4x that, and AUTO-P8/P10 then multiply the batch floor by up to 4.0.
        Nothing checked the product. A measured run escalated to **56 737
        chars**, roughly 16 000 tokens of digest, against a cap that had been
        validated at 10 000.

        On a 128k or 1M window that is merely wasteful. On a 32k profile it
        would consume most of the room the file contents need, and on 8k it
        would not fit at all — and the failure mode is a silently truncated
        prompt, not an error.

        0 disables the ceiling (no num_ctx configured — the server decides).
        """
        self._window_budget = max(0, int(max_chars))

    def _clamp_to_window(self, value: int, what: str) -> int:
        if self._window_budget and value > self._window_budget:
            if not self._window_warned:
                self._window_warned = True
                logger.warning(
                    "ArchProbe: %s of %d chars exceeds what this context "
                    "window can hold (%d) — clamped. Lower "
                    "probe_budget_headroom, or raise num_ctx.",
                    what, value, self._window_budget,
                )
            return self._window_budget
        return value

    @property
    def seeded_cap(self) -> "int | None":
        """The cap later batches should START at, or ``None`` during warm-up.

        Median, not mean: a single 23k-char round should not drag the estimate
        up for every batch after it, and the measured distribution is skewed
        (median 9 582, p90 20 028 on the run that motivated this).

        The headroom multiplier is what makes the number useful rather than
        merely descriptive — seeding at exactly the median guarantees half of
        all rounds still overflow on the first try, which is the situation
        this is meant to end.

        The 2.0 default was measured, not guessed. Replaying the 216 completed
        rounds of the run that motivated this against each candidate::

            headroom   cap     overflow   escalations avoided
                 1.0   10000        48%     0   (the status quo)
                 1.5   14373        25%    ~26
                 2.0   19165        12%    ~42
                 2.5   23956         0%    ~57

        2.5 removes every escalation but buys a 24k-char digest for every
        batch, most of which need a third of that; 2.0 keeps the prompt
        smaller and leaves the ladder to handle the 12% tail, which is
        exactly what a ladder is for.
        """
        if self._warmup <= 0 or len(self._round_costs) < self._warmup:
            return None
        costs = sorted(self._round_costs)
        mid = len(costs) // 2
        median = (costs[mid] if len(costs) % 2
                  else (costs[mid - 1] + costs[mid]) / 2)
        want = int(median * self._headroom)
        capped = max(self._base_total_chars, min(want, self._seed_ceiling))
        return self._clamp_to_window(capped, "learned digest cap")

    @property
    def has_seeded_cap(self) -> bool:
        return self.seeded_cap is not None

    def raise_budget(self, factor: float) -> int:
        """AUTO-P8: widen the per-batch digest cap for an escalated re-ask.

        Multiplies the cap this BATCH started at, not the current one, so the
        ladder's multipliers compose predictably (1.5x then 2.5x, not 1.5x
        then 3.75x).

        AUTO-P10 — this used to multiply the *configured* cap. That was
        correct while every batch started there, and became a bug the moment
        AUTO-P9 made batches start at a learned cap instead: with a
        configured 10 000, a seeded 16 624 and a 1.5x first rung, escalation
        produced 15 000 — **less room than the batch already had**. The
        measured run shows the consequence exactly: 18 step-1 escalations,
        and all 18 went on to need step 2, because step 1 could not possibly
        have helped.

        The escalation must always widen. The floor below is not defensive
        padding; it is the invariant.
        """
        widened = self._clamp_to_window(
            int(self._batch_floor * float(factor)), "escalated digest cap"
        )
        self._max_total_chars = max(
            self._max_chars, self._max_total_chars, widened
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
        # A batch starts from the seeded cap when one is known, else the configured floor; escalations stay within the batch (AUTO-P8, P9).
        self._max_total_chars = self.seeded_cap or self._base_total_chars
        # The batch's starting cap; raise_budget multiplies this, so an escalation is relative to where the batch began (AUTO-P10).
        self._batch_floor = self._max_total_chars
        self._last_dropped = []
        # "Informed" is per batch, so it is cleared on each reset (AUTO-F4).
        self._informed_symbols = set()
        self._informed_facts = [0, 0]
        self._blind_facts = [0, 0]

    @property
    def usable(self) -> bool:
        """``True`` only when there is a FRESH collect artifact to answer from.

        Mirrors ``CollectBridge.usable`` — a stale artifact counts as absent,
        exactly as it does everywhere else in the ``--auto`` path.
        """
        # read works from the working tree, so the probe is useful with a base_dir even without a collect model (AUTO-P7).
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
    def last_by_op(self) -> tuple["ProbeOpTally", ...]:
        """AUTO-P5-v2: one immutable :class:`ProbeOpTally` row per op from
        the last :meth:`execute` call, ordered by op name.

        Used to return ``dict(self._last_by_op)`` — a *block* of shared
        mutable state: the dict copy was shallow, so its ``[hits, misses]``
        values were the same list objects ``_resolve_op`` still tallies
        into, and a caller mutating one corrupted the probe itself (see
        :class:`ProbeOpTally`). Replacing that block with an ordered tuple
        of frozen rows removes the aliasing outright rather than copying
        around it, and sorting here — what :meth:`last_by_op_str` already
        did — means both accessors now agree on one order instead of one
        being insertion-order and the other sorted.
        """
        return tuple(
            ProbeOpTally(op, h, m) for op, (h, m) in sorted(self._last_by_op.items())
        )

    def last_by_op_str(self) -> str:
        """`last_by_op` as a trace-friendly string: ``facts=3/1 module=1/0``
        (hits/misses). Flat text because agent_trace stringifies params
        anyway, and a parseable one-liner beats a repr of a dict."""
        return " ".join(str(row) for row in self.last_by_op)

    @property
    def run_chars_used(self) -> int:
        """Digest characters produced across the whole run. Observability
        only — never gates anything, unlike :attr:`chars_used`."""
        return self._run_chars_used

    @property
    def last_memo_hits(self) -> int:
        """AUTO-F1: repeated misses the last :meth:`execute` answered from the
        memo — no bridge lookup performed. Counted in :attr:`last_misses`
        too (AC-F1-5); this is the breakout that makes the saving visible."""
        return self._last_memo_hits

    @property
    def run_memo_hits(self) -> int:
        """AUTO-F1: memo hits across the whole run. Observability only, like
        :attr:`run_chars_used` — never gates anything."""
        return self._run_memo_hits

    # AUTO-F4 flow: a facts ask is informed (a module hit taught the name this batch) or blind; probe_declined keeps the latest cumulative pair (F4a/F4b).

    @property
    def informed_facts(self) -> tuple:
        """AUTO-F4: ``(hits, asks)`` for `facts` ops this BATCH whose symbol
        was named in a `module` hit earlier in the same batch. Accumulates
        across every :meth:`execute` call since the last :meth:`reset` —
        unlike :attr:`last_hits`, this is not overwritten each round, because
        "informed" is a property of the whole batch's sequencing, not of one
        round in isolation."""
        return (self._informed_facts[0], self._informed_facts[1])

    @property
    def blind_facts(self) -> tuple:
        """AUTO-F4: ``(hits, asks)`` for `facts` ops this batch whose symbol
        was NOT named in any `module` hit earlier in the same batch — a
        guess. Same accumulation rule as :attr:`informed_facts`."""
        return (self._blind_facts[0], self._blind_facts[1])

    def facts_informed_str(self) -> str:
        """`informed_facts` / `blind_facts` as a trace-friendly string:
        ``informed=3/4 blind=1/9`` (hits/asks) — empty when no `facts` op has
        been asked this batch, mirroring :meth:`last_by_op_str`."""
        _i_h, _i_a = self.informed_facts
        _b_h, _b_a = self.blind_facts
        if not _i_a and not _b_a:
            return ""
        return f"informed={_i_h}/{_i_a} blind={_b_h}/{_b_a}"

    @property
    def current_cap(self) -> int:
        """The digest cap actually in force for this batch, after seeding and
        any escalation. AUTO-P11 — the logs used to report the configured
        value here, which made a working escalation read as a broken one."""
        return self._max_total_chars

    @property
    def budget_exhausted(self) -> bool:
        return self._chars_used >= self._max_total_chars

    def execute(self, ops: Iterable[ProbeOp], *, unbounded: bool = False) -> str:
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

        unbounded:
            AUTO-P12 — skip both the per-op (``max_chars``) and total-digest
            (``max_total_chars``) caps. Meant for exactly one call per batch:
            the single request still pending when ``probe_max_rounds`` is
            reached, which the round-cap branch would otherwise discard
            outright rather than throttle like every other round. Chars are
            still tallied in ``chars_used``/``run_chars_used`` for
            observability, but never gate which ops run or how much of a
            result is kept, and a call made this way is never recorded into
            ``_round_costs`` — an uncapped round is not an honest sample of
            what a normal budget needs.
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
        self._last_memo_hits = 0
        for op in ops or ():
            if not unbounded and self.budget_exhausted:
                # AUTO-P8: remember the dropped ops, so execute() can tell the model they were not answered.
                self._last_dropped = [o for o in (ops or ()) if o not in _done]
                logger.info(
                    "ArchProbe: total digest budget (%d chars) reached — "
                    "dropping %d remaining op(s) starting at %s.",
                    self._max_total_chars, len(self._last_dropped), op,
                )
                break
            _done.append(op)
            try:
                block, is_hit = self._resolve_op(op, unbounded=unbounded)
            except Exception as exc:  # noqa: BLE001 — never abort a batch
                logger.warning("ArchProbe: op %s raised: %s", op, exc)
                continue
            if is_hit:
                _hits += 1
            blocks.append(block)
            self._chars_used += len(block)
            self._run_chars_used += len(block)
        if not blocks:
            return ""
        if _hits == 0 and self._last_memo_hits == 0:
            # An all-miss round still counts its characters; a round that is all memo hits is exempt (AUTO-F1).
            logger.info(
                "ArchProbe: all %d op(s) missed (%s) — returning an empty "
                "digest so the caller stops re-asking.",
                len(blocks), ", ".join(str(op) for op in ops or ()),
            )
            return ""
        out = "## Probe results\n" + "\n\n".join(blocks)
        if not unbounded and not self._last_dropped:
            # A round that served every request is the honest sample of the room it needs (AUTO-P9).
            self._round_costs.append(self._chars_used)
        if self._last_dropped:
            # Tell the model about dropped ops: a dropped op is not the same as an empty answer.
            out += (
                "\n\n[NOT ANSWERED — the digest budget for this batch ran out "
                "before these: "
                + ", ".join(str(o) for o in self._last_dropped)
                + ". They were not looked up and are NOT known to be absent. "
                "Ask again for the one you most need.]"
            )
        return out

    # ── AUTO-F1: run-level miss memo ─────────────────────────────────────

    def _resolve_op(self, op: ProbeOp, *, unbounded: bool = False) -> tuple:
        """Resolve one op to a ready-to-append digest block.

        Returns ``(block, is_hit)``. For a ``facts`` op, checks the
        run-level miss memo BEFORE touching the bridge: a repeated miss
        for the same symbol is answered instantly from the memo, at zero
        cost, with a digest line that escalates with the repetition count
        (AC-F1-1, AC-F1-2). A miss seen for the first time falls through
        to a real lookup and, if it misses, is memoised for next time; a
        hit is never memoised (AC-F1-7).

        AUTO-F1 follow-up: scoped to ``facts`` only. The epic's own
        non-goals rule out `module`/`read` ("No change to `module` or
        `read`. They work.") — both already resolve at ~95%+, and the
        escalating message below is written for a symbol name. Applied
        indiscriminately to every op, a live run produced exactly the
        wrong claim for a `read` miss: `` `tools/auto/inner_loop.py:254-
        350` is not a symbol in this repository `` for what was actually
        just a bad line range — a path is not a symbol, and telling the
        model to use `module <path>` when it was already using `read` on
        a path is not useful, it is confusing.
        """
        _tally = self._last_by_op.setdefault(op.op, [0, 0])
        memo_key = (op.op, op.arg) if op.op == "facts" else None

        # AUTO-F4: bookkeeping only; a facts ask is tallied informed or blind when seen, and only a bridge hit counts as a hit.
        _facts_tally = None
        if op.op == "facts":
            _facts_tally = (
                self._informed_facts
                if self._arg_seen_via_module(op.arg)
                else self._blind_facts
            )
            _facts_tally[1] += 1

        if memo_key is not None and memo_key in self._miss_memo:
            self._miss_memo[memo_key] += 1
            body = self._memo_miss_body(op, self._miss_memo[memo_key])
            # A memo hit counts as a miss, so the memo does not flatter the hit rate (AC-F1-5).
            self._last_misses += 1
            self._last_memo_hits += 1
            self._run_memo_hits += 1
            _tally[1] += 1
            return self._format_block(op, body, unbounded=unbounded), False

        body = self._run_one(op)
        if not body:
            body = "(not found)"
            self._last_misses += 1
            _tally[1] += 1
            if memo_key is not None:
                self._remember_miss(memo_key)
            is_hit = False
        else:
            self._last_hits += 1
            _tally[0] += 1
            is_hit = True
            if _facts_tally is not None:
                _facts_tally[0] += 1
            elif op.op == "module":
                # AUTO-F4: a module hit teaches the informed set, from the capped text the model actually sees.
                self._learn_module_names(body if unbounded else self._cap(body))
        return self._format_block(op, body, unbounded=unbounded), is_hit

    def _arg_seen_via_module(self, arg: str) -> bool:
        """AUTO-F4: was *arg* (a `facts` symbol) named in a `module` hit
        earlier in this batch? Exact match only — `module`'s inventory lists
        bare top-level names, and a `facts` ask that does not match one of
        them exactly is exactly the guess this measurement exists to catch,
        not a near-miss to be generous about (that is AUTO-F2's job, not
        this story's)."""
        name = (arg or "").strip().strip("`\"'")
        return bool(name) and name in self._informed_symbols

    # A symbol line from collect's module block: two-space indent, then the name.
    _MODULE_NAME_RE = re.compile(r"^  (\w+)", re.M)

    def _learn_module_names(self, body: str) -> None:
        """AUTO-F4: record every top-level name a `module` hit listed, so a
        later `facts` ask for one of them in this same batch counts as
        informed. Additive only — never removes a name, and a name learned
        this batch is forgotten at the next :meth:`reset` along with
        everything else `module` could not have told a fresh batch."""
        for m in self._MODULE_NAME_RE.finditer(body or ""):
            self._informed_symbols.add(m.group(1))

    def _remember_miss(self, memo_key: tuple) -> None:
        """AC-F1-8: record a first-time miss, evicting the oldest entry first
        once the memo is at capacity so a pathological run cannot grow it
        without limit. ``OrderedDict`` preserves insertion order, so the
        first item is always the oldest miss still on record."""
        if len(self._miss_memo) >= self._memo_max_entries:
            self._miss_memo.popitem(last=False)
        self._miss_memo[memo_key] = 1

    @staticmethod
    def _memo_miss_body(op: ProbeOp, count: int) -> str:
        """AC-F1-2: the escalating message for a repeated miss — the model is
        told THIS SPECIFIC name is dead, rather than getting the identical
        ``(not found)`` it has already ignored ``count`` times."""
        plural = "" if count == 1 else "s"
        return (
            f"(not found — already looked up {count} time{plural} this run, "
            f"still absent. `{op.arg}` is not a symbol in this repository. "
            "If you mean a file, use `module <path>`; if you mean a "
            "concept, name a real function.)"
        )

    def _format_block(self, op: ProbeOp, body: str, *, unbounded: bool = False) -> str:
        capped = body if unbounded else self._cap(body)
        return f"### {op}\n{self._out_of_batch_note(capped)}{capped}"

    # ── op implementations ───────────────────────────────────────────────

    def _run_one(self, op: ProbeOp) -> str:
        if op.op == "facts":
            return self._facts(op.arg)
        if op.op == "module":
            return self._module(op.arg)
        if op.op == "read":
            return self._read(op.arg)
        # Unreachable while the allow-list holds; defensive, so a new op without an executor degrades to not found.
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

        Misses (absent, outside root, a directory) return ``""`` and are
        counted as misses like any other. Never raises.

        A file that exists but is not valid UTF-8 is NOT a miss: it is read
        with ``errors="replace"``, so undecodable bytes come back as U+FFFD
        and the file is still readable (B7).
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
            # Containment is checked before any I/O; .., absolute paths and symlinks out are refused.
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
            # errors="replace" for source reads, as elsewhere: a stray byte is not the same as a missing file (B7).
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
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
            # A head-truncated file says so, so it is not read as complete.
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
            "target_files or cited_location; Gate-1 will reject it. You "
            "already have everything this returns above — asking again will "
            "not make it citable, it will end your probing instead.]\n"
        )

    def _cap(self, text: str) -> str:
        if len(text) <= self._max_chars:
            return text
        # Same truncation style as ContextBroker and CollectBridge, so truncated blocks read the same everywhere.
        keep = self._max_chars
        return text[:keep] + f"\n… [truncated, {len(text) - keep} more chars]"
