"""tools/arena/merge.py — AR-8: `arena entry merge NN AGENT` and `arena issue land NN`.

Landing a round's winner is two commits, never one: the winner as it is
(`entry merge`), then the closing commit (`issue land`). Neither pushes, calls
the LLM or switches a branch — the operator checks the branch out and a wrong
checkout is a refusal. `git commit -a` is not used: the commit takes an explicit
path list, so a dirty tree neither loses a file nor sweeps one in, and
`git apply` never gets `--3way` (a conflict would leave conflict markers and
unmerged index entries in the tree, and "the tree is unchanged" would be a lie).

`entry merge` refuses everything before it touches anything: a round still
running, the branch the round was started on, an unknown agent and any tracked
change. Only then does it apply the winner's exported file with `epic-tasks/`
and `runs/` excluded — those two hold the agent's own bookkeeping, not its
work — and commits the paths the apply staged, no more and no fewer. The message
is the patch's own subject and body with `NN: ` when the patch names no round,
plus the model's trailer, and the commit is made under the checkout's own
identity: the patch's `From:` is never the author. `.arena/merged.json` records
who was merged and at what sha, so `issue land` can name the winner.

`issue land` writes `landed` on the ticket's `**Status:**` line only, gives
`epic-tasks/INDEX.md`'s row the same word, stages `contest-bench/NN/` and the
operator's own follow-up edits, and commits those and nothing else. It prints
the next commands and does not run them.

Every refusal is one line through `output.refuse` — exit 2, no traceback — and
everything printed goes through `output.scrub`. A missing or malformed
`entrants.json`, `state.json` or merged record degrades to "no such agent" or
"no record", never to an exception in a run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from dataclasses import dataclass
from email import policy
from email.parser import Parser
from pathlib import Path
from typing import Optional

from scripts.ticket_status import INDEX_ROW, STATUS_RE, TICKET_RE, remote_of
from tools.contest import cli as contest_cli

from . import output, rounds, tickets
from .gitref import GitRefError, git, read_utf8

EXIT_OK, EXIT_FAILED, EXIT_USAGE = rounds.EXIT_OK, rounds.EXIT_FAILED, rounds.EXIT_USAGE

#: The ticket folder, as `arena run start` writes it.
TASKS_DIR = rounds.TASKS_DIR
#: The index `issue land` keeps in step with the tickets' own status lines.
INDEX_FILE = f"{TASKS_DIR}/INDEX.md"
#: The word `issue land` writes: the one `ticket_status.py`'s `STATUS_RE` reads.
LANDED_WORD = "landed"
#: Where a merge is recorded, so `issue land` can name the winner and its sha.
MERGED_RECORD = ".arena/merged.json"
#: The bench folder a round's acceptance bench lands in.
BENCH_DIR = "contest-bench"

#: `tools/contest/export.write_entrants` names a file after the agent and, when the
#: exporter had to put a terminal state in the name, after that state too:
#: `glm-4-7.patch`, `glm-4-7.STALLED.diff`, `glm-4-7.ERROR.patch`.
_ENTRY_NAME = re.compile(r"^(?P<agent>.+?)\.(?:(?P<state>GAVE_UP|STALLED|ERROR)\.)?"
                         r"(?P<kind>patch|diff)$")
#: A subject that already names a round or a ticket; `NN: ` is prepended otherwise.
_SUBJECT_PREFIX = re.compile(r"^\d+:|^AR-\d+:")
#: `git format-patch`'s own bracket, dropped the way `git mailinfo` drops it.
_PATCH_BRACKET = re.compile(r"^\[PATCH(?:\s+\d+/\d+)?\]\s*", re.IGNORECASE)
#: Where a commit message ends and `format-patch`'s diffstat begins.
_DIFFSTAT = re.compile(r"^(?:---$|diff --git |Binary files |index [0-9a-f]+)")
#: `git mailsplit`'s mail boundary: the mbox `From ` line, not the mail's `From:`.
_MBOX_FROM = re.compile(r"^From [0-9a-fA-F]{7,40} .*\n", re.MULTILINE)
#: The two prefixes a winner's patch carries but never lands.
EXCLUDES = ("epic-tasks", "runs")


class MergeError(Exception):
    """A refusal; the message is the one line the CLI prints through `refuse`."""


# ── the git facts both verbs need ─────────────────────────────────────────────
def _git_quiet(repo: Path, *args: str) -> bool:
    """Whether `git <args>` succeeds — how `git diff --quiet` says a tree is clean."""
    try:
        git(repo, *args)
        return True
    except GitRefError:
        return False


def _read_json(path: Path) -> Optional[dict]:
    """*path* as a dict, or `None` when it is missing, broken or not an object."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _head_branch(repo: Path) -> str:
    """The checked-out branch, `""` on a detached HEAD or a refused read."""
    try:
        return git(repo, "symbolic-ref", "--short", "-q", "HEAD")
    except GitRefError:
        return ""


def _tracked_changes(repo: Path) -> list[str]:
    """The tracked paths the tree has changed; untracked files are not changes here.

    `strip=False`: the default `.strip()` eats the `M` line's leading space, and a
    ` M src/thing.py` then reads `rc/thing.py` — the path is at index 3 either way.
    """
    out = []
    for line in git(repo, "status", "--porcelain", strip=False).splitlines():
        if line.strip() and line[:2] != "??":
            out.append(line[3:].rsplit(" -> ", 1)[-1].strip())
    return out


def _status_lines(repo: Path, path: str) -> list[str]:
    """The porcelain lines git has for *path*: empty when it is new and untouched."""
    return [line for line in
            git(repo, "status", "--porcelain", "--untracked-files=all", "--", path,
                strip=False).splitlines() if line.strip()]


def _identity(repo: Path) -> str:
    """The checkout's `user.email`, `""` when there is none — the commit's author."""
    try:
        return git(repo, "config", "user.email")
    except GitRefError:
        return ""


def round_branch(repo: Path, nn: int, prof: dict[str, str]) -> str:
    """Round *nn*'s branch: the record's `branch`, else the profile's, else ``.

    A round the old command started has no `.arena/rounds/NN.json`, so then the
    profile's `branch` is the only thing to check against; with neither there is no
    branch to be wrong about and no check at all.
    """
    record = _read_json(repo / ".arena" / "rounds" / f"{nn}.json")
    branch = record.get("branch") if record else None
    if isinstance(branch, str) and branch.strip():
        return branch.strip()
    return (prof.get("branch") or "").strip()


def _branch_refusal(repo: Path, nn: int, prof: dict[str, str]) -> Optional[str]:
    """Why the checkout is not on round *nn*'s branch, `None` when there is none."""
    expected = round_branch(repo, nn, prof)
    if not expected:
        return None
    current = _head_branch(repo)
    if current == expected:
        return None
    return (f"round {nn} runs on {expected!r}, not "
            f"{current or 'HEAD (detached)'} — check the branch out first")


def _dirty_refusal(repo: Path) -> Optional[str]:
    """Why the tree is not clean, `None` when it is.

    `git diff --quiet` for the worktree and `git diff --cached --quiet` for the
    index: the two a merge is refused on, and the two `issue land` allows past.
    """
    if not _git_quiet(repo, "diff", "--quiet") or not _git_quiet(repo, "diff", "--cached", "--quiet"):
        paths = _tracked_changes(repo)
        shown = ", ".join(paths[:5]) + (f" … (+{len(paths) - 5} more)" if len(paths) > 5 else "")
        return f"the working tree has changes: {shown or 'see git status'} — " \
               "commit or restore them first"
    return None


def _warn_ahead(repo: Path, folder: Path) -> None:
    """One line when HEAD has moved past the round's base — a warning, not a refusal."""
    base = (_read_json(folder / "entrants.json") or {}).get("base")
    if not isinstance(base, str) or not base.strip():
        return
    try:
        ahead = int(git(repo, "rev-list", "--count", f"{base}..HEAD"))
    except (GitRefError, ValueError):
        return
    if ahead > 0:
        plural = "s" if ahead != 1 else ""
        print(output.scrub(f"warning: HEAD is {ahead} commit{plural} past the round's base"))


# ── the exported entry ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Entry:
    """One entrant's exported file, resolved through `duplicate_of` for the patch only.

    `agent` is the agent asked for — the commit is that agent's and the trailer is
    its model's, whatever `duplicate_of` pointed at. `state` is the terminal state
    the exporter put in the file name (`None` for a `READY`), `is_diff` the
    uncommitted `.diff` in place of a `format-patch`, and `untracked` the names that
    diff's trailing comment says are not in it.
    """

    agent: str
    path: Path
    state: Optional[str]
    is_diff: bool
    untracked: list[str]


def _entry_name(source: str) -> Optional[tuple[str, str]]:
    """*source*'s `(kind, state)` from its file name, or `None` on a name it does not own."""
    match = _ENTRY_NAME.match(Path(str(source)).name)
    return (match.group("kind"), match.group("state")) if match else None


def _untracked_names(text: str) -> list[str]:
    """The names a `.diff`'s trailing `Untracked files:` comment lists."""
    lines = [l for l in text.splitlines() if l.strip()]
    last = lines[-1] if lines else ""
    marker = "Untracked files:"
    return last[len(marker):].split() if last.startswith(marker) else []


def read_entry(folder: Path, agent: str) -> Optional[Entry]:
    """*agent*'s entry in *folder*'s `entrants.json`, or `None`.

    `None` is every refusal reason and needs no telling apart: no file or a file that
    is not an object, no such agent, a `duplicate_of` that names nothing or loops, and
    a `source` the folder does not hold. A `duplicate_of` entry carries no `source`
    of its own, so it is followed to the agent it names — the same patch text.
    """
    data = _read_json(folder / "entrants.json")
    if data is None:
        return None
    entrants = data.get("entrants")
    if not isinstance(entrants, dict) or agent not in entrants:
        return None
    seen: set[str] = set()
    current = agent
    while isinstance(entrants.get(current), dict) and "duplicate_of" in entrants[current]:
        target = entrants[current]["duplicate_of"]
        if not isinstance(target, str) or not target or target in seen:
            return None
        seen.add(current)
        current = target
    entry = entrants.get(current)
    if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
        return None
    path = folder / Path(entry["source"]).name
    if not path.is_file():
        return None
    named = _entry_name(entry["source"])
    if named is None:
        return None
    kind, state = named
    try:
        text = read_utf8(path)
    except OSError:
        text = ""
    return Entry(agent=agent, path=path, state=state, is_diff=kind == "diff",
                 untracked=_untracked_names(text))


# ── the trailer's model ───────────────────────────────────────────────────────
def _model_id_of(folder: Path, agent: str) -> Optional[str]:
    """*agent*'s `model_id` from *folder*'s `state.json`, `None` when it is not there.

    *folder* is the round's last leg, the one whose `state.json` holds the roster the
    agent ran under. A malformed file or entry is `None`, never an exception.
    """
    data = _read_json(folder / "state.json")
    if not data:
        return None
    for run in data.get("agents") or []:
        if not isinstance(run, dict):
            continue
        spec = run.get("agent")
        if not isinstance(spec, dict) or spec.get("name") != agent:
            continue
        model_id = spec.get("model_id")
        return model_id if isinstance(model_id, str) and model_id else None
    return None


def trailer_model(folder: Path, agent: str) -> str:
    """The trailer's model part: *agent*'s `model_id` squeezed the way
    `tools/contest/cli.agents_from_models` squeezes a name, else the agent's name.

    The squeeze is that call, not a copy of it: a `GLM-4.7` is `glm-4-7`, an
    `x/y-6.8-z:free` is a `y-6-8-z`, and the `-varN` an agent name may carry is
    absent because the id is recomputed from `model_id` — a model may itself end in
    `-var1`, so the agent's suffix is never stripped off its name.
    """
    model_id = _model_id_of(folder, agent)
    if not model_id:
        return agent
    try:
        specs = contest_cli.agents_from_models(model_id)
    except (ValueError, TypeError, OSError):
        return agent
    name = specs[0].name if specs else ""
    return name or agent


# ── the message a patch carries ───────────────────────────────────────────────
def _split_mails(raw: str) -> list[str]:
    """*raw* cut at its mbox `From ` lines — `git mailsplit`'s own mail boundary."""
    starts = [m.start() for m in _MBOX_FROM.finditer(raw)]
    if not starts:
        return [raw]
    return [raw[start:end] for start, end in zip(starts, starts[1:] + [len(raw)])]


def subject_of(mail: str) -> str:
    """A mail's `Subject:`: unfolded, RFC 2047-decoded, `[PATCH n/m]` dropped.

    The stdlib `email` parser does the unfold and the decode; a malformed mail gives
    `""` rather than an exception, and `format-patch`'s own bracket goes.
    """
    try:
        subject = Parser(policy=policy.default).parsestr(mail).get("Subject") or ""
    except (ValueError, TypeError, KeyError, IndexError):
        subject = ""
    return _PATCH_BRACKET.sub("", str(subject).strip(), count=1).strip()


def body_of(mail: str) -> str:
    """The commit message in a mail's body: the text up to the diffstat, as it is."""
    text = mail.split("\n\n", 1)[1] if "\n\n" in mail else ""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        if _DIFFSTAT.match(line):
            lines = lines[:index]
            break
    return "\n".join(lines).rstrip()


def read_patch_messages(path: Path) -> list[tuple[str, str]]:
    """The `(subject, body)` of each mail in *path*, one per commit the patch holds.

    The file is cut at its mbox `From ` lines, the way `git mailsplit` cuts it, and
    each mail's `Subject:` is decoded — `git mailinfo` is the tool that would do this
    and is not usable here: this build of git takes a `<msg>` and a separate
    `<patch>` and exits 129 on the one file `mailsplit` writes, so the decode is done
    with the stdlib parser and pinned in a test. An unreadable file is `[]`.
    """
    try:
        raw = read_utf8(path)
    except (OSError, ValueError):
        return []
    return [(subject_of(mail), body_of(mail)) for mail in _split_mails(raw) if mail.strip()]


def build_message(nn: int, entry: Entry, model: str, *, squash: bool = False) -> tuple[str, str]:
    """`(subject, message)` for one commit: the patch's own words, `NN: ` when the
    patch names no round, and the model's trailer as the last line.

    An uncommitted `.diff` carries no mail and gets `NN: <agent>'s uncommitted tree,
    taken as-is`. More than one mail is a `MergeError` naming the count unless
    *squash*, which takes the first mail's subject and body and says how many it
    folded. `MergeError` too for a file that holds no readable message.
    """
    if entry.is_diff:
        subject = f"{nn}: {entry.agent}'s uncommitted tree, taken as-is"
        body = ""
    else:
        mails = read_patch_messages(entry.path)
        if len(mails) > 1 and not squash:
            raise MergeError(f"{entry.path.name} holds {len(mails)} commits — "
                             "pass --squash to take one")
        if not mails or not mails[0][0]:
            raise MergeError(f"{entry.path.name} holds no readable commit message")
        subject, body = mails[0]
        if len(mails) > 1:
            folded = f"(squashed from {len(mails)} commits)"
            body = f"{body}\n\n{folded}" if body else folded
    if not _SUBJECT_PREFIX.match(subject):
        subject = f"{nn}: {subject}"
    trailer = f"Co-Authored-By: {model} <{model}@round-{nn}.contest>"
    message = f"{subject}\n\n"
    if body:
        message += f"{body}\n\n"
    return subject, f"{message}{trailer}\n"


# ── the apply and the commit ──────────────────────────────────────────────────
def _numstat(repo: Path, path: Path, *, exclude: bool = False) -> list[str]:
    """The paths `git apply --numstat -z` names for *path*, excludes on or off.

    `-z` separates the records with NUL but the counts stay tab-separated, so a path
    is the last field of its record — a binary file's `-` counts never change that.
    """
    args = ["apply", "--numstat", "-z"]
    if exclude:
        args += [f"--exclude={folder}/*" for folder in EXCLUDES]
    out = []
    for record in git(repo, *args, str(path), strip=False).split("\x00"):
        record = record.strip()
        if record:
            out.append(record.split("\t")[-1])
    return out


def dropped_paths(repo: Path, path: Path) -> list[str]:
    """The paths the excludes drop: named by `--numstat -z`, gone with the excludes on.

    Printed before the apply, so the operator sees the loss and can copy those files
    by hand. A patch git cannot read for its numstat drops nothing: nothing is applied.
    """
    try:
        all_paths = _numstat(repo, path)
        kept = _numstat(repo, path, exclude=True)
    except GitRefError:
        return []
    return sorted(set(all_paths) - set(kept))


def apply_patch(repo: Path, path: Path) -> tuple[bool, str]:
    """`git apply --index` of *path* with the winner's own bookkeeping excluded.

    Staged only — the commit comes later, on an explicit path list. No `--3way`: a
    conflict would leave conflict markers and unmerged index entries in the tree, and
    plain `--index` applies all or nothing, so a refusal leaves the checkout exactly
    as it was. `(False, git's own first error line)` when it will not apply.
    """
    args = ["apply", "--index", *(f"--exclude={folder}/*" for folder in EXCLUDES), str(path)]
    try:
        git(repo, *args)
        return True, ""
    except GitRefError as err:
        return False, str(err)


def _cached_paths(repo: Path) -> list[str]:
    """The paths the index has changed against HEAD — the commit's, and only those.

    `--no-renames` keeps a rename's deletion visible, so the source and the
    destination land in the commit together.
    """
    return [part for part in
            git(repo, "diff", "--cached", "--name-only", "--no-renames", "-z").split("\x00")
            if part]


def _commit(repo: Path, message: str, paths: list[str]) -> str:
    """`git commit -F <message> -- <paths>` — explicit paths only, never `-a`.

    *paths* were `git add`ed just before, so a new file is one git knows; `git commit
    -- path` would refuse one it does not. Returns the commit's sha; a refusal is a
    `MergeError` naming git's own words.
    """
    if not paths:
        raise MergeError("no paths to commit")
    fd, name = tempfile.mkstemp(prefix="arena-msg-", suffix=".txt")
    try:
        # surrogateescape keeps a latin-1 subject's bytes in the commit (Bug 174);
        # `newline=""` keeps CRLF a body carries on the branch (Bug 185).
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape",
                       newline="") as handle:
            handle.write(message)
        git(repo, "commit", "-F", name, "--", *paths)
        return git(repo, "rev-parse", "HEAD")
    except GitRefError as err:
        raise MergeError(f"git commit refused the paths: {err}") from err
    finally:
        Path(name).unlink(missing_ok=True)


# ── the merge record ──────────────────────────────────────────────────────────
def read_merged_record(repo: Path) -> dict:
    """`.arena/merged.json` as `{str(NN): {agent, sha, subject}}`; `{}` when broken."""
    data = _read_json(repo / MERGED_RECORD) or {}
    return {key: value for key, value in data.items() if isinstance(value, dict)}


def write_merged_record(repo: Path, nn: int, agent: str, sha: str, subject: str) -> None:
    """Record the merge at *nn*, keeping every other round's record.

    Written atomically through a sibling temp file, so a torn write cannot leave the
    other rounds' records unreadable.
    """
    records = read_merged_record(repo)
    records[str(nn)] = {"agent": agent, "sha": sha, "subject": subject}
    target = repo / MERGED_RECORD
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp")
    tmp.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)


# ── entry merge: the winner, as it is ─────────────────────────────────────────
def entry_merge(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena entry merge NN AGENT [--uncommitted] [--squash]` — commit 1.

    The refusals all come first and each is one line through `refuse`: a round still
    running, the branch the round was started on, an unknown agent, an entry whose
    state or file name the flags did not name, a tree with tracked changes, and a
    checkout with no identity to commit as. Only then is anything applied, and the
    commit takes exactly the paths the apply staged. Exit 1 when git will not apply
    the patch, the tree left as it was; 2 for a refusal.
    """
    repo = Path(repo)
    yes = bool(getattr(args, "yes", False))
    squash = bool(getattr(args, "squash", False))
    uncommitted = bool(getattr(args, "uncommitted", False))
    try:
        raw = str(args.number)
        if not raw.isdigit():
            raise MergeError(f"entry merge: {raw!r} is not a round number")
        nn = int(raw)
        agent = str(args.agent)
        config = rounds.load_config(repo)
        folder = rounds.round_folder(repo, config, nn)
        if rounds.round_alive(repo, nn, rounds.PROC_ROOT):
            raise MergeError(f"round {nn} is running in {repo}")
        if (why := _branch_refusal(repo, nn, prof)) is not None:
            raise MergeError(why)
        entry = read_entry(folder, agent)
        if entry is None:
            raise MergeError(f"no agent {agent!r} in {folder / 'entrants.json'}")
        if entry.state and not entry.is_diff and not yes:
            raise MergeError(f"{agent} ended {entry.state} — pass -y to merge it anyway")
        if entry.is_diff and not uncommitted:
            raise MergeError(f"{entry.path.name} is an uncommitted tree — pass --uncommitted")
        if not _identity(repo):
            raise MergeError("no git identity — set user.name and user.email first")
        if (why := _dirty_refusal(repo)) is not None:
            raise MergeError(why)
        _warn_ahead(repo, folder)
    except (MergeError, GitRefError, rounds.RoundError, ValueError, OSError) as err:
        return output.refuse(str(err))

    try:
        subject, message = build_message(nn, entry, trailer_model(folder, agent), squash=squash)
        for path in dropped_paths(repo, entry.path):
            print(output.scrub(f"dropped: {path}"))
        for name in entry.untracked:
            print(output.scrub(f"{name} — not in the diff, copy it by hand"))
        applied, error = apply_patch(repo, entry.path)
        if not applied:
            print(output.scrub(error))
            print(output.scrub(
                "the integration branch moved since the round — "
                f"apply by hand with `git am -3 {entry.path}`"))
            return EXIT_FAILED
        paths = _cached_paths(repo)
        if not paths:
            raise MergeError(
                "the patch leaves nothing to commit — every path it touches is excluded")
        sha = _commit(repo, message, paths)
    except (MergeError, GitRefError) as err:
        return output.refuse(str(err))

    try:
        write_merged_record(repo, nn, agent, sha, subject)
    except OSError as err:
        print(output.scrub(f"warning: could not write {MERGED_RECORD}: {err}"))
    print(output.scrub(f"merged {sha[:7]} — fix it now if needed, then: arena issue land {nn}"))
    return EXIT_OK


# ── issue land: the closing commit ────────────────────────────────────────────
def _numbered(names: list[str], nn: int) -> list[str]:
    """The *names* that are round *nn*'s ticket, matched by ticket_status.py's `TICKET_RE`."""
    found = []
    for name in names:
        match = TICKET_RE.match(name)
        if match and int(match.group(1)) == nn and not name.endswith(".rejected.md"):
            found.append(name)
    return sorted(set(found))


def read_ticket(repo: Path, nn: int) -> Optional[tuple[str, str]]:
    """`(name, text)` of round *nn*'s ticket, or `None`.

    The round's own ref first — `git show arena-round/NN:epic-tasks/<name>`, the text
    the agents were handed — then the checkout's `epic-tasks/`, then `.arena/drafts/`.
    `None` covers every way there is no single ticket: no ref, no file, and more than
    one in a folder, which is the same refusal.
    """
    ref = f"{rounds.REF_PREFIX}{nn}"
    try:
        names = git(repo, "ls-tree", "--name-only", f"{ref}:{TASKS_DIR}/").splitlines()
    except GitRefError:
        names = []
    found = _numbered(names, nn)
    if len(found) == 1:
        return found[0], git(repo, "show", f"{ref}:{TASKS_DIR}/{found[0]}", strip=False)
    for folder in (repo / TASKS_DIR, repo / ".arena" / "drafts"):
        names = [path.name for path in folder.glob("*.md")] if folder.is_dir() else []
        found = _numbered(names, nn)
        if len(found) == 1:
            return found[0], read_utf8(folder / found[0])
    return None


def _ready_counts(folder: Path) -> Optional[tuple[int, int]]:
    """`(READY, TOTAL)` from *folder*'s `state.json`, `None` when it holds no agents."""
    data = _read_json(folder / "state.json")
    if not data:
        return None
    agents = data.get("agents")
    if not isinstance(agents, list) or not agents:
        return None
    ready = sum(1 for agent in agents
                if isinstance(agent, dict) and agent.get("state") == "READY")
    return ready, len(agents)


def _extra_refusal(repo: Path, path: str) -> Optional[str]:
    """Why *path* is not the operator's follow-up edit, or `None` when it is."""
    if not path or path.startswith("-"):
        return f"{path!r} is not a path after --"
    if not (repo / path).exists():
        return f"{path}: no such file"
    if not _status_lines(repo, path):
        return f"{path}: no change to commit"
    return None


def _bench_dir(repo: Path, nn: int) -> Optional[Path]:
    """`contest-bench/NN/`, holding the round's acceptance bench, or `None`."""
    for name in (str(nn), f"{nn:02d}"):
        candidate = repo / BENCH_DIR / name
        if candidate.is_dir():
            return candidate
    return None


def _bench_paths(repo: Path, bench: Path) -> list[str]:
    """The *bench* files git tracks or would track: the `.gitignore`d ones stay out.

    `--untracked-files=all` lists the new files one per line, so `__pycache__` never
    shows up here. Read-only — the `git add` is `issue_land`'s, after every refusal,
    so a refused land leaves the bench untracked and the checkout exactly as it was.
    """
    return sorted(line[3:].rsplit(" -> ", 1)[-1].strip()
                  for line in git(repo, "status", "--porcelain", "--untracked-files=all",
                                  "--", bench.as_posix(), strip=False).splitlines()
                  if line.strip())


def _landed_word_of(text: str) -> str:
    """The word the ticket's `**Status:**` line holds, through ticket_status.py's `STATUS_RE`.

    The INDEX row gets the word the ticket says, so the two can never be typed apart.
    """
    match = STATUS_RE.search(text or "")
    return match.group(2).strip("`*").lower() if match else LANDED_WORD


def update_index_row(rows: str, nn: int, word: str) -> Optional[str]:
    """*rows* with round *nn*'s row's status cell set to *word*, `None` without one.

    `INDEX_ROW` comes from `scripts/ticket_status.py`, so arena and the old script
    agree on what a row is; only that one cell is rewritten and the rest of the line,
    and every other line, stays byte for byte.
    """
    match = re.search(INDEX_ROW.format(n=nn), rows or "", re.MULTILINE)
    if match is None:
        return None
    return rows[:match.start(2)] + word + rows[match.end(2):]


def landed_note(nn: int, record: Optional[dict], score: str = "",
                follow_up: bool = False) -> str:
    """The words after `landed` on the ticket's `**Status:**` line:
    `— round NN, winner <agent> (<score>), <sha7> as-is`.

    The parenthesis is there only when a `--score` was given — READY/TOTAL is not the
    score, so round 131's line reads `28/28 on contest-bench/131` without any other
    bracket — and the sha ends `+ follow-up` when the operator's own edits ride in the
    same commit. With no merge record it is just `— round NN`, the round landed
    without a winner on record.
    """
    words = [f"— round {nn}"]
    if record:
        agent = f"winner {record.get('agent') or '?'}"
        if score:
            agent += f" ({score})"
        sha = f"{str(record.get('sha') or '?')[:7]} {'+ follow-up' if follow_up else 'as-is'}"
        words += [agent, sha]
    return ", ".join(words)


def issue_land(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena issue land NN [-m SUBJECT] [--score TEXT] [--note TEXT] [-- PATH…]` — commit 2.

    The ticket's `**Status:**` line becomes `landed`, its `epic-tasks/INDEX.md` row
    gets the same word, `contest-bench/NN/` and the operator's own edits after `--`
    ride in the same commit, and nothing else is ever in it. Refused when a tracked
    change is not in that list, when a listed path has no change, when the round is
    still running, when there is no ticket and on the wrong branch. Exit 2 for each;
    the next commands are printed, not run.
    """
    repo = Path(repo)
    extras = list(getattr(args, "passthrough", None) or [])
    follow_up = bool(extras)
    try:
        raw = str(args.number)
        if not raw.isdigit():
            raise MergeError(f"issue land: {raw!r} is not a round number")
        nn = int(raw)
        config = rounds.load_config(repo)
        folder = rounds.round_folder(repo, config, nn)
        if rounds.round_alive(repo, nn, rounds.PROC_ROOT):
            raise MergeError(f"round {nn} is running in {repo}")
        if (why := _branch_refusal(repo, nn, prof)) is not None:
            raise MergeError(why)
        found = read_ticket(repo, nn)
        if found is None:
            raise MergeError(f"no ticket {nn} in {rounds.REF_PREFIX}{nn}, "
                             f"{TASKS_DIR}/ or .arena/drafts/")
        name, text = found
        ticket_rel = f"{TASKS_DIR}/{name}"
        # The text above may come from `arena-round/NN`, and the commit writes it over the
        # checkout's copy: an edit of that copy not yet committed would be lost without a word.
        if (repo / ticket_rel).is_file():
            if _status_lines(repo, ticket_rel):
                raise MergeError(f"{ticket_rel} has uncommitted changes — commit or drop them first")
            have = STATUS_RE.search(read_utf8(repo / ticket_rel))
            if have is not None and have.group(2).strip("`*").lower() == LANDED_WORD:
                raise MergeError(f"{ticket_rel} is already landed")
        for path in extras:
            if (why := _extra_refusal(repo, path)) is not None:
                raise MergeError(why)
        score = (getattr(args, "score", None) or "").strip()
        record = read_merged_record(repo).get(str(nn))
        note = landed_note(nn, record, score, follow_up)
        try:
            new_text = tickets.set_status_text(text, f"{LANDED_WORD} {note}")
        except ValueError:
            raise MergeError(f"{ticket_rel} has no **Status:** line to land")
        word = _landed_word_of(new_text)
        index_path = repo / INDEX_FILE
        new_rows = update_index_row(read_utf8(index_path), nn, word) if index_path.is_file() else None
        bench = _bench_dir(repo, nn)
        bench_paths = _bench_paths(repo, bench) if bench is not None else []
        allowed = {ticket_rel, *bench_paths, *extras}
        if new_rows is not None:
            allowed.add(INDEX_FILE)
        stray = [path for path in _tracked_changes(repo) if path not in allowed]
        if stray:
            raise MergeError(f"changes not in the commit: {', '.join(stray)} — "
                             "commit them separately or pass them after --")
    except (MergeError, GitRefError, rounds.RoundError, ValueError, OSError) as err:
        return output.refuse(str(err))

    try:
        (repo / ticket_rel).write_text(new_text, encoding="utf-8", errors="surrogateescape")
        if new_rows is not None:
            (repo / INDEX_FILE).write_text(new_rows, encoding="utf-8", errors="surrogateescape")
        paths = [ticket_rel]
        if new_rows is not None:
            paths.append(INDEX_FILE)
        paths += [*bench_paths, *extras]
        git(repo, "add", "--", *paths)
        subject = (getattr(args, "message", None) or "").strip()
        if not subject:
            subject = f"{nn}: ticket landed"
            if bench_paths:
                subject += f", and {bench.relative_to(repo).as_posix()} holds the round's acceptance bench"
        head: list[str] = []
        counts = _ready_counts(folder)
        if counts is not None:
            head.append(f"{counts[0]}/{counts[1]} READY")
        if record:
            sha7 = str(record.get("sha") or "?")[:7]
            head.append(f"winner {record.get('agent') or '?'} taken as-is in {sha7}")
        body = f"Round {nn}" + (f": {'; '.join(head)}" if head else "") + "."
        note_text = (getattr(args, "note", None) or "").strip()
        if note_text:
            body = f"{body}\n{note_text}"
        message = f"{subject}\n\n{body}"
        trailer = (prof.get("trailer") or "").strip()
        if trailer:
            message = f"{message}\n\n{trailer}"
        message += "\n"
        sha = _commit(repo, message, paths)
    except (MergeError, GitRefError) as err:
        return output.refuse(str(err))

    branch = _head_branch(repo) or round_branch(repo, nn, prof)
    print(output.scrub(f"landed {nn} @ {sha[:7]} — {', '.join(paths)}"))
    print(output.scrub("next: python3 -m pytest tests -n 8 -q"))
    print(output.scrub("then: python3 -m pytest tests_bugfix -n 8 -q"))
    print(output.scrub(f"push: git push {remote_of(repo, branch)} {branch}"))
    return EXIT_OK
