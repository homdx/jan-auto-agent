"""A sibling-worktree path in `opt=value`, `-Ovalue`, an assignment, a `-c` script or a shell expansion is judged, not auto-approved."""
import json

import pytest

from tools.contest.policy import HARD_DENYLIST, Policy

from test_contest_policy import ALLOW, FakeClock, StubGate, decide, make_config, make_event


def _decide(tmp_path, command):
    rounds = (tmp_path / "rounds").resolve()
    worktree = rounds / "wt-a"
    (worktree / "sub").mkdir(parents=True)
    (rounds / "wt-b").mkdir()
    gate = StubGate(json.dumps(ALLOW))
    policy = Policy(make_config(), completion_fn=gate, clock=FakeClock())
    event = make_event(permission="bash", patterns=[], command=command)
    return decide(policy, event, worktree, forbidden=HARD_DENYLIST + (rounds,)), gate


# Each was `once` with no gate call: `of=../wt-b/app.py` resolved whole names a directory
# `of=..` inside the worktree, and a quoted script was one token with no path in it.
@pytest.mark.parametrize("command", [
    "dd of=../wt-b/app.py",
    "tar --directory=../wt-b -xf a.tar",
    "git --git-dir=../wt-b/.git status",
    "cp --target-directory=../wt-b a",
    "curl --output=../wt-b/f http://x",
    "wget -O../wt-b/f http://x",
    "sort -o../wt-b/f a",
    "F=../wt-b/f; echo x > $F",
    'export F=../wt-b/f; echo x > "$F"',
    "sh -c 'echo x > ../wt-b/f'",
    'bash -c "echo x > ../wt-b/f"',
    "bash -lc 'cp a ../wt-b/a'",
    "python -c \"open('../wt-b/f','w')\"",
    "node -e \"require('fs').writeFileSync('../wt-b/f','x')\"",
])
def test_a_value_or_script_that_climbs_into_a_sibling_is_rejected(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert (decision.reply, decision.layer) == ("reject", "mechanical"), decision
    assert "forbidden" in decision.reason and gate.calls == []


# The shell works these out; the mechanical layer cannot, so the gate decides.
@pytest.mark.parametrize("command", [
    "echo x > {..,.}/wt-b/f",
    "echo x > `pwd`/../wt-b/f",
    "echo x > $F",
    "echo x >> ${OUT}",
    "F=a; echo x > $G",
])
def test_a_place_only_the_shell_can_work_out_goes_to_the_gate(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert decision.layer != "mechanical", decision
    assert len(gate.calls) == 1


@pytest.mark.parametrize("command", [
    "echo {1..5}",
    'git commit -m "fix `name` now"',
    "echo '{a,b}/x'",
    "sed -e 's/a/b/' f.txt",
    'python -c "print(1)"',
    "bash -c 'pytest -q sub/'",
    "tar -xf a.tar --directory=sub",
    "F=sub/x; echo y > $F",
    "gcc -O2 -o out main.c",
    "curl -o out.html http://x/a?b=c/d",
    "git log --format=%H -1",
])
def test_an_ordinary_command_inside_the_worktree_stays_mechanical(tmp_path, command):
    decision, gate = _decide(tmp_path, command)
    assert (decision.reply, decision.layer) == ("once", "mechanical"), decision
    assert gate.calls == []
