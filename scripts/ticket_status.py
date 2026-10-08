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
INDEX_ROW = r"^(\|\s*0*{n}\s*\|[^|\n]*\|\s*)([^|\n]*?)(\s*\|)"
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


CYCLE = """the round cycle, every step prints the next one:
  show NN            what the ticket says now
  round NN           branch <id>-NN-round off the current branch, ticket + INDEX open, commit
  (run the check and the round it prints)
  landed NN --sha S  on the base branch, after the winner is merged; prints push + next tickets
  queued MM          park a lower ticket intake refuses over
"""


def ticket_statuses(tasks_dir: Path) -> dict:
    """{number: status word} for every NN-*.md in *tasks_dir*."""
    out = {}
    for path in sorted(tasks_dir.glob("*.md")):
        m = TICKET_RE.match(path.name)
        st = STATUS_RE.search(path.read_text()) if m else None
        if m:
            out[int(m.group(1))] = st.group(2).strip("`*").lower() if st else ""
    return out


def remote_of(repo: Path, branch: str) -> str:
    """The remote *branch* pushes to; the only remote, or `origin` as last guess."""
    try:
        return git("config", f"branch.{branch}.remote", cwd=repo)
    except subprocess.CalledProcessError:
        remotes = git("remote", cwd=repo).split()
        return remotes[0] if len(remotes) == 1 else "origin"


def next_steps(status: str, number: int, repo: Path, tasks_dir: Path, base: str) -> str:
    """What the operator runs next, with this repo's own values filled in."""
    n = number
    branch = git("branch", "--show-current", cwd=repo)
    rel = tasks_dir.relative_to(repo)
    me = Path(__file__).resolve().relative_to(repo) if Path(__file__).resolve().is_relative_to(repo) \
        else Path(__file__).resolve()
    remote = remote_of(repo, base)
    statuses = ticket_statuses(tasks_dir)
    check = f"python3 scripts/next_task.py --tasks {rel}/ --progress /dev/null"
    if status == "open":
        lower = [k for k, v in statuses.items() if k < n and v not in ("landed", "queued")]
        lines = [f"", f"next, on {branch}:",
                 f"  1. check (no server) which ticket the agents get:", f"       {check}"]
        if lower:
            lines += [f"     lower tickets still on offer, intake will refuse: {lower}",
                      *[f"       python3 {me} queued {k}" for k in lower]]
        lines += [f"  2. rewriting the ticket? edit {rel}/ now and commit it yourself",
                  f"  3. run the round from this branch:",
                  f"       python3 -m tools.contest run --ticket {n}",
                  f"  4. after the round: land the winner on {base}, then",
                  f'       python3 {me} landed {n} --sha <sha> --note "round {n} winner <agent>"',
                  f"     on another machine first: git switch {base} && git pull {remote} {base}"]
        return "\n".join(lines)
    if status == "landed":
        titles = {int(TICKET_RE.match(p.name).group(1)): title_id(p.read_text(), p.name)
                  for p in tasks_dir.glob("*.md") if TICKET_RE.match(p.name)}
        nxt = sorted(k for k, v in statuses.items() if v in ("queued", "open") and k != n)
        # the landed ticket's own epic first: `KC-9` → `KC-`; old epics' parked
        # tickets only when this one has nothing left
        # a ticket whose heading starts with a number (`# 150 — …`), or has no
        # heading (the file name), has no family: every ticket still to land
        m = re.match(r"[A-Za-z]+-?", titles.get(n, ""))
        family = m.group(0) if m else ""
        nxt = [k for k in nxt if titles[k].startswith(family)] or nxt
        lines = [f"", f"next, on {branch}:", f"  1. check the log, then push:",
                 f"       git push {remote} {branch}"]
        if nxt:
            lines += [f"  2. not landed yet: " + ", ".join(f"{titles[k]} ({k})" for k in nxt[:8])
                      + (" …" if len(nxt) > 8 else ""),
                      f"     pick one and open its round:",
                      f"       python3 {me} show NN",
                      f"       python3 {me} round NN"]
        return "\n".join(lines)
    return f"\nnext: re-check which ticket is on offer:\n  {check}"


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="ticket_status.py",
        description="Flip one epic-tasks ticket's **Status:** word and commit it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CYCLE,
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
    parser.add_argument("--base", default=None,
                        help="round: the ref the round branch starts from "
                             "(default: the branch you are on)")
    parser.add_argument("--no-commit", action="store_true",
                        help="edit the file but skip git add/commit")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if args.base is None:
        args.base = git("branch", "--show-current", cwd=repo) or "HEAD"
    tasks_dir = repo / args.tasks_dir
    path = find_ticket(tasks_dir, args.number)
    text = path.read_text()
    match = STATUS_RE.search(text)
    if not match:
        raise SystemExit(f"{path}: no **Status:** line found")

    if args.status == "show":
        print(f"{path.relative_to(repo)}: {match.group(0)}")
        word = match.group(2).strip("`*").lower()
        if word != "landed":
            print(f"\nnext: python3 {Path(__file__).resolve().relative_to(repo) if Path(__file__).resolve().is_relative_to(repo) else Path(__file__).resolve()} round {args.number}"
                  + ("   (already open: run the round from its branch)" if word == "open" else ""))
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
        # a landed ticket sent back to open/queued must not keep the old sha
        was_landed = match.group(2).strip("`*").lower() == "landed"
        rest = "" if was_landed and args.status != "landed" else match.group(3)
    elif args.status == "landed":
        rest = f" {note}"  # the repo's form: landed `sha` — note
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
            # the whole status cell is rewritten: only the first word used to be,
            # so a flip kept the old sha (`open `abc`` / a landed row with the
            # previous winner's sha)
            cell = f"landed `{args.sha}`" if args.status == "landed" else args.status
            index.write_text(row_re.sub(lambda m: m.group(1) + cell + m.group(3), rows, count=1))
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
    print(next_steps(args.status, args.number, repo, tasks_dir, args.base))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
