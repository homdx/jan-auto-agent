"""tools/auto/collect_bridge.py — COLLECT-24: wires the `collect` structural
artifact into the `--auto` pipeline.

Three responsibilities, all opt-in via `[collect] use_in_auto` /
`use_in_doc` (unchanged flag from COLLECT-23):

1. **Static per-task context** — `CollectBridge.context_for(target_file)`
   returns the same COLLECT-23 block (`build_collect_context_block`) but
   budget-aware: if the raw block exceeds `max_context_chars`, it is
   shrunk via an LLM call (reusing the Pass B summarizer model/config —
   `tools.collect.summarizer.make_summarizer_call`) before falling back to
   a hard character truncation if that call fails or is unavailable.

2. **Pull-model symbol resolution** — `CollectBridge.pull_symbol(name)`
   answers a `context_request`/`missing_context` symbol name the coder
   asked for, the SAME way `tools.auto.context_broker.ContextBroker`
   already answers such requests from source code — except this answers
   from the collect model's structural facts (signature + contracts),
   which is cheaper than a project-wide file scan and works even when the
   symbol lives in a file the model hasn't seen. `ContextBroker` tries
   this as an additional pass (Pass 3) after its existing code-search
   passes, so pull-model resolution now draws on BOTH sources uniformly
   through the same `resolve()`/`fetch()` call.

3. **Staleness fallback (simple, per product decision)** — the model is
   only ever consulted when `status == "fresh"`. `"stale"` is treated
   exactly like `"absent"` here: no LLM check, no special-cased retry —
   the task just proceeds through the standard `--auto` path for that
   file/function, unchanged from pre-COLLECT-24 behaviour. `staleness`
   in `agents.ini` still controls whether `tools.collect.loader.load()`
   itself rebuilds (`refresh`), warns (`warn`), or treats stale as absent
   (`ignore`) — this module never triggers a rebuild on its own; it only
   decides whether to USE whatever `load()` handed back.
"""

from __future__ import annotations

import configparser
import logging
from typing import Optional

from tools.auto.context_assembler import build_collect_context_block

logger = logging.getLogger(__name__)

# AUTO-CR-23-2-style default budget for the collect block injected into a
# single task's prompt. Overridable via [collect] max_context_chars_auto.
_DEFAULT_MAX_CONTEXT_CHARS = 1200

_SHRINK_SYSTEM_PROMPT = (
    "You compress structural code-analysis facts for a coding assistant's "
    "prompt. Keep every symbol name, function signature, and contract "
    "description intact and unambiguous. Drop only redundant wording. "
    "Output plain text only — no markdown fences, no commentary, no "
    "preamble. Never invent facts not present in the input."
)


def _qualname_matches(qualname: str, name: str) -> bool:
    """Does collect's ``qualname`` refer to the symbol the caller named?

    COLLECT-FIX-1. Collect writes qualnames as ``<module path>:<dotted
    symbol>`` — ``tools/llm_stream.py:strip_think``,
    ``tools/auto/inner_loop.py:InnerLoop.run_task``. The previous matcher
    treated the whole string as dotted::

        qn == name or qn.endswith("." + name) or qn.split(".")[-1] == name

    which never sees the ``:`` separator, and so:

    * a module-level function or class NEVER matched its bare name —
      ``"tools/llm_stream.py:strip_think".split(".")[-1]`` is
      ``"py:strip_think"``, not ``"strip_think"``;
    * a *method* matched its bare name only by accident, because the dot in
      ``.py`` happens to fall to the left of the class dot;
    * the ``Class.method`` form never matched, because the qualname carries
      ``:InnerLoop.run_task``, not ``.InnerLoop.run_task``.

    Both public methods promised "bare name, dotted suffix, or full
    qualname" in their docstrings and delivered roughly none of it. Because
    every caller (ContextBroker Pass 3, Gate-1 grounding notes, and now
    AUTO-P's ArchProbe) fails open on a miss, the failure was silent: it
    looked like "collect does not know that symbol" rather than like a bug.
    It surfaced only when AUTO-P started reporting per-lookup outcomes and
    a real run came back 60/60 misses.

    Matching is now done on the symbol part alone, most specific first:
    exact qualname, then exact dotted symbol, then dotted suffix
    (``run_task`` matches ``InnerLoop.run_task``), then bare last
    component. A caller-supplied ``path:Symbol`` still matches exactly.
    """
    if not qualname or not name:
        return False
    if qualname == name:
        return True
    # Split off the module-path prefix; collect always uses ':' for it, and a
    # symbol name can never contain one.
    symbol = qualname.split(":", 1)[1] if ":" in qualname else qualname
    if symbol == name:
        return True
    if symbol.endswith("." + name):
        return True
    return symbol.split(".")[-1] == name



class CollectBridge:
    """Consumer-facing wrapper around a loaded `CollectModel` for `--auto`.

    Constructed once per `run_auto()` call (not per task) — see
    `tools.auto.controller.Controller._run_task_loop`. Every method is
    fail-open: on any internal error, it logs and returns `""` / `None`
    rather than raising, so a misconfigured or broken collect artifact
    never blocks a real `--auto` run.
    """

    def __init__(
        self,
        model,
        *,
        task_mode: str = "code",
        max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
        summarizer_call=None,
    ) -> None:
        self._model = model
        self._task_mode = task_mode
        self._max_context_chars = max(200, int(max_context_chars))
        # `summarizer_call` is a `tools.collect.summarizer.LlmCall`:
        # Callable[[system: str, user: str], str]. None = shrink disabled,
        # falls back to hard truncation.
        self._summarizer_call = summarizer_call
        # AUTO-METRIC: how many times the LLM shrink path actually fired
        # this run — surfaced for tests and for run-summary logging.
        self.shrink_calls = 0

    # ── availability ─────────────────────────────────────────────────────

    @property
    def usable(self) -> bool:
        """`True` only when the model is FRESH. A stale artifact is treated
        exactly like no artifact at all — see module docstring, item 3."""
        return bool(self._model is not None and getattr(self._model, "status", "absent") == "fresh")

    # ── 1. static per-task context ──────────────────────────────────────

    def context_for(self, target_file: str) -> str:
        """Budget-aware COLLECT-23 block for `target_file`, or `""`."""
        if not self.usable:
            return ""
        try:
            raw = build_collect_context_block(self._model, target_file, task_mode=self._task_mode)
        except Exception as exc:  # noqa: BLE001 — never block a task on this
            logger.warning("CollectBridge.context_for(%s): failed: %s", target_file, exc)
            return ""
        if not raw:
            return ""
        if len(raw) <= self._max_context_chars:
            return raw
        return self._shrink(raw)

    def context_for_many(self, target_files) -> str:
        """Join `context_for` blocks for several files, each budgeted
        independently, separated by a blank line. Empty files/blocks are
        skipped; returns `""` if nothing survives."""
        blocks = [b for b in (self.context_for(f) for f in target_files or []) if b]
        return "\n\n".join(blocks)

    def _shrink(self, raw: str) -> str:
        """Shrink `raw` to fit `_max_context_chars`. Tries the summarizer
        LLM once; on any failure (or if no summarizer is configured) falls
        back to a hard truncation with a notice, matching the truncation
        style `ContextBroker._cap` already uses elsewhere in this pipeline
        so a shrunk vs. truncated block is visually consistent to a
        human reading the prompt log."""
        if self._summarizer_call is not None:
            try:
                user = (
                    f"Compress the following to at most {self._max_context_chars} "
                    f"characters:\n\n{raw}"
                )
                self.shrink_calls += 1
                shrunk = self._summarizer_call(_SHRINK_SYSTEM_PROMPT, user)
                shrunk = (shrunk or "").strip()
                if shrunk and len(shrunk) <= self._max_context_chars * 1.15:
                    # small overshoot tolerance for the model rounding words;
                    # a bigger overshoot means the shrink didn't work — fall
                    # through to hard truncation instead of trusting it.
                    return shrunk
                if shrunk:
                    logger.warning(
                        "CollectBridge: shrink call overshot budget (%d > %d) — "
                        "hard-truncating instead",
                        len(shrunk), self._max_context_chars,
                    )
            except Exception as exc:  # noqa: BLE001 — fail open to truncation
                logger.warning("CollectBridge: shrink call failed: %s — hard-truncating", exc)
        excess = len(raw) - self._max_context_chars
        return raw[: self._max_context_chars] + f"\n… [+{excess} chars truncated by CollectBridge]\n"

    # ── 2. pull-model symbol resolution ─────────────────────────────────

    def pull_symbol(self, symbol_name: str) -> str:
        """Structural fact block for `symbol_name` (a bare name, a
        `Class.method` qualname, or a full `path:Qualname` collect
        reference), or `""` if unknown / model unusable.

        Matched against every module's `public_symbols` by, in order:
        exact qualname, dotted-suffix (`"method"` matches `"Class.method"`),
        then bare last-component match. First hit wins.
        """
        if not self.usable or not symbol_name:
            return ""
        name = symbol_name.strip()
        if not name:
            return ""
        try:
            for module in self._model.modules:
                for sym in module.public_symbols:
                    qn = sym.qualname
                    if _qualname_matches(qn, name):
                        return self._format_symbol_block(module.path, sym)
        except Exception as exc:  # noqa: BLE001 — never block a pull on this
            logger.warning("CollectBridge.pull_symbol(%s): failed: %s", symbol_name, exc)
        return ""

    def _format_symbol_block(self, module_path: str, sym) -> str:
        lines = [f"module: {module_path}", f"symbol: {sym.qualname}"]
        sig = getattr(sym, "signature", "") or ""
        if sig:
            lines.append(f"signature: {sig}")
        try:
            contracts = self._model.contracts_for(sym.qualname)
        except Exception:  # noqa: BLE001
            contracts = []
        for c in sorted(contracts, key=lambda c: c.name):
            lines.append(f"contract {c.name}: {c.description}")
        return "\n".join(lines)

    def module_symbols(self, module_ref: str) -> str:
        """Inventory of one module's top-level symbols, or `""` if unknown.

        AUTO-P5. `pull_symbol` answers "what is this symbol"; this answers
        "what is in this file". The Architect needs the second question far
        more often than the protocol let it ask: across two measured probe
        runs, 7 of the 9 unresolved lookups were `facts backoff` or
        `facts retry` — a *file* (`tools/backoff.py`) and a *concept*, neither
        of which is a symbol, so `pull_symbol` correctly returned nothing and
        the model had no way to get at the file it plainly meant.

        `module_ref` may be a collect path (`tools/backoff.py`), the same
        without its extension (`tools/backoff`), or a dotted import form
        (`tools.backoff`); collect itself only ever stores the first, so the
        other two are normalised before matching.

        **Exact match only** — no suffix or bare-name fallback, deliberately
        unlike `pull_symbol`. A module reference is either the file the caller
        meant or a different file entirely, and answering with a "closest
        match" would hand the Architect an inventory of the wrong module
        while looking like a successful lookup. A miss here is honest.
        """
        if not self.usable or not module_ref:
            return ""
        ref = module_ref.strip().strip("`\"'")
        if not ref:
            return ""
        candidates = {ref}
        if not ref.endswith(".py"):
            candidates.add(ref + ".py")
            candidates.add(ref.replace(".", "/") + ".py")
        try:
            for module in self._model.modules:
                if module.path in candidates:
                    return self._format_module_block(module)
        except Exception as exc:  # noqa: BLE001 — never block a pull on this
            logger.warning(
                "CollectBridge.module_symbols(%s): failed: %s", module_ref, exc
            )
        return ""

    # Symbols listed before the inventory is cut short. A module with 65
    # entries (the largest in this tree) would otherwise be truncated
    # mid-line by ArchProbe._cap, leaving the Architect with a partial list
    # it cannot tell is partial. Cutting on a symbol boundary and saying so
    # is the difference between "here is some of the file" and a silent lie.
    _MODULE_SYMBOL_LIMIT = 40

    def _format_module_block(self, module) -> str:
        """One line per symbol: `name(sig) :line — first docstring line`.

        The line number and docstring are not decoration. `signature` from
        collect is elided to `name(...)` with no parameter list, so a bare
        name+signature listing tells the model almost nothing it did not
        already know. `docstring_first_line` is populated for 37% of symbols
        in this tree and is the only prose in the record; `lineno` lets the
        Architect emit an accurate `cited_location.line_start` instead of
        guessing one, which is a Gate-1 rejection reason in its own right.
        """
        lines = [f"module: {module.path}"]
        syms = list(module.public_symbols)
        if not syms:
            # Distinct from a miss: the module resolved, it is simply empty.
            # Callers and telemetry must not conflate the two.
            lines.append("(no public top-level symbols)")
            return "\n".join(lines)
        for sym in syms[: self._MODULE_SYMBOL_LIMIT]:
            qn = getattr(sym, "qualname", "") or ""
            bare = qn.split(":", 1)[-1] if ":" in qn else qn
            # collect stores `signature` as "name(...)", NOT "(...)" — a
            # naive f"{bare}{signature}" renders "backoff_secondsbackoff_seconds(...)".
            # Found on a live artifact, not in a fixture: the test double had
            # the same shape and the doubled text still contained the expected
            # substring, so only real data exposed it.
            sig = (getattr(sym, "signature", "") or "").strip()
            if sig.startswith(bare):
                part = f"  {sig}"
            elif sig:
                part = f"  {bare}{sig}"
            else:
                part = f"  {bare}"
            lineno = getattr(sym, "lineno", None)
            if lineno:
                part += f" :{lineno}"
            doc = (getattr(sym, "docstring_first_line", "") or "").strip()
            if doc:
                part += f" — {doc}"
            lines.append(part)
        extra = len(syms) - self._MODULE_SYMBOL_LIMIT
        if extra > 0:
            lines.append(f"  … and {extra} more symbol(s) not listed")
        return "\n".join(lines)

    # ── GATE1-CTX-1/-2: read-only queries for Gate1's grounding notes ──────

    def contracts_for_symbol(self, symbol_name: str):
        """Every `ContractRecord` naming `symbol_name` (bare name, dotted
        suffix, or full qualname), or `[]` when unusable/unknown. Read-only
        variant of `pull_symbol` for a caller (Gate1) that wants the
        contract objects themselves, not a pre-formatted text block."""
        if not self.usable or not symbol_name:
            return []
        name = symbol_name.strip()
        if not name:
            return []
        try:
            for module in self._model.modules:
                for sym in module.public_symbols:
                    qn = sym.qualname
                    if _qualname_matches(qn, name):
                        return list(self._model.contracts_for(qn))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CollectBridge.contracts_for_symbol(%s): failed: %s", symbol_name, exc)
        return []

    def tests_covering(self, file_path: str):
        """Tuple of test-file paths that import module `file_path`, per
        collect's `test_map` (COLLECT built this as part of the coverage
        pass — module-level granularity, not symbol-level). `()` when
        unusable, the file has no entry, or it genuinely has zero covering
        tests (all three cases collapse to "nothing to report" for a
        grounding note either way)."""
        if not self.usable or not file_path:
            return ()
        try:
            return tuple(self._model.test_map.get(file_path, ()))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CollectBridge.tests_covering(%s): failed: %s", file_path, exc)
            return ()


# ── factory ──────────────────────────────────────────────────────────────

def make_collect_bridge(
    base_dir,
    config: configparser.ConfigParser,
    config_path: Optional[str] = None,
    *,
    task_mode: str = "code",
) -> Optional[CollectBridge]:
    """Build a `CollectBridge` for this `--auto` run, or `None` when the
    feature is off / unavailable.

    `None` is a valid, expected return — every caller must treat it as
    "no collect context this run" (identical to pre-COLLECT-24 behaviour),
    not as an error. This is the ONLY place `tools.collect.loader.load()`
    is called per run — callers must build this once and reuse it across
    every task, never call this per-task (see AUTO-METRIC test:
    `test_collect_model_loaded_once_per_run`).
    """
    key = "use_in_doc" if task_mode == "docs" else "use_in_auto"
    # Bugfix (config-crash audit): unguarded. Wrapped per-call, not via a
    # helper, so extract_config_reads still sees the literal call.
    try:
        use_collect = config.getboolean("collect", key, fallback=False)
    except ValueError as exc:
        logger.warning(
            "config [collect] %s is malformed (%s) — using default False",
            key, exc,
        )
        use_collect = False
    if not use_collect:
        return None
    try:
        from tools.collect.loader import load as load_collect_model
    except Exception as exc:  # noqa: BLE001 — opt-in feature, never fatal
        logger.warning("make_collect_bridge: collect model unavailable: %s", exc)
        return None
    try:
        model = load_collect_model(base_dir, config=config, config_path=config_path)
    except Exception as exc:  # noqa: BLE001 — same fail-open stance as COLLECT-23
        logger.warning("make_collect_bridge: load() failed: %s", exc)
        return None

    if getattr(model, "status", "absent") == "stale":
        # staleness=warn (default): loader already decided not to refresh.
        # Per product decision this is treated as a plain fallback to
        # standard --auto for the affected files — log once here so the
        # operator can see it happened, then let `.usable` gate every
        # subsequent call to False.
        logger.warning(
            "make_collect_bridge: collect artifact is stale (%s) — "
            "falling back to standard --auto context for this run "
            "(run --collect / --refresh to update it)",
            getattr(model, "reason", ""),
        )

    try:
        max_chars = config.getint(
            "collect", "max_context_chars_auto", fallback=_DEFAULT_MAX_CONTEXT_CHARS)
    except ValueError as exc:
        logger.warning(
            "config [collect] max_context_chars_auto is malformed (%s) — using default %r",
            exc, _DEFAULT_MAX_CONTEXT_CHARS,
        )
        max_chars = _DEFAULT_MAX_CONTEXT_CHARS

    summarizer_call = None
    # BUGFIX (audit): unguarded — the two reads above already catch
    # ValueError; this one didn't, contradicting this function's own
    # "opt-in, never fatal / fail-open" contract (a malformed value raised
    # straight out instead of degrading to the documented default).
    try:
        _llm_summaries = config.getboolean("collect", "llm_summaries", fallback=True)
    except ValueError as exc:
        logger.warning(
            "config [collect] llm_summaries is malformed (%s) — using default True", exc,
        )
        _llm_summaries = True
    if _llm_summaries:
        try:
            from tools.collect.summarizer import make_summarizer_call
            summarizer_call = make_summarizer_call(config, task_mode=task_mode)
        except Exception as exc:  # noqa: BLE001 — shrink is a nice-to-have
            logger.warning("make_collect_bridge: summarizer unavailable, will hard-truncate: %s", exc)
            summarizer_call = None

    return CollectBridge(
        model,
        task_mode=task_mode,
        max_context_chars=max_chars,
        summarizer_call=summarizer_call,
    )
