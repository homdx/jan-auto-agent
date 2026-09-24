"""tests/test_contest_policy.py — KC-3: ``tools/contest/policy.py`` — three layers, no silence.

``KiloClient.reply_permission`` (KC-1) can only answer ``once`` or ``reject``,
and the probe (``scripts/kilo_hello.py``, commit 67e834d) answered every
permission with one flag. This module decides which of the two, by geometry
first and a second model second, so that an unattended round never answers a
permission by silence. Without the module every test below fails at import.

The mechanical cases are table-driven on the two payloads PROBE.md recorded
live on Kilo 7.6.2, verbatim — the ``external_directory`` event for
``/tmp/*`` and the ``bash`` event for ``rm -v /tmp/testfile``. Every gate
case stubs ``completion_fn``: no test here calls a provider or a kilo server.

The cases, in the ticket's acceptance order:

  1. ``external_directory`` ``/tmp/*`` with ``tmp_roots = /tmp/kilo/*`` →
     layer ``gate``; with ``tmp_roots = /tmp/*`` → ``once``, mechanical;
  2. a pattern inside the worktree → ``once``, mechanical, no gate call;
  3. a pattern under another round's worktree → ``reject``, mechanical;
  4. a symlink inside the worktree pointing out of it → ``reject``;
  5. ``doom_loop`` → ``reject``; the ``bash`` event with
     ``deny_commands = rm -v /tmp/*`` → ``reject``, naming the pattern;
  6. the gate: ``allow`` → ``once``/``gate``, ``reject`` → ``reject``/``gate``,
     a think-wrapped reply is honoured, prose without JSON / an unknown
     verdict / a raised ``TimeoutError`` → ``reject``/``gate-failed``, and
     an empty budget → ``reject``/``budget`` without any call;
  7. the user message carries the command, the worktree, the ticket title and
     a recent tool line; the default ``completion_fn`` sends the
     ``gate_settings`` temperature, max_tokens and response_format;
  8. ``"always"`` occurs once in the module — in its docstring;
  9. ``record`` writes one line per decision with the documented keys.
"""

from __future__ import annotations

import json
import sys
import urllib.error
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.auto.llm_profile import LlmSettings
from tools.contest import policy as policy_mod
from tools.contest.policy import (
    DECISION_KEYS,
    GATE_RETRIES,
    GATE_RETRY_WAIT,
    GATE_SYSTEM_PROMPT,
    GATE_TIMEOUT,
    HARD_DENYLIST,
    LAYERS,
    MAX_REASON_CHARS,
    NULL_DEVICES,
    REPLIES,
    Decision,
    Policy,
    PolicyContext,
    gate_worst_case_sec,
)
from tools.contest.roster import ContestConfig
import tools.llm_stream as llm_stream_mod

POLICY_FILE = REPO_ROOT / "tools" / "contest" / "policy.py"

# ─────────────────────────────────────────────────────────────────────────────
# the recorded payloads (PROBE.md, "The permission payloads, verbatim")
# ─────────────────────────────────────────────────────────────────────────────

EXTERNAL_DIRECTORY_EVENT = {
    "type": "permission.asked",
    "properties": {
        "id": "per_0afb2bf60001KFTnUBoT5JQbJe",
        "sessionID": "ses_f504d5ef4ffeAbHyyPaAMRYROL",
        "permission": "external_directory",
        "patterns": ["/tmp/*"],
        "metadata": {
            "command": "rm -v /tmp/testfile",
            "description": "Remove test file with verbose output",
            "directories": ["/tmp"],
            "patterns": ["/tmp/*"],
        },
        "always": ["/tmp/*"],
        "tool": {
            "messageID": "msg_0afb2a8f0001UeomCvVgJT2RDL",
            "callID": "call_chatcmpl-tool-7721747835df45abb0e8b79792ad1114",
        },
    },
}

BASH_EVENT = {
    "type": "permission.asked",
    "properties": {
        "id": "per_0afb32854001mmQjaA39GfX6Jf",
        "sessionID": "ses_f504d18f0ffehKDcvqpkphdcji",
        "permission": "bash",
        "patterns": ["rm -v /tmp/testfile"],
        "metadata": {
            "command": "rm -v /tmp/testfile",
            "description": "Remove test file at /tmp/testfile",
        },
        "always": ["rm *"],
        "tool": {
            "messageID": "msg_0afb3022c001E6ls3tt0MQ6Z4K",
            "callID": "call_chatcmpl-tool-af873cc4dfef4ad68913fdaa72d6b7de",
        },
    },
}

TICKET_TITLE = "42-kc3-policy — three layers, no silence"
TICKET_FILES = ("tools/contest/policy.py", "tests/test_contest_policy.py")
RECENT_TOOLS = (
    {
        "type": "tool",
        "tool": "bash",
        "state": {
            "status": "error",
            "input": {"command": "rm -v /tmp/testfile", "workdir": "/tmp"},
            "output": [{"type": "text", "text": "The user rejected permission"}],
        },
    },
)

ALLOW = {"verdict": "allow", "reason": "scratch path under tmp_roots"}
REJECT = {"verdict": "reject", "reason": "writes outside the worktree"}


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

_UNSET = object()


def make_config(*, tmp_roots=(), deny_commands=(), settings=_UNSET) -> ContestConfig:
    """A round: the gate settings from contest.ini, and the two policy inputs."""
    if settings is _UNSET:
        settings = LlmSettings(
            base_url="https://policy-test/v1",
            api_key="test-gate-key",
            model="policy/gate-model",
            api_format="openai",
            response_format=True,
            temperature=0.0,
            max_tokens=256,
        )
    return ContestConfig(
        tmp_roots=tmp_roots, deny_commands=deny_commands, gate_settings=settings,
    )


class StubGate:
    """A ``completion_fn`` stand-in: returns or raises *reply*, records calls."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, url, headers, payload, timeout, **kwargs):
        self.calls.append({
            "url": url,
            "headers": headers,
            "payload": payload,
            "timeout": timeout,
            **kwargs,
        })
        if isinstance(self.reply, BaseException):
            raise self.reply
        return self.reply

    def user_message(self, index: int = 0) -> str:
        (message,) = [
            part for part in self.calls[index]["payload"]["messages"]
            if part["role"] == "user"
        ]
        return message["content"]


class FakeClock:
    """A monotonic stand-in so ``gate_elapsed`` is deterministic under test."""

    def __init__(self, step: float = 0.5):
        self.now = 0.0
        self.step = step
        self.ticks = 0

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        self.ticks += 1
        return value


def NO_SLEEP(seconds: float) -> None:
    """The gate's re-ask wait, spent on nothing: no test sleeps wall time."""
    return None


def make_ctx(worktree: Path, **overrides) -> PolicyContext:
    defaults = {
        "worktree": worktree,
        "tmp_roots": (),
        "forbidden": HARD_DENYLIST,
        "ticket_title": TICKET_TITLE,
        "ticket_files": TICKET_FILES,
        "recent_tools": RECENT_TOOLS,
        "gate_budget_left": 20,
    }
    defaults.update(overrides)
    return PolicyContext(**defaults)


def make_event(*, permission="external_directory", patterns=(), directories=(),
               command="", description="", pid="per_test01",
               sid="ses_test01", metadata=True) -> dict:
    """A permission event in the shape PROBE.md recorded."""
    properties = {
        "id": pid,
        "sessionID": sid,
        "permission": permission,
        "patterns": list(patterns),
        "always": list(patterns),
        "tool": {"messageID": "msg_test", "callID": "call_test"},
    }
    if metadata:
        meta = {}
        if directories:
            meta["directories"] = list(directories)
        if command:
            meta["command"] = command
        if description:
            meta["description"] = description
        if meta:
            properties["metadata"] = meta
    return {"type": "permission.asked", "properties": properties}


def decide(policy: Policy, event: dict, worktree: Path, **overrides) -> Decision:
    return policy.decide(event, make_ctx(worktree, **overrides))


# ─────────────────────────────────────────────────────────────────────────────
# layer 1: geometry, on the recorded payloads
# ─────────────────────────────────────────────────────────────────────────────

def test_external_directory_outside_tmp_roots_asks_the_gate(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/kilo/*", "/tmp/contest/*")),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path)

    assert decision.reply == "once"
    assert decision.layer == "gate"
    assert decision.gate_elapsed == 0.5
    assert "Remove test file" not in decision.reason
    assert decision.gate_raw == json.dumps(ALLOW)
    assert len(gate.calls) == 1


def test_external_directory_inside_tmp_roots_is_mechanical(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert decision.reason == "inside worktree/tmp_roots"
    assert decision.gate_elapsed is None
    assert decision.gate_raw is None
    assert gate.calls == []


@pytest.mark.parametrize("pattern_suffix,roots", [
    ("/tmp/kilo/scratch", ("/tmp/kilo/*",)),
    ("/tmp/kilo/scratch/nested", ("/tmp/kilo/*",)),
    ("/tmp/kilo/scratch/*", ("/tmp/kilo/*",)),
    ("/tmp/contest/round-1", ("/tmp/kilo/*", "/tmp/contest/*")),
])
def test_tmp_roots_match_the_resolved_path_and_the_pattern(tmp_path,
                                                           pattern_suffix, roots):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=roots), completion_fn=gate, clock=FakeClock())
    event = make_event(patterns=[pattern_suffix], directories=[pattern_suffix])

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert gate.calls == []


def test_pattern_inside_the_worktree_is_mechanical_without_a_gate_call(tmp_path):
    worktree = (tmp_path / "rounds" / "r1" / "hy3").resolve()
    worktree.mkdir(parents=True)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(patterns=[str(worktree / "src" / "main.py")],
                       directories=[str(worktree / "src")])

    decision = decide(policy, event, worktree)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert decision.reason == "inside worktree/tmp_roots"
    assert gate.calls == []


def test_pattern_under_another_rounds_worktree_is_rejected(tmp_path):
    other = (tmp_path / "rounds" / "r1" / "laguna").resolve()
    other.mkdir(parents=True)
    worktree = (tmp_path / "rounds" / "r1" / "hy3").resolve()
    worktree.mkdir(parents=True)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(patterns=[str(other / "tools" / "contest" / "policy.py")])

    decision = decide(policy, event, worktree, forbidden=HARD_DENYLIST + (other,))

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert str(other) in decision.reason
    assert gate.calls == []


def test_symlink_out_of_the_worktree_is_judged_by_its_target(tmp_path):
    worktree = (tmp_path / "rounds" / "r1" / "hy3").resolve()
    worktree.mkdir(parents=True)
    target = (tmp_path / "ssh").resolve()
    target.mkdir()
    (worktree / "link").symlink_to(target)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, make_event(patterns=[str(worktree / "link")]),
                      worktree, forbidden=HARD_DENYLIST + (target,))

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert str(target) in decision.reason
    assert gate.calls == []


def test_doom_loop_is_rejected_outright(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(permission="doom_loop", patterns=[str(tmp_path / "ok.txt")])

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert decision.reason == "doom loop"
    assert decision.gate_elapsed is None
    assert gate.calls == []


def test_bash_command_matching_deny_commands_is_rejected_and_named(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(deny_commands=("git push*", "rm -v /tmp/*")),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, BASH_EVENT, tmp_path)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert "rm -v /tmp/*" in decision.reason
    assert "deny_commands" in decision.reason
    assert gate.calls == []


def test_bash_command_not_in_deny_commands_reaches_the_gate(tmp_path):
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(deny_commands=("git push*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, BASH_EVENT, tmp_path)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1


def test_forbidden_beats_the_worktree(tmp_path):
    """The denylist is checked before the worktree, so a forbidden path is not
    rescued by sitting inside it."""
    worktree = tmp_path.resolve()
    gated = (worktree / ".ssh").resolve()
    gated.mkdir()
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, make_event(patterns=[str(gated / "id_rsa")]),
                      worktree, forbidden=HARD_DENYLIST + (gated,))

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert str(gated) in decision.reason
    assert gate.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# reading the event
# ─────────────────────────────────────────────────────────────────────────────

def test_paths_are_extracted_deduped_and_trailing_star_stripped():
    pairs = policy_mod._extract_paths(EXTERNAL_DIRECTORY_EVENT["properties"])

    assert len(pairs) == 1
    resolved, originals = pairs[0]
    assert resolved == Path("/tmp")
    # both spellings the event uses for the same path, in the order read
    assert originals == ("/tmp/*", "/tmp")


def test_command_text_in_patterns_is_not_a_path():
    """PROBE.md §Facts 2: a ``bash: ask`` event keeps the command in
    ``patterns``; resolving it would land under the caller's cwd and read as
    "inside the worktree", which is backwards.  The patterns entry still
    yields nothing; the command's ``/tmp/testfile`` is the one pair."""
    assert policy_mod._extract_paths(BASH_EVENT["properties"]) == [
        (Path("/tmp/testfile"), ("/tmp/testfile",)),
    ]


def test_command_text_with_a_star_is_still_command_text():
    """The ``patterns`` entry ``rm -rf /tmp/*`` still yields nothing; the
    command's ``/tmp/*`` is read like a ``patterns`` glob — trailing ``/*``
    stripped, as ``_extract_paths`` already does."""
    assert policy_mod._extract_paths(
        {"permission": "bash", "patterns": ["rm -rf /tmp/*"],
         "metadata": {"command": "rm -rf /tmp/*"}}
    ) == [(Path("/tmp"), ("/tmp/*",))]


# ─────────────────────────────────────────────────────────────────────────────
# KC-13: command path scanning for bash permissions
# ─────────────────────────────────────────────────────────────────────────────

def test_command_scans_paths_outside_tmp_roots_to_the_gate(tmp_path):
    """Live event verbatim: patterns name /tmp/contest/notes/* but the
    command also redirects to /tmp/kc6-outside-x.txt — not under tmp_roots."""
    event = make_event(
        permission="bash",
        patterns=["/tmp/contest/notes/*"],
        command="mkdir -p /tmp/contest/notes && echo hi > /tmp/contest/notes/hy3.txt && echo hi > /tmp/kc6-outside-x.txt",
        description="create notes and outside file",
    )
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert decision.layer == "gate"
    assert decision.reply == "reject"
    assert "/tmp/kc6-outside-x.txt" in gate.user_message()


def test_command_with_both_targets_under_tmp_roots_is_mechanical(tmp_path):
    """Both paths in the command land under /tmp/contest/*."""
    event = make_event(
        permission="bash",
        patterns=["/tmp/contest/notes/*"],
        command="mkdir -p /tmp/contest/notes && echo hi > /tmp/contest/out.txt",
        description="create notes",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert gate.calls == []


def test_command_with_second_path_on_denylist_is_rejected(tmp_path):
    """A command whose second path is under HARD_DENYLIST is reject/mechanical."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="cat /tmp/ok.txt > ~/.ssh/x",
        description="read and write",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert decision.reply == "reject"
    assert decision.layer == "mechanical"
    assert "forbidden" in decision.reason
    assert gate.calls == []


def test_command_with_relative_and_absolute_path_is_once(tmp_path):
    """cat scripts/x.py > /tmp/contest/out.txt — relative path inside worktree,
    absolute second under tmp_roots."""
    worktree = (tmp_path / "rounds" / "r1" / "hy3").resolve()
    worktree.mkdir(parents=True)
    event = make_event(
        permission="bash",
        patterns=[],
        command="cat scripts/x.py > /tmp/contest/out.txt",
        description="copy file",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, worktree)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert gate.calls == []


def test_command_bare_words_and_shell_syntax_are_not_paths(tmp_path):
    """A bare command word, 2>&1, $HOME/x, a URL, and a quoted path with
    spaces do not become paths or crash. With KC-15, a bash command that
    names no path outside the worktree is mechanically allowed."""
    event = make_event(
        permission="bash",
        patterns=[],
        command='reboot 2>&1 $HOME/x https://example.com/p "path with spaces"',
        description="various tokens",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    pairs = policy_mod._extract_paths(event["properties"])
    assert pairs == []

    decision = decide(policy, event, tmp_path)
    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert "no path outside" in decision.reason
    assert gate.calls == []


def test_command_scan_skips_non_string_command(tmp_path):
    """A non-string command is silently skipped (fail-open)."""
    event = make_event(permission="bash", patterns=[], metadata=False)
    event["properties"]["metadata"] = {"command": 12345}
    pairs = policy_mod._extract_paths(event["properties"])
    assert pairs == []


def test_command_scan_skips_nul_in_token(tmp_path):
    """A path containing a NUL byte is skipped (fail-open)."""
    event = make_event(
        permission="bash", patterns=[],
        command="echo \x00/tmp/evil",
    )
    pairs = policy_mod._extract_paths(event["properties"])
    assert pairs == []


def test_command_scan_only_for_bash(tmp_path):
    """The command is NOT scanned when permission is not bash."""
    event = make_event(
        permission="external_directory",
        patterns=["/tmp/contest/*"],
        command="/tmp/outside.txt",
    )
    pairs = policy_mod._extract_paths(event["properties"])
    # only the pattern is read, not the command
    assert len(pairs) == 1
    assert pairs[0][1] == ("/tmp/contest/*",)


def test_command_scan_deduplicates_paths(tmp_path):
    """The same path in patterns and command appears only once."""
    event = make_event(
        permission="bash",
        patterns=["/tmp/contest/out.txt"],
        command="cat /tmp/contest/out.txt",
    )
    pairs = policy_mod._extract_paths(event["properties"])
    assert len(pairs) == 1
    resolved, originals = pairs[0]
    assert resolved == Path("/tmp/contest/out.txt")
    assert "/tmp/contest/out.txt" in originals


def test_root_and_home_match_on_identity_alone():
    """``/`` and ``$HOME`` are ancestors of every worktree, so they may only
    name themselves — otherwise every worktree path is forbidden."""
    assert policy_mod._forbidden_match(Path("/"), HARD_DENYLIST) == Path("/")
    assert policy_mod._forbidden_match(Path("/"), HARD_DENYLIST + (Path("/"),)) == Path("/")
    assert policy_mod._forbidden_match(Path.home(), HARD_DENYLIST) == Path.home()
    assert policy_mod._forbidden_match(
        Path.home() / ".ssh" / "id_rsa", HARD_DENYLIST
    ) == Path.home() / ".ssh"
    # the checkout — and every worktree under it — is under $HOME, yet allowed
    assert policy_mod._forbidden_match(REPO_ROOT, HARD_DENYLIST) is None
    assert policy_mod._forbidden_match(REPO_ROOT / "tests", HARD_DENYLIST) is None


# ─────────────────────────────────────────────────────────────────────────────
# KC-15: bash ask for redirect/tee/cp outside the worktree; no-path bash is free
# ─────────────────────────────────────────────────────────────────────────────

def test_live_redirect_outside_worktree_to_gate(tmp_path):
    """`echo x > /tmp/kc6-outside-a.txt` has no path in patterns but the
    command scan finds /tmp/kc6-outside-a.txt, which is not under tmp_roots."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="echo x > /tmp/kc6-outside-a.txt",
        description="write outside file via redirect",
    )
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert decision.layer == "gate"
    assert "/tmp/kc6-outside-a.txt" in gate.user_message()


def test_bash_no_path_outside_is_mechanical(tmp_path):
    """`pytest -q 2>&1 | tail -n 20` has no path outside the worktree."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="python3 -m pytest tests -q 2>&1 | tail -n 20",
        description="run tests",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert "no path outside" in decision.reason
    assert gate.calls == []


def test_bash_relative_redirect_is_mechanical(tmp_path):
    """`echo x > out.txt` — a relative redirect stays inside the worktree."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="echo x > out.txt",
        description="write relative file",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert "no path outside" in decision.reason
    assert gate.calls == []


def test_bash_cp_to_tmp_roots_is_mechanical(tmp_path):
    """`cp README.md /tmp/contest/notes/a.md` — target is under tmp_roots."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="cp README.md /tmp/contest/notes/a.md",
        description="copy to contest notes",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert gate.calls == []


def test_bash_cp_outside_tmp_roots_to_gate(tmp_path):
    """`cp README.md /tmp/elsewhere/a.md` — target is not under tmp_roots."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="cp README.md /tmp/elsewhere/a.md",
        description="copy elsewhere",
    )
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert decision.layer == "gate"
    assert "/tmp/elsewhere/a.md" in gate.user_message()


def test_bash_forbidden_redirect_is_mechanical_reject(tmp_path):
    """`cat x > ~/.ssh/authorized_keys` — target is on the hard denylist."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="cat x > ~/.ssh/authorized_keys",
        description="write to ssh dir",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert "forbidden" in decision.reason
    assert gate.calls == []


def test_bash_deny_beats_no_path_rule(tmp_path):
    """`git push origin HEAD` matches deny_commands even though it has no path."""
    event = make_event(
        permission="bash",
        patterns=[],
        command="git push origin HEAD",
        description="push to remote",
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(deny_commands=("git push*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert "deny_commands" in decision.reason
    assert "no path outside" not in decision.reason
    assert gate.calls == []


def test_external_directory_no_paths_still_gates(tmp_path):
    """An external_directory event with no paths still goes to the gate."""
    event = make_event(
        permission="external_directory",
        patterns=[],
    )
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/kilo/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1

@pytest.mark.parametrize(
    "reply,expected_reply,expected_reason_in",
    [
        (json.dumps(ALLOW), "once", "scratch path under tmp_roots"),
        (json.dumps(REJECT), "reject", "writes outside the worktree"),
        # the same verdict behind a reasoning block: strip_think first
        ("</think>wandering thoughts</think>" + json.dumps(ALLOW), "once", "scratch"),
        # and one buried in prose, which is where the tolerant extractor earns
        ("Sure — " + json.dumps(ALLOW) + " hope that's fine", "once", "scratch"),
        ('{"verdict": "ALLOW", "reason": "caps"}', "once", "caps"),
        ('{"verdict": "REJECT", "reason": "caps"}', "reject", "caps"),
        ('```json\n' + json.dumps(ALLOW) + '\n```', "once", "scratch"),
    ],
)
def test_gate_verdicts(tmp_path, reply, expected_reply, expected_reason_in):
    gate = StubGate(reply)
    clock = FakeClock(step=0.25)
    policy = Policy(make_config(), completion_fn=gate, clock=clock)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.reply == expected_reply
    assert decision.layer == "gate"
    assert expected_reason_in in decision.reason
    assert decision.reason.startswith("gate:")
    assert decision.gate_elapsed == 0.25
    assert decision.gate_raw == reply
    assert clock.ticks == 2
    assert len(gate.calls) == 1


@pytest.mark.parametrize("reply", [
    "I think this looks fine, go ahead and run it.",
    '{"verdict": "maybe", "reason": "not sure"}',
    '{"reason": "no verdict key at all"}',
    '{"verdict": 7, "reason": "not a string"}',
    "",
    None,
    "[]",
])
def test_gate_without_a_verdict_fails_closed(tmp_path, reply):
    gate = StubGate(reply)
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock(), sleep=NO_SLEEP)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.reply == "reject"
    assert decision.layer == "gate-failed"
    assert decision.reason.startswith("gate unavailable:")
    # KC-55: a reply without a verdict is asked once more, after GATE_RETRY_WAIT
    assert len(gate.calls) == 2
    assert decision.gate_attempts == 2


def test_gate_exception_fails_closed_and_names_the_class(tmp_path):
    gate = StubGate(TimeoutError("https://policy-test/v1 timed out"))
    clock = FakeClock(step=0.125)
    policy = Policy(make_config(), completion_fn=gate, clock=clock)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.reply == "reject"
    assert decision.layer == "gate-failed"
    assert decision.reason == "gate unavailable: TimeoutError"
    assert decision.gate_elapsed == 0.125
    assert decision.gate_raw is None


def test_gate_empty_reply_quotes_what_came_back(tmp_path):
    gate = StubGate("thinking hard about the answer but not emitting any json at all")
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock(), sleep=NO_SLEEP)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.layer == "gate-failed"
    assert decision.reason.startswith("gate unavailable: thinking hard")
    assert decision.reason.endswith("(2 attempts)")
    assert len(decision.reason) <= MAX_REASON_CHARS


def test_gate_budget_zero_rejects_without_calling_the_gate(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",), gate_budget_left=0)

    assert (decision.reply, decision.layer) == ("reject", "budget")
    assert decision.reason == "gate budget exhausted (0)"
    assert decision.gate_elapsed is None
    assert gate.calls == []


def test_gate_without_a_gate_model_fails_closed(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(settings=None), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.reply == "reject"
    assert decision.layer == "gate-failed"
    assert "no gate model" in decision.reason
    assert gate.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# what the gate is shown
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_user_message_carries_the_context(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/kilo/*", "/tmp/contest/*")),
                    completion_fn=gate, clock=FakeClock())
    worktree = (tmp_path / "rounds" / "r1" / "hy3").resolve()
    worktree.mkdir(parents=True)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, worktree)

    assert decision.layer == "gate"
    message = gate.user_message()
    assert "rm -v /tmp/testfile" in message
    assert str(worktree) in message
    assert TICKET_TITLE in message
    assert "tools/contest/policy.py" in message
    assert "/tmp/kilo/*" in message
    assert "Remove test file with verbose output" in message
    assert "external_directory" in message
    # one recent tool line, name, input and status
    tool_lines = [line for line in message.splitlines() if line.strip().startswith("bash")]
    assert len(tool_lines) == 1
    assert '"command": "rm -v /tmp/testfile"' in tool_lines[0]
    assert "-> error" in tool_lines[0]


def test_gate_user_message_omits_the_parts_that_were_not_asked(tmp_path):
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decide(policy, make_event(permission="external_directory",
                              patterns=["/tmp/kilo/x"], metadata=False),
           tmp_path, ticket_files=(), recent_tools=(), ticket_title="")

    message = gate.user_message()
    assert "command: -" in message
    assert "description: -" in message
    assert "directories: -" in message
    assert "patterns: /tmp/kilo/x" in message
    assert "ticket: -" in message
    assert "ticket files: -" in message
    assert "recent tools: none" in message


def test_default_completion_builds_the_call_from_gate_settings(tmp_path, monkeypatch):
    seen = {}

    def fake_request_completion(url, headers, payload, timeout, **kwargs):
        seen.update(url=url, headers=headers, payload=payload,
                    timeout=timeout, **kwargs)
        return json.dumps(ALLOW)

    monkeypatch.setattr(policy_mod, "request_completion", fake_request_completion)
    settings = LlmSettings(
        base_url="https://policy-test/v1",
        api_key="test-gate-key",
        model="policy/gate-model",
        api_format="openai",
        response_format=True,
        temperature=0.2,
        max_tokens=512,
        num_ctx=4096,
        think=True,
    )
    slept: list = []
    policy = Policy(make_config(settings=settings), clock=FakeClock(),
                    sleep=lambda seconds: slept.append(seconds))

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.reply == "once"
    payload = seen["payload"]
    assert payload["model"] == "policy/gate-model"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 512
    assert payload["response_format"] == {"type": "json_object"}
    assert payload.get("stream") is not True
    roles = [part["role"] for part in payload["messages"]]
    assert roles == ["system", "user"]
    assert payload["messages"][0]["content"] == GATE_SYSTEM_PROMPT
    assert seen["url"] == "https://policy-test/v1/chat/completions"
    assert seen["timeout"] == GATE_TIMEOUT == 60.0
    assert seen["stream"] is False
    assert seen["api_format"] == "openai"
    # KC-55: the gate retries its transport failures with the [contest] budget,
    # so a 429 on kenari.id is a wait, not a reject
    assert seen["error_retries"] == 3
    assert seen["error_retry_wait_sec"] == 10.0
    assert seen["max_retry_after_sec"] == 60.0
    assert seen["ssl_context"] is None
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    # and the sleep it waits on is the policy's own, checked against the
    # deadline before it is spent — a test never waits it for real
    assert callable(seen["_sleep_fn"])
    assert callable(seen["on_retry"])
    assert slept == [], "an allow on the first call spends no wait"
    seen["_sleep_fn"](4.5)
    assert slept == [4.5]


def test_default_completion_builds_an_ollama_call(tmp_path, monkeypatch):
    seen = {}

    def fake_request_completion(url, headers, payload, timeout, **kwargs):
        seen.update(url=url, payload=payload, **kwargs)
        return json.dumps(REJECT)

    monkeypatch.setattr(policy_mod, "request_completion", fake_request_completion)
    settings = LlmSettings(
        base_url="http://127.0.0.1:11434/api", api_key="",
        model="gate-ollama", api_format="ollama", response_format=True,
    )
    policy = Policy(make_config(settings=settings), clock=FakeClock())

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert decision.reply == "reject"
    assert seen["url"].endswith("/api/chat")
    assert seen["payload"]["format"] == "json"
    assert "response_format" not in seen["payload"]


# ─────────────────────────────────────────────────────────────────────────────
# the invariants
# ─────────────────────────────────────────────────────────────────────────────

def test_always_occurs_once_and_only_in_the_module_docstring():
    """grep -c '"always"' counts one line — the module docstring's."""
    source = POLICY_FILE.read_text(encoding="utf-8")
    lines = [line for line in source.splitlines() if '"always"' in line]
    assert len(lines) == 1
    assert '"always"' in policy_mod.__doc__
    assert set(REPLIES) == {"once", "reject"}
    assert "always" not in REPLIES
    assert not any(reply == "always" for reply in REPLIES)


def test_decision_rejects_an_always_reply():
    with pytest.raises(ValueError, match="must be one of"):
        Decision("always", "mechanical", "whitelist /tmp/*")


def test_decision_rejects_an_unknown_layer():
    with pytest.raises(ValueError, match="must be one of"):
        Decision("reject", "intuition", "seemed safe")


def test_decision_reason_is_one_line_and_clamped():
    long = ("a" * 300).replace("aaaa", "a b", 1)
    decision = Decision("reject", "gate", "line one\nline two   spaced out   " + long)

    assert "\n" not in decision.reason
    assert "  " not in decision.reason
    assert len(decision.reason) <= MAX_REASON_CHARS
    assert decision.reason.startswith("line one line two spaced")


def test_records_are_frozen():
    with pytest.raises(FrozenInstanceError):
        PolicyContext(worktree=Path("/tmp")).gate_budget_left = 0
    with pytest.raises(FrozenInstanceError):
        Decision("reject", "mechanical", "x").reason = "y"


def test_gate_prompt_is_bounded_and_ends_with_the_closing_line():
    assert len(GATE_SYSTEM_PROMPT.splitlines()) <= 40
    closing = ("When unsure, reject — a rejection costs the agent one retry; "
               "an allow can cost the machine.")
    assert closing in GATE_SYSTEM_PROMPT.replace("\n", " ")
    assert GATE_SYSTEM_PROMPT.strip().endswith("cost the machine.")
    for phrase in ("tmp_roots", ".git", "~/.ssh", "curl", "git push",
                   '{"verdict": "allow" or "reject", "reason": "one line"}'):
        assert phrase in GATE_SYSTEM_PROMPT


# ─────────────────────────────────────────────────────────────────────────────
# the audit trail
# ─────────────────────────────────────────────────────────────────────────────

def test_record_appends_one_line_per_decision(tmp_path):
    log = tmp_path / "out" / "decisions.jsonl"
    policy = Policy(make_config(), completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock())
    worktree = tmp_path.resolve()

    doom = make_event(permission="doom_loop", patterns=[str(worktree / "a")],
                      pid="per_one")
    policy.record(decide(policy, doom, worktree), doom, log)
    policy.record(decide(policy, EXTERNAL_DIRECTORY_EVENT, worktree,
                         tmp_roots=("/tmp/*",)),
        EXTERNAL_DIRECTORY_EVENT, log)
    policy.record(decide(policy, EXTERNAL_DIRECTORY_EVENT, worktree,
                         tmp_roots=("/tmp/kilo/*",)),
        EXTERNAL_DIRECTORY_EVENT, log)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    entries = [json.loads(line) for line in lines]
    for entry in entries:
        assert tuple(entry) == DECISION_KEYS

    first, second, third = entries
    assert first["sessionID"] == "ses_test01"
    assert first["permission_id"] == "per_one"
    assert first["permission"] == "doom_loop"
    assert first["patterns"] == [str(worktree / "a")]
    assert first["command"] == ""
    assert (first["layer"], first["reply"]) == ("mechanical", "reject")
    assert first["reason"] == "doom loop"
    assert first["gate_elapsed"] is None
    assert isinstance(first["t"], float)

    assert second["sessionID"] == EXTERNAL_DIRECTORY_EVENT["properties"]["sessionID"]
    assert second["permission_id"] == EXTERNAL_DIRECTORY_EVENT["properties"]["id"]
    assert second["permission"] == "external_directory"
    assert second["patterns"] == ["/tmp/*"]
    assert second["command"] == "rm -v /tmp/testfile"
    assert (second["layer"], second["reply"]) == ("mechanical", "once")
    assert second["reason"] == "inside worktree/tmp_roots"
    assert second["gate_elapsed"] is None

    # the gate branch: an elapsed time, and the gate model that answered
    assert (third["layer"], third["reply"]) == ("gate", "once")
    assert isinstance(third["gate_elapsed"], float)
    assert third["gate_elapsed"] == pytest.approx(0.5)
    assert third["gate_model"] == "policy/gate-model"
    assert all(entry["gate_model"] == "policy/gate-model" for entry in entries)
    assert entries[0]["t"] <= entries[2]["t"]


def test_record_never_raises_on_a_broken_artifact(tmp_path):
    policy = Policy(make_config(), clock=FakeClock())
    decision = Decision("reject", "budget", "gate budget exhausted (0)")

    policy.record(decision, None, tmp_path / "decisions.jsonl")      # bad event
    policy.record(decision, {}, tmp_path / "decisions.jsonl")         # no properties
    policy.record(decision, {"properties": []}, tmp_path / "decisions.jsonl")
    policy.record(None, EXTERNAL_DIRECTORY_EVENT, tmp_path / "decisions.jsonl")

    entries = [json.loads(line)
               for line in (tmp_path / "decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 4
    assert all(tuple(entry) == DECISION_KEYS for entry in entries)
    assert all(entry["gate_elapsed"] is None for entry in entries)
    assert all(entry["gate_model"] == "policy/gate-model" for entry in entries)
    assert [entry["permission_id"] for entry in entries] == [
        "", "", "", "per_0afb2bf60001KFTnUBoT5JQbJe",
    ]
    # the fourth record had no Decision: the line still lands, with no reply
    assert [entry["reply"] for entry in entries] == ["reject", "reject", "reject", ""]
    assert [entry["layer"] for entry in entries] == ["budget", "budget", "budget", ""]
    assert entries[3]["permission"] == "external_directory"
    assert entries[3]["command"] == "rm -v /tmp/testfile"

    # a path that cannot be opened is logged, not raised
    policy.record(decision, EXTERNAL_DIRECTORY_EVENT, tmp_path / "decisions.jsonl" / "deeper")


# ─────────────────────────────────────────────────────────────────────────────
# fail open: decide never raises
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("event,ctx_extra", [
    ("not a mapping", {}),
    ({"properties": "no"}, {}),
    ({"properties": ["a", "b"]}, {}),
    ({}, {}),
    (EXTERNAL_DIRECTORY_EVENT, {"gate_budget_left": "soon"}),
    (EXTERNAL_DIRECTORY_EVENT, {"tmp_roots": "not-a-tuple"}),
    (EXTERNAL_DIRECTORY_EVENT, {"forbidden": "not-a-tuple"}),
    (EXTERNAL_DIRECTORY_EVENT, {"recent_tools": "not-a-tuple"}),
    (EXTERNAL_DIRECTORY_EVENT, {"ticket_files": "not-a-tuple"}),
])
def test_decide_never_raises(tmp_path, event, ctx_extra):
    policy = Policy(make_config(), completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock())
    decision = policy.decide(event, make_ctx(tmp_path, **ctx_extra))

    assert isinstance(decision, Decision)
    assert decision.reply in REPLIES
    assert decision.layer in LAYERS
    assert len(decision.reason) <= MAX_REASON_CHARS


def test_decide_resolves_a_relative_worktree(tmp_path):
    """A worktree that is not absolute is resolved against the cwd, not fatal."""
    policy = Policy(make_config(), completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock())
    ctx = PolicyContext(worktree="relative/and/unresolved",
                        tmp_roots=("/tmp/kilo/*",))

    decision = policy.decide(EXTERNAL_DIRECTORY_EVENT, ctx)

    assert decision.reply == "once"
    assert decision.layer == "gate"


def test_decide_with_no_config_or_no_event_shape(tmp_path):
    policy = Policy(None, completion_fn=StubGate(json.dumps(ALLOW)), clock=FakeClock())

    decision = policy.decide(EXTERNAL_DIRECTORY_EVENT, make_ctx(tmp_path))

    assert decision.reply == "reject"
    assert decision.layer == "gate-failed"
    assert "no gate model" in decision.reason

    policy = Policy(make_config(), completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock())
    for bad_ctx in (None, "no", 42, []):
        decision = policy.decide(EXTERNAL_DIRECTORY_EVENT, bad_ctx)
        assert decision.reply == "reject"
        assert decision.layer == "gate-failed"


def test_mechanical_layers_do_not_burn_gate_budget(tmp_path):
    """The budget counts gate calls only — geometry is free."""
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(deny_commands=("git push*", "rm -v /tmp/*")),
                    completion_fn=gate, clock=FakeClock())
    worktree = tmp_path.resolve()

    for event in (
        make_event(permission="doom_loop", patterns=[str(worktree / "a")]),
        make_event(patterns=[str(worktree / "b.py")]),
        BASH_EVENT,
    ):
        decision = policy.decide(event, make_ctx(worktree))
        assert decision.layer == "mechanical"
    assert gate.calls == []


def test_two_mixed_paths_are_not_rescued_by_the_first_one(tmp_path):
    """One path inside the worktree does not make a mixed event allowed."""
    worktree = tmp_path.resolve()
    worktree.mkdir(exist_ok=True)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(patterns=[str(worktree / "ok.py"), "/etc/passwd"])

    decision = decide(policy, event, worktree)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1


def test_tilde_in_patterns_meets_the_home_denylist(tmp_path):
    """``~`` is expanded for every source, not only the command scan: an
    ``external_directory`` pattern ``~/.ssh/*`` is forbidden, not "inside the
    worktree" because ``Path("~/.ssh").resolve()`` landed under the cwd."""
    event = make_event(permission="external_directory", patterns=["~/.ssh/*"],
                       command="ls ~/.ssh")
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert "forbidden" in decision.reason
    assert gate.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# KC-28: a null device is not a place — no gate call for `2>/dev/null`
# ─────────────────────────────────────────────────────────────────────────────

def test_redirect_into_dev_null_is_mechanical(tmp_path):
    """`pytest -q > /dev/null 2>&1` has no path outside the worktree."""
    event = make_event(permission="bash", patterns=[],
                       command="python3 -m pytest tests -q > /dev/null 2>&1")
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(tmp_roots=("/tmp/contest/*",)),
                    completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path, tmp_roots=("/tmp/contest/*",))

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert "no path outside" in decision.reason
    assert gate.calls == []


@pytest.mark.parametrize("command", [
    # the round 86 shapes, all of which the gate was asked about
    "ls tests/ | head -60; ls .smoke_tests/ 2>/dev/null | head -60",
    "pip list 2>/dev/null | grep -i -E \"pytest|timeout|xdist\"",
    "git diff --stat HEAD~1..HEAD 2>/dev/null || echo \"only one commit\"",
    "cat tools/auto/collect_bridge.py 2>/dev/null | head -50",
    "echo done >/dev/stderr; cat x < /dev/tty; echo y > /dev/stdout",
])
def test_probe_commands_with_null_devices_do_not_reach_the_gate(tmp_path, command):
    event = make_event(permission="bash", patterns=[], command=command)
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert (decision.reply, decision.layer) == ("once", "mechanical")
    assert gate.calls == []


def test_dev_null_beside_an_outside_path_still_asks_for_the_outside_path(tmp_path):
    """Only the null device is dropped: the other path still goes to the gate."""
    event = make_event(permission="bash", patterns=[],
                       command="cat x > /dev/null; cp y /tmp/elsewhere/z")
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    assert [originals for _, originals in policy_mod._extract_paths(event["properties"])] \
        == [("/tmp/elsewhere/z",)]
    decision = decide(policy, event, tmp_path)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1
    assert "/tmp/elsewhere/z" in gate.user_message()


def test_dev_null_in_patterns_yields_no_path():
    event = make_event(permission="external_directory", patterns=["/dev/null"],
                       directories=["/dev/null"])
    assert policy_mod._extract_paths(event["properties"]) == []


def test_dev_shm_is_a_place_and_still_goes_to_the_gate(tmp_path):
    event = make_event(permission="bash", patterns=[], command="echo x > /dev/shm/leak")
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, event, tmp_path)

    assert decision.layer == "gate"
    assert len(gate.calls) == 1
    assert "/dev/shm" not in NULL_DEVICES


# ─────────────────────────────────────────────────────────────────────────────
# KC-46: the rounds folder is forbidden, the agent's own worktree in it is not
# ─────────────────────────────────────────────────────────────────────────────

def _rounds(tmp_path):
    """``<tmp>/rounds`` with this agent's ``86-a``, a sibling ``86-b`` and an
    earlier round's ``85-a``; the context the runner builds for ``86-a``."""
    rounds = (tmp_path / "rounds").resolve()
    for name in ("86-a", "86-b", "85-a"):
        (rounds / name / "scripts").mkdir(parents=True)
    own = rounds / "86-a"
    return rounds, own, {"forbidden": HARD_DENYLIST + (rounds,)}


def _bash(command):
    return make_event(permission="bash", patterns=[command], command=command)


def test_own_worktree_by_absolute_path_is_mechanical_once(tmp_path):
    rounds, own, ctx = _rounds(tmp_path)
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    for command in (f"head -3 {own}/scripts/x.py",
                    f"cd {own} && python3 --version",
                    f"cat {own}/tests/SLOW_TESTS.txt"):
        decision = decide(policy, _bash(command), own, **ctx)
        assert (decision.reply, decision.layer) == ("once", "mechanical"), command
        assert decision.reason == "inside worktree/tmp_roots"
    assert gate.calls == []


@pytest.mark.parametrize("other", ["86-b", "85-a"])
def test_another_worktree_under_the_rounds_folder_stays_forbidden(tmp_path, other):
    """A sibling of this round and an earlier round's worktree alike."""
    rounds, own, ctx = _rounds(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, _bash(f"cat {rounds / other}/x.py"), own, **ctx)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert "forbidden" in decision.reason and str(rounds) in decision.reason
    assert gate.calls == []


def test_own_file_beside_a_siblings_file_is_rejected(tmp_path):
    rounds, own, ctx = _rounds(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, _bash(f"diff {own}/a.py {rounds}/86-b/a.py"), own, **ctx)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert f"{rounds}/86-b/a.py" in decision.reason
    assert gate.calls == []


def test_the_rounds_folder_itself_stays_forbidden(tmp_path):
    """Only the worktree is let through, not the folder that holds it."""
    rounds, own, ctx = _rounds(tmp_path)
    policy = Policy(make_config(), completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock())

    decision = decide(policy, _bash(f"ls {rounds}"), own, **ctx)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")


def test_an_entry_inside_the_own_worktree_still_beats_it(tmp_path):
    """The exemption covers the worktree's ancestors only: a forbidden
    ``.git`` inside the worktree is still forbidden."""
    rounds, own, ctx = _rounds(tmp_path)
    dotgit = own / ".git"
    dotgit.mkdir()
    policy = Policy(make_config(), completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock())

    decision = decide(policy, _bash(f"cat {dotgit}/config"), own,
                      forbidden=ctx["forbidden"] + (dotgit,))

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert str(dotgit) in decision.reason


def test_forbidden_match_without_a_worktree_is_unchanged(tmp_path):
    rounds, own, _ = _rounds(tmp_path)
    assert policy_mod._forbidden_match(own / "x.py", (rounds,)) == rounds
    assert policy_mod._forbidden_match(own / "x.py", (rounds,), own) is None
    assert policy_mod._forbidden_match(rounds / "86-b" / "x.py", (rounds,), own) == rounds


@pytest.mark.parametrize("spelling", ["dotdot", "symlink"])
def test_an_own_worktree_spelling_that_lands_on_a_sibling_stays_forbidden(tmp_path, spelling):
    """The exemption is judged on the resolved path: ``<own>/../86-b`` and a
    symlink in the own worktree that points at a sibling are the sibling."""
    rounds, own, ctx = _rounds(tmp_path)
    if spelling == "dotdot":
        path = f"{own}/../86-b/x.py"
    else:
        (own / "peek").symlink_to(rounds / "86-b")
        path = f"{own}/peek/x.py"
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())

    decision = decide(policy, _bash(f"cat {path}"), own, **ctx)

    assert (decision.reply, decision.layer) == ("reject", "mechanical")
    assert decision.reason.startswith("forbidden: ")
    assert gate.calls == []


# ─────────────────────────────────────────────────────────────────────────────
# KC-55: the gate rides out a rate limit inside a bounded wait
# ─────────────────────────────────────────────────────────────────────────────

#: The body the fake opener returns when it answers: one allow verdict, the way
#: the wire carries it.
ALLOW_BODY = {"choices": [{"message": {"role": "assistant", "content": json.dumps(ALLOW)}}]}

#: The sentence a gate-failed reason ends with after a 429 or a 5xx.
OVERLOAD_TAIL = ("the reviewer is overloaded; a command inside your worktree "
                 "needs no reviewer")

GATE_URL = "https://policy-test/v1/chat/completions"


def rate_limit(url: str = GATE_URL, retry_after: "str | None" = None
               ) -> urllib.error.HTTPError:
    """One 429, the way kenari.id sent it in round 66.

    *retry_after* is the header the server sent. Absent, ``request_completion``
    waits its own ``error_retry_wait_sec`` instead.
    """
    headers = {"Retry-After": retry_after} if retry_after else None
    return urllib.error.HTTPError(url, 429, "Too Many Requests", headers, None)


class FakeResponse:
    """A response body as ``request_completion`` reads it: ``read``, as a context."""

    def __init__(self, body: str):
        self._body = body.encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """``urllib.request.urlopen`` — the one call site ``request_completion`` has.

    Each entry is an exception to raise or a body to return; the last entry is
    held for as many calls as the retry loops make. Nothing here opens a socket,
    which is why these tests run with the network down.
    """

    def __init__(self, *results):
        self.results = list(results)
        self.opens = 0

    def __call__(self, request, timeout=None, context=None, **kwargs):
        self.opens += 1
        result = self.results[min(self.opens - 1, len(self.results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return FakeResponse(result if isinstance(result, str) else json.dumps(result))


class FakeSleep:
    """A sleep that spends its seconds on the fake clock — no wall time."""

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds: float):
        self.calls.append(seconds)
        self.clock.now += seconds
        return None


class TwiceGate:
    """A ``completion_fn`` that answers *first*, then *second* — the re-ask."""

    def __init__(self, first, second):
        self.results = [first, second]
        self.calls = []

    def __call__(self, url, headers, payload, timeout, **kwargs):
        self.calls.append(dict(kwargs))
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def retry_config(*, retries: int = 3, wait: float = 10.0, max_wait: float = 60.0,
                 deadline: float = 600.0, settings=_UNSET) -> ContestConfig:
    """The policy's config with the four ``[contest] gate_*`` keys named."""
    config = make_config(settings=settings)
    return replace(config, gate_retries=retries, gate_retry_wait_sec=wait,
                   gate_retry_max_wait_sec=max_wait, gate_deadline_sec=deadline)


def test_a_429_is_retried_and_the_gate_answers(tmp_path, monkeypatch):
    """Round 66's 429: one wait of the server's own Retry-After, then a verdict."""
    opener = FakeOpener(rate_limit(retry_after="7"), ALLOW_BODY)
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(), clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("once", "gate")
    assert decision.gate_attempts == 2
    assert opener.opens == 2
    assert slept.calls == [7.0]
    assert decision.gate_elapsed >= 7.0


def test_a_429_every_time_names_the_status_the_attempts_and_the_overload(tmp_path,
                                                                          monkeypatch):
    opener = FakeOpener(rate_limit())
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(), clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason.startswith("gate unavailable: HTTP 429 (4 attempts")
    assert decision.reason.endswith(OVERLOAD_TAIL)
    assert decision.gate_attempts == 4
    assert opener.opens == 4
    assert slept.calls == [10.0, 10.0, 10.0]
    # the reason is shown to the agent: never the URL, the body or a key
    for forbidden in ("http://", "https://", "Bearer", "test-gate-key", "api_key"):
        assert forbidden not in decision.reason


def test_a_retry_after_longer_than_the_cap_fails_at_once(tmp_path, monkeypatch):
    """A 3600 s Retry-After is a quota reset, not a blip: one call, no wait."""
    opener = FakeOpener(rate_limit(retry_after="3600"))
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(), clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason.startswith("gate unavailable: HTTP 429")
    assert decision.gate_attempts == 1
    assert opener.opens == 1
    assert slept.calls == []


def test_a_refused_key_fails_at_once_without_the_overload_hint(tmp_path, monkeypatch):
    """401 is neither a blip nor an overload: one call, the status, nothing else."""
    opener = FakeOpener(urllib.error.HTTPError(GATE_URL, 401, "Unauthorized", {}, None))
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(), clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason == "gate unavailable: HTTP 401"
    assert OVERLOAD_TAIL not in decision.reason
    assert opener.opens == 1
    assert slept.calls == []


def test_zero_gate_retries_is_today_s_fail_fast_gate(tmp_path, monkeypatch):
    """``gate_retries = 0``: one call, no wait — but the 429 is still named."""
    opener = FakeOpener(rate_limit())
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(retries=0), clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason.startswith("gate unavailable: HTTP 429")
    assert opener.opens == 1
    assert slept.calls == []


def test_an_unparsable_reply_is_asked_once_more(tmp_path):
    """KC-37 §1: an empty body is the same ask again, after GATE_RETRY_WAIT."""
    gate = TwiceGate("", json.dumps(ALLOW))
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(), completion_fn=gate, clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("once", "gate")
    assert decision.gate_attempts == 2
    assert len(gate.calls) == 2
    assert slept.calls == [GATE_RETRY_WAIT]


def test_an_unparsable_reply_twice_names_both_attempts(tmp_path):
    gate = TwiceGate("", "")
    clock = FakeClock(step=0.1)
    policy = Policy(retry_config(), completion_fn=gate, clock=clock, sleep=FakeSleep(clock))

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason == "gate unavailable: empty reply (2 attempts)"
    assert decision.gate_attempts == 2
    assert len(gate.calls) == 2


def test_a_clean_reject_is_never_askaed(tmp_path):
    """A clean reject is a verdict: one call, never re-asked."""
    gate = StubGate(json.dumps(REJECT))
    policy = Policy(retry_config(), completion_fn=gate, clock=FakeClock(), sleep=NO_SLEEP)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate")
    assert decision.gate_attempts == 1
    assert len(gate.calls) == 1


def test_the_budget_counts_decisions_not_attempts(tmp_path, monkeypatch):
    """One call left buys the whole decision: its retries are not budget units."""
    opener = FakeOpener(rate_limit(retry_after="7"), ALLOW_BODY)
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    policy = Policy(retry_config(), clock=clock, sleep=FakeSleep(clock))

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",), gate_budget_left=1)

    assert (decision.reply, decision.layer) == ("once", "gate")
    assert decision.gate_attempts == 2


def test_the_record_only_names_the_attempts_when_there_were_two(tmp_path, monkeypatch):
    """A one-attempt record is today's line, byte for byte; two gets the count."""
    log = tmp_path / "out" / "decisions.jsonl"
    # the first decision is a clean allow, the second a 429 then an allow
    opener = FakeOpener(ALLOW_BODY, rate_limit(retry_after="7"), ALLOW_BODY)
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    policy = Policy(retry_config(), clock=clock, sleep=FakeSleep(clock))
    worktree = tmp_path.resolve()

    policy.record(decide(policy, EXTERNAL_DIRECTORY_EVENT, worktree,
                         tmp_roots=("/tmp/kilo/*",)),
                  EXTERNAL_DIRECTORY_EVENT, log)
    policy.record(decide(policy, EXTERNAL_DIRECTORY_EVENT, worktree,
                         tmp_roots=("/tmp/kilo/*",)),
                  EXTERNAL_DIRECTORY_EVENT, log)

    entries = [json.loads(line)
               for line in log.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 2
    assert tuple(entries[0]) == DECISION_KEYS
    assert "gate_attempts" not in entries[0]
    assert entries[1]["gate_attempts"] == 2
    assert entries[1]["layer"] == "gate"


def test_the_gate_worst_case_is_the_deadline_plus_the_timeout():
    """`gate_deadline_sec + GATE_TIMEOUT` — the one number intake and the tests read."""
    assert gate_worst_case_sec(ContestConfig()) == 660.0
    assert gate_worst_case_sec(retry_config(deadline=100)) == 160.0
    # fail open: no config, or a deadline that is not a number
    assert gate_worst_case_sec(None) == 660.0
    bad = replace(ContestConfig(), gate_deadline_sec="six hundred")
    assert gate_worst_case_sec(bad) == 660.0


def test_malformed_gate_limits_degrade_to_their_defaults():
    """Fail open: a config that names no gate_* values still fails fast, as today."""
    config = replace(ContestConfig(), gate_retries="three", gate_retry_wait_sec="ten",
                     gate_retry_max_wait_sec=-5, gate_deadline_sec=None)
    policy = Policy(config, completion_fn=StubGate(json.dumps(ALLOW)),
                    clock=FakeClock(), sleep=NO_SLEEP)

    assert policy._gate_limits() == (3, 10.0, 60.0, 600.0)


def test_the_deadline_stops_the_gate_before_a_wait_that_would_pass_it(tmp_path,
                                                                        monkeypatch):
    opener = FakeOpener(rate_limit(retry_after="10"))
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(retries=10, wait=10.0, deadline=30),
                    clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason.startswith("gate unavailable: HTTP 429")
    assert "deadline 30 s" in decision.reason
    assert slept.calls == [10.0, 10.0]
    assert sum(slept.calls) <= 30.0
    assert opener.opens == 3


def test_a_timeout_is_named_not_runtime_error(tmp_path, monkeypatch):
    """``request_completion`` wraps a timeout in a RuntimeError: the reason
    reads the network error out of the text instead."""
    opener = FakeOpener(TimeoutError("timed out"))
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(), clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason.startswith("gate unavailable: TimeoutError (")
    assert "RuntimeError" not in decision.reason
    assert decision.gate_attempts == 4
    assert opener.opens == 4


def test_a_garbled_body_still_stops_at_the_deadline(tmp_path, monkeypatch):
    """The read loop reopens on every pass, so one call could make
    ``(gate_retries + 1) ** 2`` requests: the deadline bounds it instead."""
    opener = FakeOpener("this is not json")
    monkeypatch.setattr(llm_stream_mod.urllib.request, "urlopen", opener)
    clock = FakeClock(step=0.1)
    slept = FakeSleep(clock)
    policy = Policy(retry_config(retries=3, wait=10.0, deadline=15),
                    clock=clock, sleep=slept)

    decision = decide(policy, EXTERNAL_DIRECTORY_EVENT, tmp_path,
                      tmp_roots=("/tmp/kilo/*",))

    assert (decision.reply, decision.layer) == ("reject", "gate-failed")
    assert decision.reason.startswith("gate unavailable: JSONDecodeError (")
    assert "deadline 15 s" in decision.reason
    assert opener.opens == 2
    assert opener.opens < (3 + 1) ** 2
    assert sum(slept.calls) <= 15.0
