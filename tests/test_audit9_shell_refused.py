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


def test_case_pattern_closer_does_not_hide_later_unmatched_parentheses():
    assert is_well_formed("case x in a) echo ok;; esac")
    assert is_well_formed("(case x in a) echo ok;; esac)")
    assert not is_well_formed("case x in a) echo ok;; esac )")
    assert not is_well_formed("case x in a) echo ok;;")
    assert not is_well_formed("case x in a) echo ok ) ;; esac")
    assert not is_well_formed("(case x in a) echo ok;; b) echo other;; esac))")
    assert not is_well_formed("case x in () echo ok;; esac")


def test_case_arms_balance_independently_of_surrounding_shell_groups():
    for command in (
        "(case x in a) echo a;; b) echo b;; esac)",
        "case x in (a) echo a;; (b|c) (echo b);; esac",
        "case x in esac",
        "case in in in) echo in;; esac",
        "case x in a) case y in b) echo b;; esac;;& c) echo c;; esac",
        "case x in a) echo a;& b) echo b; esac",
        "case { in '{') echo ok;; esac",
        "case x in @(a|+(b))) echo ok;; esac",
        "'case' x",
    ):
        assert is_well_formed(command), command


def test_quoted_operands_and_case_patterns_keep_their_shell_meaning():
    for command in (
        "pytest -q > 'results.txt'",
        'pytest -q | "cat"',
        "pytest -q && 'true'",
        "pytest -q > results\\ file",
        "echo ${value:-)}",
        '(echo ok)# comment with a "',
        "case x in\n a|b) echo a;;\n *) echo b;;\nesac",
    ):
        assert is_well_formed(command), command
    assert not is_well_formed("pytest -q > # missing target")
