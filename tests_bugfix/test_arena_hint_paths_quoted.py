"""A path in an arena hint is shell-quoted, so a checkout or file with a space pastes back as one argument."""

import shlex

from tools.arena import tickets


def test_hint_path_keeps_a_space_and_a_parenthesis_as_one_argument():
    path = "/home/me/My Repo (old)/wt"
    hint = f"git -C {tickets._hint_path(path)} status"
    assert shlex.split(hint) == ["git", "-C", path, "status"]


def test_hint_path_leaves_a_plain_path_unquoted():
    assert tickets._hint_path("/srv/repo/epic-tasks/01-x.md") == "/srv/repo/epic-tasks/01-x.md"
