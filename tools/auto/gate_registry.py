"""tools/auto/gate_registry.py — declarative Gate-3 registry (GATES-1).

Before this module, ``InnerLoop.run_task`` carried five hand-written,
structurally identical blocks — canon, fact, continuity, theme, prosody —
totalling ~330 lines, plus five identical construction blocks in
``make_inner_loop``. Every one of them implemented the same contract:

    for each target file: read it, call validator.check(...), collect a
    problem block on a negative verdict, fail-OPEN on any exception; if
    any problems were collected, spend one revision and REJECT while the
    per-gate cap allows it, otherwise log a warning and ACCEPT_AT_CAP.

Five copies of one policy is five chances to let them drift apart — and
they already had: the canon gate skips the check entirely once its cap is
spent (the others still run it, so a now-passing text is accepted cleanly
rather than warned through), filters its file list through
``should_check``, and records the *unprefixed* conflict text in its
``AttemptRecord`` where the others record the prefixed feedback.

Those differences are preserved here as explicit per-gate flags rather
than as accidents of copy-paste, so the shared policy lives in exactly
one place (:func:`run_gates`) and each gate declares only what is
genuinely specific to it.

Adding a sixth gate is now one :class:`GateSpec` entry plus the validator
module — not an edit to three separate regions of ``inner_loop.py`` with
a new revision counter to remember to thread through.

Gate order is the order of :data:`GATES`, which is data: it can be
reordered, or filtered per task mode, without touching control flow.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Per-gate check adapters
# ─────────────────────────────────────────────────────────────────────────────
# Each adapter receives the shared per-file context and returns the
# validator's verdict object. They exist because the five validators do
# NOT share a check() signature (a deliberate non-goal here: unifying the
# signatures would mean touching five validator modules and their tests,
# which is a much larger blast radius than unifying the *loop*).

def _check_canon(validator, *, text, rel_path, task, loop, base_dir_path):
    return validator.check(text, rel_path, base_dir=base_dir_path)


def _check_fact(validator, *, text, rel_path, task, loop, base_dir_path):
    return validator.check(loop._task_with_goal(task), text)


def _check_continuity(validator, *, text, rel_path, task, loop, base_dir_path):
    from tools.auto.continuity_validator import (  # noqa: PLC0415 — circular import
        find_previous_chapter_text, read_story_bible,
    )
    bible = read_story_bible(base_dir_path)
    prev = find_previous_chapter_text(rel_path, base_dir_path)
    known_facts = bible + "\n\n--- previous chapter ---\n" + prev
    return validator.check(known_facts, text)


def _check_theme(validator, *, text, rel_path, task, loop, base_dir_path):
    return validator.check(text)


def _check_prosody(validator, *, text, rel_path, task, loop, base_dir_path):
    return validator.check(loop._task_with_goal(task), text)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict predicates
# ─────────────────────────────────────────────────────────────────────────────

def _rejected_by_conflict(verdict) -> bool:
    """CanonResult exposes ``has_conflict`` rather than ``approved``."""
    return bool(verdict.has_conflict)


def _rejected_by_approved(verdict) -> bool:
    return not verdict.approved


# ─────────────────────────────────────────────────────────────────────────────
# GateSpec
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateSpec:
    """Everything :func:`run_gates` needs to run one Gate-3 gate.

    Attributes
    ----------
    name:
        Stage name used in the run trace (``canon``, ``fact``, …) and as
        the per-gate revision-counter key.
    attr:
        Attribute on the ``InnerLoop`` holding the validator instance
        (``canon_validator``, …). ``None`` there means "gate disabled",
        which is how a failed/absent factory already degrades today.
    cap_attr / default_cap:
        Where the revision cap is read from on the validator, and the
        fallback when the validator doesn't expose it.
    check:
        Adapter from the shared per-file context to ``validator.check``.
    is_rejection:
        Predicate turning a verdict object into a bool.
    reject_label:
        Prefix for the coder-visible feedback (``"canon rejected"``).
    reject_log / cap_log:
        Log message formats. Kept per-gate because the existing wording
        differs (``"canon REJECT"`` vs ``"fact-check rejected"``) and log
        text is observable in tests via caplog.
    factory_module / factory_name:
        Import path of the ``make_*`` factory, used by
        :func:`build_validators`.
    skip_check_when_capped:
        ``True`` only for canon, which historically stops calling the
        validator once its cap is spent. The others keep checking so a
        now-clean text is accepted without a warning.
    file_filter_attr:
        Optional validator method name (``should_check``) narrowing which
        target files this gate looks at.
    record_prefixed_feedback:
        ``True`` records the prefixed feedback in the ``AttemptRecord``;
        canon records the bare conflict text.
    """

    name: str
    attr: str
    cap_attr: str
    default_cap: int
    check: Callable[..., Any]
    is_rejection: Callable[[Any], bool]
    reject_label: str
    reject_log: str
    cap_log: str
    factory_module: str
    factory_name: str
    skip_check_when_capped: bool = False
    file_filter_attr: Optional[str] = None
    record_prefixed_feedback: bool = True
    modes: tuple[str, ...] = ("creative",)


#: Gate execution order. This list is the pipeline — reorder it to reorder
#: the gates. Each entry's ``modes`` says which task modes it applies to.
GATES: tuple[GateSpec, ...] = (
    GateSpec(
        name="canon",
        attr="canon_validator",
        cap_attr="max_canon_revisions",
        default_cap=1,
        check=_check_canon,
        is_rejection=_rejected_by_conflict,
        reject_label="canon rejected",
        reject_log="InnerLoop: attempt %d canon REJECT (%d/%d) — %s",
        cap_log=(
            "InnerLoop: canon revision cap (%d) reached for %s — "
            "accepting chapter with possible unresolved canon issues."
        ),
        factory_module="tools.auto.canon_validator",
        factory_name="make_canon_validator",
        skip_check_when_capped=True,
        file_filter_attr="should_check",
        record_prefixed_feedback=False,
    ),
    GateSpec(
        name="fact",
        attr="fact_validator",
        cap_attr="max_fact_revisions",
        default_cap=1,
        check=_check_fact,
        is_rejection=_rejected_by_approved,
        reject_label="fact-check rejected",
        reject_log="InnerLoop: attempt %d fact-check rejected (%d/%d) — %s",
        cap_log=(
            "InnerLoop: fact revision cap (%d) reached — "
            "accepting chapter with possible unresolved fact contradiction."
        ),
        factory_module="tools.auto.fact_validator",
        factory_name="make_fact_validator",
    ),
    GateSpec(
        name="continuity",
        attr="continuity_validator",
        cap_attr="max_continuity_revisions",
        default_cap=1,
        check=_check_continuity,
        is_rejection=_rejected_by_approved,
        reject_label="continuity rejected",
        reject_log="InnerLoop: attempt %d continuity rejected (%d/%d) — %s",
        cap_log=(
            "InnerLoop: continuity revision cap (%d) reached — "
            "accepting chapter with possible unresolved continuity issues."
        ),
        factory_module="tools.auto.continuity_validator",
        factory_name="make_continuity_validator",
    ),
    GateSpec(
        name="theme",
        attr="theme_validator",
        cap_attr="max_theme_revisions",
        default_cap=2,
        check=_check_theme,
        is_rejection=_rejected_by_approved,
        reject_label="theme rejected",
        reject_log="InnerLoop: attempt %d theme rejected (%d/%d) — %s",
        cap_log=(
            "InnerLoop: theme revision cap (%d) reached — "
            "accepting chapter with possible unresolved theme issues."
        ),
        factory_module="tools.auto.theme_validator",
        factory_name="make_theme_validator",
    ),
    GateSpec(
        name="prosody",
        attr="prosody_validator",
        cap_attr="max_prosody_revisions",
        default_cap=2,
        check=_check_prosody,
        is_rejection=_rejected_by_approved,
        reject_label="prosody rejected",
        reject_log="InnerLoop: attempt %d prosody rejected (%d/%d) — %s",
        cap_log=(
            "InnerLoop: prosody revision cap (%d) reached — "
            "accepting poem with possible unresolved rhythm/rhyme issues."
        ),
        factory_module="tools.auto.prosody",
        factory_name="make_prosody_validator",
    ),
)


@dataclass
class GateRejection:
    """One gate rejected this attempt; the caller must re-loop."""

    gate: str
    feedback: str        # coder-visible, prefixed with the gate's label
    record_text: str     # what goes into the AttemptRecord


#: Gate name → spec, for config lookups.
GATES_BY_NAME: dict[str, GateSpec] = {spec.name: spec for spec in GATES}


def resolve_gate_order(config, task_mode: str) -> tuple[GateSpec, ...]:
    """Return the gates to run for *task_mode*, in execution order.

    GATES-2. Reads an optional ``[gates]`` section whose keys are task
    modes::

        [gates]
        creative = canon, fact, continuity, theme, prosody
        code     =

    Semantics, chosen so that every existing config keeps its current
    behaviour without being edited:

    * **No ``[gates]`` section, or no key for this mode** — fall back to
      :data:`GATES` filtered by each spec's ``modes``. This is exactly
      the hard-coded behaviour that predates this function, so an
      untouched ``agents.ini`` is unaffected.
    * **Key present but empty** — no gates at all. Distinct from an
      absent key on purpose: ``creative =`` is how you turn the whole
      Gate-3 layer off (useful when benchmarking validators, where the
      gates are the thing being measured rather than applied).
    * **Unknown gate name** — logged and skipped, not raised. A typo in
      a config file must not abort a run that would otherwise work; the
      warning names the offending entry and lists what is available.
    * **Duplicate name** — kept once, at its first position, so a
      copy-paste slip can't make a gate spend its revision cap twice per
      attempt.

    When the key IS present, the listed order wins and each spec's
    ``modes`` field is not consulted: the config key already names the
    mode, so re-filtering would silently drop a gate the user asked for
    by name, which is the opposite of what an explicit list means.
    """
    if config is None or not config.has_section("gates"):
        return tuple(s for s in GATES if task_mode in s.modes)
    if not config.has_option("gates", task_mode):
        return tuple(s for s in GATES if task_mode in s.modes)

    try:
        raw = config.get("gates", task_mode)
    except Exception as exc:  # noqa: BLE001 — never block the run on config
        logger.warning("config [gates] %s unreadable (%s) — using defaults", task_mode, exc)
        return tuple(s for s in GATES if task_mode in s.modes)

    order: list[GateSpec] = []
    seen: set[str] = set()
    for token in raw.split(","):
        name = token.strip()
        if not name or name in seen:
            continue
        spec = GATES_BY_NAME.get(name)
        if spec is None:
            logger.warning(
                "config [gates] %s: unknown gate %r — skipped (known: %s)",
                task_mode, name, ", ".join(GATES_BY_NAME),
            )
            continue
        seen.add(name)
        order.append(spec)
    return tuple(order)


def build_validators(config, base_dir, *, task_mode: str, broker=None) -> dict:
    """Construct every gate validator applicable to *task_mode*.

    Replaces five copy-pasted ``try: from … import make_X`` blocks in
    ``make_inner_loop``. A factory that raises or is missing yields
    ``None`` for that gate — the same never-block-the-loop-on-setup
    behaviour each block implemented individually.

    Returns a dict keyed by :attr:`GateSpec.attr`, ready to splat into
    ``InnerLoop(...)`` as keyword arguments.
    """
    # GATES-2: only build validators for gates the config actually enables.
    # Every other gate's attr is set to None, which run_gates reads as
    # "off" — so a disabled gate costs nothing at construction time
    # either (no LLM client, no story-bible read).
    enabled = {s.name for s in resolve_gate_order(config, task_mode)}
    out: dict = {}
    for spec in GATES:
        if spec.name not in enabled:
            out[spec.attr] = None
            continue
        try:
            module = __import__(spec.factory_module, fromlist=[spec.factory_name])
            factory = getattr(module, spec.factory_name)
            # The factories don't share a signature either; pass only what
            # each declares. Introspection here is cheaper and far less
            # invasive than rewriting five factories and their test suites.
            import inspect  # noqa: PLC0415
            params = inspect.signature(factory).parameters
            kwargs: dict = {}
            if "base_dir" in params:
                kwargs["base_dir"] = base_dir
            if "task_mode" in params:
                kwargs["task_mode"] = task_mode
            if "broker" in params:
                kwargs["broker"] = broker
            out[spec.attr] = factory(config, **kwargs)
        except Exception as exc:  # noqa: BLE001 — never block the loop on setup
            logger.warning(
                "make_inner_loop: %s validator unavailable — %s", spec.name, exc
            )
            out[spec.attr] = None
    return out


def run_gates(
    loop,
    *,
    task: dict,
    task_id: str,
    attempt: int,
    target_files,
    base_dir_path: Path,
    revisions: dict,
    trace_stage: Callable[..., None],
) -> Optional[GateRejection]:
    """Run every applicable gate in :data:`GATES` order.

    Returns ``None`` when the attempt survives every gate, or a
    :class:`GateRejection` for the first gate that rejects it. *revisions*
    is mutated in place — it holds the per-gate spent-revision counters
    that used to be five separate locals in ``run_task``.

    Every gate is fail-OPEN: an exception from ``check`` (or from reading
    the file) approves that file rather than failing the attempt, matching
    the pre-existing per-block behaviour exactly.
    """
    if not target_files:
        return None

    # GATES-2: the loop carries its resolved order when make_inner_loop
    # built it. Falling back to the mode-filtered registry keeps every
    # directly-constructed InnerLoop (tests, embedders) working unchanged.
    # ``is None``, not truthiness: an EMPTY order means "every gate is
    # switched off" (config `creative =`) and must NOT fall back to the
    # registry default. Testing `if not order` here silently re-enabled
    # every gate for exactly the config that asked for none.
    order = getattr(loop, "gate_order", None)
    if order is None:
        order = tuple(s for s in GATES if loop.task_mode in s.modes)

    for spec in order:
        validator = getattr(loop, spec.attr, None)
        if validator is None:
            continue

        cap = getattr(validator, spec.cap_attr, spec.default_cap)
        used = revisions.get(spec.name, 0)

        files = list(target_files)
        if spec.file_filter_attr is not None:
            predicate = getattr(validator, spec.file_filter_attr)
            files = [f for f in files if predicate(f)]
        if not files:
            continue

        if spec.skip_check_when_capped and used >= cap:
            logger.warning(spec.cap_log, cap, files)
            trace_stage(task_id, attempt, spec.name, "ACCEPTED_AT_CAP", cap=cap)
            continue

        problem_blocks: list[str] = []
        for rel_path in files:
            try:
                text = (base_dir_path / rel_path).read_text(
                    encoding="utf-8", errors="replace"
                )
                verdict = spec.check(
                    validator,
                    text=text,
                    rel_path=rel_path,
                    task=task,
                    loop=loop,
                    base_dir_path=base_dir_path,
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning(
                    "InnerLoop: %s check raised for %s — %s; approving.",
                    spec.name, rel_path, exc,
                )
                verdict = None

            if verdict is not None and spec.is_rejection(verdict):
                problem_blocks.append(f"{rel_path}:\n{verdict.feedback()}")

        if not problem_blocks:
            continue

        blob = "\n\n".join(problem_blocks)
        if used < cap:
            revisions[spec.name] = used + 1
            logger.info(
                spec.reject_log,
                attempt, revisions[spec.name], cap, blob.replace("\n", " ")[:120],
            )
            full = f"{spec.reject_label}\n{blob}"
            trace_stage(
                task_id, attempt, spec.name, "REJECTED",
                revisions_used=revisions[spec.name], cap=cap,
            )
            return GateRejection(
                gate=spec.name,
                feedback=full,
                record_text=full if spec.record_prefixed_feedback else blob,
            )

        # Cap spent and the text still fails — accept with a warning rather
        # than ping-ponging the loop forever.
        if spec.skip_check_when_capped:
            logger.warning(spec.cap_log, cap, files)
        else:
            logger.warning(spec.cap_log, cap)
        trace_stage(task_id, attempt, spec.name, "ACCEPTED_AT_CAP", cap=cap)

    return None
