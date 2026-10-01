# KC-2 — `tools/contest/roster.py`: `contest.ini` — the agents, the limits, and the gate model's profile

**Status:** landed — ideal patch from a 6-entry contest, `kc2/*.patch`: DeepSeek, Laguna-S-2-1, SenSenova-6-7, SenSenova-6-8-var1, SenSenova-6-8-var2, Step3-7-Flash — four dropped before scoring: Laguna-S-2-1, SenSenova-6-8-var1/var2 and Step3-7-Flash each bundled the on-ticket files inside a diff that also touched (or re-included) 12 to 76 unrelated files spanning other epics and failed to apply cleanly on top of KC-1. Of the two clean 5-file diffs, DeepSeek's own `contest.ini` cited a commit hash (`scripts/kilo_hello.py (commit 67e834d)`) — the exact stale-pin anti-pattern this epic is removing — where SenSenova-6-7 points to `docs/kilo-contest/PROBE.md` only; SenSenova-6-7 also raises consistently (fail-closed) on a broken `gate_llm_profile` instead of DeepSeek's silent fail-open fallback. SenSenova-6-7 taken as-is. `tests` + `tests_bugfix` green. Re-verified live against all three real models — see `docs/kilo-contest/PROBE.md`'s "KC-2 roster re-verified live" section.  
**Severity:** MEDIUM  
**File:** `tools/contest/roster.py` (new)  
**Symbol:** `ContestConfig`, `AgentSpec`, `load_roster`, `DEFAULTS`  
**Round:** 41  
**Size:** S  
**Source:** the probe ran three models by hand (`--model kenary/laguna-s-2-1:free` …). The contest needs the same information once, in a file, with the limits the runner (KC-6) and the policy (KC-3) read, and a named LLM profile for the safety gate in the same shape Gate 1 already uses (`[gate1] presence_llm_profile = gate1_llm` → `tools.auto.llm_profile.resolve_llm_profile`).  
**Depends on:** KC-1 only for the package folder.  
**Also touches:** `contest.ini` (new, committed, no secrets), `tests/test_contest_roster.py` (new), `.gitignore` (`contest.local.ini`, `contest-out/`)

---

## What happens today

There is no roster. `agents*.ini` describe the auto pipeline's own LLM
profiles and know nothing about Kilo, worktrees, or which models compete.
`resolve_llm_profile(config, section, key, defaults=LlmSettings(...))`
exists and is the repo's one way to name a second model for a gate; it is
what the safety gate must use so that `api_key`, `base_url`, `api_format`,
`response_format`, `think` behave exactly as in `[gate1_llm]`.

## What must change

1. **`contest.ini`** (committed) with three kinds of section:

   ```ini
   [contest]
   kilo_bin                  = auto            ; or a path; `auto` = newest VS Code extension
   server                    = spawn           ; or http://127.0.0.1:PORT to attach
   max_parallel              = 3
   max_rework                = 2               ; rework prompts per agent after the first turn
   turn_timeout_sec          = 1800            ; one prompt → idle
   idle_event_timeout_sec    = 300             ; no event at all for this long → abort
   max_questions_per_turn    = 3
   tmp_roots                 = /tmp/kilo/*, /tmp/contest/*
   deny_commands             = git push*, sudo *, rm -rf /*, curl * | sh, wget * | sh
   gate_llm_profile          = contest_gate_llm
   gate_max_calls_per_session= 20
   out_dir                   = contest-out
   rounds_dir                = ../rounds

   [contest_gate_llm]                          ; LlmSettings fields, as [gate1_llm]
   base_url        = https://example/v1
   api_key         = ${CONTEST_GATE_API_KEY}   ; env reference, never a literal in the committed file
   model           = some/model
   api_format      = openai
   response_format = true
   temperature     = 0.0
   max_tokens      = 256

   [contest.agent.laguna]
   model      = kenary/laguna-s-2-1:free
   kilo_agent =                               ; empty = server default
   variant    =
   [contest.agent.mistral]
   model      = kenary/mistral-medium-3-5:free
   [contest.agent.hy3]
   model      = kenary/hy3:free
   ```

2. **`load_roster(path, *, overlay=None) -> ContestConfig`** — reads
   `path`, then `contest.local.ini` next to it if present (git-ignored;
   where a real `api_key` lives), `overlay` last. `${ENV}` references in
   any value are expanded from the environment; an unresolved one is an
   error naming the key. **Unknown keys in `[contest]` or in an agent
   section are an error** (fail-closed, like `tools/config_safe.py` for the
   auto config); unknown sections are ignored (so `agents.ini` can be
   passed as an overlay without complaint).

3. **`ContestConfig`** — frozen dataclass with every `[contest]` key typed
   (`tmp_roots: tuple[str, ...]`, `deny_commands: tuple[str, ...]`,
   ints as ints), `agents: tuple[AgentSpec, ...]` in file order, and
   `gate_settings: LlmSettings` resolved via
   `resolve_llm_profile(config, "contest", "gate_llm_profile", defaults=DEFAULTS_GATE)`.
   `AgentSpec` = `name`, `provider_id`, `model_id` (split at the **first**
   `/`, as `kilo models` prints it — `kilo/~anthropic/x` is provider
   `kilo`), `kilo_agent: str | None`, `variant: str | None`. `name` must
   match `[a-z0-9][a-z0-9_-]*` (it becomes a branch and a folder).
   Duplicate names → error. Zero agents → error.

4. **`ContestConfig.session_rules() -> list[dict]`** — the rule list KC-1's
   `create_session` sends: `*` allow, `external_directory` ask,
   `doom_loop` ask, then one `{"permission": "bash", "pattern": p, "action": "deny"}`
   per `deny_commands` entry. Single source of truth for the rules; KC-3
   and KC-6 import it.

5. `contest.ini` carries the three probe models as the default roster and
   a comment block at the top saying what each key does (the repo's
   `agents.ini` style).

## Acceptance

- [ ] `tests/test_contest_roster.py`: the committed `contest.ini` loads;
      `agents` are the three probe models with the right provider/model
      split; `kilo/~anthropic/claude-haiku-latest` splits at the first
      slash; a `${MISSING_ENV}` value errors naming the key; an unknown
      `[contest]` key errors naming it; a duplicate agent name errors;
      `contest.local.ini` overrides `api_key`; `session_rules()` has the
      three fixed rules first and one `bash deny` per `deny_commands`
      entry, in order.
- [ ] `gate_settings` comes from `resolve_llm_profile` (assert by
      monkeypatching it) with `response_format` true by default.
- [ ] `.gitignore` has `contest.local.ini` and `contest-out/`.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Reading `~/.config/kilo/kilo.jsonc` to validate that a model exists —
  the server answers that at `create_session` (KC-6 reports it).
- Per-agent `max_rework` / timeouts — one set of limits per round.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No secret in a committed file; tests use dummy values.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
