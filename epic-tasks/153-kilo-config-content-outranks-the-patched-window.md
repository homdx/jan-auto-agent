# 153 — a window the spawn overlay already set is not re-sized by `PATCH /config`: check it took, or keep the watch armed

**Status:** landed
**Severity:** HIGH
**File:** tools/contest/runner.py
**Symbol:** run_agent._push_remembered_limit, KiloBackend.set_model_limit
**Round:** 153
**Size:** S
**Also touches:** tools/contest/backend.py, tools/contest/kilo_client.py (`GET /provider` read-back), tests/test_contest_kilo_tap_reconnect.py or a new tests/test_contest_window_readback.py, tests/_kilo_fake.py

**Depends on:** 151 (landed on arena).

Found while judging round 151, checked live on Kilo 7.6.2 with `kilo serve` and no model call (`/provider`, `PATCH /config`, the files on disk).

## Facts (live, 7.6.2)

1. `KILO_CONFIG_CONTENT` outranks the workspace's `.kilo/kilo.jsonc`, field by field: a model whose `limit` the env content sets keeps that limit whatever the project file says, and a `PATCH /config` for it answers 200, writes the file — and `GET /provider` still shows the env limit. A field the env content does not set is taken from the project file, and the project file is read on the first open of the workspace (a model with no env limit showed the file's 333 333 at spawn, and a PATCH resized it at once).
2. Kilo writes its own `.kilo/.gitignore` (`node_modules`, `package.json`, the lock files, `.gitignore`, `agent-manager.json`, no trailing newline) when it opens a workspace that has a `.kilo/` and no `.gitignore` in it; it never rewrites one that is there. 151's guard now extends that file with the project names.

## The bug

`cli._spawn_overlay` hands every remembered window to the round's server in `KILO_CONFIG_CONTENT` (`provider.<p>.models.<m>.limit`). When the same model overflows again mid-round below that window, `_push_remembered_limit` sends a `PATCH /config` for the smaller size; Kilo answers 200 and keeps the overlay's limit (fact 1). The runner marks `pushed_limit[0]` and `context_watch` stands down, so the turn runs to the old, larger window — the refusal the memory just recorded. The same holds for any limit the operator's own `KILO_CONFIG_CONTENT` sets.

## Fix

- After the PATCH (and the reconnect), read the model back from `GET /provider?directory=` and compare `limit.context` (and `limit.input` when sent) with what was pushed. Only a match marks `pushed_limit[0]`; a mismatch logs one line (`Kilo kept <n> from KILO_CONFIG_CONTENT — the watch stays armed`) and is remembered like `push_refused` for that size, so it is not re-PATCHed every prompt.
- Or, cheaper and also acceptable: do not PATCH a model whose limit the server's spawn content carries (the runner knows the overlay), and keep the watch armed for it.

## Tests (offline, `tests/_kilo_fake.py`; each fails on 151)

1. The fake keeps a per-model limit from its "env" and ignores a PATCH for it: one PATCH, no `pushed_limit`, the watch stops the turn at the smaller size, one warning line.
2. A model with no env limit: PATCH, read-back matches, watch off (today's behaviour).
3. A read-back that fails (HTTP error): the watch stays armed.

## Live check on real `:free` models (required)

The offline fake cannot show which config Kilo obeys — that is what this ticket is about — so the fix is not done until it is seen on a real server:

1. `kilo serve` 7.6.2 spawned by the round's own path (`cli._spawn_overlay`), one or two `:free` models that refuse below Kilo's declared window (glm-4.5-flash at 98 777 is the known one), a remembered window in the overlay.
2. A second overflow below that window in the same run (a smaller `context-memory.json` record, or a big read).
3. Recorded in this ticket: the `GET /provider` limit before and after the PATCH, the runner's read-back line, and that the watch — not the provider — stopped the turn at the smaller size. A model with no limit in the overlay is checked the same way: there the PATCH must take.

Without this record the ticket stays open.

## Acceptance

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

## Live check — recorded in round 153 (Kilo 7.6.2, `zai/glm-4.5-flash`)

Real `kilo serve` spawned by the round's own path (`cli._spawn_overlay`), a stub ticket whose first
turn changes nothing (so the runner sends a rework prompt), the remembered window seeded in
`context-memory.json` (98 777 → `KILO_CONFIG_CONTENT` carries `limit {context 130777, input 98777,
output 32000}`), and a smaller record appended right after the first prompt so the second prompt
pushes a window the overlay already set. All twelve entries and the base were run this way, three at a
time, on the same model.

| | base (151) | landed (a2fe341) |
|---|---|---|
| `GET /provider?directory=` limit before the PATCH | `{context 130777, input 98777, output 32000}` | the same |
| the PATCH | 200, `.kilo/kilo.jsonc` written, instance reloaded | the same |
| `GET /provider?directory=` limit after the PATCH | `{context 130777, input 98777, output 32000}` — unchanged | the same — unchanged |
| the runner's line | `Kilo now sizes zai/glm-4.5-flash = 40,000 … the in-turn watch stays off` | `Kilo kept 130,777 from KILO_CONFIG_CONTENT — the watch stays armed (the read-back reports 130,777 for zai/glm-4.5-flash where the patch asked for 72,000)` |
| who stopped the turn | nobody — the watch stood down on the 200 | the watch: `context 32,037 tokens = 80.1% of 40,000 (remembered) inside the turn — stopping it to compact before the next prompt`, while Kilo's own window was 98 777 and the provider had not refused |

A model with no limit in the overlay (empty memory at spawn, 85 000 pushed on the second prompt): the
PATCH takes, the read-back matches, and the runner says `Kilo now sizes zai/glm-4.5-flash = 85,000
(compact at 80 % of it, after 85,000 tokens) — was 131,072` — the same line on base and on every entry
but `glm-4.7-flash` (its read-back method does not exist: it reported `kept` for a window that took).
