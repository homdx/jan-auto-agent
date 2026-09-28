#!/usr/bin/env python3
"""KC-42's replay: round 64's recorded event streams, through the first-touch
clock, offline.

    python3 contest-bench/kc42/replay64.py <contest-out/64> [--json]

Round 64 is the round the ticket was written from, and it holds all three cases
plus five counter-examples in its `contest-out/64/<agent>/events.jsonl`:

| agent | how it ended | what it did |
| --- | --- | --- |
| `nex-n2-5-pro` | stalled after 1357 s | 54 695 events inside one `task` subagent; never opened a file |
| `laguna-s-2-1` | error after 759 s | streamed text, provider error; never opened a file |
| `agnes-3-0-flash` | error after 336 s | two permission asks, then the provider errored |
| `glm-4-7-flash` | READY | flat tree behind a live heartbeat, but it *had* touched files |
| the other four | — | wrote code; the clock must stay disarmed |

The clock itself is the runner's own `_FirstTouch`, driven here with a fake
`Workspace`, the recorded stream as its only source of "is the session working",
and the agent's final worktree state as the one sample it is allowed. That is
the point of the exercise: the predicate is *has the worktree changed at all*,
so the replay proves the predicate against real data and cannot prove that a
wedged model reacts to a nudge — `live_probe.py` is that half, and it is run by
hand.

Nothing here starts a server, a subprocess model or a provider. It is not a
test; `tests/test_contest_runner.py` is where the shipped assertions live.

If *round64* does not exist the script says so and exits 2 — it does not invent
data and it does not pass. Point it at a checkout that holds the round.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.contest.runner import _FirstTouch  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

#: the three agents the ticket says the clock must catch, and the five it must
#: not. `touched` is each agent's real outcome for the round — read from the
#: branch and the events, not from this file; it is what the assertion is about.
EXPECTED_DEAD = ("nex-n2-5-pro", "laguna-s-2-1", "agnes-3-0-flash")
EXPECTED_ALIVE = ("agnes-2-5-flash", "glm-4-7-flash", "hy3", "mimo-v2-5",
                  "step-3-7-flash")

#: The committed default. Replayed at the real values, so a change to
#: `contest.ini` shows up here as a different verdict rather than as a test that
#: still agrees with itself.
FIRST_TOUCH_SEC = 420.0
FIRST_TOUCH_NUDGES = 1
SESSIONS_PER_ATTEMPT = 2


class _Recorder:
    """What the replay watches for instead of a real session: the events, the
    aborts and the session replacements a run made, with their timestamps."""

    def __init__(self, events):
        self.events = events
        self.nudges: list = []
        self.resets: list = []
        self.dead = False

    def nudge(self, elapsed: float = 0.0) -> None:
        self.nudges.append(len(self.nudges) + 1)

    def reset(self) -> None:
        self.resets.append(len(self.resets) + 1)

    def kill(self) -> None:
        self.dead = True


class _Workspace:
    """The one predicate input: has anything been touched *yet*.

    Answers from the replay's own clock: false until the scaled offset of the
    agent's first `file.edited` event, true from then on — so `glm-4-7-flash`,
    whose tree was flat for long stretches behind a live heartbeat after it had
    written, is a *disarmed* clock, not merely a not-yet-fired one.
    """

    def __init__(self, touch_at: float | None):
        self.touch_at = touch_at
        self.start = time.monotonic()

    def _touched(self) -> bool:
        return self.touch_at is not None and time.monotonic() - self.start >= self.touch_at


def read_events(path: Path) -> list:
    """`(t, type)` for one `events.jsonl`, oldest first.

    Each line is `{"t": <unix seconds>, "event": {"type": ...}}` as
    `KiloBackend(events_log=...)` writes it. A line without either is skipped:
    a made-up timestamp would decide the verdict.
    """
    out: list = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
            out.append((float(record["t"]), str(record["event"]["type"])))
        except (ValueError, TypeError, KeyError):
            continue
    out.sort(key=lambda pair: pair[0])
    return out


def first_touch_at(events: list) -> float | None:
    """Seconds from the stream's first event to its first `file.edited`, or
    None when the agent never wrote a file. Kilo emits `file.edited` for every
    edit/write tool call, so this is the worktree's first change as the round
    saw it — the one fact the round's output keeps (the worktrees are gone)."""
    if not events:
        return None
    for t, kind in events:
        if kind == "file.edited":
            return t - events[0][0]
    return None


def replay(agent: str, events: list, touch_at: float | None,
           recorder: _Recorder) -> _FirstTouch:
    """Drive one `_FirstTouch` over one recorded turn, with no real clock.

    The watch polls on its own thread and this replay is not a test, so the
    simplest honest driving is the real one: a workspace that answers `touched`
    the way the round ended, and `time.sleep` in place of the real second. The
    recorded stream is what says *how long* the agent chatted without writing,
    so the budget is scaled to it — a 1357 s turn is replayed in a couple of
    seconds and the deadline that mattered live (420 s) is scaled with it, which
    is the only way the predicate can be compared across eight agents whose
    turns ran from 336 s to an hour.
    """
    import tools.contest.runner as runner_mod
    ws = _Workspace(None if touch_at is None else touch_at * 0.001)
    span = max(0.0, events[-1][0] - events[0][0]) if events else 0.0
    # 1000x: a 22-minute turn in ~1.4 s, and 420 s of first-touch in 0.42 s
    scale = 0.001
    budget = FIRST_TOUCH_SEC * scale
    poll = max(0.01, budget / 10.0)
    runner_mod.FIRST_TOUCH_POLL_SEC = poll

    class _Spec:
        name = agent

    watch = _FirstTouch(
        _FakeRun(), ws, _Spec(), None,
        nudge=recorder.nudge, reset=recorder.reset, kill=recorder.kill,
        budget=budget, nudges=FIRST_TOUCH_NUDGES, ceiling=SESSIONS_PER_ATTEMPT)
    watch._has_touched = ws._touched
    ws.start = time.monotonic()
    watch.start()
    # the recorded span, compressed by the same factor, so a turn that ended at
    # 336 s really is shorter than one that ran 1357 s
    time.sleep(max(0.2, span * scale))
    # and then three more deadlines' worth, which is nudge, reset and DEAD
    time.sleep(budget * (FIRST_TOUCH_NUDGES + 3))
    watch.stop()
    return watch


class _FakeRun:
    """The two counters the watch spends, and nothing else."""

    first_touch_nudges_used = 0
    first_touch_resets = 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("round64", type=Path, help="the round's output dir, e.g. contest-out/64")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    round_dir = args.round64
    if not (round_dir / "state.json").exists() and not any(round_dir.glob("*/events.jsonl")):
        print(f"{round_dir} holds no round: no state.json and no agent's events.jsonl",
              file=sys.stderr)
        return 2

    rows = []
    for agent in sorted(p.name for p in round_dir.iterdir() if p.is_dir()):
        events_path = round_dir / agent / "events.jsonl"
        if not events_path.exists():
            continue
        events = read_events(events_path)
        touch_at = first_touch_at(events)
        touched = touch_at is not None
        if not events:
            rows.append({"agent": agent, "events": 0, "verdict": "cannot say"})
            continue
        recorder = _Recorder(events)
        replay(agent, events, touch_at, recorder)
        dead = recorder.dead
        want_dead = agent in EXPECTED_DEAD
        rows.append({
            "agent": agent, "events": len(events), "touched": touched,
            "nudges": len(recorder.nudges), "resets": len(recorder.resets),
            "verdict": "DEAD" if dead else "alive",
            "expected": "DEAD" if want_dead else "alive",
            "ok": dead == want_dead,
        })

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(f"{'agent':32} {'events':>8} {'touched':>8} {'nudge':>6} {'reset':>6}  verdict")
        for row in rows:
            if row["verdict"] == "cannot say":
                print(f"{row['agent']:32} {row['events']:>8} {'?':>8} {'-':>6} {'-':>6}  cannot say")
                continue
            flag = "ok" if row.get("ok") else "MISS"
            print(f"{row['agent']:32} {row['events']:>8} {str(row['touched']):>8} "
                  f"{row['nudges']:>6} {row['resets']:>6}  {row['verdict']} (want "
                  f"{row['expected']}) {flag}")
    missed = [r for r in rows if r.get("ok") is False]
    unknown = [r for r in rows if r["verdict"] == "cannot say"]
    if unknown:
        print(f"\n{len(unknown)} agent(s) the round cannot classify — the replay says so "
              f"rather than guessing", file=sys.stderr)
    if missed:
        print(f"\n{len(missed)} agent(s) disagree with the ticket's expectation", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
