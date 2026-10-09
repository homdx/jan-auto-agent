# 210 — review leftovers: a cached base-check pass ignores the step list, no warning under three voters, the summary line of a step is stderr's, JSONC comments after a value, a NaN retry wait

**Status:** landed `fe8a69a` — a cached base-check pass needs every current step, a quorum warning, JSONC comments, NaN wait is no time; 47 stays as 200 decided
**Origin:** operator review (`bugs-to-review/6.txt`, "Findings that were not changed"), each re-probed on `arena` @ `fda510c`; the review left them unchanged because each needs a decision or is pinned by an existing test — the decisions are made below so the round does not have to ask
**Severity:** LOW (45, 46), INSIGNIFICANT (47, 48, 49)
**File:** tools/arena/basecheck.py
**Symbol:** _hit, _summary (+ scripts/claim_vote.py main, scripts/claim_vote.py _kilo_settings, tools/contest/kilo_client.py _retry_past_the_bound)
**Round:** 210
**Size:** S
**Also touches:** tests/test_arena_base_check.py (the one existing test that pins 45), tests_bugfix/

## Why

The operator review left five findings alone and said why. Four of them are plain defects with a cheap, safe fix once the open question is answered; the answers are written here, so this ticket is a round like any other.

## Bugs (each probed)

**45 — `arena base check` takes a cached pass for the current step list** (`basecheck.py:217`, `_hit`; called at `:351`).
`_hit` looks the sha up and accepts an entry with `ok: true` whatever steps it holds; it never compares them with `STEPS` (`basecheck.py:47`). A pass cached when there were three steps is a "hit" after a fourth is added, and the table prints three rows and a green verdict for a check that never ran the fourth. The existing test `test_arena_base_check` pins today's behaviour; it is the test that has to change. **Decision:** a hit needs every step name of the current `STEPS` present in the stored rows (and nothing else is required); otherwise the check runs for real and overwrites the entry. The cache keeps its format.

**46 — `claim_vote` with fewer than three voters can never accept and says nothing** (`claim_vote.py:290–298`, `MIN_COMMITTED = 3`).
That follows the documented quorum, but `main` does not tell the operator: a run with `--profiles a b` produces a table of `UNSURE` for every claim with no word why. **Decision:** one line on stderr before the run when `len(voters) < MIN_COMMITTED` — "N voters, quorum is 3: no claim can be accepted" — and the run goes on; the exit code is unchanged.

**47 — a step's summary line is stderr's last line** (`basecheck.py:251`, `_summary`).
It reads `f"{stdout}\n{stderr}"` and takes the last non-empty line, so a pytest step that also wrote a warning to stderr shows that warning, not pytest's final line (`7262 passed, 52 skipped …`). **Decision:** the summary is the last non-empty line of stdout when stdout has any, else of stderr; for a failed pytest step the `FAILED …` lines are collected from both as today.

**48 — `_kilo_settings` strips only whole-line `//` comments** (`claim_vote.py:106`: `re.sub(r"^\s*//.*$", "", …)`).
`kilo.jsonc` is JSONC: a comment after a value (`"baseURL": "https://x", // note`) or a `/* … */` block makes `json.loads` raise, and `--add-profiles` and `--models` die with a traceback. **Decision:** a small JSONC reader that removes `//` and `/* */` comments **outside strings** (a `//` inside `"https://…"` must survive); on a parse error one refusal line, not a traceback.

**49 — a `NaN` retry wait is read as a long delay** (`kilo_client.py:139`, `_retry_past_the_bound`).
`nan <= bound` is `False`, so a `NaN` wait falls through to the quota/backoff tests: probed — `_retry_past_the_bound(float("nan"), 3, False, 60.0)` → `"backoff"`, and with `attempt <= 1` → `"quota"`. Kilo's own JSON cannot hold `NaN`, so this is reachable only through a hand-made state; the fix is one line. **Decision:** a `NaN` `wait` is treated as `None` ("Kilo gave no numeric time"); `inf` is a real, very long delay and stays what it is.

## Tests

One file per bug in `tests_bugfix/` (`test_<area>_<what>_210.py`), each failing on the old code:
45 — a stored pass with a missing step is not a hit; a complete one still is (the old pinned test is changed to say so and the commit names it); 46 — `--profiles a b` prints the quorum line once and still runs; three voters print nothing; 47 — stdout with a pytest summary and stderr with a warning: the summary is pytest's line; stdout empty: stderr's; 48 — a trailing `//` comment, a block comment, and a `//` inside a URL string all parse; garbage is one refusal line; 49 — a `nan` wait gives `None`; `inf` still answers `"quota"`/`"backoff"` as before.

## Review material

Review report `bugs-to-review/6.txt`, "Findings that were not changed" (no test file: each is a decision this ticket records).
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "210" -q
python3 -m pytest tests -k "base_check or claim_check or kilo_client" -q
python3 -m pytest .smoke_tests/ -q
```
