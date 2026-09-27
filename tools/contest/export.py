"""tools/contest/export.py — KC-7: the round's folder is the one `contest-bench` reads.

`cli.export_patches` leaves one `.patch` or `.diff` per agent in `<out_dir>` and
prints them; this module writes the two files that turn that folder into the
input `contest-bench` starts from, so an operator does not hand-write them after
the round:

- `write_entrants` — `entrants.json`, in the shape
  `contest-bench/harness/setup_worktrees.py` and `validate_inputs.py` read:
  `base` is the round's base sha and one entry per exported file keyed by agent
  name. `source` is relative to the repo root when the file is under it, because
  both scripts resolve it against `--repo`, not against the JSON file's folder.
- `write_summary` — `SUMMARY.md`: the round's facts, one row per agent from
  `state.table_rows()`, the gate's own decisions, and the two commands to run
  next. `cmd_status` prints the same table off `state.json`.

Both stay inside *out_dir* and both are fail-open the way the round is: a patch
file that cannot be read still gets its `source` and simply no `duplicate_of`, a
`decisions.jsonl` that cannot be parsed contributes no line, an unreadable
`state.json` is `cmd_status`'s one-line refusal, and a round with no patch at all
writes no `entrants.json` — its `SUMMARY.md` is still written.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from tools.contest.runner import AgentState

__all__ = [
    "ENTRANTS_FILE",
    "SUMMARY_FILE",
    "render_table",
    "round_patches",
    "write_entrants",
    "write_summary",
]

#: The two files the round's folder gains. `contest-bench` reads the first, the
#: operator reads the second.
ENTRANTS_FILE = "entrants.json"
SUMMARY_FILE = "SUMMARY.md"

#: The extensions `export_patches` writes, and the only ones an entrant may point at.
_PATCH_EXTENSIONS = (".patch", ".diff")

#: The terminal states whose file name carries them, `<agent>.<STATE>.patch`.
_STATED_SUFFIXES = ("GAVE_UP", "STALLED", "ERROR")

#: `From <sha> <date>` — the first line of every `git format-patch` commit, and
#: the one that differs between two agents that made the same commit.
_FROM_SHA_RE = re.compile(r"^From [0-9a-fA-F]{7,40}(?:\s|$)")

#: `Date: <rfc2822>` — the same commit's author date, formatted by git.
_DATE_RE = re.compile(r"^Date:\s")

#: The `decisions.jsonl` layers SUMMARY's "Decisions worth a look" reads: the
#: gate's own verdicts, the allowed ones and the failed ones alike, because the
#: operator reads the section to see whether the gate blocked what the ticket
#: needed. `mechanical` never gets here — the mechanical layer is policy, not
#: judgement.
_GATE_LAYERS = ("gate", "gate-failed")

#: The two commands SUMMARY ends with — `contest-bench`'s own entry point and
#: the scorer that reads the worktrees it builds. `scripts/contest_reset.sh` is
#: deliberately absent: it prepares a round's worktrees and cleans nothing up.
_SETUP_WORKTREES = "python3 contest-bench/harness/setup_worktrees.py"
_JUDGE = "python3 scripts/judge_epic_round.py"


# ─────────────────────────────────────────────────────────────────────────────
# the paths
# ─────────────────────────────────────────────────────────────────────────────

def _repo_root(out_dir, repo=None) -> Path:
    """The repo root *out_dir* sits under: *repo* when given, else walked up.

    Walking up stops at the first folder holding a `.git`, which is this repo's
    root and the base the round ran on — the same root `setup_worktrees.py`'s
    `--repo` names. *repo* wins over the walk, because `cmd_run` already knows
    it. Fail-open: no `.git` anywhere up the tree gives *out_dir* itself, and
    every `source` then writes out as an absolute path, which both scripts
    resolve too.
    """
    if repo is not None:
        try:
            return Path(repo).resolve()
        except OSError:
            pass
    try:
        current = Path(out_dir).resolve()
    except OSError:
        return Path(out_dir)
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def _source_of(path: Path, root: Path) -> str:
    """*path* as `setup_worktrees.py` wants it: repo-relative under *root*, absolute else.

    Both `setup_worktrees.py` and `validate_inputs.py` resolve a non-absolute
    `source` against their own `--repo`, so a path that is not under the round's
    repo would silently point at a file that is not there. That is the one case
    an absolute `source` is worth.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(resolved)


def _display_of(path: Path, root: Path) -> str:
    """*path* the way an operator types it: repo-relative when it sits under *root*.

    For the two commands SUMMARY ends with — a command with an absolute path in
    it is harder to paste than one without, and both forms resolve to the same
    file.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    try:
        return str(resolved.relative_to(root))
    except ValueError:
        return str(resolved)


def _name_and_state(path) -> tuple:
    """`(agent name, state or "")` off one export file name.

    `export_patches` names its files `<agent>.patch`, `<agent>.<STATE>.patch` and
    `<agent>.<STATE>.diff` for the three scored terminal states, so the state is
    always a `<STATE>.` marker the name is what is left. A file that is neither
    of the two extensions gives `("", "")`, so the caller skips it.
    """
    stem = Path(path).name
    for extension in _PATCH_EXTENSIONS:
        if not stem.endswith(extension):
            continue
        rest = stem[: -len(extension)]
        state = ""
        for suffix in _STATED_SUFFIXES:
            if rest.endswith("." + suffix):
                state = suffix
                rest = rest[: -(len(suffix) + 1)]
                break
        return rest, state
    return "", ""


def round_patches(out_dir, state=None) -> list:
    """The round's `.patch` and `.diff` files in *out_dir*, in the state's agent order.

    `export_patches` returns them in state order and `cmd_run` hands that list on,
    so this exists only for `cmd_status`, which finds them again after the fact.
    Without *state* they come back sorted by name; with one, in agent order with
    the unmatched ones last. An *out_dir* that is not a folder gives `[]` — a
    round whose export was refused has no files to name, not an exception.
    """
    out = Path(out_dir)
    if not out.is_dir():
        return []
    try:
        found = [path for path in out.iterdir()
                 if path.is_file() and path.suffix in _PATCH_EXTENSIONS]
    except OSError:
        return []
    agents = getattr(state, "agents", None)
    names = []
    for run in agents or ():
        name = getattr(getattr(run, "agent", None), "name", "")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    if not names:
        return sorted(found, key=lambda path: path.name)
    by_name: dict = {}
    for path in found:
        by_name.setdefault(_name_and_state(path)[0], []).append(path)
    ordered = []
    for name in names:
        ordered.extend(sorted(by_name.pop(name, []), key=lambda path: path.name))
    ordered.extend(sorted((path for group in by_name.values() for path in group),
                          key=lambda path: path.name))
    return ordered


def _files_by_agent(paths) -> dict:
    """`{agent: file name}` off the exported paths — the table's last column.

    First file per agent wins, and `export_patches` appends the `.patch`es before
    the `.diff`s, so an agent that has both is named by the one that carries its
    commit.
    """
    files: dict = {}
    for path in paths:
        name, _ = _name_and_state(path)
        if name and name not in files:
            files[name] = Path(path).name
    return files


# ─────────────────────────────────────────────────────────────────────────────
# entrants.json
# ─────────────────────────────────────────────────────────────────────────────

def _read_text(path: Path):
    """The text of *path*, or `None` when it cannot be read.

    `None` means "no duplicate_of", not "empty patch": two files that both
    vanished would otherwise be declared byte-identical to each other, and the
    second would be dropped from the round.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _dedup_key(text: str) -> str:
    """*text* with the `From <sha>` and `Date:` lines dropped — the comparison key.

    Two agents that made the same change against the same base produce patches
    that differ in the commit sha on the first line and in the formatted date on
    the `Date:` line and in nothing else. `validate_inputs.py` marks exactly that
    pair `duplicate_of` for the bench, which sets the second up once and scores
    it as one.
    """
    kept = [line for line in text.splitlines()
            if not _FROM_SHA_RE.match(line) and not _DATE_RE.match(line)]
    return "\n".join(kept)


def write_entrants(out_dir, base_sha, state, paths, *, repo=None) -> Path | None:
    """Write `<out_dir>/entrants.json` from the round's exported files.

    One entry per file in *paths*, keyed by the agent name its file name carries
    — so a `GAVE_UP`, `STALLED` or `ERROR` entry is present and gets `"state"`
    alongside `"source"`, because its state is already in its file name and the
    bench scores it like any other. A file that is byte-identical to an earlier
    one after `_dedup_key` gets `"duplicate_of"` instead of `"source"`: the first
    of a duplicate pair is the one in the list, the order *paths* has.

    *paths* empty writes no file and returns `None` — there is nothing for
    `setup_worktrees.py` to apply, so an entrants file naming the base alone
    would be a folder that looks set up. Any other failure to write propagates
    to `cmd_run`, which reports it as a `warn:` line and keeps its exit code.
    """
    out = Path(out_dir)
    root = _repo_root(out, repo)
    entrants: dict = {}
    first_by_key: dict = {}
    for path in paths:
        path = Path(path)
        name, state_name = _name_and_state(path)
        if not name:
            continue
        entry: dict = {"source": _source_of(path, root)}
        if state_name:
            entry["state"] = state_name
        text = _read_text(path)
        key = _dedup_key(text) if text is not None else None
        if key is not None and key in first_by_key:
            entrants[name] = {"duplicate_of": first_by_key[key]}
        else:
            if key is not None:
                first_by_key[key] = name
            entrants[name] = entry
    if not entrants:
        return None
    payload = {"base": str(base_sha or ""), "entrants": entrants}
    out.mkdir(parents=True, exist_ok=True)
    target = out / ENTRANTS_FILE
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


# ─────────────────────────────────────────────────────────────────────────────
# SUMMARY.md
# ─────────────────────────────────────────────────────────────────────────────

#: The table's columns, in order: `state.table_rows()`'s keys, one per cell.
_TABLE_COLUMNS = (
    "name", "model", "state", "attempts", "sessions", "turns", "asked", "allowed", "rejected",
    "gated", "gate-failed", "questions", "cost", "tokens in", "tokens out",
    "fill%", "compactions", "commit", "last reason", "file",
)


def _cell(value) -> str:
    """One markdown cell: pipes escaped, `None` as `-`, booleans and numbers as text."""
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _number(value) -> int:
    """*value* as an int, `0` for the malformed — a bad cell is not a bad row."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _token_pair(tokens) -> tuple:
    """`(in, out)` off one run's `tokens` dict: in is input plus cache reads.

    `run.tokens` is what Kilo reports per session — `input`, `cache.read`,
    `reasoning` and `output` — and the split an operator wants is "what went in
    against the context window" versus "what the model wrote". A dict that is not
    a dict is `(0, 0)`, not an exception: an unreadable run still gets its row.
    """
    if not isinstance(tokens, dict):
        return 0, 0
    cache = tokens.get("cache")
    cache_read = _number(cache.get("read") if isinstance(cache, dict) else 0)
    came_in = _number(tokens.get("input")) + cache_read
    went_out = _number(tokens.get("output")) + _number(tokens.get("reasoning"))
    return came_in, went_out


def _fill_cell(fill) -> str:
    """The run's last fill as SUMMARY's ``fill%`` cell, ``-`` when it was never read.

    KC-10: the percent of the context window the last turn left, the same number
    its `turns.jsonl` line carries. ``-`` for ``None`` — a run whose model was
    never sized knows nothing — never ``0%``, which would read as "an empty
    session" instead of "no size".
    """
    if isinstance(fill, (int, float)) and not isinstance(fill, bool):
        return f"{float(fill):.1f}%"
    return "-"


def _utc(value) -> str:
    """One timestamp the way an operator reads it, `2026-09-27 07:30:41 UTC`.

    `state.started_at` is `time.time()`, so the summary's own end time is taken
    at write time — SUMMARY is written immediately after the round ends, and the
    write is what the end is.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OverflowError, OSError, ValueError):
        return "-"


def _wall(started_at, ended_at) -> str:
    """The round's wall time in seconds, or `-` when the start is not a number."""
    try:
        seconds = float(ended_at) - float(started_at)
    except (TypeError, ValueError):
        return "-"
    return f"{max(0.0, seconds):.0f} s"


def render_table(state, paths) -> list:
    """The SUMMARY table as markdown lines, one row per agent — `status` prints it too.

    `state.table_rows()` gives the facts, *paths* gives each agent's exported
    file. Every row is printed even when the row has no file: an agent that ended
    `STALLED` on a clean tree has nothing to score, and dropping it from the table
    would make the table disagree with the round.
    """
    rows = []
    try:
        rows = list(state.table_rows())
    except Exception:  # noqa: BLE001 — an unreadable state is an empty table, not a crash
        rows = []
    files = _files_by_agent(paths)
    lines = ["| " + " | ".join(_TABLE_COLUMNS) + " |",
             "|" + "|".join(" --- " for _ in _TABLE_COLUMNS) + "|"]
    for row in rows:
        if not isinstance(row, dict):
            continue
        permissions = row.get("permissions")
        permissions = permissions if isinstance(permissions, dict) else {}
        came_in, went_out = _token_pair(row.get("tokens"))
        cost = row.get("cost")
        commit = str(row.get("commit") or "")
        cells = [
            row.get("name"),
            row.get("model"),
            row.get("state"),
            row.get("attempts"),
            _number(row.get("sessions")),
            row.get("turns"),
            _number(permissions.get("asked")),
            _number(permissions.get("allowed")),
            _number(permissions.get("rejected")),
            _number(permissions.get("gated")),
            _number(permissions.get("gate_failed")),
            row.get("questions"),
            _cell(cost),
            came_in,
            went_out,
            _fill_cell(row.get("fill")),
            _number(row.get("compactions")),
            commit[:12] if commit else "-",
            row.get("last_reason") or "-",
            files.get(str(row.get("name") or ""), "-"),
        ]
        lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    return lines


def _gate_lines(out_dir, state) -> list:
    """One markdown bullet per `gate` and `gate-failed` decision of the round.

    Read from each agent's own `<out>/<agent>/decisions.jsonl`, in the state's
    agent order — the policy writes one line per ask, and the layer says whether
    the gate spoke (`gate`) or could not (`gate-failed`). The operator reads the
    section to see whether the gate blocked something the ticket needed, so the
    command and the reason ride on the line, and a line with neither is dropped
    rather than printed as an empty bullet.

    Fail-open: a missing, unreadable or malformed file contributes no lines, and a
    run whose `state.json` has no agent name is skipped. Never a refusal.
    """
    out = Path(out_dir)
    agents = getattr(state, "agents", None) or ()
    names = []
    for run in agents:
        name = getattr(getattr(run, "agent", None), "name", "")
        if isinstance(name, str) and name and name not in names:
            names.append(name)
    lines = []
    for name in names:
        path = out / name / "decisions.jsonl"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            layer = str(item.get("layer") or "")
            if layer not in _GATE_LAYERS:
                continue
            reply = str(item.get("reply") or "")
            # an edit or a read has no command: its paths are what was asked
            patterns = item.get("patterns")
            named = ", ".join(str(x) for x in patterns) if isinstance(patterns, list) else ""
            command = str(item.get("command") or "").strip() or named.strip()
            reason = str(item.get("reason") or "").strip()
            if not (command or reason):
                continue
            parts = [f"{name}: {layer}"]
            if reply:
                parts.append(reply)
            if command:
                parts.append("`" + command.replace("`", "'") + "`")
            if reason:
                parts.append(reason)
            lines.append("- " + " — ".join(parts))
    return lines


def write_summary(out_dir, state, base_sha, paths, *, repo=None, gate="") -> Path:
    """Write `<out_dir>/SUMMARY.md` — the round's facts, table, gate decisions, next commands.

    Three parts, in order: a header of the ticket, the base, the gate model, the
    round's start and end and its wall time; the table from `render_table`; and
    "Decisions worth a look", the `gate` and `gate-failed` decisions of every
    agent's `decisions.jsonl`. It ends with the two commands to run next, built
    from the real paths — `setup_worktrees.py` on the `entrants.json` this call
    writes alongside, and `judge_epic_round.py` with a `--worktree` per agent
    from the state's own workspaces. `scripts/contest_reset.sh` is not named: it
    prepares a round's worktrees and cleans nothing up.

    *gate* is the label `cli._gate_plan_label` prints in the plan, so the summary
    and the plan say the same word about the gate; `""` when the round has none.
    *paths* empty still writes the summary — a round nobody finished is the one
    whose table and gate refusals the operator most needs — and leaves out the
    `setup_worktrees.py` line, since `write_entrants` wrote no file to name.
    """
    out = Path(out_dir)
    root = _repo_root(out, repo)
    agents = getattr(state, "agents", None) or ()
    ticket = getattr(state, "ticket", "") or ""
    try:
        round_no = getattr(state, "round_no", None)
        round_no = int(round_no)
    except (TypeError, ValueError):
        round_no = 0
    started = getattr(state, "started_at", None)
    ended = time.time()
    worktrees = []
    for run in agents:
        workspace = getattr(run, "workspace", None)
        path = getattr(workspace, "path", None)
        agent = getattr(run, "agent", None)
        name = getattr(agent, "name", "")
        if path is None or not isinstance(name, str) or not name:
            continue
        worktrees.append((name, str(path)))

    header = [f"# Round {round_no:02d}: {ticket or '-'}", ""]
    for label, value in (
        ("ticket", ticket or "-"),
        ("base", str(base_sha or "-")),
        ("gate", str(gate or "off")),
        ("started", _utc(started)),
        ("ended", _utc(ended)),
        ("wall", _wall(started, ended)),
    ):
        header.append(f"- {label}: {value}")
    header.append("")

    body = ["## Agents", ""] + render_table(state, paths) + [""]
    decisions = _gate_lines(out, state)
    decision_part = ["## Decisions worth a look", ""]
    decision_part.extend(decisions if decisions else ["_(no gate decision)_"])
    decision_part.append("")

    entrants = _display_of(out / ENTRANTS_FILE, root)
    judge_parts = [f"{_JUDGE} --round {round_no}", f"--base {base_sha or '-'}"]
    for name, path in worktrees:
        judge_parts.append(f"--worktree {name}={path}")
    next_part = ["## Next", "", "```bash"]
    if paths:
        next_part.append(f"{_SETUP_WORKTREES} {entrants} --wt {_display_of(out, root)}/wt")
    next_part += [" ".join(judge_parts), "```", ""]

    out.mkdir(parents=True, exist_ok=True)
    target = out / SUMMARY_FILE
    target.write_text("\n".join(header + body + decision_part + next_part), encoding="utf-8")
    return target
