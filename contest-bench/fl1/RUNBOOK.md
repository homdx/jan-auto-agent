# FL-1 as a benchmark ticket — the one nobody has beaten

FL-1 (`epic-tasks/84-fl-1-full-tests-suite-is-not-reproducible-green-under-n-4-three-independent-root-causes.md`)
is the hardest ticket filed so far, and it is hard in a way no other ticket
in this repo is. It has no new feature to build and no API to design: the
whole task is *the suite is not reproducibly green under load, make it
green without weakening a single assertion that catches a real regression*.

Round 84 handed it to five agents. **None of them finished it.** Two moved
the needle and each stopped with exactly one residual failure; the two
residuals were disjoint — they were each other's missing half. It took
Opus 5 over three hours and five full stress rounds to close.

And that was still not the end. `e500d40` — the patch this runbook first
called the reference — went 48 consecutive stress runs green and was *still
incomplete*: a sixth round on a harder-loaded box found four more failures,
one of them the production bug that had been underneath the whole family
(C5, below). The full account is **`POSTMORTEM-FL-1.md`** (repo root; copy
in `docs/kilo-contest/`) — ten causes, six rounds, the taxonomy in §8 and
the diagnostic toolkit in Appendix B. Read it before scoring a rerun.

That is the measure of the ticket: two green stress passes in a row are not
evidence of a finished fix, and the strongest single attempt on record
needed twelve further stress runs after it looked done.

That failure pattern is why this folder exists. FL-1 is the best
intelligence test in the repo: it cannot be faked, it cannot be pattern
matched from the ticket text, and a candidate that has not actually fixed
it produces a red suite that looks exactly like a candidate that has.

## 1. The base

The tree every agent starts from is commit **`6471230`**
(`epic-tasks: ideas/ merged in — KC-39..KC-44 promoted …`). That is the
`base_ref` for any rerun: the ticket is `next` in `epic-tasks/`, all three
of its named causes are live, and the three further families below are live
and undocumented.

The reference solution is **`e500d40`** (13 files, +717/−194) **plus the
follow-up commit immediately after it on `kc`** — the one that adds
`POSTMORTEM-FL-1.md` and rewrites `EventTap._record` / `EventTap.wait` in
`tools/contest/kilo_client.py`. `e500d40` alone is *not* a complete fix; it
is round 0–5 of six. Do not read either before scoring a round — read the
candidates first, rank them by the stress run, then read `e500d40`'s commit
message (families A–C6) and `POSTMORTEM-FL-1.md` (all ten causes, plus G
and S).

## 2. Why the ticket resists a single pass

The causes are **layered**. Each one only becomes visible once the one above
it is fixed, so a candidate that has genuinely made progress still sees a
red suite and cannot tell "my fix did not work" from "the next layer just
surfaced". Opus found six families in the timing group alone, one per stress
round, in this order:

| # | family | what it actually is |
|---|---|---|
| A | metrics lock spans a blocking `fsync` | in the ticket. 1000 serialized disk barriers; a real production hazard |
| B | `float(MagicMock()) == 1.0` | in the ticket. Silently installs a 1-second wall-clock budget nobody configured |
| C1 | the margin is absolute, not a ratio | in the ticket. **Widen the slow path, do not tighten the bound** |
| C2 | a counted heartbeat is a race on its total length | the hook's `git commit` pushes idle past the last of 60 beats |
| C3 | the gap that matters is inside the hook | bracketing a hook with one beat each side leaves the commit uncovered |
| C4 | **a dropped beat is a lost event — and this one is not timing at all** | `run_round` never waited for the event stream to connect; every beat landed on nobody. No amount of widening would ever have fixed it |
| C5 | **emission is not delivery — `EventTap` starved the clock it feeds** | `_record` did a `flush()` **per event on the reader thread**, so the next SSE line was not read until that `write(2)` returned — seconds, under dirty-page writeback. `wait` added a 200 ms poll on top. The silence clock is measured from when the caller *sees* an event, so a turn that never stopped emitting was declared stalled: **the agent's own event log starved the agent**. Not observable from a fixture, and not fixable with margin |
| C6 | the harvest tests | a `git commit` inside a wall-clock silence window cannot be made to work: the hook's work is unbounded, the window is not |
| G | `.git/index.lock` is a transient, not a failure | **production.** One collision costs a task its commit — a red suite here, lost agent work in a real run (§6) |
| S | a client timeout against a server that had not been scheduled | `StubServer.start()` binds in `__init__` and hands `serve_forever` to a daemon thread, so the socket queues a connection before anything serves it. The reply is not slow — nobody has run yet |

Three of these are traps that punish the *correct-looking* reflex:

- **C4 punishes "it's flaky, widen the window."** The natural move on a
  timing flake is to tune constants. C4 needs a handshake — `_make_backend`
  waiting for the subscriber count to **rise** after building each backend
  (not merely to be non-zero, which is wrong in a multi-agent round).
- **C5 inverts C1**, the rule the candidate has just learned. For a test
  that must *survive* a window rather than trip it, tolerance for a starved
  box `== window == how long the test runs`. There is no margin to widen and
  no beat interval that substitutes: robustness is bought with wall time or
  not at all. The two tests of that shape are among the slowest in the suite
  and their docstrings say why.
- **C6 is a call Opus got wrong twice** before killing it, because the
  wrong shape survived a wider window *and* a heartbeat through the hook
  before the fourth pass caught it again. `_commits_above` reads the branch
  at harvest time and cannot tell when a commit was made — so the test that
  looked stricter was proving nothing.
- **C5 punishes treating the transport as infrastructure.** Every candidate,
  and the first five rounds by hand, looked for the starvation in the code
  under test. It was in the *logging* of the event stream — a `flush()` on
  the reader thread. Nothing in the ticket points at `EventTap`, and no
  fixture can see it: the emitter's timestamps are all on time.

### The taxonomy — four shapes, four different rules

Straight out of `POSTMORTEM-FL-1.md` §8. Every failure in this ticket is one
of these four, they take **four different fixes**, and confusing them is
what turned this into six rounds. The single most useful page for scoring a
candidate: read which shape it thought it had.

| shape | looks like | the rule |
|---|---|---|
| 1 — fast path, not the slow path | a bound separating two code paths | **widen the slow path, never tighten the bound.** A 0.5 s window against a 60 s pause bounded at 20 s proves more than the same thing bounded at 1.5 s |
| 2 — a hang guard wearing an assertion's clothes | `subprocess.run(timeout=15)`, `urlopen(timeout=10)` | **make it big and name it.** These catch nothing; `pytest-timeout` is the real hang guard and it prints a stack dump |
| 3 — an upper bound measured around unbounded work | `elapsed < backoff + 2 * turn_timeout` around a whole harness | **delete it, or measure where the thing happens.** That one read 78 s on a loaded box while the claim it stood for was true |
| 4 — must survive a real-time window | the shape with no margin to widen | **take it out of real time.** Window == runtime == tolerance is one number. Drive the unit over a stub on a fake clock; leave integration only what is load-proof |

A candidate that applies rule 1 to a shape-4 test — the reflex, and the most
common single error — has not understood the ticket, however green it is.

## 3. What the round is really scoring

Not "is the suite green once". Green once is luck. The scoreable claims are:

1. **Reproducibly green under the operator's stress run** (§4), several
   times, on a box with other load.
2. **No assertion that catches a real regression was weakened.** Deliberately
   short deadlines, where firing *is* the claim, must be untouched; only
   scaffolding deadlines and upper bounds that duplicate a load-independent
   assertion may move. A candidate that turns the suite green by raising
   `--timeout` or deleting a bound has failed, however green it is.
3. **The ground rules** — `CollectBridge._shrink` byte-identical,
   `scripts/judge_epic_round.py` untouched, `scripts/sync_test_tiers.py --check`
   clean, one commit.
4. **Did it find anything under the three named causes?** This is the
   discriminator. The ticket names A, B and C1. A candidate that fixes
   exactly those three has done the reading; a candidate that reaches C4 or
   C5 has done the work.
5. **Was each fix matched to the shape of its failure?** (the taxonomy in
   §2). Widening a shape-4 window, or keeping a shape-3 bound and merely
   enlarging it, is a wrong answer that goes green.
6. **Are the guards verified by failing?** A green suite proves nothing if
   the guard cannot fail. The reference verified both new guards by
   **injecting the regression each exists to catch** — remove the silence
   clock, remove the lock retry, watch the test go red (`POSTMORTEM-FL-1.md`
   §9). A candidate that cannot demonstrate this for the guards it touched
   has an unmeasured claim.
7. **Was a production bug found, or only tests edited?** Four of the ten
   causes are production bugs (A, B, C5, G), two of them in code nobody
   suspected, inside tests already written off as flaky. A patch that is
   *entirely* test-side has by construction missed C5 and G.
8. **Does anything left in the suite still bet on wall time?** The shape
   greps in `POSTMORTEM-FL-1.md` Appendix B.3 enumerate every wall-clock
   upper bound, every timeout literal, every short rendezvous and every
   silence window in both test roots. They found a latent flake no run had
   yet hit. Run them against the candidate's tree; a surviving shape-3 bound
   is an open item even if it never fired.

### The acceptance bar this ticket actually sets

The bar moved once already, and this is where it now sits:

- **48 consecutive green suite runs is the passing evidence**, not two or
  three — 186 072 test executions, 1 h 40 min of wall clock, on a box under
  other load. `e500d40` looked finished long before that and was not.
- **A 38 % spread in suite runtime** between the fastest and slowest of
  those runs is the scale of noise every surviving margin has to absorb.
  Any margin narrower than that is a future red run.
- **A red run anywhere is a fail** — there is no "known flaky" allowance in
  this ticket, because "known flaky" is the bug.

## 4. The stress run

The operator's own command — four suites, **32** worker processes
(4 × `-n 8`) on 8 cores, a ~4× oversubscription ratio, which is what every
margin in this ticket had to survive:

```bash
pytest tests -n 8 & pytest tests_bugfix -n 8 & \
pytest tests -n 8 & pytest tests_bugfix -n 8 &
```

This is the whole bench. It needs no fake provider, no `kilo` binary and no
scenario file — the suite *is* the test data, and the load is the input.
Run it on `6471230` first: it must be red, on a different test each time.

One run is not a result. FL-1's own Diagnosis was 19 runs of `pytest tests -n 4`,
8 red, a different victim every time — so a candidate needs several clean
stress runs before it counts, and a single red run anywhere is a fail.

Per candidate:

```bash
S=../cb-fl1                                  # outside the repo tree
git worktree add $S/<entry> <entry-sha>
( cd $S/<entry> && git diff --stat 6471230..HEAD && \
  git diff 6471230 HEAD -- tools/auto/collect_bridge.py | wc -l && \
  python3 scripts/sync_test_tiers.py --check | tail -1 )
# then the stress command above, from inside $S/<entry>, several times
```

**Never run two candidates' stress runs at once** — the whole point of the
command is that it saturates the box, so a second one invalidates both.
They run sequentially, one candidate at a time, and the two test roots
(`tests`, `tests_bugfix`) are never combined into one pytest invocation.

## 5. Reading a red run

The trap when scoring this round is that **most failures in a candidate's
log are not that candidate's bug**. Four of round 84's five entries were
KC-34/35/37 candidates carrying an unfixed FL-1 on top, so nearly every red
line in their logs is a leaking FL-1 flake. Score by *which family is still
firing*, not by the failure count:

| symptom | family |
|---|---|
| `TestThreadSafety` slow / 180 s timeout on the metrics tests | A |
| a 1-second budget nobody configured, in an outer-loop test | B |
| a bound missed by a small margin under load | C1 |
| `test_events_of_the_session_keep_a_turn_alive` aborted at READY | C2 / C5 |
| an error-turn test failing around a hook's `git commit` | C3 / C6 |
| a round that prompts, beats, and sees **nothing** | C4 |
| `CommitOnSuccess: git error for task T1 — git add -u failed` | the `index.lock` bug (§6) = G |
| a turn aborted as stalled whose event log shows it emitting throughout | C5 — the tap, not the turn |
| a loopback POST to a stub server timing out at 10 s | S |

## 6. The production bug hiding in the flakes

The stress run exposed a real one, and a candidate that reports it has
earned a point the ticket never asked for: git holds `.git/index.lock` for
the whole of any index-writing command, and a second git that finds it
there exits 128 **without waiting**. `GitManager` had always *named* stale
locks in its error text and never waited for a held one, so one collision
costs a task its commit — a red suite here, and lost agent work in a real
autonomous run. `e500d40` retries that one signature, 8 attempts 0.25 s
apart, bounded on purpose (a stale lock never clears, so the ladder costs
two seconds and then raises the same error). An ordinary git failure is not
retried. Three tests ship with it and the first fails without it.

## 7. One thing to know before rerunning

`tests/test_gate1_corpus_precision.py` reads the **live source** of
`MetricsCollector.record`, which family A rewrites. It is green on
`e500d40`, but anyone adding "deliberately" / "by design" to that docstring
turns it red — `intentional_design_note` fires on the block. One round-84
candidate lost two tests to exactly that and it was a real regression in
the candidate, not a flake.

## 8. What the first pass still missed — read this before calling a rerun done

`e500d40` was 48 stress runs green when it was committed. Round 6, on a box
under heavier load, produced four more failures. Every one of them is a
pattern a rerun can hit, and three of the four are *not* in the ticket:

| what fired | what it actually was | shape |
|---|---|---|
| a turn that never stopped emitting, declared stalled | **C5** — `EventTap._record` flushing per event on the reader thread, plus a 200 ms poll in `wait` | 4 → fix is "out of real time", not margin |
| `test_events_of_the_session_keep_wait_idle_alive` red at an 8 s window — the **fourth** round for this one test | window == runtime == tolerance. Widening only makes it slower. Now driven over a scripted tap on a **fake clock**; the integration twin keeps only the load-proof half | 4 |
| `test_retry_backoff_is_observed` at 78 s | an upper bound measured around `_run_one` — real git worktrees, two turns, the harvest. None of that is the backoff. Deleted; the real claim is the turns' own timestamps | 3 |
| the lock-retry tests from round 5 — Opus's *own* previous fix | a `time.sleep(0.5)` releaser racing a bounded ladder, and a stopwatch around a subprocess. Now the release happens *in* the backoff, via a named `GitManager._backoff` seam, and the ladder is **counted, not timed** | 3 + 4 |

Two lessons for the scorer:

1. **The fixer reintroduces the bug.** Round 5's fix shipped two fresh
   wall-clock bets. Check a candidate's *new* tests against the taxonomy
   with the same eye as the old ones.
2. **The same test failing a fourth time means the shape is wrong**, not
   that the number is. Four consecutive "widen it" passes on one test is
   the signature of a misclassified shape-4.

## 9. Standing record

| round | date | entries | finished | reference |
|---|---|---|---|---|
| 84 | 2026-09-22 | 5 (`Sonet5-FL-1-84`, `kc37-Dots3-note`, `kc37-SenSenova-6-7-var1`, `kc37-SenSenova-6-7-var2`, `kc37-SenSenova-6-8-var1`) | 0 | — |
| 84 (hand) | 2026-09-22 | Opus 5, >3 h, 5 stress rounds | partially — 4 causes left | `e500d40` |
| 84 (hand, follow-up) | 2026-09-23 | Opus 5, round 6 + 12 further stress runs | yes — 48 consecutive green | `e500d40` + the follow-up commit, `POSTMORTEM-FL-1.md` |

`Sonet5-FL-1-84` owns families A and B in the reference patch unchanged, and
had C1 half-right (it shrank the beat where the answer was to widen the
window) plus C6's correct shape, which Opus overrode twice before arriving
back at it. `kc37-SenSenova-6-8-var1`'s single residual failure was exactly
family B. Neither reached C2–C5.

Beat it: a candidate that closes FL-1 from `6471230` in one pass, reproducibly
green under §4, with no weakened assertion, is the first one to. Note what
"closes" now means — all ten causes, including C5 and G, which are
production bugs the ticket never names, and 48 consecutive green runs rather
than a handful. The strongest attempt on record took two sittings to get
there.
