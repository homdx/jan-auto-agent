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
     A forbidden entry that holds the worktree (the rounds folder) does not
     claim the worktree itself (KC-46).
     ``doom_loop`` is rejected outright, and a ``bash`` command that matches
     ``deny_commands`` is rejected too. No model is called on this branch.
  2. ``Policy._ask_gate`` — one call to the gate model, a *different* model
     than the agent's (KC-2's ``[contest] gate_llm_profile``), which reads
     the command, the paths, the ticket and the recent tool calls and answers
     one JSON verdict.
   3. KC-55: a transport failure is not a verdict. A 429, a 402, a 5xx, a
      timeout and a dropped connection are retried by
      ``tools.llm_stream.request_completion``'s own loop, with the wait budget
      of ``[contest] gate_retries`` / ``gate_retry_wait_sec`` /
      ``gate_retry_max_wait_sec``; a reply that carries no verdict is asked
      once more after ``GATE_RETRY_WAIT``; and every wait stops no later than
      ``gate_deadline_sec`` in, because the gate runs synchronously inside
      ``KiloClient.wait_idle``'s loop and every second it spends counts against
      the round's silence clock. A refusal the endpoint never retries — a 401,
      a 403, a 404 — still fails at once.
   4. Fail closed. An exhausted budget, an unparseable reply, a deadline, or an
      exception from the transport becomes a ``reject`` whose ``reason`` the
      agent can read — never an exception out of ``decide``, so a broken gate
      stops the tool call and not the round.

The default ``completion_fn`` is ``tools.llm_stream.request_completion`` with
the URL, headers and payload that ``tools.llm_stream.build_chat_request``
builds from ``ContestConfig.gate_settings``, exactly the way Gate 1's
presence check builds its own call: ``response_format`` when the endpoint
supports it, ``temperature`` and ``max_tokens`` from the settings,
``stream=False`` and a 60 s timeout. KC-55 passes the retry budget, the
deadline-checked sleep and an ``on_retry`` that keeps the last message with it.
Nothing in this module opens a connection on its own; the tests stub
``completion_fn``.

Payload shapes are the ones PROBE.md recorded live on Kilo 7.6.2 — including
the two places a ``bash`` event keeps command text instead of a path, which
is why ``_pathlike`` refuses to treat shell syntax as a file name.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
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
    "GATE_PROBE_MESSAGE",
    "GATE_RETRIES",
    "GATE_RETRY_WAIT",
    "GATE_SYSTEM_PROMPT",
    "GATE_TIMEOUT",
    "HARD_DENYLIST",
    "LAYERS",
    "MAX_REASON_CHARS",
    "NULL_DEVICES",
    "REPLIES",
    "Decision",
    "Policy",
    "PolicyContext",
    "gate_error_name",
    "gate_verdict",
    "gate_worst_case_sec",
]

#: The gate is the expensive second model; it gets one shot per permission.
GATE_TIMEOUT = 60.0

#: KC-55: a reply that carries neither ``allow`` nor ``reject`` — the empty body
#: included — is asked once more, after ``GATE_RETRY_WAIT``. ``request_completion``
#: already retried that call's own transport errors, so this is the one extra
#: call the gate spends on a body it could not parse; the last attempt's verdict
#: stands, and a clean verdict is never re-asked.
GATE_RETRIES = 1
GATE_RETRY_WAIT = 2.0

#: KC-55 §5: the probe intake sends the gate once before the round starts — the
#: same request shape as a real ask, with one attempt and no retries, so a dead
#: key or a model that is not on offer is refused at intake instead of after 30
#: minutes of ``gate-failed`` rejects.
GATE_PROBE_MESSAGE = "permission: probe\ncommand: true"

#: KC-55 §5: intake's probe spends one attempt only — a 429 is transient and is
#: exactly what the real ask retries, so a probe that burned the budget would
#: hide the very thing it is there to notice.
_GATE_PROBE_RETRIES = 0

#: KC-55 §1: the gate's transport budget. Absent or unreadable values degrade to
#: these, so a policy built without a roster still fails fast like KC-3's gate.
_GATE_RETRY_DEFAULTS = (3, 10.0, 60.0, 600.0)

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

#: KC-55: the sentence a gate-failed reason ends with after a 429 or a 5xx, for
#: the agent that is about to read it — the reviewer is overloaded, and a
#: command inside its own worktree never needed one in the first place.
_GATE_OVERLOAD_HINT = (
    " — the reviewer is overloaded; a command inside your worktree needs no reviewer"
)

#: KC-55: the status of ``HTTP 429 from https://…``, the RuntimeError
#: ``request_completion`` raises. The reason names the status only: never the
#: URL, never the body, never a key.
_GATE_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")

#: KC-55: the error name of ``TimeoutError calling https://…`` or
#: ``ValueError reading response body from …`` — ``request_completion`` wraps a
#: dropped connection or a garbled body in a RuntimeError, so the name comes
#: out of the text rather than from ``type(exc).__name__``.
_GATE_NETWORK_RE = re.compile(r"^(\w+Error) (?:calling|reading)\b")


class _GateDeadline(Exception):
    """KC-55: one whole gate decision has used up ``gate_deadline_sec``.

    Subclasses ``Exception`` directly, not ``RuntimeError``, ``OSError`` or
    ``ValueError`` — neither ``except`` clause in ``request_completion``
    swallows it: ``_open`` catches ``HTTPError`` and the network errors, and the
    read loop catches those plus ``ValueError``. It must reach ``_ask_gate``,
    which turns it into a ``gate-failed`` reject instead of one more wait.
    """


def _gate_transport_name(text: str, fallback: str = "RuntimeError") -> str:
    """``HTTP 429``, ``TimeoutError``, … out of one transport failure's text.

    The status wins over the error class, because a 429 is the fact an operator
    can act on and ``RuntimeError`` is the wrapper that hides it. ``fallback``
    is the exception's own class name: what today's gate already puts in the
    reason when neither pattern matches.
    """
    text = text or ""
    match = _GATE_STATUS_RE.search(text)
    if match:
        return f"HTTP {match.group(1)}"
    match = _GATE_NETWORK_RE.search(text)
    if match:
        return match.group(1)
    return fallback


def gate_error_name(exc: BaseException) -> str:
    """KC-55: the name of a failed gate call, for a reason and for intake's probe."""
    return _gate_transport_name(str(exc), type(exc).__name__)


def gate_verdict(reply) -> str:
    """KC-55: ``allow``, ``reject`` or ``""`` — the intake probe's only question.

    The probe does not read the gate's reasoning; it only needs to know that the
    endpoint answered something it could parse.
    """
    verdict, _reason = _extract_verdict(reply)
    return verdict


def _gate_failed_reason(name: str, attempts: int, elapsed: float,
                        deadline: float = 0.0, deadline_hit: bool = False) -> str:
    """The gate-failed reason: what failed, how hard the gate tried, how long it took.

    One attempt reads exactly as today's reason, ``gate unavailable: <name>`` —
    the status only. More attempts add the count and the total wall time, and a
    decision that ran into its deadline names that too, after the error it was
    last waiting on. A 429 or a 5xx ends with the overload hint.
    """
    reason = f"gate unavailable: {name}"
    if attempts > 1:
        detail = f" ({attempts} attempts, {elapsed:.1f} s"
        if deadline_hit:
            detail += f", deadline {deadline:.0f} s"
        reason += detail + ")"
    if name == "HTTP 429" or name.startswith("HTTP 5"):
        reason += _GATE_OVERLOAD_HINT
    return reason


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

#: The devices a command writes to or reads from without touching a place
#: (KC-28): ``2>/dev/null`` is the commonest suffix a model puts on a probe,
#: and a gate call to ask whether writing nothing to nowhere is safe is a call
#: the gate can lose. Matched against the token as written and as resolved
#: (``/dev/stdout`` is a symlink into ``/proc``). Only these four: ``/dev/shm``
#: and the block devices are places the gate should see.
NULL_DEVICES: frozenset = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


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
    the rounds folder (every other worktree, this round's and earlier ones'),
    the repo's own ``.git`` and the hard denylist; an entry that is the
    worktree or one of its ancestors never forbids a path inside the worktree
    (KC-46). ``recent_tools`` are the last few ``tool`` parts of the session
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
    #: KC-55: the transport attempts and re-asks behind this decision — the
    #: ``_sleep_fn`` calls plus one, since one of those counts the retry budget
    #: and the other the re-ask of an unparsable reply. ``1`` is today's
    #: fail-fast gate, and ``record`` then writes no ``gate_attempts`` key, so
    #: the one-attempt records stay byte-identical.
    gate_attempts: int = 1

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


def gate_worst_case_sec(config: "ContestConfig | None") -> float:
    """KC-55: the longest one gate decision may take, in seconds.

    ``gate_deadline_sec`` bounds the waits, but the call in flight when the
    deadline trips still gets its own ``GATE_TIMEOUT`` — that is the one sum a
    formula over the retry counts cannot give: in ``request_completion`` the
    read loop reopens the connection on every pass, and ``_open`` has its own
    attempt counter, so one call can make up to ``(gate_retries + 1)**2``
    requests. Intake compares this with ``idle_event_timeout_sec`` and the
    tests read the same number; neither writes the sum itself.

    Fail open: no config, or a deadline that is not a number, gives the roster's
    own defaults rather than an exception.
    """
    deadline = getattr(config, "gate_deadline_sec", _GATE_RETRY_DEFAULTS[3])
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        deadline = _GATE_RETRY_DEFAULTS[3]
    return float(deadline) + GATE_TIMEOUT


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


#: One token of a ``bash`` command: a quoted string kept whole (so
#: ``"/tmp/my dir/f"`` is one token, not a path and a bare word), else a
#: maximal run up to whitespace or a shell operator — ``>``, ``>>``, ``<``,
#: ``|``, ``&&``, ``;``, ``(`` and ``)`` each end a token, so a redirect
#: target or the second command of a pipeline starts fresh.
_CMD_TOKEN = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s<>|&;()"\']+')


def _command_paths(command) -> list:
    """The path-shaped tokens of a ``bash`` command, quotes stripped (KC-13).

    Kilo names the *first* outside directory it detects in ``patterns``; a
    redirect target, a second argument or a ``cd`` elsewhere on the same line
    is only in ``metadata.command``. Every token that ``_pathlike`` accepts
    (``/…``, ``~…``, ``./…``, ``../…``) is returned in command order; a bare
    word (``reboot``), ``2>&1``, ``$HOME/x`` and a URL are not paths and are
    dropped. String work only: no shell is started. Fail-open — a non-string
    command or one holding a NUL yields ``[]``.

    KC-52: a token that is exactly ``/`` — quoted or not — is the division
    operator, not the filesystem root, and is dropped. The scan tokenises the
    whole command, heredoc body included, so ``x = tmp_path / "logs"``,
    ``expr 6 / 3`` and ``a / b`` in ``awk`` or ``bc`` each produced a ``/``
    token; ``_pathlike`` accepts it because it starts with ``/``, it resolves
    to ``/``, and ``/`` is the exact-only entry of ``HARD_DENYLIST`` — a
    mechanical, gate-free reject of a harmless inline script. Only the lone
    slash goes: ``/etc``, ``/tmp/x``, ``//x`` and ``/*`` are still paths.
    ``ls /`` is now a no-path ask; ``deny_commands`` is matched against the
    raw command text, not this list, so it is unaffected; and an
    ``external_directory`` pattern of ``/`` is never scanned here.
    """
    if not isinstance(command, str) or "\x00" in command:
        return []
    paths = []
    for token in _CMD_TOKEN.findall(command):
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        if not token or token == "/":
            continue
        if _pathlike(token):
            paths.append(token)
    return paths


def _extract_paths(props: dict, base: "Path | None" = None) -> list:
    """The event's paths as ``(resolved, (original, ...))`` pairs.

    From ``properties.patterns`` and ``properties.metadata.directories`` /
    ``.patterns``, a trailing ``/*`` stripped and the remainder resolved —
    symlinks resolved, so a link out of the worktree is judged by its target.
    De-duplicated by the *resolved* path: one event names the same path in
    two spellings (``patterns: ["/tmp/*"]`` and
    ``metadata.directories: ["/tmp"]`` in PROBE.md), and both spellings are
    kept for the glob match, so neither one fails on its own.

    For a ``bash`` permission ``metadata.command`` is scanned too (KC-13):
    every path token of the command joins the same list, so a command that
    touches two outside places is judged by both, not by the one Kilo chose
    to report. A leading ``~`` is expanded for every source, so ``~/.ssh/x``
    meets the hard denylist instead of resolving under the caller's cwd.

    KC-51: a target that is *not* absolute after ``~`` expansion — a ``./…``
    or ``../…`` token from a command — is joined to *base* before it is
    resolved, so it resolves against the agent's worktree, the directory the
    command starts in, and not against the runner process's cwd. A ``cd`` on
    the same line is not followed: the path is still judged against *base*,
    and the ``cd`` target itself is a ``/…`` or ``~…`` token that is judged
    on its own. ``external_directory`` patterns arrive absolute and are
    unaffected. With no *base* (a direct caller, an old test) behaviour is
    today's: the target resolves against the caller's cwd. A token naming one
    of ``NULL_DEVICES`` is dropped (KC-28).
    """
    meta = _metadata(props)
    raw = list(_as_list(props.get("patterns")))
    raw.extend(_as_list(meta.get("directories")))
    raw.extend(_as_list(meta.get("patterns")))
    if _as_str(props.get("permission")) == "bash":
        raw.extend(_command_paths(meta.get("command")))

    originals: dict = {}
    order: list = []
    for item in raw:
        original = _as_str(item)
        if not original or not _pathlike(original):
            continue
        # KC-52 follow-up: ``/*`` strips to the root, not to the empty
        # string — "" is not absolute, so it was joined to *base* (KC-51)
        # and ``rm -rf /*`` was judged "inside worktree", auto-approved
        # before ``deny_commands`` was ever consulted.
        target = original.removesuffix("/*") or "/"
        if target.startswith("~"):
            # ``~/.ssh/x`` must land on the home denylist, not under the cwd
            target = os.path.expanduser(target)
        if base is not None and not Path(target).is_absolute():
            # KC-51: a relative command token resolves against the worktree,
            # the directory the command starts in — never the runner's cwd.
            target = str(Path(base) / target)
        resolved = _resolve(target)
        if resolved is None:
            continue
        if target in NULL_DEVICES or str(resolved) in NULL_DEVICES:
            # KC-28: a null device is not a place — a command whose only
            # path is ``/dev/null`` is a no-path ask, settled by layer 1
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


def _forbidden_match(path: Path, forbidden, worktree: "Path | None" = None) -> "Path | None":
    """The first forbidden entry *path* is equal to or under, or ``None``.

    KC-46: with *worktree*, a *path* inside it is not claimed by an entry that
    is the worktree or one of its ancestors. The runner forbids the whole
    rounds folder, and the agent's own worktree is in it. An entry *inside*
    the worktree (a ``.git``, a ``.ssh``) still wins over the worktree.
    """
    own = worktree is not None and _inside(path, worktree)
    for entry in _as_list(forbidden):
        if not isinstance(entry, Path):
            continue
        if own and _inside(worktree, entry):
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


def _reply_text(reply) -> str:
    """The gate's reply as text: for ``gate_raw`` and the reason's quote.

    Fail open — a reply that is neither a string nor something ``json.dumps``
    can serialise is still a reply, not an exception out of the policy.
    """
    if isinstance(reply, str):
        return reply
    if reply is None:
        return ""
    try:
        return json.dumps(reply, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(reply)


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
    deterministic under test, and ``sleep`` replaces ``time.sleep`` the same
    way, so a test never waits the gate's retry backoff for real.

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
        sleep: "Callable | None" = None,
    ) -> None:
        self._config = config if isinstance(config, ContestConfig) else None
        self._completion_fn = completion_fn if completion_fn is not None else self._default_completion
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep

    # ── the one external call ──────────────────────────────────────────────

    @property
    def _settings(self) -> "LlmSettings | None":
        """The gate's ``LlmSettings``, or ``None`` when there is no gate."""
        config = self._config
        if config is None:
            return None
        settings = getattr(config, "gate_settings", None)
        return settings if isinstance(settings, LlmSettings) else None

    def _gate_limits(self) -> tuple:
        """KC-55: ``(retries, wait_sec, max_wait_sec, deadline_sec)``.

        The four ``[contest] gate_*`` keys, each degrading to its own default
        when absent, unreadable or negative — so a policy built on a config
        that is not the roster's still fails fast instead of raising, and no
        config can cap the gate's wait budget below zero.
        """
        config = self._config if isinstance(self._config, ContestConfig) else ContestConfig()

        def value(key: str, default: float, whole: bool = False) -> float:
            try:
                number = int(getattr(config, key, default)) if whole else float(
                    getattr(config, key, default))
            except (TypeError, ValueError):
                return default
            return default if number < 0 else number

        return (
            value("gate_retries", _GATE_RETRY_DEFAULTS[0], whole=True),
            value("gate_retry_wait_sec", _GATE_RETRY_DEFAULTS[1]),
            value("gate_retry_max_wait_sec", _GATE_RETRY_DEFAULTS[2]),
            value("gate_deadline_sec", _GATE_RETRY_DEFAULTS[3]),
        )

    def _default_completion(self, url, headers, payload, timeout, *, stream=False,
                            api_format="openai", ssl_context=None,
                            error_retries: int = 0, error_retry_wait_sec: float = 0.0,
                            max_retry_after_sec: float = 0.0, _sleep_fn=None,
                            on_retry=None):
        """The default ``completion_fn``: ``request_completion``, unmodified.

        KC-3's gate passed no retries — fail fast, so a rate limit cost one
        second and a verdict. KC-55 lets it retry: the keyword defaults stay
        fail-fast for a caller that names none, and ``_ask_gate`` names all
        five, the deadline-checked sleep included.
        """
        return request_completion(
            url, headers, payload, timeout,
            stream=stream, api_format=api_format, ssl_context=ssl_context,
            error_retries=error_retries, error_retry_wait_sec=error_retry_wait_sec,
            max_retry_after_sec=max_retry_after_sec, _sleep_fn=_sleep_fn,
            on_retry=on_retry,
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
        ``bash`` command matching ``deny_commands``; a ``bash`` ask with no
        path outside the worktree/tmp_roots; otherwise ``None``.
        """
        permission = _as_str(props.get("permission"))
        if permission == "doom_loop":
            return Decision("reject", "mechanical", "doom loop")

        worktree = _resolve(ctx.worktree)
        pairs = _extract_paths(props, worktree)
        for resolved, originals in pairs:
            entry = _forbidden_match(resolved, ctx.forbidden, worktree)
            if entry is not None:
                return Decision(
                    "reject", "mechanical",
                    f"forbidden: {', '.join(originals)} is at or under {entry}",
                )

        if _inside_worktree_or_tmp(pairs, worktree, self._tmp_roots(ctx)):
            return Decision("once", "mechanical", "inside worktree/tmp_roots")

        if permission == "bash":
            pattern = _deny_match(_as_str(_metadata(props).get("command")),
                                  self._deny_commands())
            if pattern is not None:
                return Decision(
                    "reject", "mechanical",
                    f"deny_commands match: {pattern}",
                )
            if not pairs:
                return Decision(
                    "once", "mechanical",
                    "bash: no path outside worktree/tmp_roots",
                )
        return None

    # ── layer 2 ────────────────────────────────────────────────────────────

    def _ssl_context(self):
        """The gate's ssl context: ``None`` unless its profile disables verify."""
        settings = self._settings
        if settings is None:
            return None
        return None if getattr(settings, "verify_ssl", True) else make_unverified_context()

    def _gate_request(self, user_msg: str) -> "tuple | None":
        """``(url, headers, payload, api_format)`` for one gate call.

        ``None`` when the round has no gate model. One place both the real ask
        and intake's probe build their call, so the two cannot drift: the same
        ``build_chat_request`` arguments, the same prompt, the same timeout.
        """
        settings = self._settings
        if settings is None:
            return None
        api_format = _as_str(settings.api_format) or "openai"
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
        return url, headers, payload, api_format

    def probe_gate(self, user_msg: str = GATE_PROBE_MESSAGE) -> str:
        """KC-55 §5: one call to the gate, one attempt, no retries.

        Intake sends it before the round starts, so a refused key or a model
        the endpoint does not offer fails there with a line naming
        ``contest.local.ini`` instead of after minutes of ``gate-failed``
        rejects. One attempt on purpose: a 429 is transient and is exactly
        what a real ask retries, so a probe that burned the whole budget would
        hide the thing it is there to notice. It is not one of any agent's
        ``gate_max_calls_per_session`` calls — it happens before a session
        exists.

        Returns the reply text, which intake does not parse; raises whatever
        the transport raises, since intake decides what a status means.
        """
        request = self._gate_request(user_msg)
        if request is None:
            return ""
        url, headers, payload, api_format = request
        return self._completion_fn(
            url, headers, payload, GATE_TIMEOUT,
            stream=False, api_format=api_format,
            ssl_context=self._ssl_context(),
            error_retries=_GATE_PROBE_RETRIES,
            error_retry_wait_sec=0.0,
            max_retry_after_sec=0.0,
            _sleep_fn=None,
            on_retry=None,
        )

    def _ask_gate(self, props: dict, ctx: PolicyContext) -> Decision:
        """The gate model's verdict, or a ``reject`` that names the budget.

        KC-55: the one call carries the gate's own transport retries — a 429 or
        a dropped connection is retried rather than answered ``reject`` — and a
        reply without a verdict is asked once more, after ``GATE_RETRY_WAIT``.
        Every wait goes through one deadline check, because the gate runs
        synchronously inside ``KiloClient.wait_idle``'s loop: while it waits
        the session produces no events, so every second counts against the
        round's silence clock. ``gate_elapsed`` is the total wall time across
        all attempts and waits; ``gate_attempts`` is the ``_sleep_fn`` calls
        plus one, never the ``on_retry`` calls, since the quota-reset path
        reports without waiting.
        """
        budget = ctx.gate_budget_left
        budget = budget if isinstance(budget, (int, float)) else 0
        if budget <= 0:
            return Decision(
                "reject", "budget",
                f"gate budget exhausted ({int(budget)})",
            )

        request = self._gate_request(_gate_user_message(props, ctx, self._tmp_roots(ctx)))
        if request is None:
            return Decision(
                "reject", "gate-failed",
                "gate unavailable: no gate model configured",
            )
        url, headers, payload, api_format = request

        retries, retry_wait, max_wait, deadline = self._gate_limits()
        start = self._clock()
        waits: list = [0]
        last: list = [None]
        permission = _as_str(props.get("permission")) or "permission"

        def wait(seconds: float) -> None:
            """One wait, checked against the deadline before it is spent."""
            if deadline > 0 and self._clock() - start + seconds > deadline:
                raise _GateDeadline(f"{deadline:.0f} s")
            waits[0] += 1
            self._sleep(seconds)

        def remember(message: str) -> None:
            """The last transport message: what the reason names when it ends."""
            last[0] = _gate_transport_name(message)

        def call() -> str:
            """The one call, with the gate's retry budget and this deadline."""
            return self._completion_fn(
                url, headers, payload, GATE_TIMEOUT,
                stream=False, api_format=api_format,
                ssl_context=self._ssl_context(),
                error_retries=retries,
                error_retry_wait_sec=retry_wait,
                max_retry_after_sec=max_wait,
                _sleep_fn=wait,
                on_retry=remember,
            )

        attempts = 0
        text = ""
        try:
            for ask in range(GATE_RETRIES + 1):
                reply = call()
                attempts = waits[0] + 1
                text = _reply_text(reply)
                verdict, reason = _extract_verdict(reply)
                if verdict in ("allow", "reject"):
                    # a clean verdict is never re-asked, in either direction
                    elapsed = float(self._clock() - start)
                    if verdict == "allow":
                        return Decision("once", "gate",
                                        f"gate: {reason or 'allowed'}", elapsed, text, attempts)
                    return Decision("reject", "gate",
                                    f"gate: {reason or 'rejected'}", elapsed, text, attempts)
                if ask < GATE_RETRIES:
                    # the same request once more; the last attempt's verdict stands
                    wait(GATE_RETRY_WAIT)
            elapsed = float(self._clock() - start)
            quote = text.strip()[:_GATE_FAIL_QUOTE] or "empty reply"
            return Decision(
                "reject", "gate-failed",
                f"gate unavailable: {quote} ({attempts} attempts)",
                elapsed, text, attempts,
            )
        except _GateDeadline as exc:
            attempts = waits[0] + 1
            elapsed = float(self._clock() - start)
            logger.warning(
                "Policy._ask_gate [%s] failed closed: deadline %s (%d attempt(s))",
                permission, exc, attempts,
            )
            return Decision(
                "reject", "gate-failed",
                _gate_failed_reason(last[0] or "deadline", attempts, elapsed,
                                    deadline, True),
                elapsed, None, attempts,
            )
        except Exception as exc:  # noqa: BLE001 — the gate must not sink a round
            attempts = waits[0] + 1
            elapsed = float(self._clock() - start)
            logger.warning(
                "Policy._ask_gate [%s] failed closed: %s: %s (%d attempt(s))",
                permission, type(exc).__name__, exc, attempts,
            )
            return Decision(
                "reject", "gate-failed",
                _gate_failed_reason(_gate_transport_name(str(exc), type(exc).__name__),
                                    attempts, elapsed),
                elapsed, None, attempts,
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
            attempts = getattr(decision, "gate_attempts", 1) if decision_ok else 1
            if isinstance(attempts, (int, float)) and not isinstance(attempts, bool) \
                    and attempts > 1:
                # KC-55: how hard the gate tried. Absent for one attempt, so a
                # record of today's fail-fast gate stays byte-identical.
                entry["gate_attempts"] = int(attempts)
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 — a log line is not a round
            logger.warning("Policy.record could not write %s: %s: %s",
                           path, type(exc).__name__, exc)
