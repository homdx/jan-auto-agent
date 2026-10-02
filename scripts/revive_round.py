#!/usr/bin/env python3
"""revive_round.py — put a round's ended agents back into play for `--resume`.

    scripts/revive_round.py 112                     contest-out/112/state.json
    scripts/revive_round.py contest-out/112         a round's output folder
    scripts/revive_round.py path/to/state.json      the file itself
    scripts/revive_round.py 112 --dry-run           print only, write nothing
    scripts/revive_round.py 65                      a round of legs: its last leg,
                                                    contest-out/65.3/state.json
    scripts/revive_round.py 112 --dead              DEAD agents come back too
    scripts/revive_round.py 112 --agent NAME        that one agent only

A round of legs writes one folder per leg, `NN.1 … NN.K`, and only the last
leg's state.json is the whole round: an agent a leg handed on is stale in the
earlier legs' files. So a bare number or the round's own folder resolves to
the highest leg folder there is, and a leg folder that is not the last one is
refused unless `--any-leg` says so. Legs share one worktree per agent, so an
earlier leg restarts on the tree as the later legs left it, not as it was
then — only the agents' states come from that leg. Resume that leg's folder with
`run --ticket NN --out contest-out/NN.K --legs 1 --resume`.

`--resume` skips every terminal agent. This script sets each STALLED, GAVE_UP
and ERROR agent back to WAITING, which is not terminal, so the next
`run --ticket NN --resume` harvests its tree first: a finished commit is taken
as READY, and anything else restarts in the same tree with a fresh attempt
budget. READY agents are left as they are. The old state, attempt and error are
kept on each revived agent under `revived_from`, and the file as it was is
copied to `state.before-revive.json` next to it.

Run it only when the round is not running: a live runner rewrites state.json
on every transition and would undo this.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

#: The terminal states this script brings back; READY stays terminal.
REVIVE = ("STALLED", "GAVE_UP", "ERROR")
#: `--dead` adds this one: no edit in the first-touch window, a terminal state.
DEAD_STATE = "DEAD"
#: Not terminal, so `_plan` harvests the tree and restarts the agent.
REVIVED_STATE = "WAITING"
BACKUP_NAME = "state.before-revive.json"


def leg_folders(folder: Path) -> list[Path]:
    """The leg folders `<folder>.1 … <folder>.K` next to *folder*, by leg number."""
    if not folder.parent.is_dir():
        return []
    legs = []
    for sibling in folder.parent.iterdir():
        stem, dot, leg = sibling.name.rpartition(".")
        if dot and stem == folder.name and leg.isdigit() and sibling.is_dir():
            legs.append((int(leg), sibling))
    return [path for _, path in sorted(legs)]


def state_path(target: str, any_leg: bool = False) -> Path:
    """`112`, a round's folder, a leg's folder or a state.json path — as the
    state.json path of the round's whole state; SystemExit on an earlier leg."""
    path = Path(target)
    if target.isdigit() and not path.exists():
        # a bare round number: the default out_dir under the repo this script is in
        path = Path(__file__).resolve().parent.parent / "contest-out" / target
    folder = path.parent if path.name == "state.json" else path
    legs = leg_folders(folder)
    if legs and not (folder / "state.json").is_file():
        # the round's own folder of a round of legs: its last leg holds the round
        folder = legs[-1]
        print(f"revive_round: a round of {len(legs)} legs, using {folder}")
    stem, dot, leg = folder.name.rpartition(".")
    if dot and leg.isdigit():
        later = leg_folders(folder.with_name(stem))
        if later and later[-1] != folder and any_leg:
            print(f"revive_round: {folder} is an earlier leg than {later[-1]} (--any-leg); "
                  f"the worktrees hold what the later legs left")
        elif later and later[-1] != folder:
            raise SystemExit(f"revive_round: {folder} is not the last leg, {later[-1]} is — "
                             f"only the last leg's state.json is the whole round; --any-leg to revive it anyway")
    return folder / "state.json"


def revive_agents(agents: list[dict], only: str | None = None, dead: bool = False) -> list[str]:
    """Set the revivable agents in *agents* back to WAITING, in place; the revived names.

    STALLED, GAVE_UP and ERROR are revivable, and DEAD only with *dead*. *only*
    narrows it to the agent of that name. The old state, attempt and error stay
    on the agent under `revived_from`.
    """
    states = (*REVIVE, DEAD_STATE) if dead else REVIVE
    revived: list[str] = []
    for agent in agents:
        name = agent.get("agent", {}).get("name", "?")
        before = agent.get("state")
        if before not in states or (only is not None and name != only):
            continue
        agent["revived_from"] = {"state": before, "attempt": agent.get("attempt"),
                                 "last_error": agent.get("last_error")}
        agent["state"] = REVIVED_STATE
        agent["last_error"] = None
        revived.append(name)
    return revived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("round", help="round number, a round's output folder, or its state.json")
    parser.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    parser.add_argument("--any-leg", action="store_true",
                        help="revive a leg folder that is not the round's last leg")
    parser.add_argument("--dead", action="store_true", help="bring DEAD agents back too")
    parser.add_argument("--agent", metavar="NAME", help="revive this one agent only")
    args = parser.parse_args(argv)

    path = state_path(args.round, args.any_leg)
    if not path.is_file():
        print(f"revive_round: no {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        agents = data["agents"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"revive_round: {path} is unreadable: {exc}", file=sys.stderr)
        return 1

    names = revive_agents(agents, only=args.agent, dead=args.dead)
    revived = len(names)
    for agent in agents:
        name = agent.get("agent", {}).get("name", "?")
        if name in names:
            print(f"{name}: {agent['revived_from']['state']} -> {REVIVED_STATE}")
        else:
            print(f"{name}: {agent.get('state')} (kept)")

    if args.dry_run or not revived:
        print(f"{revived} to revive" + (" (dry run, nothing written)" if args.dry_run else ""))
        return 0
    shutil.copy2(path, path.with_name(BACKUP_NAME))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{revived} revived; the old file is {path.with_name(BACKUP_NAME)}")
    stem, dot, leg = path.parent.name.rpartition(".")
    if dot and leg.isdigit():
        # a leg is resumed on its own: the relay itself refuses --resume
        print(f"resume: run --ticket {stem} --out {path.parent} --legs 1 --resume")
    return 0


if __name__ == "__main__":
    sys.exit(main())
