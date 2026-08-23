"""tools/auto/plan_validator.py — AUTO-H1: Re-validate an existing plan.

``IMPROVEMENTS.md`` / ``.agent/plan.json`` is produced once by the Architect
+ Gate 1 pipeline (see ``plan_emitter.py``) and then trusted for the rest of
the run. Gate 1 already runs a two-stage false-positive check at that
moment — but only at that moment. Nothing re-checks a task between plan time
and execute time, so a task can still reach the Coder as a false positive
when:

  * the LLM's Stage-B "is the problem actually present?" verdict was itself
    wrong (hallucinated verdicts happen on small/local models — the whole
    reason Gate 1 exists in the first place);
  * the codebase changed after the plan was written (a human fixed the same
    thing by hand, or an earlier auto task's own diff incidentally closed a
    later task's gap too);
  * the plan was hand-edited or restored from an older run.

This module re-runs the *exact same* Gate 1 check
(``tools.auto.gate1_filter.filter_candidates``) that the Architect trusts at
plan time, but against the live ``todo`` / ``in_progress`` tasks already
sitting in ``.agent/plan.json``, using the CURRENT file contents on disk.
Anything Gate 1 would now reject is:

  1. removed from ``.agent/plan.json`` (``StateStore.remove_task`` — see
     that method's docstring for why a bare status flip to BLOCKED is not
     enough);
  2. stripped out of ``IMPROVEMENTS.md`` — its ``### AUTO-Tn: ...`` section
     is deleted so the human-readable backlog stops listing work that will
     never run;
  3. appended to ``IMPROVEMENTS-FALSE.md`` at the repo root instead of
     being silently dropped, so a human can still see what the agent
     decided NOT to do, and why.

``done`` and ``blocked`` tasks are left untouched: a ``done`` task already
has a commit behind it, and a ``blocked`` task is already excluded from
execution (re-validating it would spend an LLM call for no behavioural
change).

Public surface consumed by ``main.py``::

    from tools.auto.plan_validator import run_validate
    exit_code = run_validate(base_dir=".", config_path="agents.ini")

Lower-level surface for scripts / tests::

    from tools.auto.plan_validator import validate_plan
    report = validate_plan(base_dir=".", config_path="agents.ini")
    # report.checked  — int, how many todo/in_progress tasks were examined
    # report.kept     — list[str], task ids Gate 1 re-confirmed
    # report.removed  — list[RemovedTask], task ids pulled from the plan

This is a deliberate separate one-shot mode (``--validate-plan``), not baked
into every ``--auto`` run: it costs one LLM call per pending task, same as
the original Gate 1 pass, and is meant to be run *between* a ``--dry-run``
plan and real execution — exactly the gap the workflow of reading
``IMPROVEMENTS.md`` by hand and pruning obvious false positives was already
covering manually.
"""

from __future__ import annotations

import configparser
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import filter_candidates, _is_technical_failure
from tools.auto.git_manager import GitError, make_git_manager
from tools.auto.plan_emitter import IMPROVEMENTS_FILENAME
from tools.auto.state import STATUS_IN_PROGRESS, STATUS_TODO, StateStore
from tools.auto.utils import _ts, atomic_write_text, normalize_task_mode

logger = logging.getLogger(__name__)

# Name of the false-positive log written to the repo root.
IMPROVEMENTS_FALSE_FILENAME = "IMPROVEMENTS-FALSE.md"

_COMMIT_TASK_ID = "AUTO-H1"


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RemovedTask:
    """A plan.json task that re-validation confirmed should not be executed.

    Attributes
    ----------
    task_id:
        The task's id in plan.json, e.g. ``"AUTO-T4"``.
    title, instruction, target_files:
        Copied from the task at the moment it was removed, so the record in
        IMPROVEMENTS-FALSE.md is self-contained even after the task no
        longer exists in plan.json.
    stage:
        Which Gate 1 stage produced the rejection — ``"existence"``
        (cited file/symbol/line range no longer resolves) or ``"presence"``
        (an LLM confirmed the claimed gap is already closed) or
        ``"duplicate"`` (fingerprint-identical to another pending task).
    reason:
        Gate 1's human-readable explanation for the rejection.
    """

    task_id: str
    title: str
    instruction: str
    target_files: list[str] = field(default_factory=list)
    stage: str = ""
    reason: str = ""


@dataclass
class ValidationReport:
    """Outcome of one :func:`validate_plan` call."""

    checked: int
    kept: list[str]
    removed: list[RemovedTask]
    presence_check_skipped: bool = False
    presence_check_skip_reason: str = ""
    # AUTO-REMOVE-GUARD-1: tasks Gate 1 could not reach a verdict on this
    # run (an LLM-call technical failure survived every retry — see
    # gate1_filter.py's AUTO-RETRY-BACKOFF-1) — left completely untouched
    # in plan.json (still `todo`), NOT removed. Reusing RemovedTask's
    # shape purely for its title/instruction/target_files/reason fields;
    # nothing in this list was actually removed from anywhere.
    inconclusive: "list[RemovedTask]" = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Task dict <-> CandidateTask
# ─────────────────────────────────────────────────────────────────────────────

def _task_to_candidate(task: dict) -> CandidateTask:
    """Reconstruct the :class:`CandidateTask` Gate 1 expects from a plan.json task dict.

    ``PrioritisedBacklog.to_state_tasks`` is the inverse of this (see
    ``backlog_prioritiser.py``) — ``cited_locations[0]`` there is written
    with exactly the ``{file, symbol, line_start, line_end, new_file}``
    shape :class:`CitedLocation` expects, so round-tripping it back is a
    direct field mapping. A task with no recorded citation (e.g. one that
    was hand-added to plan.json rather than produced by the Architect) falls
    back to its first ``target_files`` entry with no anchor — the best
    grounding available without a symbol/line citation; Gate 1's existence
    check will require that file to exist.
    """
    locs = task.get("cited_locations") or []
    loc0 = locs[0] if locs else None
    if isinstance(loc0, dict):
        cited_location = CitedLocation(
            file=loc0.get("file", ""),
            symbol=loc0.get("symbol"),
            line_start=loc0.get("line_start"),
            line_end=loc0.get("line_end"),
            new_file=bool(loc0.get("new_file", False)),
        )
    else:
        # No citation at all, OR a malformed cited_locations[0] that isn't
        # the expected dict shape (e.g. a bare string from a hand-edited
        # plan.json). Either way, fall back to the first target_files entry
        # with no anchor rather than crashing on loc0.get(...).
        target = task.get("target_files") or [""]
        cited_location = CitedLocation(file=target[0])

    return CandidateTask(
        title=task.get("title", ""),
        instruction=task.get("instruction", ""),
        target_files=list(task.get("target_files") or []),
        acceptance_check=task.get("acceptance_check", ""),
        cited_location=cited_location,
        cluster=task.get("cluster", ""),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Core validation
# ─────────────────────────────────────────────────────────────────────────────

def _presence_check_skip_reason(
    cfg: configparser.ConfigParser, task_mode: str,
) -> str:
    """Return why Gate 1's Stage B (LLM presence check) will be skipped for
    every candidate this run, or ``""`` if it will run normally.

    Mirrors the exact two conditions ``Gate1Filter.filter()`` checks —
    ``self._skip_llm or self._task_mode == "creative"`` — so this warning
    can never drift out of sync with what Gate 1 actually does. Both are
    legitimate, deliberate settings (``skip_llm`` for a fast/offline
    existence-only pass; creative mode's AUTO-CR-8 rule because a new/empty
    story chapter has no existing content to check "is the problem still
    present?" against) — but silently applying them here means a task whose
    citation is still valid always comes back "confirmed", even if the
    claimed problem was fixed by hand ten minutes ago. That's easy to
    mistake for a real re-check (this project ships several presets —
    agents_32k.ini, agents_64k.ini, agents_128k.ini, agents_stub.ini — that
    default to task_mode = creative, unlike agents.ini / agents_4k.ini's
    task_mode = code).
    """
    if cfg.getboolean("gate1", "skip_llm", fallback=False):
        return "[gate1] skip_llm=true"
    if task_mode != "code":
        return f"[auto] task_mode={task_mode!r} (non-code modes skip Stage B by design)"
    return ""


def validate_plan(
    base_dir: "str | Path",
    config_path: str = "agents.ini",
) -> ValidationReport:
    """Re-check every ``todo`` / ``in_progress`` task in an existing plan.

    Parameters
    ----------
    base_dir:
        Project root — must already contain ``.agent/plan.json`` (built by
        a prior ``--auto "<goal>"`` run, dry-run or not).
    config_path:
        Path to ``agents.ini``.

    Returns
    -------
    ValidationReport

    Raises
    ------
    RuntimeError
        If ``.agent/plan.json`` does not exist yet — this function
        re-validates a plan, it does not build one.
    """
    base_path = Path(base_dir).resolve()
    agent_dir = base_path / ".agent"
    plan_path = agent_dir / "plan.json"

    if not plan_path.exists():
        raise RuntimeError(
            f"No plan found at {plan_path} — --validate-plan re-checks an "
            f"existing plan, it does not build one.\n"
            f'Run `python main.py --auto "<goal>" --dry-run --base '
            f"{base_path}` first to generate it, then re-run --validate-plan."
        )

    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    # AUTO-FIX (medium-priority audit): a malformed (present but broken)
    # config file used to raise a raw configparser.Error mid-validate-plan
    # with no path context. Fall back to defaults (same as a missing file
    # already did) rather than letting the whole --validate-plan run die
    # on a config typo — this call only re-checks an existing plan, it
    # doesn't need [auto]/[gate1_validate] to be perfect to do that.
    if Path(config_path).exists():
        try:
            cfg.read(config_path, encoding="utf-8")
        except configparser.Error as exc:
            logger.warning(
                "validate_plan: %s is malformed (%s) — proceeding with "
                "defaults for all config-driven options", config_path, exc,
            )
    raw_mode = cfg.get("auto", "task_mode", fallback="code")
    task_mode, mode_warning = normalize_task_mode(raw_mode)
    if mode_warning:
        logger.warning("validate_plan: %s", mode_warning)

    state = StateStore(agent_dir)
    # StateStore.initialise() only ever compares the incoming goal against
    # the one already on disk (see its docstring) — reusing the stored goal
    # here always takes the resume/load path and never raises the
    # "different goal" guard, regardless of what goal the plan was built
    # under.
    # AUTO-FIX (high-priority audit): reading plan.json here used to be
    # completely unguarded, so a corrupted plan.json (interrupted write,
    # manual edit gone wrong) crashed --validate-plan itself — the exact
    # command an operator runs to check a plan's health — with a bare,
    # path-less JSONDecodeError instead of a clear, actionable message.
    try:
        stored_goal = json.loads(plan_path.read_text(encoding="utf-8")).get("goal", "")
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"validate_plan: {plan_path} could not be read as JSON ({exc}). "
            f"The plan file may be corrupted or was interrupted mid-write. "
            f"Check for a {plan_path}.bak snapshot, or re-run "
            f'`python main.py --auto "<goal>" --dry-run --base {base_path}` '
            f"to regenerate it."
        ) from exc
    state.initialise(stored_goal, base_path)

    pending = [
        t for t in state.all_tasks()
        if t["status"] in (STATUS_TODO, STATUS_IN_PROGRESS)
    ]
    if not pending:
        logger.info("validate_plan: no todo/in_progress tasks — nothing to check")
        state.log("validate-plan: no todo/in_progress tasks — nothing to check")
        return ValidationReport(checked=0, kept=[], removed=[])

    candidates = [_task_to_candidate(t) for t in pending]
    # Gate 1 works on CandidateTask objects and knows nothing about plan.json
    # task ids. filter_candidates() preserves object identity for every
    # candidate it accepts or wraps in a FilterResult (see gate1_filter.py:
    # Stage A/B/C all append the SAME object references, never copies), so
    # mapping by id() back to the originating task is exact.
    task_id_of = {id(c): t["id"] for c, t in zip(candidates, pending)}

    skip_reason = _presence_check_skip_reason(cfg, task_mode)
    if skip_reason:
        warning = (
            f"Gate 1's LLM presence check is disabled ({skip_reason}) — this "
            f"run only re-checks that each task's cited file/symbol/line "
            f"range still exists. It will NOT verify whether the claimed "
            f"problem is still actually present, so every task whose "
            f"citation is still valid will come back \"confirmed\" here even "
            f"if it was already fixed by hand. To run the real check, point "
            f"--config at a config with task_mode = code and skip_llm = false "
            f"(e.g. agents.ini)."
        )
        logger.warning("validate_plan: %s", warning)
        print(f"⚠️  {warning}")

    # AUTO-H2-4: optional independent verification model. Re-confirming a
    # candidate with the SAME model that proposed it (the default — no
    # [gate1_validate] section) is a consistency check, not independent
    # verification: a model's own systematic blind spot (this session found
    # three false positives from one such blind spot, see AUTO-H2 epic)
    # reproduces identically on a second call with the same weights and
    # temperature=0. Setting [gate1_validate] model (and optionally active)
    # in the config passed to --validate-plan points this specific re-check
    # at a different model without touching live --auto's Gate 1 at all —
    # tools/auto/pipeline.py's call site never passes these overrides.
    model_override  = cfg.get("gate1_validate", "model",  fallback=None)
    active_override = cfg.get("gate1_validate", "active", fallback=None)
    if model_override or active_override:
        logger.info(
            "validate_plan: using independent verification model (active=%s, model=%s)",
            active_override or "<same as [api] active>",
            model_override or "<default for that active profile>",
        )

    print(f"\n🔎 Re-validating {len(pending)} pending task(s) against current code...")
    # GATE1-CTX-1/-2: same collect wiring as --auto's live Gate 1 call site
    # (tools/auto/pipeline.py via AutoController._get_collect_bridge) — a
    # single bridge built once for this whole validate-plan run, never per
    # candidate. None when [collect] use_in_auto/use_in_doc is off or the
    # artifact is unavailable/stale — every note it feeds degrades to "" in
    # that case, identical to today's behavior.
    from tools.auto.collect_bridge import make_collect_bridge
    collect_bridge = make_collect_bridge(base_path, cfg, config_path, task_mode=task_mode)
    accepted, rejected = filter_candidates(
        candidates, base_path, cfg, cluster_files=None, task_mode=task_mode,
        model_override=model_override, active_override=active_override,
        collect_bridge=collect_bridge,
    )

    removed: list[RemovedTask] = []
    inconclusive: list[RemovedTask] = []
    for fr in rejected:
        tid = task_id_of[id(fr.candidate)]
        record = RemovedTask(
            task_id=tid,
            title=fr.candidate.title,
            instruction=fr.candidate.instruction,
            target_files=list(fr.candidate.target_files),
            stage=fr.stage,
            reason=fr.reason,
        )
        if _is_technical_failure(fr.reason):
            # AUTO-REMOVE-GUARD-1: a technical failure (LLM call/parse
            # error surviving every retry — see gate1_filter.py's
            # AUTO-RETRY-BACKOFF-1) is NOT evidence the task is already
            # fixed; it just means Gate 1 couldn't reach a verdict this
            # run, e.g. a provider outage. Removing it from plan.json
            # here would be indistinguishable from a genuine "confirmed
            # already fixed" rejection, but WRONG — the task might still
            # be fully real and unaddressed. Leave it completely
            # untouched (still `todo`, no plan.json/IMPROVEMENTS.md/
            # IMPROVEMENTS-FALSE.md writes at all) so the next
            # --validate-plan run re-checks JUST this one instead of
            # either silently discarding it or forcing a full re-run of
            # the whole plan from scratch.
            inconclusive.append(record)
            continue
        removed.append(record)

    kept = [task_id_of[id(c)] for c in accepted]

    if removed:
        for r in removed:
            state.remove_task(r.task_id)
        _remove_from_improvements_md(base_path, [r.task_id for r in removed])
        _append_false_positives(base_path, removed)
        _commit_validation(base_path, cfg, removed)

    if inconclusive:
        logger.warning(
            "validate_plan: %d task(s) left unchanged (still todo) after "
            "a technical failure — re-run --validate-plan to retry just "
            "these: %s",
            len(inconclusive), ", ".join(r.task_id for r in inconclusive),
        )

    state.log(
        f"validate-plan: checked {len(pending)} task(s) — "
        f"{len(kept)} confirmed, {len(removed)} removed as false "
        f"positive(s)"
        + (f", {len(inconclusive)} left unresolved (technical failure)"
           if inconclusive else "")
    )

    return ValidationReport(
        checked=len(pending), kept=kept, removed=removed,
        presence_check_skipped=bool(skip_reason),
        presence_check_skip_reason=skip_reason,
        inconclusive=inconclusive,
    )


# ─────────────────────────────────────────────────────────────────────────────
# IMPROVEMENTS.md — strip the false positive's section
# ─────────────────────────────────────────────────────────────────────────────

# Matches from a task's "### <id>: ..." heading up to (but not including)
# either the next task heading, the start of the Manual Suggestions section,
# or end of file. DOTALL so it spans the whole multi-line entry (including
# fenced code blocks).
#
# The closing lookahead is deliberately the exact literal
# "\n## Manual Suggestions" rather than a generic "\n## " — a task's own
# acceptance_check or instruction text can legitimately contain a line
# starting with "## " (a shell comment, a markdown snippet quoted in the
# instruction, ...), and a generic boundary would truncate that entry's
# removal early, leaving its tail behind as orphaned text.
def _task_section_pattern(task_id: str) -> re.Pattern:
    return re.compile(
        r"### " + re.escape(task_id) + r":.*?(?=\n### |\n## Manual Suggestions|\Z)",
        re.DOTALL,
    )


_AUTO_SECTION_EMPTY_NOTE = (
    "_All autonomous tasks originally planned here were confirmed as false "
    "positives during --validate-plan — see IMPROVEMENTS-FALSE.md._\n\n"
)


def _remove_from_improvements_md(base_dir: Path, removed_ids: "list[str]") -> None:
    """Strip each removed task's section out of IMPROVEMENTS.md, if present.

    IMPROVEMENTS.md (``plan_emitter.IMPROVEMENTS_FILENAME``) is rendered
    once, at plan time, by ``to_improvements_md`` and is a plain static file
    after that — nothing re-reads or re-renders it later. Without this step,
    a task removed by validate_plan() would vanish from plan.json and show
    up in IMPROVEMENTS-FALSE.md while its ``### AUTO-Tn: ...`` entry stayed
    behind here, so the two files would openly disagree about whether the
    task is still planned.

    This performs a targeted regex removal per id rather than re-rendering
    the whole file from a ``PrioritisedBacklog`` (which validate_plan()
    doesn't have — only ``plan_emitter.emit()``, run once at plan time,
    ever builds one), so every section this run doesn't touch is left
    byte-for-byte unchanged.

    No-ops silently if IMPROVEMENTS.md doesn't exist (e.g. plan.json was
    hand-seeded rather than produced by a real ``--auto ... --dry-run``) or
    if none of *removed_ids* actually appear in it.
    """
    md_path = base_dir / IMPROVEMENTS_FILENAME
    if not md_path.exists() or not removed_ids:
        return

    original = md_path.read_text(encoding="utf-8")
    text = original
    for task_id in removed_ids:
        text = _task_section_pattern(task_id).sub("", text)

    if text == original:
        return  # none of removed_ids had a section here — nothing to write

    # Collapse any run of 3+ blank lines left behind by a removed section.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # If every "### " entry under "## Autonomous Tasks" is now gone, leave a
    # note instead of a heading with nothing under it.
    auto_section = re.search(
        r"## Autonomous Tasks\n\n(.*?)(?=\n## Manual Suggestions|\Z)",
        text, re.DOTALL,
    )
    if (
        auto_section
        and "### " not in auto_section.group(1)
        and "_No autonomous tasks" not in auto_section.group(1)
        and "_All autonomous tasks" not in auto_section.group(1)
    ):
        text = text.replace(
            "## Autonomous Tasks\n\n",
            "## Autonomous Tasks\n\n" + _AUTO_SECTION_EMPTY_NOTE,
            1,
        )

    atomic_write_text(md_path, text)
    logger.info(
        "_remove_from_improvements_md: stripped %d section(s) from %s",
        len(removed_ids), md_path,
    )


# ─────────────────────────────────────────────────────────────────────────────
# IMPROVEMENTS-FALSE.md
# ─────────────────────────────────────────────────────────────────────────────

_FALSE_MD_HEADER = """\
# IMPROVEMENTS-FALSE.md

Tasks that were once planned in `IMPROVEMENTS.md` / `.agent/plan.json` and
were later confirmed — by re-running the same Gate 1 check the Architect
uses at plan time, against the code as it stands now — to be false
positives. They have been removed from the active plan and are recorded
here instead of being silently dropped, so a human can see what the agent
decided NOT to do, and why. None of these are required to do.

Generated / appended by `python main.py --validate-plan` (AUTO-H1).
"""


def _render_entry(r: RemovedTask) -> str:
    targets = ", ".join(f"`{f}`" for f in r.target_files) or "_none recorded_"
    return (
        f"### {r.task_id}: {r.title}\n\n"
        f"**Target files:** {targets}  \n"
        f"**Gate 1 stage:** `{r.stage}`  \n"
        f"**Reason:** {r.reason}  \n"
        f"**Checked at:** {_ts()}\n\n"
        f"**Original instruction:**\n\n{r.instruction}\n\n---\n\n"
    )


def _already_recorded_ids(md_text: str) -> set[str]:
    """Return the task ids already present as ``### <id>: ...`` headings."""
    ids: set[str] = set()
    for line in md_text.splitlines():
        if line.startswith("### "):
            ids.add(line[len("### "):].split(":", 1)[0].strip())
    return ids


def _append_false_positives(base_dir: Path, removed: "list[RemovedTask]") -> None:
    """Append *removed* to IMPROVEMENTS-FALSE.md at the repo root.

    Read-modify-write, written back with :func:`atomic_write_text` rather
    than a plain ``open(..., "a")`` — consistent with every other
    agent-owned file in this codebase (plan.json, progress.json,
    synopsis.md, cluster_hashes.json all use the same temp-file +
    ``os.replace`` pattern) so a mid-write kill can't leave a half-written
    entry behind.

    Idempotent per task id: re-running --validate-plan after a task id has
    already been recorded here does not duplicate that entry.
    """
    md_path = base_dir / IMPROVEMENTS_FALSE_FILENAME
    existing = md_path.read_text(encoding="utf-8") if md_path.exists() else _FALSE_MD_HEADER

    already = _already_recorded_ids(existing)
    new_entries = [_render_entry(r) for r in removed if r.task_id not in already]
    if not new_entries:
        logger.debug("_append_false_positives: all task ids already recorded — no write")
        return

    atomic_write_text(md_path, existing.rstrip() + "\n\n" + "".join(new_entries))
    logger.info(
        "_append_false_positives: recorded %d new entr%s in %s",
        len(new_entries), "y" if len(new_entries) == 1 else "ies", md_path,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Git commit
# ─────────────────────────────────────────────────────────────────────────────

def _commit_validation(
    base_dir: Path,
    cfg: configparser.ConfigParser,
    removed: "list[RemovedTask]",
) -> Optional[str]:
    """Commit IMPROVEMENTS-FALSE.md with the agent identity.

    Guarded exactly like ``AutoController._setup_git``: a git failure (git
    not installed, base_dir can't be made a repo, etc.) is logged but never
    raises — the plan has already been quarantined in plan.json regardless
    of whether git is available to record the change. ``.agent/`` itself is
    gitignored (see ``git_manager.py``'s ``_GITIGNORE_ENTRIES``), so this
    commit only ever picks up IMPROVEMENTS-FALSE.md (and any other
    already-tracked/untracked repo files a human happened to touch).
    """
    try:
        git = make_git_manager(base_dir, cfg)
    except GitError as exc:
        logger.warning("_commit_validation: git setup failed — %s", exc)
        return None

    n = len(removed)
    msg = f"auto({_COMMIT_TASK_ID}): validate plan — {n} false positive{'s' if n != 1 else ''} removed"
    try:
        commit_hash = git.commit(msg)
    except GitError as exc:
        logger.warning("_commit_validation: commit failed — %s", exc)
        return None
    if commit_hash:
        logger.info("_commit_validation: committed as %s", commit_hash[:12])
    return commit_hash


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_validate(
    base_dir: "str | Path" = ".",
    config_path: str = "agents.ini",
) -> int:
    """Run :func:`validate_plan`, print a report, return a process exit code.

    Mirrors ``tools.auto.controller.run_auto``'s error-handling contract:
    known, actionable errors (no plan yet, a missing/malformed agents.ini
    key) print as ``Error: ...`` with no traceback; anything unexpected is
    logged with a traceback and printed as ``Fatal error: ...``. Both return
    1; success (including "nothing to check") returns 0.
    """
    plan_path = Path(base_dir).resolve() / ".agent" / "plan.json"
    print(f"🔎 Validating plan at {plan_path} ...")

    try:
        report = validate_plan(base_dir, config_path)
    except (RuntimeError, configparser.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover — safety net, see run_auto
        logger.exception("Unhandled error in --validate-plan: %s", exc)
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1

    if report.checked == 0:
        print("No todo/in_progress tasks to check — nothing to do.")
        return 0

    print(
        f"Checked {report.checked} task(s): "
        f"{len(report.kept)} confirmed still needed, "
        f"{len(report.removed)} removed as false positive(s)"
        + (f", {len(report.inconclusive)} left unresolved (technical failure)"
           if report.inconclusive else "")
        + "."
    )
    if report.presence_check_skipped:
        print(
            f"⚠️  Note: only file/symbol/line existence was checked this run "
            f"— the LLM \"is this still a real problem\" check did not run "
            f"({report.presence_check_skip_reason}). \"confirmed\" above "
            f"means the citation is still valid, not that anything was "
            f"re-verified as still broken."
        )
    if report.removed:
        for r in report.removed:
            print(f"  - {r.task_id} ({r.stage}): {r.reason}")
        print(
            f"Removed from IMPROVEMENTS.md and plan.json; recorded in "
            f"{IMPROVEMENTS_FALSE_FILENAME}. Committed."
        )
    if report.inconclusive:
        # AUTO-REMOVE-GUARD-1: these were left completely untouched in
        # plan.json (still `todo`) — nothing to commit, nothing removed.
        print(
            f"⚠️  {len(report.inconclusive)} task(s) could not be checked "
            f"due to a technical failure (network/provider error surviving "
            f"every retry) and were left unchanged in plan.json:"
        )
        for r in report.inconclusive:
            print(f"  - {r.task_id}: {r.reason}")
        print("Re-run --validate-plan to retry just these.")

    return 0
