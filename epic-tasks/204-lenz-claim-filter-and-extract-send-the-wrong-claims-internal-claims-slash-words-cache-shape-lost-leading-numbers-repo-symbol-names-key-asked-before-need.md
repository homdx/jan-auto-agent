# 204 — `lenz_claim_filter.py` and `claim_extract.py` send the wrong claims and ask for a key first: internal claims, slash words, a cache that is not an object, lost leading numbers, repo symbol names, `LENZ_API_KEY` before need

**Status:** landed — round 204, winner Opus5 (as-is: code, tests, RUNBOOK), black-box bench 36/36; the Sonet5, Luna6 variants did not land
**Origin:** operator review (`bugs-to-review/`, bugs 19, 20, 21, 22, 218, 219 and the cached-run scripts), each re-probed on `arena` @ `00355fd`
**Severity:** LOW (23–28; 23 and 24 cost a paid Lenz credit or a wrong `CODE-CHECK`, 25 is a traceback, 26 and 27 feed the voters a sentence nobody wrote)
**File:** scripts/lenz_claim_filter.py
**Symbol:** pick_extracted, is_internal, Cache, split_sentences, _repo_symbols (+ scripts/claim_extract.py main, lenz_claim_filter main)
**Round:** 204
**Size:** M
**Also touches:** scripts/claim_extract.py, docs/claim-check/RUNBOOK.md, tests_bugfix/

## Why

The filter's promise is "send Lenz only what is about the outside world, never what is about this repository", and `claim_vote` relies on `is_internal` to turn an own-code claim into `CODE-CHECK` instead of accepting votes on it. Six readers in the two scripts break that or send/vote on a sentence the report never held. Probed on `arena` @ `00355fd`.

## Bugs (each probed)

**23 — `pick_extracted` keeps claims about this repository** (`lenz_claim_filter.py:208`).
The module docstring says it drops everything about the repo; the function the CLI calls (`pick_extracted`) drops only ticket ids and claims with no hint word. Probed: `pick_extracted(["The function tools/contest/gates.py judge_worktree returns a scorecard when pytest passes.", "Python raises ValueError when int() gets bad input via pytest."], 5)` → both kept. The first is sent to paid `/assess` and gets a meaningless verdict. Fix: `pick_extracted(claims, limit, symbols)` applies `is_internal(c, symbols)` exactly as `pick_claims` does; the CLI passes `_repo_symbols(root)`.

**24 — `is_internal` reads ordinary slash words as file paths** (`is_internal`, the `word/word` path pattern).
Probed: `is_internal("Use read/write access.", set())`, `"Either and/or works."`, `"It runs over TCP/IP."` → all `True`, the same as `"See tools/contest/cli.py."`. A public claim containing one is never sent to Lenz, and in `claim_vote` is forced to `CODE-CHECK` with `unanimous` cleared. Fix: a short list of slash words (`and/or`, `read/write`, `TCP/IP`, `input/output`, `client/server`, `yes/no`, `true/false`, `either/or`, `w/o`…) is excluded before the path test, or the path pattern requires an extension or a known top-level directory of the repo. A real path (`tools/contest/cli.py`, `scripts/x.sh`, `tests/`) stays internal; a test pins both lists.

**25 — a cache file that is valid JSON but not an object crashes** (`Cache`).
`json.loads` of `[]` or `null` succeeds, no error is caught, and the next `get` raises `AttributeError: 'list' object has no attribute 'get'` (`'NoneType'` for `null`). Probed with both. Fix: anything that is not a `dict` is an empty cache (and is rewritten as an object on the next `put`).

**26 — `split_sentences` eats the first words of a sentence** (`split_sentences`; the marker pattern `^[-*\d.\s]+`).
It strips any leading run of `-`, `*`, digits, dots and spaces — not a list marker but anything that *looks* like one. Probed: `"404 is returned when a page is missing."` → `is returned when a page is missing.`; `"3.5 seconds is the default timeout."` → `seconds is the default timeout.`; likewise `1.2.3 is the version…`, `2 ** 10 equals 1024`, `-1 is returned…`. The models then vote on, and Lenz is paid for and caches under the key of, a sentence nobody wrote. Fix: strip only a real list marker — `-`, `*`, `+`, `1.`, `12)`, `1.2.` **followed by a space** — once.

**27 — `_repo_symbols` does not know the code of a file whose name has a space or a non-ASCII letter** (`lenz_claim_filter.py:56`).
It runs `git ls-files "*.py"` and cuts the output with `.split()`. Probed on a repo holding `my module.py` and `ф.py`: the names come back `['"\\321\\204', 'module']` — `my module.py` is two "files", and git quotes the Cyrillic name. Neither file is read, so a claim naming `foo_bar` or `cyr_fn` is not internal, and `claim_vote` ("never accept votes on a claim about our own code") is handed one to accept. Fix: `git ls-files -z "*.py"`, split on NUL, raw bytes decoded `utf-8`/`surrogateescape`.

**28 — the scripts ask for `LENZ_API_KEY` before they know anything must be sent** (`claim_extract.py main`, `lenz_claim_filter.py main`).
The runbook says "an answer is cached by section text: a second run sends nothing". Both scripts read the key first and exit (`SystemExit: LENZ_API_KEY is not set and there is no console to ask on`) even when every section is already cached or nothing is left to send; and `lenz_claim_filter.py --json` writes no file when the list to send is empty, so the next stage reads a stale or missing file. Fix: decide what is to be sent first; ask for the key only for a non-empty send; write the output file, `[]` if need be, in every case.

## Tests

One file per bug in `tests_bugfix/` (`test_lenz_<what>_204.py`), each failing on the old code:
23 — the two claims above, only the second is kept (also a claim naming a symbol of a temp repo); 24 — the three slash sentences are public and `tools/contest/cli.py`, `tests/`, `scripts/x.sh` stay internal; 25 — `[]` and `null` cache files; 26 — the six sentences above round-trip unchanged and `- a`, `* a`, `+ a`, `1. a`, `12) a`, `1.2. a` lose only their marker; 27 — a temp git repo with `my module.py` and `ф.py`; 28 — a fully cached section needs no key and sends nothing (assert no network call); `--json` with nothing to send writes `[]`.

## Review material

Review tests (held by the operator, outside the repo): `test_lenz_split_sentences_numbers_218`, `test_lenz_repo_symbols_names_219`, and the filter/extract cases of `test_claim_check_scripts`.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "204" -q
python3 -m pytest .smoke_tests/ -q
python3 -m pytest tests/test_claim_check.py -q
```
