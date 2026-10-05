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
from .gitref import GitRefError, git, printable, read_utf8

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
        if name.isdigit():
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
        llm_call, review_call = contest_cli.draft_callables(config, bool(args.no_review))
    except contest_cli.DraftSetupError as err:
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

    reviewed = review_call is not None
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
