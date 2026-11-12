"""AUDIT-9 shell parse refusals must not receive cached results."""

from tools.contest.testcache import is_well_formed


def test_shell_refused_commands_are_rejected():
    for command in ("pytest -q &&", "pytest -q ||", "pytest -q |", "pytest >",
                    "pytest >>", "pytest <", "pytest -q )"):
        assert not is_well_formed(command), command


def test_valid_shell_controls_stay_accepted():
    for command in (
        "a && b", "a || b", "a | b", "a > f", "a 2>&1", "a &", "a;",
        "a &&\n b", "(a; b)", "$(a)", "case x in a) echo a;; esac",
        "echo '>'", 'echo ")"', "echo '&&'", "a # > ) &&",
    ):
        assert is_well_formed(command), command
