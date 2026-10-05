"""tools/arena/rounds.py — AR-3: `arena run start NN` and `arena run list`
(AR-4 `run view`, AR-5 `run rerun`).

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
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from scripts.revive_round import (BACKUP_NAME, DEAD_STATE, REVIVE, REVIVED_STATE, leg_folders,
                                  revive_agents)
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
    # AR-62: a flag both the profile and the passthrough give reaches the
    # runner once — the passthrough's, since it comes last.
    flags = profile.dedupe_flags([*profile.profile_flags(prof), *passthrough])
    return [sys.executable, "-m", "tools.contest", "run", "--ticket", str(nn),
            "--base", f"{REF_PREFIX}{nn}", *flags]


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


def _run_child(repo: Path, nn: int, line: list[str], state: Path,
               started: Optional[float] = None) -> int:
    """Print *line*, run it as the foreground child under the lock file, map its exit.

    *state* is the round's `state.json`: a runner exit of 2 is "no agent ready"
    when it rewrote that file after the start, a plain failure when it did not.
    *started* is that "after"; `run rerun` writes the file itself just before
    the start, so it passes the instant just past its own write.
    """
    print(output.scrub(" ".join(line)), flush=True)
    lock = repo / ".arena" / "locks" / f"{nn}.pid"
    lock_tmp = repo / ".arena" / "locks" / f"{nn}.pid.tmp"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if started is None:
        # one second of slack: some filesystems keep mtimes in whole seconds
        started = time.time() - 1
    lock.write_text(str(os.getpid()), encoding="utf-8")
    try:
        child = SPAWN(line, cwd=str(repo))
        lock_tmp.write_text(f"{child.pid}\n", encoding="utf-8")
        os.replace(lock_tmp, lock)
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                # Ctrl-C reached the child too (same process group): wait it out
                continue
    except OSError as err:
        lock.unlink(missing_ok=True)
        return output.refuse(f"cannot start the runner: {err}")
    finally:
        lock.unlink(missing_ok=True)
    return _map_exit(code, state, started)


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

    return _run_child(repo, nn, line, round_folder(repo, config, nn) / "state.json")


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


# ── run view ─────────────────────────────────────────────────────────────────
#: `NN` or `NN.K`, digits only; `07.2` is round 7 leg 2.
_VIEW_ARG = re.compile(r"^(\d+)(?:\.(\d+))?$")

#: The `-o json` columns, one row per agent in `state.json` order.
VIEW_COLUMNS = ["agent", "state", "attempt", "tokens", "commit"]


def _total_tokens(tokens) -> int:
    """`input + output + reasoning` as an integer; 0 for a missing or odd value."""
    if isinstance(tokens, bool):
        return 0
    if isinstance(tokens, (int, float)):
        return int(tokens)
    if not isinstance(tokens, dict):
        return 0
    total = 0
    for key in ("input", "output", "reasoning"):
        value = tokens.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += int(value)
    return total


def _agent_name(agent) -> str:
    """The agent's name: `state.json` holds its spec as a dict, older files a bare string."""
    if isinstance(agent, dict):
        return str(agent.get("name") or "")
    return "" if agent is None else str(agent)


def view_rows(data: dict) -> list[dict]:
    """One row per agent in *data* (a parsed `state.json`), in file order."""
    rows = []
    for item in data["agents"]:
        if not isinstance(item, dict):
            continue
        attempt = item.get("attempt")
        rows.append({
            "agent": _agent_name(item.get("agent")),
            "state": str(item.get("state") or ""),
            "attempt": attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0,
            "tokens": _total_tokens(item.get("tokens")),
            "commit": str(item.get("commit") or "")[:12],
        })
    return rows


def _view_header(repo: Path, nn: int, folder: Path, data: dict) -> str:
    """`round NN · leg K/N · <running|done> · base <sha7>`; no `leg` part without legs."""
    parts = [f"round {nn}"]
    match = _ROUND_DIR.match(folder.name)
    legs = leg_folders(folder.with_name(f"{nn:02d}"))
    if match and match.group(2) and legs:
        # N is the last leg's number, not the folder count: a round whose
        # `NN.1` was cleaned away still reads `leg 2/2`, never `leg 2/1`
        last = int(legs[-1].name.rpartition(".")[2])
        parts.append(f"leg {int(match.group(2))}/{last}")
    parts.append("running" if round_alive(repo, nn, PROC_ROOT) else "done")
    base = data.get("base_sha")
    parts.append(f"base {str(base)[:7] if base else '?'}")
    return " · ".join(parts)


def run_view(repo: Path, args: argparse.Namespace) -> int:
    """`arena run view NN[.K]`: one round or one leg, found the way `run list` finds it.

    Exit 1 with one line for no `state.json` or an unreadable one; a bad argument
    is a refusal. The table is `tools.contest status`'s, called in process.
    """
    match = _VIEW_ARG.match(str(args.run))
    if not match:
        return output.refuse(f"run view: {args.run!r} is not NN or NN.K")
    nn = int(match.group(1))
    leg = int(match.group(2)) if match.group(2) is not None else None
    try:
        config = load_config(repo)
    except RoundError as err:
        return output.refuse(str(err))
    folder = round_folder(repo, config, nn, leg=leg)
    state = folder / "state.json"
    if not state.is_file():
        print(output.scrub(f"arena: no round {args.run} (looked in {folder})"),
              file=sys.stderr)
        return EXIT_FAILED
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
            raise ValueError("not a round state: no agents list")
    except (OSError, ValueError) as err:
        print(output.scrub(f"arena: {state} is unreadable: {err}"), file=sys.stderr)
        return EXIT_FAILED
    if args.output == "json":
        output.emit(view_rows(data), VIEW_COLUMNS, "json")
        return EXIT_OK
    print(output.scrub(_view_header(repo, nn, folder, data)))
    sys.stdout.flush()
    return contest_cli.cmd_status(argparse.Namespace(
        ticket=nn, out=str(folder), roster=contest_cli.DEFAULT_ROSTER))


# ── run rerun ────────────────────────────────────────────────────────────────
def _drop_flag(words: list[str], flag: str, takes_value: bool) -> list[str]:
    """*words* without *flag* in its `--flag V`, `--flag=V` and bare spellings."""
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word == flag:
            skip = takes_value
            continue
        if word.startswith(flag + "="):
            continue
        out.append(word)
    return out


def build_rerun_line(nn: int, base: str, prof: dict[str, str], passthrough: list[str],
                     leg_folder: Optional[Path]) -> list[str]:
    """The child's argv for a rerun: `run start`'s line, `--resume` on the end.

    `--fresh` is dropped wherever it comes from — it would wipe the trees being
    resumed. A round of legs (*leg_folder* is its `NN.K` folder) resumes that
    leg alone, `--out <folder> --legs 1`, whatever the profile's `legs` says;
    a round without legs gets neither flag. `RoundError` for an owned flag in
    *passthrough*.
    """
    for word in passthrough:
        flag = _owned(word)
        if flag:
            raise RoundError(f"{flag} is set by arena, not after --")
    words = [*profile.profile_flags({k: v for k, v in prof.items() if k != "fresh"}),
             *passthrough]
    words = _drop_flag(words, "--fresh", False)
    words = _drop_flag(words, "--resume", False)
    words = _drop_flag(words, "--legs", True)
    if leg_folder is not None:
        words += ["--out", str(leg_folder), "--legs", "1"]
    flags = profile.dedupe_flags(words)
    return [sys.executable, "-m", "tools.contest", "run", "--ticket", str(nn),
            "--base", base, *flags, "--resume"]


def _rerun_refusal(data: dict, args: argparse.Namespace) -> Optional[str]:
    """Why `--agent NAME` cannot be brought back, or None."""
    names = [_agent_name(a.get("agent")) for a in data["agents"] if isinstance(a, dict)]
    if args.agent not in names:
        return f"no agent {args.agent!r} in the round (agents: {', '.join(names) or 'none'})"
    state = next(str(a.get("state")) for a in data["agents"]
                 if isinstance(a, dict) and _agent_name(a.get("agent")) == args.agent)
    if state == DEAD_STATE and not args.dead:
        return f"{args.agent} is {DEAD_STATE} — add --dead to bring it back"
    if state not in (*REVIVE, DEAD_STATE):
        return f"{args.agent} is {state} — only {', '.join(REVIVE)} or {DEAD_STATE} come back"
    return None


def run_rerun(repo: Path, args: argparse.Namespace, prof: dict[str, str]) -> int:
    """`arena run rerun NN[.K] (--failed | --agent NAME) [--dead]`: revive, then resume.

    Every refusal comes before anything is written. Exit 1 for a round with no
    `state.json`, 3 when `--failed` finds nothing to bring back.
    """
    repo = Path(repo)
    match = _VIEW_ARG.match(str(args.run))
    try:
        if not match:
            raise RoundError(f"run rerun: {args.run!r} is not NN or NN.K")
        if bool(args.failed) == bool(args.agent):
            raise RoundError("run rerun: give --failed or --agent NAME, one of them")
        nn = int(match.group(1))
        leg = int(match.group(2)) if match.group(2) is not None else None
        config = load_config(repo)
        folder = round_folder(repo, config, nn, leg=leg)
        state = folder / "state.json"
        if not state.is_file():
            print(output.scrub(f"arena: no round {args.run} (looked in {folder})"),
                  file=sys.stderr)
            return EXIT_FAILED
        if round_alive(repo, nn, PROC_ROOT):
            raise RoundError(f"round {nn} is running in {repo}")
        legs = leg_folders(folder.with_name(f"{nn:02d}"))
        in_legs = bool(_ROUND_DIR.match(folder.name) and "." in folder.name)
        if in_legs and legs and legs[-1] != folder and not args.yes:
            raise RoundError(f"{folder.name} is not the last leg ({legs[-1].name}): later legs "
                             "already changed the trees — add -y to rerun it anyway")
        try:
            data = json.loads(state.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
                raise ValueError("not a round state: no agents list")
        except (OSError, ValueError) as err:
            print(output.scrub(f"arena: {state} is unreadable: {err}"), file=sys.stderr)
            return EXIT_FAILED
        if args.agent:
            why = _rerun_refusal(data, args)
            if why:
                raise RoundError(why)
        base = _rev(repo, f"{REF_PREFIX}{nn}") and f"{REF_PREFIX}{nn}" or data.get("base_sha")
        if not base:
            raise RoundError(f"no {REF_PREFIX}{nn} and no base_sha in {state}")
        line = build_rerun_line(nn, str(base), prof, args.passthrough,
                                folder if in_legs else None)
    except (RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return output.refuse(str(err))

    before = {_agent_name(a.get("agent")): a.get("state")
              for a in data["agents"] if isinstance(a, dict)}
    revived = revive_agents([a for a in data["agents"] if isinstance(a, dict)],
                            only=args.agent or None, dead=args.dead)
    if not revived:
        print(f"arena: nothing to revive in round {args.run}", file=sys.stderr)
        return EXIT_NOTHING
    for name, was in before.items():
        print(output.scrub(f"{name}: {was} -> {REVIVED_STATE}" if name in revived
                           else f"{name}: {was} (kept)"))
    if args.dry_run:
        print(f"{len(revived)} to revive (dry run, nothing written)")
        return EXIT_OK
    try:
        shutil.copy2(state, state.with_name(BACKUP_NAME))
        state.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        wrote = state.stat().st_mtime
    except OSError as err:
        return output.refuse(f"cannot write {state}: {err}")
    return _run_child(repo, nn, line, state, started=math.nextafter(wrote, math.inf))
