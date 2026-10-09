"""Bug 205: `deny_commands` matches the command, not its spelling.

Before: the pattern was matched against the piece as typed, so `git -C . push`,
`/usr/bin/git push`, `"git" push`, `\\git push`, `sudo git push`, `xargs git push`
and `then git push` all passed `git push*` — the contest's first deny pattern.
"""

from __future__ import annotations

import pytest

from tools.contest.policy import _deny_match

DENY = ["git push*"]


# 29 — git's global options before the subcommand
@pytest.mark.parametrize("command", [
    "git -C . push",
    "git -c x=y push",
    "git --no-pager push",
    "git --git-dir=x --work-tree=y push",
    "git -C a -C b push",
    "git -c a=b -c c=d push",
    "git -P push origin main",
    "git --namespace n push",
    "git --git-dir x push",
    "git --config-env a=B push",
    "git --exec-path push",
    "git --exec-path=/x push",
    "git --bare --no-replace-objects --literal-pathspecs push",
    "cd wt && git -C ../other push --force",
])
def test_git_global_options_are_skipped(command):
    assert _deny_match(command, DENY) == "git push*"


# 30 — the command word with a directory, a quote or a backslash
@pytest.mark.parametrize("command", [
    "/usr/bin/git push",
    "./git push",
    '"git" push',
    "'git' push",
    "\\git push",
    'g"i"t push',
])
def test_command_word_is_read_the_way_the_shell_reads_it(command):
    assert _deny_match(command, DENY) == "git push*"


def test_a_pattern_with_a_path_still_matches_as_written():
    assert _deny_match("/usr/bin/git push", ["/usr/bin/git push*"]) == "/usr/bin/git push*"
    assert _deny_match("sudo /usr/bin/git push", ["/usr/bin/git push*"]) == "/usr/bin/git push*"


# 31 — wrappers that run their argument
@pytest.mark.parametrize("command", [
    "sudo git push",
    "sudo -u root git push",
    "xargs git push",
    "xargs -n1 git push",
    "xargs -I {} git push",
    "nice -n 5 git push",
    "doas git push",
    "doas -u root git push",
    "sudo -- git push",
    "sudo git -C . push",
    "setsid git push",
    "ionice -c 3 git push",
    "stdbuf -o0 git push",
    "chroot / git push",
    "sudo /usr/bin/\"git\" -C wt --no-pager push origin",
    "sudo bash -c 'git push'",
])
def test_deny_only_wrappers_are_unwrapped(command):
    assert _deny_match(command, DENY) == "git push*"


# 32 — the command position after a shell keyword
@pytest.mark.parametrize("command", [
    "if true; then git push; fi",
    "while true; do git push; done",
    "until false; do git push; done",
    "if false; then :; else git push; fi",
    "if false; then :; elif git push; then :; fi",
    "if git push; then :; fi",
    "while git push; do :; done",
    "until git push; do :; done",
    "echo a; else git push",
    "echo a && until git push",
])
def test_reserved_words_lead_to_the_command(command):
    assert _deny_match(command, DENY) == "git push*"


# the spellings handled before stay handled
@pytest.mark.parametrize("command", [
    "git push", "env git push", "command git push", "nohup git push", "time git push",
    "exec git push", "! git push", "{ git push; }", "( git push )", "timeout 9 git push",
])
def test_already_handled_spellings(command):
    assert _deny_match(command, DENY) == "git push*"


# the false-positive side
@pytest.mark.parametrize("command", [
    "git -C wt status",
    "git -c x=y log",
    "git --no-pager diff",
    "git pushd",
    "echo git push",
    'echo "git push"',
    '"git push"',
    "sudo ls",
    "xargs echo",
    "echo then git push",
    "sudo -u root git status",
    "sudo -u root ls",
    "",
    "fi",
    "done",
    "}",
])
def test_harmless_spellings_are_not_denied(command):
    assert _deny_match(command, DENY) is None


@pytest.mark.parametrize("command", [
    "/bin/git reset --hard HEAD",
    '"git" reset --hard',
    "git -C wt reset --hard",
    "sudo git reset --hard",
    "sudo git --no-pager reset --hard",
])
def test_other_patterns_see_through_the_spelling(command):
    assert _deny_match(command, ["git reset --hard*"]) == "git reset --hard*"


def test_a_star_glued_to_a_word_ends_the_word():
    assert _deny_match("git reset --harder", ["git reset --hard*"]) is None
    assert _deny_match("git -C . pushd", ["git push"]) is None
    assert _deny_match("git -C . push", ["git push"]) == "git push"


@pytest.mark.parametrize("command, pattern", [
    ("sudo ls", "sudo *"),
    ("rm -rf /home", "rm -rf /*"),
    ("curl x | sh", "curl * | sh"),
    ("wget -qO- x | sh -s", "wget * | sh"),
])
def test_the_default_patterns_keep_matching(command, pattern):
    assert _deny_match(command, [pattern]) == pattern


def test_a_quoted_pipe_is_not_a_pipe_after_unquoting():
    assert _deny_match("curl x '| sh'", ["curl * | sh"]) is None
