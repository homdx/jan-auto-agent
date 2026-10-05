# 179 — `revive_agents` raises `AttributeError` on an agent whose `agent` field is a bare string or `null`

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 167 — legacy or hand-edited state.json only
**Severity:** LOW
**File:** scripts/revive_round.py
**Symbol:** revive_agents, main
**Round:** 179
**Size:** XS
**Also touches:** tools/arena/rounds.py (`run_rerun`), tests/test_arena_run_rerun.py

## The bug

`revive_agents` reads the name with `agent.get("agent", {}).get("name", "?")`. That works only when `agent["agent"]` is a dict. `tools/arena/rounds._agent_name` documents that older `state.json` files hold the spec as a **bare string** ("`state.json` holds its spec as a dict, older files a bare string"), and a spec can also be `null`. Both crash:

```
>>> from scripts.revive_round import revive_agents
>>> revive_agents([{"agent": "opus", "state": "STALLED"}])
AttributeError: 'str' object has no attribute 'get'
>>> revive_agents([{"agent": None, "state": "STALLED"}])
AttributeError: 'NoneType' object has no attribute 'get'
```

`arena run rerun` is the worst case: `_rerun_refusal` and the `before` table use `_agent_name` (string-tolerant) and pass, and only then `run_rerun` calls `revive_agents` — a traceback after every refusal was supposedly checked, which contradicts the module's promise that "every refusal comes before anything is written" and "never a traceback". `revive_round.main` has the same line twice (the print loop), and `data["agents"]` being a non-dict/list (`[]`, a string) raises `TypeError` that the `except (OSError, ValueError, KeyError)` does not catch.

## Review (second pass)

Narrower than first written. The current runner always writes `agent` as a dict (`AgentSpec(**data["agent"])` in `runner.py` reads it back that way, and a bare string would already break `--resume`), so a string/`null` spec comes only from an older or hand-edited `state.json`. The crash is real and reproduced, but it is a legacy-file path, not something a normal round produces; `rounds._agent_name` was written for exactly that legacy case and `revive_agents` is the one function that was not given the same tolerance.

## Fix

- One `agent_name(entry)` helper in `scripts/revive_round.py` (dict → `name`, str → itself, anything else → `"?"`), used by `revive_agents`, the print loop in `main`, and imported by `tools/arena/rounds._agent_name` so the two cannot drift again.
- `main` catches `TypeError`/`AttributeError` on a malformed `agents` (not a list of dicts) and prints the usual one-line `unreadable` message, exit 1.
- Non-dict entries in `agents` are skipped, as `rounds.run_rerun` already does.

## Tests

1. `revive_agents` on a bare-string, `null` and missing `agent` field revives by state and names them `opus` / `?` / `?`; `only="opus"` picks the string-spec agent.
2. `main` on `{"agents": "x"}` and `[]` exits 1 with one line, no traceback.
3. `run_rerun --agent opus` on a state with a bare-string agent reaches the child (or `--dry-run` prints `1 to revive`).

## Acceptance

```bash
python3 -m pytest tests/test_arena_run_rerun.py -n 4 -q
```
