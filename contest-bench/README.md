# contest-bench — black-box scoring of contest entries by test data, not by their tests

A round of the epic competition (`docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`)
produces N patches for one ticket. Their own tests prove only what each
author thought of. This bench feeds the **same input data** to every entry
and scores what came out — then the entries are ranked by the union of that
data, and the code itself is read only afterwards, when the ideal patch is
written from the best pieces.

Method (the one used for RUN-9, 20 submissions):

1. one git worktree per unique entry at the ticket base, patch applied;
2. write scenarios from the ticket's Acceptance list → run them on every tree;
3. read the code of the top group, write data that splits them, re-run;
4. repeat 3 once or twice; the final table is the union of all rounds.

Everything runs against a **fake provider**, never a live one.

## Layout

```
contest-bench/
  README.md                 this file
  harness/                  round-independent
    provider.py             fake OpenAI-SSE / Ollama-NDJSON provider at the urllib.request.urlopen level
    run_one.py              one (worktree, scenario) in a fresh interpreter → @@RESULT@@{json}
    run_all.py              worktrees × scenarios, sequential, merged into results.json
    static_checks.py        ground rules that need no execution (one commit, tests shipped, _shrink untouched, …)
    setup_worktrees.py      entrants.json → worktrees (format-patch / plain diff / patch inside a zip)
    validate_inputs.py      "did I forget a submission?" — inputs vs entrants.json vs worktrees vs results
    table.py                score table (+ --md) with per-entrant misses and what they got instead
    catalogue.py            SCENARIOS.md generator: what goes in, what is expected, what base does
  run9/                     one folder per round
    entrants.json           name → source file (+ base override, mail_index, duplicate_of)
    scenarios.py            the round's test data and checks
    SCENARIOS.md            generated catalogue (expected)
    results.json            raw results (every check, every entrant, with details)
    RESULTS.md              generated table + misses (got)
    REPORT.md               hand-written: findings, nominations, decisions for the ideal patch
```

The submissions themselves (`run9/*.patch`, `run9/sonets/*.zip`) are **not**
committed — `entrants.json` records their names and which ones were
byte-identical.

## Running a round

```bash
# 0. inputs: a folder with *.patch / *.diff / *.zip; write <round>/entrants.json (see setup_worktrees.py docstring)
# 1. worktrees (anywhere outside the repo tree; a `base` tree is added automatically)
python3 contest-bench/harness/setup_worktrees.py contest-bench/run9/entrants.json --wt /tmp/cb-wt
# 2. did every input land? (also checks changed-file sets against the patches)
python3 contest-bench/harness/validate_inputs.py contest-bench/run9/entrants.json --inputs run9 --wt /tmp/cb-wt
# 3. run everything (≈ 2 min for 19 trees × 45 scenarios); re-run a subset by naming entrants and/or scenarios
python3 contest-bench/harness/run_all.py --wt /tmp/cb-wt --scenarios contest-bench/run9/scenarios.py \
        --out contest-bench/run9/results.json --base-ref 777535b
python3 contest-bench/harness/run_all.py --wt /tmp/cb-wt --scenarios contest-bench/run9/scenarios.py \
        --out contest-bench/run9/results.json --base-ref 777535b sn68-v3 K8_404_during_transport_retry
# 4. table, catalogue, completeness
python3 contest-bench/harness/table.py contest-bench/run9/results.json --scenarios contest-bench/run9/scenarios.py
python3 contest-bench/harness/table.py contest-bench/run9/results.json --scenarios contest-bench/run9/scenarios.py --md > contest-bench/run9/RESULTS.md
python3 contest-bench/harness/catalogue.py contest-bench/run9/scenarios.py contest-bench/run9/results.json > contest-bench/run9/SCENARIOS.md
python3 contest-bench/harness/validate_inputs.py contest-bench/run9/entrants.json --inputs run9 --wt /tmp/cb-wt --results contest-bench/run9/results.json
# 5. clean up
git worktree list | grep cb-wt | awk '{print $1}' | xargs -n1 git worktree remove --force
```

`--base-ref` is the commit the tickets were handed out from; an entry rebased
onto a later commit (RUN-9: `sn68-v2` on `777535b`) is still measured on its
own commits only (`merge-base`).

`python` is not on PATH — use `python3`. One subprocess at a time; the bench
never runs entries in parallel.

## Writing scenarios

A scenario is a dict in `SCENARIOS`:

```python
"A1_transport_then_verdict": {
    "plans": _plan1(P.transport_empty(), P.transport_empty(), P.rejected()),  # per candidate, per call; last repeats
    "candidates": 1,                       # tools/c0.py … tools/c{n-1}.py, each a CandidateTask
    "config": {"presence_empty_retries": "0"},   # [gate1] overrides on top of GATE1_DEFAULTS
    "api_format": "ollama",                # optional; default openai
    "extra_sections": {"gate1_llm": {...}},# optional extra ini sections (profiles)
    "checks": a1_checks,                   # def checks(r) -> [ck(name, ok, detail), ...]
}
```

Reply specs (`provider.py`): `rejected()`, `confirmed_for()`, `transport_empty()`,
`exhausted_empty()`, `reasoning_only()`, `garbled()`, plus raw dicts
(`{"http": (400, body)}`, `{"exc": "timeout"}`, `content=`, `finish_reason=`,
`usage=` (int or `f(payload)`), `reasoning=`, `reasoning_key=`, `role_content=""`,
`delay=` (busy-wait seconds), `finish_inline`, `usage_inline`, `ollama`). A plan
entry may be a `callable(payload) -> spec` to answer differently by request
(e.g. a verdict only on the `think=off` call).

`checks(r)` sees: `r.calls(i)` (request payloads candidate *i* received, in
order — `max_tokens`, `temperature`, `messages`, `stream_options`, `reasoning`,
`thinking`, …), `r.outcome[title]` (`rejected` / `confirmed` / `unknown-kept` /
`unknown-dropped`), `r.counters` (the `counters=` dict `filter()` filled),
`r.filter` (the `Gate1Filter` — attribute fallback for counters), `r.logs` /
`r.log_lines(needle, level)`, `r.sleeps` (durations handed to `time.sleep`),
`r.tracer.events`, `r.split_line` (`format_gate1_split`). `mode="stream"`
scenarios skip the filter and drive `tools.llm_stream` directly via a `drive`
function.

Naming: check names start with a group prefix (`A1.`, `K8.` …); `GROUPS` in
`scenarios.py` maps prefixes to table columns, `BONUS_PREFIXES` marks checks
shown but not counted, `CONFIG_KEYS` lists ini keys `static_checks.py` must find
documented.

Two rules of thumb that paid off in RUN-9:

* **Stub below the API the entries were free to design.** RUN-9 let entries
  pick `request_completion_ex` or an `on_meta=` callback; stubbing `urlopen`
  made the same data fit both. Where a scenario must call the new API
  directly (`I1_stream_meta`), an adapter tries the known shapes and reports
  which one it found.
* **Base first.** Run the unpatched tree before any entry: checks it already
  passes are regression guards, checks it fails measure the fix. If base
  passes a check meant to measure the fix, the check is vacuous (E3 in RUN-9
  passed on base until it also required the first request to carry the field).

## What the bench does not do

* It does not run the entries' own test files, and it does not run the
  project suite — do that separately for the shortlist
  (`python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180`,
  sequentially, inside the entry's worktree).
* It does not read code. Part 2 (the ideal patch) does.
