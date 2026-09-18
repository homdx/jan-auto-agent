# RUN-11 — A Ctrl-C during the collect Pass B loses every summary already paid for

**Status:** landed — `e84240b` (by hand, no contest; Pass B checkpoints every landed summary to `<dir>/collect_summarize_state.json`, resumed by the next `--collect --refresh` / `--auto` start; the entry line bills the true rebuild — `collector_version` change = full rebuild, `collect_refresh` carries `reason: "version" | "sha"`; tests in `tests/test_collect_summarizer.py`, `tests/test_collect_cli.py`, `tests/test_collect_bridge_stale_entry.py`).
**Severity:** MEDIUM  
**File:** `tools/collect/cli.py`  
**Symbol:** the two batch calls of `summarize_repo` — `build_context` ≈ 392 (the full build: first run, `--rebuild`, a `collector_version` mismatch) and `action_refresh` ≈ 991 (the incremental batch) — and `tools/auto/collect_bridge._refresh_on_entry` ≈ 267–340 (the stdout line and the `collect_refresh` trace event)  
**Round:** 39  
**Size:** S  
**Source:** live runs on `../testtext6` / `../testtext2`, 2026-09-17. The three clones carry `.collect/` artifacts built by `collector_version "1"`; the code is `"2"` since `74cd58f`. Every `--auto` start therefore went into `_refresh_on_entry` → `action_refresh` → *full build*: Pass A plus one Pass B LLM call per module, 543 modules (531 on testtext2), on the architect's own `[api_remote]` model — at the baseline's measured 41 s/module (`collect_refresh {"modules":"113","seconds":"4691.61"}`, `3d901f545cce`) that is six-plus hours before the architect's first call. Meanwhile stdout said `collect: artifact stale (git_sha c21f426, HEAD e5d46fc) — refreshing 1 module(s)` (`18`, `74` on the other clones): the count is the hash diff, the reason (version bump) is not on the line, and the trace carries the same number. Two processes were started on the same clone; a Ctrl-C on either — the obvious move once the 12:54 `HTTP 429 … waiting 60.0s` storm hit — would have thrown away every summary that process had already received, because nothing is written to `.collect/` until the whole batch, Pass C and every builder are done.  
**Depends on:** nothing. The resume machinery exists: `tools.collect.summarizer.summarize_repo(checkpoint_path=…)` persists each landed summary at once (`tools.backoff.save_state`, atomic rename), skips checkpointed modules on the next call, clears the file when the batch completes — `tests/test_collect_summarizer.py::test_summarize_repo_checkpoint_*` cover it. No production caller passes `checkpoint_path`.  
**Also touches:** `tools/collect/summarizer.py` (`summarize_repo`: `hashes=` and `on_resume=`), `tools/auto/collect_bridge.py` (`_refresh_on_entry`), `tests/test_collect_summarizer.py`, `tests/test_collect_cli.py`, `tests/test_collect_bridge_stale_entry.py`, `agents.ini` `[collect]` comment (one sentence naming the checkpoint file), `docs/collect-epics/` wherever RUN-6's entry refresh is described

---

## What happens today

Pass B is the expensive half of every collect build: one sequential LLM
call per module, `[collect] max_retries` (10) outer attempts each with
the `[collect] error_retries × error_retry_wait_sec` ladder inside. The
result of the batch is held in memory (`summarized` in `build_context`,
`summarized_by_path` in `action_refresh`) until Pass C, the registries,
the test map and the risk index have run, and only then does
`_write_artifact` touch `.collect/`. A `KeyboardInterrupt`, a `kill`, an
OOM or a lost terminal anywhere in those hours leaves `.collect/` exactly
as it was — same version-1 manifest, same stale sha — so the next
`--auto` start (or `--collect --refresh`) takes the same decision and
starts the same batch from module 1. The 300 summaries the provider
already returned, and billed, are gone.

`summarize_repo` was written with the opposite contract and has kept it
since EPIC E: given `checkpoint_path`, every landed summary is saved
immediately under `{"loop": "collect_summarize", "modules": {path:
{purpose, notes}}}`, a restart with the same path attaches the saved
summaries without calling the LLM, and the file is removed when the
batch ends. Both tests pass. The three callers — `build_context`,
`action_refresh`, `action_module` — pass `progress_fn` and `on_error`
and nothing else, so in production the checkpoint is never created.

The second gap is on the same path. `_refresh_on_entry` reads the
previous manifest to count `diff_files(...).changed` for its stdout line,
and `action_refresh` reads the same manifest to decide that a
`collector_version` mismatch means a full build — but the bridge never
looks at `collector_version`, so the line says `refreshing 1 module(s)`
while Pass B is about to run over 543, and `collect_refresh.modules` in
the trace records `1`. The operator's only signal that the long path was
taken is the `[k/543] summarized …` progress stream, which starts after
the first module lands — up to 27 minutes into a 429 storm.

## What must change

1. **The two batch calls checkpoint.** `build_context` gains
   `checkpoint_path: Optional[Path] = None` and passes it to
   `summarize_repo`; `_full_build` sets it, and `action_refresh`'s
   incremental batch uses the same path:
   `resolve_collect_dir(root, config) / "collect_summarize_state.json"`
   (the name the summarizer tests already use). `.collect/` is
   git-ignored and `manifest.is_dirty` excludes the dir by path, so the
   file never turns `dirty` on. `action_module` (one module, `--module`)
   passes no path: a one-module batch has nothing to resume, and
   `summarize_repo`'s `clear_state` at its end would otherwise wipe an
   interrupted full build's checkpoint.

2. **An entry is tied to the source it summarised.** `summarize_repo`
   gains `hashes: Optional[Dict[str, str]] = None`; with it, each saved
   entry carries `"sha": hashes[path]`, and on load an entry is kept only
   if `hashes is None or entry.get("sha") == hashes.get(path)` — a
   summary of a file that changed between the interrupted run and this
   one is re-summarised, not reused. Both batch callers already hold
   `current_hashes` / `hashes` and pass them.

3. **A resume is visible.** `summarize_repo` gains
   `on_resume: Optional[Callable[[int, int], None]]` — called once,
   before the loop, with `(kept, dropped)` when a checkpoint was read
   (`kept` entries attached, `dropped` rejected by the sha check); not
   called when there was no checkpoint. `cli.py` passes
   `_print_summarize_resume`: `[collect] Pass B resumes: 300 module(s)
   kept from the interrupted run, 2 re-summarised (source changed)` on
   stdout, `logger.info` the same. The `[k/N]` progress total already
   excludes checkpointed modules — `N` is what is still owed.

4. **Ctrl-C is the cheap way out, and the ticket says so.** Nothing to
   change in `_refresh_on_entry`'s `except Exception` — `KeyboardInterrupt`
   propagates and ends the run — but the acceptance below pins the
   round-trip: interrupt at module *k*, restart, the LLM is asked for
   modules *k..N* only, the artifact is written with all *N* summaries,
   the checkpoint is gone. The `agents.ini` `[collect]` comment gains one
   sentence: *"Pass B checkpoints every landed summary to `<dir>/collect_summarize_state.json`;
   a Ctrl-C mid-batch costs one module — the next `--collect --refresh`
   or `--auto` start resumes. Delete the file to re-summarise from
   scratch."*

5. **The entry line and the trace say what Pass B is about to do.**
   In `_refresh_on_entry`, after `previous = read_manifest(...)`: if
   `previous.collector_version != manifest_mod.COLLECTOR_VERSION` the
   count is `len(modules)` and the reason is the version bump —
   `collect: artifact stale (git_sha c21f426, HEAD e5d46fc; collector_version '1' → '2') — full rebuild, 543 module(s)`;
   otherwise the line is unchanged. The `collect_refresh` trace event
   gains `reason: "version" | "sha"` and `modules` becomes the number Pass
   B is actually asked for (all modules on a version bump, the diff
   otherwise). `scripts/trace_round_snapshot.py` needs no change — it reads
   `seconds` and `ok`.

## Acceptance

- [ ] `tests/test_collect_summarizer.py`: entries carry `sha` when
      `hashes` is given; on resume an entry whose `sha` differs from the
      current hash is dropped and its module re-summarised, a matching one
      is attached without an LLM call; `on_resume` is called once with
      `(kept, dropped)` and not at all when no checkpoint exists; the two
      existing checkpoint tests unchanged.
- [ ] `tests/test_collect_cli.py`: `action_collect` under a stub LLM that
      raises `KeyboardInterrupt` on the 3rd module → `.collect/collect_summarize_state.json`
      has 2 entries, no `artifact.json`; a second `action_collect` with a
      counting stub calls the LLM exactly N − 2 times, writes all N
      summaries, the checkpoint file is gone, stdout has one
      `Pass B resumes: 2 module(s) kept` line. Same round-trip through
      `action_refresh`'s incremental batch (a manifest whose hashes differ
      for 5 files, interrupt after 2).
- [ ] `action_module` creates no checkpoint, and a checkpoint left by an
      interrupted full build is byte-for-byte untouched after
      `action_module` runs.
- [ ] `tests/test_collect_bridge_stale_entry.py`: with a version-1 manifest
      and N modules the stdout line reads `collector_version '1' → '2') —
      full rebuild, N module(s)` and the trace has `modules == N`,
      `reason == "version"`; with the current version and 3 changed files
      it reads `refreshing 3 module(s)` and `reason == "sha"`;
      `KeyboardInterrupt` from `action_refresh` propagates out of
      `_refresh_on_entry` (no `pack OFF` line, no `collect_refresh` event).
- [ ] `agents.ini` `[collect]` names the checkpoint file.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- The Pass B retry ladder (`[collect] max_retries`, `error_retries`,
  `error_retry_wait_sec`, `max_retry_after_sec`) — unchanged; RUN-10 owns
  the auto-mode budgets. A checkpoint makes the operator's Ctrl-C cost one
  module, which is the tool against a 429 storm this ticket adds.
- Resuming inside a module — the in-flight call is lost on interrupt.
- A lock on `.collect/`: two processes refreshing the same tree race on
  the artifact today and on the checkpoint after this ticket; the last
  writer wins either way. Running two on one clone is an operator error,
  not this ticket's guard.
- A `[0/N]` line before the first module lands — the entry line (item 5)
  carries N; the CLI `--collect` path prints the full-build reason already.
- Pass C / registries / builders — cheap, in-memory, re-run on resume.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
