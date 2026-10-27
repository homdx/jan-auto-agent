"""A deny_commands pattern behind an unknown wrapper or a shell-built command word goes to the gate, not "no path"."""
import json

import pytest

from tools.contest.policy import HARD_DENYLIST, Policy

from test_contest_policy import ALLOW, FakeClock, StubGate, decide, make_config, make_event

DENY = ("git push*", "rm -rf /*", "sudo *", "curl * | sh")


def _decide(tmp_path, command):
    rounds = (tmp_path / "rounds").resolve()
    worktree = rounds / "wt-a"
    worktree.mkdir(parents=True)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(deny_commands=DENY), completion_fn=gate, clock=FakeClock())
    event = make_event(permission="bash", patterns=[], command=command)
    return decide(policy, event, worktree, forbidden=HARD_DENYLIST + (rounds,)), gate


# Each was `once` from the mechanical layer with no gate call: the deny matcher does not
# read the wrapper, and the command names no path. The lock file sits in the worktree, so
# `flock` is not sent up by a /tmp path instead.
@pytest.mark.parametrize("command", [
    "flock lk git push",
    "taskset 1 git push",
    "chrt 1 git push",
    "strace git push",
    "watch git push",
    "unshare -r git push",
    "nsenter -t 1 git push",
    "parallel git push ::: a",
    r"find . -exec git push \;",
    "su -c 'git push'",
    "script -qc 'git push' /dev/null",
    "busybox sh -c 'git push'",
    "taskset 1 sudo ls",
    "git${IFS}push",
    "x=git; $x push",
    "$(echo git) push",
    "git $(echo push)",
    "git -C sub `echo push`",
    'git -c alias.p="!git push" p',
    "echo $(taskset 1 git push)",
])
def test_a_deny_pattern_the_matcher_cannot_read_goes_to_the_gate(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert decision.layer == "gate", decision
    assert len(gate.calls) == 1
    message = gate.user_message()
    assert "deny_commands: git push*, rm -rf /*, sudo *, curl * | sh" in message
    assert "suspicion: " in message


@pytest.mark.parametrize("command", [
    "git push origin main", "nohup git push", "timeout 5 git push", "g''it push",
    "bash -c 'git push'", "ls; sudo ls",
])
def test_a_form_the_matcher_reads_is_still_rejected_without_the_gate(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert (decision.reply, decision.layer) == ("reject", "mechanical"), decision
    assert gate.calls == []


@pytest.mark.parametrize("command", [
    "pytest -q", "git status", "git log --format=%H -1", 'git commit -m "$MSG"',
    "git commit -m 'push it'", "rm -rf build", "curl localhost:8000", "git pushd",
    "python app.py & curl -s localhost:8000",
])
def test_an_ordinary_command_stays_mechanical(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert (decision.reply, decision.layer) == ("once", "mechanical"), decision
    assert gate.calls == []


def test_without_deny_commands_nothing_is_suspicious(tmp_path):
    worktree = (tmp_path / "wt").resolve()
    worktree.mkdir()
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(permission="bash", patterns=[], command="taskset 1 git push")
    decision = decide(policy, event, worktree)
    assert (decision.reply, decision.layer) == ("once", "mechanical") and gate.calls == []
