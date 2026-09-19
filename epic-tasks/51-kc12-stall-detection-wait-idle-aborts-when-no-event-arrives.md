# KC-12 — `wait_idle` aborts on silence: no `/event` for `idle_event_timeout_sec` seconds is a stall, not a slow answer

**Status:** open — round 51 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`).
**Severity:** MEDIUM
**File:** `tools/contest/kilo_client.py` (`wait_idle`)
**Symbol:** `wait_idle`, `IdleResult`
**Round:** 51
**Size:** S
**Source:** [KC-1](40-kc1-kilo-client-and-the-fake-server-built-from-the-probe.md) gives `wait_idle` one overall `timeout`; [KC-6](45-kc6-runner-prompt-wait-harvest-rework-in-the-same-session-for-n-agents.md) already assumes a *separate* `idle_event_timeout_sec` exists ("no event for `idle_event_timeout_sec` → abort → STALLED", line 56, and the acceptance case "`idle_event_timeout_sec = 1` with a fake that emits nothing → `STALLED` within 3 s", line 109) but KC-1's `wait_idle` signature never grew that parameter. This ticket closes that gap at the primitive, so KC-6 can wire it through instead of inventing it.
**Depends on:** KC-1 (`tools/contest/kilo_client.py` must exist), KC-2 (`contest.ini`'s `[contest] idle_event_timeout_sec` — this ticket is the primitive KC-2's key is meant to drive; it must not invent its own constant or a second config path).
**Also touches:** `tests/_kilo_fake.py` (needs a turn shape that goes silent for N seconds before `idle`/nothing), `tests/test_contest_kilo_client.py`

---

## What happens today

`wait_idle(tap, session, timeout, ...)` only bounds the *whole* wait: a
session that goes silent 5 seconds after a 300-second `timeout` and a
session that dribbles out a `session.status busy` event every 4 seconds for
299 seconds both wait the full 300 seconds before anything happens. The
first case is dead (a hung provider, a dropped connection, `kilo serve`
wedged) and should be caught in seconds; the second is alive and must not
be killed early. `timeout` alone cannot tell them apart.

## What must change

1. `wait_idle(tap, session, timeout, *, idle_event_timeout=None, on_permission, on_question) -> IdleResult`
   — `idle_event_timeout` (seconds, default `None` = disabled, matching
   today's behaviour exactly when omitted). **The caller (KC-6's runner)
   is the only place that decides the value — it passes
   `config.idle_event_timeout_sec` straight from `contest.ini`'s `[contest]`
   section (KC-2, default `300`). `wait_idle` itself never hardcodes a
   number or reads `contest.ini` — it is a pure function of its
   arguments**, so the same primitive is usable from a test, from
   `kilo_hello.py`-style scripts, or from any future caller with its own
   idea of what "too quiet" means.
2. When set: track the timestamp of the *last* event seen for this
   `sessionID` (any type, not just the ones `wait_idle` already filters
   on — a `file.edited` or `session.status busy` counts as "alive"). If
   more than `idle_event_timeout` seconds pass with no such event, call
   `session.abort` (as timeout already does) and return
   `IdleResult(status="timeout", ...)` — same status the overall timeout
   produces; the caller (KC-6) tells the two apart via elapsed time if it
   cares, this primitive does not need a fourth status.
3. The overall `timeout` and `idle_event_timeout` race independently —
   whichever fires first wins. A session idle within `timeout` but that
   goes silent for longer than `idle_event_timeout` partway through is
   still caught.
4. No busy-poll: implement with the same `tap.wait(pred, timeout=min(remaining_overall, remaining_since_last_event))`
   loop already in KC-1's `wait_idle`, just recomputing the per-iteration
   timeout from "time since last event" instead of only "time since
   start".

## Why the value must stay in `contest.ini`, not a code constant

A hardcoded or too-short `idle_event_timeout` is a footgun: an agent
turn that runs a slow local command inside its own session (a full
`pytest` run, a big `git clone`, a long build) can legitimately produce
no `/event` traffic for minutes — that is not a stall, Kilo is just not
reporting anything while a tool call blocks. Killing that turn early
turns a slow-but-working attempt into a `STALLED` verdict and throws away
real work. This is exactly why KC-2 put `idle_event_timeout_sec` in
`contest.ini` at `300` (5 minutes) instead of a small default — the
number is a judgment call about the slowest legitimate silent tool call
the roster's agents are expected to run, and it must stay operator-tunable
per round, not buried in `wait_idle`'s signature as a constant. This
ticket only builds the mechanism; KC-6 is responsible for sourcing the
number from config and for never calling `wait_idle` with a value shorter
than what a real test/build command in that round's tasks needs.

## Test (this is the scenario asked for)

Add to `tests/_kilo_fake.py` a turn shape:
```python
{"pause_before_idle_sec": 120}   # emits session.status busy, then sleeps 120s, never sends idle
```
Add to `tests/test_contest_kilo_client.py`:
```python
def test_wait_idle_aborts_on_stall(fake_kilo_server):
    # turn configured with pause_before_idle_sec=120
    result = client.wait_idle(tap, session, timeout=300, idle_event_timeout=60,
                               on_permission=..., on_question=...)
    # must return well before 120s — bounded by idle_event_timeout, not by pause length
    assert result.status == "timeout"
    assert result.elapsed < 70          # 60s idle_event_timeout + slack, not 120s
    assert fake_kilo_server.recorded_abort_for(session.id)
```
In the real ticket this constant is the point: **do not wait out the full
simulated pause** — 60s `idle_event_timeout` must cut off a 120s silence at
~60s, proving the stall path fires independently of the overall 300s
`timeout`. (When actually run, shrink both to test-speed values — e.g.
`pause_before_idle_sec=2`, `idle_event_timeout=0.5` — so the suite stays
fast; the numbers above are the illustrative 120/60 from the ticket, not
the literal sleep the test suite should perform.)

## Acceptance

- [ ] `wait_idle` takes `idle_event_timeout` (optional, default `None`);
      omitting it reproduces today's behaviour byte-for-byte (existing
      KC-1 tests still pass unmodified).
- [ ] A session silent for longer than `idle_event_timeout` is aborted and
      returned as `status="timeout"` well before the overall `timeout`
      elapses, proven by a fast (sub-second-scale) test per above.
- [ ] A session that keeps emitting *any* event (not just the ones already
      matched) within `idle_event_timeout` of each other is never stalled
      out early, even if it runs past several `idle_event_timeout`
      windows before finishing.
- [ ] `python3 scripts/sync_test_tiers.py` run; `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (sequentially).

## Out of scope

- Reading `contest.ini` from `kilo_client.py` — `wait_idle` only accepts
  the already-resolved number; KC-6 wires `config.idle_event_timeout_sec`
  through when it calls `wait_idle` — that part is KC-6's job once this
  primitive exists.
- Distinguishing "stalled" from "ran past overall timeout" with a separate
  `IdleResult.status` value — not needed until a caller actually needs to
  tell them apart.
- Deciding the default value of `idle_event_timeout_sec` — already fixed
  at `300` in KC-2's `contest.ini`; not this ticket's call to change.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

Every line below is a way a KC-5 entry lost points on the round bench;
the scorer checks all of them mechanically, so check them yourself first.

- [ ] `python3 --version` on the judge is **3.10.12**. Every new/changed
      module imports there: `python3 -c "import tools.contest.kilo_client"` from the
      repo root **and** from `/tmp` (with `PYTHONPATH` unset — the sys.path
      bootstrap is yours to ship). No backslash and no nested same-quote
      inside an f-string expression (a 3.12-only `f"{x.split("\t")}"`
      is a `SyntaxError` here and scores 0).
- [ ] Exactly **one** commit on top of the base: `git log --oneline <base>..HEAD`
      prints one line. Only this ticket's work is in it — no other KC
      ticket, no "while I was here" fixes; amend, do not stack.
- [ ] `git diff --stat <base>..HEAD` names only the files under **File:**
      and **Also touches:** (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim — **Symbol:** is the
      contract the bench calls: `wait_idle(tap, session, timeout, *, idle_event_timeout=None, on_permission, on_question) -> IdleResult`, `IdleResult`. Read the modules this ticket
      builds on before calling them (`tools/contest/kilo_client.py` — `EventTap.wait(pred, timeout=)` and the KC-1 `wait_idle` loop you extend); a keyword you invented
      (`run_tests_flag=`, `base="HEAD"`) is a `TypeError` on every scenario.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean (the new test
      file has its `.smoke_tests/` symlink; the pre-commit hook runs this).
- [ ] The new test is red without the change: check the test file alone
      out onto the base (`git stash` / `git checkout <base> -- <src>`),
      run it, see it fail; restore.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical:
      `git diff <base> HEAD -- tools/auto/collect_bridge.py` is empty.
- [ ] Then, and only then, `scripts/append_task.py` from the worktree;
      open `runs/<you>/PROGRESS.csv` and see your row with the sha of the
      one commit (the script stores `--outcome DONE` as `FIXED`; that is fine).
- [ ] What you hand in is `git format-patch <base>..HEAD` of that one
      commit — not a raw `git diff`, not the whole branch, not an empty file.
- [ ] The KC-1 tests in `tests/test_contest_kilo_client.py` are **unmodified**
      (`git diff <base> HEAD -- tests/test_contest_kilo_client.py` shows only
      added tests) and still pass — omitting `idle_event_timeout` is today's
      behaviour byte-for-byte.
- [ ] The stall test finishes in under 3 s wall time and asserts the fake
      recorded an `abort` for that session; the numbers are test-speed
      (`pause_before_idle_sec=2`, `idle_event_timeout=0.5`), not 120/60.
- [ ] No number is hardcoded in `wait_idle`; `grep -n 'contest.ini\|300' tools/contest/kilo_client.py` finds nothing new.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
