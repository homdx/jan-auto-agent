# KC-34 — `tools/contest/backend.py`: abstract `KiloClient` behind a `ContestBackend` protocol so non-kilo providers (OpenRouter, plain OpenAI-compatible) can run in the same round

**Status:** open  
**Severity:** HIGH  
**File:** `tools/contest/backend.py` (new), `tools/contest/kilo_client.py`,
`tools/contest/runner.py`, `tools/contest/roster.py`, `tools/contest/cli.py`,
`contest.ini`  
**Symbol:** `ContestBackend`, `KiloBackend`, `OpenRouterBackend`,
`ContestConfig.backend`, `run_round`, `run_agent`, `agents_from_models`  
**Round:** 73  
**Size:** M  
**Source:** `tools/contest/cli.py:122` — `agents_from_models` already accepts
a `provider: str = "kenary"` parameter, but its only caller, `_apply_flags`
(cli.py:292, `agents_from_models(args.models)`), never passes it — so
`"kenary"` is the only value ever reachable today, and every
`AgentSpec.provider_id` flows from there into
`KiloClient.create_session(provider_id=…)`. `KiloClient` talks to a Kilo
server; a Kilo server routes to kenary (and any other provider Kilo knows).
There is no path to run an agent against OpenRouter, a direct OpenAI-compatible
endpoint, or any other non-kilo backend — even though `tools.llm_stream`
already handles an OpenAI-compatible endpoint (which covers OpenRouter) and
Ollama via `api_format`.
Operators with openrouter keys and no kilo binary cannot run a round at all.  
**Depends on:** KC-6 (`run_agent`, `run_round`, landed `e8c6ad3`),
KC-16 (`cli.py` `cmd_run` / `agents_from_models`, landed `1304950`).  
**Also touches:** `tests/test_contest_backend.py` (new),
`tests/test_contest_runner.py` (extend), `tests/_kilo_fake.py` (extend with
a minimal OpenRouter fake)

---

## What happens today

`run_round` receives a `KiloServer` and creates one `KiloClient` per agent:

```python
# runner.py:839
client = KiloClient(server, str(run.workspace.path))
tap    = EventTap(server.base_url, str(run.workspace.path), ...)
```

`KiloClient.create_session` POSTs to `/session`; `KiloClient.prompt` POSTs to
`/session/{id}/prompt_async`; `EventTap` SSE-streams `/events`. Every step
assumes a Kilo HTTP server. No abstraction exists. Adding openrouter means
forking `runner.py`.

## What must change

### 1. `tools/contest/backend.py` — the protocol

```python
from __future__ import annotations
from typing import Callable, Protocol, runtime_checkable

@runtime_checkable
class ContestBackend(Protocol):
    """One agent session on one backend.

    A backend owns the full lifecycle of one session: create, prompt,
    wait for the model to go idle, abort on stall.  It does NOT own the
    worktree or the harvest — those stay in runner.py.

    Every method raises `ContestBackendError` on a non-retryable failure;
    retryable failures set `error["data"]["isRetryable"] = True` in the
    dict passed to the runner's existing `_retryable()` check (KC-19).
    """

    def create_session(self, provider_id: str, model_id: str, *,
                       rules: list, title: str,
                       agent: str | None = None) -> "SessionRef": ...

    def prompt(self, session: "SessionRef", text: str) -> None: ...

    def abort(self, session: "SessionRef") -> None: ...

    # CORRECTION (review pass): the real `KiloClient.wait_idle` is
    # `(self, tap, session, timeout, *, idle_event_timeout=None,
    # on_permission, on_question)` — `on_permission`/`on_question` are
    # REQUIRED callables with no defaults, and there is no `max_questions`
    # concept anywhere in the codebase today (`max_questions_per_turn` is a
    # *round* config value the runner's own `on_question` closure counts
    # against and turns into a `stall()`, not something a backend primitive
    # takes as an argument). The protocol below matches what `run_agent`
    # actually calls it with; see "Open question" below for what
    # `OpenRouterBackend` does with these two callbacks.
    def wait_idle(self, session: "SessionRef", timeout: float, *,
                  idle_event_timeout: float | None = None,
                  on_permission: Callable[[dict], tuple],
                  on_question: Callable[[dict], None]) -> "IdleResult": ...

    # CORRECTION (review pass): `run_agent`'s `on_permission` closure calls
    # `client.tool_parts(session)` (runner.py:516) to build the recent-tools
    # context a policy decision reads — this has no home in the protocol as
    # first drafted. Added here rather than dropped, so `KiloBackend`
    # delegates it as-is and `OpenRouterBackend` has an explicit (even if
    # trivial, e.g. `[]`) obligation to satisfy instead of a silent gap.
    def tool_parts(self, session: "SessionRef") -> list: ...

    def close(self) -> None: ...


class ContestBackendError(Exception):
    """Non-retryable backend failure (mirrors KiloHttpError's role)."""
```

`SessionRef`, `IdleResult` stay in `kilo_client.py` — they are plain
dataclasses with no kilo dependency.

### 2. `KiloBackend` — wraps existing `KiloClient` + `EventTap`

```python
# tools/contest/backend.py

class KiloBackend:
    """ContestBackend backed by a Kilo server (today's default)."""

    def __init__(self, server: KiloServer, directory: str) -> None:
        self._client = KiloClient(server, directory)
        self._tap    = EventTap(server.base_url, directory, ...)

    # each method delegates to self._client / self._tap
    def create_session(self, ...): return self._client.create_session(...)
    def prompt(self, ...):         self._client.prompt(...)
    def abort(self, ...):          self._client.abort(...)
    def wait_idle(self, ...):      return self._client.wait_idle(self._tap, ...)
    def tool_parts(self, ...):     return self._client.tool_parts(...)

    def close(self) -> None:
        # CORRECTION (review pass): NOT a no-op. `run_round`'s own `work()`
        # closure (runner.py:845-855) currently calls `tap.stop()` and
        # `tap.join(2.0)` directly in its `finally` block, per agent, after
        # every run — that per-agent cleanup has to move somewhere once
        # `work()` no longer holds a bare `tap`, and `close()` is where it
        # belongs. `server.close()` (the Kilo *server* process) stays
        # round-level and unrelated to this.
        self._tap.stop()
        self._tap.join(2.0)
```

`run_round`'s `work()` closure also calls `_wait_for_stream(tap)`
(runner.py:846) *before* the first prompt — a readiness wait, polling
`tap._socket`, that has no protocol equivalent above and no obvious one for
a subprocess-based backend. Either the protocol needs a `wait_ready()`
(a no-op for `OpenRouterBackend`, since a freshly-spawned subprocess has
nothing to race), or `run_round` keeps this check Kilo-specific via
`isinstance(backend, KiloBackend)` — pick one before implementing; the
draft above does neither.

`run_agent`'s signature changes from

```python
def run_agent(run, *, client: KiloClient, tap: EventTap, ...)
```

to

```python
def run_agent(run, *, backend: ContestBackend, ...)
```

All internal calls replace `client.X(tap, session, …)` with
`backend.X(session, …)`. `run_round` passes `KiloBackend(server, ws.path)`
for each agent — the kilo path is byte-identical in behaviour.

### 3. `OpenRouterBackend` — direct OpenAI-compatible streaming

```python
# tools/contest/backend.py

class OpenRouterBackend:
    """ContestBackend that drives a model via OpenRouter (or any OpenAI-
    compatible /chat/completions endpoint) inside a subprocess agent.

    The 'session' is a subprocess running the agent loop (tools/auto/
    inner_loop style) in the worktree; prompt() writes to its stdin;
    wait_idle() reads stdout until the agent emits the IDLE sentinel or
    the timeout fires; abort() sends SIGTERM.

    api_key, base_url, and model are the three fields from
    tools.auto.llm_profile (the same shape agents.ini uses).
    """

    def __init__(self, api_key: str, base_url: str,
                 directory: str, timeout: float = 30.0) -> None: ...
```

The agent subprocess is the existing `tools/auto/inner_loop.py` (or a
thin wrapper); the backend bridges stdin/stdout with the runner's existing
turn model. Worktree isolation and harvest are unchanged — only how the
model receives prompts and returns idle changes.

### 4. `roster.py` — `ContestConfig.backend` field

Add one key to `[contest]`:

```ini
backend = kilo   ; kilo (default) | openrouter
```

`ContestConfig` gains:

```python
backend: str = "kilo"   # "kilo" | "openrouter"
```

`load_roster` reads it like `kilo_bin`. Unknown values raise `RosterError`
at load time: `backend must be 'kilo' or 'openrouter', got 'foo'`.

ADDITION (review pass — see the correction under §5): resolving
`OpenRouterBackend`'s own credential needs a second `LlmSettings`, built
the same way `gate_settings` already is (`resolve_llm_profile`, an
`[contest_openrouter_llm]` section, `${CONTEST_OPENROUTER_API_KEY}`).
`ContestConfig` gains a matching field:

```python
openrouter_settings: LlmSettings | None = field(default_factory=lambda: None)
```

`_build` resolves it the same way `gate_settings` is resolved today
(roster.py:354-359), only required — and `RosterError` at load time — when
`backend == "openrouter"`.

### 5. `cli.py` — `--backend` flag and `agents_from_models` default

`_parser` / `run` subparser gains:

```python
run.add_argument(
    "--backend", default=None, choices=["kilo", "openrouter"],
    help="override the roster's backend (kilo | openrouter)",
)
```

CORRECTION (review pass): `agents_from_models` already has a `provider`
parameter (`provider: str = "kenary"`, cli.py:122) — nothing to add there.
The actual gap is `_apply_flags` (cli.py:289-299), which calls
`agents_from_models(args.models)` with no `provider=` at all today. Fix the
call site instead:

```python
def _apply_flags(config: ContestConfig, args: argparse.Namespace) -> ContestConfig:
    if args.models:
        provider = "openrouter" if config.backend == "openrouter" else "kenary"
        config = replace(config, agents=agents_from_models(args.models, provider=provider))
    ...
```

Callers that pass `--backend openrouter` without an explicit `provider/`
prefix get `openrouter/model:free` automatically.

`cmd_run` applies `_apply_flags(config, args)` which already handles
`--max-parallel`; add `backend` there:

```python
if args.backend:
    config = dataclasses.replace(config, backend=args.backend)
```

`cmd_run` then builds the backend:

CORRECTION (review pass): `config.gate_llm` doesn't exist — the field is
`config.gate_settings` (`ContestConfig.gate_settings: LlmSettings`,
roster.py:192, resolved from `[contest_gate_llm]`). More importantly,
`gate_settings` is the *safety gate's own* model profile (the second
opinion `policy.py` calls on a risky command) — it is not, and should not
be, the competing agent's own credential. Reusing it is a placeholder, not
a real answer, and the ticket needs to pick one before implementation:

- add a new resolved profile the same way the gate's is built —
  `openrouter_llm_profile` in `[contest]`, defaulting to a new
  `[contest_openrouter_llm]` section, `api_key = ${CONTEST_OPENROUTER_API_KEY}`,
  mirroring `[contest_gate_llm]` exactly (same `resolve_llm_profile` call,
  same `contest.local.ini` override path for the real key); or
- add `api_key`/`base_url` directly to `ContestConfig` as flat fields.

Either way `ContestConfig` needs a new field this ticket doesn't currently
declare — the sketch below uses the first option:

```python
if config.backend == "openrouter":
    # one OpenRouterBackend per agent, no KiloServer
    def _make_backend(ws):
        return OpenRouterBackend(
            api_key=config.openrouter_settings.api_key,
            base_url=config.openrouter_settings.base_url,
            directory=str(ws.path),
        )
else:
    server = _start_server(config, out_dir)
    def _make_backend(ws):
        return KiloBackend(server, str(ws.path))
```

`run_round` receives `make_backend: Callable[[Workspace], ContestBackend]`
instead of `server: KiloServer`.

### 6. `contest.ini` addition

```ini
[contest]
# ... existing keys ...
backend = kilo                  ; kilo (default) | openrouter
openrouter_llm_profile = contest_openrouter_llm   ; only read when backend = openrouter

[contest_openrouter_llm]
# same shape as [contest_gate_llm]; the real key goes in contest.local.ini
api_key  = ${CONTEST_OPENROUTER_API_KEY}
base_url = https://openrouter.ai/api/v1
model    =                      ; unused here — OpenRouterBackend gets its
                                 ; model id per-agent from AgentSpec, not
                                 ; from this section
```

## Open question — needs a decision before implementation

`on_permission`/`on_question` (see the corrected protocol in §1) are how
`policy.py`'s three-layer gate actually gets enforced today: every bash
command a Kilo session wants to run surfaces as a `permission.*.asked`
event, `run_agent`'s `on_permission` closure builds a `PolicyContext` from
it and calls `Policy.decide`, which is what `deny_commands`/`ask_commands`/
`tmp_roots`/the forbidden-sibling-worktree check actually enforce. That
whole mechanism is shaped around Kilo's own event stream.

`OpenRouterBackend`'s subprocess agent loop (`tools/auto/inner_loop.py`
style) has no Kilo-shaped permission-asked event — nothing in this ticket
says what it emits instead, or how (or whether) `Policy.decide` gets called
for a bash command that subprocess wants to run. Landing this as drafted
means either:

- `OpenRouterBackend` maps its own tool-call/shell-command moments onto the
  same `on_permission`/`on_question` callbacks (needs a defined event shape
  to hand `Policy.decide`, and `tool_parts` needs a real implementation,
  not just `[]`); or
- it runs with no gate at all, same as `--no-gate` today but not opt-in —
  worth stating explicitly, in the ticket and in the round's `_print_plan`
  output, if that's the accepted answer.

Pick one and update §1/§3 accordingly before this is implemented; the
Acceptance list below doesn't yet test either path.

## Acceptance

- [ ] `tests/test_contest_backend.py` (new):
  - `KiloBackend` satisfies `isinstance(b, ContestBackend)` at runtime
    (Protocol is `@runtime_checkable`);
  - `OpenRouterBackend` satisfies the same check;
  - `KiloBackend.create_session` delegates to the existing `KiloClient`
    (monkeypatched); a `KiloHttpError` becomes `ContestBackendError`;
  - `KiloBackend.tool_parts` delegates to `KiloClient.tool_parts`;
  - `KiloBackend.close` calls `tap.stop()` then `tap.join(2.0)` on its own
    tap (not a no-op — see the corrected §2);
  - `OpenRouterBackend.prompt` writes the text to the subprocess stdin;
    `wait_idle` returns `IdleResult(status="idle")` when the sentinel
    appears, `IdleResult(status="timeout")` on timeout, and calls
    `on_permission`/`on_question` per whatever the Open Question above
    resolves to (or documents that it never calls them, if the answer is
    "no gate on this backend").
- [ ] `tests/test_contest_runner.py` — extend:
  - `run_agent` called with a `KiloBackend` fake behaves byte-for-byte
    as today (all existing tests pass unmodified after the rename);
  - `run_agent` called with an `OpenRouterBackend` fake reaches `READY`
    on a clean commit;
  - `run_round`'s `work()` no longer references a bare `tap` — the
    pre-flight readiness wait and the per-agent `tap.stop()`/`tap.join()`
    move to whatever §2's `wait_ready()`-or-`isinstance` decision lands on.
- [ ] `tests/test_contest_roster.py`:
  - `backend = openrouter` parses; `backend = bad` raises `RosterError`;
  - default is `"kilo"`.
- [ ] `tests/test_contest_cli.py` — extend:
  - `--backend openrouter` sets `config.backend == "openrouter"`;
  - `agents_from_models("agnes-2-5-flash:free", provider="openrouter")`
    produces `provider_id="openrouter"`.
- [ ] Every existing test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.
- [ ] `python3 -m tools.contest run --ticket NN --backend openrouter --models agnes-2-5-flash:free` is a valid invocation (intake passes, `OpenRouterBackend` is constructed).

## Out of scope

- Ollama as a backend — the subprocess agent approach works there too but
  ollama's API is not OpenAI-compatible on all endpoints; a separate
  `OllamaBackend` is a follow-up.
- Multi-backend rounds (some agents on kilo, others on openrouter in one
  round) — `ContestConfig.backend` is one value for the whole round.
- Gate model on openrouter — the gate still calls `tools.auto.llm_profile`
  directly; only the competing agents change backend.
- Any change to `tools/contest/harvest.py`, `workspace.py`, or `gates.py` —
  those are backend-agnostic already. `policy.py` is NOT out of scope in
  the same sense: `Policy.decide` itself imports nothing Kilo-specific, but
  every call site is built around Kilo's `permission.*.asked` event shape —
  see the Open Question above. Leave `policy.py`'s own code untouched, but
  don't read "no change needed" as "no decision needed" for how
  `OpenRouterBackend` feeds it.

## Self-check before `append_task.py`

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "from tools.contest.backend import ContestBackend, KiloBackend, OpenRouterBackend"` clean.
- [ ] Exactly **one** commit; only the files listed in `**File:**` and the test files touched.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/backend.py` (new),
      `tools/contest/kilo_client.py`, `tools/contest/runner.py`,
      `tools/contest/roster.py`, `tools/contest/cli.py`, `contest.ini`,
      `tests/test_contest_backend.py` (new), `tests/test_contest_runner.py`,
      `tests/test_contest_roster.py`, `tests/test_contest_cli.py`
      (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.
- [ ] New tests red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` with the sha of the one commit.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
