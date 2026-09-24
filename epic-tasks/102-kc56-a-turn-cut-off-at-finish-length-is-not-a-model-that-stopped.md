# KC-56 — A turn that ends on `finish: length` was cut off, not finished; it is continued or re-sessioned, not harvested as "did nothing"

**Status:** queued — found live 2026-09-24 in round 74 (base `3017885`). Two `sensenova` agents ended their first turn idle on an assistant message with `finish: "length"` and no text. That means the reply was cut off by a token limit; the model did not decide it was done. Both worktrees were clean, so KC-22 granted no continue. The runner sent both to `HARVESTING`, which ran the four pytest roots on an untouched base (905 s and 1593 s under `_TEST_RUNS_LOCK`) to reach the `REWORK` everyone could see coming. One of the two then went into its rework in the same full session, was cut off again, and ended `ERROR` on the next prompt.
**Severity:** HIGH (two of ten slots lost their first hour to a limit the runner can see in the message it already fetches. The empty harvests also held the global test lock for 42 min combined, and every other agent's harvest queued behind them)
**File:** `tools/contest/runner.py`
**Symbol:** `run_agent` (the `idle.status == "idle"` branch, ~line 740), `_cut_off` (new)
**Round:** 102
**Size:** S
**Source:** `contest-out/74/sensenova-6-7-flash-lite-var1/events.jsonl`, `contest-out/74/sensenova-6-8-flash-lite-var2/events.jsonl` (`message.updated`, `info.finish`, `info.tokens`), `contest-out/74/state.json` (`turns`).
**Depends on:** KC-22 (continue budget, `continue_message`), KC-54 (the fresh-session path for an overflow, queued), KC-39 (`max_sessions_per_attempt`, queued). `KiloClient.messages()` is already in the tree.
**Also touches:** `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (only if the fake needs a `finish` on its assistant message)

---

## What happens today

Every assistant message of round 74 whose `finish` was neither `tool-calls` nor `stop`:

| agent | time | finish | input | output | reasoning | cache read | what came next |
|---|---|---|---|---|---|---|---|
| `sensenova-6-7-flash-lite-var1` | 14:38:19 | `length` | 23 352 | 0 | **32 000** | 188 416 | idle, clean tree → HARVESTING 905 s → REWORK |
| `sensenova-6-8-flash-lite-var2` | 14:37:57 | `length` | 207 527 | 0 | 2 688 | 0 | idle, clean tree → HARVESTING 1593 s → REWORK |
| `sensenova-6-7-flash-lite-var1` | 15:24:08 | `length` | 244 410 | 0 | 17 734 | 0 | idle (rework 1), clean tree → HARVESTING 458 s → REWORK 2 → `ContextOverflowError` 4 s after the prompt → **ERROR** |
| `sensenova-6-8-flash-lite-var2` | 15:44:41 | `length` | 9 393 | 219 | 805 | 212 992 | Kilo compacted on its own (15:47:53), the turn went on |

The first line is the **output budget**. `reasoning` is exactly the 32 000
output tokens the request asked for at `variant = high`, and no text or tool
call came after it. The third line is the **context window**:
244 410 + 17 734 = 262 144, the model's `limit.context`. The second is the same
kind at a smaller window. The last line shows that Kilo sometimes recovers by
itself, and when it does the turn does not go idle, so the runner never sees it.

In all three cut-off cases the runner saw `session.idle`, read a clean tree,
and treated it as KC-22 says: "A clean tree (the model did nothing) is not a
continue". The model had not done nothing. It was mid-thought when the limit
cut it off.

## What must change

1. **`_cut_off(client, session) -> str | None`** (`runner.py`). After an
   `idle`, read the session's last assistant message
   (`KiloClient.messages`, fail-open: any error → `None`). It returns
   `"context"` when `info.finish == "length"` and `input + cache.read +
   reasoning + output` is at or above 95 % of the model's `limit.context`
   (from `GET /provider`, already read at intake; unknown limit → treat as
   `"output"`). It returns `"output"` for any other `finish == "length"`, and
   `None` otherwise.
2. **`"output"` → a continue, clean tree or not.** Same session, counted in
   `max_continues_per_attempt`, with a short message instead of
   `continue_message`: your last reply hit the output limit before any text or
   tool call; think less, and make the next step a tool call. `turns.jsonl`
   records `cut_off: "output"`.
3. **`"context"` → a fresh session** on the KC-54 path (abort, new session,
   `round_prompt(..., dirty=_dirty_tree(ws))`, `run.attempt` unchanged),
   capped by KC-39's `max_sessions_per_attempt`. A same-session continue into a
   full context is exactly the 4-second `ContextOverflowError` of the third
   row. `turns.jsonl` records `cut_off: "context"` and `new_session`.
4. **No harvest for a cut-off turn** while either budget lasts. Once both are
   spent it falls through to today's path, which KC-50 makes cheap.
5. **Rework into a full session** (the third row's second half) is the same
   check made before sending a rework: see KC-39 §7.

## Acceptance

- [ ] A fake session whose last assistant message is `finish: "length"`,
  `reasoning == output limit`, on a clean tree → a continue in the same session
  with the cut-off message, no `HARVESTING`, `cut_off: "output"` in
  `turns.jsonl`.
- [ ] The same with `input` at 96 % of `limit.context` → a new session with
  `round_prompt(dirty=…)`, `run.attempt` unchanged, `cut_off: "context"`.
- [ ] `finish: "stop"` on a clean tree → exactly today's path (HARVESTING).
- [ ] `messages()` raising → exactly today's path (fail-open).
- [ ] Budgets spent → today's path. Every existing runner test unmodified and green.

## Out of scope

- The variant. `high` asking for 32 000 output tokens is KC-49's resolution,
  and a model that thinks for all of them is still allowed to.
- Kilo's own compaction (row four). When it works, the turn never idles.
- KC-54's `ContextOverflowError` path. This ticket reuses it and does not change it.

## Self-check before `append_task.py`

- [ ] `python3 --version` is **3.10.12**, and `python3 -c "import tools.contest.runner"` runs clean.
- [ ] Exactly **one** commit above the base, holding only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py`,
  `tests/test_contest_runner.py` and `tests/_kilo_fake.py` if touched (plus
  `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the sha of the one commit.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
