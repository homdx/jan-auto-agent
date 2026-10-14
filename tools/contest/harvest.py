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

With `run_tests` the roots run on the claim's commit, not on the worktree they
are read from: the commit is checked out into a throwaway worktree, the roots
run there, and the worktree is removed again in `finally`. The agent's tree is
read and left byte for byte as it was — no stash, no clean, no checkout — so an
untracked file cannot make a red commit score `READY` and an uncommitted fix
cannot make a green one score `REWORK`. What the tree holds beyond the commit is
reported rather than hidden: the non-blocking `uncommitted_files` reason, whose
noise filter is the repo's own `.gitignore`, never a list here.

Standard library only.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tools.contest.gates import (
    BRIDGE,
    DEADLINE_COMMIT_EMAIL,
    declared_files,
    git,
    judge_worktree,
    run_tests_detail,
    _slow_tests,
)
from tools.contest.workspace import Workspace
from tools.git_run import run_git

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
    "tests_slow",
    "uncommitted_files",
)

#: Outcomes that count as done; append_task.py rewrites DONE to FIXED, so both are accepted.
_DONE_OUTCOMES = frozenset({"DONE", "FIXED"})

#: Max length of a reason's text; tests_failed and uncommitted_files may run longer (their payload).
TEXT_LIMIT = 200

#: How many `git status` lines `uncommitted_files` names; the rest is counted.
UNCOMMITTED_MAX = 10

#: A commit claim must be a hex sha; HEAD, branch and tag names resolve to different commits.
_SHA_RE = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)


@dataclass(frozen=True)
class Reason:
    """One mechanical objection to accepting a worktree.

    `code` is one of `REASON_CODES`; `text` is one sentence for the agent, at
    most `TEXT_LIMIT` chars, naming the file or the number so nothing has to be
    looked up; `blocking` is False only for `off_ticket_files` and
    `uncommitted_files`, which are reported so the operator sees them without
    stopping the round.
    """

    code: str
    text: str
    blocking: bool = True


@dataclass(frozen=True)
class Harvest:
    """The verdict on one agent's worktree for one ticket.

    `verdict` is `READY` iff no reason is blocking. `reasons` lists everything
    found — a `READY` can still carry the non-blocking off-ticket note. `commit`
    is the full 40-char sha the agent's claim in `PROGRESS.csv` resolves to;
    when there is no row at all, KC-30 names the branch's one commit instead —
    the sha a patch is formatted from, so a stalled agent that never ran
    `append_task.py` still hands the operator something. None when the claim
    cannot be resolved, and always for a branch with two commits or none:
    an ambiguous branch names no sha, whatever a row says.
    `facts` is the `judge_worktree` row (plus `tests_run` when tests ran);
    `elapsed` is the wall time of this harvest in seconds.
    """

    verdict: Literal["READY", "REWORK"]
    reasons: tuple[Reason, ...]
    commit: str | None
    facts: dict
    elapsed: float
    #: Seconds waited for the round's pytest lock before the roots ran (KC-57); 0 unless serialised.
    waited: float = 0.0
    #: KC-57: agents ahead in the pytest queue when this harvest waited.
    ahead: int = 0


def _progress_rows(progress_csv: Path) -> list[dict]:
    """The rows of *progress_csv*, or [] when the file does not exist yet.

    A queue file the reader cannot answer is "no rows", not a raise: a NUL
    byte (a binary tool wrote into it) or an invalid UTF-8 byte (an editor
    saved it in another encoding) degrades the claim to absent, the way an
    unreadable index degrades `_status_lines` — the harvest settles on
    `no_progress_row` instead of dying on the runner's own file.
    """
    if not progress_csv.is_file():
        return []
    try:
        with open(progress_csv, newline="", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return []
    # A NUL is checked here, so the verdict does not depend on the Python version's csv behaviour.
    if "\0" in text:
        return []
    try:
        return list(csv.DictReader(io.StringIO(text, newline="")))
    except csv.Error:
        return []


def _is_ancestor(path: Path, commit: str) -> bool:
    """Whether *commit* is an ancestor of the worktree's HEAD.

    False both when the commit is on another line of history and when it does
    not resolve at all — an unknown sha is certainly not on the branch.
    """
    try:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _at_or_under_base(ws: Workspace, commit: str) -> bool:
    """Whether *commit* is the round's base or one of its ancestors — a claim
    that names no commit of the agent's change. False with no base to compare."""
    base = getattr(ws, "base_sha", "") or ""
    if not base:
        return False
    try:
        r = subprocess.run(["git", "merge-base", "--is-ancestor", commit, base],
                           cwd=str(ws.path), capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
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


def _status_lines(ws: Workspace) -> list[str]:
    """The worktree's `git status --porcelain` lines: what the tree holds that
    the commit does not.

    `git` applies the repo's own `.gitignore` and never reports an ignored
    path, so this carries no exclusion list of its own — nothing here knows a
    tier directory or a test root. The one thing left out is the runner's own
    ground: the directory `ws.progress_csv` lives in, as `Workspace` names it
    (the harvest reads the claim from there). A repo that does not ignore it
    would otherwise hear, every harvest, that its queue file is missing from the
    commit — an invitation to commit the runner's file. `--no-optional-locks`
    keeps the read a read: `git status` would otherwise refresh and rewrite
    the tree's index. Empty when git cannot answer (not a worktree, an
    unreadable index): a missing answer means nothing was learned, and is
    never a raise.
    """
    try:
        r = run_git(["git", "--no-optional-locks", "status", "--porcelain",
                     "--untracked-files=all"], cwd=str(ws.path))
    except OSError:
        return []
    if r.returncode:
        return []
    ground = ws.progress_csv.parent.relative_to(ws.path).as_posix() + "/"
    lines = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        name = line[3:].rsplit(" -> ", 1)[-1].strip('"')
        if (name + "/").startswith(ground):
            continue
        lines.append(line)
    return lines


def _commit_worktree(ws: Workspace, commit: str) -> tuple[str | None, str | None, str]:
    """A throwaway detached worktree checked out at *commit*, for the roots.

    Returns `(target, parent, error)`: *target* is where the roots run and
    *parent* the temp directory to delete with it. `(None, None, error)` when
    no checkout could be made — and then the roots do not run at all. They
    never fall back to *ws*: a tree green only because of what the commit
    lacks is exactly what this harvest must not score, and a silent fallback
    would score it the moment git stumbles.

    `git worktree add --detach` checks out exactly what git has at *commit*: a
    committed relative symlink resolves inside the new worktree, and anything
    the tree holds that is not committed is simply not there. Nothing here
    knows this repo's layout — no tier name, no test root — because the
    checkout is git's own and the roots are the caller's to name.
    """
    try:
        parent = tempfile.mkdtemp(prefix="kc60-commit-")
    except OSError as exc:
        return None, None, f"no temp directory for the checkout: {exc}"
    target = os.path.join(parent, "commit")
    try:
        r = run_git(["git", "worktree", "add", "-q", "--detach", target, commit],
                    cwd=str(ws.path))
        error = "" if r.returncode == 0 and os.path.isdir(target) else (
            (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}")
    except OSError as exc:
        error = str(exc)
    if error:
        _drop_worktree(ws, parent)
        return None, None, error
    return target, parent, ""


def _drop_worktree(ws: Workspace, parent: str | None) -> None:
    """Remove the throwaway checkout, on the success path and on an exception.

    `git worktree remove --force` unregisters it (the roots may have left a
    pytest cache in it, which is why the `--force`); the `rmtree` takes
    whatever git did not. Only when git could not remove it does `worktree
    prune` drop the registration the deleted folder left behind — never on
    the ordinary path, where there is nothing stale to prune. None of it
    touches *ws.path* — only the copy is dropped.
    """
    if parent is None:
        return
    removed = False
    try:
        removed = run_git(["git", "worktree", "remove", "--force",
                           os.path.join(parent, "commit")], cwd=str(ws.path)).returncode == 0
    except OSError:
        pass
    shutil.rmtree(parent, ignore_errors=True)
    if not removed:
        try:
            run_git(["git", "worktree", "prune"], cwd=str(ws.path))
        except OSError:
            pass


def _branch_commit(path: Path, facts: dict) -> str | None:
    """KC-30: the full sha of the branch's one commit, from the scorecard row.

    `facts["sha"]` is the short sha `judge_worktree` counted for the commit, so
    it is resolved back here to the 40-char form `_resolve_claim` returns and
    `Harvest.commit` keeps one shape. `git rev-parse HEAD` is the fallback, and
    it is the same commit — with exactly one on the branch, the scorecard
    counted the tip.

    None when there is not exactly one commit to name — two (amend them into
    one), none (nothing to export) — an ambiguous branch names no sha. None for
    a `facts` row without `commits` (a path git will not read), and for a git
    that cannot answer: a sha that cannot be resolved is no sha, never an
    exception into the harvest.
    """
    if facts.get("commits") != 1:
        return None
    sha = facts.get("sha")
    if isinstance(sha, str) and sha:
        try:
            full = git(str(path), "rev-parse", "--verify", "--quiet",
                       f"{sha}^{{commit}}")
        except (OSError, subprocess.TimeoutExpired):
            full = ""
        if full:
            return full
    try:
        return git(str(path), "rev-parse", "HEAD") or None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _last_tail_section(tail: list[str]) -> list[str]:
    """The tail's last `--- ` section — the root that used the budget.

    `run_tests_detail` keeps one section per root that did not pass, so a root
    that finished in time can still carry its own durations table in the same
    list. The budget's reason must name the root that ran out of time, not the
    slowest test of the one that did not.
    """
    for i in range(len(tail) - 1, -1, -1):
        if tail[i].startswith("--- "):
            return tail[i:]
    return tail


def _tests_slow_reason(budget: float, tail: list[str]) -> Reason:
    """`tests_slow`: the roots blew the harvest's wall-clock budget.

    The reason names the budget, so the rework prompt tells the agent to make
    the suite faster rather than to fix a red test — and names where the time
    went, when pytest said so: the durations table when the suite reached its
    own summary, the test it was still inside of when it was ended. Node ids
    are added while they fit, so the sentence never breaks `TEXT_LIMIT`.
    """
    kind, ids = _slow_tests(_last_tail_section(tail))
    text = f"the suite used more than the {budget:g}s harvest budget"
    if kind == "running":
        # The last node id is the test pytest had started and not finished.
        ids = ids[-1:]
    label = "slowest tests" if kind == "durations" else "the budget hit in"
    picked: list[str] = []
    for node in ids[:5]:
        candidate = f"{text}; {label}: {' '.join(picked + [node])}"
        if len(candidate) > TEXT_LIMIT:
            break
        picked.append(node)
    if picked:
        text = f"{text}; {label}: {' '.join(picked)}"
    return Reason("tests_slow", text, blocking=True)


def _uncommitted_reason(lines: list[str]) -> Reason:
    """`uncommitted_files`: the tree holds what the commit does not.

    Non-blocking, because the commit is what is scored — but it is what tells
    an agent why a green worktree scored red: the file is here, and it is not
    in the commit they handed in. The lines are quoted as `git status` prints
    them, so `??` is an untracked file and ` M` an edit left uncommitted.
    """
    extra = "" if len(lines) <= UNCOMMITTED_MAX else (
        f" (+{len(lines) - UNCOMMITTED_MAX} more)")
    if len(lines) == 1:
        text = (f"{lines[0].strip()} is not in the commit you handed in — the tests "
                "ran on the commit, not on your worktree")
    else:
        text = (f"{len(lines)} worktree changes are not in the commit you handed in — "
                f"the tests ran on the commit, not on your worktree: "
                f"{', '.join(line.strip() for line in lines[:UNCOMMITTED_MAX])}{extra}")
    return Reason("uncommitted_files", text, blocking=False)


def harvest(ws: Workspace, ticket_path: Path, *, run_tests: bool = False,
            budget_sec: float = 0.0, waited: float = 0.0, ahead: int = 0,
            test_lock=None) -> Harvest:
    """Score one worktree against its ticket and return the verdict.

    *ws* is the agent's checkout (`tools/contest/workspace.py`), *ticket_path*
    its ticket file, and *run_tests* whether to run the four pytest roots —
    kept off by default because it is the slow part of a round. With the roots
    on they run on the claim's commit, checked out into a throwaway worktree
    that is removed again in `finally`: this tree is never stashed, cleaned or
    checked out, and whatever it holds beyond the commit is reported as the
    non-blocking `uncommitted_files` reason instead of being judged.

    KC-50: the roots run only for a verdict they can still decide. A blocking
    reason already on the list settles `REWORK`, so they are skipped and
    `facts["tests_run"]` is `"skipped: <first blocking code>"` — no
    `tests_failed` for roots that never ran. *test_lock* is the round's
    serialization as a context manager, held around `run_tests_detail` alone:
    the git calls and the scorecard never wait behind another agent's roots.

    `budget_sec > 0` bounds the roots together (KC-57): past it the suite is
    `tests_slow`, not `tests_failed`, so the rework prompt says to make the
    suite faster rather than fixing a red test. `0` keeps today's behaviour.

    Raises `FileNotFoundError` for an unreadable ticket; a runner holding a bad
    ticket path is a bug, not a harvest result.
    """
    budget = max(0.0, float(budget_sec or 0))
    start = time.monotonic()
    ticket = Path(ticket_path).name
    declared = declared_files(ticket_path)

    # ── the claim: the last PROGRESS.csv row for this ticket ──────────────
    claim = None
    for row in _progress_rows(ws.progress_csv):
        if (row.get("ticket") or "").strip() == ticket:
            claim = row
    # A short or missing row field reads as empty, so the row scores no_commit, not a crash.
    claimed_commit = ((claim or {}).get("commit") or "").strip()
    outcome = ((claim or {}).get("outcome") or "").strip().upper()

    progress = f"runs/{ws.agent}/PROGRESS.csv"  # ws.progress_csv, for a short reason
    reasons: list[Reason] = []
    resolved: str | None = None  # the full sha the claim names, once it is on the branch
    claimed_base = False         # the claim names the round's base (round 83)
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
            # A claim must name a commit, not a name that means a different commit per branch.
            reasons.append(Reason(
                "commit_not_on_branch",
                f"commit {claimed_commit[:40]} is not a sha — commit once, then "
                "append_task.py --commit $(git rev-parse HEAD)",
            ))
        else:
            sha = _resolve_claim(ws.path, claimed_commit)
            claimed_base = sha is not None and _at_or_under_base(ws, sha)
            if claimed_base and _is_ancestor(ws.path, sha):
                # Round 83: the base is never the agent's commit, so a claim of it is refused.
                reasons.append(Reason(
                    "commit_not_on_branch",
                    f"commit {sha[:12]} is the round's base or under it, not your "
                    "change — commit once, then append_task.py --commit "
                    "$(git rev-parse HEAD)",
                ))
            elif sha is not None and _is_ancestor(ws.path, sha):
                resolved = sha
            else:
                # Round 119: after an amend the row is stale, so the first step is the row (round 119).
                head = git(str(ws.path), "rev-parse", "--short", "HEAD")
                head_is_change = bool(head) and not _at_or_under_base(ws, head)
                if head_is_change:
                    fix = (f"if HEAD holds your change (amended?), append_task.py "
                           f"--commit {head}; else cherry-pick it")
                else:
                    fix = "rebase it onto the branch"
                reasons.append(Reason(
                    "commit_not_on_branch",
                    f"commit {(sha or claimed_commit)[:12]} is not an ancestor of HEAD "
                    f"({head or 'unresolved'}) on {ws.branch} — {fix}",
                ))

    # ── the facts: the mechanical scorecard row ───────────────────────────
    facts = judge_worktree(ws.agent, str(ws.path), ws.base_sha, list(declared),
                           want_tests=False)
    # Whether the one commit is the runner's deadline commit, a fact for SUMMARY and the judge (KC-41).
    facts["deadline_commit"] = (
        facts.get("commits") == 1
        and git(str(ws.path), "log", "-1", "--format=%ce", "HEAD") == DEADLINE_COMMIT_EMAIL)

    if "commits" not in facts:
        # A path that is not a git worktree has no commit to score, so it fails the commit-count gate.
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
        # A base with no bridge at all (external --target repo) has nothing to keep byte-identical (KC-76).
        if facts["shrink"] in ("CHANGED", "GONE"):
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

    # The verdict's commit is the claim's sha, else the branch's one commit, else nothing (KC-30).
    commit = resolved if facts.get("commits") == 1 else None
    if commit is None and (claim is None or claimed_base):
        # A claim of the base is not a commit of the change, so the branch's one commit is named (round 83).
        commit = _branch_commit(ws.path, facts)

    # Roots run only for a verdict they can decide; a verdict already REWORK skips them.
    if run_tests:
        blocking_reasons = [r for r in reasons if r.blocking]
        if blocking_reasons:
            facts["tests_run"] = f"skipped: {blocking_reasons[0].code}"
        else:
            # The roots run on the scored commit, not on this tree; the tree is only read (KC-60).
            outside = _status_lines(ws)
            if "commits" in facts:
                # No resolvable claim means HEAD is scored, with the blocking reason already listed.
                target, parent, error = _commit_worktree(ws, resolved or "HEAD")
            else:
                target, parent, error = (None, None,
                                         f"{ws.path} is not a git worktree")
            if target is None:
                # Roots that did not run are not a pass, and the reason says why they did not.
                facts["tests_run"] = "checkout✗"
                reasons.append(Reason(
                    "tests_failed",
                    f"the tests could not run on commit "
                    f"{(resolved or 'HEAD')[:12]}: {error}",
                ))
            else:
                try:
                    with (test_lock or contextlib.nullcontext()):
                        summary, tail = run_tests_detail(
                            target, budget_sec=budget)
                finally:
                    _drop_worktree(ws, parent)
                facts["tests_run"] = summary
                if "budget✗" in summary:
                    reasons.append(_tests_slow_reason(budget, tail))
                elif "✗" in summary:
                    reasons.append(Reason(
                        "tests_failed",
                        f"the tests do not pass: {summary}\n"
                        + "\n".join(tail),
                    ))
            if outside:
                reasons.append(_uncommitted_reason(outside))

    verdict = "REWORK" if any(r.blocking for r in reasons) else "READY"
    return Harvest(
        verdict=verdict,
        reasons=tuple(reasons),
        commit=commit,
        facts=facts,
        elapsed=time.monotonic() - start,
        waited=max(0.0, float(waited or 0)),
        ahead=max(0, int(ahead or 0)),
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
    if str((h.facts or {}).get("tests_run") or "").startswith("skipped:"):
        lines.append("")
        lines.append(
            "The test roots were not run: fix the items above first.")
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
