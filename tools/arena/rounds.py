"""tools/arena/rounds.py — AR-3: `arena run start NN` and `arena run list`.

`run start NN` is the long `python3 -m tools.contest run --ticket NN …` line in
one command. It finds the ticket (`.arena/drafts/`, then the checkout's
`epic-tasks/`, then `epic-tasks/` on the integration branch), builds a
one-commit base ref `arena-round/NN` holding it — plumbing in a temporary
index (`gitref.commit_file_on`), so the operator's checkout never moves — and
runs the old runner as a foreground child with the profile's flags (AR-2) and
the `--` passthrough. The old command does the round; arena only builds its
line and maps its exit code.

`run list` reads `<out_dir>` (the roster's, never a literal `contest-out`):
one row per round, legs collapsed, newest first.

Every refusal happens before anything is written and is one line through
`output.refuse`. A broken input — a failing git command, an unreadable
`state.json`, a bad ini — is a refusal or a `?` row, never a traceback.

The ref lives under `refs/heads/arena-round/NN`, never `arena/NN`: this repo
has a branch `arena`, and a ref cannot be both a file and a directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from scripts.revive_round import leg_folders
from tools.contest import cli as contest_cli
from tools.contest import roster

from . import output, profile
from .gitref import GitRefError, commit_file_on, git, tree_with_file

#: The ref prefix (`arena-round/7`); the full ref is under `refs/heads/`.
REF_PREFIX = "arena-round/"

#: The ticket folder in the checkout and on the integration branch.
TASKS_DIR = contest_cli.TASKS_DIR

#: `07-x.md` is round 7 — the same match as `gates.ticket_for_round`.
_TICKET_RE = re.compile(r"^0*(\d+)-.*\.md$")
_STATUS_LINE = re.compile(r"^\*\*Status:\*\*.*$", re.MULTILINE)
#: A round folder: `06` or `06.2`.
_ROUND_DIR = re.compile(r"^(\d+)(?:\.(\d+))?$")

#: Flags arena sets itself; the passthrough may never carry them.
_OWNED_FLAGS = ("--ticket", "--base", "--target", "--out")

#: The `/proc` `run start` and `run list` read — a seam, so tests use a fake tree.
PROC_ROOT = "/proc"

#: The command that starts the child — a seam, so tests run a stub child.
SPAWN: Callable[..., subprocess.Popen] = subprocess.Popen

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_NOTHING, EXIT_NO_READY = 0, 1, 2, 3, 4


class RoundError(Exception):
    """A `run start` refusal; the message is the one line the CLI prints."""


# ── the roster's out_dir and the round folders ───────────────────────────────
def load_config(repo: Path):
    """The roster `tools.contest run` reads, for its `out_dir`. `RoundError` on a bad ini."""
    # the same default `cmd_run` sets: the committed ${CONTEST_GATE_API_KEY}
    # reference must resolve for `load_roster` to read the file at all
    os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-round")
    try:
        return roster.load_roster(Path(repo) / "contest.ini")
    except roster.RosterError as err:
        raise RoundError(f"contest.ini: {err}") from err


def round_folder(repo: Path, config, nn: int, leg: Optional[int] = None) -> Path:
    """`<out_dir>/NN`, or the leg folder `NN.K`.

    A bare *nn* is its highest leg when legs exist and `NN/state.json` does not.
    """
    base = contest_cli._round_out_dir(Path(repo), config, nn)
    if leg is not None:
        return contest_cli._leg_out_dir(base, leg)
    legs = leg_folders(base)
    if legs and not (base / "state.json").is_file():
        return legs[-1]
    return base


def _has_state(repo: Path, config, nn: int) -> bool:
    """True when `NN/` or any `NN.K/` holds a `state.json`."""
    base = contest_cli._round_out_dir(Path(repo), config, nn)
    return any((f / "state.json").is_file() for f in [base, *leg_folders(base)])


# ── is round NN running? ─────────────────────────────────────────────────────
def _cmdline(proc_root: Path, pid: str) -> Optional[list[str]]:
    try:
        raw = (proc_root / pid / "cmdline").read_bytes()
    except OSError:
        return None
    return [w.decode("utf-8", "replace") for w in raw.split(b"\0") if w]


def _matches(words: Optional[list[str]], nn: int) -> bool:
    """`tools.contest`, `run` and `--ticket NN` / `--ticket=NN` (`07` is 7)."""
    if not words or "tools.contest" not in words or "run" not in words:
        return False
    for i, word in enumerate(words):
        value = None
        if word == "--ticket" and i + 1 < len(words):
            value = words[i + 1]
        elif word.startswith("--ticket="):
            value = word.split("=", 1)[1]
        if value is not None and value.isdigit() and int(value) == nn:
            return True
    return False


def round_alive(repo: Path, nn: int, proc_root: str = "/proc") -> bool:
    """True when a `tools.contest run --ticket NN` runs in *repo*, or the lock names one.

    A pid whose `cwd` or `cmdline` cannot be read does not match.
    """
    root = Path(proc_root)
    repo = Path(repo).resolve()
    try:
        pids = [p.name for p in root.iterdir() if p.name.isdigit()]
    except OSError:
        pids = []
    for pid in pids:
        if not _matches(_cmdline(root, pid), nn):
            continue
        try:
            cwd = Path(os.readlink(root / pid / "cwd")).resolve()
        except OSError:
            continue
        if cwd == repo:
            return True
    lock = repo / ".arena" / "locks" / f"{nn}.pid"
    try:
        pid = lock.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return pid.isdigit() and _matches(_cmdline(root, pid), nn)


# ── finding the ticket and the branch ────────────────────────────────────────
def _numbered(names: list[str], nn: int) -> list[str]:
    out = []
    for name in names:
        match = _TICKET_RE.match(name)
        if match and int(match.group(1)) == nn and not name.endswith(".rejected.md"):
            out.append(name)
    return sorted(out)


def find_ticket(repo: Path, nn: int, branch: str) -> tuple[str, str]:
    """`(file name, text)` of ticket *nn*: drafts, then the checkout, then *branch*."""
    repo = Path(repo)
    for folder in (repo / ".arena" / "drafts", repo / TASKS_DIR):
        names = [p.name for p in folder.glob("*.md")] if folder.is_dir() else []
        found = _numbered(names, nn)
        if len(found) > 1:
            rel = folder.relative_to(repo).as_posix()
            raise RoundError(f"more than one ticket {nn} in {rel}/: {', '.join(found)}")
        if found:
            return found[0], (folder / found[0]).read_text(encoding="utf-8")
    listed = git(repo, "ls-tree", "--name-only", branch, TASKS_DIR + "/")
    found = _numbered([line.rsplit("/", 1)[-1] for line in listed.splitlines()], nn)
    if len(found) > 1:
        raise RoundError(f"more than one ticket {nn} in {TASKS_DIR}/ on {branch}: "
                         f"{', '.join(found)}")
    if not found:
        raise RoundError(f"no ticket {nn} in .arena/drafts/, {TASKS_DIR}/ or on {branch}")
    return found[0], git(repo, "show", f"{branch}:{TASKS_DIR}/{found[0]}", strip=False)


def integration_branch(repo: Path, flag: Optional[str], prof: dict[str, str]) -> str:
    """`--branch`, else the profile's `branch`, else the checked-out branch."""
    branch = flag or (prof.get("branch") or "").strip()
    if not branch:
        try:
            branch = git(repo, "symbolic-ref", "--short", "-q", "HEAD")
        except GitRefError:
            branch = ""
        if not branch:
            raise RoundError("HEAD is detached — pass --branch or set the profile's branch")
    try:
        git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")
    except GitRefError:
        raise RoundError(f"branch {branch!r} does not exist") from None
    return branch


def _dirty_tasks(repo: Path) -> list[str]:
    """Untracked or modified files under the checkout's `epic-tasks/`."""
    listed = git(repo, "status", "--porcelain", "--untracked-files=all", "--", TASKS_DIR + "/")
    return [line[3:] for line in listed.splitlines() if line.strip()]


def open_status(text: str) -> str:
    """*text* with its `**Status:**` line forced to `open`."""
    return _STATUS_LINE.sub("**Status:** open", text, count=1)


# ── the base ref ─────────────────────────────────────────────────────────────
def _rev(repo: Path, spec: str) -> Optional[str]:
    try:
        return git(repo, "rev-parse", "--verify", "-q", spec)
    except GitRefError:
        return None


def build_round_ref(repo: Path, nn: int, branch: str, name: str, content: str, *,
                    fresh: bool = False, state_exists: bool = False,
                    yes: bool = False) -> str:
    """Point `arena-round/NN` at the integration tip plus the ticket; its sha.

    Reuses an equal ref; refuses a different one unless *fresh* (and, when a
    round folder holds a `state.json`, *yes*).
    """
    repo = Path(repo)
    ref = f"refs/heads/{REF_PREFIX}{nn}"
    tip = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")
    path = f"{TASKS_DIR}/{name}"
    on_tip = _rev(repo, f"{tip}:{path}")
    tip_holds = on_tip is not None and on_tip == git(repo, "hash-object", "--stdin",
                                                     stdin=content)

    existing = _rev(repo, ref)
    if existing is not None:
        if tip_holds:
            same = existing == tip
        else:
            same = (_rev(repo, f"{existing}^") == tip
                    and git(repo, "rev-parse", f"{existing}^{{tree}}")
                    == tree_with_file(repo, tip, path, content))
        if same:
            return existing
        if not fresh:
            raise RoundError(f"{REF_PREFIX}{nn} holds another ticket text or an older "
                             f"tip of {branch} — pass --fresh-ticket to rebuild it")
        if state_exists and not yes:
            raise RoundError(f"round {nn} has a state.json — --fresh-ticket rewrites "
                             f"{REF_PREFIX}{nn} under it only with -y")
    if tip_holds:
        git(repo, "update-ref", ref, tip)
        return tip
    return commit_file_on(repo, tip, path, content, f"{nn}: ticket for the round", ref)


# ── the run line and the child ───────────────────────────────────────────────
def _owned(word: str) -> Optional[str]:
    flag = word.split("=", 1)[0]
    return flag if flag in _OWNED_FLAGS else None


def build_run_line(nn: int, prof: dict[str, str], passthrough: list[str]) -> list[str]:
    """The child's argv. `RoundError` for an arena-owned flag in *passthrough*."""
    for word in passthrough:
        flag = _owned(word)
        if flag:
            raise RoundError(f"{flag} is set by arena, not after --")
    return [sys.executable, "-m", "tools.contest", "run", "--ticket", str(nn),
            "--base", f"{REF_PREFIX}{nn}", *profile.profile_flags(prof), *passthrough]


def _map_exit(code: int, state: Path, started: float) -> int:
    if code in (EXIT_OK, EXIT_FAILED):
        return code
    if code == 2:
        try:
            ran = state.stat().st_mtime >= started
        except OSError:
            ran = False
        return EXIT_NO_READY if ran else EXIT_FAILED
    return EXIT_FAILED


def run_start(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena run start NN`: refusals first, then the ref, the record and the child."""
    repo = Path(repo)
    nn = args.ticket
    try:
        line = build_run_line(nn, prof, args.passthrough)
        config = load_config(repo)
        branch = integration_branch(repo, args.branch, prof)
        if round_alive(repo, nn, PROC_ROOT):
            raise RoundError(f"round {nn} is already running in {repo}")
        dirty = _dirty_tasks(repo)
        if dirty:
            raise RoundError(f"{TASKS_DIR}/ has uncommitted files: {', '.join(dirty)} — "
                             "move drafts to .arena/drafts/")
        name, text = find_ticket(repo, nn, branch)
        content = open_status(text)
        sha = build_round_ref(repo, nn, branch, name, content, fresh=args.fresh_ticket,
                              state_exists=_has_state(repo, config, nn), yes=args.yes)
    except (RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return output.refuse(str(err))

    record = {
        "branch": branch,
        "base_ref": f"{REF_PREFIX}{nn}",
        "base_sha": sha,
        "ticket_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    rounds_dir = repo / ".arena" / "rounds"
    rounds_dir.mkdir(parents=True, exist_ok=True)
    (rounds_dir / f"{nn}.json").write_text(json.dumps(record, indent=2) + "\n",
                                           encoding="utf-8")

    print(output.scrub(" ".join(line)), flush=True)
    lock = repo / ".arena" / "locks" / f"{nn}.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    # one second of slack: some filesystems keep mtimes in whole seconds
    started = time.time() - 1
    try:
        child = SPAWN(line, cwd=str(repo))
    except OSError as err:
        return output.refuse(f"cannot start the runner: {err}")
    try:
        lock.write_text(f"{child.pid}\n", encoding="utf-8")
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                # Ctrl-C reached the child too (same process group): wait it out
                continue
    finally:
        lock.unlink(missing_ok=True)
    return _map_exit(code, round_folder(repo, config, nn) / "state.json", started)


# ── run list ─────────────────────────────────────────────────────────────────
def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def list_rows(repo: Path, config, now: Optional[float] = None,
              proc_root: Optional[str] = None) -> list[dict]:
    """One row per round in `<out_dir>`, newest first."""
    repo = Path(repo)
    out = repo / config.out_dir
    now = time.time() if now is None else now
    groups: dict[int, list[tuple[int, Path]]] = {}
    if out.is_dir():
        for entry in out.iterdir():
            match = _ROUND_DIR.match(entry.name)
            if match and entry.is_dir():
                leg = int(match.group(2)) if match.group(2) else 0
                groups.setdefault(int(match.group(1)), []).append((leg, entry))
    rows = []
    for nn, folders in groups.items():
        legs = sorted(f for f in folders if f[0])
        last = round_folder(repo, config, nn)
        state = last / "state.json"
        try:
            mtime = state.stat().st_mtime
        except OSError:
            mtime = last.stat().st_mtime
        data = None
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            agents = data["agents"]
            ready = sum(1 for a in agents if a.get("state") == "READY")
            counts = f"{ready}/{len(agents)}"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            data, counts = None, ""
        planned = data.get("legs") if isinstance(data, dict) else None
        if round_alive(repo, nn, proc_root or PROC_ROOT):
            status = "running"
        else:
            status = "done" if data is not None else "?"
        rows.append({
            "RUN": nn,
            "LEGS": f"{len(legs)}/{planned or len(legs)}" if legs else "",
            "STATE": status,
            "AGE": _age(now - mtime),
            "READY/TOTAL": counts,
            "_mtime": mtime,
        })
    rows.sort(key=lambda r: r["_mtime"], reverse=True)
    for row in rows:
        del row["_mtime"]
    return rows


LIST_COLUMNS = ["RUN", "LEGS", "STATE", "AGE", "READY/TOTAL"]


def run_list(repo: Path, args: argparse.Namespace) -> int:
    """`arena run list`: the table, or exit 3 with one stderr line for no rounds."""
    try:
        config = load_config(repo)
    except RoundError as err:
        return output.refuse(str(err))
    rows = list_rows(repo, config)
    if not rows:
        print(f"arena: no rounds in {config.out_dir}/", file=sys.stderr)
        return EXIT_NOTHING
    output.emit(rows, LIST_COLUMNS, args.output)
    return EXIT_OK
