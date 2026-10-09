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

from scripts.revive_round import (BACKUP_NAME, DEAD_STATE, REVIVE, REVIVED_STATE, agent_name,
                                  leg_folders,
                                  revive_agents)
from tools.contest import cli as contest_cli
from tools.contest import roster, workspace

from . import output, profile
from .gitref import (GitRefError, commit_file_on, git, ls_tree_names, read_utf8,
                     status_paths, tree_with_file)

#: The ref prefix (`arena-round/7`); the full ref is under `refs/heads/`.
REF_PREFIX = "arena-round/"

#: The ticket folder in the checkout and on the integration branch.
TASKS_DIR = contest_cli.TASKS_DIR

#: `07-x.md` is round 7 — the same match as `gates.ticket_for_round`.
_TICKET_RE = re.compile(r"^0*(\d+)-.*\.md$")
# No `$`: in MULTILINE mode it only matches before a `\n`, so a CRLF line's `\r`
# would have to be taken by the match and the replacement would drop it.
_STATUS_LINE = re.compile(r"^\*\*Status:\*\*[^\r\n]*", re.MULTILINE)
#: A round folder: `06` or `06.2`.
_ROUND_DIR = re.compile(r"^(\d+)(?:\.(\d+))?$")

#: Flags arena sets itself; the passthrough may never carry them.
_OWNED_FLAGS = profile.OWNED_FLAGS

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


def _as_int(text: str) -> Optional[int]:
    """`int(text)`, or None when `int()` refuses it.

    Bug 185: `str.isdigit()` is no stand-in for that — `'²'.isdigit()` is True and
    `int('²')` raises, as does an `int()` of a 4300+ digit string. This reads what
    the runner's own argparse `type=int` would: `07` is 7.
    """
    try:
        return int(text)
    except ValueError:
        return None


def _matches(words: Optional[list[str]], nn: int) -> bool:
    """`tools.contest`, `run` and `--ticket NN` / `--ticket=NN` (`07` is 7).

    Every process on the box passes through here, so a word that is no number is
    a process that is not round NN — never an exception (bug 185).
    """
    if not words or "tools.contest" not in words or "run" not in words:
        return False
    for i, word in enumerate(words):
        value = None
        if word == "--ticket" and i + 1 < len(words):
            value = words[i + 1]
        elif word.startswith("--ticket="):
            value = word.split("=", 1)[1]
        if value is not None and _as_int(value) == nn:
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
        pid = lock.read_text(encoding="utf-8", errors="replace").strip()
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
            return found[0], read_utf8(folder / found[0])
    names = ls_tree_names(repo, branch, TASKS_DIR + "/")
    found = _numbered([name.rsplit("/", 1)[-1] for name in names], nn)
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
    # Bug 186: `show-ref --verify` takes an exact ref name. `rev-parse --verify`
    # evaluates revision syntax, so `main~1`, `main^` or `main@{1}` "existed" and a
    # round was built off an older commit than the branch's tip, without a word.
    try:
        git(repo, "show-ref", "--verify", "-q", f"refs/heads/{branch}")
    except GitRefError:
        raise RoundError(f"branch {branch!r} does not exist") from None
    return branch


def _dirty_tasks(repo: Path) -> list[str]:
    """Untracked or modified files under the checkout's `epic-tasks/`, by their real names.

    Bug 209: `git()` stripped the whole `status --porcelain` output, so the first
    line lost its leading space and `line[3:]` cut a letter — the refusal named
    `pic-tasks/01-a.md` instead of the file, and a non-ASCII name came back with
    its quote escapes, a rename as `old -> new`.
    """
    return status_paths(repo, TASKS_DIR + "/")


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
    """The arena-owned flag *word* sets, abbreviations included (bug 172)."""
    return profile.owned_flag(word, _OWNED_FLAGS)


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


def _map_exit(code: int, state: Path, started: float,
              written: Optional[bytes] = None) -> int:
    """The runner's exit as arena's. *written* is what arena itself put in
    *state* just before the start (`run rerun`): a different body afterwards is
    the runner's rewrite even when a coarse-mtime filesystem gives it the same
    mtime as arena's write (bug 178)."""
    if code in (EXIT_OK, EXIT_FAILED):
        return code
    if code == 2:
        try:
            ran = state.stat().st_mtime >= started
            if not ran and written is not None:
                ran = state.read_bytes() != written
        except OSError:
            ran = False
        return EXIT_NO_READY if ran else EXIT_FAILED
    return EXIT_FAILED


def _run_child(repo: Path, nn: int, line: list[str], state: Path,
               started: Optional[float] = None, written: Optional[bytes] = None) -> int:
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
    try:
        try:
            lock.write_text(str(os.getpid()), encoding="utf-8")
            child = SPAWN(line, cwd=str(repo))
        except OSError as err:
            return output.refuse(f"cannot start the runner: {err}")
        # Bug 181: the runner is up from here on. A failed swap to its pid keeps
        # arena's own pid in the lock and still waits the child out — never
        # "cannot start" over a running, unlocked runner. An exception that is
        # no OSError (from SPAWN too) leaves through the `finally`, lock gone (157).
        try:
            lock_tmp.write_text(f"{child.pid}\n", encoding="utf-8")
            os.replace(lock_tmp, lock)
        except OSError:
            lock_tmp.unlink(missing_ok=True)
        while True:
            try:
                code = child.wait()
                break
            except KeyboardInterrupt:
                # Ctrl-C reached the child too (same process group): wait it out
                continue
    finally:
        lock.unlink(missing_ok=True)
    return _map_exit(code, state, started, written)


# ── 211: the old round's uncommitted worktrees, and the one-shot `--fresh` ───
def dirty_worktrees(repo: Path, config, nn: int) -> list[tuple[Path, int, int]]:
    """`(worktree, modified, untracked)` for each `<rounds_dir>/NN-*` holding uncommitted work.

    The same read the runner's intake refuses on (`workspace._dirty_outside_runs`:
    the round's own `runs/` scratch is not work). `RoundError` when git cannot
    answer — a tree that cannot be read is not reported clean.
    """
    root = workspace._resolve_rounds_dir(Path(repo), str(config.rounds_dir))
    found = []
    if not root.is_dir():
        return found
    for tree in sorted(root.glob(f"{nn:02d}-*")):
        if not (tree / ".git").exists():
            continue
        try:
            lines = workspace._dirty_outside_runs(tree)
        except workspace.WorkspaceError as err:
            raise RoundError(str(err)) from err
        if lines:
            untracked = sum(1 for line in lines if line.startswith("??"))
            found.append((tree, len(lines) - untracked, untracked))
    return found


def _tree_label(repo: Path, tree: Path) -> str:
    try:
        return str(tree.relative_to(Path(repo).resolve()))
    except ValueError:
        return str(tree)


def _refusal_over_dirty(repo: Path, nn: int, dirty: list[tuple[Path, int, int]]) -> str:
    """The intake's refusal in arena's words: what to run in arena, not in `tools.contest`."""
    names = ", ".join(_tree_label(repo, t) for t, _m, _u in dirty[:3])
    more = f" and {len(dirty) - 3} more" if len(dirty) > 3 else ""
    return (f"{names}{more} hold{'s' if len(dirty) == 1 else ''} uncommitted work from the "
            f"old round — rerun with `arena run start {nn} --fresh` to discard it, or "
            f"`arena run rerun {nn} --failed` to continue it")


def _print_dirty(repo: Path, dirty: list[tuple[Path, int, int]]) -> None:
    for tree, modified, untracked in dirty:
        print(output.scrub(f"  {_tree_label(repo, tree)}: M {modified}, ?? {untracked}"))


def _profile_name(args: argparse.Namespace) -> str:
    """The profile this start runs on: the one `cli` resolved, else `-p`, else `default`."""
    return getattr(args, "active_profile", None) or getattr(args, "profile", None) or "default"


def _confirm_discard(count: int, yes: bool) -> bool:
    """`discard N worktrees? [y/N]`; `-y` skips the question, no terminal is a no."""
    if yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input(f"discard {count} worktree{'s' if count != 1 else ''}? [y/N] "
                     ).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def backup_round_folder(repo: Path, config, nn: int, name: str) -> Optional[Path]:
    """Copy `<out_dir>/NN` to `NN.<name>-<UTC stamp>` beside it; never over an existing folder.

    None when the round has no folder yet. The result never matches `_ROUND_DIR`,
    so `run list` and `run view` do not take it for a leg.
    """
    base = contest_cli._round_out_dir(Path(repo), config, nn)
    if not base.is_dir():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = base.with_name(f"{base.name}.{name}-{stamp}")
    extra = 1
    while target.exists():
        extra += 1
        target = base.with_name(f"{base.name}.{name}-{stamp}-{extra}")
    try:
        shutil.copytree(base, target, symlinks=True)
    except (OSError, shutil.Error) as err:
        shutil.rmtree(target, ignore_errors=True)
        raise RoundError(f"cannot back up {base}: {err}") from err
    return target


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
        # AR-14 §5: the lower open tickets intake would hand out first, named
        # before `arena-round/NN` is built. Deferred import: tickets imports us.
        from . import tickets as _tickets
        if _tickets.report_intake_blockers(repo, branch, nn,
                                           getattr(args, "output", "table")):
            return EXIT_USAGE
        dirty = _dirty_tasks(repo)
        if dirty:
            raise RoundError(f"{TASKS_DIR}/ has uncommitted files: {', '.join(dirty)} — "
                             "move drafts to .arena/drafts/")
        # 211: `--fresh` is a spoken word — this flag, or the profile's `fresh=yes`
        # (named, never silent), or one after `--`; never stored by arena itself.
        if getattr(args, "fresh", False) and "--fresh" not in line:
            line.append("--fresh")
        fresh = "--fresh" in line
        old_trees = dirty_worktrees(repo, config, nn)
        if old_trees and not fresh:
            raise RoundError(_refusal_over_dirty(repo, nn, old_trees))
        if fresh:
            if str(prof.get("fresh", "")).strip() and profile.fresh_on(prof["fresh"]):
                print(f"profile {_profile_name(args)} has fresh=yes: "
                      "this start discards uncommitted work")
            if old_trees:
                print(f"--fresh discards uncommitted work in {len(old_trees)} worktree"
                      f"{'s' if len(old_trees) != 1 else ''}:")
                _print_dirty(repo, old_trees)
                if not _confirm_discard(len(old_trees), getattr(args, "yes", False)):
                    raise RoundError("not discarded — add -y to discard without a terminal "
                                     "(nothing was changed)")
        name, text = find_ticket(repo, nn, branch)
        content = open_status(text)
        sha = build_round_ref(repo, nn, branch, name, content, fresh=args.fresh_ticket,
                              state_exists=_has_state(repo, config, nn), yes=args.yes)
    except (RoundError, GitRefError, profile.ProfileError, OSError) as err:
        return output.refuse(str(err))

    if fresh and not getattr(args, "no_backup", False):
        try:
            kept = backup_round_folder(repo, config, nn, _profile_name(args))
        except RoundError as err:
            return output.refuse(str(err))
        if kept is not None:
            print(output.scrub(f"old results kept in {_tree_label(repo, kept)}"))

    record = {
        "branch": branch,
        "base_ref": f"{REF_PREFIX}{nn}",
        "base_sha": sha,
        "ticket_sha256": hashlib.sha256(content.encode("utf-8", "surrogateescape")).hexdigest(),
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
        # Bug 177: pick from the folders found, never a re-derived `NN` path —
        # an unpadded `7/` or `8.1/` has no `07/` beside it. A base with a
        # state.json wins (the padded one first), else the highest leg, as
        # `round_folder` says.
        bases = sorted((f for _leg, f in folders if not _leg),
                       key=lambda f: (not (f / "state.json").is_file(), f.name != f"{nn:02d}"))
        if bases and ((bases[0] / "state.json").is_file() or not legs):
            last = bases[0]
        else:
            last = legs[-1][1]
        state = last / "state.json"
        try:
            mtime = state.stat().st_mtime
        except OSError:
            try:
                mtime = last.stat().st_mtime
            except OSError:
                mtime = 0.0
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
#: `NN` or `NN.K`, digits only; `07.2` is round 7 leg 2. Bug 187: at most 18 digits
#: a part — `int()` of a 4300+ digit string raises, and no round has such a number.
_VIEW_ARG = re.compile(r"^(\d{1,18})(?:\.(\d{1,18}))?$")

#: The `-o json` columns, one row per agent in `state.json` order.
VIEW_COLUMNS = ["agent", "state", "attempt", "tokens", "commit"]


def _count(value) -> int:
    """*value* as a token count: a number, finite — anything else is 0.

    Bug 184: `json.loads` reads `NaN` and `Infinity`, and `int()` of either raises
    (ValueError, OverflowError), so a `state.json` holding one ended `run view -o
    json` in a traceback.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value)


def _total_tokens(tokens) -> int:
    """`input + output + reasoning` as an integer; 0 for a missing or odd value."""
    if not isinstance(tokens, dict):
        return _count(tokens)
    return sum(_count(tokens.get(key)) for key in ("input", "output", "reasoning"))


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
        return output.refuse(f"run view: {args.run!r:.60} is not NN or NN.K")
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
    names = [agent_name(a) for a in data["agents"] if isinstance(a, dict)]
    if args.agent not in names:
        return f"no agent {args.agent!r} in the round (agents: {', '.join(names) or 'none'})"
    state = next(str(a.get("state")) for a in data["agents"]
                 if isinstance(a, dict) and agent_name(a) == args.agent)
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
            raise RoundError(f"run rerun: {args.run!r:.60} is not NN or NN.K")
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

    before = {agent_name(a): a.get("state")
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
        body = json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")
        state.write_bytes(body)
        wrote = state.stat().st_mtime
    except OSError as err:
        return output.refuse(f"cannot write {state}: {err}")
    return _run_child(repo, nn, line, state, started=math.nextafter(wrote, math.inf),
                      written=body)
