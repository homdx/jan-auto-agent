#!/usr/bin/env python3
"""Write the leg record of a finished round, for a manual two-leg relay (KC-77).

Run by hand between leg 1 and leg 2. Reads `<out>/state.json`, and for every
agent that did not end READY reports what the agent's own checkout holds:
the diffstat and files against the round's base_sha, the commit, uncommitted
edits, the last harvest verdict with its reason codes, and an empty
"what is left" section for the operator to fill in.

    python3 contest-bench/kc77/leg_record.py contest-out/01.1 > contest-out/01.1/leg-1-record.md
    python3 contest-bench/kc77/leg_record.py contest-out/01.1 --all      # READY agents too
    python3 contest-bench/kc77/leg_record.py contest-out/01.1 --agent laguna

The checkout path comes from state.json (`agents[i].workspace.path`), not from
a guessed `../rounds/NN-<agent>`. Nothing here starts kilo or touches a provider.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else f"(git {' '.join(args)} failed: {r.stderr.strip()})"


def last_harvest(agent: dict) -> dict:
    for turn in reversed(agent.get("turns") or []):
        if turn.get("harvest"):
            return turn["harvest"]
    return {}


def record(agent: dict, base: str) -> str:
    name = agent["agent"]["name"]
    path = Path(agent["workspace"]["path"])
    harvest = last_harvest(agent)
    lines = [f"## {name}", "",
             f"- state: {agent.get('state')}",
             f"- commit: {agent.get('commit') or 'none'}",
             f"- harvest verdict: {harvest.get('verdict', 'none')}",
             f"- harvest reasons: {harvest.get('reasons', [])}",
             f"- checkout: {path}", ""]
    if not path.is_dir():
        return "\n".join(lines + ["(checkout is gone — nothing to diff)", ""])
    lines += ["### Files and diffstat against the base", "```",
              git(path, "diff", "--stat", f"{base}..HEAD") or "(no commit above the base)", "```", "",
              "### Uncommitted edits", "```", git(path, "status", "--short") or "(clean)", "```", "",
              "### What is left", "", "<operator: read the diff and list what the ticket still needs>", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", help="the round's output dir, e.g. contest-out/01.1")
    ap.add_argument("--all", action="store_true", help="include agents that ended READY")
    ap.add_argument("--agent", action="append", help="only this agent (repeatable)")
    args = ap.parse_args(argv)
    state = json.loads((Path(args.out) / "state.json").read_text(encoding="utf-8"))
    base = state["base_sha"]
    out = [f"# Leg record — ticket {state.get('ticket')} (round {state.get('round_no')})", "",
           f"base: {base}", ""]
    shown = 0
    for agent in state.get("agents", []):
        name = agent["agent"]["name"]
        if args.agent and name not in args.agent:
            continue
        if agent.get("state") == "READY" and not args.all and not args.agent:
            out += [f"## {name}", "", "READY in this leg — no next leg.", ""]
            continue
        out.append(record(agent, base))
        shown += 1
    print("\n".join(out))
    return 0 if shown else 3


if __name__ == "__main__":
    sys.exit(main())
