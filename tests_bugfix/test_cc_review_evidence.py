"""Review of cc-epic-docs: what the voter is shown reaches the quote check — ticket quotes over lines, a `|`-led code line, files of any case and language, a long body's own lines."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.claimcheck import anchors as cc_anchors  # noqa: E402
from tools.claimcheck import evidence_git, evidence_source  # noqa: E402
from tools.claimcheck.model import Chunk, Pack  # noqa: E402
from tools.claimcheck.pack import find_all_quote  # noqa: E402

TICKET = "# CC-1 — a title\n\n**Status:** open\n\n## Why\n\nthe first line of the reason\nand its second line here\n" \
         + "".join(f"filler line {i} of the why section\n" for i in range(200)) + "## Next\n\nmore\n"


def _ticket_pack(tmp_path):
    (tmp_path / "epic-tasks").mkdir()
    (tmp_path / "epic-tasks" / "1-cc-1.md").write_text(TICKET)
    view = cc_anchors.PathRepoView(tmp_path)
    [ra] = cc_anchors.resolve_anchors(cc_anchors.extract_anchors("ticket epic-tasks/1-cc-1.md says so"), view)
    chunk = evidence_git.ticket_chunk(ra, view, max_chunk_chars=800)
    return Pack("c", "abc1234", (chunk,), False, 0, 0), chunk


def test_a_two_line_quote_of_a_ticket_is_found(tmp_path):
    pack, chunk = _ticket_pack(tmp_path)
    assert " 7| the first line" in chunk.text                       # what the voter sees
    for quote in ("the first line of the reason\nand its second line here",
                  " 7| the first line of the reason\n 8| and its second line here"):
        assert find_all_quote(pack, quote) == [chunk.id]


def test_the_tickets_omission_marker_is_not_a_quoted_line(tmp_path):
    pack, chunk = _ticket_pack(tmp_path)
    marker = next(ln for ln in chunk.text.splitlines() if "the rest of the ticket omitted" in ln)
    assert find_all_quote(pack, marker) == []
    last = [ln for ln in chunk.text.splitlines() if "|" in ln][-1].split("| ", 1)[1]
    assert find_all_quote(pack, last + "\n" + marker) == []      # across the omission: not a quote


def test_a_code_line_that_starts_with_a_pipe_is_quoted_verbatim():
    src = Chunk("src:a.py:1-2", "source", "a.py", 1, 2, " 1| x = (a\n 2|     | b)", ())
    pack = Pack("c", "s", (src,), False, 0, 0)
    assert find_all_quote(pack, "x = (a\n    | b)") == [src.id]
    assert find_all_quote(pack, " 1| x = (a\n 2|     | b)") == [src.id]


def test_files_of_any_case_and_language_are_anchors_that_resolve(tmp_path):
    for name in ("Tests.MD", "App.java", "run.js", "x.cfg"):
        (tmp_path / name).write_text("x\n")
    view = cc_anchors.PathRepoView(tmp_path)
    for name in ("Tests.MD", "App.java", "run.js", "x.cfg"):
        for claim in (f"the file `{name}` says so", f"the file {name} says so"):
            got = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
            assert [(r.anchor.kind, r.found, r.path) for r in got] == [("path", True, name)], claim
            assert cc_anchors.classify(claim, got) == "code"


def test_a_name_of_a_suffix_the_repo_has_none_of_stays_a_world_claim(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    view = cc_anchors.PathRepoView(tmp_path)
    claim = "Node.js runs JavaScript outside a browser."
    got = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
    assert cc_anchors.classify(claim, got) == "world" and not cc_anchors.is_dangling(got)


def test_a_long_body_shows_its_own_lines_when_no_window_is_found(tmp_path):
    body = ["def process_items(items):", '    """Walk the items."""']
    body += [f"    total_{i} = items[{i}] * 2  # check=False handling {i}" for i in range(218)]
    body.append("    return None")
    (tmp_path / "big.py").write_text("\n".join(body) + "\n")
    view = cc_anchors.PathRepoView(tmp_path)
    claim = "`process_items` in big.py handles check=False for every item and returns None"
    resolved = cc_anchors.resolve_anchors(cc_anchors.extract_anchors(claim), view)
    [chunk] = [c for c in evidence_source.source_chunks(resolved, view, claim=claim, max_chunk_chars=2400)
               if c.id.startswith("src:big.py")]
    assert "total_0 = items[0]" in chunk.text            # the body's first lines
    assert "return None" in chunk.text                   # and its last
    assert len(chunk.text) <= 2400 + 200
    assert "lines omitted" in chunk.text
