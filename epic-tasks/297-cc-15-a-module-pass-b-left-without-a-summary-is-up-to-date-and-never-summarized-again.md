# CC-15 — a module Pass B left without a summary is "up to date" and is never summarized again

**Status:** landed (round 297: agnes-2-5-flash as the base, made to say what the ticket says; black box 297, 25 checks). Before: FINAL, open — found 2026-10-14 while building the collect model of the layer-2 target for CC-13 (295) on sensenova; reproduced with a fake model
(below). Cut it into `epic-tasks/` with the first word `open` when the round starts.
**Severity:** MEDIUM (a summary the provider failed to give once is gone for good: the judges' packs, the reader's contract notes and the docs read an
artifact that silently lacks it, and every check says the artifact is fine)
**File:** `tools/collect/cli.py`, `tools/collect/manifest.py`, `tools/collect/summarizer.py`
**Symbol:** `action_refresh` (the incremental batch), `_freshness` / `--check`, `build_manifest` / `Manifest` / `is_fresh`, the `[i/n] summarized` progress line of
`summarize_repo`
**Round:** 297
**Size:** S
**Depends on:** RUN-11 (39, the Pass B checkpoint, landed), CC-13 (295, landed)
**Also touches:** `tools/collect/loader.py` (`load_readonly`: the count it reports), `tests/test_collect_cli.py`, `tests/test_collect_summarizer.py`, a new
`tests/test_collect_unsummarized.py`

---

## What happens today

Pass B asks the model for each changed module's `purpose`/`notes`. When the provider fails for a module past `[collect] max_retries` and the
`error_retries` ladder (an HTTP 500, a 429 "rpm exhausted" that outlasts `max_retry_after_sec`, a reply that never parses), the summarizer gives up and the
module is written **without a summary** — correctly: no stale prose is passed off as current. But the manifest is written with that module's **new content
hash**, the same as for a module that was summarized. From then on the module is "up to date":

```
collect 0: full build: 2 module(s) re-summarized                         store.py  llm   "Defines AlphaStore, load_rows."
           (store.py changed: AlphaStore -> BetaStore; the model answers 500 for store.py)
collect 1: incrementally refreshed 1 changed module(s)                    store.py  -     (no summary)
           log: "giving up on pkg/store.py after 2 error(s)" … then "[1/1] summarized pkg/store.py"
           (the model is healthy again)
collect 2: already up to date — nothing to do                             store.py  -
--check  : collect check: up to date
--refresh: tree unchanged — recomputed derived artifacts only             store.py  -
```

Only `--rebuild` (every module again: about an hour of Pass B on this repository) or `--module pkg/store.py` brings it back, and nothing tells the operator
to run either. The progress line even says `summarized` for the module that got nothing, and the build's last line says `incrementally refreshed 1 changed
module(s)` without a word about the missing summary.

This is the common case on a free provider, not a corner: sensenova answered 79 HTTP 429 in one CC-10 run, and the CC-13 artifact for `4ff7d14` is being built
on it now. Every module that ran out of retries there stays unsummarized in the artifact the layer-2 `+collect` variants hand the judges.

Reproduction (no network): a fake OpenAI-compatible server whose `purpose` names the classes of the module it is shown, `[collect] llm_summaries = true`,
`error_retries = 0`, `max_retries = 1`:

1. `main.py --collect` on a two-module repository (`pkg/store.py` with `class AlphaStore`, `pkg/still.py`): both summarized.
2. Rename the class in `pkg/store.py`, commit, make the server answer 500 for `store.py`, `--collect`: `store.py` has no summary.
3. Let the server answer again, `--collect`: "already up to date", `store.py` still has none; `--check` "up to date"; `--refresh` "tree unchanged".

The same run with the server healthy shows that the rest of the incremental update is right: a renamed class, a deleted module and a moved file leave no
stale name in `artifact.json`, the rendered pages or the manifest, and only the changed modules are sent to the model.

## What it does

* **The manifest records which modules have no summary.** `Manifest.unsummarized` (a sorted list of paths; an older manifest without the key reads as `[]`):
  the modules of an `llm_summaries = true` build whose Pass B produced no summary — the summarizer gave up, or the reply never parsed. A `--no-llm` build
  records `[]`: no summary is expected there, and its carried summaries are already tagged `llm-stale` (V8).
* **Such a module is not up to date.** With `[collect] llm_summaries = true`, a manifest whose `unsummarized` is not empty is stale for `--collect`, `--check`
  and `load` / `load_readonly` (the collect line of Gate 1 then says `stale (N modules without a summary)`); `--collect` / `--refresh` sends exactly those
  modules to Pass B again, with the changed ones, and the tree's other modules are not re-summarized. A module that fails again stays listed and is tried at
  the next refresh; it never blocks the others.
* **The log says what happened.** The progress line of a module that got nothing says `no summary` (with the last error), not `summarized`; the build's last
  line counts them: `incrementally refreshed 1 changed module(s), 1 without a summary (tried again at the next --collect)`; `--check` says `stale (1 module
  without a summary)`.

## Tests (sketch)

`tests/test_collect_unsummarized.py`, a fake model in-process (a `ThreadingHTTPServer` on port 0, the reply shape of `request_completion`: a JSON body, and SSE
when the payload asks for a stream):

* the reproduction above, step for step: after the failing refresh `unsummarized == ["pkg/store.py"]`, `--check` is stale with the count, and the next
  `--collect` asks the model about `pkg/store.py` alone and records its summary; `unsummarized == []` after it;
* a module that fails twice is listed twice and does not stop `pkg/still.py` from being summarized when it changes;
* a `--no-llm` build records `[]` and is not stale for missing summaries; an older manifest without the key is read as `[]`;
* `load_readonly` reports the stale state and the count (CC-13's line);
* the progress line says `no summary` for the failed module.

## Acceptance (two layers)

**Layer 1 — offline, in the round.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged; a black box
(`contest-bench/297/blackbox_297.py`) running `main.py --collect` as a process against the fake server through the three steps.

**Layer 2 — the operator, live.** On the CC-13 artifact of `4ff7d14` built on sensenova: `python3 main.py --collect --check --base <target>` names the modules
without a summary (if any), one `--collect` fills them, and the count in the build's last line goes to 0.

## Contest

A mini contest, **Opus, Sonnet and Haiku by hand**, like CC-12 and CC-13: the change is small and the offline acceptance deterministic; the risk is the
freshness test (three callers: `_freshness`, `load`, `load_readonly`) and the manifest's backward compatibility.

## Not in scope

The summarizer's retry ladder and its settings, Pass C (the verifier), what a summary says, `--module`, the RUN-11 checkpoint.
