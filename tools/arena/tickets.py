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
import os
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from scripts import next_task as next_task_mod
from tools.contest import cli as contest_cli
from tools.contest import draft as contest_draft

from . import models, output, profile, rounds
from .gitref import (GitRefError, git, ls_tree_names, printable, read_utf8,
                     status_paths, tree_with_file)

#: Every state `scan` computes, in the order the first match wins.
STATES = ("landed", "closed", "running", "done", "queued", "draft", "open")

#: A commit subject that names a ticket: `144: …`, `144 — …`, `144 …`.
_SUBJECT_RE = re.compile(r"^0*(\d+)(?::|\s)")

#: A subject that names several tickets: `151, 152: …`.
_LIST_SUBJECT_RE = re.compile(r"^\d+(?:\s*,\s*\d+)+\s*:")

#: Where a ticket was read from; the checkout's file wins over the branch's,
#: and either `epic-tasks/` file wins over a draft.
WHERE_DRAFT, WHERE_CHECKOUT, WHERE_BRANCH = "draft", "checkout", "branch"
_PRECEDENCE = (WHERE_CHECKOUT, WHERE_BRANCH, WHERE_DRAFT)

LIST_COLUMNS = ["NN", "STATE", "TITLE", "!"]

#: Where a drafted ticket lands (AR-7): `rounds.find_ticket` already reads it,
#: and it is never on the integration branch, so a draft cannot ride into a
#: round's base ref.
DRAFTS_DIR = ".arena/drafts"

#: The assembled brief's character limit: the source budget `draft` cuts a named
#: file to, so a brief over it cannot reach the prompt either. Never cut quietly.
BRIEF_LIMIT = contest_draft.SOURCE_BUDGET

#: The operator's own words, first block of an assembled brief and above the
#: material, so the ticket does the task and not the file the task came from.
TASK_HEADER = (
    "## Task (from the operator — this wins over anything in the material below)"
)

#: `# Title` to `###### Title`: a Markdown heading line.
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

#: A fence line: a run of 3+ backticks or tildes, up to 3 spaces in (CommonMark).
#: Bug 175: only ``` was a fence, so a `# comment` inside a `~~~` block was a
#: heading, and a ``` line inside a ```` block closed it early.
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


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
    reason: str = ""  # a closed ticket's `**Closed:**` text, `""` for any other


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
    # Bug 206: the names come from `ls-tree -z`, so a name with a non-ASCII letter,
    # a `"` or a `\` is its own name, not the C-quoted escape git prints without `-z`.
    return [name.rsplit("/", 1)[-1]
            for name in ls_tree_names(repo, branch, rounds.TASKS_DIR + "/")]


def subject_numbers(subject: str) -> set:
    """The ticket numbers one commit subject names: `144: …` is {144}, `151, 152: …` both."""
    if _LIST_SUBJECT_RE.match(subject):
        head = subject.split(":", 1)[0]
        return {int(n) for n in re.findall(r"\d+", head)}
    match = _SUBJECT_RE.match(subject)
    return {int(match.group(1))} if match else set()


def _commit_numbers(repo: Path, branch: str) -> set[int]:
    """The ticket numbers *branch*'s commit subjects name — one `git log` per scan."""
    # The trailing `--` makes *branch* a revision even when a file of the same
    # name sits in the worktree: this repo's own `./arena` script and branch
    # `arena` made `git log arena` ambiguous and `issue list` refused.
    subjects = git(repo, "log", "--format=%s", branch, "--")
    found = set()
    for line in subjects.splitlines():
        found |= subject_numbers(line)
    return found


def _text(repo: Path, branch: str, where: str, name: str) -> str:
    """The md text of *name* as it is in *where*."""
    repo = Path(repo)
    if where == WHERE_BRANCH:
        return printable(git(repo, "show", f"{branch}:{rounds.TASKS_DIR}/{name}",
                             strip=False))
    folder = repo / (".arena/drafts" if where == WHERE_DRAFT else rounds.TASKS_DIR)
    return printable(read_utf8(folder / name))


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
        # AR-14: `issue list` shows a closed ticket with its reason, the
        # `**Closed:**` line `issue close` wrote under the status. It is not a
        # flag: a closed ticket is not a mismatch, and `!` is for those.
        reason = closed_reason(text) if status == "closed" else ""
        tickets.append(Ticket(
            number=nn,
            title=contest_draft.title_of(text),
            path=_path(where, branch, name),
            where=where,
            status_field=status,
            state=_state(repo, config, nn, where, status, proc_root),
            flags=flags,
            reason=reason,
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


# ── AR-7: the next free number ───────────────────────────────────────────────

def _shown(repo: Path, path) -> str:
    """*path* repo-relative when it is inside *repo*, absolute otherwise."""
    path = Path(path)
    try:
        relative = path.resolve().relative_to(Path(repo).resolve())
    except (ValueError, OSError):
        return str(path)
    return relative.as_posix() or str(path)


def _roster_out_dir(repo: Path, config) -> Path:
    """The roster's `out_dir` under *repo*; a missing or empty value is the default."""
    out = getattr(config, "out_dir", None)
    if not isinstance(out, str) or not out.strip():
        out = "contest-out"
    return Path(repo) / out.strip()


def _round_entries(repo: Path, config) -> list:
    """`(number, folder name)` for every `NN` or `NN.K` under the roster's `out_dir`.

    Zero-padded or not, both are read as integers, and any other name is ignored —
    `context-memory.json` and a stray log are not rounds. An absent or unreadable
    `out_dir` is `[]`, not a refusal: a roster that names no such folder holds no
    rounds, and `next_number` stops seeing them rather than failing the brief.
    """
    folder = _roster_out_dir(repo, config)
    if not folder.is_dir():
        return []
    try:
        entries = list(folder.iterdir())
    except OSError:
        return []
    found = []
    for entry in entries:
        match = rounds._ROUND_DIR.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name))
    return found


def _ref_numbers(repo: Path) -> set:
    """The round numbers under `refs/heads/arena-round/`, zero padding allowed."""
    found = set()
    listed = git(repo, "for-each-ref", "--format=%(refname)",
                 "refs/heads/" + rounds.REF_PREFIX)
    for line in listed.splitlines():
        name = line.rpartition(rounds.REF_PREFIX)[2]
        # Bug 185: ASCII digits only, and a length `int()` takes — `'²'.isdigit()` is
        # True, `int('²')` raises, and `arena run` never writes such a ref.
        if re.fullmatch(r"[0-9]{1,18}", name):
            found.add(int(name))
    return found


def _rejected_numbers(repo: Path) -> set:
    """The numbers a `.rejected.md` still holds, in either ticket folder.

    A refused draft keeps its number, so a different brief cannot be written onto
    a number whose rejected text is still lying there. `--number NN` is the way
    past it: an explicit number may retry a rejected one.
    """
    found = set()
    for folder in (Path(repo) / DRAFTS_DIR, Path(repo) / rounds.TASKS_DIR):
        for name in _folder_names(folder):
            match = rounds._TICKET_RE.match(name)
            if match and name.endswith(contest_draft.REJECTED_SUFFIX):
                found.add(int(match.group(1)))
    return found


def next_number(repo: Path, config, branch: str) -> int:
    """Max + 1 over every place a ticket number lives; 1 when nothing does.

    Not the first gap: a gap is a number the operator picked away, and handing it
    back to a different brief is the stale-round bug `contest draft` handed out —
    `draft.next_round` is never called here. The sources are the tickets `scan`
    sees (drafts, the checkout's and the branch's `epic-tasks/`), the
    `.rejected.md` still lying around, the round folders under the roster's
    `out_dir` and the `arena-round/NN` refs.

    `GitRefError` when *branch* cannot be read, `OSError` for a file; the caller
    refuses.
    """
    repo = Path(repo)
    held = {ticket.number for ticket in scan(repo, config, branch, rounds.PROC_ROOT)}
    held |= _rejected_numbers(repo)
    held |= {number for number, _name in _round_entries(repo, config)}
    held |= _ref_numbers(repo)
    return max(held) + 1 if held else 1


def _held_by(repo: Path, config, branch: str, number: int) -> Optional[str]:
    """Why *number* is not free, or `None` — a ticket, then a round, then a ref.

    A `.rejected.md` does not block: that is how a refused number is retried.
    """
    repo = Path(repo)
    for ticket in scan(repo, config, branch, rounds.PROC_ROOT):
        if ticket.number == number:
            return f"ticket {number} exists ({ticket.path})"
    folder = _roster_out_dir(repo, config)
    for held_number, name in _round_entries(repo, config):
        if held_number == number:
            return f"round {number} exists ({_shown(repo, folder / name)})"
    if number in _ref_numbers(repo):
        return f"round {number} exists ({rounds.REF_PREFIX}{number})"
    return None


# ── AR-7: the brief ──────────────────────────────────────────────────────────


def _headings(lines: list) -> list:
    """`(line index, level)` for every heading outside a fenced code block.

    A fence (```, ~~~ or a longer run) turns the heading count off until the
    same character, at least as long, closes it — so an example in code is not
    a heading: a `#` in a fixture would otherwise start a section and cut the
    real one short.
    """
    heads = []
    fence = None  # the opening run while inside a block, e.g. "````" or "~~~"
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match and not (match.group(1)[0] == "`" and "`" in match.group(2)):
                fence = match.group(1)
                continue
        else:
            # only the same character, at least as long, with nothing after, closes
            if (match and match.group(1)[0] == fence[0]
                    and len(match.group(1)) >= len(fence) and not match.group(2).strip()):
                fence = None
            continue
        match = _HEADING_RE.match(line)
        if match:
            heads.append((index, len(match.group(1))))
    return heads


def _is_item(text: str, item: str) -> bool:
    """Whether a heading's text names *item*: `AR-7` in `AR-7 — …`, never `AR-70`.

    The name must be followed by a non-word character or the end of the line, so
    `AR-7` does not match `AR-70` or `AR-7b`; the match is case-sensitive.
    """
    pattern = re.compile(r"^" + re.escape(item) + r"(?:\W|$)")
    return bool(pattern.match(text))


def _cut_item(lines: list, item: str, label: str) -> str:
    """The `### ID …` section of *lines*; `ValueError` when there is not one.

    The section runs from its heading to the next heading of the same or a higher
    level — fewer or equal `#`, so a subsection stays inside — or to the end of
    the file, and the heading line itself is part of it.
    """
    heads = _headings(lines)
    hits = [(index, level) for index, level in heads
            if _is_item(_HEADING_RE.match(lines[index]).group(2), item)]
    if not hits:
        raise ValueError(f"no section {item} in {label}")
    if len(hits) > 1:
        where = ", ".join(str(index + 1) for index, _level in hits)
        raise ValueError(f"{item} is in {label} twice: lines {where}")
    start, level = hits[0]
    end = len(lines)
    for index, other in heads:
        if index > start and other <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _read_brief_file(repo: Path, raw) -> tuple:
    """One `--file` as `(path, text, label)`: a relative path against *repo*.

    The repo root, never the caller's cwd — `arena` runs from anywhere. The file
    is only read; a path outside the repo is allowed and printed as it is.
    """
    path = Path(raw)
    path = path if path.is_absolute() else Path(repo) / path
    label = _shown(repo, path)
    if not path.exists():
        raise ValueError(f"--file {label}: no such file")
    if path.is_dir():
        raise ValueError(f"--file {label}: it is a directory")
    try:
        body = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"--file {label}: not UTF-8") from None
    except OSError as err:
        raise ValueError(f"--file {label}: {err}") from err
    return path, body, label


def build_brief(repo, text, files, item) -> str:
    """The brief the drafter gets: the operator's text, then each file or section.

    Each block is present only when it has content, and blocks are separated by
    one blank line: `## Task (…)` for the words, `## Material: <path>` for a whole
    file, `## Material: <path> — section <ID>` for one section of it. Without the
    text the brief is the material blocks only.

    Every refusal is a `ValueError` whose message is the one line the caller
    prints, before the collect and before any model call: an absent brief,
    `--item` without one `--file` or with more than one, a `--file` that is
    missing, a directory or not UTF-8, a file or a cut section that is empty, an
    `--item` that is not there or is twice, and a brief over `BRIEF_LIMIT`.
    Nothing here is written and nothing is cut: a cut epic is a ticket written
    against half a spec.
    """
    repo = Path(repo)
    files = list(files or [])
    blocks = []
    if text and text.strip():
        blocks.append(f"{TASK_HEADER}\n\n{text.strip()}")
    if item is not None:
        if not files:
            raise ValueError("--item needs one --file")
        if len(files) > 1:
            raise ValueError("--item cuts one --file, not several")
    if not blocks and not files:
        raise ValueError("no brief — pass text, --file PATH or --file PATH --item ID")
    for raw in files:
        _path, body, label = _read_brief_file(repo, raw)
        material = body
        header = f"## Material: {label}"
        if item is not None:
            material = _cut_item(body.split("\n"), item, label)
            header += f" — section {item}"
        if not material.strip():
            raise ValueError(f"--file {label} is empty")
        blocks.append(f"{header}\n\n{material.strip()}")
    brief = "\n\n".join(blocks)
    if len(brief) > BRIEF_LIMIT:
        raise ValueError(f"brief is {len(brief)} characters, limit {BRIEF_LIMIT} — "
                         "cut it with --item ID or a shorter file")
    return brief


# ── AR-7: the draft ──────────────────────────────────────────────────────────

def _chosen_number(repo: Path, config, branch: str, raw) -> int:
    """`--number NN` as a free number, else `next_number`; `ValueError` when not."""
    if raw is None:
        return next_number(repo, config, branch)
    text = str(raw).strip()
    if not re.fullmatch(r"\d+", text) or int(text) <= 0:
        raise ValueError(f"--number {raw!r} is not a positive integer")
    number = int(text)
    taken = _held_by(repo, config, branch, number)
    if taken is not None:
        raise ValueError(taken)
    return number


def issue_create(repo: Path, args: argparse.Namespace, prof: dict) -> int:
    """`arena issue create`: one ticket drafted into `.arena/drafts/`, no branch.

    Refusals first, each one line and each before the collect and before any model
    call: the roster, the branch, the brief, the number, the writer and reviewer.
    Then `draft.draft_ticket` with `out_dir` on the drafts folder and `commit=False`
    — the draft is where `rounds.find_ticket` already looks, so it is one
    `arena run start NN` away from a round, and `epic-tasks/`, the branch and HEAD
    are untouched. A lint or a review refusal is exit 2 with the problems and the
    `.rejected.md`; the JSON keeps the same fields.
    """
    repo = Path(repo)
    try:
        config = rounds.load_config(repo)
    except (rounds.RoundError, OSError) as err:
        return output.refuse(str(err))
    try:
        branch = rounds.integration_branch(repo, args.branch, prof)
    except (rounds.RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return output.refuse(str(err))
    try:
        brief = build_brief(repo, getattr(args, "text", None), args.file, args.item)
    except ValueError as err:
        return output.refuse(str(err))
    try:
        number = _chosen_number(repo, config, branch, args.number)
    except (rounds.RoundError, GitRefError, OSError, ValueError) as err:
        return output.refuse(str(err))
    try:
        # AR-63: the role pair, resolved one role at a time — the `--writer` /
        # `--reviewer` flag, then the profile's role, then AR-7's old keys.
        llm_call, review_call, pair_info = models.draft_pair(repo, args, prof, config)
    except (models.ModelError, contest_cli.DraftSetupError, profile.ProfileError) as err:
        return output.refuse(str(err))
    try:
        result = contest_draft.draft_ticket(
            brief,
            repo=repo,
            config=config,
            round_no=number,
            out_dir=repo / DRAFTS_DIR,
            llm_call=llm_call,
            review_call=review_call,
            commit=False,
        )
    except ValueError as err:
        return output.refuse(str(err))

    reviewed = pair_info["reviewed"]
    if not reviewed:
        print("review skipped (--no-review)", file=sys.stderr)
    if args.output == "json":
        print(json.dumps(_scrubbed({
            "number": number,
            "path": _shown(repo, result.path) if result.path is not None else None,
            "rejected": bool(result.rejected),
            "problems": list(result.problems),
            "rejected_path": (_shown(repo, result.rejected_path)
                             if result.rejected_path is not None else None),
            "reviewed": reviewed,
            "writer": pair_info["writer"],
            "reviewer": pair_info["reviewer"],
            "same_model": pair_info["same_model"],
        })))
    elif result.rejected:
        for problem in result.problems:
            print(f"- {output.scrub(str(problem))}", file=sys.stderr)
        if result.rejected_path is not None:
            print(f"rejected draft: {_shown(repo, result.rejected_path)}", file=sys.stderr)
    else:
        print(f"ticket {number} drafted: {_shown(repo, result.path)}")
        print(f"next: arena run start {number}")
    return rounds.EXIT_USAGE if result.rejected else rounds.EXIT_OK


def _branch_and_scan(repo: Path, args: argparse.Namespace, prof: dict[str, str]):
    """`(branch, tickets)`; `RoundError`, `GitRefError`, `OSError` for the caller."""
    config = rounds.load_config(repo)
    branch = rounds.integration_branch(repo, args.branch, prof)
    return branch, scan(repo, config, branch, rounds.PROC_ROOT)


def _row(t: Ticket) -> dict:
    # a closed ticket reads `closed (why)`; `-o json` keeps `state` a bare word and
    # carries the reason in its own key
    state = f"{t.state} ({t.reason})" if t.state == "closed" and t.reason else t.state
    return {"NN": t.number, "STATE": state, "TITLE": t.title, "!": "!" if t.flags else ""}


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
    # Bug 187: at most 18 digits — `int()` of a 4300+ digit string raises
    if not re.fullmatch(r"\d{1,18}", str(args.number)):
        return output.refuse(f"issue view: {args.number!r:.60} is not a ticket number")
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


# ── AR-14: the status verbs ───────────────────────────────────────────────────

#: The four words a `**Status:**` line may hold. `landed` is `issue land`'s
#: (AR-8): nothing in this module sets it, and it refuses every verb here.
STATUS_WORDS = ("open", "queued", "closed", "landed")

#: `verb → (the words it may come from, the word it writes)`.
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "queue": (("open",), "queued"),
    "open": (("queued",), "open"),
    "close": (("open", "queued"), "closed"),
    "reopen": (("closed",), "open"),
}

#: The verbs that are a no-op on the word they write themselves: `queue` on
#: `queued`, `open` on `open`, `close` on `closed`. `reopen` is not one of them —
#: its target is `open`, and `reopen` of an open ticket is refused, not repeated.
IDEMPOTENT = ("queue", "open", "close")

#: The verb that writes *word* — a hint must name a verb, not a status word.
VERB_FOR: dict[str, str] = {"open": "open", "queued": "queue", "closed": "close"}

#: The verbs' steps, in order — one mark per step in the `flow:` line.
STATUS_FLOW = ("branch resolved", "where B is checked out", "ticket found on B",
               "status readable", "transition allowed", "round not running",
               "write", "commit")
#: `edit`: the same first six, then the editor's own three before the commit.
EDIT_FLOW = STATUS_FLOW[:6] + ("editor", "status unchanged", "lint", "commit")
#: `run start`'s steps, for §5's blocking report; the `NN` is the round's number.
RUN_START_FLOW = ("branch found", "ticket found", "intake", "build arena-round/NN",
                  "start sessions")

#: The `**Status:**` line, the whole line — its word and the note after it.
_STATUS_LINE_RE = re.compile(r"^[ \t]*\*\*Status:\*\*[ \t]*[^\n]*$", re.MULTILINE)
#: The `**Closed:**` line `close` adds under it and `reopen` removes. Bug 200-13:
#: no `\r?\n?` here — an ending the pattern claims is one the removal code stops
#: taking a second of, which is what ate the blank line after the line.
_CLOSED_LINE_RE = re.compile(r"^[ \t]*\*\*Closed:\*\*[ \t]*[^\n]*$", re.MULTILINE)

#: `git worktree list --porcelain` says *branch* is checked out in this checkout.
_HERE = object()


def status_word(text: str) -> str:
    """The first word of *text*'s `**Status:**` line, lower-cased; `""` when absent.

    The first word only: `queued (judged on arena)` reads as `queued`, the note
    never changes the state — `next_task.py`'s `_status` reads it the same way.
    """
    return next_task_mod._status(text or "")


def status_note(text: str) -> str:
    """What follows the word on the `**Status:**` line, or `""`.

    `(judged on arena)` → `judged on arena`; a note without parentheses is kept
    as it is, so an existing line is never re-wrapped by accident.
    """
    match = _STATUS_LINE_RE.search(text or "")
    if match is None:
        return ""
    rest = match.group(0).split("**Status:**", 1)[1].strip()
    words = rest.split()
    if not words:
        return ""
    rest = rest[len(words[0]):].strip()
    if rest.startswith("(") and rest.endswith(")"):
        return rest[1:-1].strip()
    return rest


def closed_reason(text: str) -> str:
    """*text*'s `**Closed:**` line's text, or `""` when there is none."""
    match = _CLOSED_LINE_RE.search(text or "")
    if match is None:
        return ""
    return match.group(0).split("**Closed:**", 1)[1].strip()


def _lines(text: str) -> list[tuple[str, str]]:
    """*text* as `(line, ending)` pairs, the endings `\\r\\n`, `\\n` or `""`.

    Split on `\\n` only, a lone `\\r` is not a break here: a ticket blob may hold
    one (bug 185), and `splitlines` would call it a line and re-join it changed.
    """
    lines: list[tuple[str, str]] = []
    start = 0
    while (pos := text.find("\n", start)) != -1:
        line = text[start:pos]
        if line.endswith("\r"):
            lines.append((line[:-1], "\r\n"))
        else:
            lines.append((line, "\n"))
        start = pos + 1
    lines.append((text[start:], ""))
    return lines


def set_status_text(text: str, word: str, note: str = "", reason: str = "") -> str:
    """*text* with its `**Status:**` line set to *word*/*note*, its `**Closed:**`
    line written for `closed` and removed otherwise; everything else byte for byte.

    `ValueError` when there is no `**Status:**` line: the position of the line is
    never invented here, and a status that is added is added with `issue edit`.

    One pass over the lines, bug 200: the status line and the `**Closed:**` line
    are found in the same text, so a `**Closed:**` sitting *above* the status
    line no longer shifts the status line's offsets into the middle of a later
    line. The new lines take the status line's own ending, so a `\\r\\n` ticket
    stays `\\r\\n`; the old `**Closed:**` line goes with its own ending only, so
    `close` then `reopen` is the identity.
    """
    lines = _lines(text)
    status_i = next((i for i, (body, _) in enumerate(lines) if _STATUS_LINE_RE.match(body)),
                    None)
    if status_i is None:
        raise ValueError("no **Status:** line")
    closed_i = next((i for i, (body, _) in enumerate(lines)
                     if i != status_i and _CLOSED_LINE_RE.match(body)), None)

    note = (note or "").strip()
    status = f"**Status:** {word}"
    if note:
        status += f" {note if note.startswith('(') else '(' + note + ')'}"
    reason = (reason or "").strip()
    block = [status]
    if word == "closed" and reason:
        block.append(f"**Closed:** {reason}")
    # the status line's ending, or `\\n` when it was the file's last line and has
    # none — a `**Closed:**` under it still needs a break of its own
    ending = lines[status_i][1] or "\n"

    out: list[str] = []
    for i, (body, end) in enumerate(lines):
        if i == closed_i:
            continue  # the old line goes with its own ending and nothing else
        if i == status_i:
            out.extend(body_ + (ending if j < len(block) - 1 else end)
                       for j, body_ in enumerate(block))
        else:
            out.append(body + end)
    return "".join(out)


# ── the git facts a verb needs ────────────────────────────────────────────────

def _rev(repo: Path, spec: str) -> Optional[str]:
    """*spec*'s sha, or `None` — a missing ref is not an error here."""
    try:
        return git(repo, "rev-parse", "--verify", "-q", spec)
    except GitRefError:
        return None


def _worktree_of(repo: Path, branch: str):
    """`_HERE`, the other checkout's path, or `None`: where *branch* is out.

    `git worktree list --porcelain`, one block per checkout: a branch is checked
    out in at most one of them, and a checkout whose working tree would go stale
    is a refusal, not a plumbing commit under its feet.
    """
    here = os.path.realpath(str(Path(repo)))
    for block in git(repo, "worktree", "list", "--porcelain").split("\n\n"):
        path, branch_ref = None, None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = os.path.realpath(line[len("worktree "):].strip())
            elif line.startswith("branch "):
                branch_ref = line[len("branch "):].strip()
        if path and branch_ref == f"refs/heads/{branch}":
            return _HERE if path == here else path
    return None


def _worktree_or_none(repo: Path, branch: str):
    """`_worktree_of` for a `where:` line: a failed read is not one to crash over."""
    try:
        return _worktree_of(repo, branch)
    except GitRefError:
        return None


def _head_branch(repo: Path) -> str:
    """The checked-out branch, `""` on a detached HEAD."""
    try:
        return git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    except GitRefError:
        return ""


def _modified(repo: Path) -> list[str]:
    """The checkout's untracked or modified files, by path.

    `.arena/` is excluded: arena's own state and a refused edit's kept temp file
    are not the operator's uncommitted work. Bug 209 (and 44): read through
    `gitref.status_paths` — the stripped `status --porcelain` line cut a letter,
    so a name that no longer started with `.arena/` counted as the operator's
    work, and a clean checkout read `(1 modified)`.
    """
    return [path for path in status_paths(repo) if not path.startswith(".arena/")]


def _uncommitted(repo: Path, rel: str) -> list[str]:
    """*rel*'s untracked or modified entries — a dirty ticket file is a refusal."""
    return status_paths(repo, rel)


def _local_branches(repo: Path) -> list[str]:
    """Every local branch, most recently committed first — a hint must name a real one."""
    listed = git(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)",
                 "refs/heads/")
    return [line for line in listed.splitlines() if line.strip()]


def _round_state(repo: Path, nn: int) -> str:
    """`running` / `done` / `not started` / `?` for round *nn*."""
    if rounds.round_alive(repo, nn, rounds.PROC_ROOT):
        return "running"
    try:
        config = rounds.load_config(repo)
        return "done" if rounds._has_state(repo, config, nn) else "not started"
    except (rounds.RoundError, OSError):
        return "?"


def _on_branch(repo: Path, branch: str, nn: int) -> list[str]:
    """The names of ticket *nn* on *branch*'s `epic-tasks/`, `[]` when there is none."""
    try:
        names = ls_tree_names(repo, f"refs/heads/{branch}", rounds.TASKS_DIR + "/")
    except GitRefError:
        return []
    return rounds._numbered([name.rsplit("/", 1)[-1] for name in names], nn)


def _in_folder(repo: Path, folder: str, nn: int) -> list[str]:
    """The names of ticket *nn* in *folder* as checked out, `[]` when it is not there."""
    path = Path(repo) / folder
    names = [p.name for p in path.glob("*.md")] if path.is_dir() else []
    return rounds._numbered(names, nn)


def _body_on_branch(repo: Path, branch: str, name: str) -> str:
    """The ticket's text from *branch*'s tree — the status is written on the branch."""
    return printable(git(repo, "show", f"refs/heads/{branch}:{rounds.TASKS_DIR}/{name}",
                         strip=False))


# ── the flow block's text ─────────────────────────────────────────────────────

def _flow(labels: tuple, at: int, detail: str = "") -> list:
    """*labels* with *at* the failed step: `ok` before it, `todo` after it."""
    return [(label, "ok", "") if i < at
            else (label, "fail", detail) if i == at
            else (label, "todo", "") for i, label in enumerate(labels)]


def _checkout_text(repo: Path) -> str:
    """`checkout /path on arena (clean)` — the place the operator is standing in."""
    head = _head_branch(repo)
    modified = _modified(repo)
    clean = f" (clean)" if not modified else f" ({len(modified)} modified)"
    return f"checkout {os.path.realpath(str(Path(repo)))} on {head or 'detached HEAD'}{clean}"


def _branch_text(repo: Path, branch: str) -> str:
    """`target branch B @ e508db4, checked out here` — or where it is instead."""
    sha = _rev(repo, f"refs/heads/{branch}")
    if sha is None:
        where = "on origin only" if _rev(repo, f"refs/remotes/origin/{branch}") else "missing"
        return f"target branch {branch} @ ?, {where}"
    out = _worktree_or_none(repo, branch)
    if out is _HERE:
        where = "checked out here"
    elif out is None:
        where = "not checked out"
    else:
        where = f"checked out in {out}"
    return f"target branch {branch} @ {sha[:7]}, {where}"


def _where_text(repo: Path, branch: Optional[str], nn: Optional[int]) -> str:
    """The `where:` line: this checkout, the target branch, the round's state."""
    parts = [_checkout_text(repo)]
    if branch:
        parts.append(_branch_text(repo, branch))
    if nn is not None:
        parts.append(f"round {nn}: {_round_state(repo, nn)}")
    return " · ".join(parts)


def _ticket_text_desc(repo: Path, branch: str, nn: int, found: Optional[dict]) -> str:
    """The `ticket:` line; `?` for a field not known yet, never a guess."""
    state = _round_state(repo, nn)
    if not found:
        return (f"{nn} (?) on {branch}: ? · Status: ? · round {nn}: {state}")
    name = f"{rounds.TASKS_DIR}/{found['name']}" if found.get("name") else "?"
    title = found.get("ar") or ""
    head = f"{nn} ({title})" if title else f"{nn}"
    word = found.get("word") or ""
    note = found.get("note") or ""
    status = (f"Status: {word}" + (f" ({note})" if note else "")) if word else "Status: missing"
    return f"{head} {name} on {branch} · {status} · round {nn}: {state}"


def _refuse(msg, args, *, where=None, ticket=None, flow=None, hints=None) -> int:
    """The AR-14 refusal: one line, the flow block, then the hints, exit 2."""
    return output.refuse_ctx(msg, where=where, ticket=ticket, flow=flow, hints=hints,
                             fmt=getattr(args, "output", "table"))


def _verb_flags(verb: str, reason: str = "", note: str = "") -> str:
    """The `--reason` / `--note` flags *verb* gets, quoted — a hint must be pasteable."""
    out = ""
    if verb == "close":
        out += f" --reason {shlex.quote(reason or '…')}"
    elif note:
        out += f" --note {shlex.quote(note)}"
    return out


def _cmd(verb: str, nn: int, branch: str, reason: str = "", note: str = "") -> str:
    """The same command again, with the real NN and branch filled in."""
    return f"arena issue {verb} {nn} --branch {branch}{_verb_flags(verb, reason, note)}"


def _issue_land_exists() -> bool:
    """Whether `issue land` (AR-8) is a verb with a handler — its hint is conditional."""
    try:
        from tools.arena import cli as arena_cli
        return arena_cli.OBJECTS["issue"].verbs["land"].handler is not None
    except (ImportError, KeyError, AttributeError):
        return False


def _branch_refusal(err, repo: Path, args, prof: dict, verb: str, nn: int) -> int:
    """The three step-0 refusals: no branch, a branch only on origin, a missing one."""
    where = _checkout_text(repo)
    wanted = (getattr(args, "branch", None) or "").strip() or (prof.get("branch") or "").strip()
    message, detail, hints = str(err), "", []
    branches = []
    try:
        branches = _local_branches(repo)
    except GitRefError:
        pass
    if wanted and _rev(repo, f"refs/remotes/origin/{wanted}") is not None:
        where += f" · target branch {wanted} @ ?, on origin only"
        detail = f"only on origin"
        message = f"{wanted} is only on origin — a remote ref is never written"
        hints = [
            {"why": f"create {wanted} from origin", "command": f"git branch {wanted} origin/{wanted}"},
            {"why": "then the same command again", "command": _cmd(verb, nn, wanted)},
        ]
    elif wanted and branches:
        import difflib
        close = difflib.get_close_matches(wanted, branches, n=3) or branches[:1]
        where += f" · target branch {wanted} @ ?, missing"
        detail = "missing"
        message = f"branch {wanted!r} does not exist"
        hints = [{"why": "look for it", "command": f"git branch --list '*{wanted.split('-')[0]}*'"}]
        for name in close:
            hints.append({"why": f"if it is {name}",
                          "command": _cmd(verb, nn, name)})
    else:
        target = branches[0] if branches else wanted
        if target:
            hints.append({"why": f"the last branch committed here",
                          "command": _cmd(verb, nn, target)})
        if branches:
            hints.append({"why": "or switch to one", "command": f"git switch {branches[0]}"})
        if not hints:
            hints.append({"why": "name the branch", "command": f"arena issue {verb} {nn} --branch main"})
    return _refuse(message, args, where=where, ticket=None,
                   flow=_flow(STATUS_FLOW, 0, detail or wanted or ""), hints=hints)


# ── the write: checked out here, or one plumbing commit on B ──────────────────

def _worktree_or_refuse(repo: Path, branch: str, args, verb: str, nn: int, flow: tuple,
                        where: str, ticket: str):
    """`_worktree_of`'s answer, or an exit code when the read that would give it
    failed: a checkout that cannot be listed is not one to write under, so the
    read that failed is named.
    """
    try:
        return _worktree_of(repo, branch)
    except GitRefError as err:
        return _refuse(
            f"cannot tell where {branch} is checked out: `git worktree list` said: {err}",
            args, where=where, ticket=ticket,
            flow=_flow(flow, 1, "worktree list unreadable"),
            hints=[{"why": "the read that failed",
                    "command": f"git -C {os.path.realpath(str(repo))} worktree list"},
                   {"why": "then the same command again", "command": _cmd(verb, nn, branch)}])


def _commit_plumbed(repo: Path, branch: str, rel: str, content: str, subject: str) -> str:
    """One commit on *branch* in a temporary index; the new sha.

    `update-ref`'s old value makes it a compare-and-swap: a branch that moved
    between the read and the write is refused, and nothing is lost.
    """
    expect = _rev(repo, f"refs/heads/{branch}")
    if expect is None:
        raise GitRefError(f"branch {branch!r} does not exist")
    tip = f"refs/heads/{branch}"
    tree = tree_with_file(repo, tip, rel, content)
    sha = git(repo, "commit-tree", tree, "-p", tip, "-m", subject)
    git(repo, "update-ref", tip, sha, expect)
    current = _rev(repo, tip)
    if current != sha:
        raise GitRefError(f"git update-ref {tip}: the branch moved from {expect[:7]} to "
                          f"{str(current)[:7]} — another arena call committed first")
    return sha


def _write_and_commit(repo: Path, args, prof, verb, nn, branch, name, text, new_text,
                      out, word, target, note, reason, wt, flow, where, ticket) -> int:
    """Steps `write` and `commit`, the success line and the push command."""
    rel = f"{rounds.TASKS_DIR}/{name}"
    subject = f"{nn}: status {word or 'none'} → {target}"
    root = os.path.realpath(str(Path(repo)))
    try:
        if wt is _HERE:
            dirty = _uncommitted(repo, rel)
            if dirty:
                return _refuse(
                    f"{rel} has uncommitted edits — they are never overwritten or swept "
                    f"into the commit",
                    args, where=where, ticket=ticket,
                    flow=_flow(STATUS_FLOW, 6, f"uncommitted: {', '.join(dirty)}"),
                    hints=[
                        {"why": "see them", "command": f"git -C {root} diff -- {rel}"},
                        {"why": "keep them, then run the same command again",
                         "command": f"git -C {root} commit --only -m \"{nn}: ticket\" -- {rel}"},
                        {"why": "drop them", "command": f"git -C {root} restore -- {rel}"},
                        {"why": "then the same command again", "command": _cmd(verb, nn, branch, note=note)},
                    ])
            # `--only` commits this one path: other staged or modified files stay out
            (repo / rel).write_text(new_text, encoding="utf-8", errors="surrogateescape")
            git(repo, "commit", "--only", "-m", subject, "--", rel)
            sha = _rev(repo, "HEAD") or ""
        else:
            sha = _commit_plumbed(repo, branch, rel, new_text, subject)
    except GitRefError as err:
        message = str(err)
        # `update-ref` refusing is the compare-and-swap: the branch moved between
        # the read and the write, so a second arena call committed first.
        moved = "cannot lock ref" in message or "moved from" in message
        first = ({"why": f"what moved {branch}", "command": f"git log -3 --oneline {branch}"}
                 if moved
                 else {"why": "what the commit refused", "command": f"git -C {root} status"})
        return _refuse(message, args, where=where, ticket=ticket,
                       flow=_flow(STATUS_FLOW, 7,
                                  f"{branch} moved while committing" if moved
                                  else "the commit refused it"),
                       hints=[first,
                              {"why": "then the same command again",
                               "command": _cmd(verb, nn, branch, note=note)}])
    if out == "json":
        print(json.dumps(_scrubbed({"ticket": nn, "branch": branch, "from": word or "",
                                    "to": target, "commit": sha})))
    else:
        print(f"{nn}: {word or 'none'} → {target} on {branch} @ {str(sha)[:7]} · "
              f"git push origin {branch}")
    return rounds.EXIT_OK


def issue_status(repo: Path, args: argparse.Namespace, prof: dict, verb: str) -> int:
    """`arena issue queue|open|close|reopen NN [--branch B] [--reason|--note TEXT]`.

    The only writers of a ticket's `**Status:**` line apart from `issue land`
    (AR-8). Everything is resolved before anything is written: the branch, where
    it is checked out, the ticket on it, the status it holds, the transition and
    the round. A refusal is one line, then the flow block and the hints.
    """
    repo = Path(repo)
    out = getattr(args, "output", "table")
    raw = str(getattr(args, "number", "") or "")
    if not re.fullmatch(r"\d+", raw) or int(raw) <= 0:
        return output.refuse(f"issue {verb}: {raw!r} is not a ticket number")
    nn = int(raw)
    from_states, target = TRANSITIONS[verb]
    reason = (getattr(args, "reason", None) or "").strip()
    note = (getattr(args, "note", None) or "").strip()

    try:
        branch = rounds.integration_branch(repo, args.branch, prof)
    except (rounds.RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return _branch_refusal(err, repo, args, prof, verb, nn)
    where = _where_text(repo, branch, nn)

    wt = _worktree_or_refuse(repo, branch, args, verb, nn, STATUS_FLOW, where,
                             _ticket_text_desc(repo, branch, nn, None))
    if isinstance(wt, int):        # `worktree list` failed: already refused
        return wt
    if wt is not None and wt is not _HERE:
        return _refuse(
            f"{branch} is checked out in {wt} — its working tree would go stale",
            args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
            flow=_flow(STATUS_FLOW, 1, f"checked out in {wt}"),
            hints=[{"why": f"run it there instead",
                    "command": f"cd {wt} && arena issue {verb} {nn}{_verb_flags(verb, reason, note)}"}])

    names = _on_branch(repo, branch, nn)
    checkout = _in_folder(repo, rounds.TASKS_DIR, nn)
    drafts = _in_folder(repo, DRAFTS_DIR, nn)
    if len(names) > 1:
        logs = [f"git log -1 --format=%h\\ %s {branch} -- {rounds.TASKS_DIR}/{n}" for n in names]
        return _refuse(
            f"two tickets {nn} on {branch}: {', '.join(names)} — find_ticket would refuse too",
            args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
            flow=_flow(STATUS_FLOW, 2, f"two files: {', '.join(names)}"),
            hints=[{"why": f"what {n} last was", "command": log} for n, log in zip(names, logs)]
                + [{"why": f"drop {n} if it is the wrong one",
                    "command": f"git -C {os.path.realpath(str(repo))} rm {rounds.TASKS_DIR}/{n}"}
                   for n in names])
    if not names:
        if drafts and not checkout:
            file = drafts[0]
            return _refuse(
                f"{nn} is only in {DRAFTS_DIR}/ — a draft has no **Status:** line yet",
                args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
                flow=_flow(STATUS_FLOW, 2, f"draft only: {DRAFTS_DIR}/{file}"),
                hints=[{"why": f"publish it first",
                        "command": f"arena issue create --file {DRAFTS_DIR}/{file}"}])
        if checkout:
            file = checkout[0]
            rel = f"{rounds.TASKS_DIR}/{file}"
            return _refuse(
                f"{nn} is in the checkout's {rel} but not committed on {branch} — the status "
                f"is written on the branch",
                args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
                flow=_flow(STATUS_FLOW, 2, "not on B"),
                hints=[{"why": f"commit it on {branch} first",
                        "command": f"git -C {os.path.realpath(str(repo))} commit --only -m "
                                   f"\"{nn}: ticket\" -- {rel}"},
                       {"why": "then the same command again", "command": _cmd(verb, nn, branch, note=note)}])
        return _refuse(
            f"no ticket {nn} in {DRAFTS_DIR}/, {rounds.TASKS_DIR}/ or on {branch}",
            args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
            flow=_flow(STATUS_FLOW, 2, f"no ticket {nn}"),
            hints=[{"why": "what is there", "command": f"arena issue list --branch {branch}"}])

    name = names[0]
    try:
        text = _body_on_branch(repo, branch, name)
    except GitRefError as err:
        return _refuse(str(err), args, where=where,
                       ticket=_ticket_text_desc(repo, branch, nn, None),
                       flow=_flow(STATUS_FLOW, 3, "unreadable"), hints=[])
    word = status_word(text)
    cur_note = status_note(text)
    found = {"name": name, "ar": contest_draft.title_of(text), "word": word, "note": cur_note}
    ticket = _ticket_text_desc(repo, branch, nn, found)
    if not word:
        return _refuse(
            f"{nn} has no **Status:** line — a status verb does not invent where it goes",
            args, where=where, ticket=ticket, flow=_flow(STATUS_FLOW, 3, "no line"),
            hints=[{"why": f"add `**Status:** open` under the title",
                    "command": f"arena issue edit {nn} --branch {branch}"}])
    if word not in STATUS_WORDS:
        return _refuse(
            f"{nn}: **Status:** {word!r} is not one of {', '.join(STATUS_WORDS)}",
            args, where=where, ticket=ticket, flow=_flow(STATUS_FLOW, 3, f"unknown word {word!r}"),
            hints=[{"why": "fix the word", "command": f"arena issue edit {nn} --branch {branch}"}])
    if word == "landed":
        # §7: both hints, always — `issue land` is the verb that owns this word, whether
        # or not AR-8's handler is there yet (only §5's blocker report waits for it)
        hints = [{"why": "where it landed", "command": f"arena issue view {nn} --branch {branch}"},
                 {"why": "the verb that sets it (AR-8)",
                  "command": f"arena issue land {nn} --branch {branch}"}]
        return _refuse(
            f"{nn} is landed — landed is set by `arena issue land` (AR-8), not {verb}",
            args, where=where, ticket=ticket, flow=_flow(STATUS_FLOW, 4, "landed"), hints=hints)
    if verb == "close" and not reason:
        return _refuse(
            f"close needs --reason: why is {nn} closed?",
            args, where=where, ticket=ticket, flow=_flow(STATUS_FLOW, 4, "no --reason"),
            hints=[{"why": "the same command with a reason",
                    "command": f"arena issue close {nn} --branch {branch} --reason \"…\""}])
    new_text = set_status_text(text, target, "" if target == "closed" else note,
                               reason if target == "closed" else "")
    repeatable = word == target and verb in IDEMPOTENT
    if not repeatable and word not in from_states:
        if word == "closed":
            hint = {"why": f"{verb} is not allowed from closed", "command": _cmd("reopen", nn, branch)}
        else:
            hint = {"why": f"{verb} is not allowed from {word}",
                    "command": _cmd({"queued": "open", "open": "queue", "closed": "reopen"}.get(word, "open"),
                                    nn, branch)}
        return _refuse(
            f"{verb} is not allowed from {word} (allowed: {', '.join(from_states)})",
            args, where=where, ticket=ticket,
            flow=_flow(STATUS_FLOW, 4, f"{word} → {target}"), hints=[hint])
    if repeatable and new_text == text:
        # already there, and nothing new to say: no commit, no flow, one line.
        if out == "json":
            print(json.dumps(_scrubbed({"ticket": nn, "branch": branch, "from": word,
                                        "to": target, "commit": None})))
        else:
            print(f"{nn}: already {target}")
        return rounds.EXIT_OK
    if rounds.round_alive(repo, nn, rounds.PROC_ROOT):
        return _refuse(
            f"round {nn} is running — a running ticket's status is not written",
            args, where=where, ticket=ticket, flow=_flow(STATUS_FLOW, 5, "running"),
            hints=[{"why": "watch it", "command": f"arena status {nn}"},
                   {"why": "or stop it", "command": f"arena run stop {nn}"},
                   {"why": "then the same command again", "command": _cmd(verb, nn, branch, note=note)}])
    if not _confirm_status(nn, word, target, branch, bool(getattr(args, "yes", False))):
        return output.refuse(f"not applied — {rounds.TASKS_DIR}/{name} unchanged on {branch}")
    return _write_and_commit(repo, args, prof, verb, nn, branch, name, text, new_text, out,
                             word, target, note, reason, wt, _flow(STATUS_FLOW, 6),
                             where, ticket)


def _confirm_status(nn: int, word: str, target: str, branch: str, yes: bool) -> bool:
    """§1: the before → after line, then `apply? [y/N]` — unless *yes*, which skips both
    (the success line says it once). EOF is a no, as in `models._confirm`: a closed
    stdin never writes a commit nobody agreed to."""
    if yes:
        return True
    print(f"{nn}: {word or 'none'} → {target} on {branch}")
    try:
        return input("apply? [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, OSError):
        # a closed stdin, a cron job and a captured one are all a no, never a crash
        return False


def _header_problems(text: str) -> list[str]:
    """The missing `**Label:**` lines of `draft.HEADER_FIELDS`, intake's own check."""
    return [f"missing **{label}:** header field"
            for label in contest_draft.HEADER_FIELDS
            if not re.search(rf"^\*\*{re.escape(label)}:\*\*", text or "", re.MULTILINE)]


def _editor_path(repo: Path, name: str) -> Path:
    """The temp file `$EDITOR` opens; kept when a refusal keeps the edit."""
    folder = Path(repo) / ".arena" / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="arena-edit-", dir=str(folder)))
    return tmp / name


def issue_edit(repo: Path, args: argparse.Namespace, prof: dict) -> int:
    """`arena issue edit NN [--branch B]`: $EDITOR on the ticket, then lint and commit.

    The editor may do anything but the `**Status:**` word: that is the verbs'
    line. A refused edit keeps the edited text in a temp file and prints its
    path, so nothing the editor did is lost.
    """
    repo = Path(repo)
    out = getattr(args, "output", "table")
    raw = str(getattr(args, "number", "") or "")
    if not re.fullmatch(r"\d+", raw) or int(raw) <= 0:
        return output.refuse(f"issue edit: {raw!r} is not a ticket number")
    nn = int(raw)
    verb = "edit"

    try:
        branch = rounds.integration_branch(repo, args.branch, prof)
    except (rounds.RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return _branch_refusal(err, repo, args, prof, verb, nn)
    where = _where_text(repo, branch, nn)

    wt = _worktree_or_refuse(repo, branch, args, "edit", nn, EDIT_FLOW, where,
                             _ticket_text_desc(repo, branch, nn, None))
    if isinstance(wt, int):        # `worktree list` failed: already refused
        return wt
    if wt is not None and wt is not _HERE:
        return _refuse(
            f"{branch} is checked out in {wt} — its working tree would go stale",
            args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
            flow=_flow(EDIT_FLOW, 1, f"checked out in {wt}"),
            hints=[{"why": f"run it there instead", "command": f"cd {wt} && arena issue edit {nn}"}])

    names = _on_branch(repo, branch, nn)
    if len(names) > 1:
        return _refuse(
            f"two tickets {nn} on {branch}: {', '.join(names)} — find_ticket would refuse too",
            args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
            flow=_flow(EDIT_FLOW, 2, f"two files: {', '.join(names)}"),
            hints=[{"why": f"what {n} last was",
                    "command": f"git log -1 --format=%h\\ %s {branch} -- {rounds.TASKS_DIR}/{n}"}
                   for n in names]
                  + [{"why": f"drop {n} if it is the wrong one",
                      "command": f"git -C {os.path.realpath(str(repo))} rm {rounds.TASKS_DIR}/{n}"}
                     for n in names])
    if not names:
        checkout = _in_folder(repo, rounds.TASKS_DIR, nn)
        drafts = _in_folder(repo, DRAFTS_DIR, nn)
        if drafts and not checkout:
            return _refuse(
                f"{nn} is only in {DRAFTS_DIR}/ — a draft has no **Status:** line yet",
                args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
                flow=_flow(EDIT_FLOW, 2, "draft only"),
                hints=[{"why": "publish it first",
                        "command": f"arena issue create --file {DRAFTS_DIR}/{drafts[0]}"}])
        if checkout:
            rel = f"{rounds.TASKS_DIR}/{checkout[0]}"
            return _refuse(
                f"{nn} is in the checkout's {rel} but not committed on {branch}",
                args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
                flow=_flow(EDIT_FLOW, 2, "not on B"),
                hints=[{"why": f"commit it on {branch} first",
                        "command": f"git -C {os.path.realpath(str(repo))} commit --only -m "
                                   f"\"{nn}: ticket\" -- {rel}"},
                       {"why": "then the same command again", "command": _cmd("edit", nn, branch)}])
        return _refuse(
            f"no ticket {nn} in {DRAFTS_DIR}/, {rounds.TASKS_DIR}/ or on {branch}",
            args, where=where, ticket=_ticket_text_desc(repo, branch, nn, None),
            flow=_flow(EDIT_FLOW, 2, f"no ticket {nn}"),
            hints=[{"why": "what is there", "command": f"arena issue list --branch {branch}"}])

    name = names[0]
    rel = f"{rounds.TASKS_DIR}/{name}"
    try:
        text = _body_on_branch(repo, branch, name)
    except GitRefError as err:
        return _refuse(str(err), args, where=where,
                       ticket=_ticket_text_desc(repo, branch, nn, None),
                       flow=_flow(EDIT_FLOW, 3, "unreadable"), hints=[])
    word = status_word(text)
    found = {"name": name, "ar": contest_draft.title_of(text), "word": word,
             "note": status_note(text)}
    ticket = _ticket_text_desc(repo, branch, nn, found)
    if rounds.round_alive(repo, nn, rounds.PROC_ROOT):
        return _refuse(
            f"round {nn} is running — a running ticket's text is not written",
            args, where=where, ticket=ticket, flow=_flow(EDIT_FLOW, 5, "running"),
            hints=[{"why": "watch it", "command": f"arena status {nn}"},
                   {"why": "or stop it", "command": f"arena run stop {nn}"},
                   {"why": "then the same command again", "command": _cmd("edit", nn, branch)}])

    editor = (os.environ.get("EDITOR") or os.environ.get("VISUAL") or "").strip()
    if not editor:
        if not sys.stdin.isatty():
            return _refuse(
                "no $EDITOR set — edit needs one", args, where=where, ticket=ticket,
                flow=_flow(EDIT_FLOW, 6, "no $EDITOR"),
                hints=[{"why": "set one for this call",
                        "command": f"EDITOR=vi arena issue edit {nn} --branch {branch}"}])
        editor = "vi"
    path = _editor_path(repo, name)
    path.write_text(text, encoding="utf-8", errors="surrogateescape")
    try:
        proc = subprocess.run(shlex.split(editor) + [str(path)], cwd=str(repo), env=os.environ)
    except (OSError, ValueError) as err:
        return _refuse(f"$EDITOR {editor!r} did not run: {err}", args, where=where, ticket=ticket,
                       flow=_flow(EDIT_FLOW, 6, str(err)),
                       hints=[{"why": "try again", "command": _cmd("edit", nn, branch)}])
    if proc.returncode != 0:
        return _refuse(f"$EDITOR {editor!r} exited {proc.returncode}", args, where=where,
                       ticket=ticket, flow=_flow(EDIT_FLOW, 6, f"exited {proc.returncode}"),
                       hints=[{"why": "the edited text, kept", "command": str(path)},
                              {"why": "try again", "command": _cmd("edit", nn, branch)}])

    new_text = read_utf8(path)
    if new_text == text:
        if out == "json":
            print(json.dumps(_scrubbed({"ticket": nn, "branch": branch, "from": word or "",
                                        "to": word or "", "commit": None})))
        else:
            print(f"{nn}: no change")
        return rounds.EXIT_OK
    new_word = status_word(new_text)
    verb_for = VERB_FOR.get(new_word)
    # §3, §6: a status that reads as one of the four words is the verbs' line, and
    # `landed` is `issue land`'s; a missing or unknown word is exactly what the operator
    # is sent here to fix, so `edit` may write it — to anything but `landed`
    hand_changed = (word in STATUS_WORDS and new_word != word) or \
                   (new_word == "landed" and word != "landed")
    if hand_changed:
        return _refuse(
            f"the editor changed **Status:** {word!r} → {new_word!r} — use "
            f"`arena issue {verb_for or 'queue'}` for that",
            args, where=where, ticket=ticket,
            flow=_flow(EDIT_FLOW, 7, f"{word} → {new_word}"),
            hints=[{"why": f"the {new_word} you edited towards",
                    "command": _cmd(verb_for or "queue", nn, branch)},
                   {"why": "the rest of the edit, kept here", "command": str(path)},
                   {"why": "then the same command again", "command": _cmd("edit", nn, branch)}])
    problems = _header_problems(new_text)
    if problems:
        return _refuse(
            "; ".join(problems), args, where=where, ticket=ticket,
            flow=_flow(EDIT_FLOW, 8, "the lint refused it"),
            hints=[{"why": "the edited text, kept here", "command": str(path)},
                   {"why": "then the same command again", "command": _cmd("edit", nn, branch)}])

    if wt is _HERE:
        dirty = _uncommitted(repo, rel)
        if dirty:
            return _refuse(
                f"{rel} has uncommitted edits — they are never overwritten or swept "
                f"into the commit",
                args, where=where, ticket=ticket,
                flow=_flow(EDIT_FLOW, 9, f"uncommitted: {', '.join(dirty)}"),
                hints=[{"why": "see them", "command": f"git -C {os.path.realpath(str(repo))} diff -- {rel}"},
                       {"why": "keep them", "command": f"git -C {os.path.realpath(str(repo))} commit --only -m \"{nn}: ticket\" -- {rel}"},
                       {"why": "drop them", "command": f"git -C {os.path.realpath(str(repo))} restore -- {rel}"},
                       {"why": "the edited text, kept here", "command": str(path)},
                       {"why": "then the same command again", "command": _cmd("edit", nn, branch)}])
        (repo / rel).write_text(new_text, encoding="utf-8", errors="surrogateescape")
        try:
            git(repo, "commit", "--only", "-m", f"{nn}: ticket edited", "--", rel)
            sha = _rev(repo, "HEAD") or ""
        except GitRefError as err:
            return _refuse(str(err), args, where=where, ticket=ticket,
                           flow=_flow(EDIT_FLOW, 9, "the commit refused it"),
                           hints=[{"why": "what the commit refused",
                                   "command": f"git -C {os.path.realpath(str(repo))} status"},
                                  {"why": "the edited text, kept here", "command": str(path)}])
    else:
        try:
            sha = _commit_plumbed(repo, branch, rel, new_text, f"{nn}: ticket edited")
        except GitRefError as err:
            message = str(err)
            hints = ([{"why": "what moved the branch", "command": f"git log -3 --oneline {branch}"}
                      if "moved from" in message
                      else [{"why": "what the commit refused", "command": f"git -C {os.path.realpath(str(repo))} status"}]
                      ]) + [{"why": "the edited text, kept here", "command": str(path)},
                           {"why": "then the same command again", "command": _cmd("edit", nn, branch)}]
            return _refuse(message, args, where=where, ticket=ticket,
                           flow=_flow(EDIT_FLOW, 9, "the commit refused it"), hints=hints)
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError:
        pass
    if out == "json":
        print(json.dumps(_scrubbed({"ticket": nn, "branch": branch, "from": word or "",
                                    "to": new_word, "commit": sha})))
    else:
        print(f"{nn}: edited on {branch} @ {str(sha)[:7]} · git push origin {branch}")
    return rounds.EXIT_OK


# ── AR-14 §5: what blocks a run start ─────────────────────────────────────────

def _run_start_flow(number: int) -> tuple:
    """`RUN_START_FLOW` with the round's own number: the literal `NN` is a placeholder."""
    return tuple(label.replace("NN", str(number)) for label in RUN_START_FLOW)


def blocking_tickets(repo: Path, branch: str, number: int) -> list[dict]:
    """The tickets on *branch*'s `epic-tasks/` that intake would hand out first.

    A lower number whose `**Status:**` first word is not in `next_task.SKIP_STATUS`
    — including no line at all and an unknown word — and whose name its progress
    file does not record. Read through `scripts/next_task.py` itself (`_status`,
    `SKIP_STATUS`, `recorded`), so the check and the script never disagree.
    """
    repo = Path(repo)
    try:
        names = ls_tree_names(repo, f"refs/heads/{branch}", rounds.TASKS_DIR + "/")
    except GitRefError:
        return []
    # A progress file that will not decode is not a blocker: `run start` never
    # dies on an unreadable CSV, it reads the file as if it were empty.
    try:
        recorded = next_task_mod.recorded(repo / rounds.TASKS_DIR / "PROGRESS.csv")
    except (OSError, ValueError):
        recorded = {}
    found = []
    for full in names:
        name = full.rsplit("/", 1)[-1]
        match = rounds._TICKET_RE.match(name)
        if not match or name.endswith(contest_draft.REJECTED_SUFFIX):
            continue
        ticket_no = int(match.group(1))
        if ticket_no >= number:
            continue
        if name in recorded:
            continue
        try:
            body = printable(git(repo, "show",
                                 f"refs/heads/{branch}:{rounds.TASKS_DIR}/{name}", strip=False))
        except GitRefError:
            continue
        status = next_task_mod._status(body)
        if status.startswith(next_task_mod.SKIP_STATUS):
            continue
        found.append({"number": ticket_no, "name": name, "status": status,
                      "ar": contest_draft.title_of(body)})
    return sorted(found, key=lambda item: item["number"])


def report_intake_blockers(repo: Path, branch: str, number: int, fmt: str = "table") -> bool:
    """§5: one report per blocking ticket, before anything is built; True when any."""
    repo = Path(repo)
    blockers = blocking_tickets(repo, branch, number)
    if not blockers:
        return False
    numbers = ", ".join(str(b["number"]) for b in blockers)
    land = _issue_land_exists()
    for item in blockers:
        nn = item["number"]
        hints = []
        status = item["status"]
        if not status:
            hints.append({"why": f"{nn} has no **Status:** line",
                          "command": f"arena issue edit {nn} --branch {branch}"})
        elif status not in STATUS_WORDS:
            hints.append({"why": f"{nn}'s **Status:** {status!r} is not "
                                 f"{', '.join(STATUS_WORDS)} — fix it",
                          "command": f"arena issue edit {nn} --branch {branch}"})
        hints += [
            {"why": f"{nn}'s round is done elsewhere — park it",
             "command": f"arena issue queue {nn} --branch {branch}"},
            {"why": f"{nn} is no longer wanted",
             "command": f"arena issue close {nn} --branch {branch} --reason \"…\""},
        ]
        if land:
            hints.append({"why": f"{nn}'s code is on {branch}",
                          "command": f"arena issue land {nn} --branch {branch}"})
        output.refuse_ctx(
            f"{nn} ({item['ar']}) is {status or 'open'} on {branch} and comes before "
            f"{number} — intake would hand the sessions {nn}",
            where=_where_text(repo, branch, number),
            ticket=_ticket_text_desc(repo, branch, nn,
                                     {"name": item["name"], "ar": item["ar"],
                                      "word": status, "note": ""}),
            flow=_flow(_run_start_flow(number), 2, f"lower open tickets {numbers}"),
            hints=hints, fmt=fmt)
    return True
