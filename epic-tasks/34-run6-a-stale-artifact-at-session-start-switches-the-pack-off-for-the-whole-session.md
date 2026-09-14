# RUN-6 — A stale artifact at session start switches the pack off for the whole session

**Status:** landed — ideal patch from the round-34 competition (base: Sensenova 6-8 var2 — real mini-repo tests, stdout in both branches; 6-8 var1's provenance lookup via `resolve_collect_dir`; module-level helpers as in 6-7. Reviewer: the stdout count is the *changed* modules (Pass A + hash run once and handed to `action_refresh(modules=, hashes=)`, so it costs no extra scan), SHAs cut to 7 chars, `collect_refresh` counted by `scripts/trace_round_snapshot.py`, `staleness = refresh` interplay and "second bridge does not refresh again" pinned by tests)  
**Severity:** HIGH  
**File:** `tools/auto/collect_bridge.py`  
**Symbol:** `make_collect_bridge` (≈ 1168–1195, the `status == "stale"` branch) and `CollectBridge.usable` (≈ 240–275)  
**Round:** 34  
**Size:** S  
**Source:** the latest sessions on `../testtext` (run `9b478ce1fbce`) and `../testtext6` (run `a068c82b35a5`), 2026-09-14 12:50, first sessions on the M4-instrumented code: `collect.blocks = 0`, `collect_miss reason=stale` ×22 and ×23. The artifact's `git_sha` (`07e783c` / `d129767`) is 8+ agent commits behind the worktree HEAD — every session since 13.09 15:39 ran with the pack silently off. Across both trees the pack reached the coder in 9 of 55 executed tasks  
**Depends on:** —  
**Also touches:** `tools/collect/loader.py` (`load`, staleness policy ≈ 440–470), `agents.ini` (`[collect]` docs), `README.md` §collect, `tests/test_collect_bridge*.py`  

---

## What happens today

`--auto` builds the collect model once at start. If the artifact is older
than the tree (`git_sha` ≠ HEAD or the manifest is `dirty`), the loader
returns `status="stale"` and, under the default `staleness = warn`, the
bridge logs one WARNING and reports `usable = False` for every call of the
session. V9's `auto_refresh_between_tasks = true` repairs the paths *this
session's* commits touch — it never runs when the artifact was already
stale on entry, because there is no usable model to repair.

The operator turned on `use_in_auto = true` and
`auto_refresh_between_tasks = true`. Both say "I want the pack, keep it
fresh". The result was one warning line in `run.log` and 45 `collect_miss
stale` events that nobody reads during a run. A resumed run — the normal
way these trees are driven, 6–10 sessions per goal — is stale by
construction: the previous session's own commits moved HEAD.

`staleness = refresh` exists (`loader.py` ≈ 467 → `cli.action_refresh`,
incremental since V7) and would have fixed it. It is not the default, the
warning does not name it, and nothing in `--auto`'s startup output says
"the pack is off".

## What must change

1. **`auto_refresh_between_tasks = true` implies refresh-on-entry.** In
   `make_collect_bridge`, when the model is `stale` and the config has
   `auto_refresh_between_tasks = true`, run the same incremental refresh
   `staleness = refresh` would run (`cli.action_refresh`, V7: only changed
   modules go through Pass B), then reload. The rationale is the V9 one:
   the operator has already paid for per-path repairs; a whole-artifact
   repair on entry is the same operation over the set of paths that moved.
   `staleness = warn` without the V9 flag keeps today's behaviour.

2. **Fail open on refresh failure.** If the refresh raises (provider down,
   `--no-llm` mismatch), log one WARNING and fall back to today's stale
   path — never block a run on the pack.

3. **Say it on stdout.** `--auto` prints its startup banner; add one line
   in both cases: `collect: artifact stale (git_sha 07e783c, HEAD b0b152a)
   — refreshing 14 module(s)` or `… — pack OFF for this session (set
   [collect] staleness = refresh)`. A warning in `run.log` is not where an
   operator looks while a run is going.

4. **One M4 event.** Emit `collect_refresh` with `modules=<n>`,
   `seconds=<t>`, `ok=<bool>` so the live snapshot can show that the
   refresh ran and what it cost.

5. **Docs.** `agents.ini` `[collect]` comment for `auto_refresh_between_tasks`
   gains one sentence about entry; `README.md` §collect table row for
   `staleness` says which key makes `refresh` unnecessary.

## Acceptance

- [x] `tests/`: loader stub returns `stale`, config `auto_refresh_between_tasks = true`
      → `action_refresh` called once with the tree root, bridge `usable`
      after reload.
- [x] Same with `auto_refresh_between_tasks = false` → no refresh, `usable`
      False, warning text names `staleness = refresh` (today's path).
- [x] `action_refresh` raises → one WARNING, bridge falls back to stale,
      no exception reaches `--auto`.
- [x] `collect_refresh` event present in the trace with `modules` and `ok`.
- [x] Existing `tests_bugfix/test_collect_bridge_stale_after_task_commit.py`
      unchanged and green.
- [x] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- Changing the default of `staleness` itself.
- V8 (`--no-llm` preserving summaries) — related, still its own ticket.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
