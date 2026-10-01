#!/usr/bin/env python3
"""Live smoke of tools/contest/runner.py: the real `kilo serve`, the roster of
contest.ini (the three kenary free models), one small ticket in a sandbox repo.

    python3 contest-bench/kc6/live_smoke.py --round 1          # ticket 01: fix + test, one turn
    python3 contest-bench/kc6/live_smoke.py --round 2          # ticket 02: forces a rework and two permissions
    python3 contest-bench/kc6/live_smoke.py --round 2 --resume # after a Ctrl-C: state.json → resume
    python3 contest-bench/kc6/live_smoke.py --round 1 --agents hy3,mistral
    python3 contest-bench/kc6/live_smoke.py --round 1 --models agnes-2-5-flash:free,glm-4-7-flash:free --max-parallel 4

The sandbox (/tmp/contest/live-kc6/repo, rebuilt on --round 1) has the real
scripts/next_task.py and append_task.py, a stub CollectBridge, pkg/thing.py
and two tickets; the worktrees go to /tmp/contest/live-kc6/rounds and the
runner's artifacts to /tmp/contest/live-kc6/out<round>. The path is under
contest.ini's tmp_roots, so a model writing to /tmp/contest/notes is allowed
mechanically, a sibling worktree is forbidden, and anything else outside
goes to the gate — which, with contest.ini's placeholder gate URL, fails
closed (a `gate-failed` reject the model can read).

What the 2026-09-19 runs showed is in RUNBOOK.md §10 (the roster) and §11
(`--models`, twelve more). Requires the Kilo VS Code extension's `bin/kilo`
(find_kilo_binary) and the kenary provider signed in there; nothing here is a test — it costs real (free-tier) calls.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-smoke")

from tools.contest.kilo_client import KiloServer, find_kilo_binary  # noqa: E402
from tools.contest.roster import AgentSpec, load_roster  # noqa: E402
from tools.contest.runner import RoundState, run_round  # noqa: E402
from tools.contest.workspace import prepare_round  # noqa: E402

LIVE = Path("/tmp/contest/live-kc6")
SANDBOX = LIVE / "repo"
TICKETS = {1: "01-total-empty.md", 2: "02-mean.md"}

BRIDGE = '''"""stub for the round's ground rule"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        out = raw.strip()
        return out[:10]
'''
THING = '''"""pkg/thing.py — a tiny module for the live smoke round."""


def total(values):
    """Return the sum of *values*.

    Bug: an empty iterable returns None instead of 0, and strings are
    accepted and concatenated.
    """
    result = None
    for v in values:
        result = v if result is None else result + v
    return result
'''
TEST_BASE = "from pkg.thing import total\n\n\ndef test_total_two():\n    assert total([1, 2]) == 3\n"
GROUND = """
## Ground rules

- Do not touch `CollectBridge._shrink` in `tools/auto/collect_bridge.py`.
- Do not edit `epic-tasks/`.
- Never `git push`.
- A test ships with the change and fails without it.
"""
TICKET_01 = """# 01 — `pkg/thing.py`: `total([])` must return 0, not None

**Status:** {status} — round 1 of the live smoke.
**Severity:** LOW
**File:** `pkg/thing.py`
**Symbol:** `total`
**Round:** 1
**Size:** S
**Also touches:** `tests/test_total_empty.py` (new)

## What happens today

`total([])` returns `None` and `total(iter([]))` too; the docstring promises
a sum, and a sum of nothing is 0. Strings are also accepted and concatenated.

## What must change

`total(values)` returns `0` for an empty iterable and raises `TypeError` for
a non-numeric element (use `sum` with a start of `0`; do not special-case
strings by hand).

## Acceptance

- [ ] `tests/test_total_empty.py` (new): `total([]) == 0`, `total(iter([])) == 0`,
      `total([1.5, 2]) == 3.5`, `total(["a"])` raises `TypeError`.
- [ ] `python3 -m pytest tests -q` green.
""" + GROUND + """- Exactly one local commit on this branch.
- When done: `python3 scripts/append_task.py --progress runs/<your name>/PROGRESS.csv --ticket 01-total-empty.md --outcome DONE --commit <sha> --note "..."`.
"""
TICKET_02 = """# 02 — `pkg/thing.py`: add `mean(values)` next to `total`

**Status:** open — round 2 of the live smoke.
**Severity:** LOW
**File:** `pkg/thing.py`
**Symbol:** `mean`
**Round:** 2
**Size:** S
**Also touches:** `tests/test_mean.py` (new)

## What must change

Add `mean(values)` to `pkg/thing.py`: the arithmetic mean of a non-empty
iterable of numbers, built on `total`; an empty iterable raises
`ValueError("mean of nothing")`.

## How to work (this round's procedure — follow it literally)

1. Write the code change and commit it on its own: `git commit -m "02: mean"`.
2. Then write `tests/test_mean.py` and commit it separately: `git commit -m "02: test"`.
3. Write one line about what you did to `/tmp/contest/notes/<your name>.txt`
   (create the folder if needed) and one line to `/tmp/kc6-outside-<your name>.txt`.
   If a write is refused, do not retry it — say so in your report and go on.
4. Record the ticket with `scripts/append_task.py` as the prompt says.

## Acceptance

- [ ] `tests/test_mean.py` (new): `mean([1, 2, 3]) == 2`, `mean([1.5]) == 1.5`,
      `mean([])` raises `ValueError`.
- [ ] `python3 -m pytest tests -q` green.
""" + GROUND


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def build_sandbox(round_no: int) -> None:
    """Round 1 rebuilds the repo from scratch; round 2 adds ticket 02 on top."""
    if round_no == 1:
        shutil.rmtree(LIVE, ignore_errors=True)
        SANDBOX.mkdir(parents=True)
        git(SANDBOX, "init", "-q", "-b", "main")
        git(SANDBOX, "config", "user.email", "live@example.invalid")
        git(SANDBOX, "config", "user.name", "live")
        for name in ("next_task.py", "append_task.py"):
            (SANDBOX / "scripts").mkdir(exist_ok=True)
            shutil.copy(REPO / "scripts" / name, SANDBOX / "scripts" / name)
        files = {
            "tools/auto/collect_bridge.py": BRIDGE,
            "pkg/__init__.py": "",
            "pkg/thing.py": THING,
            "tests/__init__.py": "",
            "tests/test_base.py": TEST_BASE,
            "epic-tasks/01-total-empty.md": TICKET_01.format(status="open"),
            ".gitignore": "runs/\n__pycache__/\n.pytest_cache/\n",
        }
        for rel, text in files.items():
            (SANDBOX / rel).parent.mkdir(parents=True, exist_ok=True)
            (SANDBOX / rel).write_text(text, encoding="utf-8")
        git(SANDBOX, "add", "-A")
        git(SANDBOX, "commit", "-q", "-m", "base")
    elif not (SANDBOX / "epic-tasks" / TICKETS[2]).exists():
        (SANDBOX / "epic-tasks" / TICKETS[1]).write_text(TICKET_01.format(status="landed"), encoding="utf-8")
        (SANDBOX / "epic-tasks" / TICKETS[2]).write_text(TICKET_02, encoding="utf-8")
        git(SANDBOX, "add", "-A")
        git(SANDBOX, "commit", "-q", "-m", "round 2: ticket 02")
        (Path("/tmp/contest/notes")).mkdir(parents=True, exist_ok=True)


def agents_from_models(models: str, provider: str = "kenary") -> tuple[AgentSpec, ...]:
    """``--models a:free,b:free`` → a roster of AgentSpecs; the name is the model id
    without its ``:tag``, squeezed to the ``[a-z0-9][a-z0-9_-]*`` a branch needs."""
    specs = []
    for item in filter(None, (m.strip() for m in models.split(","))):
        prov, _, model_id = item.rpartition("/")
        name = "".join(c if c.isalnum() or c in "_-" else "-" for c in model_id.split(":")[0].lower())
        specs.append(AgentSpec(name=name.lstrip("_-"), provider_id=prov or provider, model_id=model_id))
    return tuple(specs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1, choices=sorted(TICKETS))
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--agents", default="", help="comma-separated roster names to keep")
    ap.add_argument("--models", default="",
                    help="comma-separated kenary model ids to run INSTEAD of contest.ini's roster")
    ap.add_argument("--max-parallel", type=int, default=3)
    ap.add_argument("--turn-timeout", type=int, default=600)
    ap.add_argument("--idle-timeout", type=int, default=120)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(message)s")

    out = LIVE / f"out{args.round}"
    ticket = SANDBOX / "epic-tasks" / TICKETS[args.round]
    config = load_roster(REPO / "contest.ini")
    config = replace(config, rounds_dir=str(LIVE / "rounds"), turn_timeout_sec=args.turn_timeout,
                     idle_event_timeout_sec=args.idle_timeout, max_rework=2,
                     max_parallel=args.max_parallel)
    if args.models:
        config = replace(config, agents=agents_from_models(args.models))
    if args.agents:
        keep = args.agents.split(",")
        config = replace(config, agents=tuple(a for a in config.agents if a.name in keep))
    print("agents:", [a.model for a in config.agents], flush=True)

    if args.resume:
        resume = RoundState.from_dict(json.loads((out / "state.json").read_text(encoding="utf-8")))
        workspaces = [run.workspace for run in resume.agents]
    else:
        resume = None
        if not SANDBOX.exists() and args.round != 1:
            build_sandbox(1)
        build_sandbox(args.round)
        workspaces = prepare_round(SANDBOX, config, args.round, "main")
    print("workspaces:", [(w.agent, str(w.path)) for w in workspaces], flush=True)

    out.mkdir(parents=True, exist_ok=True)
    server = KiloServer.spawn(find_kilo_binary(), log_path=str(out / "kilo-serve.log"))
    print("server:", server.base_url, flush=True)
    started = time.monotonic()
    try:
        state = run_round(config, args.round, ticket, workspaces, server=server, out_dir=out, resume=resume)
    finally:
        server.close()
    print(f"round took {time.monotonic() - started:.0f}s")
    for row in state.table_rows():
        print(json.dumps(row, ensure_ascii=False))
    for ws in workspaces:
        log = git(ws.path, "log", "--oneline", f"{ws.base_sha}..HEAD").replace("\n", " | ")
        stat = git(ws.path, "diff", "--stat", f"{ws.base_sha}..HEAD").splitlines()[-1:]
        print(f"-- {ws.agent}: {log!r} {stat}")
    return 0 if all(r["state"] == "READY" for r in state.table_rows()) else 1


if __name__ == "__main__":
    sys.exit(main())
