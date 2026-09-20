"""KC-13 black-box scenarios: `tools.contest.policy` of the entry under test,
driven through `_extract_paths` and `Policy.decide` with a stub gate — the
same public surface the ticket names as its **Symbol:**. KC13_REPO points at
the worktree; nothing from the entry's own tests is imported.

Every scenario is one `permission.asked` payload in the shape Kilo sent live
on 2026-09-19 (`patterns`, `metadata.command`, `metadata.directories`) and
one expected decision. The checks are what the corrected ticket (5ee8417)
asks for, plus the splits the KC-3 design implies (dedup across sources,
symlinks judged by their target, the scan for `bash` only).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(os.environ["KC13_REPO"]).resolve()
sys.path.insert(0, str(REPO))  # ahead of the base repo's conftest/pythonpath entries
# the base repo's conftest.py already bound `tools` to the base checkout —
# drop it so the import below is the entry's package, not the base's
for _name in [m for m in sys.modules if m == "tools" or m.startswith("tools.")]:
    del sys.modules[_name]

from tools.contest.policy import (  # noqa: E402
    HARD_DENYLIST, Decision, Policy, PolicyContext, _extract_paths,
)
from tools.contest.roster import ContestConfig, LlmSettings  # noqa: E402

assert Path(sys.modules["tools.contest.policy"].__file__).resolve().is_relative_to(REPO)

ALLOW = json.dumps({"verdict": "allow", "reason": "scratch"})
REJECT = json.dumps({"verdict": "reject", "reason": "writes outside the worktree"})
TMP_ROOTS = ("/tmp/contest/*",)

LIVE_COMMAND = ("mkdir -p /tmp/contest/notes && echo hy3 > /tmp/contest/notes/hy3.txt"
                " && echo outside > /tmp/kc6-outside-x.txt")


def bash(command, patterns=("/tmp/contest/notes/*",), directories=("/tmp/contest/notes",),
         permission="bash") -> dict:
    """A `permission.asked` event the way Kilo shapes a bash ask."""
    props = {
        "id": "per_kc13", "sessionID": "ses_kc13", "permission": permission,
        "patterns": list(patterns),
        "metadata": {"command": command, "description": "scn"},
        "always": ["*"],
        "tool": {"messageID": "msg_kc13", "callID": "call_kc13"},
    }
    if directories:
        props["metadata"]["directories"] = list(directories)
    return {"type": "permission.asked", "properties": props}


class StubGate:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, url, headers, payload, timeout, **kw):
        self.calls.append(payload)
        return self.reply

    def user_message(self, i=0) -> str:
        (m,) = [p for p in self.calls[i]["messages"] if p["role"] == "user"]
        return m["content"]


def make_policy(reply=ALLOW, *, tmp_roots=TMP_ROOTS, deny_commands=()):
    settings = LlmSettings(base_url="https://policy-test/v1", api_key="k", model="gate/m",
                           api_format="openai", response_format=True, temperature=0.0,
                           max_tokens=256)
    cfg = ContestConfig(tmp_roots=tmp_roots, deny_commands=deny_commands, gate_settings=settings)
    gate = StubGate(reply)
    return Policy(cfg, completion_fn=gate, clock=lambda: 0.0), gate


def ctx(worktree: Path, **over) -> PolicyContext:
    d = {"worktree": worktree, "tmp_roots": TMP_ROOTS, "forbidden": HARD_DENYLIST,
         "ticket_title": "KC-13", "ticket_files": ("tools/contest/policy.py",),
         "recent_tools": (), "gate_budget_left": 20}
    d.update(over)
    return PolicyContext(**d)


def resolved(event) -> list:
    return [str(r) for r, _ in _extract_paths(event["properties"])]


# ── the ticket's Acceptance, one per bullet ───────────────────────────────────

def test_s01_live_event_second_redirect_is_not_layer1_once(tmp_path):
    policy, gate = make_policy(REJECT)
    d = policy.decide(bash(LIVE_COMMAND), ctx(tmp_path))
    assert (d.reply, d.layer) == ("reject", "gate"), (d.reply, d.layer, d.reason)


def test_s02_gate_user_message_names_the_outside_target(tmp_path):
    policy, gate = make_policy(REJECT)
    policy.decide(bash(LIVE_COMMAND), ctx(tmp_path))
    assert gate.calls and "/tmp/kc6-outside-x.txt" in gate.user_message()


def test_s03_both_targets_under_tmp_roots_stay_mechanical_once(tmp_path):
    policy, gate = make_policy(REJECT)
    cmd = ("mkdir -p /tmp/contest/notes && echo a > /tmp/contest/notes/a.txt"
           " && echo b > /tmp/contest/b.txt")
    d = policy.decide(bash(cmd), ctx(tmp_path))
    assert (d.reply, d.layer) == ("once", "mechanical"), (d.reply, d.layer, d.reason)
    assert gate.calls == []


def test_s04_second_path_under_hard_denylist_is_a_mechanical_reject(tmp_path):
    policy, gate = make_policy(ALLOW)
    cmd = "mkdir -p /tmp/contest/notes && cat /tmp/contest/notes/k > ~/.ssh/x"
    d = policy.decide(bash(cmd), ctx(tmp_path))
    assert (d.reply, d.layer) == ("reject", "mechanical"), (d.reply, d.layer, d.reason)
    assert "forbidden:" in d.reason
    assert gate.calls == []


def test_s05_relative_first_path_absolute_tmp_second_is_once(tmp_path):
    policy, gate = make_policy(REJECT)
    d = policy.decide(bash("cat scripts/x.py > /tmp/contest/out.txt",
                           patterns=("/tmp/contest/*",), directories=("/tmp/contest",)),
                      ctx(tmp_path))
    assert (d.reply, d.layer) == ("once", "mechanical"), (d.reply, d.layer, d.reason)


def test_s06_bare_command_word_is_not_a_path(tmp_path):
    ev = bash("reboot", patterns=("reboot",), directories=())
    assert _extract_paths(ev["properties"]) == []
    policy, gate = make_policy(REJECT)
    d = policy.decide(ev, ctx(tmp_path))
    assert isinstance(d, Decision) and d.reply in ("once", "reject")


def test_s07_stderr_redirect_token_is_not_a_path(tmp_path):
    ev = bash("python3 -m pytest > /tmp/contest/log.txt 2>&1",
              patterns=("/tmp/contest/*",), directories=("/tmp/contest",))
    assert resolved(ev) == ["/tmp/contest"] + ["/tmp/contest/log.txt"] or \
        set(resolved(ev)) == {"/tmp/contest", "/tmp/contest/log.txt"}
    policy, gate = make_policy(REJECT)
    d = policy.decide(ev, ctx(tmp_path))
    assert (d.reply, d.layer) == ("once", "mechanical"), (d.reply, d.layer, d.reason)


def test_s08_dollar_home_token_is_not_a_path(tmp_path):
    ev = bash("echo x > $HOME/x", patterns=(), directories=())
    assert _extract_paths(ev["properties"]) == []
    policy, gate = make_policy(REJECT)
    d = policy.decide(ev, ctx(tmp_path))
    assert isinstance(d, Decision)


def test_s09_url_is_not_a_path(tmp_path):
    ev = bash("curl -s https://example.com/a/b/c -o /tmp/contest/c",
              patterns=("/tmp/contest/*",), directories=("/tmp/contest",))
    paths = resolved(ev)
    assert all(not p.startswith("/example.com") and "//" not in p for p in paths), paths
    assert "/tmp/contest/c" in paths


def test_s10_quoted_path_with_spaces_does_not_crash(tmp_path):
    ev = bash('cp x "/tmp/contest/my dir/file" && cat \'/tmp/contest/o k\'',
              patterns=("/tmp/contest/*",), directories=("/tmp/contest",))
    policy, gate = make_policy(REJECT)
    d = policy.decide(ev, ctx(tmp_path))
    assert isinstance(d, Decision) and d.reply in ("once", "reject")


# ── the two rewritten KC-3 tests, verbatim from the ticket ───────────────────

BASH_EVENT_PROPS = {
    "id": "per_0afb32854001mmQjaA39GfX6Jf", "sessionID": "ses_f504d18f0ffehKDcvqpkphdcji",
    "permission": "bash", "patterns": ["rm -v /tmp/testfile"],
    "metadata": {"command": "rm -v /tmp/testfile", "description": "Remove test file at /tmp/testfile"},
    "always": ["rm *"],
    "tool": {"messageID": "msg_0afb32854001", "callID": "call_0afb32854001"},
}


def test_s11_command_text_in_patterns_yields_only_the_commands_path():
    assert _extract_paths(BASH_EVENT_PROPS) == [(Path("/tmp/testfile"), ("/tmp/testfile",))]


def test_s12_command_glob_is_read_like_a_patterns_glob():
    props = dict(BASH_EVENT_PROPS, patterns=["rm -rf /tmp/*"],
                 metadata={"command": "rm -rf /tmp/*", "description": "x"})
    assert _extract_paths(props) == [(Path("/tmp"), ("/tmp/*",))]


# ── the scan is bash's only ──────────────────────────────────────────────────

EXTERNAL_DIRECTORY_PROPS = {
    "id": "per_0afb2bf60001KFTnUBoT5JQbJe", "sessionID": "ses_f504d5ef4ffeAbHyyPaAMRYROL",
    "permission": "external_directory", "patterns": ["/tmp/*"],
    "metadata": {"command": "rm -v /tmp/testfile", "description": "Remove test file with verbose output",
                 "directories": ["/tmp"], "patterns": ["/tmp/*"]},
    "always": ["/tmp/*"],
    "tool": {"messageID": "msg_0afb2bf60001", "callID": "call_0afb2bf60001"},
}


def test_s13_external_directory_event_is_not_scanned():
    assert _extract_paths(EXTERNAL_DIRECTORY_PROPS) == [(Path("/tmp"), ("/tmp/*", "/tmp"))]


def test_s14_an_edit_permission_with_a_command_is_not_scanned(tmp_path):
    ev = bash("cat > /tmp/kc6-outside-x.txt", patterns=(str(tmp_path / "a.py"),),
              directories=(), permission="edit")
    assert resolved(ev) == [str(tmp_path.resolve() / "a.py")]
    policy, gate = make_policy(REJECT)
    d = policy.decide(ev, ctx(tmp_path))
    assert (d.reply, d.layer) == ("once", "mechanical"), (d.reply, d.layer, d.reason)


def test_s15_a_missing_permission_key_is_not_bash():
    props = {"patterns": ["/tmp/contest/notes/*"],
             "metadata": {"command": "echo > /tmp/kc6-outside-x.txt"}}
    assert _extract_paths(props) == [(Path("/tmp/contest/notes"), ("/tmp/contest/notes/*",))]


def test_s16_doom_loop_is_still_rejected_before_any_scan(tmp_path):
    ev = bash("echo > /tmp/contest/x", permission="doom_loop")
    policy, gate = make_policy(ALLOW)
    d = policy.decide(ev, ctx(tmp_path))
    assert (d.reply, d.layer, d.reason) == ("reject", "mechanical", "doom loop")


# ── fail-open on garbage ─────────────────────────────────────────────────────

def test_s17_non_string_command_is_skipped():
    props = {"permission": "bash", "patterns": ["/tmp/contest/notes/*"],
             "metadata": {"command": 123}}
    assert _extract_paths(props) == [(Path("/tmp/contest/notes"), ("/tmp/contest/notes/*",))]
    props["metadata"]["command"] = {"a": ["/tmp/kc6-outside-x.txt"]}
    assert _extract_paths(props) == [(Path("/tmp/contest/notes"), ("/tmp/contest/notes/*",))]


def test_s18_nul_in_a_command_path_never_raises(tmp_path):
    ev = bash("echo > /tmp/contest/a\x00b && echo > /tmp/contest/ok")
    pairs = _extract_paths(ev["properties"])
    assert all("\x00" not in str(r) for r, _ in pairs)
    policy, gate = make_policy(ALLOW)
    assert isinstance(policy.decide(ev, ctx(tmp_path)), Decision)


def test_s19_decide_never_raises_on_a_garbage_event(tmp_path):
    policy, gate = make_policy(ALLOW)
    for props in ({"permission": "bash", "patterns": None, "metadata": {"command": None}},
                  {"permission": "bash", "metadata": "not a dict"},
                  {"permission": "bash", "metadata": {"command": ""}}):
        d = policy.decide({"type": "permission.asked", "properties": props}, ctx(tmp_path))
        assert isinstance(d, Decision)


# ── the union, dedup, separators, quotes, symlinks ───────────────────────────

def test_s20_first_inside_worktree_second_outside_goes_to_the_gate(tmp_path):
    inside = tmp_path / "notes.txt"
    cmd = f"cat {inside} > /tmp/kc6-outside-x.txt"
    policy, gate = make_policy(REJECT)
    d = policy.decide(bash(cmd, patterns=(str(inside),), directories=()), ctx(tmp_path))
    assert (d.reply, d.layer) == ("reject", "gate"), (d.reply, d.layer, d.reason)


def test_s21_same_path_in_patterns_and_command_is_one_pair_with_both_spellings():
    ev = bash("ls /tmp/contest/notes", patterns=("/tmp/contest/notes/*",), directories=())
    pairs = _extract_paths(ev["properties"])
    assert pairs == [(Path("/tmp/contest/notes"), ("/tmp/contest/notes/*", "/tmp/contest/notes"))]


def test_s22_every_shell_separator_starts_a_new_token():
    cmd = "(cd /tmp/contest && ls)|tee /tmp/o1/y;echo z>>/tmp/o2/z<'/tmp/o3/in'"
    ev = bash(cmd, patterns=("/tmp/contest/*",), directories=("/tmp/contest",))
    paths = set(resolved(ev))
    assert {"/tmp/contest", "/tmp/o1/y", "/tmp/o2/z", "/tmp/o3/in"} <= paths, paths


def test_s23_quotes_are_stripped_from_a_command_path():
    ev = bash("echo a > '/tmp/o4/q.txt' && echo b > \"/tmp/o5/r.txt\"",
              patterns=(), directories=())
    pairs = _extract_paths(ev["properties"])
    assert (Path("/tmp/o4/q.txt"), ("/tmp/o4/q.txt",)) in pairs, pairs
    assert (Path("/tmp/o5/r.txt"), ("/tmp/o5/r.txt",)) in pairs, pairs


def test_s24_tilde_in_the_command_is_the_home_directory():
    ev = bash("cp a ~/kc13-outside/x", patterns=(), directories=())
    pairs = _extract_paths(ev["properties"])
    assert pairs and pairs[0][0] == Path.home().resolve() / "kc13-outside" / "x", pairs


def test_s26_deny_commands_still_reject_a_command_whose_paths_are_outside(tmp_path):
    policy, gate = make_policy(ALLOW, deny_commands=("rm -rf *",))
    d = policy.decide(bash("rm -rf /tmp/kc6-outside-dir", patterns=("/tmp/kc6-outside-dir/*",),
                           directories=("/tmp/kc6-outside-dir",)), ctx(tmp_path))
    assert (d.reply, d.layer) == ("reject", "mechanical"), (d.reply, d.layer, d.reason)
    assert gate.calls == []


def test_s27_dot_slash_token_resolves_inside_the_worktree(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ev = bash("cat ./notes.txt > /tmp/contest/out.txt",
              patterns=("/tmp/contest/*",), directories=("/tmp/contest",))
    paths = resolved(ev)
    assert str(tmp_path.resolve() / "notes.txt") in paths, paths
    policy, gate = make_policy(REJECT)
    d = policy.decide(ev, ctx(tmp_path))
    assert (d.reply, d.layer) == ("once", "mechanical"), (d.reply, d.layer, d.reason)


def test_s28_patterns_path_outside_and_command_paths_inside_is_the_gate(tmp_path):
    # Kilo reported the outside one; the scan must not shadow it with the inside ones
    policy, gate = make_policy(REJECT)
    d = policy.decide(bash("ls /tmp/contest/a /tmp/contest/b", patterns=("/tmp/kc6-outside/*",),
                           directories=("/tmp/kc6-outside",)), ctx(tmp_path))
    assert (d.reply, d.layer) == ("reject", "gate"), (d.reply, d.layer, d.reason)
