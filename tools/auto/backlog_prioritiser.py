"""tools/auto/backlog_prioritiser.py — AUTO-B4: Prioritise + attach acceptance checks.

Takes the Gate-1-accepted candidate list from AUTO-B3 and produces two outputs:

1. **Autonomous backlog** — tasks that have a runnable acceptance check, ordered
   so that no task is scheduled before its dependencies are satisfied.
2. **Manual suggestions** — tasks without a runnable acceptance check (pure
   readability / style refactors).  These are recorded in ``IMPROVEMENTS.md``
   under a dedicated heading but are **never** passed to the Coder loop.

Dependency inference (static, no execution)
--------------------------------------------
Dependencies between tasks are inferred from the code structure, not from
anything the LLM says about them:

* **Same-file linear order** — if tasks A and B both touch the same file,
  and A's cited line range ends before B's starts, A is treated as a
  prerequisite of B (upstream change must land first).

* **Cross-file symbol reference** — if task B's ``instruction`` text
  mentions a symbol that is named in task A's ``cited_location.symbol``,
  and A and B target different files (B presumably *calls* what A defines),
  A is added as a soft dependency of B.

These heuristics are intentionally conservative: they only add a dependency
when the evidence is unambiguous.  The resulting DAG is topologically sorted
with Kahn's algorithm; ties within the same topological level are broken by
cluster order (preserving the Architect's original ordering).

Acceptance-check validation
----------------------------
A candidate's ``acceptance_check`` is considered **runnable** when it is
non-empty AND does not match the ``_PLACEHOLDER_RE`` pattern (common LLM
hedges such as ``"N/A"``, ``"manual review"``, ``"none"``, ``"TBD"``).

Public surface consumed by the Architect stage (``controller.py``)::

    from tools.auto.backlog_prioritiser import BacklogPrioritiser, PrioritisedBacklog

    prioritiser = BacklogPrioritiser()
    backlog = prioritiser.build(accepted_candidates)
    # backlog.auto_tasks          — list[ReadyTask], topologically ordered
    # backlog.manual_suggestions  — list[CandidateTask], excluded from auto-run
    # backlog.to_state_tasks()    — list[dict] ready for StateStore.upsert_task()
    # to_improvements_md(backlog) — full IMPROVEMENTS.md text

Configuration (agents.ini [auto])
-----------------------------------
No extra keys required for B4 itself.  The ``task_id_prefix`` key
(default ``"AUTO-T"``) controls the ``id`` prefix assigned to each task.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

from tools.auto.architect import CandidateTask
from tools.auto.state import make_task

logger = logging.getLogger(__name__)

# ── Placeholder detector ──────────────────────────────────────────────────────
# Acceptance checks that match this pattern are treated as non-runnable and
# the candidate is moved to the manual suggestions list.
_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:n/?a|none|tbd|todo|manual\s+review|manual\s+check|"
    r"code\s+review|peer\s+review|human\s+review|review\s+manually|"
    r"no\s+automated\s+check|not\s+applicable|unknown|placeholder)\s*$",
    re.IGNORECASE,
)

# Default prefix for generated task IDs.
_DEFAULT_ID_PREFIX = "AUTO-T"


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReadyTask:
    """A candidate that passed Gate 1 and has a runnable acceptance check.

    Attributes
    ----------
    task_id:
        Unique identifier assigned by the prioritiser, e.g. ``"AUTO-T1"``.
    candidate:
        The underlying :class:`~tools.auto.architect.CandidateTask`.
    dependencies:
        Ordered list of ``task_id`` strings that must be ``DONE`` before this
        task is started.  Empty list means the task is immediately runnable.
    original_index:
        Position in the pre-sort candidate list — used as a tiebreaker to
        preserve Architect ordering within the same topological level.
    """

    task_id: str
    candidate: CandidateTask
    dependencies: list[str] = field(default_factory=list)
    original_index: int = 0

    # ── Convenience passthroughs ──────────────────────────────────────────────

    @property
    def title(self) -> str:
        return self.candidate.title

    @property
    def instruction(self) -> str:
        return self.candidate.instruction

    @property
    def target_files(self) -> list[str]:
        return self.candidate.target_files

    @property
    def acceptance_check(self) -> str:
        return self.candidate.acceptance_check

    @property
    def cited_location(self):  # type: ignore[return]
        return self.candidate.cited_location

    @property
    def cluster(self) -> str:
        return self.candidate.cluster


@dataclass
class PrioritisedBacklog:
    """Output of :class:`BacklogPrioritiser`.

    Attributes
    ----------
    auto_tasks:
        Tasks with runnable acceptance checks, topologically ordered so each
        task appears after all its dependencies.
    manual_suggestions:
        Tasks without runnable acceptance checks; excluded from the Coder loop.
    """

    auto_tasks: list[ReadyTask]
    manual_suggestions: list[CandidateTask]

    # ── Derived views ─────────────────────────────────────────────────────────

    def to_state_tasks(self, *, status: str = "todo") -> list[dict]:
        """Convert all auto_tasks to schema-valid dicts for StateStore.

        Parameters
        ----------
        status:
            Initial task status (default ``"todo"``).

        Returns
        -------
        list[dict]
            In the same order as ``auto_tasks``; ready for
            ``StateStore.upsert_task()``.
        """
        result = []
        for rt in self.auto_tasks:
            loc = rt.cited_location
            cited_loc_dict = {
                "file":       loc.file,
                "symbol":     loc.symbol,
                "line_start": loc.line_start,
                "line_end":   loc.line_end,
            }
            result.append(make_task(
                id               = rt.task_id,
                title            = rt.title,
                instruction      = rt.instruction,
                target_files     = list(rt.target_files),
                acceptance_check = rt.acceptance_check,
                status           = status,
                cited_locations  = [cited_loc_dict],
                dependencies     = list(rt.dependencies),
            ))
        return result

    def summary(self) -> str:
        """One-line summary suitable for a console banner."""
        return (
            f"{len(self.auto_tasks)} auto task(s), "
            f"{len(self.manual_suggestions)} manual suggestion(s)"
        )


# Alias for backward compatibility / test imports
Backlog = PrioritisedBacklog


# ─────────────────────────────────────────────────────────────────────────────
# BacklogPrioritiser
# ─────────────────────────────────────────────────────────────────────────────

class BacklogPrioritiser:
    """Splits Gate-1-accepted candidates and orders the autonomous subset.

    Parameters
    ----------
    task_id_prefix:
        Prefix for generated task IDs.  Default ``"AUTO-T"``.
    """

    def __init__(self, task_id_prefix: str = _DEFAULT_ID_PREFIX) -> None:
        self._prefix = task_id_prefix

    # ── Public API ────────────────────────────────────────────────────────────

    def build(self, candidates: list[CandidateTask]) -> PrioritisedBacklog:
        """Build the prioritised backlog from Gate-1-accepted candidates.

        Parameters
        ----------
        candidates:
            Output of :func:`tools.auto.gate1_filter.Gate1Filter.filter`
            (the accepted list).

        Returns
        -------
        PrioritisedBacklog
            Contains ``auto_tasks`` (ordered) and ``manual_suggestions``.
        """
        # ── 1. Split on acceptance-check runnability ──────────────────────────
        auto_candidates: list[CandidateTask] = []
        manual_suggestions: list[CandidateTask] = []

        for c in candidates:
            if _is_runnable_check(c.acceptance_check):
                auto_candidates.append(c)
            else:
                logger.info(
                    "BacklogPrioritiser: %r has non-runnable acceptance_check %r "
                    "→ manual suggestions",
                    c.title,
                    c.acceptance_check,
                )
                manual_suggestions.append(c)

        print(
            f"\n📋 Backlog split: "
            f"{len(auto_candidates)} auto task(s), "
            f"{len(manual_suggestions)} manual suggestion(s)"
        )

        # ── 2. Assign task IDs ────────────────────────────────────────────────
        ready_tasks = [
            ReadyTask(
                task_id        = f"{self._prefix}{i + 1}",
                candidate      = c,
                original_index = i,
            )
            for i, c in enumerate(auto_candidates)
        ]

        # ── 3. Infer dependencies ─────────────────────────────────────────────
        if len(ready_tasks) > 1:
            self._infer_dependencies(ready_tasks)

        # ── 4. Topological sort ───────────────────────────────────────────────
        ordered = _topological_sort(ready_tasks)

        print(
            f"✅ Backlog ordered — {len(ordered)} auto task(s) ready\n"
        )

        return PrioritisedBacklog(
            auto_tasks         = ordered,
            manual_suggestions = manual_suggestions,
        )

    # ── Dependency inference ──────────────────────────────────────────────────

    def _infer_dependencies(self, tasks: list[ReadyTask]) -> None:
        """Populate ``ReadyTask.dependencies`` in-place.

        Two rules are applied (see module docstring for rationale):

        1. Same-file linear order: if A and B touch the same file and A's
           ``line_end`` is before B's ``line_start``, add A → B.
        2. Cross-file symbol reference: if B's instruction mentions A's
           cited symbol by name, and they don't share a target file, add A → B.
        """
        for i, task_b in enumerate(tasks):
            for j, task_a in enumerate(tasks):
                if i == j:
                    continue
                if task_a.task_id in task_b.dependencies:
                    continue  # already registered

                if self._same_file_upstream(task_a, task_b):
                    task_b.dependencies.append(task_a.task_id)
                    logger.debug(
                        "dep [same-file]: %s → %s",
                        task_a.task_id, task_b.task_id,
                    )
                    continue

                if self._cross_file_symbol_ref(task_a, task_b):
                    task_b.dependencies.append(task_a.task_id)
                    logger.debug(
                        "dep [symbol-ref]: %s → %s",
                        task_a.task_id, task_b.task_id,
                    )

    # ── Dependency heuristics ─────────────────────────────────────────────────

    @staticmethod
    def _same_file_upstream(task_a: ReadyTask, task_b: ReadyTask) -> bool:
        """True if A and B share a target file and A's code is above B's."""
        shared_files = set(task_a.target_files) & set(task_b.target_files)
        if not shared_files:
            return False

        loc_a = task_a.cited_location
        loc_b = task_b.cited_location

        # Both must cite the same file with numeric line anchors.
        if loc_a.file not in shared_files or loc_b.file not in shared_files:
            return False
        if loc_a.file != loc_b.file:
            return False

        a_end   = loc_a.line_end   if loc_a.line_end   is not None else loc_a.line_start
        b_start = loc_b.line_start

        if a_end is None or b_start is None:
            return False

        return a_end < b_start

    @staticmethod
    def _cross_file_symbol_ref(task_a: ReadyTask, task_b: ReadyTask) -> bool:
        """True if B's instruction mentions A's cited symbol and they differ in files."""
        # Only apply when files are truly distinct.
        if set(task_a.target_files) & set(task_b.target_files):
            return False

        symbol_a = task_a.cited_location.symbol
        if not symbol_a:
            return False

        # Require a whole-word match so "parse" doesn't match "parse_config".
        pattern = re.compile(rf"\b{re.escape(symbol_a)}\b")
        return bool(pattern.search(task_b.instruction))


# ─────────────────────────────────────────────────────────────────────────────
# Topological sort (Kahn's algorithm)
# ─────────────────────────────────────────────────────────────────────────────

def _topological_sort(tasks: list[ReadyTask]) -> list[ReadyTask]:
    """Return *tasks* in dependency-first order using Kahn's algorithm.

    Ties at the same topological level are broken by ``original_index`` to
    preserve the Architect's original cluster ordering.

    If the dependency graph contains a cycle (should not happen with the
    conservative heuristics above, but possible if callers inject custom deps),
    the cycle is broken by removing the back-edges and a warning is logged.
    The remaining tasks are appended at the end in original order.

    Parameters
    ----------
    tasks:
        ReadyTask objects with ``dependencies`` already populated.

    Returns
    -------
    list[ReadyTask]
        Same tasks in a valid topological order.
    """
    if not tasks:
        return []

    id_to_task: dict[str, ReadyTask] = {t.task_id: t for t in tasks}
    valid_ids: set[str] = set(id_to_task)

    # Build adjacency: in_degree and reverse map.
    in_degree: dict[str, int] = {t.task_id: 0 for t in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)  # A → [tasks that depend on A]

    for t in tasks:
        for dep_id in t.dependencies:
            if dep_id not in valid_ids:
                logger.debug(
                    "_topological_sort: %s has unknown dep %r — ignored",
                    t.task_id, dep_id,
                )
                continue
            in_degree[t.task_id] += 1
            dependents[dep_id].append(t.task_id)

    # Kahn: start with zero-in-degree nodes, sorted by original_index.
    queue: deque[ReadyTask] = deque(
        sorted(
            (id_to_task[tid] for tid, deg in in_degree.items() if deg == 0),
            key=lambda t: t.original_index,
        )
    )

    ordered: list[ReadyTask] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)

        # Reduce in-degree for nodes that depend on this one; enqueue new zeros.
        newly_free: list[ReadyTask] = []
        for dep_task_id in dependents[node.task_id]:
            in_degree[dep_task_id] -= 1
            if in_degree[dep_task_id] == 0:
                newly_free.append(id_to_task[dep_task_id])

        # Sort newly-freed tasks by original_index before enqueuing.
        newly_free.sort(key=lambda t: t.original_index)
        queue.extend(newly_free)

    # Handle cycles: any tasks not yet in ordered have unresolved deps.
    if len(ordered) < len(tasks):
        remaining_ids = set(id_to_task) - {t.task_id for t in ordered}
        logger.warning(
            "_topological_sort: cycle detected involving %d task(s): %s — "
            "appending in original order",
            len(remaining_ids),
            sorted(remaining_ids),
        )
        for t in sorted(
            (id_to_task[tid] for tid in remaining_ids),
            key=lambda t: t.original_index,
        ):
            ordered.append(t)

    return ordered


# ─────────────────────────────────────────────────────────────────────────────
# IMPROVEMENTS.md generator
# ─────────────────────────────────────────────────────────────────────────────

_MD_HEADER = """\
# IMPROVEMENTS.md
"""

_MD_AUTO_SECTION = "## Autonomous Tasks\n\n"
_MD_MANUAL_SECTION = "## Manual Suggestions\n\n"
_MD_MANUAL_NOTE = (
    "> The following improvements do not have a runnable acceptance check "
    "and **will not** be executed automatically.  They are recorded here "
    "for human follow-up.\n\n"
)


def to_improvements_md(backlog: PrioritisedBacklog) -> str:
    """Render *backlog* as a human-readable ``IMPROVEMENTS.md`` string.

    Format
    ------
    The file has two sections:

    * **Autonomous Tasks** — numbered, with task ID, title, instruction,
      target files, acceptance check, dependencies, and cited location.
    * **Manual Suggestions** — unnumbered bullets with title, instruction,
      and cited location.

    Parameters
    ----------
    backlog:
        A :class:`PrioritisedBacklog` from :meth:`BacklogPrioritiser.build`.

    Returns
    -------
    str
        The full Markdown content, ready to write to ``IMPROVEMENTS.md``.
    """
    parts: list[str] = [_MD_HEADER]

    # ── Autonomous tasks ──────────────────────────────────────────────────────
    parts.append(_MD_AUTO_SECTION)
    if not backlog.auto_tasks:
        parts.append("_No autonomous tasks were identified._\n\n")
    else:
        for rt in backlog.auto_tasks:
            loc = rt.cited_location
            loc_str = loc.file
            if loc.symbol:
                loc_str += f" → `{loc.symbol}`"
            if loc.line_start is not None:
                end = loc.line_end if loc.line_end is not None else loc.line_start
                loc_str += f" (lines {loc.line_start}–{end})"

            dep_str = (
                ", ".join(f"`{d}`" for d in rt.dependencies)
                if rt.dependencies
                else "none"
            )

            parts.append(
                f"### {rt.task_id}: {rt.title}\n\n"
                f"**Cluster:** {rt.cluster}  \n"
                f"**Location:** `{loc_str}`  \n"
                f"**Target files:** {', '.join(f'`{f}`' for f in rt.target_files)}  \n"
                f"**Dependencies:** {dep_str}  \n"
                f"**Acceptance check:**\n```\n{rt.acceptance_check}\n```\n\n"
                f"**Instruction:**\n\n{rt.instruction}\n\n---\n\n"
            )

    # ── Manual suggestions ────────────────────────────────────────────────────
    parts.append(_MD_MANUAL_SECTION)
    if not backlog.manual_suggestions:
        parts.append("_No manual suggestions._\n")
    else:
        parts.append(_MD_MANUAL_NOTE)
        for c in backlog.manual_suggestions:
            loc = c.cited_location
            loc_str = loc.file
            if loc.symbol:
                loc_str += f" → `{loc.symbol}`"
            if loc.line_start is not None:
                end = loc.line_end if loc.line_end is not None else loc.line_start
                loc_str += f" (lines {loc.line_start}–{end})"

            parts.append(
                f"- **{c.title}** \n"
                f"  Location: `{loc_str}`  \n"
                f"  {c.instruction}\n\n"
            )

    return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory
# ─────────────────────────────────────────────────────────────────────────────

def build_backlog(
    candidates: list[CandidateTask],
    task_id_prefix: str = _DEFAULT_ID_PREFIX,
) -> PrioritisedBacklog:
    """One-call entry point for ``AutoController``.

    Parameters
    ----------
    candidates:
        Gate-1-accepted :class:`~tools.auto.architect.CandidateTask` list.
    task_id_prefix:
        Prefix for generated task IDs (default ``"AUTO-T"``).

    Returns
    -------
    PrioritisedBacklog
    """
    return BacklogPrioritiser(task_id_prefix=task_id_prefix).build(candidates)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_runnable_check(check: str) -> bool:
    """Return True if *check* looks like a runnable shell command.

    A check is considered **non-runnable** when it is empty or matches
    ``_PLACEHOLDER_RE`` (human-review hedges produced by some LLMs).
    """
    s = (check or "").strip()
    if not s:
        return False
    return not bool(_PLACEHOLDER_RE.match(s))