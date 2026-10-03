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


# ── AR-7: issue create ───────────────────────────────────────────────────────
#: The assembled brief's ceiling — the same budget the draft gives its sources.
#: Over it is a refusal, never a silent cut: a cut epic is half a spec.
BRIEF_LIMIT = contest_draft.SOURCE_BUDGET

DRAFTS_DIR = ".arena/drafts"

TASK_HEADER = "## Task (from the operator — this wins over anything in the material below)"

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_FENCE_LINE = re.compile(r"^\s*```")


class BriefError(ValueError):
    """A brief `build_brief` refuses — one line, before collect and any model call."""


def _rel(repo: Path, path: Path) -> str:
    """*path* repo-relative when it is inside *repo*, absolute otherwise."""
    try:
        return path.resolve().relative_to(Path(repo).resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _holders(repo: Path, config, branch: str) -> list[tuple[int, str, bool]]:
    """`(NN, where, rejected)` for every place a number lives — `next_number`'s sources.

    Tickets (drafts, the checkout, the branch), `*.rejected.md` in the drafts and
    `epic-tasks/`, round folders `NN` / `NN.K` under `out_dir`, and the refs
    `arena-round/NN`. *rejected* marks the one kind an explicit `--number` may reuse.
    """
    repo = Path(repo)
    out: list[tuple[int, str, bool]] = []
    for ticket in scan(repo, config, branch, rounds.PROC_ROOT):
        out.append((ticket.number, ticket.path, False))
    for folder in (repo / DRAFTS_DIR, repo / rounds.TASKS_DIR):
        for name in _folder_names(folder):
            match = rounds._TICKET_RE.match(name)
            if match and name.endswith(contest_draft.REJECTED_SUFFIX):
                out.append((int(match.group(1)), _rel(repo, folder / name), True))
    out_root = repo / config.out_dir
    if out_root.is_dir():
        for entry in sorted(out_root.iterdir()):
            match = rounds._ROUND_DIR.match(entry.name)
            if match and entry.is_dir():
                out.append((int(match.group(1)), _rel(repo, entry), False))
    refs = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/" + rounds.REF_PREFIX)
    for ref in refs.splitlines():
        tail = ref[len(rounds.REF_PREFIX):] if ref.startswith(rounds.REF_PREFIX) else ""
        if tail.isdigit():
            out.append((int(tail), ref, False))
    return out


def next_number(repo: Path, config, branch: str) -> int:
    """Max + 1 over every place a number lives (not the first gap); 1 when none."""
    return max((nn for nn, _, _ in _holders(repo, config, branch)), default=0) + 1


def _read_file(repo: Path, raw: str) -> tuple[str, str]:
    """`(shown path, text)` of a `--file`: relative to *repo*, never to the cwd."""
    path = Path(raw)
    if not path.is_absolute():
        path = Path(repo) / path
    shown = _rel(repo, path)
    if not path.exists():
        raise BriefError(f"--file {shown}: no such file")
    if path.is_dir():
        raise BriefError(f"--file {shown}: is a directory")
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as err:
        raise BriefError(f"--file {shown}: not readable as UTF-8 ({type(err).__name__})")
    if not text.strip():
        raise BriefError(f"--file {shown}: is empty")
    return shown, text


def cut_section(text: str, item: str, shown: str) -> str:
    """The `item` section of *text*: its heading line to the next heading of the
    same or a higher level. Headings inside ``` fences are not headings."""
    lines = text.splitlines(keepends=True)
    headings: list[tuple[int, int, str]] = []  # (line index, level, heading text)
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE_LINE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING_RE.match(line.rstrip("\n"))
        if match:
            headings.append((i, len(match.group(1)), match.group(2)))
    item_re = re.compile(re.escape(item) + r"(?!\w)")
    hits = [h for h in headings if item_re.match(h[2])]
    if not hits:
        raise BriefError(f"no section {item} in {shown}")
    if len(hits) > 1:
        raise BriefError(f"{item} is in {shown} twice: lines "
                         + ", ".join(str(h[0] + 1) for h in hits))
    start, level, _ = hits[0]
    end = next((i for i, lv, _ in headings if i > start and lv <= level), len(lines))
    section = "".join(lines[start:end])
    if not section.strip():
        raise BriefError(f"section {item} in {shown} is empty")
    return section.strip("\n")


def build_brief(repo: Path, text: Optional[str], files: Optional[list[str]],
                item: Optional[str]) -> str:
    """The brief the model gets: the operator's task first, then each material block.

    `BriefError` (one line) for every refusal in AR-7's table; nothing is cut.
    """
    files = list(files or [])
    text = text if text and text.strip() else None
    if text is None and not files:
        raise BriefError("issue create: give a brief text or --file PATH")
    if item is not None and len(files) != 1:
        raise BriefError("--item needs exactly one --file")
    blocks = []
    if text is not None:
        blocks.append(f"{TASK_HEADER}\n\n{text.strip()}")
    for raw in files:
        shown, body = _read_file(repo, raw)
        if item is not None:
            blocks.append(f"## Material: {shown} — section {item}\n\n"
                          f"{cut_section(body, item, shown)}")
        else:
            blocks.append(f"## Material: {shown}\n\n{body.strip(chr(10))}")
    brief = "\n\n".join(blocks) + "\n"
    if len(brief) > BRIEF_LIMIT:
        raise BriefError(f"brief is {len(brief)} characters, limit {BRIEF_LIMIT} — "
                         "cut it with --item ID or a shorter file")
    return brief


def _number(repo: Path, config, branch: str, raw: Optional[str]) -> int:
    """`--number NN` checked against every holder but a `.rejected.md`, else max + 1."""
    holders = _holders(repo, config, branch)
    if raw is None:
        return max((nn for nn, _, _ in holders), default=0) + 1
    if not str(raw).isdigit() or int(raw) < 1:
        raise BriefError(f"--number {raw!r} is not a positive integer")
    nn = int(raw)
    for held, where, rejected in holders:
        if held == nn and not rejected:
            kind = "round" if where.startswith(rounds.REF_PREFIX) or not where.endswith(".md") \
                else "ticket"
            raise BriefError(f"{kind} {nn} exists ({where})")
    return nn


def issue_create(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena issue create ["text"] [--file F]... [--item ID] [--number NN] [--no-review]`.

    The draft lands in `.arena/drafts/NN-<slug>.md`: no branch switch, no commit.
    Every refusal before `draft_ticket` happens before collect and any model call.
    """
    repo = Path(repo)
    try:
        config = rounds.load_config(repo)
        branch = rounds.integration_branch(repo, args.branch, prof)
        brief = build_brief(repo, args.text, args.file, args.item)
        nn = _number(repo, config, branch, args.number)
        llm_call, review_call = contest_cli.draft_callables(config, args.no_review)
    except (rounds.RoundError, GitRefError, BriefError, contest_cli.DraftSetupError) as err:
        return output.refuse(str(err))
    try:
        result = contest_draft.draft_ticket(brief, repo=repo, config=config, round_no=nn,
                                    out_dir=repo / DRAFTS_DIR, llm_call=llm_call,
                                    review_call=review_call, commit=False)
    except ValueError as err:
        return output.refuse(str(err))

    def rel(path) -> Optional[str]:
        return _rel(repo, Path(path)) if path else None

    if args.output == "json":
        print(json.dumps(output._scrub_all(output.mask({
            "number": result.number, "path": rel(result.path),
            "rejected": bool(result.rejected), "problems": list(result.problems),
            "rejected_path": rel(result.rejected_path),
            "reviewed": review_call is not None}))))
        if args.no_review:
            print("review skipped (--no-review)", file=sys.stderr)
        return rounds.EXIT_USAGE if result.rejected else rounds.EXIT_OK
    if result.rejected:
        for problem in result.problems:
            print(output.scrub(f"- {problem}"), file=sys.stderr)
        if result.rejected_path:
            print(f"rejected draft: {rel(result.rejected_path)}", file=sys.stderr)
        return rounds.EXIT_USAGE
    if args.no_review:
        print("review skipped (--no-review)", file=sys.stderr)
    print(f"ticket {result.number} drafted: {rel(result.path)}")
    print(f"next: arena run start {result.number}")
    return rounds.EXIT_OK
