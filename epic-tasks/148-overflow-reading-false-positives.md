# 148 — an overflow is read only from a real size refusal; the in-turn watch stands down once Kilo has the window; `.kilo/` outlives no turn

**Status:** open
**Severity:** HIGH
**File:** tools/contest/runner.py
**Symbol:** _SIZE_REFUSAL_RE, _NOT_SIZE_RE, _is_overflow, _is_full_refusal, _context_watch_sec, run_agent._overflow_of, run_agent._remember_overflow, run_agent.context_watch, run_agent._push_remembered_limit
**Round:** 148
**Size:** L
**Also touches:** tools/contest/backend.py (`_exclude_kilo_dir`, `KiloBackend.set_model_limit`), tools/contest/roster.py, tests/test_contest_overflow_wording.py, tests_bugfix/test_contest_round145_glm_overflow.py, epic-tasks/147-context-overflow-any-wording-and-remembered-window.md

**Depends on:** ticket 147 (branch `ctx-overflow-fix`; the base is the commit that adds this ticket). Ticket 149 (`context_memory.py`) is a separate round, after this one.

The source is three independent reviews of 147, checked against the code at c0c9c74. Every "True on HEAD" below comes from calling the function on that commit.

**Already fixed in c0c9c74, do not redo:**
- "the push depends on the watch": `_push_remembered_limit` runs at PROMPTED, independent of `context_watch_sec`;
- the `PATCH /config` directory: `_url()` adds it.

**Rejected review points (do not act on them):**
- `last_ok` being `None`: `_last_reply` always returns an int.
- "the watch is untested": the tests at `test_contest_overflow_wording.py` arm it, fire it and check 91.1.
- "a negative `context_min_window` gives 32000": through the ini, the roster clamps it to 0.
- "drop the repeat guard": it stays. Part A3 fixes which numbers it compares.
- "the overflow is silently dropped when `_last_reply` raises": `_last_reply` swallows errors and returns `(0, None)`, so that `except` is unreachable.
- "no 146/147 rows in `epic-tasks/INDEX.md`": that index never lists contest tickets.
- "open the PR with this commit only": the branch is reviewed as a whole; nothing to change.
- `balance`, `credit`, `payment` unanchored in `_NOT_SIZE_RE`: no real overflow text carries them; leave them.
- The old `_OVERFLOW_RE` spellings are checked before the veto, so `rate limit: maximum context length exceeded` is an overflow. This predates 147. No provider is known to send it, and moving the veto in front of KC-54's spellings risks KC-54's own cases. Leave it.

---

## Why it matters

In `run_agent`, `idle.status == "error"` calls `_overflow_of` **first**. Only an error that is not an overflow reaches:
- the quota check;
- `_is_provider_unavailable`;
- the local-store retry;
- `_retryable`;
- round 87's external-abort `resumable` path.

So every false overflow:
- skips the retry, quota or resume path it should have taken;
- writes a record into the shared memory (`_remember_overflow`) that sizes the model for every agent for `context_memory_days`;
- PATCHes a smaller window into the running Kilo;
- compacts or opens a fresh session.

That is worse than the original bug, which lost one agent's run.

## Part A — reading an overflow

### A1. The wording path matches errors that are not size refusals

`_is_overflow` is True on HEAD for every row below:

| message | status | what it really is |
|---|---|---|
| `Request too large for gpt-4o on tokens per min (TPM): Limit 30000, Requested 45000` | 429 | rate cap |
| `Request too large for model … tokens per minute (TPM)` | 413 | rate cap (Groq) |
| `Request entity too large` | 413 | HTTP body size |
| `Request body exceeds the maximum size of 32MB` | 413 | HTTP body size |
| `Request timed out: context deadline exceeded` | 504 | timeout |
| `context deadline exceeded` | 400 | Go gateway timeout |
| `input queue exceeded` | 529 | overload |
| `Request exceeded the time limit` | 400 | timeout |
| `max_tokens exceeds the model's maximum output tokens` | 400 | output cap: a compact cannot fix it, so it loops |
| `message exceeds max image size` | 400 | attachment |
| `input exceeds 20 images` | 400 | attachment |
| `exceeded your token budget` | 400 | plan |
| `Your error message is too large to display` | 400 | not a refusal |

The wording path ignores the status, `_retryable` and `_is_provider_unavailable`. Only `_is_full_refusal` looks at them.

**Fix:**

- **In `_overflow_of`,** before either new reading, return False when:
  - the status is present and is not 400 or 413;
  - `_retryable(error)`;
  - `_is_provider_unavailable(error)`;
  - `_is_external_abort(error)`.

  The three `_OVERFLOW_RE` spellings keep their verdict exactly as they are.
- **`_SIZE_REFUSAL_RE`:**
  - drop the weak heads `request`, `message` and `conversation`;
  - after `exceed`, require a size noun (`length|size|window|tokens?`).
- **`_NOT_SIZE_RE`** adds the generic classes, **as phrases, not bare words**:
  - `per\s+(?:min|minute|hour|day)`, `\bTPM\b`, `\bRPM\b`;
  - `timed?\s*out|deadline\s+exceeded|time\s+limit|overloaded|queue\s+exceeded`;
  - `entity\s+too\s+large|body\s+(?:exceeds|too)`;
  - `max_tokens|output\s+tokens`;
  - `\bimages?\b|attachment`;
  - `token\s+budget|budget\s+exceeded|allowance`.

  A bare `file`, `body` or `queue` is **not** added: a real overflow can say "the file you sent pushed the prompt past …".
- **`_NOT_SIZE_RE` drops `wallet` and `recharge` again.** They are one provider's phrases. They belong in `quota_patterns`, where the committed `contest.ini` already has them; the comment above the regex says so.
- **Every positive must still match:**
  - zai's `Prompt exceeds max length`;
  - every positive in `tests/test_contest_overflow_wording.py`.

### A2. A wordless refusal with no status, or a non-size 400, is an overflow at 60 % fill

At `last_ok = 80 000` of a 98 777 window, `_is_full_refusal` is True on HEAD for all of these:

- **`MessageAbortedError` the runner did not send.** This is Kilo shutting down on the operator's Ctrl-C (round 87). The overflow branch now takes it ahead of the `resumable` path, records it, and compacts against a dying server.
- **`read ECONNRESET` with `metadata.code = ECONNRESET`.** `_retryable` says True for it, but only `data.isRetryable` is checked.
- **`fetch failed`** (probed), and `socket hang up` (same path, not probed).
- **`ProviderAuthError: invalid token`.**
- **Malformed or refused requests:** `Unexpected end of JSON input` (400), `Invalid tool call arguments` (400), `blocked by content policy` (400), and a schema 422.

**Fix (`_is_full_refusal`):**

- **Status:** `data.statusCode` must be present and in (400, 413).
  - Drop "no status", and drop 422 (a schema error).
  - Rename `_CONTENT_REFUSAL_STATUSES` to `_SIZE_REFUSAL_STATUSES`, since 413 is literally a size status.
- **Retryable:** use `not _retryable(error)` instead of `data.isRetryable`.
- **Names:** return False for `_is_external_abort`, for any `name` containing `Auth`, and for a name that is not an API error (`APIError`, or a name ending in `APIError`).
- **An inferred reading never teaches the memory.** `_overflow_of` reports how it read the error. When the reading came from `_is_full_refusal`:
  - `_remember_overflow` writes nothing;
  - no PATCH follows from it;
  - the compact and the continue still happen;
  - it still counts for the repeat guard (A3).

### A3. The repeat guard and the floor look at the wrong numbers

- **The old spellings bypass the guard.** They return before `overflows_seen.append`, so the guard never counts them.
  - Repro: a `ContextOverflowError` at 98 777, then zai's wording at 40 000.
  - On HEAD the second is accepted and written, and the remembered window drops from 131 072 to 40 000.
  - Fix: record the size on the old path too. Only the bookkeeping changes; the old path's verdict stays True.
- **The floor and the repeat share compare `last_ok`.** The refused request was `last_ok + grew`, and `_last_reply` already returns `grew`.
  - A fresh session at about 20 000 (the round prompt) that reads a 60 000–80 000 batch in one step is glm's live pattern from 147. It is refused with `last_ok` under 32 000, ends ERROR, and nothing is remembered.
  - Fix: compare `last_ok + (grew or 0)` for the floor and for the repeat share. The record keeps `last_ok`.
  - Note that `grew` is estimated from characters (`SUMMARY_CHARS_PER_TOKEN`), so it is approximate.
- **The first request.** With `context_min_window = 0`, a refusal of the session's first request (`last_ok = 0`) counts as an overflow from its wording alone. That contradicts the docstring and round 144's deepseek case. Fix: require `last_ok > 0` whatever the floor.
- **Logging.** Log a floor rejection for a wordless refusal too, not only for a worded one.

### A4. Cleanup

- `_overflow_of` calls `_is_overflow`, which re-scans `_OVERFLOW_RE` and `_not_a_size`; both were already evaluated. Search `_SIZE_REFUSAL_RE` directly. `_is_overflow` keeps its meaning for its other callers.
- `FULL_REFUSAL_SHARE`: keep one source, `context_memory.DEFAULT_FULL_REFUSAL_PERCENT`. Tests read `context_memory.full_refusal_percent(config)`.

## Part B — the in-turn watch and the window handed to Kilo

### B1. After a successful PATCH the watch still arms and races Kilo

Inside a round, `spec.context_limit` stays Kilo's declared 131 072, so `_context_budget` keeps returning `"remembered"`. The watch therefore arms even after `_push_remembered_limit` handed the window to Kilo.

Both fire at the same fill:
- `kilo_limit` sets `input = 0.8 × size + min(20 000, output)`, and Kilo compacts at `input − reserved`, which is 80 % of the size (the `kilo_limit` docstring, live laguna).
- The watch aborts at `compact_at_percent` = 80 %, from a 10 s poll.

Whichever fires first wins. The watch's abort throws away the step in progress.

**Fix:**
- Arm the watch only when the window was **not** handed over: `pushed_limit[0]` is not the current remembered size, because the push failed, was skipped, or the backend has no `set_model_limit`.
- After a successful push, the console says that Kilo compacts by the pushed window.

### B2. The watch thread can act after its turn ended

The thread loops on `stop.wait(every)`, then reads the transcript (`_context_tokens`, an HTTP read), and never re-checks `stop`. The `finally` sets the event but does not join.

A read that returns after `_wait_turn` returned can still:
- set `context_full` and `context_aborted`;
- write `turn["context_watch_stop"]` into a turn already appended to `turns.jsonl`;
- abort the session.

`context_aborted` is cleared at the next turn's start, so the danger is narrower than "it leaks into the next turn". It is a late abort that lands on the next turn. The runner then reads it as its own stop and compacts for no reason.

**Fix:**
- re-check `stop.is_set()` after the read and before acting;
- take the actions under a lock that the `finally` also takes before setting `stop`;
- `join(timeout=every)` in the `finally`.

### B3. Wrong and stale words

- **`context_watch`'s docstring** still says "a running server never takes a new one (live, 7.6.2: PATCH /config is answered 200 and GET /provider still says 131 072)". That came from the first, wrong probe, sent without the directory.
  - Rewrite it: the watch is the fallback for a window that could not be handed to Kilo.
  - It lags one step and cannot catch a single large jump (glm: about 42 000 in one step).
- **`_exclude_kilo_dir`** (`backend.py`):
  - Its docstring and ticket 147 say "its own, for a worktree". `git rev-parse --git-path info/exclude` gives the *common* exclude for a linked worktree, shared by all worktrees of that repository. That is harmless for `.kilo/`, but the text is wrong.
  - It reads the file twice: read it once.
  - Remember per directory that it is done, so no `git` subprocess runs on every push.

### B4. `.kilo/kilo.jsonc` outlives the turn

Kilo keeps the patch in the agent's workspace as `.kilo/kilo.jsonc`.

- **Tracked file.** If the target repository already **tracks** `.kilo/kilo.jsonc`, `info/exclude` does nothing: the PATCH rewrites a tracked file and the change lands in the agent's diff.
  - Fix: before the push, run `git ls-files .kilo` in the workspace. If anything is tracked, skip the push with a warning; B1's rule then leaves the watch armed.
- **Stale file.** On `--resume` or `scripts/revive_round.py`, the respawned server finds the stale project file. **Not verified:** which of the project file and `KILO_CONFIG_CONTENT` wins in Kilo 7.6.2.
  - Fix, whatever the answer: delete an untracked `.kilo/kilo.jsonc` when the agent's run ends (harvest) and before a resumed agent's server is spawned.
  - Record the precedence in the ticket's notes if the round checks it live.
- **The agent editing it.** No change is needed. `policy._kilo_config` already sends every edit of `.kilo/` or `kilo.json[c]` in the worktree to the gate (`configProtected`). Add one test that an edit of `.kilo/kilo.jsonc` goes to the gate and is not auto-allowed.

### B5. Small items

- `_context_watch_sec`: use `math.isfinite`, as `full_refusal_percent` does. Today `inf` slips through for configs built in code.
- `context_watch_sec`: the dataclass default is 0.0, while `roster` and `contest.ini` give 10.
  - Keep both, and add a comment on the dataclass field saying the ini default is 10.
  - Add one runner test built from the shipped `contest.ini` values.
- `_push_remembered_limit` logs `limit` as a raw dict. Format it the way the plan line does.
- **Output reserve.** `kilo_limit` with no remembered output falls back to `DEFAULT_OUTPUT_RESERVE` (32 000), and the PATCH replaces Kilo's `limit.output` with it. When intake read the model's declared output, carry that instead. **Not verified:** whether this changes any live behaviour. For glm, Kilo's own compact sat at about declared − 32k, which suggests its declared output is 32 000 anyway.

## Part C — ticket 147's own text

- Status: `fixed on branch ctx-overflow-fix` (not landed on `arena`).
- Recount the "60 cases" after this round's tests.
- Fix the "its own, for a worktree" line (B3).

---

## Tests (`tests/test_contest_overflow_wording.py` and `tests_bugfix/test_contest_round145_glm_overflow.py`; offline, no live runs needed)

**Part A**

1. Every row of A1's table is not an overflow, through both `_is_overflow` and a run (`_overflow_of`). The run ends on its proper path: quota, retry or ERROR.
2. A 429 or 5xx with size words is not an overflow, and neither is a retryable error with size words.
3. The new `_NOT_SIZE_RE` phrases do not veto a real overflow that mentions a file near the size words, e.g. `prompt is too long: the file pushed it past 131072 tokens`. (An overflow text that mentions an *image* is vetoed on purpose — an attachment cap — and is pinned as not an overflow.)
4. Every payload of A2 gives `_is_full_refusal` False at 81 % fill. A runner-unsent `MessageAbortedError` at 81 % still ends `resumable`, round 87's path.
5. A wordless 400 at 70 % is compacted and continued, the memory file is unchanged, and no PATCH is sent. A worded 400 at 70 % is remembered.
6. An old spelling at 98 777, then zai's wording at 40 000: the second is not an overflow, and nothing at 40 000 is remembered.
7. A fresh session at `last_ok` 20 000 with `grew` 60 000, refused by zai, is an overflow, and its record holds `last_ok` 20 000. **The cost, stated:** under ticket 149's rule that loose record sizes nothing. The next fresh session can read the same batch into the same wall again, and only the sessions ceiling (`_sessions_ceiling`) ends it. Assert that it ends STALLED there and does not loop.
8. **A legitimate second overflow:** an overflow at 98 777, compact, then the compacted session at `last_ok` 40 000 reads a batch of `grew` 60 000 and is refused by zai — an overflow (40 000 + 60 000 ≥ 50 % of 98 777); on HEAD the guard rejects it.
9. Floor 0: a refusal of the first request (`last_ok` 0) is not an overflow.
10. The positives still pass:
   - every existing positive;
   - zai's exact text;
   - the round 145 bugfix pin;
   - deepseek's free tier → `ERROR provider_quota`, with no memory written;
   - gpt-5-4-mini's wallet text → quota through `quota_patterns`, with `wallet` no longer in `_NOT_SIZE_RE`.

**Part B**

11. With `context_watch_sec = 0`, a remembered window below Kilo's is still PATCHed at the first prompt (the existing test sets 5).
12. After a successful push, no watch is armed for that turn. A failed push, or a backend without `set_model_limit`, arms it.
13. A watch whose read returns after `stop` is set does nothing: no flags, no abort, no `context_watch_stop`. Use a fake backend whose `messages` blocks until the turn has returned.
14. A target repo that tracks `.kilo/kilo.jsonc`: no PATCH, a warning, the file unchanged, and the watch armed.
15. After READY the untracked `.kilo/kilo.jsonc` is gone, and a resumed agent starts without it.
16. An edit of `.kilo/kilo.jsonc` goes to the gate.
17. `context_watch_sec = inf` turns the watch off.
18. `_exclude_kilo_dir` on a directory that is not a git checkout writes nothing and raises nothing.

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
