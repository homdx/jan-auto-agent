"""tools/contest/policy.py — KC-3: the contest's safety gate.

Kilo raises one ``permission.asked`` event for every tool call the session
rules did not settle on their own (``docs/kilo-contest/PROBE.md``). KC-1's
``KiloClient.reply_permission`` can only answer ``once`` or ``reject`` — this
module never replies "always", because under ``external_directory`` that
whitelists the pattern for the rest of the session (PROBE.md, §Facts 4). It
decides which of the two, in two layers, so that no permission is ever
answered by silence:

  1. ``Policy._mechanical`` — geometry, free. Every path the event names is
     resolved (a symlink out of the worktree is judged by its target) and
     compared with ``forbidden``, then with the worktree and ``tmp_roots``.
     ``doom_loop`` is rejected outright, and a ``bash`` command that matches
     ``deny_commands`` is rejected too. No model is called on this branch.
  2. ``Policy._ask_gate`` — one call to the gate model, a *different* model
     than the agent's (KC-2's ``[contest] gate_llm_profile``), which reads
     the command, the paths, the ticket and the recent tool calls and answers
     one JSON verdict.
  3. Fail closed. An exhausted budget, an unparseable reply or an exception
     from the transport becomes a ``reject`` whose ``reason`` the agent can
     read — never an exception out of ``decide``, so a broken gate stops the
     tool call and not the round.

The default ``completion_fn`` is ``tools.llm_stream.request_completion`` with
the URL, headers and payload that ``tools.llm_stream.build_chat_request``
builds from ``ContestConfig.gate_settings``, exactly the way Gate 1's
presence check builds its own call: ``response_format`` when the endpoint
supports it, ``temperature`` and ``max_tokens`` from the settings,
``stream=False``, a 60 s timeout and ``error_retries=0``. Nothing in this
module opens a connection on its own; the tests stub ``completion_fn``.

Payload shapes are the ones PROBE.md recorded live on Kilo 7.6.2 — including
the two places a ``bash`` event keeps command text instead of a path, which
is why ``_pathlike`` refuses to treat shell syntax as a file name.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from tools.auto.llm_profile import LlmSettings
from tools.contest.roster import ContestConfig
from tools.llm_stream import (
    build_chat_request,
    make_unverified_context,
    request_completion,
    strip_json_fence,
    strip_think,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DECISION_KEYS",
    "GATE_SYSTEM_PROMPT",
    "GATE_TIMEOUT",
    "HARD_DENYLIST",
    "LAYERS",
    "MAX_REASON_CHARS",
    "REPLIES",
    "Decision",
    "Policy",
    "PolicyContext",
]

#: The gate is the expensive second model; it gets one shot per permission.
GATE_TIMEOUT = 60.0

#: The only two replies a Decision may carry. ``Literal`` keeps the type
#: narrow; ``Decision.__post_init__`` keeps it narrow at run time too.
REPLIES: tuple[str, ...] = ("once", "reject")
LAYERS: tuple[str, ...] = ("mechanical", "gate", "gate-failed", "budget")

#: A reason is sent to the agent as the tool error and written to
#: decisions.jsonl — one line, so it stays readable in both places.
MAX_REASON_CHARS = 200

#: The keys ``Policy.record`` writes, in order, to decisions.jsonl.
DECISION_KEYS: tuple[str, ...] = (
    "t",
    "sessionID",
    "permission_id",
    "permission",
    "patterns",
    "command",
    "layer",
    "reply",
    "reason",
    "gate_elapsed",
    "gate_model",
)

#: How much of the gate's reply a gate-failed reason may quote.
_GATE_FAIL_QUOTE = 80

#: The paths no tool call may ever touch, from the ticket's denylist. The
#: round's other worktrees and the repo's own ``.git`` are added by KC-4
#: through ``PolicyContext.forbidden``.
_ROOT = Path("/")
_HOME: "Path | None"


def _home_dir() -> "Path | None":
    """``Path.home()`` or ``None`` when HOME is unset (rootless containers)."""
    try:
        return Path.home()
    except RuntimeError:
        return None


_HOME = _home_dir()


def _resolve(candidate) -> "Path | None":
    """``Path(candidate).resolve()``, or ``None`` instead of an exception."""
    try:
        return Path(candidate).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _hard_denylist() -> tuple:
    candidates = [_ROOT, _HOME]
    if _HOME is not None:
        candidates += [_HOME / ".ssh", _HOME / ".config",
                       _HOME / ".local" / "share" / "kilo"]
    out = []
    for entry in candidates:
        resolved = _resolve(entry)
        if resolved is not None:
            out.append(resolved)
    return tuple(out)


HARD_DENYLIST: tuple = _hard_denylist()


def _exact_only(path: Path) -> bool:
    """True for the two entries that may only ever name themselves.

    ``/`` is an ancestor of every absolute path, and ``$HOME`` is an ancestor
    of this repository and therefore of every worktree the round creates
    (``rounds_dir`` is ``../rounds``, one level above the checkout). Treating
    either as a denylist ancestor would reject every path, including every
    legitimate worktree path, so both match on identity alone. The rest of
    the denylist — ``~/.ssh``, ``~/.config``, ``~/.local/share/kilo``, the
    round's other worktrees, the repo's ``.git`` — still forbids everything
    under it.
    """
    return path == _ROOT or path == _HOME


GATE_SYSTEM_PROMPT = """\
You are the safety gate for a coding contest. One coding agent is about to
run one tool call. Decide whether that single call may run, and nothing else.

The agent lives in one worktree. Anything inside it is fine. Anything
elsewhere needs a reason you can state in one line.

ALLOW a call outside the worktree only for these two reasons:
  1. The target is a scratch location the operator allowed (tmp_roots), and
     the command only reads, writes or deletes inside it.
  2. The call is read-only and looks at a path the ticket names.

REFUSE, without exception:
  * any write, delete or move into another agent's worktree, or into this
    repository's own .git directory;
  * anything under the operator's home dotfiles (~/.ssh, ~/.config, ~/.local);
  * package installs and source grabs (pip, npm, cargo, brew, apt, git clone,
    git checkout, git fetch);
  * a network fetch piped into a shell (curl ... | sh, wget ... | sh);
  * git push, git push --force, git reset --hard, git clean -f, git rebase;
  * rm, chmod, chown, kill, reboot, shutdown or any other destructive command
    whose target is outside the worktree;
  * anything you cannot read and understand in full, or whose effect on this
    machine you cannot describe in one sentence.

Reply with one JSON object and nothing else:
  {"verdict": "allow" or "reject", "reason": "one line"}

When unsure, reject — a rejection costs the agent one retry; an allow can
cost the machine.
"""


# ─────────────────────────────────────────────────────────────────────────────
# the two records
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyContext:
    """One session, one turn: what the policy may look at.

    ``worktree`` is the session directory — resolved, because everything
    inside it is free and everything outside it is a permission event we
    answer (PROBE.md, §"The boundary is the session directory"). ``tmp_roots``
    are the scratch globs ``contest.ini`` allows outside it. ``forbidden`` is
    the round's other worktrees, the repo's own ``.git`` and the hard
    denylist. ``recent_tools`` are the last few ``tool`` parts of the session
    (KC-1's ``tool_parts``), so the gate can see what the agent has been
    doing rather than one call out of context. ``gate_budget_left`` is the
    runner's counter — the only state this policy carries.
    """

    worktree: Path
    tmp_roots: tuple = ()
    forbidden: tuple = field(default_factory=lambda: HARD_DENYLIST)
    ticket_title: str = ""
    ticket_files: tuple = ()
    recent_tools: tuple = ()
    gate_budget_left: int = 20


@dataclass(frozen=True)
class Decision:
    """One answer: what to reply, which layer decided it, and why.

    ``reply`` is ``once`` (let this call run, nothing more) or ``reject`` —
    the only two ``KiloClient.reply_permission`` accepts. ``layer`` says
    whether geometry, the gate model, a broken gate or an empty budget
    produced it, so decisions.jsonl shows which of the three answered.
    ``gate_elapsed`` and ``gate_raw`` are ``None`` when the gate never ran.
    """

    reply: str
    layer: str
    reason: str = ""
    gate_elapsed: "float | None" = None
    gate_raw: "str | None" = None

    def __post_init__(self) -> None:
        one_line = " ".join(str(self.reason or "").split())
        object.__setattr__(self, "reason", one_line[:MAX_REASON_CHARS])
        if self.reply not in REPLIES:
            raise ValueError(
                f"Decision.reply must be one of {REPLIES}, got {self.reply!r} — "
                "an 'always' reply would whitelist the pattern for the whole session"
            )
        if self.layer not in LAYERS:
            raise ValueError(
                f"Decision.layer must be one of {LAYERS}, got {self.layer!r}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# reading the event
# ─────────────────────────────────────────────────────────────────────────────

def _as_list(value) -> list:
    """A list or tuple as a list; anything else as ``[]``."""
    return list(value) if isinstance(value, (list, tuple)) else []


def _as_str(value) -> str:
    """A stripped string, or ``""`` — never an exception on malformed input."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _metadata(props: dict) -> dict:
    meta = props.get("metadata")
    return meta if isinstance(meta, dict) else {}


#: Characters a glob from ``properties.patterns`` may carry and still name a
#: path. Anything with shell syntax in it is a command: PROBE.md §Facts 2
#: records that a ``bash: ask`` event carries the command text in
#: ``patterns`` — and in ``always`` — not a file name. Resolving such a
#: string would land under the caller's cwd and read as "inside the
#: worktree", which is exactly backwards, so it is judged by
#: ``deny_commands`` instead.
_NOT_A_PATH = (" ", "\t", "\n", "|", ">", "<", "&", ";", "`")


def _pathlike(text: str) -> bool:
    """True only for a value that *names* a path, not a bare command word.

    A single-token destructive command (``reboot``, ``shutdown``) has none
    of the ``_NOT_A_PATH`` shell-syntax characters, so the old space/pipe
    check alone let it through: ``Path("reboot").resolve()`` lands under
    this process's cwd, reads as "inside the worktree", and the mechanical
    layer auto-approves a reboot without ever consulting ``deny_commands``
    or the gate. A real path is always absolute or explicitly relative
    (``/...``, ``~...``, ``./...``, ``../...``); anything else — including
    every bare command word — is judged as a command instead.
    """
    if any(ch in text for ch in _NOT_A_PATH):
        return False
    return text.startswith(("/", "~", "./", "../"))


def _extract_paths(props: dict) -> list:
    """The event's paths as ``(resolved, (original, ...))`` pairs.

    From ``properties.patterns`` and ``properties.metadata.directories`` /
    ``.patterns``, a trailing ``/*`` stripped and the remainder resolved —
    symlinks resolved, so a link out of the worktree is judged by its target.
    De-duplicated by the *resolved* path: one event names the same path in
    two spellings (``patterns: ["/tmp/*"]`` and
    ``metadata.directories: ["/tmp"]`` in PROBE.md), and both spellings are
    kept for the glob match, so neither one fails on its own.
    """
    meta = _metadata(props)
    raw = list(_as_list(props.get("patterns")))
    raw.extend(_as_list(meta.get("directories")))
    raw.extend(_as_list(meta.get("patterns")))

    originals: dict = {}
    order: list = []
    for item in raw:
        original = _as_str(item)
        if not original or not _pathlike(original):
            continue
        target = original.removesuffix("/*")
        resolved = _resolve(target)
        if resolved is None:
            continue
        if resolved not in originals:
            originals[resolved] = []
            order.append(resolved)
        if original not in originals[resolved]:
            originals[resolved].append(original)
    return [(resolved, tuple(originals[resolved])) for resolved in order]


def _inside(path: Path, root: Path) -> bool:
    """True when *path* equals *root* or is one of its descendants."""
    return path == root or root in path.parents


def _forbidden_match(path: Path, forbidden) -> "Path | None":
    """The first forbidden entry *path* is equal to or under, or ``None``."""
    for entry in _as_list(forbidden):
        if not isinstance(entry, Path):
            continue
        if path == entry:
            return entry
        if not _exact_only(entry) and _inside(path, entry):
            return entry
    return None


def _inside_worktree_or_tmp(pairs: list, worktree: "Path | None", tmp_roots) -> bool:
    """True when every path the event names is a path the agent may use.

    ``worktree`` by containment, ``tmp_roots`` by glob — matched against the
    resolved path and against every original spelling of it, so
    ``/tmp/kilo/*`` claims ``/tmp/kilo/scratch`` and ``/tmp/kilo/scratch/*``
    alike.
    """
    if not pairs:
        return False
    roots = [root for root in _as_list(tmp_roots) if isinstance(root, str) and root]
    for resolved, originals in pairs:
        if worktree is not None and _inside(resolved, worktree):
            continue
        if any(
            fnmatch.fnmatch(str(resolved), glob)
            or fnmatch.fnmatch(original, glob)
            for glob in roots
            for original in originals
        ):
            continue
        return False
    return True


def _deny_match(command: str, deny_commands) -> "str | None":
    """The first ``deny_commands`` pattern matching *command*, or ``None``."""
    if not command:
        return None
    for pattern in _as_list(deny_commands):
        if isinstance(pattern, str) and pattern and fnmatch.fnmatch(command, pattern):
            return pattern
    return None


# ─────────────────────────────────────────────────────────────────────────────
# the gate's reply
# ─────────────────────────────────────────────────────────────────────────────

def _extract_verdict(reply) -> "tuple[str, str]":
    """``(verdict, reason)`` from a gate reply — ``('', '')`` when there is
    no JSON object in it.

    ``strip_think`` first, then markdown fences, then the first ``{…}``
    object in whatever is left: the same tolerant extraction Gate 1 uses for
    its presence verdict, since the same gateways send the same shapes.
    """
    if isinstance(reply, dict):
        data = reply
    elif reply is None:
        return "", ""
    else:
        cleaned = strip_think(str(reply))
        cleaned = strip_json_fence(cleaned.strip()) if cleaned else ""
        if not cleaned:
            return "", ""
        data = None
        try:
            candidate = json.loads(cleaned, strict=False)
            if isinstance(candidate, dict):
                data = candidate
        except (json.JSONDecodeError, ValueError):
            data = None
        if data is None:
            start = cleaned.find("{")
            if start >= 0:
                try:
                    candidate, _ = json.JSONDecoder(strict=False).raw_decode(
                        cleaned[start:]
                    )
                except (json.JSONDecodeError, ValueError):
                    candidate = None
                if isinstance(candidate, dict):
                    data = candidate
        if data is None:
            return "", ""

    verdict = _as_str(data.get("verdict")).lower()
    if verdict not in ("allow", "reject"):
        return "", _as_str(data.get("reason"))
    return verdict, _as_str(data.get("reason"))


def _tool_lines(recent_tools) -> list:
    """The recent ``tool`` parts as ``name: input -> status`` lines."""
    lines: list = []
    for part in _as_list(recent_tools):
        if not isinstance(part, dict):
            continue
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        name = _as_str(part.get("tool")) or "tool"
        payload = state.get("input")
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False, default=str)
        lines.append(f"{name}: {payload} -> {_as_str(state.get('status')) or 'unknown'}")
    return lines


def _gate_user_message(props: dict, ctx: PolicyContext, tmp_roots: tuple = ()) -> str:
    """The one user message the gate reads: every labelled line, nothing else."""
    meta = _metadata(props)
    worktree = _resolve(ctx.worktree)
    roots = [root for root in _as_list(tmp_roots) if isinstance(root, str) and root]
    files = [item for item in _as_list(ctx.ticket_files) if _as_str(item)]
    lines = [
        f"permission: {_as_str(props.get('permission')) or 'unknown'}",
        f"patterns: {', '.join(_as_str(p) for p in _as_list(props.get('patterns')) if _as_str(p)) or '-'}",
        f"directories: {', '.join(_as_str(p) for p in _as_list(meta.get('directories')) if _as_str(p)) or '-'}",
        f"command: {_as_str(meta.get('command')) or '-'}",
        f"description: {_as_str(meta.get('description')) or '-'}",
        f"worktree: {worktree if worktree is not None else ctx.worktree}",
        f"tmp_roots: {', '.join(roots) or '-'}",
        f"ticket: {_as_str(ctx.ticket_title) or '-'}",
        f"ticket files: {', '.join(files) or '-'}",
    ]
    tools = _tool_lines(ctx.recent_tools)
    lines.append("recent tools:" + (" none" if not tools else ""))
    lines.extend(f"  {line}" for line in tools)
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# the policy
# ─────────────────────────────────────────────────────────────────────────────

class Policy:
    """Decides every ``permission.asked`` event of a contest session.

    Constructed with the round's ``ContestConfig`` (KC-2), which supplies
    ``deny_commands`` and the gate's own ``LlmSettings``. ``completion_fn``
    replaces the one HTTP call — tests pass a stub and never touch a
    provider; ``clock`` replaces ``time.monotonic`` so ``gate_elapsed`` is
    deterministic under test.

    :meth:`decide` never raises: any failure — a broken event, a malformed
    config, a dying transport — degrades to a ``reject`` the agent can read,
    so the round keeps running.
    """

    def __init__(
        self,
        config: "ContestConfig | None",
        *,
        completion_fn: "Callable | None" = None,
        clock: "Callable | None" = None,
    ) -> None:
        self._config = config if isinstance(config, ContestConfig) else None
        self._completion_fn = completion_fn if completion_fn is not None else self._default_completion
        self._clock = clock if clock is not None else time.monotonic

    # ── the one external call ──────────────────────────────────────────────

    @property
    def _settings(self) -> "LlmSettings | None":
        """The gate's ``LlmSettings``, or ``None`` when there is no gate."""
        config = self._config
        if config is None:
            return None
        settings = getattr(config, "gate_settings", None)
        return settings if isinstance(settings, LlmSettings) else None

    def _default_completion(self, url, headers, payload, timeout, *,
                            stream=False, api_format="openai",
                            ssl_context=None, error_retries=0):
        """The default ``completion_fn``: ``request_completion``, fail fast."""
        return request_completion(
            url, headers, payload, timeout,
            stream=stream, api_format=api_format,
            ssl_context=ssl_context, error_retries=error_retries,
        )

    def _tmp_roots(self, ctx: PolicyContext) -> tuple:
        """The scratch globs in force: ``ctx.tmp_roots``, else ``contest.ini``.

        The runner builds one context per session; when it does not repeat the
        round's ``tmp_roots`` there, the config value applies rather than an
        empty tuple — an empty default must not silently narrow what the
        round allows.
        """
        roots = tuple(
            root for root in _as_list(ctx.tmp_roots)
            if isinstance(root, str) and root
        )
        if roots:
            return roots
        config = self._config
        return tuple(config.tmp_roots or ()) if config is not None else ()

    def _deny_commands(self) -> tuple:
        config = self._config
        return tuple(config.deny_commands or ()) if config is not None else ()

    # ── layer 1 ────────────────────────────────────────────────────────────

    def _mechanical(self, props: dict, ctx: PolicyContext) -> "Decision | None":
        """Geometry alone: a Decision, or ``None`` when it cannot decide.

        In order: ``doom_loop``; any path at or under a forbidden entry;
        every path inside the worktree or under a ``tmp_roots`` glob; a
        ``bash`` command matching ``deny_commands``.
        """
        permission = _as_str(props.get("permission"))
        if permission == "doom_loop":
            return Decision("reject", "mechanical", "doom loop")

        pairs = _extract_paths(props)
        for resolved, originals in pairs:
            entry = _forbidden_match(resolved, ctx.forbidden)
            if entry is not None:
                return Decision(
                    "reject", "mechanical",
                    f"forbidden: {', '.join(originals)} is at or under {entry}",
                )

        if _inside_worktree_or_tmp(pairs, _resolve(ctx.worktree), self._tmp_roots(ctx)):
            return Decision("once", "mechanical", "inside worktree/tmp_roots")

        if permission == "bash":
            pattern = _deny_match(_as_str(_metadata(props).get("command")),
                                  self._deny_commands())
            if pattern is not None:
                return Decision(
                    "reject", "mechanical",
                    f"deny_commands match: {pattern}",
                )
        return None

    # ── layer 2 ────────────────────────────────────────────────────────────

    def _ask_gate(self, props: dict, ctx: PolicyContext) -> Decision:
        """One call to the gate model, or a ``reject`` that names the budget."""
        budget = ctx.gate_budget_left
        budget = budget if isinstance(budget, (int, float)) else 0
        if budget <= 0:
            return Decision(
                "reject", "budget",
                f"gate budget exhausted ({int(budget)})",
            )

        settings = self._settings
        if settings is None:
            return Decision(
                "reject", "gate-failed",
                "gate unavailable: no gate model configured",
            )

        api_format = _as_str(settings.api_format) or "openai"
        user_msg = _gate_user_message(props, ctx, self._tmp_roots(ctx))
        url, headers, payload = build_chat_request(
            base_url=settings.base_url,
            api_key=settings.api_key,
            model=settings.model,
            api_format=api_format,
            temperature=float(settings.temperature),
            max_tokens=int(settings.max_tokens),
            system=GATE_SYSTEM_PROMPT,
            user_msg=user_msg,
            num_ctx=int(settings.num_ctx or 0),
            think=bool(settings.think),
            response_format=bool(settings.response_format),
            think_effort=(settings.think_effort
                          if getattr(settings, "think_effort_enabled", False)
                          else None),
            stream=False,
        )

        start = self._clock()
        try:
            reply = self._completion_fn(
                url, headers, payload, GATE_TIMEOUT,
                stream=False, api_format=api_format,
                ssl_context=None if getattr(settings, "verify_ssl", True)
                else make_unverified_context(),
                error_retries=0,
            )
        except Exception as exc:  # noqa: BLE001 — the gate must not sink a round
            elapsed = float(self._clock() - start)
            logger.warning(
                "Policy._ask_gate [%s] failed closed: %s: %s",
                _as_str(props.get("permission")) or "permission",
                type(exc).__name__, exc,
            )
            return Decision(
                "reject", "gate-failed",
                f"gate unavailable: {type(exc).__name__}", elapsed, None,
            )
        elapsed = float(self._clock() - start)

        text = reply if isinstance(reply, str) else ""
        if not isinstance(reply, str) and reply is not None:
            text = json.dumps(reply, ensure_ascii=False, default=str)
        verdict, reason = _extract_verdict(reply)
        if verdict == "allow":
            return Decision("once", "gate", f"gate: {reason or 'allowed'}", elapsed, text)
        if verdict == "reject":
            return Decision("reject", "gate", f"gate: {reason or 'rejected'}", elapsed, text)
        return Decision(
            "reject", "gate-failed",
            f"gate unavailable: {text.strip()[:_GATE_FAIL_QUOTE] or 'empty reply'}",
            elapsed, text,
        )

    # ── the entry point ────────────────────────────────────────────────────

    def decide(self, event: dict, ctx: PolicyContext) -> Decision:
        """Answer one ``permission.asked`` event. Never raises, never silences.

        Layer 1 when geometry decides it, layer 2 otherwise, and a
        ``gate-failed`` reject when the event or the context is broken.
        """
        try:
            if not isinstance(event, dict):
                return Decision(
                    "reject", "gate-failed",
                    "gate unavailable: event is not a mapping",
                )
            props = event.get("properties")
            if not isinstance(props, dict):
                props = {}
            mechanical = self._mechanical(props, ctx)
            if mechanical is not None:
                return mechanical
            return self._ask_gate(props, ctx)
        except Exception as exc:  # noqa: BLE001 — see the class docstring
            logger.warning("Policy.decide failed closed: %s: %s",
                           type(exc).__name__, exc)
            return Decision(
                "reject", "gate-failed",
                f"gate unavailable: {type(exc).__name__}",
            )

    # ── the audit trail ────────────────────────────────────────────────────

    def record(self, decision: Decision, event: dict, path) -> None:
        """Append one JSON line per decision to *path* (decisions.jsonl).

        Called by the runner (KC-6); the policy does not know about the
        runner. A broken artifact — a missing directory, a bad event, a
        closed file — is logged and swallowed, never raised into a run.
        """
        try:
            props = event.get("properties") if isinstance(event, dict) else None
            props = props if isinstance(props, dict) else {}
            meta = _metadata(props)
            settings = self._settings
            decision_ok = isinstance(decision, Decision)
            entry = {
                "t": time.time(),
                "sessionID": _as_str(props.get("sessionID")),
                "permission_id": _as_str(props.get("id")),
                "permission": _as_str(props.get("permission")),
                "patterns": _as_list(props.get("patterns")),
                "command": _as_str(meta.get("command")),
                "layer": decision.layer if decision_ok else "",
                "reply": decision.reply if decision_ok else "",
                "reason": decision.reason if decision_ok else "",
                "gate_elapsed": (decision.gate_elapsed if decision_ok else None),
                "gate_model": settings.model if settings is not None else "",
            }
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 — a log line is not a round
            logger.warning("Policy.record could not write %s: %s: %s",
                           path, type(exc).__name__, exc)
