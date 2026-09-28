#!/usr/bin/env python3
"""KC-42's live probe: does a wedged model actually react to a nudge?

    python3 contest-bench/kc42/live_probe.py --models laguna-s-2-1:free,agnes-3-0-flash:free
    python3 contest-bench/kc42/live_probe.py --first-touch 90

`replay64.py` proves the predicate — a worktree that never changed, against
round 64's real streams. It cannot prove that a model which *can* write chooses
to. This does, against a real `kilo serve` and a real roster: the ticket's
first instruction is a tool call that cannot succeed, so a model that follows
it spends its turn unable to write anything, which is exactly the shape the
clock is for.

What to confirm by hand, in this order:

1. the nudge reaches the session — a `prompt_async` beyond the first one, into
   the *same* `session_id`, and a `Nothing has been modified` line in
   `<out>/<agent>/events.jsonl` or the run's own log;
2. the reset produces a **second** `session_id` in `<out>/state.json`, so the
   slot's escalation really opened a session and not just logged about one;
3. a `DEAD` agent stops costing wall time — the round's `ended` minus `started`
   is close to the clock's own budget, not `turn_timeout_sec`, and the other
   agents' rows are unaffected.

Requires the Kilo VS Code extension's `bin/kilo` and the kenary provider
signed in there. This is not a test: it costs real (free-tier) calls, which is
why it lives in `contest-bench/` and is run by hand.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-probe")

from tools.contest.kilo_client import KiloServer  # noqa: E402
from tools.contest.roster import AgentSpec, ContestConfig, load_roster  # noqa: E402
from tools.contest.runner import run_round  # noqa: E402
from tools.contest.workspace import Workspace, prepare_round  # noqa: E402

LIVE = Path("/tmp/contest/live-kc42")
SANDBOX = LIVE / "repo"

BRIDGE = '''"""stub for the round's ground rule"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        out = raw.strip()
        return out[:10]
'''
THING = '''"""pkg/thing.py — the file the wedged ticket cannot get to."""


def total(values):
    """Return the sum of *values*."""
    return sum(values)
'''
TEST_BASE = "from pkg.thing import total\n\n\ndef test_total_two():\n    assert total([1, 2]) == 3\n"

#: The ticket. Its first instruction is a command that cannot succeed — a
#: `kilo` subcommand that does not exist — and it is asked for *before* the
#: change, so a model that follows it literally cannot get to `pkg/thing.py` at
#: all. That is the shape of round 64's `nex-n2-5-pro`, produced on purpose
#: instead of waited for.
TICKET = """# 01 — `pkg/thing.py`: `total([])` must return 0

**Status:** open — KC-42's live probe.
**Severity:** LOW
**File:** `pkg/thing.py`
**Symbol:** `total`
**Round:** 1
**Size:** S
**Also touches:** `tests/test_total_empty.py` (new)

## How to work (this round's procedure — follow it literally)

1. **First**, and before anything else, run this and read what it prints:

   ```bash
   kilo config set --profile kc42-probe nothing-here
   ```

2. Only after that command has come back, edit `pkg/thing.py` so `total([])`
   returns `0` rather than raising, add `tests/test_total_empty.py` with a
   test for it, and commit both.

## Acceptance

- [ ] `tests/test_total_empty.py` (new): `total([]) == 0`.
- [ ] `python3 -m pytest tests -q` green.

## Ground rules

- Do not touch `CollectBridge._shrink` in `tools/auto/collect_bridge.py`.
- Do not edit `epic-tasks/`.
- Never `git push`.
- A test ships with the change and fails without it.
"""


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def build_sandbox() -> None:
    shutil.rmtree(LIVE, ignore_errors=True)
    SANDBOX.mkdir(parents=True)
    git(SANDBOX, "init", "-q", "-b", "main")
    git(SANDBOX, "config", "user.email", "probe@example.invalid")
    git(SANDBOX, "config", "user.name", "probe")
    for name in ("next_task.py", "append_task.py"):
        (SANDBOX / "scripts").mkdir(exist_ok=True)
        shutil.copy(REPO / "scripts" / name, SANDBOX / "scripts" / name)
    files = {
        "tools/auto/collect_bridge.py": BRIDGE,
        "pkg/__init__.py": "",
        "pkg/thing.py": THING,
        "tests/__init__.py": "",
        "tests/test_base.py": TEST_BASE,
        "epic-tasks/01-total-empty.md": TICKET,
        ".gitignore": "runs/\n__pycache__/\n.pytest_cache/\n",
    }
    for rel, text in files.items():
        (SANDBOX / rel).parent.mkdir(parents=True, exist_ok=True)
        (SANDBOX / rel).write_text(text, encoding="utf-8")
    git(SANDBOX, "add", "-A")
    git(SANDBOX, "commit", "-q", "-m", "base")


def agents_from_models(models: str, provider: str = "kenary") -> tuple:
    specs = []
    for item in filter(None, (m.strip() for m in models.split(","))):
        prov, _, model_id = item.rpartition("/")
        name = "".join(c if c.isalnum() or c in "_-" else "-"
                       for c in model_id.split(":")[0].lower())
        specs.append(AgentSpec(name=name.lstrip("_-"), provider_id=prov or provider,
                               model_id=model_id))
    return tuple(specs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="laguna-s-2-1:free,agnes-3-0-flash:free",
                    help="at least two roster models; a wedged one is the point")
    ap.add_argument("--first-touch", type=float, default=90.0,
                    help="first_touch_sec, cut well below the turn so the clock bites")
    ap.add_argument("--nudges", type=int, default=1)
    ap.add_argument("--turn", type=int, default=900, help="turn_timeout_sec")
    ap.add_argument("--out", type=Path, default=LIVE / "out")
    args = ap.parse_args()

    build_sandbox()
    config = load_roster(REPO / "contest.ini")
    config.agents = agents_from_models(args.models) or config.agents
    if len(config.agents) < 2:
        print("the probe is about a slot the clock reclaims: name at least two models",
              file=sys.stderr)
        return 2
    config.max_parallel = len(config.agents)
    config.max_rework = 0
    config.max_continues_per_attempt = 0
    config.max_sessions_per_attempt = 2
    config.run_tests = False
    # the whole point of the probe
    config.first_touch_sec = float(args.first_touch)
    config.first_touch_nudges = int(args.nudges)
    config.turn_timeout_sec = int(args.turn)
    config.turn_extend_sec = 0
    config.idle_event_timeout_sec = max(120, int(args.turn))
    config.agent_max_sec = 0
    config.gate_max_calls_per_session = 0
    config.deadline_commit = False

    ticket = SANDBOX / "epic-tasks" / "01-total-empty.md"
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    workspaces = prepare_round(config, 1, ticket, out_dir=out)
    server = KiloServer.spawn(config)

    def make_backend(ws: Workspace):
        backend = KiloBackendFor(server, str(ws.path), str(out / ws.agent / "events.jsonl"))
        return backend

    try:
        started = time.time()
        state = run_round(config, 1, ticket, workspaces, make_backend=make_backend,
                          out_dir=out)
        ended = time.time()
    finally:
        server.close()

    print(f"\nround wall time: {ended - started:.0f}s "
          f"(first_touch_sec={config.first_touch_sec:g}, turn_timeout_sec={config.turn_timeout_sec})")
    print("\n1. the nudge reached the session, and 2. the reset opened a second session:")
    for run in state.agents:
        rows = []
        turns = out / run.agent.name / "turns.jsonl"
        if turns.exists():
            for line in turns.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if row.get("first_touch_nudges") or row.get("new_session") \
                        or row.get("first_touch_dead"):
                    rows.append({k: row[k] for k in
                                 ("kind", "session_id", "new_session", "session_reason",
                                  "first_touch_nudges", "first_touch_reset",
                                  "first_touch_dead") if k in row})
        saved = json.loads((out / "state.json").read_text(encoding="utf-8"))
        for agent in saved.get("agents") or ():
            if agent.get("name") == run.agent.name:
                print(f"  {run.agent.name}: state={run.state.value} "
                      f"sessions={agent.get('sessions')} session_id={agent.get('session_id')}")
        for row in rows:
            print(f"    {row}")
    print("\n3. a DEAD agent costs the round no wall time: compare the wall time above "
          "with\n   turn_timeout_sec — a DEAD slot is gone, a STALLED one waited the "
          "turn out.")
    print(f"\nartifacts: {out}")
    return 0


def KiloBackendFor(server, directory, events_log):
    from tools.contest.backend import KiloBackend
    return KiloBackend(server, directory, events_log=events_log)


if __name__ == "__main__":
    raise SystemExit(main())
