# KC-72 — an edit of the agent's own file is geometry's, and a gate reply cut by `max_tokens` keeps its verdict

**Status:** landed — by hand, replayed on round 46's own asks offline and against the live gate, 2026-09-27; found 2026-09-27 watching round 46 (KC-7)
**Severity:** HIGH
**Round:** 118
**Size:** S
**File:** `tools/contest/policy.py`
**Symbol:** `_extract_paths`, `Policy._mechanical`, `_extract_verdict`, `Policy._ask_gate`
**Also touches:** `tools/contest/roster.py` (`DEFAULTS_GATE.max_tokens`), `contest.ini` (`[contest_gate_llm] max_tokens`), `tests/test_contest_policy_file_asks.py` (new), `tests/test_contest_roster.py`
**Depends on:** KC-3 (landed: the two layers), KC-51 (landed: a relative token joins the worktree), KC-55 (landed: the gate's retries).

Ground rules, as in every ticket: `CollectBridge._shrink` stays byte-identical, and nothing runs against a live provider config.

## Problem

Round 46 hands every agent KC-7, which names `AGENTS.md`. Kilo asks before an
edit of `AGENTS.md` (it marks the file `configProtected`), and asks like this:

```
permission: edit
patterns:   ["AGENTS.md"]
metadata:   {filepath: "/…/rounds/46-<agent>/AGENTS.md", configProtected: true, …}
```

1. **Geometry saw no path.** `_extract_paths` keeps a pattern only when
   `_pathlike` says so — `/…`, `~…`, `./…`, `../…` — the KC-13 rule that stops
   a bare command word (`reboot`) from reading as a file in the worktree. A
   file ask's pattern is a file name, relative to the session's directory, so
   `AGENTS.md` was dropped; `metadata.filepath` was never read. No path, no
   "inside the worktree", and the edit went to the gate. The OpenRouter
   backend's `read`/`write` (`backend.py`, relative paths by design) had the
   same hole.
2. **The gate ran out of room.** `hy3:free` thinks before it answers, whatever
   `think = false` asks. With `[contest_gate_llm] max_tokens = 256` it came
   back `finish_reason: length`, `content: ""` — and the KC-55 retry sent the
   same request with the same budget, which failed the same way. Once it
   came back `{"verdict": "allow", "reason": "AGENTS.md is inside the agent's
   worktree and is` — a whole verdict — and `_extract_verdict`, finding no
   closed object, called it no verdict.

Round 46: 16 gate asks, 13 `gate-failed` (`empty reply` 11, a cut `allow` 1,
`{"verdict": "` 1); 12 of the 16 were edits of `AGENTS.md` inside the agent's
own worktree, and 9 of those were refused. `sensenova-6-8-flash-lite-var1` gave up on the `AGENTS.md` line after
its refusal and shipped without it.

## What landed

- `_FILE_PERMISSIONS` (`edit`, `read`, `write`): for these asks, with a
  worktree to join to, a pattern is a file name even without a `./`, and
  `metadata.filepath` is read too. Every other ask keeps `_pathlike`'s rule;
  without a *base* nothing changes.
- `_kilo_config`: Kilo's own config in the worktree — `kilo.json[c]`,
  `opencode.json[c]`, anything under `.kilo/` or `.kilocode/` — is never
  settled by geometry, since a project `kilo.json` can change what Kilo lets
  the agent do. It goes to the gate, as before. `AGENTS.md` is this repo's
  document and is not on that list.
- `_truncated_verdict`: an object that never closed still yields a verdict
  the model finished writing, as the first key of the first object; the
  reason is marked `… (cut)`. `{"verdict": "` alone is still no verdict.
- `_roomier` / `GATE_RETRY_MIN_TOKENS` 2048: the one retry after a
  verdict-less reply asks with at least twice the profile's budget and at
  least 2048 tokens (`max_tokens`, or Ollama's `options.num_predict`).
- `DEFAULTS_GATE.max_tokens` and the committed `[contest_gate_llm] max_tokens`:
  256 → 2048. A gate pays for the tokens it writes, not for the cap.

## Checked on round 46

- Offline, every one of the round's 245 `permission.asked` through
  `Policy._mechanical`, before and after: the 12 `edit AGENTS.md` go from the
  gate to `once — inside worktree/tmp_roots`; the other 233 decide as they
  did; the 4 that reach outside the worktree (`/tmp/debug-*`, `/tmp/pytest-of-*`,
  a `cp` into `/tmp`) still go to the gate.
- Live, those 4 through `Policy.decide` against `hy3:free`: all four get a
  verdict, none `gate-failed` — `rm -rf /tmp/debug-*` refused, the `/tmp/kilo`
  writes allowed; at the old 256 the widened retry gets every one of them.
- The failed ask replayed at `max_tokens` 256 and 1024: `length` with an
  empty body twice, then `stop` with `{"verdict": "allow", …}` in ~450
  completion tokens.

## Acceptance

- [x] `tests/test_contest_policy_file_asks.py`: an edit of `AGENTS.md`,
      of a nested file and of a new file inside the worktree is `once`
      mechanical with the gate stubbed to fail; the relative pattern alone is
      enough; the OpenRouter `write` shape too; a pattern that climbs out, and
      a `filepath` outside the worktree, still go to the gate; a forbidden
      `filepath` is a mechanical `reject`; each Kilo config shape goes to the
      gate; a bare word of a `task`, `webfetch` or `bash` ask is still no
      path; a cut `allow` and a cut `reject` are verdicts; a cut with no whole
      verdict is asked again; the retry's budget is 2048; `_roomier` widens
      `num_predict` and never shrinks a larger budget.
- [x] Against the pre-fix `policy.py`, the ten behaviour tests fail.
