"""tools/arena/tickets.py — AR-6: `arena issue list` / `arena issue view`, the computed ticket state.

The `**Status:**` field of a ticket md alone lies: a round can be running or
finished while the md still says `open`, and a `NN:` commit can sit on the
integration branch while the md was never set to `landed`. `scan` reads the
three places a ticket lives (the drafts folder, the checkout's `epic-tasks/`,
`epic-tasks/` on the integration branch), the round folders and the branch's
commit subjects, and computes one state per ticket plus the mismatches.

Everything here is a read: `git` reads, `/proc` reads, file reads. No file is
written, no ref is created, no process is started besides `git`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from tools.contest import cli as contest_cli
from tools.contest import draft as contest_draft

from . import output, profile, rounds
from .gitref import GitRefError, git

#: Every state `scan` computes, in the order the first match wins.
STATES = ("landed", "closed", "running", "done", "queued", "draft", "open")

#: A commit subject that names a ticket: `144: …`, `144 — …`, `144 …`.
_SUBJECT_RE = re.compile(r"^0*(\d+)(?::|\s)")

#: Where a ticket was read from; the checkout's file wins over the branch's,
#: and either `epic-tasks/` file wins over a draft.
WHERE_DRAFT, WHERE_CHECKOUT, WHERE_BRANCH = "draft", "checkout", "branch"
_PRECEDENCE = (WHERE_CHECKOUT, WHERE_BRANCH, WHERE_DRAFT)

LIST_COLUMNS = ["NN", "STATE", "TITLE", "!"]


@dataclass
class Ticket:
    """One ticket, as `scan` computed it."""

    number: int
    title: str
    path: str  # repo-relative, or `<branch>:epic-tasks/…`
    where: str  # draft | checkout | branch
    status_field: str  # the md's `**Status:**` word, lower-cased
    state: str  # one of STATES
    flags: list[str] = field(default_factory=list)


def _by_number(names: list[str]) -> dict[int, list[str]]:
    """File names → `{ticket number: sorted names}`; `*.rejected.md` is skipped."""
    out: dict[int, list[str]] = {}
    for name in names:
        match = rounds._TICKET_RE.match(name)
        if match and not name.endswith(".rejected.md"):
            out.setdefault(int(match.group(1)), []).append(name)
    return {n: sorted(v) for n, v in out.items()}


def _folder_names(folder: Path) -> list[str]:
    return [p.name for p in folder.glob("*.md")] if folder.is_dir() else []


def _branch_names(repo: Path, branch: str) -> list[str]:
    listed = git(repo, "ls-tree", "--name-only", branch, rounds.TASKS_DIR + "/")
    return [line.rsplit("/", 1)[-1] for line in listed.splitlines()]


def _commit_numbers(repo: Path, branch: str) -> set[int]:
    """The ticket numbers *branch*'s commit subjects name — one `git log` per scan."""
    # The trailing `--` makes *branch* a revision even when a file of the same
    # name sits in the worktree: this repo's own `./arena` script and branch
    # `arena` made `git log arena` ambiguous and `issue list` refused.
    subjects = git(repo, "log", "--format=%s", branch, "--")
    found = set()
    for line in subjects.splitlines():
        match = _SUBJECT_RE.match(line)
        if match:
            found.add(int(match.group(1)))
    return found


def _text(repo: Path, branch: str, where: str, name: str) -> str:
    """The md text of *name* as it is in *where*."""
    repo = Path(repo)
    if where == WHERE_BRANCH:
        return git(repo, "show", f"{branch}:{rounds.TASKS_DIR}/{name}", strip=False)
    folder = repo / (".arena/drafts" if where == WHERE_DRAFT else rounds.TASKS_DIR)
    return (folder / name).read_text(encoding="utf-8")


def _path(where: str, branch: str, name: str) -> str:
    if where == WHERE_BRANCH:
        return f"{branch}:{rounds.TASKS_DIR}/{name}"
    return f"{'.arena/drafts' if where == WHERE_DRAFT else rounds.TASKS_DIR}/{name}"


def scan(repo: Path, config, branch: str, proc_root: str = rounds.PROC_ROOT) -> list[Ticket]:
    """Every ticket in numeric order, one entry per number.

    Raises `GitRefError` when *branch* cannot be read, `OSError` for a file.
    """
    repo = Path(repo)
    places = {
        WHERE_DRAFT: _by_number(_folder_names(repo / ".arena" / "drafts")),
        WHERE_CHECKOUT: _by_number(_folder_names(repo / rounds.TASKS_DIR)),
        WHERE_BRANCH: _by_number(_branch_names(repo, branch)),
    }
    committed = _commit_numbers(repo, branch)
    numbers = sorted({n for found in places.values() for n in found})
    tickets = []
    for nn in numbers:
        where = next(w for w in _PRECEDENCE if nn in places[w])
        names = places[where][nn]
        name = names[0]
        text = _text(repo, branch, where, name)
        status = contest_cli._status_of(text)
        flags = []
        # two files with one number in one place: the first by name is the one read
        for place in _PRECEDENCE:
            flag = f"two files for {nn}: {', '.join(places[place].get(nn, []))}"
            # the checkout and the branch usually hold the same two: say it once
            if len(places[place].get(nn, [])) > 1 and flag not in flags:
                flags.append(flag)
        if nn in places[WHERE_DRAFT] and where != WHERE_DRAFT:
            flags.append(f"draft and {rounds.TASKS_DIR} both hold {nn}")
        if nn in committed and status not in ("landed", "closed"):
            flags.append(f"commit {nn}: on {branch} but status {status or 'none'}")
        if status == "landed" and nn not in committed:
            flags.append(f"status landed but no {nn}: commit on {branch}")
        tickets.append(Ticket(
            number=nn,
            title=contest_draft.title_of(text),
            path=_path(where, branch, name),
            where=where,
            status_field=status,
            state=_state(repo, config, nn, where, status, proc_root),
            flags=flags,
        ))
    return tickets


def _state(repo: Path, config, nn: int, where: str, status: str, proc_root: str) -> str:
    """The first of `STATES` that matches (see the module docstring)."""
    if status == "landed":
        return "landed"
    if status == "closed":
        return "closed"
    if rounds._has_state(repo, config, nn):
        return "running" if rounds.round_alive(repo, nn, proc_root) else "done"
    if status == "queued":
        return "queued"
    if where == WHERE_DRAFT:
        return "draft"
    return "open"


def _branch_and_scan(repo: Path, args: argparse.Namespace, prof: dict[str, str]):
    """`(branch, tickets)`; `RoundError`, `GitRefError`, `OSError` for the caller."""
    config = rounds.load_config(repo)
    branch = rounds.integration_branch(repo, args.branch, prof)
    return branch, scan(repo, config, branch, rounds.PROC_ROOT)


def _row(t: Ticket) -> dict:
    return {"NN": t.number, "STATE": t.state, "TITLE": t.title, "!": "!" if t.flags else ""}


def issue_list(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena issue list [--state S] [--branch B]`: exit 3 with one line for no tickets."""
    if args.state is not None and args.state not in STATES:
        return output.refuse(f"issue list: unknown state {args.state!r} "
                             f"(known: {', '.join(STATES)})")
    try:
        _, tickets = _branch_and_scan(repo, args, prof)
    except (rounds.RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return output.refuse(str(err))
    if args.state is not None:
        tickets = [t for t in tickets if t.state == args.state]
    if not tickets:
        what = f"state {args.state}" if args.state is not None else "any state"
        print(f"arena: no tickets in {what}", file=sys.stderr)
        return rounds.EXIT_NOTHING
    if args.output == "json":
        print(json.dumps([_scrubbed(asdict(t)) for t in tickets]))
        return rounds.EXIT_OK
    output.emit([_row(t) for t in tickets], LIST_COLUMNS, "table")
    return rounds.EXIT_OK


def _scrubbed(value):
    """The JSON row through the same `mask` and `scrub` `output.emit` applies."""
    return output._scrub_all(output.mask(dict(value)))


def issue_view(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena issue view NN [--branch B]`: the md text, then one `! flag` line each."""
    if not re.fullmatch(r"\d+", str(args.number)):
        return output.refuse(f"issue view: {args.number!r} is not a ticket number")
    nn = int(args.number)
    try:
        branch, tickets = _branch_and_scan(repo, args, prof)
        found = next((t for t in tickets if t.number == nn), None)
        if found is None:
            print(f"arena: no ticket {nn} in .arena/drafts/, {rounds.TASKS_DIR}/ "
                  f"or on {branch}", file=sys.stderr)
            return rounds.EXIT_FAILED
        text = _text(repo, branch, found.where, Path(found.path).name.split(":")[-1])
    except (rounds.RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return output.refuse(str(err))
    if args.output == "json":
        print(json.dumps(_scrubbed({**asdict(found), "text": text})))
        return rounds.EXIT_OK
    sys.stdout.write(output.scrub(text))
    if text and not text.endswith("\n"):
        sys.stdout.write("\n")
    for flag in found.flags:
        print(output.scrub(f"! {flag}"))
    return rounds.EXIT_OK
