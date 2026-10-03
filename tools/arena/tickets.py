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
#: The longest brief `issue create` hands the model; a longer one is refused,
#: never cut — a cut epic is a ticket written against half a spec.
BRIEF_LIMIT = contest_draft.SOURCE_BUDGET

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_ROUND_DIR_RE = re.compile(r"^0*(\d+)(?:\.\d+)?$")

TASK_HEADER = "## Task (from the operator — this wins over anything in the material below)"
DRAFTS_DIR = ".arena/drafts"


class BriefError(Exception):
    """A brief that cannot be built; the message is the one line the CLI prints."""


def _shown(repo: Path, path: Path) -> str:
    """*path* repo-relative when it is inside *repo*, absolute otherwise."""
    try:
        return str(path.resolve().relative_to(Path(repo).resolve()))
    except ValueError:
        return str(path.resolve())


def _headings(lines: list[str]) -> list[tuple[int, int, str]]:
    """`(line index, level, text)` of every heading outside a fenced code block."""
    found, fenced = [], False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else _HEADING_RE.match(line)
        if match:
            found.append((i, len(match.group(1)), match.group(2)))
    return found


def _is_item(text: str, item: str) -> bool:
    """*text* starts with *item* and then a non-word character or the end."""
    return text.startswith(item) and (len(text) == len(item)
                                      or not re.match(r"\w", text[len(item)]))


def cut_section(text: str, item: str, shown: str) -> str:
    """The `### ITEM …` section of *text*: its heading to the next one of the
    same or a higher level, or the end. `BriefError` for none or for two."""
    lines = text.splitlines()
    heads = _headings(lines)
    hits = [h for h in heads if _is_item(h[2], item)]
    if not hits:
        raise BriefError(f"no section {item} in {shown}")
    if len(hits) > 1:
        raise BriefError(f"{item} is in {shown} twice: "
                         + "lines " + ", ".join(str(h[0] + 1) for h in hits))
    start, level, _ = hits[0]
    end = next((h[0] for h in heads if h[0] > start and h[1] <= level), len(lines))
    return "\n".join(lines[start:end])


def build_brief(repo: Path, text: Optional[str], files: Optional[list[str]],
                item: Optional[str]) -> str:
    """The brief the model gets (see the ticket's table); `BriefError` otherwise."""
    files = list(files or [])
    text = (text or "").strip()
    if not text and not files:
        raise BriefError("issue create: give a text, a --file, or both")
    if item is not None and not files:
        raise BriefError("issue create: --item needs a --file")
    if item is not None and len(files) > 1:
        raise BriefError("issue create: --item works on one --file, not several")
    blocks = []
    if text:
        blocks.append(f"{TASK_HEADER}\n\n{text}")
    for name in files:
        path = Path(name)
        if not path.is_absolute():
            path = Path(repo) / path
        shown = _shown(repo, path)
        if path.is_dir():
            raise BriefError(f"issue create: {shown} is a directory")
        try:
            body = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise BriefError(f"issue create: no file {shown}") from None
        except UnicodeDecodeError:
            raise BriefError(f"issue create: {shown} is not UTF-8 text") from None
        except OSError as err:
            raise BriefError(f"issue create: cannot read {shown}: {err.strerror}") from None
        head = f"## Material: {shown}"
        if item is not None:
            body = cut_section(body, item, shown)
            head += f" — section {item}"
        if not body.strip():
            raise BriefError(f"issue create: {shown} is empty")
        blocks.append(f"{head}\n\n{body.strip()}")
    brief = "\n\n".join(blocks)
    if len(brief) > BRIEF_LIMIT:
        raise BriefError(f"brief is {len(brief)} characters, limit {BRIEF_LIMIT} — "
                         "cut it with --item ID or a shorter file")
    return brief


def _round_numbers(repo: Path, config) -> dict[int, str]:
    """`{number: folder}` for `NN` and `NN.K` under the roster's `out_dir`."""
    root = Path(repo) / config.out_dir
    found: dict[int, str] = {}
    for folder in sorted(root.iterdir()) if root.is_dir() else []:
        match = _ROUND_DIR_RE.match(folder.name)
        if match and folder.is_dir():
            found.setdefault(int(match.group(1)), f"{config.out_dir}/{folder.name}")
    return found


def _ref_numbers(repo: Path) -> dict[int, str]:
    """`{number: ref}` for `refs/heads/arena-round/NN`."""
    prefix = f"refs/heads/{rounds.REF_PREFIX}"
    found = {}
    for ref in git(repo, "for-each-ref", "--format=%(refname)", prefix).splitlines():
        tail = ref[len(prefix):]
        if tail.isdigit():
            found[int(tail)] = ref
    return found


def _rejected_numbers(repo: Path) -> dict[int, str]:
    """`{number: path}` of the `*.rejected.md` files in the drafts and `epic-tasks/`."""
    found = {}
    for folder in (DRAFTS_DIR, rounds.TASKS_DIR):
        for name in _folder_names(Path(repo) / folder):
            match = rounds._TICKET_RE.match(name)
            if match and name.endswith(contest_draft.REJECTED_SUFFIX):
                found.setdefault(int(match.group(1)), f"{folder}/{name}")
    return found


def _taken(repo: Path, config, branch: str) -> dict[int, str]:
    """Every number a ticket, a round or a round ref already holds, with where."""
    found: dict[int, str] = {}
    for nn, where in _ref_numbers(repo).items():
        found[nn] = where
    for nn, folder in _round_numbers(repo, config).items():
        found[nn] = f"round {nn} exists ({folder})"
    for t in scan(repo, config, branch):
        found[t.number] = f"ticket {t.number} exists ({t.path})"
    for nn, where in list(found.items()):
        if where.startswith("refs/"):
            found[nn] = f"round {nn} exists ({where})"
    return found


def next_number(repo: Path, config, branch: str) -> int:
    """Max + 1 over every place a number lives (the first gap is never reused)."""
    held = set(_taken(repo, config, branch)) | set(_rejected_numbers(repo))
    return max(held, default=0) + 1


def _rel(repo: Path, path: Optional[Path]) -> Optional[str]:
    return None if path is None else _shown(repo, Path(path))


def issue_create(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena issue create ["text"] [--file P]… [--item ID] [--number NN] [--no-review]`."""
    repo = Path(repo)
    try:
        config = rounds.load_config(repo)
        branch = rounds.integration_branch(repo, args.branch, prof)
        brief = build_brief(repo, args.text, args.file, args.item)
        taken = _taken(repo, config, branch)
        if args.number is not None:
            if not re.fullmatch(r"\d+", str(args.number)) or int(args.number) < 1:
                return output.refuse(f"issue create: --number {args.number!r} is not "
                                     "a positive integer")
            nn = int(args.number)
            if nn in taken:
                return output.refuse(taken[nn])
        else:
            nn = next_number(repo, config, branch)
        llm_call, review_call = contest_cli.draft_callables(config, args.no_review)
        result = contest_draft.draft_ticket(
            brief, repo=repo, config=config, round_no=nn,
            out_dir=repo / DRAFTS_DIR, llm_call=llm_call,
            review_call=review_call, commit=False)
    except (rounds.RoundError, GitRefError, profile.ProfileError, BriefError,
            contest_cli.DraftSetupError, ValueError, OSError) as err:
        return output.refuse(str(err))
    if args.output == "json":
        print(json.dumps(_scrubbed({
            "number": nn, "path": _rel(repo, result.path),
            "rejected": result.rejected, "problems": list(result.problems),
            "rejected_path": _rel(repo, result.rejected_path),
            "reviewed": review_call is not None})))
        return rounds.EXIT_USAGE if result.rejected else rounds.EXIT_OK
    if result.rejected:
        for problem in result.problems:
            print(output.scrub(f"- {problem}"), file=sys.stderr)
        if result.rejected_path:
            print(f"rejected draft: {_rel(repo, result.rejected_path)}", file=sys.stderr)
        return rounds.EXIT_USAGE
    if args.no_review:
        print("review skipped (--no-review)", file=sys.stderr)
    print(f"ticket {nn} drafted: {_rel(repo, result.path)}")
    print(f"next: arena run start {nn}")
    return rounds.EXIT_OK
