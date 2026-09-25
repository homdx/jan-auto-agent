# KC-63 — a turn ends on its own `session.idle`, not on one left over from the error before it

**Status:** queued — found 2026-09-25 reading round 106 (KC-59): `mimo-v2-5` went `GAVE_UP` after three `REWORK`s in 14 min while it was working the whole time. Every turn after its first `session.error` "ended" 0.1 s after its prompt, and the harvest ran while the agent was still editing.
**Severity:** HIGH (a model that recovers from one dropped stream is scored three times on a half-written tree and given up; its prompts pile up in Kilo's queue, so from then on the runner is always one turn behind the agent)
**File:** `tools/contest/kilo_client.py`, `tools/contest/backend.py`, `tools/contest/runner.py`, `tests/_kilo_fake.py`
**Symbol:** `EventTap`, `KiloClient.wait_idle`, `ContestBackend.mark` / `wait_idle`, the runner's `PROMPTED → WAITING` step
**Round:** 110
**Size:** S
**Source:** round 106, `contest-out/106/mimo-v2-5/` (`events.jsonl`, `turns.jsonl`) and `contest-out/106/state.json`. The times are seconds after the round started at 08:30:21 UTC.

**Depends on:** KC-1 (the persistent cursor), KC-12 (silence clock), KC-19/KC-45 (retry in the same session), KC-22 (continue on uncommitted work).
**Also touches:** KC-54 (a `ContextOverflowError` opens a *new* session, so the leftover idle has a different `sessionID` there and is filtered out; that path is safe, see the table below).

---

## How we met it

1. **+369.** The provider drops mimo's stream. For the same session and at
   the same millisecond, Kilo sends one `session.error` (`UnknownError`,
   "the model's provider interrupted the response before it finished") and
   then **two** `session.idle` events.
2. `wait_idle` returns on the `session.error` (`kilo_client.py` ≈ line 1243,
   anchor `if etype == "session.error":`). The two `session.idle` events
   stay in the tap after the cursor. `EventTap.wait` hands out every event
   exactly once from a persistent cursor, so they are still waiting to be
   read.
3. **+384.** After the KC-45 backoff (15 s) the runner sends `RETRY_PROMPT`
   and calls `wait_idle`. The first event it reads is the first **stale**
   `session.idle` from +369, so the turn is `idle` in **0.15 s**
   (`turns.jsonl`: `retry`, `sent_at` 1790325405.30, `idle_at` 1790325405.45).
4. The tree has uncommitted edits, so KC-22 sends a `continue`
   ("Your turn ended before anything was committed…"). Its wait reads the
   **second** stale idle, and that turn is 0.14 s.
5. At +384.6 a second `continue` is sent. Kilo is still busy on the retry,
   so it **queues** both continue prompts: `session.queue.changed`
   `queued: ["msg_0d7b5b75a…", "msg_0d7b5b82d…"]`. Each queued prompt later
   ends with `session.turn.close reason: superseded` plus a `session.idle`.
6. **+398.** The agent finishes one step of the retry and Kilo starts the
   first queued prompt. That step's `session.idle` is taken as the end of the
   runner's third turn. The runner harvests (178 s of pytest on an unfinished
   tree) and gets `REWORK no_progress_row, commits_ne_1, no_test_file`.
   Meanwhile the agent keeps working on the next queued prompt.
7. From then on each wait reads the idle of a step queued earlier: turns of
   4 s, 13 s, 0 s, 0 s, 33 s (`state.json`). The runner sends `REWORK` into
   a busy session, and the model reads "your turn ended before anything was
   committed" again and again. It never gets a quiet turn to commit in.
   After attempt 2 the agent is `GAVE_UP` at +1031. Its worktree
   `../rounds/106-mimo-v2-5` still holds 4 edited files and a new test file,
   and none of it was scored.
8. The same thing happens again after the second `session.error` at +616.

How many `session.idle`s Kilo 7.6.2 sent within 2 s after a `session.error`
in round 106:

| agent | error | idles after | same session used again? |
|---|---|---|---|
| mimo-v2-5 | `UnknownError` (+369, +616) | 2, 2 | yes (retry): **broken** |
| sensenova-6-8-flash-lite-var1 | `MessageAbortedError` (our abort) | 2 | no, the agent ends |
| mistral-medium-3-5 | `APIError` 400 | 1 | no, `ERROR` |
| nex-n2-5-pro | `MessageAbortedError` (our abort) | 1 | no, the agent ends |
| sensenova-6-{7,8}-flash-lite-var2 | `ContextOverflowError` | 0 | new session (KC-54), safe |

The module docstring of `kilo_client.py` already records the same kind of
bug from the probe (`PROBE.md` §Facts 5, "idle after 0.0 s" with an
unchanged file). The KC-1 cursor fixed a wait that *rescanned* old events.
It does not cover an idle that arrives **after** the event that ended the
previous wait, which is this case.

## The fix in one sentence

Right before each prompt, the runner marks how many events the tap already
holds. The wait for that prompt skips every event recorded before the mark,
so an idle or error from an earlier turn can never end this turn.

Why a mark and not "wait for `busy` first": the test fake sends
`session.status` as `{"status": "busy"}` (a string), live Kilo sends
`{"status": {"type": "busy"}}`, and scripted fake turns may have no `busy`
at all. The mark is taken from the tap, so it does not depend on the
event shape. The `busy` check is added as a **diagnostic only** (§4).

## What must change — file by file

Line numbers are at `75432a2`. Find by the quoted anchor if they drifted.

### 1. `tools/contest/kilo_client.py` — `EventTap.mark()` and `EventTap.skip_to()`

**Where:** `class EventTap` (≈ line 499). Put the two methods right after
`def wait(...)` (≈ line 653).

```python
    def mark(self) -> int:
        """KC-63: how many events this tap holds right now.

        Taken right *before* a prompt is sent: every event at an index below
        it was recorded before the prompt existed, so it belongs to an
        earlier turn.
        """
        with self._cond:
            return len(self.events)

    def skip_to(self, mark: int) -> int:
        """KC-63: move the cursor forward to *mark*, never backward.

        Returns how many unread events were skipped, so the caller can
        record them. The skipped events stay in ``self.events`` and in
        ``events.jsonl``; they are only never handed to ``wait`` again.
        """
        with self._cond:
            target = max(self.cursor, min(int(mark), len(self.events)))
            skipped = target - self.cursor
            self.cursor = target
            return skipped
```

- Both take the lock (`self._cond` wraps `self.lock`), the same lock
  `_record` and `wait` take. So the cursor never moves under a `wait`
  running on another thread.
- `skip_to` never moves the cursor backward. A mark taken earlier than
  the cursor is a no-op, never a replay.

### 2. `tools/contest/kilo_client.py` — `KiloClient.wait_idle(..., since=None)`

**Where:** `def wait_idle(self, tap, session, timeout, *, …)` (≈ line 1086).

Add one keyword-only parameter, last, off by default:

```python
                  on_deadline: Callable[[float], float | None] | None = None,
                  since: int | None = None) -> IdleResult:
```

At the very top of the body, before `started = time.monotonic()`:

```python
        # KC-63: events recorded before the prompt this wait belongs to are
        # an earlier turn's. Kilo sends two session.idle after a
        # session.error (round 106, mimo-v2-5), and the next turn read them
        # as its own end.
        stale = tap.skip_to(since) if since is not None else 0
```

Add a field to `IdleResult` (the dataclass ≈ line 211, after `open_tool`),
with a default so every existing constructor call still works:

```python
    #: KC-63: unread events that the ``since`` mark skipped (0 without one)
    stale_skipped: int = 0
```

Pass `stale_skipped=stale` into **every** `IdleResult(...)` that
`wait_idle` returns: the `timeout` one (≈ line 1206), `error` (≈ 1244),
`closed` (≈ 1248) and `idle` (≈ 1255). `grep -n "return IdleResult(" tools/contest/kilo_client.py`
lists them all. Leave the frozen dataclass frozen and do not reorder its
fields.

### 3. `tools/contest/backend.py` — the backend carries the mark

**a. The protocol.** In `class ContestBackend(Protocol)` (≈ line 128), next to
`def prompt(...)` (≈ line 159):

```python
    def mark(self) -> int | None:
        """KC-63: a position in this backend's event stream, taken before a
        prompt; ``None`` when the backend has no shared stream."""
        ...
```

Add `since: int | None = None` to the protocol's `wait_idle` signature
(≈ line 186), last and keyword-only, with one line in its docstring.

**b. `KiloBackend`** (≈ line 242; `def prompt` ≈ line 284):

```python
    def mark(self) -> int | None:
        return self._tap.mark()
```

In its `wait_idle` (≈ line 303) add `since: int | None = None` and pass it
the same way `on_deadline` is passed, only when it is set:

```python
        if since is not None:
            client_kwargs["since"] = since
```

**c. `OpenRouterBackend`** (≈ line 406; `def prompt` ≈ line 474): `mark()` returns
`None`. Its `wait_idle` (≈ line 496) accepts `since=None` and ignores it.
Each session there reads its own subprocess stdout, so no other turn's
events can reach it.

**d. Test doubles.** `tests/test_contest_backend.py` ≈ line 238 and
`_RecordingBackend` in `tests/test_contest_runner.py` ≈ line 3265 take
`**kwargs` or a fixed signature. Give the fixed ones `since=None`. If a test
backend lacks `mark`, the runner must still work: see §4
(`getattr(backend, "mark", None)`).

### 4. `tools/contest/runner.py` — take the mark before every prompt

**a. Where the prompt is sent.** `run_agent`, anchor
`transition(AgentState.PROMPTED, note=note)` then `backend.prompt(session, text)`
(≈ line 1461–1463). Take the mark **immediately before** the POST, so no
event of the new turn can come before it:

```python
            transition(AgentState.PROMPTED, note=note)
            mark_fn = getattr(backend, "mark", None)
            since = mark_fn() if callable(mark_fn) else None
            try:
                backend.prompt(session, text)
```

This is the only `backend.prompt` in the per-agent loop: the first prompt,
`continue`, `retry`, `rework` and the KC-54 fresh session all go through
it. `grep -n "backend.prompt(" tools/contest/runner.py` must show one line.
If it shows more, give each one its own mark the same way.

**b. Pass it to the wait.** `_wait_turn` (≈ line 959) gets `since: int | None = None`
and adds it to `wait_kwargs` only when not `None`, like `on_deadline`
(≈ line 984). The call at ≈ line 1475 passes `since=since`.

**c. Record it.** Right after `turn["idle_status"] = idle.status`
(≈ line 1481):

```python
            stale = int(getattr(idle, "stale_skipped", 0) or 0)
            if stale:
                # KC-63: events of an earlier turn this wait did not read as its own
                turn["stale_events"] = stale
```

`turns.jsonl` then shows `stale_events: 2` on mimo's retry turn, and the
next reader does not need `events.jsonl` to see what happened.

**d. Diagnostic only: a turn that idled without going busy.** In
`kilo_client.wait_idle`, keep a flag `saw_busy`. Set it on a
`session.status` whose status is `"busy"` or `{"type": "busy"}` (accept
both shapes: the fake sends the string), or on a `session.turn.open`. If
the wait returns `idle` with `saw_busy` false, log one warning
`"%s: idle without busy after %.1fs"`. **Do not** change the result: this
is how the next variant of this bug gets noticed, not a second rule.

## `tests/_kilo_fake.py` — the fake must be able to send the leftover idles

Today an error turn emits `session.error` and returns
(anchor `session.error is *instead of* the assistant message and the idle`,
≈ line 567). Add one scenario key, `idles_after_error` (int, default 0).
After the `session.error` emit, and before the `return`:

```python
            # KC-63: Kilo 7.6.2 follows a session.error with session.idle
            # events for the same session (two for mimo-v2-5 in round 106)
            for _ in range(int(turn.get("idles_after_error") or 0)):
                self._emit({"type": "session.idle",
                            "properties": {"sessionID": session.id}})
            return
```

Document the key in the scenario comment block at the top of the file
(≈ line 36, next to `"error": {...}`).

## Tests — where they go and what they check

**`tests/test_contest_kilo_client.py`**, next to
`test_wait_idle_…cursor…` (≈ line 170, the probe's "idle after 0.0 s"
test). Use the `_probe(tmp_path, scenario)` helper (≈ line 124) and
`_reject` (≈ line 118):

1. `test_a_turn_does_not_end_on_the_idles_left_after_an_error`: scenario
   turn 1 `{"error": "UnknownError", "idles_after_error": 2}`, turn 2
   `{"events": ["busy", "file.edited", "idle"], "assistant": "done", "delay": 0.6}`.
   - Prompt 1, `wait_idle(...)` gives `status == "error"`.
   - Wait until both leftover idles are in the tap
     (`len(h.fake.events_of("session.idle")) == 2`).
   - `since = h.tap.mark()`, prompt 2, then `wait_idle(..., since=since)`.
     It returns `idle` with `elapsed >= 0.55` (turn 2's own idle is
     delayed 0.6 s) and `stale_skipped == 2`.
2. `test_without_since_the_leftover_idle_still_ends_the_wait`: the same
   scenario without `since` returns `idle` at once with
   `stale_skipped == 0`. This pins that the default path is unchanged and
   that test 1 really catches the bug.
3. `test_skip_to_never_moves_the_cursor_backward`: `tap.skip_to(0)` after
   reading events returns 0 and leaves `tap.cursor` as it was.
   `tap.skip_to(10**9)` stops at `len(tap.events)`.

**`tests/test_contest_runner.py`**, near the KC-45 retry tests
(`grep -n "RETRY_PROMPT" tests/test_contest_runner.py`):

4. `test_a_retry_after_an_error_waits_for_its_own_idle`: a fake Kilo round
   whose agent has turn 1 `{"error": "UnknownError: the model's provider interrupted the response before it finished", "idles_after_error": 2}`
   and turn 2 commits a correct entry and idles. Expect:
   - `turns.jsonl` has exactly `initial` (error) then `retry` (idle), and
     no `continue` between them;
   - the `retry` turn has `stale_events == 2`;
   - one harvest, and the agent is `READY`.

   Before the fix the same test shows a `continue` turn of < 0.5 s. Check
   this once by hand with the fix reverted, and say so in the commit
   message.
5. `test_mark_is_optional_on_a_backend`: a backend without `mark` (the
   existing `_RecordingBackend`) still runs a turn, and `since` is not
   passed.

Put new files, if any, into the tiers with
`python3 scripts/sync_test_tiers.py` and `git add` the symlink (KC-60).

## Out of scope

- Why `kenary` drops mimo's stream. KC-61 covers its daily quota.
- Kilo's prompt queue itself. We stop feeding it; we do not drain it.
- The OpenRouter subprocess backend: it has no shared stream.

## Acceptance

- [ ] Replaying mimo's round-106 sequence (error, 2 idles, then a real turn) gives one `retry` turn that ends on the turn's own idle. No `continue` is sent in between.
- [ ] That turn records `stale_events: 2` in `turns.jsonl` / `state.json`.
- [ ] `grep -n "backend.prompt(" tools/contest/runner.py`: every hit has its own mark taken right before it.
- [ ] Callers that pass no `since` (`variant.hello_probe`, existing tests) behave event for event as today.
- [ ] A turn that idles without a `busy` logs one warning, and its result does not change.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
