# FL-1 — postmortem: eleven causes behind one flaky suite

**Ticket:** `epic-tasks/84-fl-1-full-tests-suite-is-not-reproducible-green-under-n-4-three-independent-root-causes.md`
**Round:** 84
**Commits:** `e500d40` (first fix), `89012d8` (everything the stress loop found afterwards)
**Stress command** — 4 suites at once, `-n 8` each, so **32 worker processes on 8 cores** (~4× CPU oversubscription):

```bash
pytest tests -n 8 & pytest tests_bugfix -n 8 & pytest tests -n 8 & pytest tests_bugfix -n 8 &
```

---

## Prologue — a suite that fails differently every time

A flaky test is not a failing test. A failing test is a fact: it fails, you read it, you fix
it. A flaky test is a *rumour*. It fails, you re-run, it passes, and now you have to decide
what you just learned — and the honest answer is usually "nothing".

This suite had ten of them at once, and it behaved accordingly:

```
  19 consecutive runs of `pytest tests -n 4`
  ────────────────────────────────────────────────────────────────
   8 red, 11 green                                       (42 % red)
   and a DIFFERENT test lost in almost every red run:

     TestThreadSafety::test_concurrent_record_gate2_consistent_total   ×4
     test_a_silent_agent_stalls_next_to_a_chatty_one                   ×3
     TestRewriteGateContextSatisfied::test_rewriter_called…            ×3
     test_three_questions_in_one_turn_stall_and_abort                  ×1
     test_idle_event_timeout_stalls_a_silent_session                   ×1
     test_a_stalled_turn_with_a_valid_commit_is_harvested_to_ready     ×1
```

That spread is the first clue, and it is worth pausing on. One flaky test that fails 40 % of
the time is one bug. *Six* tests, in four unrelated modules, each failing occasionally —
that is not one bug being seen from six angles. It is independent causes, and there is no
way to know how many until you have fixed the last one.

The ticket said three. It was right about the three it named, and it named them well: a
lock held across an `fsync`, a `MagicMock` that coerces to `1.0`, and a set of wall-clock
margins that a loaded box walks straight through. Anyone who reads that ticket comes away
thinking this is an afternoon's work.

Five models had already tried. Their patches sat in `kc37/`, each with a test log attached,
and **not one of the five produced a green suite**.

So there are three good questions on the table before a single line is written:

1. **Why did five capable attempts all fail** at a problem whose causes are written down?
2. **Why did each of them believe it had succeeded?** — because each of them *did* see the
   suite go green, more than once, and shipped on that.
3. **What is at the bottom?** The ticket names three causes. This document ends at ten, and
   the tenth one is the reason the other rounds kept coming back.

The short version of the answer, for anyone who wants it before the detail:

> Every one of these failures is a statement about **time** — a margin, a deadline, a
> window, an ordering. There are exactly four shapes of that statement in this suite, and
> **they take four different fixes**. The reflex that fixes the first shape ("widen the
> margin") is *actively useless* on the fourth, and the fourth is where the real bug was
> hiding. I applied that reflex to the fourth shape four separate times before the stress
> loop stopped letting me.

And underneath all of it, in production code nobody suspected, a single line:

```python
log.flush()      # on the reader thread, once per event
```

The event tap — the thing that delivers events to the silence clock — was blocking on the
disk between every event it delivered. Under load it delivered them **seconds** late. The
clock, which measures from delivery, called that a session going quiet and aborted a
working agent. Six rounds of widening test margins were, in effect, six attempts to
out-wait a `write(2)`.

That line is fixed in `89012d8`. What follows is how long it took to see it, and what else
was in the way.

---

## Summary — the ten, on one page

Six rounds. Each one: fix, stress, read the single red run, discover that the previous
fix had been hiding the next cause. Four of the ten are production bugs; two of those were
in code nobody suspected, inside tests that had already been written off as flaky.

Final state: **48 consecutive suite runs green** — 186 072 test executions, 1 h 40 min of
wall clock, 5.9 h of summed run time, zero failures. Every guard verified by *injecting the
regression it exists to catch*, not merely by going green.

| # | Cause | Where | Kind |
|---|---|---|---|
| A | A process-wide lock held across a blocking `fsync` | `tools/auto/auto_metrics.py` + `tools/metrics_collector.py` | **production** |
| B | `float(MagicMock())` is `1.0`, installing a 1-second budget nobody set | `tools/auto/outer_loop.py` | **production** |
| C1 | Wall-clock bounds with a sub-second margin; upper bounds measured around unbounded setup | contest tests | test |
| C2 | A *counted* heartbeat is a bet on its total length, not just its interval | `tests/test_contest_runner.py` | fixture |
| C3 | The gap that matters is *inside* the scenario hook, not around it | `tests/_kilo_fake.py` | fixture |
| C4 | Beats emitted before the tap connected are silently dropped | `tests/test_contest_runner.py` | fixture |
| C5 | **`EventTap` starved the clock it feeds**: per-event `flush()` + a 200 ms poll | `tools/contest/kilo_client.py` | **production** |
| C6 | A real `git commit` inside a wall-clock silence window — unfixable in that shape | contest tests | test design |
| G | `.git/index.lock` contention treated as a hard failure instead of a transient | `tools/auto/git_manager.py` | **production** |
| S | A 10 s client timeout against a stub server whose `serve_forever` had not been scheduled | `tests/test_collect_ab_harness.py` | test |
| T | A 30 s **turn deadline** over 58 turns that each run a real `git commit` | `tests/test_contest_runner.py` | test |

---

## 1. The symptom, and why it was so easy to get wrong

The rotating cast of failures in the Prologue is the diagnostic fingerprint: not one flaky
test seen from six angles, but six tests failing for reasons that have nothing to do with
each other. That much was readable from the ticket.

What was *not* readable — and what cost five candidates their submissions and me six
rounds — is the second-order problem. In a suite like this, **the evidence itself is
unreliable**, in both directions. A green run does not mean fixed. A red test does not mean
you broke it. Every decision in this ticket had to be made through that fog, and the two
sections below are about what the fog costs.

### 1.1 Why a green run was not evidence — and why every model believed it was done

This is the trap the whole ticket sits in, and **every candidate fell into it honestly**.

Read the Prologue's numbers again, but from a candidate's chair this time. The 19-run loop
is 8 red — which means it is also **11 green**. The ticket ran a second loop and got 3 red
out of 12:

```
first loop:   19 runs at -n 4 →  8 red, 11 green   (42 % red)
second loop:  12 runs at -n 4 →  3 red,  9 green   (25 % red)
```

So a model that ran the suite once, saw green, and concluded "fixed" was right to feel
confident — it had a **58–75 % chance of seeing green whether or not it had fixed
anything**. Several candidates did exactly that and shipped. Their submitted logs then show
1–5 failures when re-run, and — except for two in `kc37-Dots3-note` — none of those
failures belonged to the candidate.

The operator's command is not "the same test, but bigger". It amplifies detection **twice
over**, and the two amplifications multiply:

```
    pytest tests -n 8 & pytest tests_bugfix -n 8 & pytest tests -n 8 & pytest tests_bugfix -n 8 &
    └──────────────────────────── 4 suites at once ───────────────────────────┘
                            4 × 8 = 32 workers on 8 cores

 (a) more chances to catch it     P(red pass) = 1 − (1 − q)^4      q = per-suite-run red rate
 (b) a higher q to begin with     4× CPU oversubscription, 4× the disk, 4× the fork storms
                                  → every timing margin shrinks and every queue lengthens
```

With `q = 0.1`, one suite run is green 90 % of the time and the stress pass is green only
66 % of the time — before (b) pushes `q` up at all. That is the whole reason the command
exists.

The corollary is the uncomfortable one, and it applied to me too:

| green passes in a row | what it actually rules out (at q = 0.1/pass) |
|---|---|
| 1 | nothing — 66 % chance even if nothing was fixed |
| 3 | ~29 % chance of a false "fixed" |
| 6 | ~9 % |
| 12 | ~0.8 % |

My own rounds bear this out precisely: **three** consecutive green passes happened twice
during this work, and both times the next pass was red. Only the final commit reached
twelve.

> **Rule for the archive:** on this box, fewer than six consecutive green passes of the
> stress command is not a result. It is a coin that has not landed yet.

### 1.2 What these flakes cost while they lived

The failures were not only an acceptance problem. They taxed **every round of development
in the repo**, whether or not that round touched anything related:

1. A round lands a change. The suite goes red on
   `test_a_silent_agent_stalls_next_to_a_chatty_one` — a test the change never touched.
2. The agent cannot tell "I broke this" from "this is the flake" by looking, because the
   failure is a plain `AssertionError` on a state machine, not a timeout with a smoking gun.
3. So it re-runs the suite **on the old code** to see whether the test fails there too.
   8–18 minutes.
4. One green run on the old code proves nothing (§1.1), so the honest version of step 3 is a
   *loop* — and nobody has an hour per suspicious failure.
5. The decision therefore gets made on one or two samples, and the usual outcome is
   **"known flake, ignore it"**.

That last step is the real damage. Every "ignore it" is a small, permanent reduction in
what the suite is allowed to tell you, and it is applied to exactly the tests that guard
concurrency — the ones that are hardest to reason about and most worth keeping. Two of the
ten causes in this document (`EventTap`, `GitManager`) are **production** bugs that were
sitting inside tests already dismissed as flaky.

The arithmetic of the tax, per suspicious failure:

```
  triage one failure honestly  = 2 × (a loop, not a run) ≈ 2 × 6 passes × 8 min ≈ 1.5 h
  triage one failure in practice = 1 old-code run + a judgement call ≈ 15 min + a wrong answer
```

Six rounds of this ticket produced eight red passes. Under the old regime each of those
would have been a triage event on somebody's round.

---

## 2. The first misread, and the thing that actually unblocked it

Five patches sat in `kc37/`. The obvious reading — "five models tried to fix the flakes and
all failed" — is wrong, and acting on it would have meant fixing bugs that did not exist.

Four of the five are **KC-34/35/37 candidates**: they implement unrelated tickets. Their
test logs are full of FL-1 failures only because FL-1 was not fixed yet.

| patch | what it actually is | failed | its own bug | pre-existing flake |
|---|---|---|---|---|
| `Sonet5-FL-1-84` | an FL-1 attempt | 1 | 0 | `test_events_of_the_session_keep_wait_idle_alive` |
| `kc37-SenSenova-6-8-var1` | KC-35 + KC-37 | 1 | 0 | `test_rewriter_called_when_context_satisfied` |
| `kc37-SenSenova-6-7-var1` | KC-34 + KC-37 | 4 → 2 | 0 | all |
| `kc37-SenSenova-6-7-var2` | KC-37 | 5 | 0 | all |
| `kc37-Dots3-note` | KC-37 | 3 | **2** | 1 |

Two observations fell out of that table, and both mattered:

1. **The two best residuals were disjoint.** Sonnet 5 fixed families A and B — *nobody else
   did* — but never touched `test_contest_kilo_client.py`, which is outside FL-1's declared
   file list. SenSenova-6-8 did good work on family C but never touched family B. Each was
   the other's missing half.

2. **They fixed opposite sides of the same inequality** (see §3.3), and each left too little
   slack. Neither was wrong; both were half-measures.

> **Lesson.** When several agents fail at the same task, read their *output* before their
> code. The distribution of failures across candidates is data about the codebase, not
> about the candidates.

### 2.1 I made the same mistake first, and it was worse

The table above is the *result*. It is not what I did first.

What I actually did: opened `kc37/tests_results.txt`, read the failure list, recognised the
shapes (a stall test, a budget test, a heartbeat test), and **started editing code within
about ten minutes** — before reading a single one of the five patches. I applied Sonnet 5's
patch as a base, then went straight into rewriting
`tests/test_contest_kilo_client.py`: widening `test_events_of_the_session_keep_wait_idle_alive`
from a 0.3 s window to 3 s, widening six more bounds, restructuring the heartbeat.

I was interrupted and asked, fairly: *why are you writing your own fix instead of reading
what the other models did all day?*

Everything I had written went back:

```bash
git checkout -- tests/ tools/     # ~40 minutes of edits, discarded
```

The reframe that mattered cost **one command**:

```bash
for p in kc37/*.patch; do
  echo "=== $p"
  grep '^diff --git' "$p" | sed 's/.* b\///'    # what does this patch actually touch?
done
```

```
=== kc37-Dots3-note.patch          contest.ini, tools/contest/{cli,policy,roster}.py …
=== kc37-SenSenova-6-7-var1.patch  tools/contest/backend.py, roster.py, cli.py …
=== kc37-SenSenova-6-7-var2.patch  tools/contest/{cli,policy,roster}.py …
=== kc37-SenSenova-6-8-var1.patch  tools/contest/{cli,policy,roster,runner}.py …
=== Sonet5-FL-1-84.patch           tools/metrics_collector.py, tools/auto/auto_metrics.py …
```

Four of them touch `tools/contest/policy.py` and `roster.py`. **Those are KC-37 files.**
Only the fifth touches anything FL-1 names. Five seconds of reading, and the whole premise
— "five models failed at this" — collapses.

Two things were wrong with my first pass, and they are different mistakes:

1. **I treated a log of failures as a specification.** The failures were real, but *whose*
   they were is the entire question, and the answer was one `grep` away.
2. **I mistook recognising a shape for understanding a cause.** "Heartbeat under a silence
   window, margin too small" was the right *shape* and the wrong *depth* — it is Shape 1
   reasoning applied to what turned out to be Shape 4 (§8). The edit I was making in minute
   ten is, almost line for line, the edit that then failed the stress run four more times
   over the next six rounds.

The second one is the more interesting failure, because the interruption did not fix it. I
went on to apply the same reflex — widen the margin — in rounds 1, 2, 3 and 5. What finally
broke the pattern was not better judgement, it was the stress loop refusing to agree with
me.

> **Lesson.** Speed at the start is the most expensive kind. The first ten minutes should
> buy *orientation* — whose failures are these, what do these patches claim to do — not a
> diff.

---

## 3. Round 0 — the first fix commit (`e500d40`)

Assembled by taking A and B verbatim from Sonnet 5, merging both halves of C, and applying
one rule everywhere else.

### 3.1 Family A — a lock held across a disk barrier

`record_gate2` took a process-wide lock for the whole read-modify-write, and inside that
critical section `MetricsCollector.record` did an `fsync`.

```python
# BEFORE — tools/auto/auto_metrics.py
with self._lock:                       # process-wide
    _record_gate2_locked(self._collector, ...)

# BEFORE — tools/metrics_collector.py, inside that lock
json.dump(records, f, indent=2)
f.flush()
os.fsync(f.fileno())                   # ← a disk barrier. Unbounded.
os.replace(tmp_path, self.metrics_path)
```

```
   20 threads × 50 records = 1000 records

   thread 1 ──► record_gate2 ──► [ _lock ACQUIRED ]
                                       ├─ read metrics.json
                                       ├─ append
                                       ├─ json.dump → tmpfile
                                       ├─ os.fsync()      ◄── DISK. Not microseconds.
                                       ├─ os.replace()
                                       └─ [ _lock RELEASED ]
   threads 2…20 ─► record_gate2 ──► blocked ─────────────────┘

   1000 × (full rewrite + fsync), strictly serialised, zero overlap.
   Under -n 4 this does not drain inside the 180 s pytest-timeout.
```

The `pytest-timeout` stack dump is unambiguous: 19 workers parked at
`auto_metrics.py:179 with self._lock:`, one holder at
`metrics_collector.py:83 os.fsync(f.fileno())`. Nothing is deadlocked. The lock is doing
exactly what it was told, and it was told to cover a syscall that is allowed to block.

**This is a production hazard, not a test hazard.** Every Gate-2 verdict in a real
autonomous run is a `record_gate2` call.

**Fix.** `MetricsCollector.record` owns its own write lock (so it is thread-safe for *any*
caller, not just one that brings external locking) and `fsync`s on a bounded 0.5 s cadence.
`AutoMetricsStream._lock` is gone. `os.replace` still happens per call, so the file on disk
is always correct; only the crash-durability window widens from "one record" to the
cadence, and `flush()` forces it immediately for a caller that needs more.

```python
# AFTER — tools/auto/auto_metrics.py: no stream-level lock at all
def record_gate2(self, task_id, *, approved, feedback, ...):
    try:
        # MetricsCollector.record() serialises itself now; nothing above this
        # call is shared state, so _warn_if_contaminated and .collector stay
        # freely callable from another thread.
        _record_gate2_locked(self._collector, task_id, approved=approved, ...)
    except Exception as exc:                      # contract: never raises
        logger.error("AutoMetricsStream.record_gate2: failed to record — %s", exc)

def flush(self) -> None:
    self._collector.flush()                       # the shutdown hook now means something


# AFTER — tools/metrics_collector.py
_FSYNC_INTERVAL_S = 0.5

class MetricsCollector:
    def __init__(self, ...):
        self._write_lock = threading.Lock()       # OURS, not a caller's
        self._last_fsync_monotonic = None

    def record(self, run: RunRecord) -> None:
        with self._write_lock:                    # short: in-memory + one rename
            records = list(self._load_all_cached())
            records.append(asdict(run))
            try:
                fd, tmp_path = tempfile.mkstemp(dir=..., prefix=".metrics_tmp_")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(records, f, indent=2)
                        f.flush()
                        now = time.monotonic()
                        due = (self._last_fsync_monotonic is None
                               or now - self._last_fsync_monotonic >= _FSYNC_INTERVAL_S)
                        if due:                   # ← the disk barrier, on a cadence
                            os.fsync(f.fileno())
                            self._last_fsync_monotonic = now
                except Exception:
                    os.unlink(tmp_path); raise
                os.replace(tmp_path, self.metrics_path)   # ← still atomic, every call
                self._cache = records
                ...
            except Exception as e:
                logger.error(f"MetricsCollector failed to write metrics: {e}")

    def flush(self) -> None:
        """Force the current on-disk file durable now. Never raises."""
        with self._write_lock:
            try:
                fd = os.open(self.metrics_path, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            self._last_fsync_monotonic = time.monotonic()
```

What moved, precisely:

| | before | after |
|---|---|---|
| who serialises | the *caller* (`AutoMetricsStream._lock`) | the *collector* (`_write_lock`) |
| lock spans | read + serialise + **fsync** + rename | read + serialise + rename |
| `fsync` per 1000 records | 1000 | ~5 (0.5 s cadence) |
| file correct on disk after each call | yes (`os.replace`) | yes (unchanged) |
| durable against unclean shutdown | per record | per cadence, or `flush()` on demand |
| thread-safe for a caller with no lock | **no** | yes |

The count assertion that justified the lock in the first place is untouched and still
exact. Measured after: **1000 serial records in 2.7 s** (from 1000 serialised disk round
trips).

### 3.2 Family B — a truthy non-number is a 1-second budget

```python
# BEFORE — tools/auto/outer_loop.py
return float(getattr(self.inner_loop, "max_task_seconds", 0) or 0)
```

The comment above it promised that a malformed value means "no guard". The `try/except
(TypeError, ValueError)` handles `float()` *raising*. It does not handle `float()`
**succeeding on the wrong thing**:

```python
float(MagicMock())  # 1.0   ← MagicMock implements __float__
float(True)         # 1.0   ← bool is an int subclass
float('3')          # 3.0
```

A test fixture builds `inner_loop=MagicMock()` and never sets `max_task_seconds`.
`getattr` returns another `MagicMock`, `MagicMock or 0` keeps it (truthy), `float()` makes
it `1.0` — and now a behaviour test about whether a rewriter gets called owns a
**1-second wall-clock budget across all rounds**. On a loaded box round 1 alone exceeds it.

The captured log names the mechanism exactly, and `1s = 0.0 min` is the tell — no real
budget is configured in seconds:

```
OuterLoop: task SCTX-OUTER-1 wall-clock budget (1s = 0.0 min) exhausted
across rounds — stopping before round 2.
```

```python
# AFTER — tools/auto/outer_loop.py
def _task_budget_seconds(self) -> float:
    """This task's wall-clock budget in seconds (0 disables the guard).

    "Malformed" must cover a value float() *accepts*, not just one that raises.
    """
    value = getattr(self.inner_loop, "max_task_seconds", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0                       # MagicMock, True, '3', an enum, None
    try:
        seconds = float(value)
    except (TypeError, ValueError):      # kept: the original contract
        return 0.0
    if not math.isfinite(seconds) or seconds <= 0:
        return 0.0                       # nan, inf, 0, negative
    return seconds
```

Truth table, all verified:

```
  MagicMock()                → 0.0     float() would have said 1.0
  True                       → 0.0     float() would have said 1.0
  '3'                        → 0.0     float() would have said 3.0
  0 / None / <missing attr>  → 0.0     unchanged
  float('nan') / float('inf')→ 0.0     new
  1.5 / 90                   → 1.5 / 90.0
```

The fixture was fixed too, rather than leaning on the guard — `mock_inner.max_task_seconds
= 0` states what it means, and `max_rounds` got enough room that the round the rewriter is
eligible for is reachable without spending wall clock to get there.

### 3.3 Family C — margins, and the rule

Two submissions fixed **opposite sides of the same inequality**:

```
slack = silence_window − heartbeat_interval

  Sonnet 5:      0.1 s beat under a 1 s window   → 0.9 s
  SenSenova-6-8: 0.4 s beat under a 3 s window   → 2.6 s
  both:          0.1 s beat under a 3 s window   → 2.9 s
```

What a 4×-oversubscribed box drifts past is the **absolute** margin, not the ratio. So both
sides move.

For every "it took the fast path, not the slow one" bound, one rule:

> **Widen the slow path. Do not tighten the bound.**

A test proving "the 0.5 s window fired, not the 2 s pause" has 1 s of slack. The same test
against a **60 s pause** and a **120 s deadline**, bounded at 20 s, proves strictly more and
has 19.5 s of slack — and a green run still returns in the same half-second. Only a
genuinely broken run pays for the wider gap.

Applied to six bounds in `test_contest_kilo_client.py`, both `KiloServer.spawn` health
gates, and — from Sonnet 5 — replacing stall bounds with the classification the runner
already produces (`run.state` / `turn["idle_status"]` / `run.last_error`), which is
load-independent by construction.

### 3.4 What round 0 got right, and the bet it made

A, B and the C1 rule were all correct and all still stand. Two of them are production
fixes with measurements behind them; the third is a rule that made a dozen brittle
assertions robust. The suite went green. It went green again.

By every standard this repo had ever applied — and by the standard all five earlier
candidates had used — round 0 was **finished**.

It was also wrong, and in a way that no amount of staring at it would have revealed. The
bet buried in it is this: that **family C was a margin problem**. One kind of mistake,
appearing in a dozen places, fixable by one rule.

It was not one problem. It was five, stacked, each one hidden behind the one above it. And
the margin rule — the good, correct, still-standing margin rule — fixes exactly the first.

The next pass was green. So was the one after it. The third was red.

---

## 4. Rounds 1–5 — what the stress loop kept finding

Each round: fix, run the stress command 2–6 times, read the one red run. Each failure was
one layer deeper than the last, and **each was invisible until the one above it was fixed.**

| round | passes before red | what fell over |
|---|---|---|
| `e500d40` | 2 | `test_events_of_the_session_keep_a_turn_alive` |
| `40bc436` | 2 | `test_an_error_turn_with_a_valid_commit_is_harvested_to_ready` |
| `6449e7d` | 3 | `test_a_terminal_harvest_runs_the_roots_under_the_rounds_lock` |
| `722dd48` | 2 | `test_a_stalled_turn_with_a_valid_commit…` + a genuine `index.lock` collision |
| `32033f9` | 3 | `test_stub_shapes_every_api_format_and_replays_across_the_block` |
| `8025b6c` | 3 | `test_events_of_the_session_keep_wait_idle_alive` (again) |
| **`89012d8`** | **12, none** | — |

### 4.1 C2 — a counted heartbeat is a bet on its *total length*

The test reached `READY` and was aborted anyway. The turn's hook does a real `git commit`,
which pushed the turn's `idle` out **past the last of 60 beats**:

```
beats   ├─┼─┼─┼─┼─┼─┼─┼─┼─┤ (60 × 0.1 s = 6 s, then nothing)
hook    ├──────── git commit, 0.3 s … 8 s ────────┤
idle                                              ▲ arrives at 9 s
window                                  ├─ 3 s ─┤ ▲ fires at 9 s − 6 s > 3 s
```

Widening the *interval* margin does nothing here: the bug is that the beats **ran out**.

**Fix.** `pulse(times=None)` beats until the fake stops, so the heartbeat outlives the turn
by construction. The chatty-neighbour test starts its beats *before* its git work instead
of after.

Clean, structural, no numbers to tune — the kind of fix that does not come back. Two passes
green.

The third pass lost a *different* test in the same file.

### 4.2 C3 — the gap that matters is *inside* the hook

Bracketing the hook with one beat on each side is not enough, and the error turns proved
it. The scenario hook runs **inside** `_run_turn`, on the fake's thread, while the runner's
silence clock is already running:

```
t=0.00  prompt_async returns
        ├─ runner: wait_idle() starts. silence window = 1 s.
        └─ fake:   _run_turn thread → beat … on_prompt() → git commit
                                      ▲                ▲
                                      beat before      beat after
                                      │                │
                                      └── 0.3 s … 6 s ─┘  ← NOTHING IN HERE
t=1.00  runner: "no event for 1s" → STALL → abort
t=2.40  fake:   commit lands, scripted session.error emitted  ← too late
        result: STALLED, not ERROR; run.commit is None
```

**Fix.** The fake heartbeats **for the duration of** the hook, stopping the moment it
returns — a real agent emits while it works.

```python
# BEFORE — tests/_kilo_fake.py, FakeKiloServer._run_turn
on_prompt = turn.get("on_prompt")
if callable(on_prompt):
    try:
        on_prompt(session.directory, text)        # ← git commit, 0.3 s … 8 s, silent
    except Exception as e:
        self.turn_errors.append(f"on_prompt: {type(e).__name__}: {e}")

# AFTER
HOOK_BEAT_S = 0.1

on_prompt = turn.get("on_prompt")
if callable(on_prompt):
    hook_done = threading.Event()

    def _beat() -> None:
        while not hook_done.wait(self.HOOK_BEAT_S):
            self._emit({"type": "session.status",
                        "properties": {"sessionID": session.id, "status": "busy"}})

    self._emit({"type": "session.status",          # one on entry
                "properties": {"sessionID": session.id, "status": "busy"}})
    beating = threading.Thread(target=_beat, daemon=True)
    beating.start()
    try:
        on_prompt(session.directory, text)         # ← now covered, however long it takes
    except Exception as e:
        self.turn_errors.append(f"on_prompt: {type(e).__name__}: {e}")
    finally:
        hook_done.set()                            # silence resumes exactly here
        beating.join(5)
```

The `finally` is load-bearing: the beats stop the instant the hook returns, so the silence
still starts **where the agent actually stopped**. No stall test is weakened — the silence
still has to arrive after the beats for any of them to pass.

It lives in `FakeKiloServer._run_turn`, the base class, not in one test file's subclass:
`tests/test_contest_cli.py` runs a real commit from a hook under
`idle_event_timeout_sec = 1` and had **no coverage at all** — a latent flake found by
enumerating the shape instead of waiting for a stress run to hit it.

Now the hook is covered for its entire duration, by a heartbeat ten times a second, in the
base class every test inherits. There is no gap left.

Three passes green — the longest streak so far. Then the same family failed again, with the
heartbeat running.

### 4.3 C4 — a dropped beat is a lost event, and this one is not timing

The heartbeat from C3 was running, and it still was not seen.

A `KiloBackend` opens its event stream **on a thread**. The fake, like a real server, drops
anything emitted before that stream connects. `Harness` has always waited for the tap.
`run_round` never did:

```
run_round ──► make_backend(ws) ──► KiloBackend.__init__ ──► EventTap.start()
                   │                                             │ thread:
                   │                                             │ connecting…
                   ▼                                             │
              prompt_async ────────────────────────────────►     │
              fake: _run_turn → beat, beat, beat                 │
                                 │                               │
                                 ▼                               │
                        _Bus.publish → 0 subscribers → DROPPED   │
                                                                 ▼
                                                           connected (too late)
   runner's silence clock: has seen nothing at all → stalls mid-commit
```

**No amount of widening would ever have fixed this one.** It is not a margin, not an
interval, not a duration. It is an event that was never delivered to anybody, because
nobody was listening yet.

And that is the first crack in the whole approach. For three rounds the working theory had
been *"the box is slow, the margins are too tight"* — a theory that explains every failure
so far and predicts nothing. C4 does not fit it at all. If a beat can be lost entirely,
then the question is not how *late* events arrive.

It is whether they arrive.

**Fix.**

```python
# BEFORE — tests/test_contest_runner.py
def _make_backend(fake, out_dir):
    server = KiloServer.attach(fake.url)

    def make_backend(ws):
        return KiloBackend(server, str(ws.path),
                           events_log=str(out_dir / ws.agent / "events.jsonl"))
    return make_backend          # ← hands back a backend whose tap may not be connected

# AFTER
def make_backend(ws):
    before = fake.subscribers                       # ← how many taps exist right now
    backend = KiloBackend(server, str(ws.path),
                          events_log=str(out_dir / ws.agent / "events.jsonl"))
    deadline = time.monotonic() + 30
    while fake.subscribers <= before and time.monotonic() < deadline:
        time.sleep(0.01)
    assert fake.subscribers > before, f"{ws.agent}: the tap never connected"
    return backend
```

`> before`, not `>= 1`. In a multi-agent round another agent's tap is already up, so
"non-zero" would pass immediately while *this* backend's stream was still connecting —
which is the bug, not the fix. `Harness` has waited on `subscribers < 1` since it was
written, which is why the single-agent path never showed this and `run_round` did.

The `_probe` handshakes in `test_contest_kilo_client.py` stopped polling a fixed 2 s
(`for _ in range(100): sleep(0.02)`) in favour of a 30 s deadline that still asserts
loudly if the tap genuinely never arrives.

### 4.4 C5 — emission is not delivery *(the root cause)*

This is the important one. See §5.

### 4.5 C6 — the harvest tests, and a call I got wrong twice

Six stall/error harvest tests put a real `git commit` inside a wall-clock silence window,
via the turn's `on_prompt`. Two submissions had disagreed about this: Sonnet 5 committed
**before** the run; SenSenova-6-8 kept the commit **inside** the turn and covered it with
heartbeats. I chose the in-turn shape, arguing it tested more.

It does not. `_commits_above` reads the branch **at harvest time** and cannot tell when a
commit was made — so the in-turn ordering tested nothing extra. And it cannot be made to
work: the hook's work is unbounded and the window is not. That is why it survived a wider
window *and* a heartbeat through the hook before falling over again two rounds later.

**Fix.** All six use a `prepare=` hook on `_run_one` that does the git work before the
round. Sonnet 5's shape, arrived at the long way round.

```python
# AFTER — tests/test_contest_runner.py
def _run_one(tmp_path, scenario, config=None, policy=None, prepare=None):
    sb = Sandbox(tmp_path)
    if prepare is not None:
        prepare(str(sb.ws("agent-a").path))     # ← git work, before any clock starts
    config = config or make_config(["agent-a"])
    with _BenchFake(scenario) as fake:
        h = Harness(sb, fake, config, policy=policy)
        run = h.go()
        aborted = _aborted(fake)
    return sb, fake, h, run, aborted


# and at the call sites, the hook moves out of the scenario:
- scenario = {"turns": [{"on_prompt": work_ready, "events": [], "idle": False}]}
- sb, fake, _h, run, aborted = _run_one(tmp_path, scenario, cfg)
+ scenario = {"turns": [{"events": [], "idle": False}]}
+ sb, fake, _h, run, aborted = _run_one(tmp_path, scenario, cfg,
+                                       prepare=lambda d: work_ready(d, ""))
```

Nothing is lost, and that is checkable rather than a matter of taste:

```python
# tools/contest/runner.py — the harvest's view of the commit
above = _commits_above(ws)        # `git rev-list base..HEAD`, read AT HARVEST TIME
```

`_commits_above` runs `git rev-list` against the branch when the turn is already over. It
has no way to know, and no reason to care, whether the commit was written during the turn
or ten seconds before it started. The in-turn ordering was testing the *fixture's* timing,
not the runner's.

> **Lesson.** "This version tests more" is a claim that can be checked. Check it before
> preferring it. I did not, and it cost three stress rounds.

---

### Everything would have been fine, but

By this point five causes had been found and fixed, and every one of the fixes was correct.
They are all still in the tree. Each was verified, each was reasoned about, each removed a
real defect.

And the suite still would not stay green.

Look at what the last four rounds had actually been doing. The window went from 1 s to 3 s
to 8 s. The beat went from 0.4 s to 0.1 s. The heartbeat was made unbounded, then moved
inside the hook, then moved into the base class. Every one of those changes bought a bigger
margin, and every one of them was eventually walked through by the same box.

Then came the failure that could not be argued with:

```
FAILED tests/test_contest_runner.py::test_events_of_the_session_keep_a_turn_alive
```

An **8-second** silence window. A heartbeat emitting **ten times a second** — eighty beats
inside the window that expired. The fixture had done everything right and the runner still
reported the session as silent.

Eighty events, emitted on time, and the clock never saw them.

At that point the theory has to change, because there is no margin large enough to explain
it and no beat interval small enough to fix it. Something between `_emit()` and
`last_seen = time.monotonic()` was eating whole seconds, and it had been eating them all
along — in every round, under every fix, quietly widening the gap that each new margin was
built to survive.

The five earlier fixes were not wrong. They were **downstream of something that was still
broken**, which is exactly why each of them held for two or three passes and then did not.

---

## 5. The root cause: `EventTap` starved the clock it feeds

It was the tap's own event log.

`EventTap` is the object on the receiving end of the stream — the one whose `wait()` the
silence clock is built on. It does two jobs: keep every event in memory for waiters, and
append every event to `events.jsonl` so a round can be replayed afterwards. The second job
was being done **on the reading thread, synchronously, once per event**.

```python
# BEFORE — tools/contest/kilo_client.py
def _record(self, event):
    with self.lock:
        self.events.append(event)
    self._write_log({"t": time.time(), "event": event})   # on the READER thread

def _write_log(self, entry):
    log.write(json.dumps(entry) + "\n")
    log.flush()                                           # ← every single event

def wait(self, pred, timeout):
    while time.monotonic() < deadline:
        ...drain cursor...
        time.sleep(0.2)                                   # ← poll, not notify
```

### The pipeline, and the two stalls in it

```
 fake (pulse thread)         EventTap reader thread              caller (wait_idle)
 ───────────────────         ──────────────────────              ──────────────────
   _emit(busy)
       │
       ▼
   _Bus.publish
       │
       ▼
   queue.put ──► SSE write ──► resp.readline()
                                   │
                                   ▼
                              _handle_line
                                   │
                                   ▼
                              _record:
                                with lock: events.append()   ◄── (1) visible to memory
                                _write_log → log.flush()     ◄── (2) BLOCKS HERE
                                   │                              page-cache writeback
                                   │                              throttling: SECONDS
                                   ▼                              and the next readline()
                              (next readline only now)            does not happen until
                                                                  this returns
                                                     ┌──────────────────────────┐
                                                     │ wait(): time.sleep(0.2)  │ ◄── (3)
                                                     │ poll loop                │   up to 200 ms
                                                     └──────────────────────────┘   more latency
                                                                  │
                                                                  ▼
                                                     wait_idle: last_seen = monotonic()
                                                                  ▲
                                                     THE SILENCE CLOCK STARTS HERE
```

**The clock is measured from (3), not from (1).** So both the `flush()` and the poll counted
as *the session going quiet*. A turn that was emitting the whole time got declared stalled
and aborted:

> **the agent's own event log starved the agent.**

Same hazard family as family A — a blocking disk operation sitting on a path that a timing
guard depends on — and exactly as much a production bug. In a real contest round this
aborts a working agent because the disk is busy.

**Fix.**

```python
# AFTER
def _record(self, event):
    with self._cond:                       # memory first, waiters woken first
        self.events.append(event)
        self._cond.notify_all()
    self._write_log(...)                   # log afterwards, never in the way

def _write_log(self, entry):
    log.write(...)                         # buffered
    if now - self._last_flush >= _LOG_FLUSH_INTERVAL_S:   # 0.5 s cadence
        log.flush()

def wait(self, pred, timeout):
    with self._cond:
        ...drain cursor...
        self._cond.wait(left)              # woken by _record, no polling
    if pred(event): return event           # pred runs OUTSIDE the lock
```

On-disk format and completeness at `stop()` are unchanged (`_close_log` still flushes).
Both changes are latency wins on every real turn, not just under test.

### Why this was the last one

Every round before this one had been arguing with a symptom.

A test asserts something about time. The assertion fails. The margin looks too small, so
the margin gets bigger — and for two or three passes it is. Then the disk gets busy at the
wrong moment, the reader thread parks inside `write(2)`, and the new margin is walked
through exactly like the old one. The fix was never wrong; it was just aimed at the wrong
end of the pipe.

You cannot see this from inside a test. The fixture emits on time and can prove it. The
runner reports silence and can prove it. Both are telling the truth, and the lie is in the
six hops between them — which is why five rounds of increasingly careful fixture work kept
producing five rounds of increasingly confident wrong answers.

Once the reader stops blocking and waiters stop polling, there is nothing left in that gap
to absorb seconds. That is a **mechanistic** reason to expect stability, and it is a
different kind of claim from "it has been green for a while": 12 passes and 48 suite runs
later, it still is.

### The shape that cannot be fixed with margin at all

```
 window ├────────────────────────────────►
 beats  │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ │ idle
        └gap┘

 A regression cuts the turn at `window`.
 The only proof is SURVIVING longer than `window`.
 It survives only if every gap between two DELIVERED events < window.

    ⇒ tolerance for a starved box == window == how long the test runs
```

Three numbers, one value. There is no margin to widen and no beat interval that
substitutes. Robustness is bought with wall time or not at all.

So that claim moved out of integration entirely, onto a scripted tap driven by a fake clock
— **no transport to starve and no real time at all**:

```python
class _ScriptedTap:
    """Stands in for EventTap. Time moves only when this object moves it,
    by exactly what wait_idle said it was willing to wait."""
    def wait(self, pred, timeout):
        deadline = self._clock.now + timeout
        while True:
            if not self._events or self._next_at > deadline:
                self._clock.now = deadline          # the stream was silent
                return None
            self._clock.now = self._next_at
            self._next_at = self._clock.now + self._every
            event = self._events.pop(0)
            if pred(event):
                return event
            # consumed, NOT returned — exactly what EventTap does with an
            # event the caller's predicate rejects. This is what makes the
            # regression visible.
```

Faithful in the two ways that matter: the cursor advances past every event looked at, and a
rejected event is **consumed without being returned**, so a `wait_idle` that stopped
counting `session.status` runs its clock out.

The clock and the probe, in full — there is very little to it, which is the point:

```python
class _FakeClock:
    """``time.monotonic`` that only moves when a test moves it."""
    def __init__(self, start: float = 1000.0):
        self.now = float(start)

    def monotonic(self) -> float:
        return self.now


def _silence_clock_probe(monkeypatch, events, *, window, every, aborts=None):
    clock = _FakeClock()
    monkeypatch.setattr(kilo_client_module.time, "monotonic", clock.monotonic)

    class _Client(KiloClient):
        def __init__(self):            # no server, no HTTP, no directory
            pass
        def _abort_quietly(self, session):
            (aborts if aborts is not None else []).append(session.id)

    session = SessionRef(id="ses_probe", provider_id="p", model_id="m", directory="/nowhere")
    tap = _ScriptedTap(events, every=every, clock=clock)
    return _Client().wait_idle(tap, session, 10_000.0, idle_event_timeout=window,
                               on_permission=_reject, on_question=lambda event: None)
```

and the three tests it carries:

```python
def test_events_of_the_session_keep_wait_idle_alive(monkeypatch):
    """21 events at 0.4 s carry the turn through eight 1 s windows."""
    window, every = 1.0, 0.4
    beats = [_ev("session.status", status="busy") for _ in range(20)]
    aborts: list = []
    res = _silence_clock_probe(monkeypatch, beats + [_ev("session.idle")],
                               window=window, every=every, aborts=aborts)
    assert res.status == "idle"
    assert res.elapsed > window * 8, res.elapsed      # 8.4 s of fake time
    assert aborts == []


def test_a_beat_the_wait_does_not_count_lets_the_silence_clock_run_out(monkeypatch):
    """The same stream, but the beats belong to ANOTHER session — so the
    predicate rejects them, and the clock runs out at exactly the window.
    This is what a KC-12 regression looks like from the outside."""
    window, every = 1.0, 0.4
    beats = [_ev("session.status", session_id="ses_someone_else", status="busy")
             for _ in range(20)]
    aborts: list = []
    res = _silence_clock_probe(monkeypatch, beats + [_ev("session.idle")],
                               window=window, every=every, aborts=aborts)
    assert res.status == "timeout"
    assert res.elapsed == window, res.elapsed         # exactly. not "about".
    assert aborts == ["ses_probe"]


def test_the_silence_clock_is_off_when_no_idle_event_timeout_is_given(monkeypatch):
    """idle_event_timeout=None is KC-1's wait: a silent stream runs to the
    overall deadline instead of being cut at a window."""
    ...
    res = _Client().wait_idle(tap, session, 42.0, idle_event_timeout=None, ...)
    assert res.status == "timeout"
    assert res.elapsed == 42.0                        # exactly.
```

`assert res.elapsed == window` — an **exact equality on a duration**, which is only
writable because no real clock is involved. The integration version of the same assertion
was `assert elapsed < 1.5` and it was the flakiest line in the file.

Verified by injecting the exact regression:

```diff
 # tools/contest/kilo_client.py, inside wait_idle's `wanted()`
-            return silence is not None or etype in _SESSION_EVENTS
+            return etype in _SESSION_EVENTS
```
```
FAILED test_events_of_the_session_keep_wait_idle_alive - AssertionError: assert 'timeout' == 'idle'
```

---

## 6. Structure — how the pieces fit, and where each race sits

Everything above is easier to hold in one piece with the object graph in front of you.
This is one agent, one turn, in a test.

### 6.1 The object graph

```
  ┌──────────────────────────────── test process ─────────────────────────────────┐
  │                                                                               │
  │   run_round(cfg, …, make_backend=…)                                           │
  │        │                                                                      │
  │        ├─ per workspace: make_backend(ws) ──► KiloBackend                      │
  │        │                                        ├── KiloClient ── HTTP ──┐    │
  │        │                                        └── EventTap   ── SSE ───┤    │
  │        │                                              │ (reader thread)  │    │
  │        ▼                                              │                  │    │
  │   run_agent(run, backend=…)                           │                  │    │
  │        ├─ backend.prompt(session, text) ──────────────┼──────────────────┤    │
  │        └─ backend.wait_idle(session, …) ──► tap.wait(pred, left)         │    │
  │                  ▲                                                       │    │
  │                  │ the SILENCE CLOCK lives here                          │    │
  │                                                                          │    │
  │   ┌──────────────────── FakeKiloServer (ThreadingHTTPServer) ─────────────┘    │
  │   │                                                                            │
  │   │   POST /session      ──► _create_session ──► _emit(session.created)         │
  │   │   POST /prompt_async ──► spawn thread: _run_turn(session, turn, text)       │
  │   │                              ├─ on_prompt(dir, text)   ← REAL git commit    │
  │   │                              ├─ _emit(busy) …                               │
  │   │                              ├─ _sleep(delay)                               │
  │   │                              └─ _emit(idle) | _emit(session.error)          │
  │   │   GET  /event        ──► _Bus.subscribe() ──► queue ──► SSE writer          │
  │   │                                                                            │
  │   └────────────────────────────────────────────────────────────────────────────┘
  └───────────────────────────────────────────────────────────────────────────────┘
```

Four threads are live during one turn, and **none of them is scheduled on demand**:

| thread | owns | starves on |
|---|---|---|
| main / worker | `run_agent`, `wait_idle`, the silence clock | `tap.wait` returning |
| fake's `_run_turn` | the scenario hook, the scripted events | `git`, `_sleep` |
| fake's SSE writer (per subscriber) | `queue.get` → socket write | the socket, the GIL |
| `EventTap._run` (reader) | `readline` → `_record` → **log write** | **the disk** ← C5 |

### 6.2 One turn, as a timeline, with every cause marked

```
 t   main / run_agent            fake _run_turn            EventTap reader
 ─────────────────────────────────────────────────────────────────────────────
 0   make_backend(ws)
     └─ EventTap.start() ─────────────────────────────────► connecting…
                                                              ▲
                                        ┌─────────────────────┘
                                        │  C4: nothing waits for this.
                                        │  Events emitted now are DROPPED —
                                        │  the bus has no subscriber yet.
 0+  prompt_async ─────────────────────► thread starts
     wait_idle() begins
     last_seen = now                    on_prompt():
     silence window opens                 git add / commit
                                          0.3 s … 8 s, SILENT
                                        ▲
                                        │  C3: the gap that matters is in
                                        │  here, not before or after it.
                                        │
                                        │  C6: and the commit this produces
                                        │  is what the harvest will read —
                                        │  racing the stall it is under.
 w   silence window expires
     └─ STALL → abort → harvest
        _commits_above(ws) → []  ← commit has not landed
        run.commit = None
 ─────────────────────────────────────────────────────────────────────────────
                                        _emit(busy) ────────► readline
                                                              _record:
                                                                events.append()
                                                                log.flush()  ◄── C5
                                                                (BLOCKS: seconds)
                                                              │
                                          wait(): sleep(0.2)  │  ◄── C5
                                                              ▼
     last_seen = now  ◄─────────────────────────────────── returned here
     ▲
     └─ the clock is measured from THIS instant, not from _emit.
```

### 6.3 The silence clock itself, annotated

The production code is correct and unchanged. It is worth reading once, because every
family-C failure is a statement about **one line** of it.

```python
# tools/contest/kilo_client.py — KiloClient.wait_idle (unchanged by this ticket)
started   = time.monotonic()
deadline  = started + float(timeout)          # the overall bound
silence   = float(idle_event_timeout)         # the KC-12 bound; None = off
last_seen = started                           # ← the clock's origin

def wanted(event) -> bool:
    if event.get("type") == "tap.closed":
        return True
    if (event.get("properties") or {}).get("sessionID") != session_id:
        return False                          # ← only THIS session's events
    # with the silence clock on, EVERY event of this session wakes the wait:
    # the ones acted on below are handled, the rest only reset the clock
    return silence is not None or etype in _SESSION_EVENTS   # ← the KC-12 claim

while True:
    now  = time.monotonic()
    left = deadline - now
    if silence is not None:
        left = min(left, silence - (now - last_seen))   # ← the two bounds race
    if left <= 0:
        self._abort_quietly(session)
        return IdleResult(status="timeout", ...)        # ← what a starved box produces
    event = tap.wait(wanted, left)
    if event is None:
        continue
    last_seen = time.monotonic()              # ← RESET. Measured from DELIVERY.
    ...
```

Three lines carry the whole ticket:

* `return silence is not None or ...` — the KC-12 claim. Deleting the first clause is the
  regression the deterministic test injects, and it makes the beats invisible.
* `last_seen = time.monotonic()` — set **after `tap.wait` returns**, i.e. from when the
  caller *sees* the event. This is why C5 (a blocked reader, a polling waiter) is
  indistinguishable from a genuinely silent session.
* `left = min(left, silence - (now - last_seen))` — the two deadlines race, and both come
  back as `status="timeout"`. The runner tells them apart by `elapsed`, which is why the
  tests assert on `idle_status` (`"stalled"` vs `"timeout"`) rather than on a stopwatch.

---

## 7. Two production bugs the stress run exposed

Neither was in the ticket. Both lose real work in a real autonomous run.

### G — `.git/index.lock` treated as a hard failure

```
CommitOnSuccess: git error for task T1 — git add -u failed
fatal: Unable to create '.../.git/index.lock': File exists.
```

git holds `index.lock` for the whole of any index-writing command (`add`, `commit`,
`status` when it refreshes), and a second git that finds it there **exits 128 without
waiting**. `GitManager` had always *named* stale locks in its error text and never waited
for a held one. One collision costs a task its commit.

**Fix.** Retry that one signature, and only that one.

```python
# AFTER — tools/auto/git_manager.py
_LOCK_CONTENTION_RE = re.compile(r"Unable to create '.*\.lock': File exists", re.IGNORECASE)
_LOCK_RETRIES = 8
_LOCK_BACKOFF_S = 0.25

def _run(self, cmd: list[str], error_msg: str) -> str:
    for attempt in range(self._LOCK_RETRIES):
        try:
            return self._run_once(cmd, error_msg)
        except GitError as exc:
            last = attempt == self._LOCK_RETRIES - 1
            if last or not self._LOCK_CONTENTION_RE.search(str(exc)):
                raise                       # ← anything else: straight through
            logger.debug("GitManager: %s held by another git — retry %d/%d",
                         " ".join(cmd), attempt + 1, self._LOCK_RETRIES - 1)
            self._backoff(self._LOCK_BACKOFF_S)
    raise AssertionError("unreachable")     # pragma: no cover

def _backoff(self, seconds: float) -> None:
    """Wait between two lock retries.

    A named seam rather than a bare time.sleep, so a test can stand in for the
    wait without patching the `time` module for its whole process — too broad
    to be safe, and under `pytest -n` a way to change behaviour a long way
    from the test doing it.
    """
    time.sleep(seconds)

def _run_once(self, cmd, error_msg):        # ← the old _run body, unchanged
    ...
```

Bounded deliberately. A **stale** lock left by a crashed git never clears, so the ladder
costs two seconds and then raises exactly the error it raised before — which already names
stale locks as a likely cause. The contended case, which is the real one, is bought.

The three tests that ship with it are themselves free of wall-clock bets — the release
happens *inside* the backoff, so the ordering is exact:

```python
def test_a_held_index_lock_is_waited_out_not_raised(tmp_path, monkeypatch):
    gm = _repo_with_a_commit(tmp_path)
    lock = tmp_path / ".git" / "index.lock"
    lock.write_text("")                       # somebody else has the index
    backoffs: list = []

    def release_instead_of_sleeping(seconds):
        backoffs.append(seconds)
        if lock.exists():
            lock.unlink()                     # the other git finishes, right here

    monkeypatch.setattr(gm, "_backoff", release_instead_of_sleeping)
    (tmp_path / "a.txt").write_text("2\n")
    gm._run(["git", "add", "-u"], "git add -u failed")

    assert backoffs == [gm._LOCK_BACKOFF_S]   # exactly one: attempt 1 failed, 2 worked
    assert gm.has_staged_changes() is True
```

An earlier version of this test used a `time.sleep(0.5)` in a releaser thread racing the
ladder — the same wall-clock bet this whole ticket is about. **It failed the stress run**,
which is a pleasing way to be taught your own lesson.

### S — a client timeout against a server that had not been scheduled

`StubServer.start()` binds in `__init__` and hands `serve_forever` to a daemon thread. The
listen socket therefore **accepts and queues** a connection before anything is serving it.

```
  client: connect  ──► OK (kernel backlog)
  client: POST     ──► queued
  client: waits 10 s ──► TimeoutError
  server thread: …still waiting for the scheduler…
```

The reply is not slow; **nobody has run yet**. 10 s looks generous for a loopback POST that
normally answers in milliseconds, which is exactly why it read as safe.

---

## 8. The taxonomy — four shapes, four different rules

Every failure in this ticket is one of four shapes, and they do **not** take the same fix.
Confusing them is what made this take six rounds.

### Shape 1 — "fast path, not the slow path"

*A bound that distinguishes two code paths.*
**Rule: widen the slow path, never tighten the bound.** The green run stays fast; only a
broken run pays. A 0.5 s window against a 60 s pause bounded at 20 s proves more than the
same thing bounded at 1.5 s.

### Shape 2 — a hang guard wearing an assertion's clothes

*`subprocess.run(timeout=15)`, `urlopen(timeout=10)`, `wait_idle(..., 5.0)`.*
**Rule: make it big and name it.** These catch nothing. `pytest-timeout` is the right hang
guard and it prints a stack dump; a bare `TimeoutError` deep in `http.client` says nothing
about what broke. 21 of these were raised from 5 s to 60 s across two files.

### Shape 3 — an upper bound measured around unbounded work

*`elapsed < backoff + 2 * turn_timeout` around `_run_one`, which builds real git worktrees.*
**Rule: delete it, or measure where the thing actually happens.** The retry backoff is
recorded in the turn's own `sent_at`/`idle_at`. The wall clock around the whole harness read
**78 s** on a doubly-loaded box while the real assertion was true.

### Shape 4 — "must survive a real-time window"

*The one that cannot be fixed with margin.*
**Rule: take it out of real time.** Window, runtime and tolerance are one number. Drive the
unit over a stub and a fake clock; leave integration to assert only what is load-proof
(here: "a turn that keeps emitting is not aborted", under a window twelve times the beats).

---

## 9. Verification

A green suite proves nothing if the guards cannot fail. Both new guards were verified by
**injecting the regression they exist to catch**:

```python
# tools/contest/kilo_client.py — remove KC-12's silence-clock accounting
-  return silence is not None or etype in _SESSION_EVENTS
+  return etype in _SESSION_EVENTS
```
→ `test_events_of_the_session_keep_wait_idle_alive` fails: `assert 'timeout' == 'idle'`.

```python
# tools/auto/git_manager.py — remove the lock retry
-  for attempt in range(self._LOCK_RETRIES):
+  for attempt in range(1):
```
→ both `index.lock` tests fail.

Then the loop:

```
48 consecutive suite runs · 186 072 test executions
1 h 40 min wall clock · 5.9 h summed run time · 0 failures
```

12 passes of the operator's command, in two batches of six, back to back:

```
  batch 1   00:33:41 → 01:21:50     6 passes, 24 suite runs, 0 red
  batch 2   01:21:52 → 02:13:30     6 passes, 24 suite runs, 0 red
  ─────────────────────────────────────────────────────────────────
  total     00:33:41 → 02:13:30     1 h 39 min 49 s wall clock
```

Per suite run, over all 48:

| | n | min | median | max |
|---|---|---|---|---|
| `pytest tests -n 8` | 24 | 420 s | 494 s | **578 s** |
| `pytest tests_bugfix -n 8` | 24 | 284 s | 388 s | **465 s** |
| all | 48 | 284 s | 447 s | 578 s |

The spread is the point: the same suite ran anywhere from **7 min 00 s to 9 min 38 s**
depending on what the other three were doing. A 38 % swing in total runtime is the scale of
the scheduling noise every margin in §8 has to survive, and it is why the surviving
wall-clock bounds are 20 s and 120 s rather than 1.5 s and 10 s.

For contrast, the best previous streak was **3** passes, twice — see §1.1 for why three
means very little.

Constraints held throughout: no `--timeout` value raised, no assertion that catches a real
regression weakened, `scripts/sync_test_tiers.py --check` clean, `CollectBridge._shrink`
byte-identical.

---

## 9.1 Round 7 — the one that 48 green runs did not find

Written after the fact, and it is the most useful entry in this document.

The stress command in §9 was re-run against the merged tree, to validate the
benchmark runbook rather than to find anything. The first pass came back with:

```
FAILED tests/test_contest_runner.py::test_retry_backoff_is_observed
AssertionError: assert <AgentState.STALLED> is <AgentState.READY>
                last_error = 'no idle after 30s'
```

Not the silence clock this time — the **turn deadline**. `turn_timeout_sec`
is a wall-clock bound the *runner* puts over an entire turn, and 58 turns in
that file run a real `git commit` from their `on_prompt` hook underneath it.
At the default of 30 s it is a bound around unbounded work, which is
precisely Shape 2 from §8 — a hang guard wearing an assertion's clothes.

```python
# BEFORE — tests/test_contest_runner.py, make_config()
kw = dict(agents=specs, max_parallel=1, max_rework=2, turn_timeout_sec=30, ...)

# AFTER
kw = dict(agents=specs, max_parallel=1, max_rework=2, turn_timeout_sec=300, ...)
```

Plus four scaffolding copies of the same number. The one test that is
actually *about* the turn deadline sets `turn_timeout_sec=1` explicitly and
is untouched; nothing in the file asserts that the default fires.

Three things this settles, and they are worth more than the fix:

1. **48 consecutive green runs is a floor, not a proof.** This document's own
   verification section says the streak is what makes the claim credible.
   It does — and it still missed a cause of a shape the document itself
   names. A clean stress run means *no evidence of a problem*, never
   *evidence of no problem*.

2. **The taxonomy earned its keep.** The stress run produced one line. §8's
   table said which of four fixes that line called for, `grep` found every
   other copy of the number, and it was closed in minutes instead of another
   8-minute pass per instance. That is the difference between a postmortem
   that records what happened and one that is usable.

3. **The audit missed it for a readable reason.** `turn_timeout_sec` looks
   like a *config value for the production runner*, not a test bound — so
   the shape greps in Appendix B.3, which hunt `elapsed <`, `timeout=` and
   short rendezvous, walk straight past it. The corrected rule:

   > Anything a test sets that can **end** the work it is measuring is a
   > deadline over that work, whoever enforces it — the test, the runner, or
   > the config.

---

## 10. What I would do differently

1. **Read the failure logs before the candidate code.** The distribution of failures across
   five patches was the single most informative artifact, and it was sitting in a text file
   the whole time.

2. **Check "this version tests more" before believing it.** Preferring the in-turn commit
   ordering (§4.5) cost three rounds. One minute of reading `_commits_above` would have
   shown it tested nothing extra.

3. **Classify before fixing.** I applied Shape-1's rule ("widen the margin") to a Shape-4
   problem four times. Shape 4 is immune to it by construction, and I could have derived
   that on paper after the first failure instead of after the fourth.

4. **Enumerate the shape, don't wait for the stress run to find each instance.** Once C3 was
   understood, grepping for every scenario hook under a short window found a latent flake in
   `test_contest_cli.py` that no run had hit yet. That is the cheap direction; an 8-minute
   stress pass per instance is the expensive one.

5. **When a fixture "should" be fast and isn't, suspect the plumbing, not the box.** Three
   rounds were spent padding margins around a `flush()` that was blocking the reader thread.
   "The box is slow" is a hypothesis that explains everything and predicts nothing.

6. **Grep for the *concept*, not the syntax.** Appendix B.3's greps find `elapsed <`,
   `timeout=` and short rendezvous. They do not find `turn_timeout_sec=30`, because it
   reads as production config — and that is how cause T survived 48 green runs (§9.1). The
   question to grep for is not "what looks like a deadline" but "what can end the work this
   test is measuring".

### The one that is worth keeping

All five collapse into a single habit, and it is not a technical one.

Every wrong turn in this ticket — mine and the five candidates' — was a moment where
something *looked* settled and the looking was cheap. The results file looked like five
failed attempts; one `grep` said otherwise. The in-turn commit ordering looked like it
tested more; one function said otherwise. The margins looked too tight; the pipeline said
otherwise. A green suite looked like a fixed suite; arithmetic said otherwise.

In each case the cost of checking was under a minute, and the cost of not checking was
measured in hours of stress runs.

That is the thing to take from this. Not the four shapes — those are specific to timing
bugs and you will meet them again anyway. The habit is: **when a conclusion is about to
change what you do next, find the cheapest thing that could refute it, and spend the
minute.** The stress loop was never really the tool that solved this. It was the tool that
kept refusing to let an unchecked conclusion stand, six times in a row, until the checking
got done.

---

## 11. What is kept, what is lost, and what to re-collect

An honest inventory, so the next round knows what it can lean on.

### Kept, and reproducible from the repo

| artefact | where |
|---|---|
| The five candidate patches | `kc37/*.patch` |
| Their stress logs — the single most useful input | `kc37/tests_results.txt` |
| The ticket, with the original `-n 4` loop statistics and the `pytest-timeout` stack dump | `epic-tasks/84-fl-1-*.md` |
| First fix, as one commit with the full reasoning in its message | `e500d40` |
| Everything found afterwards, as one commit | `89012d8` |
| Both as standalone patches | `fl1/FL-1-84-Opus5.patch`, `fl1/FL-1-84-Opus5-followup.patch` |
| This document | `POSTMORTEM-FL-1.md` |
| Every fix's reasoning, inline, next to the code it explains | the diffs themselves |

The commit messages are deliberately long. Each one states the failure it was reacting to,
verbatim, and why the previous round's fix could not have covered it. Reading
`git log e500d40..89012d8` in order is the same story as §4 of this document, told by the
person who did not yet know how it ended.

### Lost

* **The per-run logs of rounds 0–5.** I deleted each batch before starting the next
  (`rm -f $SP/*.log`). What survives is in this document and in the commit messages: which
  test failed, in which pass, with the traceback quoted where it mattered. The raw logs are
  gone.
* **Exact pass counts for the early rounds.** The table in §4 is reconstructed from the
  commit messages and is accurate to the failure, but the "passes before red" column for
  rounds 0–5 should be read as *at least* that many, not exactly.

> **If this is done again: do not delete the logs.** A `--timeout` stack dump from a hung
> run is worth more than any amount of reasoning about what might have hung, and the
> ticket's own diagnosis section exists only because someone kept one.

### What is *not* in this document and would have to be re-collected

* **A measurement of the `EventTap` stall.** The mechanism is established by reading
  (`log.flush()` on the reader thread, per event) and by the failure disappearing, but
  nobody instrumented `_write_log` to record how long it actually blocked under load. A
  histogram of that would be the direct proof, and it would settle whether 0.5 s is the
  right cadence or whether it should be adaptive.
* **Whether the same hazard exists on other reader threads.** `EventTap` was found because
  it sat on a timing path. Nothing has audited the rest of the repo for
  "blocking disk write on a thread something is waiting on".
* **The `kc37-Dots3-note` gate-1 regression.** Two real failures
  (`intentional_design_note` firing on the block for `tools/metrics_collector.py::record`)
  that do not reproduce on this tree. Not a flake, not fixed here, and worth a look if that
  candidate is revisited. Note the trap it sits next to: that corpus test reads the **live
  source** of `MetricsCollector.record`, which family A rewrites — it is verified green, but
  anyone adding "deliberately" or "by design" to that docstring turns it red.

---

## Appendix A — minimal reproductions

```bash
# Family B — the coercion the guard missed
python3 -c "from unittest.mock import MagicMock; print(float(MagicMock()), float(True))"
# 1.0 1.0

# Family A — how expensive one record is, and whether the lock is why it serialises
python3 - <<'PY'
import time, pathlib, tempfile
from tools.auto.auto_metrics import AutoMetricsStream
d = pathlib.Path(tempfile.mkdtemp()) / "x"; d.mkdir(parents=True)
s = AutoMetricsStream(d)
t0 = time.monotonic()
for i in range(1000):
    s.record_gate2(f"T{i}", approved=True, feedback="x")
print("1000 serial records:", round(time.monotonic() - t0, 2), "s")   # 2.7 s after the fix
PY

# G — a held index.lock, and that the retry rides it out
python3 - <<'PY'
import subprocess, tempfile, threading, time, pathlib, sys
sys.path.insert(0, ".")
from tools.auto.git_manager import GitManager
d = pathlib.Path(tempfile.mkdtemp())
subprocess.run(["git", "init", "-q"], cwd=d, check=True)
(d / "a.txt").write_text("1\n")
gm = GitManager(d); gm.configure_identity()
subprocess.run(["git", "add", "-A"], cwd=d, check=True)
subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)
lock = d / ".git" / "index.lock"; lock.write_text("")
threading.Thread(target=lambda: (time.sleep(0.6), lock.unlink()), daemon=True).start()
(d / "a.txt").write_text("2\n")
t0 = time.monotonic()
gm._run(["git", "add", "-u"], "git add -u failed")
print("contended add recovered after %.2fs" % (time.monotonic() - t0))
PY
```

```bash
# the loop that found all of it
for i in $(seq 1 6); do
  ( pytest tests -n 8        > /tmp/fl_${i}_1.log 2>&1 &
    pytest tests_bugfix -n 8 > /tmp/fl_${i}_2.log 2>&1 &
    pytest tests -n 8        > /tmp/fl_${i}_3.log 2>&1 &
    pytest tests_bugfix -n 8 > /tmp/fl_${i}_4.log 2>&1 &
    wait )
done
grep -h '^FAILED' /tmp/fl_*.log | sort | uniq -c    # must be empty
```

---

## Appendix B — the diagnostic toolkit, in full

Every script below is one I actually ran. Two of them did the work; one was a dead end and
is kept because the dead end is informative.

### B.1 The workhorse — the stress loop with per-pass attribution

Everything was found by this. The shape matters: **log each of the four concurrent suites
separately**, print a one-line summary per pass so progress is visible across a 50-minute
run, and tally at the end.

```bash
SP=/tmp/fl1-logs; mkdir -p $SP; rm -f $SP/*.log

for r in a b c d e f; do
  echo "===== pass $r $(date +%T)"
  ( pytest tests        -n 8 > $SP/${r}1.log 2>&1 &
    pytest tests_bugfix -n 8 > $SP/${r}2.log 2>&1 &
    pytest tests        -n 8 > $SP/${r}3.log 2>&1 &
    pytest tests_bugfix -n 8 > $SP/${r}4.log 2>&1 &
    wait )
  for i in 1 2 3 4; do echo "  run $i: $(tail -1 $SP/${r}$i.log)"; done
  grep -h '^FAILED' $SP/${r}*.log          # fail fast, visibly, per pass
done

echo "=== all FAILED across $(ls $SP/*.log | wc -l) suite runs:"
grep -h '^FAILED' $SP/*.log | sort | uniq -c | sort -rn
```

The `uniq -c` at the end is the part that matters over many rounds: **a flake that appears
twice is a different problem from two flakes that appear once**, and only the tally tells
you which you have.

Per-run statistics from the same logs, which is where §9's spread table comes from:

```bash
grep -ho 'in [0-9.]*s (' $SP/*.log | tr -dc '0-9.\n' | sort -n | awk '
  {a[NR]=$1; s+=$1}
  END {printf "n=%d  min=%.0fs  median=%.0fs  max=%.0fs  sum=%.1f h\n",
              NR, a[1], a[int(NR/2)], a[NR], s/3600}'
```

### B.2 The one that proved the guards — regression injection

A green test proves nothing about a guard. Break the production code *in the exact way the
guard exists to catch*, confirm red, restore, confirm green. This is what turned "the suite
passes" into "the suite would notice".

```bash
SP=/tmp/fl1-logs
cp tools/contest/kilo_client.py $SP/kc_good.py          # 1. save

python3 - <<'PY'                                         # 2. inject, surgically
import pathlib
p = pathlib.Path("tools/contest/kilo_client.py"); s = p.read_text()
old = "            return silence is not None or etype in _SESSION_EVENTS"
new = "            return etype in _SESSION_EVENTS   # INJECTED KC-12 REGRESSION"
assert old in s, "anchor moved — the injection would have been a no-op"
p.write_text(s.replace(old, new, 1)); print("regression injected")
PY

pytest tests/test_contest_kilo_client.py -q -rf | tail -5   # 3. must be RED

cp $SP/kc_good.py tools/contest/kilo_client.py              # 4. restore
pytest tests/test_contest_kilo_client.py -q | tail -2       # 5. must be GREEN
```

The `assert old in s` is not decoration. Without it a stale anchor silently injects
nothing, the tests pass, and you conclude the guard works when you never tested it.

Same pattern for the `index.lock` retry — collapse the ladder to a single attempt:

```python
s = s.replace("for attempt in range(self._LOCK_RETRIES):", "for attempt in range(1):", 1)
```

→ both lock tests fail. Restore → both pass.

### B.3 Searching for the *shape*, not the failure

After C3 was understood, the cheap move was to find every other instance before a stress
run cost 8 minutes finding one. This is how the latent `test_contest_cli.py` flake was
found — no run had ever hit it.

```bash
# every wall-clock upper bound in the test suites
grep -rn "monotonic() - started <\|monotonic() - t0 <\|elapsed <\|elapsed_ms <" \
     tests/ tests_bugfix/ --include=*.py

# every timeout literal, as a histogram — the outliers are the suspicious ones
grep -rhon "timeout=[0-9.]*" tests/*.py tests_bugfix/*.py \
  | sed 's/.*://' | sort | uniq -c | sort -rn

# every short rendezvous: a Barrier or a join that can break under load
grep -rn "Barrier\|\.wait([0-9]\|\.join([0-9]" tests/*.py tests_bugfix/*.py

# every silence window a test configures, and who runs a real hook under one
grep -rn "idle_event_timeout" tests/*.py
grep -rn "on_prompt" tests/test_contest_cli.py      # <- found the latent one

# every HTTP client pointed at a local stub
grep -rn "urlopen(.*timeout=\|requests\.\(get\|post\)" tests/ tests_bugfix/
```

The histogram is the highest-yield of these. `100 × timeout=5` in a suite is not a hundred
considered decisions; it is one habit — and habits are uniform enough to fix in one pass.

### B.4 Reading the candidates before their code

```bash
# what does each patch actually touch? (the command from §2.1)
for p in kc37/*.patch; do
  echo "=== $p"; grep '^diff --git' "$p" | sed 's/.* b\///'
done

# does it even apply, and how many files?
for p in kc37/*.patch; do
  echo "=== $p  ($(grep -c '^diff --git' $p) files)"
  git apply --check --3way "$p" 2>&1 | head -3
done

# multi-commit patches hide things: look for a second subject line
grep -n '^From \|^Subject' kc37/*.patch
```

That last one is how the KC-35 commit inside `kc37-SenSenova-6-8-var1.patch` surfaced —
`[PATCH 1/2]` in the header, and the *second* commit held all of its family-C work. Judging
that patch by its first commit alone would have missed the half worth taking.

### B.5 Micro-benchmarks that answered a question in seconds

```python
# Family B — is the coercion really the bug?  (2 seconds, settled it)
from unittest.mock import MagicMock
print(float(MagicMock()), float(True), float('3'))      # 1.0 1.0 3.0
```

```python
# Family A — how expensive is one record, and is the lock why it serialises?
import time, pathlib, tempfile
from tools.auto.auto_metrics import AutoMetricsStream
d = pathlib.Path(tempfile.mkdtemp()) / "x"; d.mkdir(parents=True)
s = AutoMetricsStream(d)
t0 = time.monotonic()
for i in range(1000):
    s.record_gate2(f"T{i}", approved=True, feedback="x")
print("1000 serial records:", round(time.monotonic() - t0, 2), "s")
#   before the fix: a substantial fraction of the 180 s timeout
#   after:          2.7 s
```

```python
# G — does the retry ride out a contended lock, and still fail on a stale one?
import subprocess, tempfile, threading, time, pathlib, sys
sys.path.insert(0, ".")
from tools.auto.git_manager import GitManager, GitError
d = pathlib.Path(tempfile.mkdtemp())
subprocess.run(["git", "init", "-q"], cwd=d, check=True)
(d / "a.txt").write_text("1\n")
gm = GitManager(d); gm.configure_identity()
subprocess.run(["git", "add", "-A"], cwd=d, check=True)
subprocess.run(["git", "commit", "-qm", "init"], cwd=d, check=True)

lock = d / ".git" / "index.lock"
lock.write_text("")                                   # somebody else holds the index
threading.Thread(target=lambda: (time.sleep(0.6), lock.unlink()), daemon=True).start()
(d / "a.txt").write_text("2\n")
t0 = time.monotonic()
gm._run(["git", "add", "-u"], "git add -u failed")
print("contended add recovered after %.2fs" % (time.monotonic() - t0))     # 0.76 s

lock.write_text("")                                   # a stale lock nobody will release
t0 = time.monotonic()
try:
    gm._run(["git", "add", "-u"], "git add -u failed")
except GitError:
    print("stale lock still raises after %.2fs" % (time.monotonic() - t0))  # 1.76 s
```

Both halves matter. The first proves the fix works; the second proves it is still
**bounded**, which is what stops a retry from quietly becoming a hang.

### B.6 Verifying a patch really is the working tree

Before handing anything over: apply the patch to a clean checkout of its base and compare
**tree hashes**, not diffs. A tree hash is the whole content of the tree in one number; a
diff can agree while a file is missing.

```bash
BASE=b97b7c1
TREE=$(git rev-parse HEAD^{tree})
SQ=$(git commit-tree $TREE -p $BASE -F msg.txt)        # one commit, HEAD's exact tree
git format-patch --stdout $BASE..$SQ > fl1/FL-1-84.patch

git worktree add -q --detach /tmp/verify $BASE
( cd /tmp/verify && git am --keep-non-patch < $PWD/fl1/FL-1-84.patch \
    && echo "applied: $(git rev-parse HEAD^{tree})" )
echo "mine   : $(git rev-parse HEAD^{tree})"           # must be the same string
git worktree remove --force /tmp/verify
```

`git commit-tree` is the useful piece: it builds a single squashed commit from the current
tree without touching the branch, rebasing, or risking a conflict resolution that silently
drops a hunk.

### B.7 The dead end — a synthetic load generator that reproduced nothing

Written to get a fast feedback loop instead of paying 8 minutes per stress pass. It did not
work, and the way it failed is worth keeping.

```python
# load.py — synthetic CPU load: N busy processes that also churn the disk a little
import multiprocessing as mp, os, sys, time, hashlib, tempfile

def burn(stop_at):
    h = hashlib.sha256()
    d = tempfile.mkdtemp()
    i = 0
    while time.time() < stop_at:
        h.update(os.urandom(4096))
        i += 1
        if i % 2000 == 0:
            with open(os.path.join(d, "x"), "wb") as f:
                f.write(h.digest() * 100); f.flush(); os.fsync(f.fileno())

if __name__ == "__main__":
    n, secs = int(sys.argv[1]), float(sys.argv[2])
    stop = time.time() + secs
    ps = [mp.Process(target=burn, args=(stop,)) for _ in range(n)]
    [p.start() for p in ps]; [p.join() for p in ps]
```

```bash
python3 load.py 56 420 &                      # 56 busy processes on 8 cores
sleep 5; uptime                               # load average: 75.81
for i in 1 2 3 4; do
  pytest tests/test_contest_kilo_client.py -n 8 -q --timeout=300 -rf > k_$i.log 2>&1
  echo "run $i: $(tail -2 k_$i.log)"
done
```

**Result: load average 75, four runs, zero failures** — on the very tests the real stress
command kills.

That negative result is the first real evidence for what became §5. CPU starvation alone
does not reproduce these; what does is *the whole pipeline under a saturated disk* — 32
pytest workers forking, writing and `fsync`ing at once. `hashlib` in a loop makes a box
busy; it does not make `write(2)` block.

At the time I read this as "my load generator is too weak" and moved on, which cost three
more rounds. The correct reading was **"the bottleneck is not CPU"** — which is three
quarters of the way to the answer.

> **Lesson.** A reproduction attempt that fails tells you where the cause *is not*. Write
> that down before moving on.

---

## Appendix C — every race, in one map

```
 PRODUCTION                                            TESTS / FIXTURES
 ──────────                                            ────────────────

 A  AutoMetricsStream._lock                            C1  margins & bounds
      └─ held across ─► MetricsCollector.record            ├─ slack < 1 s
                            └─ os.fsync()  ✗                └─ bounds around unbounded setup
      fix: collector owns its lock; fsync on cadence        fix: widen the SLOW path

 B  _task_budget_seconds                               C2  pulse(times=N)
      └─ float(MagicMock()) → 1.0  ✗                        └─ beats ran out before idle  ✗
      fix: accept only a real positive number               fix: times=None

 C5 EventTap._record                                   C3  scenario hook
      ├─ log.flush() per event, reader thread ✗             └─ git commit, no events  ✗
      └─ wait(): time.sleep(0.2) poll         ✗             fix: beat THROUGH the hook
      fix: notify first, flush on cadence,                      (in the base fake)
           Condition instead of polling
                                                       C4  run_round
 G  GitManager._run                                        └─ no tap handshake → beats
      └─ index.lock: File exists → 128  ✗                       dropped  ✗
      fix: bounded retry on that signature                 fix: wait for subscriber count
                                                                to RISE

                                                       C6  harvest tests
                                                           └─ commit inside the window  ✗
                                                           fix: commit before the run

                                                       S   StubServer
                                                           └─ 10 s vs unscheduled
                                                              serve_forever  ✗
                                                           fix: hang guard, 120 s
```
