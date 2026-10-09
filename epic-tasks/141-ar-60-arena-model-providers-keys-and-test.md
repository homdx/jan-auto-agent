# AR-60 — direct providers with a key, `arena model test`, results on record

**Status:** landed
**Severity:** LOW
**File:** tools/arena/models.py
**Symbol:** provider_key, test_models, model_scores
**Round:** 141
**Size:** M
**Depends on:** AR-59 (round 140)
**Also touches:** tools/arena/cli.py, tests/test_arena_model_test.py
**Replaces:** AR-39, AR-40 of the epic backlog

---

## Why

AR-59 lists only what Kilo already has. A provider not yet in `kilo.jsonc` must be
searched with its own key, and a model must be tested before it goes into a round.
Today `scripts/py_model_test.py` does both, but the key is typed on the command line
and the score is only printed. AR-60 keeps the key out of the command line and keeps
the score.

---

## The key

- `contest.local.ini`:

  ```ini
  [arena.provider.NAME]
  base_url = https://host/v1
  api_key  = ${ARENA_KEY_NAME}
  ```

  `api_key` must be a `${ENV}` reference; a literal key is a refusal naming the section.
- Env `ARENA_KEY_<NAME>` is used when the section has no `api_key`. `<NAME>` is the
  provider name upper-cased with every non-alphanumeric character as `_`
  (`my-prov` → `ARENA_KEY_MY_PROV`).

## Without any ini file

Everything works with **no ini at all**, by env and flags only:

```bash
export ARENA_KEY_MYPROV=…            # the key
export ARENA_URL_MYPROV=https://host/v1   # or: --url https://host/v1
arena model available myprov --free
arena model test myprov/m-a:free
```

- `base_url`: `--url URL` (only with exactly one provider), else env
  `ARENA_URL_<NAME>`, else `[arena.provider.NAME] base_url`.
- A key without a URL: the hint line
  `arena: no URL for 'X' — export ARENA_URL_X=… or pass --url`, and the provider
  goes through Kilo.
- Scores go to `.arena/model-scores.json` as with an ini; `model_cache_days`
  falls back to 7.
- No ini file is created by `available` or `test`.
- A provider with a key and `base_url` is listed and tested **directly**
  (`free_from_direct_api` and the direct mode of `py_model_test.py`, imported).
  A provider without one goes through Kilo (AR-59's path).
- Hints, one stderr line each, the command goes on:
  - no key for a provider that is not in Kilo:
    `arena: no key for 'X' — export ARENA_KEY_X=… or add [arena.provider.X] api_key = ${ARENA_KEY_X} to contest.local.ini`;
  - a model found directly but missing from Kilo:
    `arena: X/m is not in kilo.jsonc — a round cannot run it until it is added there`.
- The key and any URL holding it go through `mask` / `scrub`; never on stdout,
  stderr or `-o json`.

## Commands

- `arena model available PROVIDER… [--free]` — AR-59's command, now also for the
  direct providers.
- `arena model test NAME[,NAME…]` — the 15-check code task of `py_model_test.py`
  per model, through Kilo or directly, by the rule above. Names are checked like
  AR-59's `use` (refusal + hint).
- `arena model available … --test` — tests every listed row.
- Testing is never required: `use` works without a score.

## Results on record

- `.arena/model-scores.json`: a JSON list of
  `{provider, model, via: kilo|direct, score, max, error, at}`; `error` holds the
  reason when there is no score (401, timeout, no code).
- Same cache rules as AR-59 (KC-67 pattern): records older than `model_cache_days`
  dropped on read and write, atomic `os.replace`, a broken file is empty.
- The newest record per provider+model wins.
- AR-59's `LAST-TEST` column shows it: `14/15 2d`, or `401 2d` for an error.

---

## Tests (`tests/test_arena_model_test.py`)

The direct provider is a local HTTP server on port 0 in the test's own process
(no xdist group needed); Kilo is faked through AR-59's seam; the test run of a model
is faked through a module seam (`models.RUN_TEST`).

1. A section with `api_key = ${ENV}` and the env set → the request carries the key,
   it never appears in out / err / json.
2. A literal key in the ini → refusal naming the section.
3. No key for a provider not in Kilo → the hint line, exit 0.
4. A direct model missing from Kilo → the `kilo.jsonc` hint.
5. Two providers (one direct, one Kilo) in one `available` call.
6. `model test a,b` writes two records; `available` shows `LAST-TEST`.
7. A failing test writes `error` and the column shows it.
8. Records older than `model_cache_days` are gone; the write is atomic.
9. A typo in `model test` → refusal + hint, nothing run.
10. No ini at all: `ARENA_KEY_MY_PROV` + `ARENA_URL_MY_PROV` (provider `my-prov`)
    list and test the local server directly; `--url` does the same; a key with no URL
    gives the URL hint; no ini file appears.
11. `model test` runs at most 4 models at once (the shared Kilo store, as
    `py_model_test.py -j`), and `-o json` of `test` parses.

## Rules

- No real provider/model names or keys in committed files.
- Never read or print `agents_128k.ini`; write only `contest.local.ini` and `.arena/`.
- Every refusal is one line through `output.refuse`.
- Commit subject starts with the round number.

## Final check (operator)

After landing, a real run on two providers — one through Kilo, one direct with a key:
`available --free`, `test` on two models each, `use` with one tested and one untested.
