"""Bug 175: `arena issue create --item` cuts a section short at a `#` inside a ~~~ or ```` fence."""

from tools.arena.tickets import _cut_item


def _cut(doc: str) -> str:
    return _cut_item(doc.split("\n"), "AR-1", "epic.md")


def test_a_comment_in_a_tilde_fence_is_not_a_heading():
    doc = ("### AR-1 — first\nText.\n~~~python\n# set up\nx = 1\n~~~\n"
           "Tail of AR-1.\n### AR-2 — next\nNot this.\n")
    cut = _cut(doc)
    assert cut.endswith("Tail of AR-1.")
    assert "AR-2" not in cut


def test_a_backtick_line_inside_a_longer_fence_does_not_close_it():
    doc = ("### AR-1 — first\n````markdown\n```\n# Heading in the example\n```\n"
           "````\nTail of AR-1.\n### AR-2\n")
    assert _cut(doc).endswith("Tail of AR-1.")


def test_a_tilde_line_does_not_close_a_backtick_fence():
    doc = ("### AR-1\n```\n~~~\n# still code\n```\nTail of AR-1.\n### AR-2\n")
    assert _cut(doc).endswith("Tail of AR-1.")


def test_a_plain_backtick_fence_still_works_and_the_next_item_still_ends_it():
    doc = "### AR-1\n```sh\n# a comment\n```\nTail.\n### AR-2\nOther.\n"
    assert _cut(doc) == "### AR-1\n```sh\n# a comment\n```\nTail."
