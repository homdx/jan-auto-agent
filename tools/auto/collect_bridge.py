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

4. **V9: freshness within a run.** Item 3 only covers the artifact's
   status at load time. It says nothing about the middle of a run:
   `status` is computed once inside `load()`, the bridge is cached for
   the lifetime of the `Controller`, and `--auto` edits and commits
   source files in between. Without item 4 a task that edits
   `pkg/a.py` hands every later task a symbol list, `guarded` count and
   risk score describing that file as it was before the run started,
   under a header that says "do not contradict". `invalidate(paths)`
   fixes it on write rather than on a timer: the controller marks the
   paths a task committed dirty, and `context_for` / `pull_symbol` /
   `module_symbols` / `contracts_for_symbol` return `""` / `[]` for a
   dirty path — the item-3 contract, now applied per path instead of per
   artifact. Clean paths are untouched, so one edited file does not blind
   the pack for the other hundreds. `[collect] auto_refresh_between_tasks`
   (default `false`) instead re-runs the existing incremental
   `action_module` for the edited path and folds the fresh `ModuleRecord`
   back into the in-memory model. The model *object* is still built once
   per run: records are patched, `load()` is never called a second time.
"""

from __future__ import annotations

import configparser
import dataclasses
import json
import logging
from pathlib import Path
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
        auto_refresh: bool = False,
        base_dir=None,
        config=None,
        config_path=None,
        module_refresh_fn=None,
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
        # V9: module paths written by a task of this run since `load()`.
        # `load()` saw the tree before any of them; `status` cannot have
        # changed to say so, so this set is what carries it.
        self._dirty_paths: set = set()
        # Paths whose in-run repair already failed once — not retried again
        # until the next commit dirties the path, at which point
        # `invalidate()` drops them from here so the repair is re-attempted.
        self._repair_failed: set = set()
        # V9: `[collect] auto_refresh_between_tasks` (default false).
        self._auto_refresh = bool(auto_refresh)
        self._base_dir = base_dir
        self._config = config
        self._config_path = config_path
        # Injectable replacement for `action_module` — one module, one LLM
        # call. Tests pass a stub; production uses `_action_module_record`.
        self._module_refresh_fn = module_refresh_fn
        # M4 `collect_miss` counters: reason -> count. M4's run_trace events
        # do not exist in this tree yet, so the bridge keeps the tally
        # itself and hands it out for a run summary.
        self.collect_misses: dict = {}

    # ── availability ─────────────────────────────────────────────────────

    @property
    def status(self) -> str:
        """`"fresh"` / `"stale"` / `"absent"` — the model's own status, so a
        caller can distinguish "there is an artifact, it is just old" from
        "there is nothing at all".

        Fail-open like the rest of this class: no model, no attribute, a
        non-string value or an object whose `__getattr__` raises all read as
        `"absent"`, never as an exception into a run.
        """
        if self._model is None:
            return "absent"
        try:
            value = getattr(self._model, "status", "absent")
        except Exception:  # noqa: BLE001 — diagnostics, never a run blocker
            return "absent"
        return value if isinstance(value, str) else "absent"

    @property
    def usable(self) -> bool:
        """`True` only when the model is FRESH. A stale artifact is treated
        exactly like no artifact at all — see module docstring, item 3."""
        return bool(self._model is not None and self.status == "fresh")

    # ── V9: invalidate on write ─────────────────────────────────────────

    def _dirty_set(self) -> frozenset:
        """The dirty paths, or `()` when `__init__` never ran.

        `getattr` rather than a plain attribute read: some tests build a
        bridge via `__new__()` and skip `__init__` entirely, and a missing
        set must read as "nothing was written" — the pre-V9 behaviour —
        rather than raising into a run.
        """
        return frozenset(getattr(self, "_dirty_paths", ()))

    @property
    def dirty_paths(self) -> frozenset:
        """Module paths this run has written since `load()`, so a caller can
        tell "no data at all" from "the model is there, it just predates this
        task's commit"."""
        return self._dirty_set()

    @property
    def has_dirty(self) -> bool:
        return bool(self._dirty_set())

    def summary(self) -> str:
        """One line saying how much invalidate-on-write withheld this run.

        V9 item 4 says `collect_miss(reason="dirty")` is emitted so a run can
        report how many blocks the rule suppressed; the tally lives on
        `collect_misses`, and this is its reader — the controller logs it
        once, at the end of the task loop. `""` when nothing was withheld
        and nothing is still dirty, so a run with no edits adds no noise to
        run.log. Never raises.
        """
        withheld = {}
        try:
            for reason, count in (self.collect_misses or {}).items():
                withheld[str(reason)] = int(count)
        except Exception:  # noqa: BLE001 — a summary must never sink a run
            return ""
        parts = []
        if withheld:
            total = sum(withheld.values())
            detail = ", ".join(f"{reason}={count}"
                               for reason, count in sorted(withheld.items()))
            parts.append(
                f"collect withheld {total} block(s) for path(s) this run wrote "
                f"({detail})"
            )
        if self.has_dirty:
            parts.append(
                f"{len(self.dirty_paths)} path(s) still unrefreshed at run end: "
                f"{', '.join(sorted(self.dirty_paths))}"
            )
        return "; ".join(parts)

    def _normalize_path(self, path) -> Optional[str]:
        """`pkg/a.py` from a task or git path, or `None` when it is not a
        collect-modelled module.

        Collect keys modules by a repo-relative POSIX path ending in `.py`;
        anything else — a doc, a config file, `.agent/` state — is not in
        the model, so a write to it must never blind the pack. Non-strings
        (`Path`, `bytes`, …) are coerced rather than raising, per the
        fail-open stance every other method here takes.
        """
        if path is None:
            return None
        if not isinstance(path, str):
            try:
                path = str(path)
            except Exception:  # noqa: BLE001 — fail open, never a run blocker
                return None
        ref = path.strip().strip("`\"'")
        if not ref:
            return None
        ref = ref.replace("\\", "/")
        while ref.startswith("./"):
            ref = ref[2:]
        if ref == ".." or ref.startswith("../"):
            # Above the tree root: git never reports such a path and the
            # model holds nothing there, so there is nothing to blind.
            return None
        if ref.startswith("/"):
            # An absolute path only means something relative to the tree the
            # model was built from; outside it there is nothing to blind.
            base = getattr(self, "_base_dir", None)
            try:
                ref = Path(ref).resolve().relative_to(Path(base).resolve()).as_posix() \
                    if base is not None else ref.lstrip("/")
            except (ValueError, OSError):
                return None
        return ref if ref.endswith(".py") else None

    def _is_dirty(self, ref: str) -> bool:
        """Whether a lookup for `ref` must be withheld.

        Normalises first, so `"pkg/a.py"`, `"./pkg/a.py"`,
        `"/pkg/a.py"` and a `Path` all mean the same thing. When
        `auto_refresh=True`, a dirty path is repaired in place before
        answering — one module, one LLM call — and only paths the repair
        could not fix stay withheld (V9, items 1 and 2).
        """
        if not ref:
            return False
        dirty_paths = self._dirty_set()
        norm = self._normalize_path(ref) or ref
        if norm not in dirty_paths:
            return False
        if not getattr(self, "_auto_refresh", False):
            return True
        failed = getattr(self, "_repair_failed", None)
        if failed is None:
            failed = self._repair_failed = set()
        if norm in failed:
            # Already tried once this run and could not repair it. Every
            # caller would otherwise re-run action_module — and re-pay its
            # LLM call — on each lookup of a path that is going to stay dirty.
            return True
        if self._refresh_module_in_place(norm):
            self._dirty_paths = set(dirty_paths - {norm})
            logger.info("CollectBridge: %s refreshed in place — not blinded", norm)
            return False
        failed.add(norm)
        return True

    def _module_record(self, path: str):
        """The model's current `ModuleRecord` for `path`, or `None`. Used to
        re-read a module after `_is_dirty` repaired it mid-scan."""
        try:
            for module in self._model.modules:
                if module.path == path:
                    return module
        except Exception:  # noqa: BLE001
            pass
        return None

    def invalidate(self, paths) -> None:
        """Mark every module path `paths` names as written since `load()`.

        Call this with the paths a task's commit actually changed, right
        after a successful commit: the artifact was built before that edit,
        and `status` — computed once inside `load()` — cannot detect it.
        Afterwards `context_for` / `pull_symbol` / `module_symbols` /
        `contracts_for_symbol` return nothing for a dirty path, while every
        clean path keeps its block for the rest of the run (V9, item 1).

        This only records dirt. With `auto_refresh=True`, the repair itself
        happens lazily the first time a dirty path is actually asked for —
        the existing incremental `action_module`, one module, one LLM call —
        so a path no later task reads is never paid for (V9, item 2).

        `paths` may be anything iterable; `None`, an empty list, or a
        generator that raises all degrade to "nothing was written" rather
        than to an exception. Non-module entries are dropped, so a commit
        that only touched `README.md` or `agents.ini` invalidates nothing.
        Never raises.
        """
        new_dirty = set()
        try:
            for path in paths or []:
                norm = self._normalize_path(path)
                if norm is not None:
                    new_dirty.add(norm)
        except Exception as exc:  # noqa: BLE001 — malformed input, fail open
            logger.warning(
                "CollectBridge.invalidate: malformed paths (%s) — using the "
                "paths that parsed: %s", type(exc).__name__, exc,
            )
        if not new_dirty:
            return
        self._dirty_paths = set(self._dirty_set() | new_dirty)
        # A new commit means new facts, so a repair that already failed for
        # these paths is worth one more attempt: an outage that started at
        # task N must not keep blinding a path that task N+2 edited again.
        # The attempt stays once per *edit* — never once per lookup.
        failed = getattr(self, "_repair_failed", None)
        if failed is not None and new_dirty & failed:
            failed -= new_dirty
        logger.info(
            "CollectBridge.invalidate: %d path(s) now dirty — collect "
            "blocks withheld for them: %s",
            len(new_dirty), ", ".join(sorted(new_dirty)),
        )

    def _miss(self, reason: str, target: str) -> None:
        """One `collect_miss` (M4): count it, log it, trace it. Never
        raises.

        M4's `run_trace` events do not exist in this tree yet, so the tally
        lives here on `collect_misses` (reason -> count, for a run summary)
        and the trace call already uses M4's field names — `reason` and
        `target_file`. `reason` is `"dirty"` for this ticket; M4 adds the
        `absent` / `stale` / `unknown_module` reasons when it lands.
        """
        try:
            self.collect_misses[reason] = self.collect_misses.get(reason, 0) + 1
        except Exception:  # noqa: BLE001 — a counter must never sink a run
            pass
        try:
            # Lazy: keeps the M4 trace out of this module's import graph, so
            # importing collect_bridge never pays for the tracing stack.
            from tools.agent_trace import tracer

            tracer.event(
                source="collect_bridge", target="auto_run",
                kind="collect_miss",
                params={"reason": reason, "target_file": target},
            )
        except Exception:  # noqa: BLE001 — tracing must never sink a run
            pass
        logger.info("collect_miss reason=%s target=%s", reason, target)

    def _refresh_module_in_place(self, path: str):
        """`auto_refresh=True`: re-scan one edited module and fold the fresh
        `ModuleRecord` into the in-memory model. Returns the record, or
        `None` to keep the path dirty (the blind is the safe outcome).

        The model *object* is still built once per run: `load()` is never
        called here, only the one module's record is replaced in a copy of
        the loaded model (V9, item 3).
        """
        if self._model is None:
            return None
        status = getattr(self._model, "status", "absent")
        if status != "fresh":
            # `usable` is False for such a model anyway, so repairing it
            # would spend an LLM call that no block would ever be served.
            return None
        try:
            known = {m.path for m in (getattr(self._model, "modules", ()) or ())}
        except Exception:  # noqa: BLE001
            return None
        if path not in known:
            # Not in the model: no prior record to update, so a refresh would
            # only invent one. Leave it dirty.
            return None

        record = None
        if getattr(self, "_module_refresh_fn", None) is not None:
            try:
                record = self._module_refresh_fn(path)
            except Exception as exc:  # noqa: BLE001 — fail open to a blind
                logger.warning(
                    "CollectBridge: auto refresh of %s failed (%s: %s) — "
                    "keeping it dirty", path, type(exc).__name__, exc,
                )
                return None
        else:
            record = self._action_module_record(path)

        if record is None or getattr(record, "path", None) != path:
            return None
        try:
            modules = tuple(record if m.path == path else m for m in self._model.modules)
            self._model = dataclasses.replace(self._model, modules=modules)
        except Exception as exc:  # noqa: BLE001 — keep the model as loaded
            logger.warning(
                "CollectBridge: could not fold the refreshed record for %s "
                "into the model (%s: %s) — keeping it dirty",
                path, type(exc).__name__, exc,
            )
            return None
        return record

    def _action_module_record(self, path: str):
        """Run the existing incremental `action_module` for one module and
        read the patched `ModuleRecord` back out of the artifact it wrote.

        One module, one LLM call — the incremental path `--module` already
        documents. The artifact is written to `.collect/`, which is
        git-ignored, so this adds no git noise; and the on-disk artifact
        ends the run describing the tree it describes. Any failure — no
        base_dir, no config, unreadable artifact, a missing module entry —
        returns `None` and the caller keeps the path dirty.
        """
        if getattr(self, "_base_dir", None) is None or getattr(self, "_config", None) is None:
            return None
        try:
            from tools.collect import cli as cli_mod
            from tools.collect.model import ModuleRecord

            # The same Pass B summarizer `_shrink` uses — `make_collect_bridge`
            # already built it under `[collect] llm_summaries`, so a repair
            # is one LLM call with the flag on and structural-only without.
            cli_mod.action_module(
                self._base_dir, path,
                config=self._config, config_path=self._config_path,
                llm_call=getattr(self, "_summarizer_call", None),
            )
            artifact = (
                cli_mod.resolve_collect_dir(self._base_dir, self._config)
                / cli_mod.ARTIFACT_FILENAME
            )
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            for entry in payload.get("modules", []):
                if entry.get("path") == path:
                    return ModuleRecord.from_dict(entry)
            return None
        except Exception as exc:  # noqa: BLE001 — repair is best effort
            logger.warning(
                "CollectBridge: auto refresh of %s failed (%s: %s) — keeping "
                "it dirty", path, type(exc).__name__, exc,
            )
            return None

    # ── 1. static per-task context ──────────────────────────────────────

    def context_for(self, target_file: str) -> str:
        """Budget-aware COLLECT-23 block for `target_file`, or `""`."""
        if not self.usable:
            return ""
        # V9: the model predates this path's edit, so its facts describe a
        # tree that no longer exists. Runs before the budget check and
        # before `_shrink`, so a dirty path neither spends the shrink LLM
        # call nor returns a shrunk block of pre-edit facts.
        if self._is_dirty(target_file):
            self._miss("dirty", target_file)
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

        V9: symbols whose module was edited by a task of this run are
        skipped, because their record predates the edit — a clean duplicate
        in another module still answers. When the only matches were dirty
        the lookup returns `""` and a `collect_miss` is counted.
        """
        if not self.usable or not symbol_name:
            return ""
        name = symbol_name.strip()
        if not name:
            return ""
        dirty_hit = False
        dirty_paths = self._dirty_set()
        deferred: list = []
        try:
            for module in list(self._model.modules):
                path = module.path
                if path in dirty_paths:
                    if not any(
                        _qualname_matches(s.qualname, name) for s in module.public_symbols
                    ):
                        # The pre-edit record does not know the name — but
                        # the edit may have ADDED it. Repairing now would
                        # pay an LLM call for every dirty path on every
                        # pull; a clean record elsewhere may still answer,
                        # so the repair waits until nothing else did.
                        deferred.append(path)
                        continue
                    # V9: this record predates the edit, so it cannot be
                    # trusted as is. `_is_dirty` repairs it when
                    # auto_refresh is on; otherwise a clean duplicate
                    # elsewhere still wins, so the loop keeps going.
                    if self._is_dirty(path):
                        dirty_hit = True
                        continue
                    module = self._module_record(path) or module
                for sym in module.public_symbols:
                    if _qualname_matches(sym.qualname, name):
                        return self._format_symbol_block(path, sym)
            for path, module in self._repaired_deferred(deferred):
                for sym in module.public_symbols:
                    if _qualname_matches(sym.qualname, name):
                        return self._format_symbol_block(path, sym)
        except Exception as exc:  # noqa: BLE001 — never block a pull on this
            logger.warning("CollectBridge.pull_symbol(%s): failed: %s", symbol_name, exc)
        if dirty_hit:
            self._miss("dirty", name)
        return ""

    def _repaired_deferred(self, paths):
        """Yield `(path, ModuleRecord)` for each dirty `paths` entry that
        `_is_dirty` could repair in place — the second look a symbol lookup
        takes when no pre-edit record answered.

        With `auto_refresh` off this yields nothing: an added symbol is
        withheld like every other post-edit fact, and no miss is counted
        for it, since the pre-edit model never claimed to know it.
        """
        if not paths or not getattr(self, "_auto_refresh", False):
            return
        for path in paths:
            if not self._is_dirty(path):
                module = self._module_record(path)
                if module is not None:
                    yield path, module

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

        V9: a module edited by a task of this run returns `""` — every
        candidate spelling of `module_ref` that the bridge considers is
        checked, so `"tools/backoff"` and `"tools.backoff"` are withheld
        just as surely as `"tools/backoff.py"` is.
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
        if any(self._is_dirty(c) for c in candidates):
            self._miss("dirty", ref)
            return ""
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
        contract objects themselves, not a pre-formatted text block.

        V9: symbols whose module was edited this run are skipped, exactly
        like `pull_symbol` — a contract is a fact about that module's code,
        and the artifact was built before the edit. Gate1's grounding notes
        would otherwise assert pre-edit contracts as ground truth."""
        if not self.usable or not symbol_name:
            return []
        name = symbol_name.strip()
        if not name:
            return []
        dirty_hit = False
        dirty_paths = self._dirty_set()
        deferred: list = []
        try:
            for module in list(self._model.modules):
                path = module.path
                if path in dirty_paths:
                    if not any(
                        _qualname_matches(s.qualname, name) for s in module.public_symbols
                    ):
                        deferred.append(path)   # maybe added by the edit
                        continue
                    if self._is_dirty(path):
                        dirty_hit = True
                        continue
                    module = self._module_record(path) or module
                for sym in module.public_symbols:
                    qn = sym.qualname
                    if _qualname_matches(qn, name):
                        return list(self._model.contracts_for(qn))
            for path, module in self._repaired_deferred(deferred):
                for sym in module.public_symbols:
                    qn = sym.qualname
                    if _qualname_matches(qn, name):
                        return list(self._model.contracts_for(qn))
        except Exception as exc:  # noqa: BLE001
            logger.warning("CollectBridge.contracts_for_symbol(%s): failed: %s", symbol_name, exc)
        if dirty_hit:
            self._miss("dirty", name)
        return []

    def tests_covering(self, file_path: str):
        """Tuple of test-file paths that import module `file_path`, per
        collect's `test_map` (COLLECT built this as part of the coverage
        pass — module-level granularity, not symbol-level). `()` when
        unusable, the file has no entry, or it genuinely has zero covering
        tests (all three cases collapse to "nothing to report" for a
        grounding note either way).

        Deliberately NOT blinded by `invalidate()`: `test_map` records which
        *test* files import which modules — an edit to a source module does
        not change that mapping, only a change to a test file or its imports
        does, and the block for the edited path is already withheld by
        `context_for`.
        """
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
    # V9: `[collect] auto_refresh_between_tasks` (default false). When true,
    # a path a task edits is repaired via the existing incremental
    # `action_module` (one module, one LLM call) instead of being blinded
    # for the rest of the run. A malformed value degrades to the default,
    # like every other `[collect]` read in this function.
    try:
        auto_refresh = config.getboolean("collect", "auto_refresh_between_tasks", fallback=False)
    except ValueError as exc:
        logger.warning(
            "config [collect] auto_refresh_between_tasks is malformed (%s) — using default False",
            exc,
        )
        auto_refresh = False

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
        auto_refresh=auto_refresh,
        base_dir=base_dir,
        config=config,
        config_path=config_path,
    )
