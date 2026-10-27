"""HARDEN-20: five small defects — a JavaScript property arrow whose body is a bare word, the
Coder flattening a mixed file's line endings, a prompt whose `function` names the verb, a
task removal that cuts the entry quoting its heading, and the judge and the export disagreeing
about a ticket's claims."""

import importlib.util
import sys
from pathlib import Path

import pytest

from tools.actions import _edit_line_ends
from tools.auto.plan_validator import _without_section
from tools.auto.utils import in_style_of
from tools.block_extractor import extract_block
from tools.contest import export
from tools.prompt_parser import parse_prompt

ROOT = Path(__file__).resolve().parent.parent


# ── Part A — a bare word after a property arrow's `=>` is a body in JavaScript ─────


#: Each row on its own line: a property arrow's head is anchored at a line start.
_A_SOURCE = (
    "const o = {\n"
    "  a: (a) => undefined,\n"
    "  b: (a) => null,\n"
    "  c: (a) => true,\n"
    "  d: (a) => fallback,\n"
    "  e: () => undefined,\n"
    "  f: (a) => 0,\n"
    "  g: (a) => init(),\n"
    "  h: (a) => x + 1,\n"
    "};\n"
)


@pytest.mark.parametrize("ext", [".js", ".jsx"])
@pytest.mark.parametrize("name", ["a", "b", "c", "d", "e", "f", "g", "h"])
def test_a_a_bare_word_after_an_arrow_is_a_body_in_javascript(ext, name):
    block = extract_block(_A_SOURCE, name, ext)
    assert block, f"{ext} {name!r} not found"
    assert name + ":" in block


@pytest.mark.parametrize("ext", [".ts", ".tsx"])
def test_a_the_same_bare_word_is_still_a_type_in_typescript(ext):
    assert extract_block("type H = {\n  e: () => undefined;\n};\n", "e", ext) == ""
    assert extract_block("interface I {\n  onSave: (v: string) => void;\n}\n", "onSave", ext) == ""


@pytest.mark.parametrize("ext", [".js", ".jsx"])
def test_a_an_expression_body_is_still_found_in_javascript(ext):
    assert "persist(u)" in extract_block(
        "const o = {\n  save: async (u) => persist(u),\n};\n", "save", ext)


# ── Part B — an untouched line keeps its own ending, on both write paths ────────────


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


_MIXED = b"alpha\nbeta\r\ngamma\r\ndelta\r\n"
_MIXED_EDIT = "alpha\nbeta\ngamma\ndelta2\n"
_MIXED_OUT = "alpha\nbeta\r\ngamma\r\ndelta2\r\n"


def _both_paths(tmp_path, data, content):
    """The two write paths, on the same file: `/edit`'s rule and the Coder's `in_style_of`."""
    dest = _write(tmp_path / "mixed.txt", data)
    return _edit_line_ends(str(dest), content), in_style_of(dest, content)


def test_b_an_untouched_line_keeps_its_own_ending(tmp_path):
    """A task that touches one line of a mixed file produces a one-line diff."""
    edited, written = _both_paths(tmp_path, _MIXED, _MIXED_EDIT)
    assert edited == _MIXED_OUT, repr(edited)
    assert written == _MIXED_OUT, repr(written)
    original = [l for l in _MIXED.decode().split("\n") if l != ""]
    revised = [l for l in _MIXED_OUT.split("\n") if l != ""]
    assert [i for i, (o, r) in enumerate(zip(original, revised)) if o != r] == [3]


@pytest.mark.parametrize("data, out", [
    (b"a\nb\nc\n", "a\nb\nc\n"),
    (b"a\r\nb\r\nc\r\n", "a\r\nb\r\nc\r\n"),
])
def test_b_a_uniform_file_is_untouched_on_both_paths(tmp_path, data, out):
    edited, written = _both_paths(tmp_path, data, "a\nb\nc\n")
    assert edited == out == written


def test_b_an_unterminated_last_line_is_not_glued_to_its_neighbour(tmp_path):
    """`alpha\\nbeta` with no final newline edited to repeat its last line."""
    dest = _write(tmp_path / "nolf.txt", b"alpha\nbeta")
    for reply in ("beta\nalpha\nbeta\n", "beta\nalpha\n"):
        assert _edit_line_ends(str(dest), reply) == reply, repr(reply)
        assert in_style_of(dest, reply) == reply, repr(reply)


@pytest.mark.parametrize("data, reply", [
    (b"def f():\n    return x", "def f():\n    return x\n    return x\n"),
    (b"class A:\n    pass", "class A:\n    pass\n    pass\n"),
    (b"a\n\n", "a\n\n\n"),
])
def test_b_a_repeated_unterminated_last_line_is_not_glued(tmp_path, data, reply):
    dest = _write(tmp_path / "repeat.txt", data)
    assert _edit_line_ends(str(dest), reply) == reply, repr(reply)
    assert in_style_of(dest, reply) == reply, repr(reply)


def test_b_a_legacy_encoding_is_decoded_before_the_match(tmp_path):
    """Both Cyrillic CRs are kept, not flattened to LF."""
    data = "Привет\r\nмир\r\nok\nend\n".encode("cp1251")
    reply = "Привет\nмир\nok2\nend\n"
    expected = "Привет\r\nмир\r\nok2\nend\n"
    edited, written = _both_paths(tmp_path, data, reply)
    assert edited == expected, repr(edited)
    assert written == expected, repr(written)


def test_b_a_bom_does_not_hide_the_first_line(tmp_path):
    dest = _write(tmp_path / "bom.txt", "\ufeffa = 1\r\nb = 2\nc = 3\nd = 4\n".encode("utf-8"))
    expected = "\ufeffa = 1\r\nb = 2\nc = 32\nd = 4\n"
    assert _edit_line_ends(str(dest), "a = 1\nb = 2\nc = 32\nd = 4\n") == expected
    assert in_style_of(dest, "a = 1\nb = 2\nc = 32\nd = 4\n") == expected


def test_b_an_unreadable_destination_is_fail_open(tmp_path):
    missing = str(tmp_path / "absent.txt")
    assert _edit_line_ends(missing, "a\nb\n") == "a\nb\n"
    assert in_style_of(tmp_path / "absent.txt", "a\nb\n") == "a\nb\n"


def test_b_an_empty_destination_is_flat_lf(tmp_path):
    assert in_style_of(_write(tmp_path / "empty.txt", b""), "a\r\nb\r\n") == "a\nb\n"


# ── Part C — `function` followed by a verb names the real symbol ────────────────────


_C_SOURCE_PY = (
    'def handle_request(payload):\n'
    '    """Dispatch a payload."""\n'
    '    if not payload:\n'
    '        return None\n'
    '    data = parse_data(payload)\n'
    '    return data\n'
    '\n'
    '\n'
    'def parse_data(payload):\n'
    '    return payload.decode()\n'
)

_C_SOURCE_CLASS = (
    "class Parser:\n"
    "    def run(self):\n"
    "        return self.text\n"
    '\n'
    '\n'
    "class parse_data:\n"
    "    x = 1\n"
)

_C_SOURCE_JS = (
    "function handle_request(payload) {\n"
    "  return parse_data(payload);\n"
    "}\n"
)


@pytest.mark.parametrize("prompt, source, expected", [
    ("what does the handle_request function return in app.py", _C_SOURCE_PY, "handle_request"),
    ("why does the handle_request function fail in app.py", _C_SOURCE_PY, "handle_request"),
    ("how does the handle_request function work in app.py", _C_SOURCE_PY, "handle_request"),
    ("what does the parse_data class represent in app.py", _C_SOURCE_CLASS, "parse_data"),
    ("tell me what the handle_request function returns in app.py", _C_SOURCE_JS, "handle_request"),
])
def test_c_a_verb_after_the_keyword_is_not_the_symbol(prompt, source, expected):
    assert parse_prompt(prompt, source=source).target_name == expected, prompt


@pytest.mark.parametrize("prompt, source, expected", [
    ("show function handle_request in app.py", _C_SOURCE_JS, "handle_request"),
    ("show def handle_request( in app.py", _C_SOURCE_PY, "handle_request"),
    ("show class Parser in app.py", _C_SOURCE_CLASS, "Parser"),
    ("show def helper in small-fix.py", "", "helper"),
])
def test_c_a_real_declaration_head_still_wins(prompt, source, expected):
    assert parse_prompt(prompt, source=source).target_name == expected, prompt


# ── Part D — removing a task cuts only the task that owns the heading ───────────────


_D_SOURCE = (
    "## Manual Suggestions\n"
    "\n"
    "### AUTO-1: first\n"
    "Auto-1 body, quoting ### AUTO-2: second title mid-line, and then the rest of the sentence.\n"
    "More of AUTO-1.\n"
    "\n"
    "---\n"
    "\n"
    "### AUTO-2: second title\n"
    "This is AUTO-2.\n"
    "\n"
    "---\n"
    "\n"
    "### AUTO-3: third\n"
    "AUTO-3 body.\n"
)

_D_IDS = ["AUTO-1", "AUTO-2", "AUTO-3"]


def test_d_removing_a_task_keeps_an_entry_that_quotes_its_heading():
    out = _without_section(_D_SOURCE, "AUTO-2", _D_IDS)
    assert "This is AUTO-2." not in out
    assert out == (
        "## Manual Suggestions\n"
        "\n"
        "### AUTO-1: first\n"
        "Auto-1 body, quoting ### AUTO-2: second title mid-line, and then the rest of the sentence.\n"
        "More of AUTO-1.\n"
        "\n"
        "---\n"
        "\n"
        "### AUTO-3: third\n"
        "AUTO-3 body.\n"
    )


def test_d_removing_a_task_with_no_quotes_is_unchanged():
    out = _without_section(_D_SOURCE, "AUTO-3", _D_IDS)
    assert "AUTO-3" not in out
    assert "### AUTO-1: first" in out and "### AUTO-2: second title" in out


@pytest.mark.parametrize("ids, target, keep", [
    (["AUTO-1", "AUTO-10", "AUTO-100"], "AUTO-1", ["AUTO-10", "AUTO-100"]),
    (["AUTO-1", "AUTO-10", "AUTO-100"], "AUTO-10", ["AUTO-1", "AUTO-100"]),
    (["AUTO-1", "AUTO-10", "AUTO-100"], "AUTO-100", ["AUTO-1", "AUTO-10"]),
])
def test_d_prefix_sharing_ids_are_still_removed_one_at_a_time(ids, target, keep):
    src = "".join(
        f"### {t}: {t}\n{t} body\n\n---\n\n" if t != ids[-1] else f"### {t}: {t}\n{t} body\n"
        for t in ids
    )
    out = _without_section(src, target, ids)
    for kept in keep:
        assert f"### {kept}: {kept}" in out
    assert f"### {target}: {target}" not in out


# ── Part E — the judge and the export read one ticket's claims the same way ─────────


def _judge():
    spec = importlib.util.spec_from_file_location(
        "_harden20_judge", ROOT / "scripts" / "judge_epic_round.py")
    judge = importlib.util.module_from_spec(spec)
    sys.modules["_harden20_judge"] = judge
    spec.loader.exec_module(judge)
    return judge


def _ticket(heading, nl):
    return (
        f"# Ticket{nl}"
        f"{heading}{nl}"
        f"{nl}"
        f"- [new] the behaviour is there{nl}"
        f"{nl}"
        f"## Acceptance{nl}"
        f"{nl}"
        f"- one{nl}"
    )


_E_HEADINGS = [
    "## Claims", "### Claims", "#### Claims", "##### Claims", "###### Claims", "# Claims",
    "## Claims:", "### Claims:", "#### Claims:", "## Claims (3)", "#### Claims (all)",
    "## CLAIMS", "##Claims", "### Claims Extra words", "## Claim", "## Claims in the wild",
]


@pytest.mark.parametrize("heading", _E_HEADINGS)
@pytest.mark.parametrize("nl", ["\n", "\r\n"])
def test_e_the_judge_and_the_export_agree_on_every_shape(heading, nl):
    text = _ticket(heading, nl)
    judge = _judge()
    try:
        judged = bool(judge.ticket_claims(text, "318-x.md"))
    except ValueError:
        judged = False
    assert export._has_claims_heading(text) is judged, heading


@pytest.mark.parametrize("heading", ["## Claims", "### Claims", "#### Claims"])
def test_e_a_crlf_ticket_gives_the_same_claims_as_lf(heading):
    judge = _judge()
    assert judge.ticket_claims(_ticket(heading, "\n"), "318-x.md") == \
        judge.ticket_claims(_ticket(heading, "\r\n"), "318-x.md")
    assert judge.ticket_claims(_ticket(heading, "\n"), "318-x.md") == [
        {"claim": "the behaviour is there", "expect": "new"}
    ]


def test_e_a_deep_claims_heading_is_announced():
    assert export._has_claims_heading(_ticket("#### Claims", "\n"))


def test_e_a_claims_heading_inside_a_fence_is_neither_judged_nor_announced():
    text = "```\n## Claims\n- [new] a\n```\n"
    assert _judge().ticket_claims(text, "318-x.md") == []
    assert export._has_claims_heading(text) is False


@pytest.mark.parametrize("nl", ["\n", "\r\n"])
def test_e_a_claims_heading_with_no_claim_still_refuses_with_a_line_number(nl):
    with pytest.raises(ValueError, match="318-x.md:1"):
        _judge().ticket_claims(f"## Claims{nl}{nl}", "318-x.md")
