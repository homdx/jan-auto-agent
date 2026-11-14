"""AUDIT-9 follow-up (360): interactive /edit keeps a literal think tag inside the file it writes.

`_edit_file_content` used to run strip_think() over the whole reply, and the reply was the file, so a tag
written in a source string, a comment, a Markdown example or a template was thrown away and /edit wrote the
damaged text. The replacement now travels as the `content` string of a JSON object; only an outer reasoning
wrapper is dropped at the response boundary. Only the model transport and the validator are stubbed here.

The tags are spelled with escapes (OPEN, CLOSE): this module is also read by tools that treat a tag as reasoning.
"""

import json

import pytest

import tools.actions as A

OPEN = "\x3cthink>"
CLOSE = "\x3c/think>"
WRAPPER = f"{OPEN}let me think about the values first{CLOSE}\n"
ORIGINAL = "before = 1\nPLACEHOLDER = 0\nafter = 2\n"


def _file(line: str) -> str:
    return f"before = 1\n{line}\nafter = 2\n"


class Editor:
    """The real run_edit and _edit_file_content, with the model transport and the validator stubbed."""

    def __init__(self, monkeypatch, tmp_path, original=ORIGINAL, name="sample.py", original_bytes=None):
        act = A.OrchestratorActions.__new__(A.OrchestratorActions)
        act.max_iterations, act.timeout_seconds, act.stream_agents = 3, 60, False
        act.model, act.api_format, act.ssl_context = "stub", "openai", None
        act._chat_url = lambda *a, **k: "http://stub/v1/chat/completions"
        act._headers = lambda *a, **k: {}
        act._cfg_temp = lambda *a, **k: 0.0
        self.act, self.dir, self.name = act, tmp_path, name
        self.path = tmp_path / name
        self.path.write_bytes(original_bytes if original_bytes is not None else original.encode("utf-8"))
        self.before = self.path.read_bytes()
        self.calls, self.prompts, self.validated = [], [], []
        self.replies = []
        act._validate_edit = self._validate
        monkeypatch.setattr(A, "request_completion", self._request)
        monkeypatch.setattr("time.sleep", lambda *a, **k: None)
        monkeypatch.setattr("tools.backoff.api_error_pause", lambda *a, **k: None)

    def _request(self, url, headers, payload, timeout, *a, **kw):
        self.calls.append(1)
        self.prompts.append(payload["messages"])
        assert len(self.calls) <= 12, "the editor retries without a bound"
        return self.replies[min(len(self.calls), len(self.replies)) - 1]

    def _validate(self, instruction, original, revised, *a, **kw):
        self.validated.append(revised)
        return {"status": "approved"}

    def run(self, *replies):
        self.replies = list(replies)
        self.act.run_edit(f"/edit {self.name} :: update the values", str(self.dir))
        return self

    @property
    def saved(self) -> bytes:
        return self.path.read_bytes()

    @property
    def backups(self):
        return list(self.dir.glob(self.name + ".bak.*"))


def reply(content: str) -> str:
    return json.dumps({"content": content})


# ── a literal tag inside a valid replacement survives, with the lines around it ──────────────────────

LITERALS = [
    f'PROMPT = "{OPEN}keep{CLOSE}"',
    f'OPEN = "{OPEN}"',
    f'CLOSE = "{CLOSE}"',
    f'DOC = """\n{OPEN}\nkeep this\n{CLOSE}\n"""',
    f'# the model may emit {OPEN}...{CLOSE} blocks',
    f'TEMPLATE = "{OPEN}{{reason}}{CLOSE}\\n{{answer}}"',
    f'ARROW = "a -> b"  # {CLOSE} > {OPEN}',
]


@pytest.mark.parametrize("line", LITERALS)
def test_a_literal_tag_in_the_replacement_reaches_the_validator_and_the_disk(monkeypatch, tmp_path, line):
    revised = _file(line)
    ed = Editor(monkeypatch, tmp_path).run(reply(revised))
    assert ed.validated == [revised]
    assert ed.saved == revised.encode("utf-8")
    assert len(ed.backups) == 1 and ed.backups[0].read_bytes() == ed.before


@pytest.mark.parametrize("revised", [
    f"{OPEN}keep{CLOSE} is documented here\nsecond\n",
    f"{OPEN}\nsecond\n",
    f"{CLOSE}\nsecond\n",
])
def test_a_file_that_begins_with_a_literal_tag_is_written_whole(monkeypatch, tmp_path, revised):
    ed = Editor(monkeypatch, tmp_path, original="old line\nsecond\n", name="notes.md").run(reply(revised))
    assert ed.validated == [revised]
    assert ed.saved == revised.encode("utf-8")


# ── a genuine outer reasoning wrapper is dropped, the literal inside the file survives ───────────────

@pytest.mark.parametrize("wrapper", [
    WRAPPER,
    f"{OPEN}\nline one\nline two\n{CLOSE}\n\n",
    f'{OPEN}maybe {{"content": "draft"}} first, then the real one{CLOSE}\n',
])
@pytest.mark.parametrize("fence", [False, True])
def test_an_outer_wrapper_is_absent_from_the_file_and_the_inner_literals_survive(monkeypatch, tmp_path, wrapper, fence):
    revised = _file(f'PROMPT = "{OPEN}keep{CLOSE}"\nA = b -> c\nD = 1 > 0')
    body = f"```json\n{reply(revised)}\n```" if fence else reply(revised)
    ed = Editor(monkeypatch, tmp_path).run(wrapper + body)
    assert ed.validated == [revised]
    assert ed.saved == revised.encode("utf-8")
    assert "let me think" not in ed.saved.decode("utf-8")


def test_a_wrapper_whose_opening_tag_the_provider_already_took_is_dropped(monkeypatch, tmp_path):
    revised = _file(f'PROMPT = "{OPEN}keep{CLOSE}"')
    ed = Editor(monkeypatch, tmp_path).run(f"I will think first.\n{CLOSE}\n" + reply(revised))
    assert ed.saved == revised.encode("utf-8")


# ── an unusable reply leaves the file byte-identical, makes no backup and stops ──────────────────────

MALFORMED = {
    "truncated": '{"content": "before = 1\\nPROMPT = \\"',
    "no content key": '{"text": "before = 1\\n"}',
    "content not a string": '{"content": 42}',
    "content is null": '{"content": null}',
    "empty": "",
    "only whitespace": "  \n",
    "reasoning only": f"{OPEN}nothing to write{CLOSE}",
    "unclosed reasoning": f"{OPEN}reasoning that never ends and no file",
    "reasoning around an object only": f'{OPEN}{{"content": "draft"}}{CLOSE}',
    "prose only": "Sure, I updated the values.",
    "a bare array": '["before = 1"]',
    "array containing a replacement": '[{"content": "Draft"}]',
    "truncated outer object": '{"draft": {"content": "Draft"}',
    "two replacements": '{"content": "Draft"}\n{"content": "Final"}',
    "draft before a closing-only wrapper": '{"content": "Draft"}\n' + CLOSE + '\n{"content": "Final"}',
    "unclosed fence": '```json\n{"content": "Draft"}',
    "nested fence": '```json\n```json\n{"content": "Draft"}\n```\n```',
    "extra key": '{"content": "Draft", "complete": false}',
    "duplicate content": '{"content": "Draft", "content": "Final"}',
    "unframed commentary": 'Here is the file:\n{"content": "Draft"}\nDone.',
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_reply_leaves_the_file_untouched_and_the_retry_is_bounded(monkeypatch, tmp_path, case, capsys):
    ed = Editor(monkeypatch, tmp_path).run(MALFORMED[case])
    assert ed.saved == ed.before
    assert ed.backups == []
    assert ed.validated == []
    assert 1 <= len(ed.calls) <= 4, len(ed.calls)
    assert "FILE EDITED" not in capsys.readouterr().out


def test_the_diagnostic_names_the_problem(monkeypatch, tmp_path, capsys):
    Editor(monkeypatch, tmp_path).run(MALFORMED["truncated"])
    out = capsys.readouterr().out.lower()
    assert "json" in out and "unchanged" in out


def test_a_usable_reply_after_a_malformed_one_is_written(monkeypatch, tmp_path):
    good = _file(f'PROMPT = "{OPEN}keep{CLOSE}"')
    ed = Editor(monkeypatch, tmp_path).run(f"{OPEN}unfinished", reply(good))
    assert ed.saved == good.encode("utf-8")
    assert ed.validated == [good]
    assert len(ed.backups) == 1 and len(ed.calls) == 2


# ── the prompts name the same boundary on every attempt ──────────────────────────────────────────────

def _text(messages) -> str:
    return json.dumps(messages).lower()


def test_the_first_prompt_asks_for_the_content_boundary(monkeypatch, tmp_path):
    ed = Editor(monkeypatch, tmp_path).run(reply(_file("VALUE = 7")))
    first = _text(ed.prompts[0])
    assert "json" in first and "content" in first


def test_a_retry_after_a_rejected_edit_repeats_the_boundary(monkeypatch, tmp_path):
    first, second = _file("VALUE = 7"), _file("VALUE = 8")
    ed = Editor(monkeypatch, tmp_path)
    verdicts = iter([{"status": "needs_fix", "feedback": "use 8"}, {"status": "approved"}])
    ed.act._validate_edit = lambda instruction, original, revised, *a, **k: (ed.validated.append(revised), next(verdicts))[1]
    ed.run(reply(first), reply(second))
    assert ed.validated == [first, second]
    assert ed.saved == second.encode("utf-8")
    retry = _text(ed.prompts[1][-1:])  # the correction turn
    assert "json" in retry and "content" in retry


# ── the rest of /edit is unchanged ───────────────────────────────────────────────────────────────────

def test_a_plain_edit_is_written_with_a_backup_of_the_original(monkeypatch, tmp_path):
    revised = _file("VALUE = 7")
    ed = Editor(monkeypatch, tmp_path).run(reply(revised))
    assert ed.saved == revised.encode("utf-8")
    assert [b.read_bytes() for b in ed.backups] == [ORIGINAL.encode("utf-8")]


def test_a_bom_and_crlf_file_keeps_both(monkeypatch, tmp_path):
    original = b"\xef\xbb\xbfa = 1\r\nb = 2\r\n"
    ed = Editor(monkeypatch, tmp_path, original_bytes=original).run(reply("a = 1\nb = 3\n"))
    assert ed.saved.startswith(b"\xef\xbb\xbf") and b"b = 3" in ed.saved and b"\r\n" in ed.saved


# ── the parser on its own ────────────────────────────────────────────────────────────────────────────

def PARSE(raw):
    return A.OrchestratorActions._parse_edit_reply(raw)


def test_the_parser_returns_the_content_verbatim():
    content = f'a = "{OPEN}x{CLOSE}"\n\t{CLOSE}\n'
    assert PARSE(reply(content)) == (content, None)
    assert PARSE(WRAPPER + reply(content)) == (content, None)
    assert PARSE(f"```json\n{reply(content)}\n```") == (content, None)


def test_the_parser_refuses_what_it_cannot_classify():
    for raw in (None, "", f"{OPEN}no end", "plain text", '{"content": 1}', '{"content": "x'):
        content, error = PARSE(raw)
        assert content is None and error, raw


def test_the_parser_refuses_a_deeply_nested_truncated_reply():
    content, error = PARSE('{"a": ' * 5000)
    assert content is None and error
