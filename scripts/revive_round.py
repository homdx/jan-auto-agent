#!/usr/bin/env python3
"""revive_round.py — put a round's ended agents back into play for `--resume`.

    scripts/revive_round.py 112                     contest-out/112/state.json
    scripts/revive_round.py contest-out/112         a round's output folder
    scripts/revive_round.py path/to/state.json      the file itself
    scripts/revive_round.py 112 --dry-run           print only, write nothing

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
#: Not terminal, so `_plan` harvests the tree and restarts the agent.
REVIVED_STATE = "WAITING"
BACKUP_NAME = "state.before-revive.json"


def state_path(target: str) -> Path:
    """`112`, a round's folder or a state.json path — as the state.json path."""
    path = Path(target)
    if target.isdigit() and not path.exists():
        # a bare round number: the default out_dir under the repo this script is in
        path = Path(__file__).resolve().parent.parent / "contest-out" / target
    if path.is_dir():
        path = path / "state.json"
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("round", help="round number, a round's output folder, or its state.json")
    parser.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    args = parser.parse_args(argv)

    path = state_path(args.round)
    if not path.is_file():
        print(f"revive_round: no {path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        agents = data["agents"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"revive_round: {path} is unreadable: {exc}", file=sys.stderr)
        return 1

    revived = 0
    for agent in agents:
        name = agent.get("agent", {}).get("name", "?")
        before = agent.get("state")
        if before in REVIVE:
            agent["revived_from"] = {"state": before, "attempt": agent.get("attempt"),
                                     "last_error": agent.get("last_error")}
            agent["state"] = REVIVED_STATE
            agent["last_error"] = None
            revived += 1
            print(f"{name}: {before} -> {REVIVED_STATE}")
        else:
            print(f"{name}: {before} (kept)")

    if args.dry_run or not revived:
        print(f"{revived} to revive" + (" (dry run, nothing written)" if args.dry_run else ""))
        return 0
    shutil.copy2(path, path.with_name(BACKUP_NAME))
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{revived} revived; the old file is {path.with_name(BACKUP_NAME)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
