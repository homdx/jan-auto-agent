# FL-6 — a counted keep-alive pulse idles before the work it covers, and a turn that did everything comes back `GAVE_UP — no_progress_row`

**Status:** landed (2026-09-24, by hand — the `FL-6:` commit right before this ticket). Found the same day in a full `pytest tests -n 6` of the KC-27 merge: one red, `test_events_of_the_session_keep_a_turn_alive`, 5/5 green alone.
**Severity:** MEDIUM (a red full suite that is not a regression. It costs a stress run to tell apart, and the same shape sits in a second test)
**File:** `tests/test_contest_runner.py`
**Symbol:** `_BenchFake.pulse`, `test_events_of_the_session_keep_a_turn_alive`, `test_a_silent_agent_stalls_next_to_a_chatty_one`
**Round:** 100
**Size:** XS
**Depends on:** FL-1 (84, `e500d40`), which named the bet in `pulse`'s docstring. FL-4 (87) is the checker that should learn this shape (see "For FL-4").

---

## What happened

```
FAILED tests/test_contest_runner.py::test_events_of_the_session_keep_a_turn_alive
AssertionError: (<AgentState.GAVE_UP: 'GAVE_UP'>, 'REWORK after the last attempt: no_progress_row')
assert <AgentState.GAVE_UP: 'GAVE_UP'> is <AgentState.READY: 'READY'>
```

The test puts two things into one `on_prompt` hook:

```python
fake.pulse(session_id, KEEPALIVE_BEAT_S, CHATTY_BEATS, then_idle=True),  # 25 × 0.2 s
work_ready(d, t)                                                         # git add / merge-base / log / commit / rev-parse, then the PROGRESS.csv row
```

`FakeKiloServer._run_turn` runs the hook on its own thread, and
`prompt_async` has already returned. `pulse` beats on another thread and
emits `session.idle` when its count runs out: about 5 s after the prompt,
whatever the hook is doing. `wait_idle` returns on that idle and the harvest
reads `runs/agent-a/PROGRESS.csv`. On an idle box `work_ready` finishes in
well under a second and the row is there. On a loaded box the five `git`
calls take longer than 5 s, so the harvest finds no row. The reworks see the
same thing, and a turn that committed and claimed comes back `GAVE_UP`.

`test_a_silent_agent_stalls_next_to_a_chatty_one` has the same shape with a
tighter count: the neighbour `agent-b` beats `NEIGHBOUR_BEATS = 10` (2 s),
idles, and must be READY.

`pulse`'s own docstring (FL-1) already named the bet, "on how long a git
commit takes", for the *beats*. These two tests make the same bet on the
*idle*.

## Reproduce

**Deterministically, on any box.** Delay the hook's `_claim` past the count;
the error is the exact one above:

```python
import time, pytest, test_contest_runner as m

@pytest.mark.parametrize("delay", [0.0, 6.0])
def test_probe_keep_alive(delay, tmp_path, monkeypatch):
    real = m._claim
    monkeypatch.setattr(m, "_claim", lambda *a, **k: (time.sleep(delay), real(*a, **k))[1])
    m.test_events_of_the_session_keep_a_turn_alive(tmp_path)

@pytest.mark.parametrize("delay", [0.0, 3.0])
def test_probe_chatty_neighbour(delay, tmp_path, monkeypatch):
    real = m._claim
    monkeypatch.setattr(m, "_claim", lambda *a, **k: (time.sleep(delay), real(*a, **k))[1])
    m.test_a_silent_agent_stalls_next_to_a_chatty_one(tmp_path)
```

(put it in `tests/`, run it with `-n 0`, and delete it)

| probe | before | after |
|---|---|---|
| keep-alive, +0 s | pass | pass |
| keep-alive, **+6 s** | **FAIL** `GAVE_UP — no_progress_row` | pass |
| chatty neighbour, +0 s | pass | pass |
| chatty neighbour, **+3 s** | **FAIL** `GAVE_UP — no_progress_row` | pass |

**What does not reproduce it, and why.**
- A `git` shim on `PATH` that sleeps before every call, at +0.5, +1 and +1.5 s:
  it slows the harvest's own `git` as much as the hook's, so the harvest
  still reads the row late enough.
- 8 and 16 CPU burners (`while True`) × 10 serial runs: 10/10 green. Plain CPU
  load is not enough on its own; the hook's `git` has to be slow relative to
  the pulse's `time.sleep`.

**Under real load.** It is rare. On the unfixed tree (`da4e257`), 22 full runs never hit it:

| load | runs | FL-6 reds |
|---|---|---|
| `pytest tests -n 2` | 2 | 0 |
| `pytest tests -n 4` | 2 | 0 |
| `pytest tests -n 6` | 2 | 0 |
| `pytest tests -n 8` | 2 | 0 |
| `tests -n 4` & `tests_bugfix -n 4`, concurrently | 2 | 0 |
| `tests -n 6` & `tests_bugfix -n 6`, concurrently | 2 | 0 |
| `tests -n 8` & `tests_bugfix -n 8`, concurrently | 2 | 0 |
| CPU burners ×8 / ×16, the test 10× serially each | 20 | 0 |

The one red came under more than all of these: `tests -n 6` alongside two
other full-suite runs and a judge's pytest batch, load average about 20 on
this 4-core/8-thread i5-1135G7. No worker count reproduces it on demand, so a
stress run is the wrong tool for it. **Use the deterministic probe above**:
it fails every time, in seconds.

## The fix (landed)

`_BenchFake.pulse` gains `idle_after: threading.Event | None`. Past its count
the pulse keeps beating until the event is set, then idles. Both tests set the
event in a `finally` after `work_ready`. The count is now the least the
session chats; the idle never comes before the work, and a real agent's never
does.

- No window, beat interval, count or assertion changed. Nothing outside the
  bench moved.
- `test_a_counted_pulse_idles_only_after_the_work_it_covers` pins the
  helper's contract without a real race: 3 counted beats, at least 6 seen, no
  idle until the event, then the idle is the last event. It is red on the old
  `pulse`, which idled after the third beat.
- `tests/test_contest_runner.py` 77/77, `sync_test_tiers --check` clean.

## For FL-4

FL-4's `scripts/check_test_clocks.py` looks for three wall-clock shapes. This
is a fourth: **a counted timer that ends the turn (`then_idle=True`, or any
emit of `session.idle` on its own thread) in the same hook as real work.** It
is safe only if the idle waits on the work (`idle_after=`). The check: a
`pulse(..., then_idle=True)` call without `idle_after=` in a function that
also calls `work_*` or `_git`.
