# 147 — a context overflow in any provider's words, a remembered window below Kilo's, and a watch inside the turn

**Status:** open
**Severity:** HIGH
**File:** tools/contest/runner.py
**Symbol:** _is_overflow, _is_full_refusal, _context_budget, run_agent._overflow_of, run_agent.context_watch
**Round:** 147
**Size:** M
**Also touches:** tools/contest/kilo_client.py, tools/contest/backend.py, tests/_kilo_fake.py, tools/contest/context_memory.py, tools/contest/cli.py, tools/contest/roster.py, contest.ini, tests/test_contest_overflow_wording.py, tests/test_contest_context_memory.py, tests_bugfix/test_contest_round145_glm_overflow.py

A hand fix (branch `ctx-overflow-fix`), written for review rather than as a round.

---

## The bug (round 145, glm-4.5-flash on zai)

The agent worked for 45 minutes in one turn and grew step by step: 94 109, 96 942, … 98 777 tokens. The next request was refused with `Prompt exceeds max length` (HTTP 400, no error name). The runner ended the agent `ERROR`.

Three faults combined:

1. **The overflow was not recognised.** `_OVERFLOW_RE` knew three spellings (`ContextOverflowError`, `maximum context length`, `context_length_exceeded`). zai's wording was none of them, so the error was a plain error:
   - nothing went into the KC-67 memory;
   - there was no compact and no continue;
   - the run was lost.
2. **Kilo's declared window always beat the memory.** zai declares 131 072 for the model. `_context_budget` and `_with_remembered_limits` used Kilo's number whenever there was one. A remembered refusal at 98 777 would therefore never have moved the compact earlier: the runner compacts at 80 % of 131 072 = 104 857, which is after the wall.
3. **Nothing resized the model inside the round.**
   - Kilo compacts inside a turn by the model's `limit`. The round's overlay hands it one only at spawn.
   - The runner's KC-69 gate sits at permission asks, and reads inside the worktree ask nothing.
   - Live, 7.6.2: a running server *does* take a new `limit` through `PATCH /config`, **when the request carries the session's own `directory`**:
     - `GET /provider` then says 130 777;
     - an 83 657-token session past `input` is compacted by Kilo itself, to a summary and a 2 885-token reply.
   - The same patch sent without the directory lands in the server's own cwd project (written there as `.kilo/kilo.jsonc`), and no session sees it. A first probe made that mistake and wrongly concluded "no resize on the fly".

A second live finding (this fix's own runs): glm read 16 ballast files in batches — 81 311 tokens OK, then about 42 000 more in one step. KC-73 makes a record whose `grew` is over 25 % of `last_ok` size nothing. So every fresh session read the same batch into the same wall: 5 overflows, then STALLED.

What must not happen, either way: round 144's `deepseek-v4-flash-free` was refused with `This prompt is longer than the free tier allows` — a plan's cap. It must stay `ERROR provider_quota`, write nothing into the memory, and never start a loop of compacts.

---

## The fix

### Recognising an overflow (`runner.py`)

- **`_SIZE_REFUSAL_RE`** matches the *shape* of a size refusal, not one provider's string. There is a thing that has a size (prompt, context, input, request, message, tokens) and a word that says it is past one (`exceeds`, `too long/large`, `longer than the max/limit`, `too many tokens`), in either order, within one clause.
- **`_NOT_SIZE_RE`** vetoes the generic words of money, plan, key and rate: `rate limit`, `quota`, `credit`, `balance`, `wallet`, `recharge`, `payment`, `free tier`, `unauthorized`, `forbidden`, `api key`. The round's `quota_patterns` veto too, so providers' own phrases stay in the ini (KC-61's rule). The committed `quota_patterns` gain `wallet balance | recharge`: round 145's gpt-5-4-mini ended a plain `session.error` on an empty wallet instead of `provider_quota`.
- **`_is_full_refusal`** covers a refusal with **no** size words at all, when all of these hold:
  - the status is 400, 413 or 422, or there is no status;
  - it is not retryable;
  - it is not money, plan, key or rate;
  - the last reply that went through is at least `context_full_refusal_percent` of the window.
- **`run_agent._overflow_of`** decides, in this order:
  1. the three old spellings are an overflow, exactly as before;
  2. money, plan, key or rate → not an overflow;
  3. **the session must have grown:** a last reply under `context_min_window` → not an overflow. This keeps a plan's cap out of the memory;
  4. size words, or a full refusal → an overflow;
  5. **no loop:** a second one in the same run whose last reply is under half of the first one's → not an overflow. The compact left far less than the wall and the provider still refused, so it is something else.

  Each reading is logged with the numbers.

### The window (`context_memory.py`, `runner.py`, `cli.py`)

- `size_of(record, min_window)`:
  - a `last_ok` under `context_min_window` sizes nothing;
  - a loose `last_ok` (KC-73) at or above it *does* size the model. It is a reply the provider took, so compacting at 80 % of it is early but never late;
  - named limits are untouched;
  - with `context_min_window = 0`, KC-73 is exactly as it was.
- `_context_budget`: a remembered size **smaller** than Kilo's wins (`source = "remembered"`).
- `_with_remembered_limits`: the same rule, so the next round's `KILO_CONFIG_CONTENT` hands Kilo the smaller window, and Kilo compacts in time inside every turn.

### The window handed to the running Kilo (`kilo_client.set_model_limit`, `backend.KiloBackend.set_model_limit`, `runner.py: run_agent._push_remembered_limit`)

- **When:** a remembered window smaller than Kilo's (or one Kilo was never told) is sent at once:
  - right after the overflow is written to the memory;
  - when a turn starts with such a window already remembered, e.g. a revived agent.
- **How:** `PATCH /config` for the agent's own workspace, carrying `context_memory.kilo_limit` — the same limit the next round's spawn overlay carries. Each size is sent once.
- **Keeping the file out of git:** Kilo keeps the patch as `.kilo/kilo.jsonc` in that workspace, so `.kilo/` is added to the worktree's own `info/exclude` first. It is local, never in the agent's diff or commit.
- **Other backends:** a backend without the method is skipped.
- **On failure:** a warning, and the watch below still stands.

### The watch inside a turn, the second line (`runner.py: run_agent.context_watch`)

- **When it is armed:** while a turn waits, and only when the window comes from the memory or the fallback rather than from Kilo. Kilo compacts by its own size, so it needs no help there.
- **What it does:** every `context_watch_sec` it reads the fill. At `compact_at_percent` it stops the turn the way KC-69's refused ask does: `context_full` is set, the session is aborted once, and the next prompt compacts first.
- **What it records:** `turn["context_watch_stop"]` is the fill it stopped at.

### Config (`contest.ini`, `roster.py`) — no new number lives in code or in the memory file

| key | committed | meaning |
|---|---|---|
| `context_min_window` | 32000 | the smallest window a `last_ok` may size a model by, and the least a session must hold for a size refusal to count; 0 = off |
| `context_full_refusal_percent` | 60 | a wordless refusal is an overflow at this fill; 0 = off |
| `context_watch_sec` | 10 | the watch's read interval; 0 = off (also the dataclass default, so a config built in code behaves as before) |

---

## Tests

### Offline: `tests/test_contest_overflow_wording.py` (new, 60 cases)

1. Size refusals in nine providers' words are overflows. Free tier, wallet, rate limit, quota, key, a bare `Bad Request` and a tool-schema error are not.
2. A wordless refusal at or past the share is an overflow. It is not one when:
   - the session is small;
   - there is no size, or no reply yet;
   - the status is 429, 401 or 503;
   - it is retryable;
   - it is `ProviderQuota` or `ProviderUnavailable`.
3. The run replays zai's refusal with and without words:
   - with no continue left it ends STALLED `context overflow`, and the memory holds `last_ok = 98 777`;
   - with continues left it compacts, continues and ends READY.
4. A plan's cap ends `ERROR`, with no memory and no compact. A refusal under `context_min_window` ends `ERROR` with nothing written; with the floor off, it goes through. A second refusal far below the first ends `ERROR` and is not remembered.
5. The window floor decides which `last_ok` sizes a model: live glm's loose 81 311 does; KC-73's 17 382 and 14 179 do not.
6. A remembered 98 777 beats Kilo's 131 072:
   - the next session compacts at 83 %;
   - the next round's Kilo overlay carries it;
   - a remembered size above Kilo's changes nothing.
7. The window handed to Kilo:
   - an overflow below Kilo's window is `PATCH`ed once, for agent-a's worktree, with `kilo_limit`'s numbers, and `.kilo/` is in its `info/exclude`;
   - a run that starts with such a memory sends it before the first wait;
   - nothing is sent when Kilo's window is the smaller, and nothing for a plan's cap.
8. The watch:
   - it stops a 90 000-token turn (91.1 % of 98 777), and the next prompt is compacted first;
   - it is not armed for Kilo's own size, or at 0.
9. The three keys are read from the ini and committed in `contest.ini`.

### Offline: the rest

- `tests_bugfix/test_contest_round145_glm_overflow.py` pins zai's wording against the free tier, and the smaller window.
- `tests/test_contest_context_memory.py` changes one assertion: `size_of(last_ok=999)` is 999 with the floor off and `None` with the floor on.

### Live — scratch scripts, not committed; real `kilo serve`, the real runner `run_agent` through the test harness

- **zai/glm-4.5-flash**, declared 131 072, a ticket that reads 16 × 8 000-token files:
  - the real `Prompt exceeds max length` is read as an overflow and remembered;
  - the batch-read case is what led to the floor rule above.
- **orcarouter/deepseek-v4-flash-free**:
  - it ends `ERROR provider_quota` after about 75 000 tokens of work;
  - no memory file is created and there are 0 compactions.
- **`PATCH /config` probe:**
  - with the session's directory, Kilo 7.6.2 resizes the model on the running server and compacts by it (above);
  - the user's `~/.config/kilo/kilo.jsonc` is untouched; the project file lands in the patched directory.

---

## Acceptance

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```
