# KC-35 — A roster model that is missing from Kilo's provider list is named with the entry to add, and `--register-missing` registers it for the round

**Status:** landed `c0fff79` (2026-09-24) — round 74, winner `sensenova-6-8-flash-lite-var2` (its uncommitted tree, taken as is: it ran out of its hour re-running the full suite for a summary line `-qq` never printed, FL-7), plus two fixes by hand: with the flag, an id it would register is neither a refusal nor a hint when the round is refused on other grounds, and an overlay server that does not start is one `registration did not take: … did not start` line. Three tests added for those and for the variant being checked on the registered offer. `hy3` (READY `f2c7f10`) checked variants against the unregistered offer, so `--variant high` refused every registered id; `agnes-2-0-flash` (READY `afff242`) passed `env` as a string (TypeError at spawn) and dropped the registered agents from the round. Found 2026-09-21 running round 64 (KC-25's own contest): `agnes-3-0-flash:free` and `nex-n2-5-pro:free` both ended `ERROR` 10 s after the prompt, 2 of 8 slots lost. Third time the same failure is recorded (RUNBOOK round 1, round 52/58 in KC-25's Source, now round 64).  
**Severity:** MEDIUM  
**File:** `tools/contest/cli.py` (`intake`, `_start_server`, `_parser`, `_print_plan`), `tools/contest/kilo_client.py` (`KiloServer.spawn`)  
**Symbol:** `KiloServer.spawn(env=…)`, `roster_missing` (new), `registration_overlay` (new), `merge_config_content` (new), `--register-missing`  
**Round:** 74  
**Size:** S  
**Source:** live, 2026-09-21, this operator's box. The same ids answer over HTTP on the provider (`curl https://kenari.id/v1/chat/completions`, HTTP 200 for all four ids tried) and are refused by Kilo (`Model not found: kenary/<id>`) because they are not in the `kenary` provider's `models` in `~/.config/kilo/kilo.jsonc`. Reproduced with the real Kilo CLI 7.6.2 and 7.7.5 (below).  
**Depends on:** KC-25 (`KiloClient.providers()`, `roster_on_offer`, the throwaway-server intake read) — this ticket is built on top of it; KC-16 (`cmd_run`).  
**Also touches:** `tests/test_contest_cli.py`, `tests/test_contest_kilo_client.py`, `tests/_kilo_fake.py` (only if the stub binary needs a provider route)

---

## In one paragraph

A model can be perfectly healthy on the provider and still unusable in a round: Kilo only routes to ids listed under the provider in `kilo.jsonc`. For any other id `POST /session` succeeds and the **first turn** fails with `session.error … Model not found: kenary/<id>`. Operators read that as "404", but it is **not an HTTP status** — no request ever reaches kenari; it is Kilo refusing the id from its own list. Today the runner learns this ten seconds into the round (one dead slot per missing id), and nothing tells the operator what to do about it. This ticket makes the runner say so **before** the round, print the exact entry to add, and — only when asked — register the missing ids for that round through Kilo's own `KILO_CONFIG_CONTENT`, without editing `kilo.jsonc`.

## What happens today

### Symptom (real log, 2026-09-21, `--models` with 8 ids, `--max-parallel 8`)

```text
$ time python3 -m tools.contest run --ticket 64 --models agnes-2-5-flash:free,mimo-v2-5:free,step-3-7-flash:free,hy3:free,laguna-s-2-1:free,nex-n2-5-pro:free,agnes-3-0-flash:free,glm-4-7-flash:free --max-parallel 8
ticket   64-kc25-intake-checks-every-roster-model-against-get-provider-and-names-the-id-to-use.md — KC-25 — …
agents   8: kenary/agnes-2-5-flash:free, kenary/mimo-v2-5:free, kenary/step-3-7-flash:free, kenary/hy3:free, kenary/laguna-s-2-1:free, kenary/nex-n2-5-pro:free, kenary/agnes-3-0-flash:free, kenary/glm-4-7-flash:free
parallel 8
…
2026-09-21 11:07:19,973 tools.contest.runner agnes-3-0-flash: PROMPTED — attempt 0 (initial)
2026-09-21 11:07:19,974 tools.contest.runner nex-n2-5-pro: PROMPTED — attempt 0 (initial)
2026-09-21 11:07:20,038 tools.contest.runner agnes-3-0-flash: WAITING
2026-09-21 11:07:20,044 tools.contest.runner nex-n2-5-pro: WAITING
2026-09-21 11:07:30,254 tools.contest.runner agnes-3-0-flash: ERROR — session.error: {"name": "UnknownError", "data": {"message": "Model not found: kenary/agnes-3-0-flash:free. Did you mean: agnes-2-0-flash:free, agnes-2-5-flash:free, glm-4-7-flash:free?"}}
2026-09-21 11:07:30,264 tools.contest.runner nex-n2-5-pro: ERROR — session.error: {"name": "UnknownError", "data": {"message": "Model not found: kenary/nex-n2-5-pro:free. Did you mean: agnes-2-0-flash:free, agnes-2-5-flash:free, glm-4-7-flash:free?"}}
```

- Intake **passed**; eight sessions were opened; two died at t+10.3 s with no tool call made. The other six carried on (`WAITING`) until the operator's Ctrl-C at 11:07:33.
- The message comes from Kilo, not from the provider: it names the id **with Kilo's provider prefix** (`kenary/…`) and offers Kilo's own list as `Did you mean`.

### The provider serves the same ids (operator's curl, same key, same minute)

| id | HTTP | time | reply |
|---|---|---|---|
| `hy3:free` | 200 | 3.1 s | `Hello! How can I help you today?` |
| `agnes-2-5-flash:free` | 200 | 11.0 s | `Hello! How can I help you today?` |
| `agnes-3-0-flash:free` | 200 | 44.3 s | `Hello!` |
| `nex-n2-5-pro:free` | 200 | 1.9 s | `Hello!` |

```bash
curl -sS https://kenari.id/v1/chat/completions \
  -H "Authorization: Bearer $KENARI_KEY" -H "Content-Type: application/json" \
  -d '{"model":"agnes-3-0-flash:free","messages":[{"role":"user","content":"Say hello"}],"max_tokens":256}'
```

Note the id here is **bare** (`agnes-3-0-flash:free`); `kenary/` exists only inside Kilo.

### Where the list lives, and why the repo cannot fix it by itself

`kilo serve` builds its model list from `~/.config/kilo/kilo.jsonc` (`$XDG_CONFIG_HOME/kilo/`), one entry per model, and the repo reads none of it (KC-25 *Out of scope*: "the server's view is the only one that matters"). `contest.ini` and `agents_from_models` only pass `provider/model` through. So adding a model to `contest.ini` or `--models` never registers it in Kilo.

### Two ways an operator gets this wrong

1. **Reads it as a provider outage** ("404") and goes looking at kenari. The differential:

   | | id missing from Kilo's list | id missing on the provider |
   |---|---|---|
   | when | ~10 s after the prompt, before any tool call | after the turn has started |
   | text | `Model not found: kenary/<id>. Did you mean: …` | provider-side error (e.g. round 1: `the model's provider rejected the request. check the model id, request fields, and context length`) |
   | check | `kilo models \| grep kenary/<id>` | `curl …/v1/chat/completions` as above |

2. **Trusts `Did you mean`.** It compares spelling, not availability. `difflib.get_close_matches` over the ids seen so far suggests `agnes-2-0-flash:free` for `agnes-3-0-flash:free` and `agnes-2-5-flash:free` for `nex-n2-5-pro:free` — a *different model* in the second case. KC-25's failure line will carry the same suggestion, so it needs a sentence saying the id may simply be unregistered.

## Verified — the fix works, without touching `kilo.jsonc`

Real CLI, `@kilocode/cli` 7.6.2 and 7.7.5, a stand-in global `kilo.jsonc` shaped like the one KC-10/KC-11 describe (provider `kenary`, display name `kenari`, entries with `name` + `reasoning`), `hy3:free` and `agnes-2-5-flash:free` registered:

```text
$ kilo models | grep kenary                                   # baseline
kenary/agnes-2-5-flash:free
kenary/hy3:free

$ KILO_CONFIG_CONTENT='{"provider":{"kenary":{"models":{
    "agnes-3-0-flash:free":{"name":"agnes-3-0-flash:free","reasoning":true},
    "nex-n2-5-pro:free":{"name":"nex-n2-5-pro:free","reasoning":true}}}}}' kilo models | grep kenary
kenary/agnes-2-5-flash:free
kenary/agnes-3-0-flash:free
kenary/hy3:free
kenary/nex-n2-5-pro:free
```

- The overlay **deep-merges**: existing models, `name`, `options.baseURL` and `apiKey` are untouched (7.6.2 and 7.7.5).
- `KILO_CONFIG=/path/extra.jsonc` merges the same way (7.6.2).
- On 7.7.5, `kilo serve` + `GET /provider?directory=…` reported `kenary` (`name: kenari`) as **connected**, and both new models as `status: active`, `capabilities.toolcall: true`, `capabilities.reasoning: true` — the same endpoint KC-25's intake reads.
- `KiloServer.spawn` starts the child with **no `env=`**, so the child already inherits the operator's environment: exporting the variable before `python3 -m tools.contest run` works today, with no code change (that is the workaround below).
- Not verified: a full round against kenari (no network to it, no key, from where this was checked).

## Operator workaround today (no code)

```bash
# permanent: add under provider.kenary.models in ~/.config/kilo/kilo.jsonc
"agnes-3-0-flash:free": { "name": "agnes-3-0-flash:free", "reasoning": true },
"nex-n2-5-pro:free":    { "name": "nex-n2-5-pro:free",    "reasoning": true }

# or per run, nothing edited:
export KILO_CONFIG_CONTENT='{"provider":{"kenary":{"models":{"agnes-3-0-flash:free":{"name":"agnes-3-0-flash:free","reasoning":true},"nex-n2-5-pro:free":{"name":"nex-n2-5-pro:free","reasoning":true}}}}}'

# check every roster id is registered before a round
for m in agnes-2-5-flash:free mimo-v2-5:free step-3-7-flash:free hy3:free laguna-s-2-1:free nex-n2-5-pro:free agnes-3-0-flash:free glm-4-7-flash:free; do
  kilo models | grep -qx "kenary/$m" || echo "missing in Kilo: $m"
done
```

## What must change

Everything below is on top of KC-25 (which finds the missing id and names it). This ticket adds the remedy.

1. **`KiloServer.spawn(..., env: dict | None = None)`** (`kilo_client.py`). `env` entries are added on top of `os.environ` for the child (`{**os.environ, **env}`); `None` keeps today's behaviour exactly. No other change to spawn.
2. **`roster_missing(providers: dict, agents) -> list`** (`cli.py`, pure, next to KC-25's `roster_on_offer`): the agents whose provider is **known and in `connected`** but whose model id is **not on offer** — nothing else. Unknown provider, or a provider without credentials, is *not* "missing": registering a model cannot create a provider or supply a key.
3. **`registration_overlay(agents) -> dict`** (pure): `{"provider": {<pid>: {"models": {<mid>: {"name": <mid>, "reasoning": True}}}}}`, grouped by provider, ids in roster order, `{}` for an empty list. `reasoning: True` mirrors the existing `kenary`/`sensenova123` entries (KC-11's Source) and is what the verification above ran with.
4. **`merge_config_content(existing: str | None, overlay: dict) -> str`** (pure): the JSON string for `KILO_CONFIG_CONTENT`. An unset/empty `existing` gives the overlay; a JSON object is deep-merged with the overlay's models added; anything else is a `ValueError` that intake prints as `intake: KILO_CONFIG_CONTENT is not a JSON object: …`.
5. **Hint line in intake (no flag).** When `roster_missing(...)` is non-empty, after the KC-25 failure lines print **once**:
   `intake: hint: <n> id(s) are not in Kilo's model list for provider '<pid>'. "did you mean" compares spelling only — if the provider serves the id, add it under provider.<pid>.models in kilo.jsonc, or re-run with --register-missing.`
   KC-25's own lines and their wording stay as they are; the hint is an extra line.
6. **`--register-missing` on `run`** (default off). With it, in intake:
   - the agents in `roster_missing(...)` are **not failures**; the rest of KC-25's `roster_on_offer` still is (computed over the other agents);
   - `server = spawn` only. With an attached URL the server reads its own `kilo.jsonc` and this process cannot change it: one failure line, `--register-missing needs server = spawn: an attached server reads its own kilo.jsonc`;
   - intake builds `merge_config_content(os.environ.get("KILO_CONFIG_CONTENT"), registration_overlay(missing))`, spawns a **second throwaway server** with `env={"KILO_CONFIG_CONTENT": …}`, reads `providers()` again and requires `roster_missing(...) == []` — else `intake: registration did not take: <ids>` and no worktree is created;
   - the string is kept on the `Intake` (`Intake.config_content`, default `None`); `cmd_run` passes it to `_start_server(config, out_dir, env=…)` so the real server gets the same overlay. `_start_server` without it is unchanged.
7. **`_print_plan`** adds one line when the flag registered something: `registered for this round (kilo.jsonc untouched): kenary/agnes-3-0-flash:free, kenary/nex-n2-5-pro:free`.

### Expected log after the change (proposed wording — not produced by code yet)

Default, ids not registered:

```text
$ python3 -m tools.contest run --ticket 64 --models …,nex-n2-5-pro:free,agnes-3-0-flash:free,…
intake: [agnes-3-0-flash] kenary/agnes-3-0-flash:free: no model 'agnes-3-0-flash:free' under 'kenary' — on offer: agnes-2-0-flash:free, agnes-2-5-flash:free, glm-4-7-flash:free, hy3:free, … (did you mean agnes-2-0-flash:free?)
intake: [nex-n2-5-pro] kenary/nex-n2-5-pro:free: no model 'nex-n2-5-pro:free' under 'kenary' — on offer: agnes-2-0-flash:free, agnes-2-5-flash:free, glm-4-7-flash:free, hy3:free, … (did you mean agnes-2-5-flash:free?)
intake: hint: 2 id(s) are not in Kilo's model list for provider 'kenary'. "did you mean" compares spelling only — if the provider serves the id, add it under provider.kenary.models in kilo.jsonc, or re-run with --register-missing.
$ echo $?
1
```

(The `on offer` list above is illustrative — built from the ids seen in the logs; the real line prints the server's own sorted list.)

With `--register-missing`:

```text
$ python3 -m tools.contest run --ticket 64 --models … --register-missing
ticket   64-kc25-… 
base     067ca5c2c0ba
agents   8: kenary/agnes-2-5-flash:free, …, kenary/nex-n2-5-pro:free, kenary/agnes-3-0-flash:free, …
registered for this round (kilo.jsonc untouched): kenary/nex-n2-5-pro:free, kenary/agnes-3-0-flash:free
parallel 8
…
… tools.contest.runner agnes-3-0-flash: PROMPTED — attempt 0 (initial)
… tools.contest.runner agnes-3-0-flash: WAITING
```

No `Model not found`. If the provider itself does not serve the id, the run now fails the way a provider-side problem should: after the turn starts, with the provider's own message (see the differential table).

Still a failure with the flag (provider cannot be invented):

```text
intake: [sensenova-6-8-flash-lite] sensenova/sensenova-6.8-flash-lite: no provider 'sensenova' — that is the display name of provider 'sensenova123'; use sensenova123/sensenova-6.8-flash-lite
```

## Acceptance

- [ ] `tests/test_contest_kilo_client.py`: `KiloServer.spawn(stub, log_path=…, env={"KILO_CONFIG_CONTENT": "{}"})` — a stub binary (the one the spawn tests already use) records its environment; the child saw the entry **and** the parent's variables; `env=None` behaves exactly as before (existing spawn tests unmodified).
- [ ] `tests/test_contest_cli.py`, pure functions, no server:
  - `roster_missing` returns exactly the known-and-connected-provider / unknown-model agents; empty for an all-on-offer roster; ignores unknown provider and not-connected provider (those stay `roster_on_offer` lines);
  - `registration_overlay`: grouped by provider, roster order, `{}` for none, `{"name": id, "reasoning": True}` per model;
  - `merge_config_content`: `None`/empty → the overlay; an existing object keeps its models and gains the new ones; an existing `{"provider": {"kenary": {"models": {"hy3:free": {...}}}}}` keeps `hy3:free`; a non-object or invalid JSON → `ValueError`.
- [ ] Intake without the flag: a roster with one missing model prints the KC-25 line **and** the hint line once, exit `EXIT_FAILED`, no worktree created; a roster entirely on offer prints no hint.
- [ ] Intake with `--register-missing`: with a stub `kilo` whose `/provider` answer includes the models named in its `KILO_CONFIG_CONTENT`, intake passes, `Intake.config_content` names the missing ids, the real server is spawned with the same string, `_print_plan` shows the `registered for this round` line; when the stub ignores the variable, intake fails with `registration did not take: <ids>` and no worktree is created.
- [ ] `--register-missing` with an attached server (`server = http://…`) → the one `needs server = spawn` line, exit `EXIT_FAILED`.
- [ ] A missing **provider** or a provider that is not connected is still an intake failure with the flag.
- [ ] An operator's own `KILO_CONFIG_CONTENT` in the environment survives the merge (its models are still on offer in the second throwaway).
- [ ] Every existing intake and spawn test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Reading or writing `kilo.jsonc` (KC-25's decision stands: the server's view is the only one that matters).
- Inventing a provider, a `baseURL` or a key — a provider that is unknown or not connected stays a failure.
- Asking the provider itself whether it serves the id (`GET <baseURL>/models` needs the key Kilo holds) — that is the operator's curl.
- Non-Kilo backends (KC-34): they change where a session runs, not which ids Kilo lists.
- Rewording KC-25's failure lines or changing its exit codes.
- Seen in the same log and left alone: after the operator's Ctrl-C six `abort(...) failed: KiloHttpError: POST /session/<id>/abort -> 503` lines. Also `agnes-3-0-flash:free` needed 44 s for a one-word reply; whether `idle_event_timeout_sec = 300` matters for it is not established here.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**; `python3 -c "import tools.contest.cli, tools.contest.kilo_client"` from the repo root. No backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/cli.py`, `tools/contest/kilo_client.py`, `tests/_kilo_fake.py` (only if touched), `tests/test_contest_cli.py`, `tests/test_contest_kilo_client.py` (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `git diff <base>..HEAD -- tests/test_contest_kilo_client.py | grep -c '^-[^-]'` is 0 — earlier tests gain, never lose, lines.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**, both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
