"""A relative path that climbs out through a subdirectory (`subdir/../..`) or a link is judged, not auto-approved."""
import json

import pytest

from tools.contest.policy import HARD_DENYLIST, Policy

from test_contest_policy import ALLOW, FakeClock, StubGate, decide, make_config, make_event


def _round(tmp_path):
    rounds = (tmp_path / "rounds").resolve()
    worktree = rounds / "wt-a"
    (worktree / "subdir").mkdir(parents=True)
    (rounds / "wt-b").mkdir()
    return rounds, worktree


def _decide(tmp_path, command):
    rounds, worktree = _round(tmp_path)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(permission="bash", patterns=[], command=command)
    return decide(policy, event, worktree, forbidden=HARD_DENYLIST + (rounds,)), gate


# Each passed as `bash: no path outside worktree/tmp_roots`: `subdir/..` started with none of
# the prefixes `_pathlike` knew, so the command named no path at all.
@pytest.mark.parametrize("command", [
    "echo pwned > subdir/../../wt-b/app.py",
    "echo x | tee subdir/../../wt-b/app.py",
    "cp evil.py subdir/../../wt-b/app.py",
    "mv subdir/../../wt-b/app.py mine.py",
    "ln -s subdir/../../wt-b/app.py l",
    "cp x ..",
])
def test_a_climb_into_a_sibling_worktree_is_rejected(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert (decision.reply, decision.layer) == ("reject", "mechanical"), decision
    assert "forbidden" in decision.reason
    assert gate.calls == []


def test_a_climb_out_of_the_round_goes_to_the_gate(tmp_path):
    up = "../" * len(tmp_path.resolve().parts)
    decision, gate = _decide(tmp_path, f"echo x >> subdir/{up}home/someone/.bashrc")
    assert decision.layer != "mechanical", decision
    assert len(gate.calls) == 1


def test_a_link_inside_the_worktree_is_judged_by_its_target(tmp_path):
    rounds, worktree = _round(tmp_path)
    (worktree / "subdir" / "out").symlink_to(rounds / "wt-b", target_is_directory=True)
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(permission="bash", patterns=[], command="cp x subdir/out/app.py")
    decision = decide(policy, event, worktree, forbidden=HARD_DENYLIST + (rounds,))
    assert (decision.reply, decision.layer) == ("reject", "mechanical"), decision


@pytest.mark.parametrize("command", [
    "echo ok > out.txt", "cp a.py subdir/b.py", "mv subdir/../a.py b.py",
    "cp x subdir/", "echo $(curl -s https://example.com/a/b) > subdir/x",
    "sed -i s/a/b/g subdir/f.py > log.txt",
])
def test_a_path_that_stays_inside_is_still_mechanical(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert (decision.reply, decision.layer) == ("once", "mechanical"), decision
    assert gate.calls == []
