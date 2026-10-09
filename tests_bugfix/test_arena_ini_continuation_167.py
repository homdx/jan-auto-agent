"""Bug 167: `_with_key` must replace a whole multi-line value, blank and comment lines included."""
from tools.arena import models
from tools.contest import roster


def _read(text):
    parser = roster._new_parser()
    parser.read_string(text)
    return parser["arena.profile.p"]


def test_a_blank_line_inside_the_value_does_not_leave_a_stale_model():
    text = "[arena.profile.p]\nmodels = a,\n  b,\n\n  c\nkeep = 1\n"
    out = models._with_key(text, "p", "models", "z")
    assert _read(out)["models"] == "z"
    assert _read(out)["keep"] == "1"


def test_removing_a_value_with_a_blank_line_leaves_a_parseable_ini():
    text = "[arena.profile.p]\nmodels = a,\n  b,\n\n  c\nkeep = 1\n"
    section = _read(models._with_key(text, "p", "models", None))
    assert "models" not in section and section["keep"] == "1"


def test_a_comment_line_inside_the_value_does_not_end_it():
    text = "[arena.profile.p]\nmodels = a,\n  # old\n  c\n"
    assert _read(models._with_key(text, "p", "models", "z"))["models"] == "z"


def test_trailing_blank_and_comment_lines_after_the_value_are_kept():
    text = "[arena.profile.p]\nmodels = a,\n  b\n\n# note\nkeep = 1\n\n[other]\nx = 2\n"
    out = models._with_key(text, "p", "models", "z")
    assert out == "[arena.profile.p]\nmodels = z\n\n# note\nkeep = 1\n\n[other]\nx = 2\n"
