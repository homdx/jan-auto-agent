#!/usr/bin/env python3
"""scripts/ticket_status.py — flip one epic-tasks/NN-*.md ticket's
**Status:** line and commit that single file.

The same edit `tools/contest/cli.py`'s `_park_line` makes by hand for the
"queued" direction, generalized to all three words plus a read-only "show".

Usage:
    ticket_status.py show   NN
    ticket_status.py open   NN [--note TEXT]
    ticket_status.py queued NN [--note TEXT]
    ticket_status.py landed NN --sha SHA [--note TEXT]
    ticket_status.py round  NN [--base kc]

    round   — the round branch in one go: `git switch -c kc-9-48-round <base>`
              (the ticket's id lower-cased, its number, `-round`), then `open`
              on it. Run the round from that branch; nothing is pushed.

    NN is the ticket number, as in epic-tasks/NN-*.md (leading zeros optional).

    --note replaces everything after the status word on that one line
    (e.g. "round 69 starts now", or for landed just the winner/summary —
    --sha is prefixed on automatically, in backticks, matching every other
    landed row in epic-tasks/). Without --note, whatever text already
    followed the word is left as is.

`open`   — the ticket intake()/next_task.py will pick up next.
`queued` — parks a ticket, the same word intake prints as one of the "two
           ways past it" when a lower-numbered ticket is still open.
`landed` — after a winner is merged; --sha is required.
`show`   — prints the current status line only, writes and commits nothing.

The ticket's row in epic-tasks/INDEX.md gets the same first word, so the
two never disagree. Only the **Status:** line is touched (found by regex, not a line number, so
it is robust to edits anywhere else in the file); the file is then
`git add`ed and committed on its own, so intake's "epic-tasks/ is committed
at the base" check passes. --no-commit edits without committing, to review
the diff first.
"""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

STATUS_RE = re.compile(r"^(\*\*Status:\*\*\s*)(\S+)(.*)$", re.MULTILINE)
TICKET_RE = re.compile(r"^0*(\d+)-.*\.md$")
#: `| 48 | `KC-9` | queued — …` → the status word in the third cell.
INDEX_ROW = r"^(\|\s*0*{n}\s*\|[^|\n]*\|\s*)(\S+)"
VALID_WRITE = ("open", "queued", "landed")


def find_ticket(tasks_dir: Path, number: int) -> Path:
    for path in sorted(tasks_dir.glob("*.md")):
        match = TICKET_RE.match(path.name)
        if match and int(match.group(1)) == number:
            return path
    raise SystemExit(f"no ticket numbered {number} in {tasks_dir}")


def title_id(text: str, fallback: str) -> str:
    match = re.search(r"^#\s*([A-Za-z0-9_.-]+)", text, re.MULTILINE)
    return match.group(1) if match else fallback


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ticket_status.py",
        description="Flip one epic-tasks ticket's **Status:** word and commit it.",
    )
    parser.add_argument("status", choices=VALID_WRITE + ("show", "round"),
                        help="open | queued | landed | show | round")
    parser.add_argument("number", type=int, metavar="NN",
                        help="the ticket number, as in epic-tasks/NN-*.md")
    parser.add_argument("--note", default=None,
                        help="replaces the text after the status word on that "
                             "line; default: keep what was already there")
    parser.add_argument("--sha", default=None,
                        help="required with 'landed'; put first, in backticks, "
                             "before --note")
    parser.add_argument("--tasks-dir", default="epic-tasks",
                        help="default: epic-tasks, relative to --repo")
    parser.add_argument("--repo", default=".",
                        help="repo root (default: current directory)")
    parser.add_argument("--base", default="kc",
                        help="round: the ref the round branch starts from (default: kc)")
    parser.add_argument("--no-commit", action="store_true",
                        help="edit the file but skip git add/commit")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    tasks_dir = repo / args.tasks_dir
    path = find_ticket(tasks_dir, args.number)
    text = path.read_text()
    match = STATUS_RE.search(text)
    if not match:
        raise SystemExit(f"{path}: no **Status:** line found")

    if args.status == "show":
        print(f"{path.relative_to(repo)}: {match.group(0)}")
        return 0

    if args.status == "round":
        branch = f"{title_id(text, 'ticket').lower()}-{args.number}-round"
        git("switch", "-c", branch, args.base, cwd=repo)
        print(f"switched to a new branch {branch} at {args.base}")
        args.status = "open"
        text = path.read_text()
        match = STATUS_RE.search(text)

    if args.status == "landed" and not args.sha:
        parser.error("landed needs --sha")

    note = args.note
    if args.status == "landed":
        note = f"`{args.sha}`" + (f" — {note}" if note else "")

    if note is None:
        rest = match.group(3)
    elif note.startswith((" —", " -")):
        rest = note
    else:
        rest = f" — {note}"
    new_line = f"{match.group(1)}{args.status}{rest}"
    path.write_text(text[:match.start()] + new_line + text[match.end():])

    rel = path.relative_to(repo)
    print(f"{rel}: {new_line}")
    paths = [str(rel)]
    index = tasks_dir / "INDEX.md"
    if index.exists():
        rows = index.read_text()
        row_re = re.compile(INDEX_ROW.format(n=args.number), re.MULTILINE)
        if row_re.search(rows):
            index.write_text(row_re.sub(lambda m: m.group(1) + args.status, rows, count=1))
            paths.append(str(index.relative_to(repo)))
            print(f"{paths[-1]}: row {args.number} -> {args.status}")

    if args.no_commit:
        print("(edited only — --no-commit, not committed)")
        return 0

    git("add", "--", *paths, cwd=repo)
    label = title_id(text, path.name)
    message = f"{args.tasks_dir}: {label} ({args.number}) {args.status}"
    if args.status == "open":
        message += f" — round {args.number}"
    git("commit", "-m", message, "--", *paths, cwd=repo)
    print(f"committed: {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
