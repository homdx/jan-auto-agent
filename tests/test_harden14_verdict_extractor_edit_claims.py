"""tests/test_harden14_verdict_extractor_edit_claims.py — HARDEN-14.

Four places where the code answered "yes" or "found" by default and the default was wrong:

* **A** a creative validator's `NOT READY` / `Not coherent` / `Nothing makes sense` was read
  as an approval. The continuity, fact and theme validators have no re-ask, so such a reply
  approved the chapter it had thrown out. A `NOT` / `NOTHING` reply is now a rejection unless
  it is on the problem side.
* **B** the block extractor answered "not found" for a prototype whose macro is on the next
  line (a regression of fix 38), a zero-argument / async / multi-line parameter-list property
  arrow, and `= 0` / `= delete` / `= default` declarations. "Not found" is what Gate 1 reads
  as a hallucination.
* **C** `/edit` showed a cp1251 file as latin-1 mojibake, flattened a mixed file's line
  endings, and wrote a UTF-16 file it had not refused.
* **D** `ticket_claims` answered `[]` for a `## Claims:` heading with a trailing colon, so the
  judge scored nothing and said nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.auto.inner_loop import _parse_verdict_soft  # noqa: E402
from tools.block_extractor import extract_block  # noqa: E402


# ── Part A — a NOT reply that is not on the approval side rejects ──────────────

#: The 24 live rejections the ticket's table holds: a `NOT` / `NOTHING` reply whose first
#: word is not on the problem side. On the base every one of them was approved.
_A_REJECTIONS = [
    "NOT READY",
    "Not coherent",
    "Not enough tension",
    "NOT SUITABLE: tone is off",
    "Not quite — the ending is rushed",
    "Not yet. Fix the timeline.",
    "Not really; the motive is unclear",
    "Not in character for Ana",
    "Not a match for the established voice",
    "Not matching the style guide",
    "Nothing is right about this scene",
    "Nothing makes sense in the final paragraph",
    "Nothing was fixed from my last review.",
    "Nothing matches the bible.",
    "Nothing is consistent with chapter 1.",
    "Nothing here fits the established characters.",
    "Not a single detail matches the bible.",
    "Not in character: Anna would never say this.",
    "Not plausible: the timeline breaks in chapter 4.",
    "Not believable.",
    "Not faithful to the canon: …",
    "Not accurate: …",
    "Not aligned with the bible.",
    "Not usable.",
]

#: The problem-side shapes — the exception list. A new rejection phrase needs no new code.
_A_APPROVALS = [
    "Nothing to report",
    "Not in conflict with the bible",
    "No major inconsistencies",
    "Not a problem",
    "Not rejected",
    "Nothing found",
    "Not bad",
    "NOT BAD",
    "NOTHING TO ADD",
    "NOTE: fine",
]


@pytest.mark.parametrize("reply", _A_REJECTIONS)
def test_a_not_rejection_is_not_approved(reply):
    approved, _, unparseable = _parse_verdict_soft(reply)
    assert approved is False, reply
    # parsed, not failed open: fail-open is what approved the chapter here
    assert unparseable is False
    # the reason is the whole reply, as today
    assert _.strip() == reply.strip()


@pytest.mark.parametrize("reply", _A_APPROVALS)
def test_the_problem_side_still_approves(reply):
    assert _parse_verdict_soft(reply)[0] is True, reply


@pytest.mark.parametrize("reply", ["NOT APPROVED", "Nothing works", "NOT OK", "Not good enough"])
def test_an_approval_side_not_still_rejects(reply):
    assert _parse_verdict_soft(reply)[0] is False, reply


# ── Part B1 — a macro on the next line is part of the declaration ──────────────

_B1_MACRO = [
    ("int api(int a)\n    DEPRECATED_MACRO;\n", "api"),
    ("int api(int a)\n    EXPORT_API;\n", "api"),
    ("int api(int a)\n    NORETURN;\n", "api"),
    ("int api(int a)\n    __attribute__((deprecated));\n", "api"),
]

#: fix 38's call table: these must stay "not found" (B1).
_B1_REFUSALS = ["x->f(1);", "ready && f(1);", "label: f(1);", "timeout: f(5),"]


@pytest.mark.parametrize("source, name", _B1_MACRO)
def test_b1_a_macro_or_attribute_tail_is_found(source, name):
    assert extract_block(source, name, ".c"), repr(source)


@pytest.mark.parametrize("source", _B1_REFUSALS)
def test_b1_a_call_site_is_still_not_a_declaration(source):
    assert extract_block(source, "f", ".c") == "", source


# ── Part B2 — a zero-argument, async or multi-line property arrow ──────────────

_B2_SHAPES = [
    ("handler", "const api = {\n  handler: () => {\n    return 1;\n  },\n};\n"),
    ("handler", "const api = {\n  handler: async () => {\n    return 1;\n  },\n};\n"),
    ("handler", "const api = {\n  handler: (\n  a,\n  b\n) => {\n    return 1;\n  },\n};\n"),
]

#: Not a definition: a value that is not a function, or a TypeScript type member.
_B2_NOT_DEFINITIONS = [
    ("value", "const o = {\n  value: 5,\n};\n"),
    ("value", "const o = {\n  value: (a + b),\n};\n"),
    ("value", "const o = {\n  value: (a, b),\n};\n"),
    ("onSave", "interface I {\n  onSave: (value: string) => void;\n}\n"),
    ("onOpen", "const t = {\n  onOpen: (id: number) => Promise<void>,\n};\n"),
    ("callback", "function h(callback: (ev: Event) => void) {}\n"),
]

_EXTENSIONS = (".js", ".ts", ".jsx", ".tsx")


@pytest.mark.parametrize("ext", _EXTENSIONS)
@pytest.mark.parametrize("name, source", _B2_SHAPES)
def test_b2_the_new_arrow_shapes_are_found(ext, name, source):
    block = extract_block(source, name, ext)
    assert block, f"{ext} {name!r} not found in {source!r}"
    assert block.rstrip().endswith("}")


@pytest.mark.parametrize("ext", _EXTENSIONS)
@pytest.mark.parametrize("name, source", _B2_NOT_DEFINITIONS)
def test_b2_a_type_member_or_non_function_is_not_a_definition(ext, name, source):
    assert extract_block(source, name, ext) == "", f"{ext} {name!r} in {source!r}"


@pytest.mark.parametrize("ext", _EXTENSIONS)
def test_b2_an_expression_body_definition_is_still_found(ext):
    block = extract_block(
        "const api = {\n  save: async (u) => persist(u),\n};\n", "save", ext,
    )
    assert block and "save: async (u) => persist(u)" in block


# ── Part B3 — `= 0`, `= delete`, `= default` are body-less declarations ─────────

_B3_FOUND = [
    ("virtual void f() = 0;\n", "f"),
    ("virtual int f() const = 0;\n", "f"),
    ("void g() = delete;\n", "g"),
    ("~I() = default;\n", "~I"),
]

_B3_NOT_DECLARATIONS = ["int x = 0;\n", "a = b;\n"]


@pytest.mark.parametrize("source, name", _B3_FOUND)
def test_b3_a_bodyless_cpp_declaration_is_found(source, name):
    assert extract_block(source, name, ".cpp").rstrip().endswith(";"), repr(source)


@pytest.mark.parametrize("source", _B3_NOT_DECLARATIONS)
def test_b3_an_initialiser_is_not_a_function_declaration(source):
    name = source.split()[1].split("(")[0]
    assert extract_block(source, name, ".cpp") == "", source


# ── Part D — a `## Claims:` heading opens the section ──────────────────────────


def _claims(text):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_judge", ROOT / "scripts" / "judge_epic_round.py")
    judge = importlib.util.module_from_spec(spec)
    sys.modules["_judge"] = judge
    spec.loader.exec_module(judge)
    return judge.ticket_claims(text, "312-x.md")


@pytest.mark.parametrize("heading", ["## Claims:", "### Claims", "#### Claims", "## Claims"])
def test_d_a_claims_heading_with_a_colon_or_deeper_level_gives_the_claim(heading):
    claims = _claims(f"{heading}\n\n- [new] a\n")
    assert claims == [{"claim": "a", "expect": "new"}], heading


def test_d_a_ticket_without_a_claims_heading_still_gives_none():
    assert _claims("# Ticket\n\nno section here\n") == []


def test_d_a_claims_heading_with_prose_under_it_is_a_refusal_naming_the_line():
    with pytest.raises(ValueError, match="312-x.md:1"):
        _claims("## Claims:\n\nsome prose, no claim\n")
