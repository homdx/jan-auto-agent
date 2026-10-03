# AR-63 — the ticket writer and reviewer as profile roles: `arena model set-role / unset-role`, `issue create --writer/--reviewer/--same-model`

**Status:** queued
**Severity:** LOW
**File:** tools/arena/models.py
**Symbol:** set_role, unset_role, role_settings, draft_pair
**Round:** 146
**Size:** M
**Also touches:** tools/arena/profile.py, tools/arena/cli.py, tools/arena/tickets.py, tests/test_arena_model_roles.py, tests/test_arena_issue_create.py

**Depends on:** AR-7 (round 145, `arena issue create`). Start this round only over the commit that lands AR-7.

---

## Why

After AR-7, `arena issue create` takes its two models (the writer, and the reviewer that checks the draft) only from the old `tools.contest` keys: `[contest] draft_llm_profile` and `draft_review_llm_profile` / `gate_llm_profile` in `contest.local.ini`. Each key names a hand-written `LlmSettings` section (`base_url`, `api_key`, `model`). Switching the writer means editing the ini by hand.

The round's own models are already managed from the command line (`arena model use / drop -p PROFILE`, AR-59), checked by name, and kept per profile. AR-63 does the same for the two ticket roles:

- `arena model set-role writer|reviewer NAME -p P` and `arena model unset-role writer|reviewer -p P` store the role in the profile;
- `arena -p P issue create …` drafts with that profile's pair;
- `--writer NAME` / `--reviewer NAME` override one call without touching the profile;
- `--same-model` (or the profile key `same_model_review = yes`) lets one model both write and review, for an operator who has only one model. Two different models stay the default and the rule; one model is an explicit, visible choice.

---

## What AR-1 … AR-7, AR-59 … AR-62 already give you (read them before writing)

- `tools/arena/profile.py`:
  - `KNOWN_KEYS` is the one key registry. A key mapped to `None` is arena-owned data, not a runner flag; `profile_flags` skips it.
  - `load_profiles(repo)` reads `contest.ini` then `contest.local.ini`, and refuses unknown keys.
  - `fresh_on(value)` is the yes/no parser.
- `tools/arena/models.py`:
  - `provider_config(repo, provider, url_flag)` → `(base_url, api_key)`. The key is `[arena.provider.NAME] api_key = ${ENV}` (a literal key is refused) or env `ARENA_KEY_<NAME>`. The URL is env `ARENA_URL_<NAME>` or the section's `base_url`.
  - `resolve_test_targets(repo, names, url_flag, now)` checks each name against the direct provider's own model list and returns specs with `via = "direct" | "kilo"` and `url`, `key` for direct ones.
  - `write_profile_keys(repo, name, {key: value | None})` is the one ini writer: `contest.local.ini` only, an in-place text edit, atomic, every other line (an `api_key` among them) kept byte for byte.
  - `_confirm(...)` is `use`'s before → after prompt; `-y` skips the prompt. `_selected_profile(repo, args)`: `-p`, else `[arena] profile`, else `default`.
  - `ModelError` is the one-line refusal; `_hide` / `output.scrub` take keys out of any text.
- `tools/arena/cli.py`:
  - `OBJECTS["model"]` holds `available`, `test`, `use`, `drop`.
  - `_set_values` is `profile set`'s key check. It already refuses `models` ("set by `arena model use`").
- `tools/arena/tickets.py` (AR-7): `issue_create(repo, args, prof)`, step 5: `contest_cli.draft_callables(config, args.no_review)` → `(llm_call, review_call)`, and `DraftSetupError` is a refusal.
- `tools/contest/cli.py` (AR-7): `draft_callables`, `DraftSetupError`, `_same_model(a, b)`.
- `tools/contest/draft.py`: `llm_call_for(settings, system=…)`, `REVIEW_SYSTEM_PROMPT`.
- `tools/contest/roster.py`: `DEFAULTS_DRAFT`, the `LlmSettings` defaults the old draft keys resolve against.
- `tools/auto/llm_profile.py`: `LlmSettings(base_url, api_key, model, …)`, frozen dataclass.

---

## What to build

### 1. Profile keys (`tools/arena/profile.py`)

Add three keys to `KNOWN_KEYS`, all mapped to `None` (arena data, never a runner flag):

| key | value | written by |
|---|---|---|
| `writer` | `provider/model` | `arena model set-role writer` only |
| `reviewer` | `provider/model` | `arena model set-role reviewer` only |
| `same_model_review` | `yes` / `no` (`fresh_on` rules) | `arena profile set NAME same_model_review=yes -y` |

- `load_profiles` refuses a `same_model_review` value that `fresh_on` refuses, naming the section, as it already does for `fresh`.
- `profile_flags` output is unchanged for every existing profile: the three keys never produce a flag.
- `profile set` (`cli._set_values`) refuses `writer` and `reviewer` the same way it refuses `models`: `writer is set by \`arena model set-role\`, not \`profile set\``. It accepts `same_model_review` and checks it with `fresh_on`.
- `profile view` shows the three keys as rows like any other key.

### 2. `arena model set-role ROLE NAME [-p P] [-y]` (`tools/arena/models.py: set_role`)

- `ROLE` is `writer` or `reviewer`; anything else is a refusal listing the two.
- `NAME` is exactly one `provider/model`:
  - no `,`; no `@variant` (a role has no variant);
  - a name without `provider/` is a refusal: `name the provider: PROVIDER/MODEL`.
- **The provider must be direct.** The draft is one HTTP call (`llm_call_for`), not a Kilo session, so a provider reachable only through Kilo cannot write tickets. `provider_config(repo, provider)` must give both a URL and a key. Otherwise:
  `writer needs a direct provider: 'P' has no base_url/api_key — [arena.provider.P] api_key = ${ENV} and base_url, or ARENA_KEY_P / ARENA_URL_P`
  (`P` is the env name form `_env_name` makes; the key value is never in the message).
- The model is checked against the provider's own list, the way `resolve_test_targets` does for a direct name. An unknown model is a refusal with `_check_known`'s hints. Calling `resolve_test_targets(repo, [NAME], None, now)` and requiring `via == "direct"` is the expected way; do not copy it.
- Before → after, as `use` does: `profile 'P' — writer =`, `before: …`, `after: …`; prompt unless `-y`; EOF or "no" → refusal `not applied — contest.local.ini unchanged`.
- The same name as the current value → `profile 'P': writer = NAME (unchanged)`, exit 0, nothing written.
- Writes `writer = NAME` (or `reviewer = …`) with `write_profile_keys`. A missing profile section is created, as `use` creates one.
- **Hint, not refusal:** when the other role of the same profile names the same model (compare the part after `provider/`, lower-cased, as `_same_model` compares) and `same_model_review` is not on, print one stderr line:
  `arena: writer and reviewer are the same model — issue create will refuse without --same-model (or profile set P same_model_review=yes)`.
- The judge check of `use` (`check_not_judge`) does **not** apply: a role is not a round agent.

### 3. `arena model unset-role ROLE [-p P] [-y]` (`unset_role`)

- Removes the role's key (`write_profile_keys(..., {role: None})`), with the same before → after and prompt.
- An unknown profile, or a profile without that role → refusal: `profile 'P' has no writer`.

### 4. Which models `issue create` uses (`role_settings`, `draft_pair`)

New flags on `arena issue create`: `--writer NAME`, `--reviewer NAME`, `--same-model`. The existing `--no-review` stays.

Each role is resolved **on its own**, first match wins:

| | writer | reviewer |
|---|---|---|
| 1 | `--writer NAME` | `--reviewer NAME` |
| 2 | the profile's `writer` | the profile's `reviewer` |
| 3 | the old keys: `[contest] draft_llm_profile` | the old keys: `draft_review_llm_profile`, else `gate_llm_profile` |
| 4 | — | with `--same-model` / `same_model_review = yes`: the writer |

- The profile is the one `-p` / `[arena] profile` selects (`prof` in `issue_create`).
- Steps 1–2 build an `LlmSettings` with `role_settings(repo, name) -> LlmSettings`:
  - `base_url`, `api_key` from `provider_config`; `model` = the part after `provider/`;
  - every other field from `roster.DEFAULTS_DRAFT`;
  - the same direct-provider and known-model checks as `set_role`; each failure is a `ModelError`.
  - A `--writer` / `--reviewer` flag is checked exactly like a stored role. A stored role whose provider lost its key since `set-role` is refused at this point, naming the profile key: `profile 'P': writer = NAME — 'P' has no api_key …`.
- Step 3 is AR-7's `draft_callables` path, unchanged. Mixing is allowed: a profile writer with the old reviewer key, or the reverse.
- `draft_pair(repo, args, prof, config) -> tuple[Callable, Optional[Callable], dict]` returns `(llm_call, review_call, info)`. `info` holds `{"writer": "provider/model", "reviewer": "provider/model" | None, "same_model": bool, "reviewed": bool}`, with names only and never a key or URL. `issue_create` step 5 calls `draft_pair` instead of `draft_callables`. `draft_callables` itself is not changed.

**Refusals** (exit 2, one line, before collect and before any model call, nothing written):
- No writer anywhere → `no ticket writer — arena model set-role writer PROVIDER/MODEL -p P, or --writer, or [contest] draft_llm_profile`.
- No reviewer anywhere, with neither `--no-review` nor same-model on → `no ticket reviewer — arena model set-role reviewer …, --reviewer, --same-model, or --no-review`.
- **Writer and reviewer are the same model** (`_same_model` on the two `LlmSettings`, so the provider and URL do not matter) and same-model is off → `writer and reviewer are the same model (M) — pick another reviewer, or pass --same-model`.
- `--same-model` together with `--no-review` → `--same-model and --no-review contradict: one reviews with the writer, the other skips the review`.
- `--writer` / `--reviewer` given an invalid name → `set_role`'s refusal text for that name.

**Same model on** (`--same-model`, or the profile's `same_model_review = yes`):
- writer == reviewer is allowed. When no reviewer is found in steps 1–3, the writer is the reviewer (step 4).
- The review is still a separate call with `draft.REVIEW_SYSTEM_PROMPT` (`llm_call_for(settings, system=REVIEW_SYSTEM_PROMPT)`), never the writer's own conversation.
- **Only when the two really are one model**, one stderr line: `review by the writer's own model (M) — --same-model`. With two different models and same-model on, nothing is printed and nothing changes.

### 5. Output additions (`issue create`)

- Success, table mode: AR-7's two stdout lines are unchanged. One new stderr line before them: `writer M1 · reviewer M2` (or `· reviewer skipped (--no-review)`, or `· reviewer M1 (same model)`). Names only.
- `-o json`: the AR-7 object gets `"writer"`, `"reviewer"` (null with `--no-review`) and `"same_model"` (bool). `reviewed` keeps its AR-7 meaning.

### 6. CLI (`tools/arena/cli.py`)

- `OBJECTS["model"].verbs`: add `"set-role"` (`"set a profile's ticket writer or reviewer"`, `"AR-63"`) and `"unset-role"` (`"remove a profile's ticket writer or reviewer"`, `"AR-63"`). `-y` / `-p` work after the verb like `use` (`_late_globals`).
- `issue create`'s `add_arguments`: `--writer NAME`, `--reviewer NAME`, `--same-model` (store_true).

---

## Not in this ticket

- More than one reviewer (a list, voting) — later, if wanted.
- Ticket roles through Kilo (`kilo run`): the draft stays one direct HTTP call.
- `arena model test` of the role models before `set-role` — the operator runs `arena model test NAME` first if they want a score.
- No change to `tools.contest draft` (`cmd_draft`) or to `draft_callables`: the old command keeps its own rules, including its refusal of one model.

---

## Tests

### `tests/test_arena_model_roles.py` (new)

Set-up: a throw-away repo under `tmp_path`, `cli.REPO_ROOT` patched, `contest.local.ini` holding `[arena.provider.direct]` with `api_key = ${T_DIRECT_KEY}` and `base_url = http://127.0.0.1:9/v1`, env `T_DIRECT_KEY=sk-test-ROLESECRET` via `monkeypatch.setenv`. The direct model list is a fake: patch `models._direct_for` to return records for `model-a` and `model-b`. Kilo is patched to know `kiloprov/model-k` only (through the `KILO_LIST` seam). No network, no kilo binary. Placeholder names only.

1. `model set-role writer direct/model-a -p p1 -y` → exit 0. `contest.local.ini` has `[arena.profile.p1]` `writer = direct/model-a`, and every other line of the file is byte for byte as before (the `api_key` line included).
2. Without `-y`: before/after printed, stdin `n` (and stdin EOF) → exit 2, file unchanged.
3. Same value again → `(unchanged)`, exit 0, file mtime and bytes unchanged.
4. Refusals, exit 2, one line, file unchanged: role `judge`; `model-a` without provider; `direct/model-a@high`; `direct/a,direct/b`; `direct/nosuch` (hint text from `_check_known`); `kiloprov/model-k` (a Kilo-only provider → the "needs a direct provider" line); a provider whose section has a literal key.
5. `set-role reviewer direct/model-a` when writer is `direct/model-a` → exit 0, written, plus the one-line same-model hint on stderr. With `same_model_review = yes` in the profile → no hint.
6. `unset-role writer -p p1 -y` removes the line only; again → refusal `has no writer`; unknown profile → refusal.
7. `profile set p1 writer=direct/model-a -y` → refusal naming `set-role`; `profile set p1 same_model_review=maybe -y` → refusal; `same_model_review=yes -y` → written.
8. `profile_flags` of a profile with `writer`, `reviewer`, `same_model_review` set is identical to the same profile without them (no flag leaks into the round command line).
9. The key `sk-test-ROLESECRET` appears in no stdout or stderr in any case above, and in no `-o json`.

### Additions to `tests/test_arena_issue_create.py` (append only)

The AR-7 fakes stay (fake writer/reviewer recording their prompts; `draft.run_collect` a no-op). Additionally patch `draft.llm_call_for` so that each call records **which `LlmSettings.model`** and which `system` it was made for, and returns the matching fake.

10. **Profile pair.** Profile `p1`: `writer = direct/model-a`, `reviewer = direct/model-b`. `arena -p p1 issue create "brief"` → exit 0. The writer fake was built for `model-a`, the reviewer for `model-b` with `REVIEW_SYSTEM_PROMPT`. The stderr line is `writer direct/model-a · reviewer direct/model-b`. The old `[contest] draft_llm_profile` sections, when present, are **not** used.
11. **Flags win.** `--writer direct/model-b --reviewer direct/model-a` beats the profile.
12. **Each role on its own.** A profile writer plus the old `gate_llm_profile` reviewer → both used. The old writer key plus `--reviewer` → both used.
13. **Same model refused.** The profile has writer = reviewer = `direct/model-a`, no flag → exit 2, one line, no fake called, nothing under `.arena/drafts/`. The same with the writer through `direct/` and the reviewer through the old key naming `model-a` behind another URL → refused (`_same_model` ignores the URL).
14. **`--same-model`.**
    - Same pair plus `--same-model` → exit 0. The reviewer fake is called with `REVIEW_SYSTEM_PROMPT` and `model-a`, as a separate call. The stderr line `review by the writer's own model (model-a) — --same-model` appears once.
    - Writer only, no reviewer anywhere, `--same-model` → the writer reviews (step 4).
    - Two different models plus `--same-model` → nothing extra printed.
    - Profile `same_model_review = yes` instead of the flag → same as the flag.
    - `--same-model --no-review` → exit 2.
15. **Nothing set at all** (no profile roles, no old keys) → exit 2 with the "no ticket writer" line naming `set-role`.
16. **A stored role that broke** (the env key unset after `set-role`) → exit 2, the line names the profile key, no model called.
17. **`-o json`** has `writer`, `reviewer`, `same_model`; `reviewer` is null with `--no-review`.
18. **AR-7's own cases** (1–14 of ticket 145) still pass untouched; they use the old keys, so step 3 must keep working with no profile role set.
19. **Secrets.** The key never appears in stdout, stderr or JSON in cases 10–17.

Tier new test files with `python3 scripts/sync_test_tiers.py`; never edit the tier symlinks by hand.

---

## Acceptance

Run each command separately and in this order:

```bash
python3 scripts/sync_test_tiers.py
```

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests/test_arena_model_roles.py tests/test_arena_issue_create.py tests/test_arena_profile.py tests/test_arena_profile_set.py tests/test_arena_models.py tests/test_arena_model_test.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass. `contest-bench/139`, `142`, `143`, `144` and `145` still pass.

---

## Rules

- **No change under `tools/contest/`** (not `cli.py`, not `draft.py`, not `roster.py`) and nothing under `scripts/`. Import `draft_callables`, `DraftSetupError`, `_same_model`, `llm_call_for`, `REVIEW_SYSTEM_PROMPT`, `DEFAULTS_DRAFT`; do not copy them.
- `contest.local.ini` is written only through `write_profile_keys`; never `contest.ini`, never `agents_128k.ini`.
- `REPO_ROOT` is the only repo seam; never read the current working directory.
- **No real model call anywhere in the tests.** Every LLM and every model list is a fake; no network.
- **Never run a real round while working on this ticket** — no `python3 -m tools.contest run …`, no `arena run start|rerun …`, no `scripts/revive_round.py` on a real `contest-out/`. Never run `arena issue create` or `arena model set-role` against the real repo.
- Every refusal is one line through `output.refuse`, exit 2, and writes nothing. Every `issue create` refusal added here happens before collect and before any model call.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files. Never print an api key or a URL with credentials; `info`, JSON and every message carry names only.
- Do not change existing tests except to add cases.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/models.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `146: …`.
