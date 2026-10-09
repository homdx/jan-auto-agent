# 212 — a repeated pytest run on an unchanged tree is answered from the round's own earlier run, in one line, instead of running for minutes

**Status:** landed — round 212
**Origin:** sandbox rounds 900 and 901 in qwen26 (corpus: `scripts/test_run_corpus.py`, 30 + 12 pytest runs of 13 free/regular models)
**Severity:** MEDIUM
**File:** tools/contest/testcache.py
**Symbol:** TestRunCache, parse_pytest, tree_fingerprint
**Round:** 212
**Size:** M
**Also touches:** tools/contest/policy.py (`decide`), tools/contest/runner.py (the event tap), tools/contest/roster.py (config keys), contest.ini, tests/, tests_bugfix/

## Why

What the sandbox rounds showed, per number:

- A full suite takes 95–650 s on a loaded box; Kilo's bash tool kills any call that runs past its own 120 s default. The model then **repeats the identical command** — one agent ran the same command 10 times (6 repeats on an unchanged tree, 723 s of 1205 s wasted). Two models also wrote `--timeout=1200000`, taking milliseconds for seconds.
- Ten agents ran the same suite on the same base tree at the same moment (load average 40 on 8 cores). Every one of those runs gave the same answer.
- Models spell one run many ways: `.smoke_tests/ .regression_tests/ tests_bugfix`, `tests`, bare `pytest -n auto …`, separate calls joined by `&&`, wrapped in `time`, `/usr/bin/time -v`, `{ time …; }`, `T0=$(date +%s); … echo "wall=…"`, `cd <worktree> &&`. Nearly all end in `| tail -N`.
- Every one of these commands raises a `bash` permission ask (probed in `decisions.jsonl`), and a `reject` carries a `message` the model reads as the tool's error (`KiloClient.reply_permission`). So a policy can answer a repeat **without running it**: reject with the earlier result in one line.

## What to build

**`tools/contest/testcache.py`** (new, no import of the runner or the policy):

- `parse_pytest(command: str) -> list[PytestRun]` — every pytest invocation found in a shell command line (`python3 -m pytest`, `pytest`, `py.test`), through `&&`, `;`, `|`, `( … )`, `{ …; }`, `time`, `/usr/bin/time -v`, `nice`, `timeout N`, `env X=Y`, `cd DIR &&`. A `PytestRun` has the normalised `roots` (a frozenset; `.smoke_tests`, `.regression_tests` and `tests` all map to `tests`, trailing slashes dropped), `flags` (a frozenset of the flags that change **what runs or what is printed**: `-k`, `-m`, `-x`, `--maxfail`, `--lf`, `--ff`, `--collect-only`/`--collect-q`, `--durations`, `-v`, `-q`, `--tb`), and ignores the flags that change **only how it runs**: `-n`, `-p no:cacheprovider`, `--timeout`, `-W`, `--color`. A command it cannot read returns `[]` — never an exception — and then the doubt step below decides whether to ask the model; if not, the policy lets the run go as it does today.
- (the store half lives in `tools/contest/testcache_store.py`: `tree_fingerprint`, `TestRunCache`; the parsing half stays in `testcache.py`: `parse_pytest`, `summarise`, `progress_line`, `classify_with_llm`.)
- `tree_fingerprint(worktree) -> str | None` — a hash of HEAD plus the working tree (tracked diff and untracked non-ignored files' contents); `None` when git fails, and then nothing is cached or served.
- `TestRunCache(path)` — `record(run, fingerprint, summary, rc, wall_s, agent)` appends one JSON line; `lookup(run, fingerprint) -> Entry | None`. One file per round (`contest-out/NN/test-cache.jsonl`), appended under a lock so ten agents write it safely; a torn last line is skipped on read.
- `summarise(output: str, rc: int | None) -> Summary | None` — the pytest summary line (`7262 passed, 52 skipped in 94.31s`, `3 failed, 1 error …`), found anywhere in the output (the model's own `| tail -5` may have cut it away; a run with no summary line is **not** recorded). A run the tool killed (its output ends with `terminated command after exceeding timeout`) is not recorded either.
- `progress_line(entry, age_s) -> str` — the one line the model reads, e.g. `[##########] cached 100%  tests unchanged since 07:42 (run by agnes-2-5-flash, 94 s): 7262 passed, 52 skipped — not re-run`. Failures are served too, with the failing test names (at most 10): a repeat of a red run on an unchanged tree is red.

**Doubt and the fallback.** The mechanical parser answers first. When it cannot decide whether a command runs tests (a script name, a `make test`, a `bash run_tests.sh`, an alias, a shell function), `classify_with_llm(command, settings) -> PytestRun | None` asks the gate model one short question — "does this command run the pytest suite, and with which roots and flags? answer JSON" — through the gate's own `LlmSettings`, with a hard 5 s budget and no retries. Its answer is only a **parse**: it never decides a hit by itself, the fingerprint and the stored run still have to match. When the model is overloaded, answers late, or answers something unreadable, the result is `None` and **the command runs directly**, exactly as without the cache. The answer is cached per command string in the round's file, so a script name is asked about once. `test_cache_llm = on|off` (default `on` when the cache is on).

**Policy (`policy.py`, `decide`)**: after the mechanical geometry and `deny_commands` checks and before the gate, for a `bash` ask whose command parses to exactly the pytest runs the cache holds on this worktree's fingerprint, answer `Decision("reject", "test-cache", progress_line(...))`. A command with any non-pytest part that has effects (anything but `cd`, `echo`, `time`, `date`, `tail`, `head`) is not answered from the cache. `decisions.jsonl` shows `layer = "test-cache"`.

**Recording (`runner.py`)**: the event tap sees every completed `bash` part; for one that parses to pytest runs and has a summary, record it under the fingerprint of the worktree **at the time the call started** (taken when the ask was answered `once`).

**Config (`roster.py`, `contest.ini`, `[contest]`)**: `test_cache = on|off` (default `off` until a round has judged it), `test_cache_share = round|agent` (default `round`: one agent's run serves another's on an identical fingerprint — all agents start from the same base).

## Decisions

- A hit is a **reject**: the model sees it as an error. Its text starts with `cached:` and says "not re-run; edit a file and run again to get a fresh run", so a model does not loop on it.
- A different fingerprint, a different parsed run, `test_cache = off`, or any parse doubt means the run goes through exactly as today.
- The LLM fallback can only add a parse, never a hit; any failure of it is "run directly". The cache is never on the critical path of a round.
- Concurrent pytest runs of different roots in one worktree (seen in round 900) are not handled here.

## Tests

`tests/test_contest_test_cache.py` — the parser over every line of `tests/data/sandbox_pytest_commands.txt` (31 real commands from the sandbox rounds; each parses to the roots and flags written beside it in the test); normalisation (`-n 4` ≡ `-n 1`; `tests` ≡ the two tier roots; `-x` ≠ no `-x`); summary extraction (passed/failed/error/skipped, summary cut off by `tail` → not recorded, killed run → not recorded); the cache file under ten concurrent writers and a torn last line; `tests_bugfix/test_policy_test_cache_212.py` — a policy with a recorded run on the fingerprint answers `reject`/`test-cache`; after a file change in the worktree it lets the run go; with `test_cache = off` it never answers; a `deny_commands` match still wins.

## Acceptance

```bash
python3 -m pytest tests/test_contest_test_cache.py tests_bugfix -k "212 or test_cache" -q
python3 -m pytest .smoke_tests/ -q
```
