# 193 — `gates._end_process_group` returns when the group's leader is gone, with members still running

**Status:** landed
**Severity:** MEDIUM
**File:** tools/contest/gates.py
**Symbol:** _end_process_group, _end_group_members
**Round:** 193
**Size:** XS
**Also touches:** tests/test_contest_gates_end_process_group.py

Found while judging round 184 (2026-10-06) with `arena run judge`'s cross phase.

## What the matrix showed

Ticket 184 changes one file, `tests/test_contest_harvest.py`. Every entry's code column is the same
code plus its own copy of that test file, so a cell differs from another only by the test and by noise.
The cross matrix had two classes of failing test, spread over every column alike, the base's and
the ideal's included:

| failure | cells (loaded box, `--jobs 2`) | cells (quiet box, `--jobs 1`) |
|---|---|---|
| `Failed: the harvest's pytest process group is still alive` | 6–9 per column | 4–9 per column |
| `assert 'test_slow_kc57.py::test_slow_kc57' in '… the budget hit in: tests/test_probe.py::test_probe'` | 4–8 per column | 3–7 per column |

The second class is the entries' own tests asserting the slow test's name unconditionally. Ticket
184 says the name is only there when a marker says the nested pytest reached the test (the ideal does
that). It is a flaw of those entries' tests, not of any code, and it is not this ticket.

The first class is a defect of the code under test, the same in every column. Run twice, on a
loaded and a quiet box, different cells failed each time, and the same test run by hand on the
ideal passed 3/3. It is a race.

## The bug

`_end_process_group(proc)` sends TERM to the group, then `proc.wait(timeout=2.0)`. `proc.wait`
returns when the group's **leader** (the harvest's pytest) has exited. The loop then breaks, and KILL
is never sent. A member of the group that is still inside its own TERM handling, or that ignores
TERM, outlives the leader. The harvest returns with a process of the suite still running. The
docstring and the harvest budget's contract are "nothing of the suite is left running".

## How to reproduce (offline, deterministic)

```python
import os, subprocess, sys, time
from tools.contest import gates
child = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)"
leader = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(60)"
proc = subprocess.Popen([sys.executable, "-c", leader], start_new_session=True)
time.sleep(1)
gates._end_process_group(proc)
os.killpg(proc.pid, 0)      # no error: the group is alive
```

The leader dies on TERM, `_end_process_group` returns in 0.0 s, and `os.killpg(pgid, 0)` succeeds.

## Fix

After the TERM/KILL loop, `_end_group_members(pgid)` sweeps the group: while `killpg(pgid, 0)` says
it exists, send KILL and look again every 20 ms, for at most 2 s, and stop at the first `OSError`
(fail-open, like the rest). While a group exists its id is not reused, so the sweep signals the
group's own members.

## Tests (`tests/test_contest_gates_end_process_group.py`)

1. A leader that dies on TERM and a deaf member: after the end the group is empty, within 5 s.
   Fails on the old code.
2. A deaf leader and a deaf member: both ended (old behaviour, kept).
3. A group that ends on TERM is not waited on: back in under 1 s.
4. A group that is already gone: no error.

Mutation: the sweep sending `SIGCONT` instead of `SIGKILL` fails test 1.

## Seen while judging, not in this ticket

- `arena run judge` ran round 184's cells at `--jobs 2` and the box's load went to 27. The
  tests of this ticket's own subject assert a 3 s budget, so a cell beside another cell is the load
  that breaks them; the verb defaults to `--jobs 1` and says so when the load is above the core
  count (AR-64).
- Under `--jobs 1` a 12 × 11 cross of this round took 93 minutes (the entries' tests of
  agnes-2-5-flash and sensenova-6-8-flash-lite-var1 take 65 s and 78 s, against 14 s for the rest).
