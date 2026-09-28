#!/usr/bin/env python3
"""KC-40's live probe: can a nearly-full free-tier model still write a
truthful account of the diff it left behind?

`tests/test_contest_runner_summary.py` proves the ordering — the ask goes out
before any `summarize`, the reply is copied to `<agent>.summary.md`, an empty
reply or a `session.error` falls back to KC-39's fresh session with the worktree
lines, a captured summary is carried into a later reset, and a second session of
the same attempt appends rather than overwrites. Whether a real model past the
middle of a 20 000-token window can still say *what it changed, why, and what is
left* is empirical, and this is the probe that answers it:

  1. a ticket that makes one small real edit to `pkg/thing.py` and leaves it
     **uncommitted** — the diff the summary is supposed to explain — then asks
     for blocks of about a thousand `a` characters, the operator's own example,
     to burn context fast;
  2. `context_limit_fallback` / `summary_at_percent` / `compact_at_percent`
     sized down (20 000 / 55 / 85) so the edge trips in minutes instead of by
     waiting on a real 32 768 window. Not lower: the ask is checked when a turn
     ends, and one Kilo step is already 12–16 k tokens, so at 4 000 the first
     turn ends past 100 % and the ask is skipped (round 1 of the probe);
  3. the runner over the real `kilo serve`, on the roster of `contest.ini` or on
     the two models of `--models`;
  4. afterwards, one block per agent: the summary the session wrote, the diff it
     was written about, and the turns that asked for it.

    python3 contest-bench/kc40/live_probe.py
    python3 contest-bench/kc40/live_probe.py --models agnes-2-5-flash:free,glm-4-7-flash:free
    python3 contest-bench/kc40/live_probe.py --budget 20000 --ask 55 --compact 85
    python3 contest-bench/kc40/live_probe.py --round 2      # reuse the worktrees

The judge reads the summary and the diff side by side and says whether the
account is truthful. A fluent summary that is unrelated to the actual diff is a
failure, and a plumbing success with a useless or hallucinated summary does not
close the ticket — the verdicts go into RESULTS.md by hand.

Requires the Kilo VS Code extension's `bin/kilo` (`find_kilo_binary`) and the
kenary provider signed in there. Nothing here is a test; it costs real
(free-tier) calls.
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
os.environ.setdefault("CONTEST_GATE_API_KEY", "unset-for-the-probe")

from tools.contest.backend import KiloBackend  # noqa: E402
from tools.contest.kilo_client import KiloServer, find_kilo_binary  # noqa: E402
from tools.contest.roster import AgentSpec, load_roster  # noqa: E402
from tools.contest.runner import RoundState, run_round  # noqa: E402
from tools.contest.workspace import prepare_round  # noqa: E402

LIVE = Path("/tmp/contest/live-kc40")
SANDBOX = LIVE / "repo"
TICKET = "01-total-ignores-non-numbers.md"

BRIDGE = '''"""stub for the probe's ground rule"""


class CollectBridge:
    def _shrink(self, raw: str) -> str:
        out = raw.strip()
        return out[:10]
'''
THING = '''"""pkg/thing.py — a tiny module for the KC-40 probe."""


def total(values):
    """Return the sum of *values*.

    Non-numeric elements are accepted today and concatenated, which is what
    `total(["a"])` turns into `"a"` instead of an error.
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
"""
TICKET_BODY = """# 01 — `pkg/thing.py`: `total(["a"])` must raise `TypeError`

**Status:** open — the KC-40 probe, round 79.
**Severity:** LOW
**File:** `pkg/thing.py`
**Symbol:** `total`
**Round:** 1
**Size:** S

## What must change

`total(values)` raises `TypeError` for a non-numeric element instead of
concatenating it. Build it on `sum` with a start of 0; do not special-case
strings by hand.

## How to work (this probe's procedure — follow it literally)

1. Make the change in `pkg/thing.py` and **leave it uncommitted**: never
   `git add` and never `git commit`. The probe needs an uncommitted diff, and
   it is the diff the summary of step 3 is about.
2. Do not write a test file, and do not record the ticket.
3. Then keep replying with a block of about 1 000 `a` characters, one block per
   reply, until you are told to stop. Change no file while doing that — if
   asked, write an account of the work in the chat instead, and change no file
   in that reply either.
""" + GROUND


def git(cwd, *args) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def build_sandbox() -> None:
    """The sandbox repo: one file to edit, one test that keeps it honest."""
    shutil.rmtree(SANDBOX, ignore_errors=True)
    SANDBOX.mkdir(parents=True)
    git(SANDBOX, "init", "-q", "-b", "main")
    git(SANDBOX, "config", "user.email", "live@example.invalid")
    git(SANDBOX, "config", "user.name", "live")
    for rel, text in {
        "tools/auto/collect_bridge.py": BRIDGE,
        "pkg/__init__.py": "",
        "pkg/thing.py": THING,
        "tests/__init__.py": "",
        "tests/test_base.py": TEST_BASE,
        "epic-tasks/" + TICKET: TICKET_BODY,
        ".gitignore": "runs/\n__pycache__/\n.pytest_cache/\n",
    }.items():
        (SANDBOX / rel).parent.mkdir(parents=True, exist_ok=True)
        (SANDBOX / rel).write_text(text, encoding="utf-8")
    git(SANDBOX, "add", "-A")
    git(SANDBOX, "commit", "-q", "-m", "base")


def agents_from_models(models: str, provider: str = "kenary") -> tuple[AgentSpec, ...]:
    """``--models a:free,b:free`` → a roster of AgentSpecs; the name is the model
    id without its ``:tag``, squeezed to what a branch name allows."""
    specs = []
    for item in filter(None, (m.strip() for m in models.split(","))):
        prov, _, model_id = item.rpartition("/")
        name = "".join(c if c.isalnum() or c in "_-" else "-" for c in model_id.split(":")[0].lower())
        specs.append(AgentSpec(name=name.lstrip("_-"), provider_id=prov or provider, model_id=model_id))
    return tuple(specs)


def report(out_dir: Path, workspace) -> None:
    """One block per agent: the summary, the diff it was about, the asks.

    The runner's own fields are printed as they were written, so the judge can
    read the summary and the diff side by side and decide whether the account is
    truthful — that decision is written into RESULTS.md by hand.
    """
    state = json.loads((out_dir / "state.json").read_text(encoding="utf-8"))
    run = next((r for r in state["agents"] if r["workspace"]["agent"] == workspace.agent), None)
    if run is None:
        print(f"-- {workspace.agent}: no run in {out_dir / 'state.json'}")
        return
    name = run["agent"]["name"]
    model = run["agent"]["provider_id"] + "/" + run["agent"]["model_id"]
    summary_path = out_dir / f"{workspace.agent}.summary.md"
    asks = [t for t in run["turns"] if t.get("summary_attempted")]
    print(f"-- {name} ({model}): {run['state']} — "
          f"summaries={run['summaries']} compactions={run['compactions']} "
          f"sessions={run['sessions']} continues={run['continues']}")
    for turn in asks:
        print(f"   ask  attempt={turn['attempt']} kind={turn['kind']} "
              f"fill={turn.get('summary_fill')}% of the ask at {turn.get('summary_at')}% "
              f"outcome={turn.get('summary_outcome')} "
              f"captured={turn.get('summary_captured')} {turn.get('summary_sec', 0)}s")
    if summary_path.exists():
        text = summary_path.read_text(encoding="utf-8")
        print(f"   file {summary_path} ({len(text)} chars)")
        print("   summary:")
        for line in text.splitlines():
            print(f"     | {line}")
    else:
        print("   file (no <agent>.summary.md — the ask never produced a reply)")
        if run["summary"]:
            print("   run.summary (carried but never captured):")
            for line in run["summary"].splitlines():
                print(f"     | {line}")
    try:
        diff = git(workspace.path, "diff", "--no-ext-diff", workspace.base_sha)
    except SystemExit:
        diff = "(git diff failed)"
    print(f"   diff vs {workspace.base_sha[:12]} (committed and uncommitted):")
    for line in diff.splitlines():
        print(f"     | {line}")
    log = git(workspace.path, "log", "--oneline", f"{workspace.base_sha}..HEAD")
    print(f"   commits above base: {log or 'none (the diff is uncommitted)'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--reset", action="store_true",
                    help="rebuild the sandbox repo (the default is to reuse it)")
    ap.add_argument("--agents", default="", help="comma-separated roster names to keep")
    ap.add_argument("--models", default="",
                    help="comma-separated kenary model ids to run INSTEAD of contest.ini's roster")
    ap.add_argument("--max-parallel", type=int, default=2)
    ap.add_argument("--budget", type=int, default=20_000,
                    help="`context_limit_fallback`: the window a model with no "
                         "declared limit is sized against; below one Kilo step "
                         "(12-16k) the first turn ends past 100%% and is never asked")
    ap.add_argument("--ask", type=int, default=55,
                    help="`summary_at_percent`: the fill at which the summary is asked for")
    ap.add_argument("--compact", type=int, default=85,
                    help="`compact_at_percent`: must sit above `--ask`, or the "
                         "compact would take the session first")
    ap.add_argument("--max-rework", type=int, default=4)
    ap.add_argument("--turn-timeout", type=int, default=600)
    ap.add_argument("--idle-timeout", type=int, default=180)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(message)s")

    out = LIVE / f"out{args.round}"
    config = replace(
        load_roster(REPO / "contest.ini"),
        rounds_dir=str(LIVE / "rounds"),
        context_limit_fallback=args.budget,
        summary_at_percent=float(args.ask),
        compact_at_percent=float(args.compact),
        max_rework=args.max_rework,
        max_parallel=args.max_parallel,
        turn_timeout_sec=args.turn_timeout,
        idle_event_timeout_sec=args.idle_timeout,
    )
    if args.models:
        config = replace(config, agents=agents_from_models(args.models))
    if args.agents:
        keep = args.agents.split(",")
        config = replace(config, agents=tuple(a for a in config.agents if a.name in keep))
    print(f"agents: {[a.model for a in config.agents]} — budget={args.budget} "
          f"ask={args.ask}% compact={args.compact}%", flush=True)

    if args.reset or not SANDBOX.exists():
        build_sandbox()
    workspaces = prepare_round(SANDBOX, config, args.round, "main")
    print(f"workspaces: {[(w.agent, str(w.path)) for w in workspaces]}", flush=True)

    out.mkdir(parents=True, exist_ok=True)
    server = KiloServer.spawn(find_kilo_binary(), log_path=str(out / "kilo-serve.log"))
    print(f"server: {server.base_url}", flush=True)
    started = time.monotonic()

    def make_backend(workspace):
        # one `KiloBackend` per worktree over the shared server, as `cmd_run`
        # builds them (`cli._make_backends`)
        return KiloBackend(server, str(workspace.path),
                           events_log=str(out / workspace.agent / "events.jsonl"))
    try:
        state = run_round(config, args.round, SANDBOX / "epic-tasks" / TICKET,
                          workspaces, make_backend=make_backend, out_dir=out)
    finally:
        server.close()
    print(f"round took {time.monotonic() - started:.0f}s")
    for row in state.table_rows():
        print(json.dumps(row, ensure_ascii=False))
    print()
    for ws in workspaces:
        report(out, ws)
    return 0 if any(Path(out, f"{ws.agent}.summary.md").exists() for ws in workspaces) else 1


if __name__ == "__main__":
    sys.exit(main())
