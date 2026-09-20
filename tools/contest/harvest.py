"""tools/contest/harvest.py — KC-5: the round's checks, as a verdict the runner sends back.

`tools/contest/gates.py` scores one worktree into a table row for the operator.
This module combines that row with the agent's own claim of being done — the last
`runs/<agent>/PROGRESS.csv` row for the ticket, written by
`scripts/append_task.py` — and turns the two into a `READY`/`REWORK` verdict whose
reasons are sentences an agent can act on, so the runner (KC-6) can rework in the
same session instead of asking a person to read a diff.

Harvest is mechanical by design: no LLM reads the diff here. The scoring side
(`contest-bench`, the operator) reads code; the runner only needs to say what to
fix. `rework_message` renders a `Harvest` into the text the runner prompts with.

Reasons are reported in a fixed order and `reasons` always lists everything
found, so a `READY` verdict can still carry a non-blocking note — the note is
information, not a stop.

Standard library only.
"""

from __future__ import annotations

import csv
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools.contest.gates import (
    BRIDGE,
    declared_files,
    git,
    judge_worktree,
    run_tests_detail,
)
from tools.contest.workspace import Workspace

__all__ = ["Harvest", "Reason", "harvest", "rework_message"]

#: The reason codes `harvest` may report, in the order it reports them.
REASON_CODES = (
    "no_progress_row",
    "progress_not_done",
    "no_commit",
    "commit_not_on_branch",
    "commits_ne_1",
    "pushed",
    "no_test_file",
    "shrink_changed",
    "off_ticket_files",
    "tests_failed",
)

#: Outcomes that mean "the ticket is done". `append_task.py` upper-cases
#: `--outcome` and rewrites `DONE` onto `FIXED` (the epic-round prompt says
#: `--outcome DONE`; it means FIXED), so a row it wrote says `FIXED`. Both mean
#: done; everything else — `SKIPPED`, `ALREADY-OK`, a typo — does not.
_DONE_OUTCOMES = frozenset({"DONE", "FIXED"})

#: A reason's `text` is one sentence for the agent. `tests_failed` is the one
#: exception and carries the pytest tail on top of its sentence — the failure
#: cause is at the tail, so that is what the agent needs.
TEXT_LIMIT = 200

#: A claim is a commit only when it is written as a hex sha. `HEAD`, `@`, a
#: branch and a tag all resolve in git, so `merge-base --is-ancestor` cannot
#: tell them from a sha — a symbolic name pins nothing, it resolves to
#: something else on every branch it is read from. 7 chars is the shortest
#: length git resolves unambiguously, 40 is a commit object in full.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)


@dataclass(frozen=True)
class Reason:
    """One mechanical objection to accepting a worktree.

    `code` is one of `REASON_CODES`; `text` is one sentence for the agent, at
    most `TEXT_LIMIT` chars, naming the file or the number so nothing has to be
    looked up; `blocking` is False only for `off_ticket_files`, which is
    reported so the operator sees it without stopping the round.
    """

    code: str
    text: str
    blocking: bool = True


@dataclass(frozen=True)
class Harvest:
    """The verdict on one agent's worktree for one ticket.

    `verdict` is `READY` iff no reason is blocking. `reasons` lists everything
    found — a `READY` can still carry the non-blocking off-ticket note. `commit`
    is the full 40-char sha the agent's claim in `PROGRESS.csv` resolves to, or
    None when the row, its commit or its sha is missing or unresolvable.
    `facts` is the `judge_worktree` row (plus `tests_run` when tests ran);
    `elapsed` is the wall time of this harvest in seconds.
    """

    verdict: Literal["READY", "REWORK"]
    reasons: tuple[Reason, ...]
    commit: str | None
    facts: dict
    elapsed: float


def _progress_rows(progress_csv: Path) -> list[dict]:
    """The rows of *progress_csv*, or [] when the file does not exist yet."""
    if not progress_csv.is_file():
        return []
    with open(progress_csv, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _is_ancestor(path: Path, commit: str) -> bool:
    """Whether *commit* is an ancestor of the worktree's HEAD.

    False both when the commit is on another line of history and when it does
    not resolve at all — an unknown sha is certainly not on the branch.
    """
    r = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
        cwd=str(path),
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def _resolve_claim(path: Path, claimed: str) -> str | None:
    """The full 40-char sha *claimed* names in *path*, or None.

    Two checks, in front of `_is_ancestor`: the shape (`_SHA_RE` — `HEAD`, `@`,
    a branch, a tag and a 6-char prefix are refused even though git would
    resolve them; a claim pins one commit, not a name that stands for a
    different commit on every branch it is read from) and
    `git rev-parse --verify --quiet <claimed>^{commit}`, which refuses a hex
    string that is not a commit of this worktree — an ambiguous prefix, a
    typo. The resolution is returned, not the claim as written: a short sha
    becomes the 40-char one, an upper-case one lower-case.
    """
    if not _SHA_RE.fullmatch(claimed):
        return None
    return git(str(path), "rev-parse", "--verify", "--quiet", f"{claimed}^{{commit}}") or None


def harvest(ws: Workspace, ticket_path: Path, *, run_tests: bool = False) -> Harvest:
    """Score one worktree against its ticket and return the verdict.

    *ws* is the agent's checkout (`tools/contest/workspace.py`), *ticket_path*
    its ticket file, and *run_tests* whether to run the four pytest roots —
    kept off by default because it is the slow part of a round.

    Raises `FileNotFoundError` for an unreadable ticket; a runner holding a bad
    ticket path is a bug, not a harvest result.
    """
    start = time.monotonic()
    ticket = Path(ticket_path).name
    declared = declared_files(ticket_path)

    # ── the claim: the last PROGRESS.csv row for this ticket ──────────────
    claim = None
    for row in _progress_rows(ws.progress_csv):
        if (row.get("ticket") or "").strip() == ticket:
            claim = row
    claimed_commit = (claim or {}).get("commit", "").strip()
    outcome = (claim or {}).get("outcome", "").strip().upper()

    progress = f"runs/{ws.agent}/PROGRESS.csv"  # ws.progress_csv, for a short reason
    reasons: list[Reason] = []
    resolved: str | None = None  # the full sha the claim names, once it is on the branch
    if claim is None:
        reasons.append(Reason(
            "no_progress_row",
            f"{progress} has no row for {ticket} — run "
            "append_task.py to record it before the round can be scored",
        ))
    else:
        if outcome not in _DONE_OUTCOMES:
            reasons.append(Reason(
                "progress_not_done",
                f"{progress} says outcome {outcome or 'EMPTY'} for "
                f"{ticket}; the round closes only on DONE",
            ))
        if not claimed_commit:
            reasons.append(Reason(
                "no_commit",
                f"{progress} row for {ticket} has an empty commit — "
                "commit once, then append_task.py --commit <sha>",
            ))
        elif not _SHA_RE.fullmatch(claimed_commit):
            # `HEAD`, `@`, a branch and a tag all resolve in git, so they would
            # pass `merge-base --is-ancestor` too — but a claim must name a
            # commit, not a name that stands for a different one on every
            # branch. Quoted as written (cut to a sha's length), so the agent
            # sees what it put in the row.
            reasons.append(Reason(
                "commit_not_on_branch",
                f"commit {claimed_commit[:40]} is not a sha — commit once, then "
                "append_task.py --commit $(git rev-parse HEAD)",
            ))
        else:
            sha = _resolve_claim(ws.path, claimed_commit)
            if sha is not None and _is_ancestor(ws.path, sha):
                resolved = sha
            else:
                head = git(str(ws.path), "rev-parse", "--short", "HEAD")
                reasons.append(Reason(
                    "commit_not_on_branch",
                    f"commit {(sha or claimed_commit)[:12]} is not an ancestor of HEAD "
                    f"({head or 'unresolved'}) on {ws.branch} — rebase it onto the branch",
                ))

    # ── the facts: the mechanical scorecard row ───────────────────────────
    facts = judge_worktree(ws.agent, str(ws.path), ws.base_sha, list(declared),
                           want_tests=False)

    if "commits" not in facts:
        # A path that is not a git worktree has nothing to score; there is no
        # commit, so this collapses into the commit-count gate with the path named.
        reasons.append(Reason(
            "commits_ne_1",
            f"{ws.path} is not a git worktree — nothing to score",
        ))
    else:
        if facts["commits"] != 1:
            reasons.append(Reason(
                "commits_ne_1",
                f"{facts['commits']} commits on {ws.branch} for {ticket} — amend them "
                "into exactly one commit",
            ))
        if facts["pushed"] == "yes":
            reasons.append(Reason(
                "pushed",
                f"HEAD {facts['sha'][:12]} is reachable from a remote — delete the "
                "remote ref before the round is scored",
            ))
        if facts["test_files"] == 0:
            reasons.append(Reason(
                "no_test_file",
                f"{ticket} shipped no test file — add one under tests/ that fails "
                "without the change",
            ))
        if facts["shrink"] != "same":
            reasons.append(Reason(
                "shrink_changed",
                f"CollectBridge._shrink in {BRIDGE} is {facts['shrink']} against the "
                "base — it must stay byte-identical",
            ))
        if facts["off_ticket"]:
            reasons.append(Reason(
                "off_ticket_files",
                f"touched {facts['off_ticket']} file(s) outside the ticket's declared "
                f"list: {facts['off_ticket_files']}",
                blocking=False,
            ))

    if run_tests:
        summary, tail = run_tests_detail(str(ws.path))
        facts["tests_run"] = summary
        if "✗" in summary:
            reasons.append(Reason(
                "tests_failed",
                f"the tests do not pass: {summary}\n" + "\n".join(tail),
            ))

    verdict = "REWORK" if any(r.blocking for r in reasons) else "READY"
    return Harvest(
        verdict=verdict,
        reasons=tuple(reasons),
        commit=resolved,
        facts=facts,
        elapsed=time.monotonic() - start,
    )


def rework_message(h: Harvest, attempt: int, max_rework: int) -> str:
    """The text the runner sends into the session for a REWORK verdict.

    A fixed header with the attempt counter, one bullet per blocking reason,
    the non-blocking ones under "Also noted", and the two ground rules that
    settle a round. The ticket text is not repeated — the session already has
    it, and repeating it costs context for nothing.
    """
    blocking = [r for r in h.reasons if r.blocking]
    noted = [r for r in h.reasons if not r.blocking]

    lines = [
        "Your ticket is not accepted yet. "
        f"Attempt {attempt} of {max_rework}. "
        "Fix the items below in this same worktree, amend into **one** commit, "
        "and run `append_task.py` again.",
        "",
    ]
    lines += [f"- {r.text}" for r in blocking]
    if noted:
        lines += ["", "Also noted:"]
        lines += [f"- {r.text}" for r in noted]
    lines += [
        "",
        "Ground rules that settle a round:",
        "- `CollectBridge._shrink` stays byte-identical to the base.",
        "- Ship a test that fails without the change.",
    ]
    return "\n".join(lines)
