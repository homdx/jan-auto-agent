"""CC-3: source evidence — symbol bodies with the file's line numbers, keyword windows, collect facts."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors
from tools.claimcheck.evidence_source import (
    MAX_READ_BYTES, collect_chunks, keyword_tokens, keyword_windows, source_chunks,
)
from tools.claimcheck.model import Anchor, Chunk, ResolvedAnchor

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "claimcheck" / "source_golden"


def repo(tmp_path: Path, files: dict) -> PathRepoView:
    for rel, content in files.items():
        full = tmp_path / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            full.write_bytes(content)
        else:
            full.write_text(content, encoding="utf-8", newline="")
    return PathRepoView(tmp_path)


def chunks_for(view, claim: str, **kw) -> list:
    return source_chunks(resolve_anchors(extract_anchors(claim), view), view, claim=claim, **kw)


def numbered(chunk: Chunk) -> dict:
    """line number -> text, as the chunk prints them."""
    out = {}
    for line in chunk.text.split("\n"):
        head, sep, rest = line.partition("| ")
        if sep and head.strip().isdigit():
            out[int(head)] = rest
    return out


def found(kind: str, text: str, path: str, qualname: str = "", lines=(0, 0), candidates=()) -> ResolvedAnchor:
    return ResolvedAnchor(Anchor(kind, text, 0, len(text)), True, path=path, qualname=qualname,
                          lines=lines, candidates=candidates)


MOD = '''"""A module."""

import os
import re


class Gate:
    """A gate."""

    def check(self, text):
        def inner(line):
            return line.strip()
        return [inner(x) for x in text.split("\\n")]


@staticmethod
@property
def decorated(x):
    """Doc of decorated."""
    return x + 1
'''


def test_function_body_exact_lines(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    [chunk] = chunks_for(v, "`m.decorated` adds one.")
    lines = MOD.split("\n")
    assert (chunk.id, chunk.start, chunk.end, chunk.kind) == ("src:m.py:16-20", 16, 20, "source")
    assert numbered(chunk) == {n: lines[n - 1] for n in range(16, 21)}
    assert chunk.text.split("\n")[0] == " 16| @staticmethod"


def test_method_in_class_and_nested_function(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    [method] = chunks_for(v, "`Gate.check` splits on newlines.")
    assert (method.start, method.end) == (10, 13)
    [inner] = chunks_for(v, "`Gate.check.inner` strips the line.")
    assert (inner.start, inner.end) == (11, 12)
    assert numbered(inner)[12] == "            return line.strip()"


def test_decorators_and_docstring_are_included(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    [chunk] = chunks_for(v, "`m.decorated` has a docstring.")
    assert chunk.start == 16 and numbered(chunk)[16] == "@staticmethod"
    assert '    """Doc of decorated."""' in numbered(chunk).values()


def _long_function() -> str:
    body = ['def run_all(steps):', '    """Run every step."""']
    for i in range(1, 199):
        body.append("    out = subprocess.run(steps, check=False)" if i == 120 else f"    x{i} = {i} * 2")
    return "import subprocess\n\n\n" + "\n".join(body) + "\n"


def test_long_body_keeps_signature_and_keyword_windows(tmp_path):
    src = _long_function()
    v = repo(tmp_path, {"r.py": src})
    [chunk] = chunks_for(v, "`run_all` calls subprocess with `check=False`.", max_chunk_chars=1200)
    assert (chunk.start, chunk.end) == (4, 203)
    shown = numbered(chunk)
    assert shown[4] == "def run_all(steps):" and shown[5] == '    """Run every step."""'
    hit = 4 + 1 + 120     # the check=False line
    assert shown[hit] == "    out = subprocess.run(steps, check=False)"
    assert all(n in shown for n in range(hit - 3, hit + 4))
    assert "# … 116 lines omitted (6-121)" in chunk.text      # between the docstring and the window
    assert chunk.text.endswith("# … 75 lines omitted (129-203)")
    assert len(chunk.text) <= 1200


@pytest.mark.parametrize("claim, tokens", [
    ("`config.load_timeout` reads `timeout` from `[defaults]`",
     ["config.load_timeout", "load_timeout", "timeout", "[defaults]"]),
    ("the pattern uses re.M, compares x == 3 and calls run(cmd, check=False) with \"utf-8\"",
     ["re.M", "==", "3", "run", "check=False", "utf-8"]),
    ("it doesn't return -1 when 'abc' < 40", ["-1", "abc", "<", "40"]),
    ("`__init__` sets `self._data` to {}", ["__init__", "self._data", "_data"]),
    ("returns >= 0.5 or != 7", [">=", "0.5", "!=", "7"]),
    ("e.g. a sentence of plain prose, i.e. nothing code-like", []),
    ("", []),
])
def test_keyword_tokens(claim, tokens):
    assert keyword_tokens(claim) == tokens


def test_keyword_tokens_are_linear_on_hostile_text():
    t0 = time.monotonic()
    for hostile in ("a_" * 200_000, "`" * 100_000, '"' * 100_000 + "x", "'a" * 100_000, "1." * 100_000,
                    "x=" * 100_000):
        keyword_tokens(hostile)
    assert time.monotonic() - t0 < 2.0


def test_windows_merge_and_are_capped_at_five():
    lines = [f"line {i}" for i in range(1, 201)]
    for at in (50, 52):                    # two hits two lines apart: one window 47..55
        lines[at - 1] = "x = check_me()"
    assert keyword_windows("\n".join(lines), "`check_me`") == [(47, 55)]
    lines = [f"line {i}" for i in range(1, 201)]
    for k in range(10):                    # ten hits far apart: the first five windows
        lines[10 + 18 * k] = "hit = needle_x"
    got = keyword_windows("\n".join(lines), "`needle_x`")
    assert got == [(8, 14), (26, 32), (44, 50), (62, 68), (80, 86)]


def test_windows_stay_within_the_budget():
    lines = [f"line {i}" for i in range(1, 201)]
    for k in range(10):
        lines[10 + 18 * k] = "hit = needle_x"
    cost = sum(len(lines[i]) + 7 for i in [*range(7, 14), *range(25, 32)])   # 3-digit gutter, "| ", newline
    got = keyword_windows("\n".join(lines), "`needle_x`", budget=cost)
    assert got == [(8, 14), (26, 32)]


def test_windows_prefer_the_rare_token_and_ignore_noise():
    lines = [f"step_{i:03d} = 0" for i in range(1, 101)]
    lines[70] = "done = run(check=False)"
    assert keyword_windows("\n".join(lines), "returns 0 with `check=False`") == [(68, 74)]


def test_claim_without_code_tokens_gives_no_window():
    assert keyword_windows("a\nb\nc\n", "the function is slow") == []


def test_path_anchor_gives_imports_and_definition_list(tmp_path):
    src = "import os\nimport re\n\n\n" + "".join(
        f"def f{i}(x):\n    return x + {i}\n\n\n" for i in range(60)) + "def target_fn(y):\n    return special_marker(y)\n"
    v = repo(tmp_path, {"pkg/big.py": src})
    [chunk] = chunks_for(v, "`pkg/big.py` calls `special_marker`.", max_chunk_chars=1500)
    shown = numbered(chunk)
    assert chunk.id.startswith("src:pkg/big.py:1-") and shown[1] == "import os" and shown[2] == "import re"
    assert "# definitions (name  line):" in chunk.text and "#   f0  5" in chunk.text
    marker_line = src.split("\n").index("    return special_marker(y)") + 1
    assert shown[marker_line] == "    return special_marker(y)"
    assert len(chunk.text) <= 1500


def test_small_path_anchor_is_the_whole_file(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    [chunk] = chunks_for(v, "`m.py` imports re.")
    assert (chunk.start, chunk.end) == (1, 20) and len(numbered(chunk)) == 20


def test_path_line_range_is_plus_minus_three(tmp_path):
    v = repo(tmp_path, {"tools/gates.py": "".join(f"line_{i} = {i}\n" for i in range(1, 101))})
    [chunk] = chunks_for(v, "`tools/gates.py:40-44` sets the lines.")
    assert (chunk.id, chunk.start, chunk.end) == ("src:tools/gates.py:37-47", 37, 47)
    assert sorted(numbered(chunk)) == list(range(37, 48))
    [edge] = chunks_for(v, "`tools/gates.py:2` is near the top.")
    assert (edge.start, edge.end) == (1, 5)
    [tail] = chunks_for(v, "`tools/gates.py:99` is near the end.")
    assert (tail.start, tail.end) == (96, 100)
    assert chunks_for(v, "`tools/gates.py:500` is past the end.") == []


TESTS = '''import pytest


@pytest.fixture
def base(tmp_path):
    return tmp_path


def unrelated():
    return 1


@pytest.fixture(name="store")
def _store(base):
    return {"root": base}


def test_reads(store):
    assert store["root"]
'''


def test_test_anchor_includes_same_file_fixtures(tmp_path):
    v = repo(tmp_path, {"tests/test_s.py": TESTS})
    got = chunks_for(v, "`tests/test_s.py::test_reads` reads the root.")
    assert [c.id for c in got] == ["src:tests/test_s.py:18-19", "src:tests/test_s.py:13-15",
                                   "src:tests/test_s.py:4-6"]
    assert got[1].why == "fixture store" and numbered(got[1])[14] == "def _store(base):"
    assert "unrelated" not in "".join(c.text for c in got)


def test_overlapping_anchors_are_merged(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    got = chunks_for(v, "`Gate` and `Gate.check` split the text.")
    assert [c.id for c in got] == ["src:m.py:7-13"]
    assert got[0].why == "Gate, Gate.check"
    assert list(numbered(got[0])) == list(range(7, 14))      # every line once


def test_property_and_setter_both_come(tmp_path):
    src = ("class C:\n    @property\n    def v(self):\n        return 1\n\n"
           "    @v.setter\n    def v(self, x):\n        raise AttributeError\n")
    v = repo(tmp_path, {"c.py": src})
    got = chunks_for(v, "`C.v` is read-only.")
    assert [c.id for c in got] == ["src:c.py:2-4", "src:c.py:6-8"]


def test_symbol_goes_to_the_file_the_claim_names(tmp_path):
    a = "def _commits_above():\n    return 0\n"
    b = "def _commits_above():\n    raise RuntimeError\n"
    v = repo(tmp_path, {"a/runner.py": a, "b/workspace.py": b})
    got = chunks_for(v, "`_commits_above` in `b/workspace.py` returns 0.")
    assert [c.id for c in got] == ["src:b/workspace.py:1-2"]


class _Model:
    available = True

    def callers_of(self, path, *, exclude_tests=True, limit=0):
        return ["tools/x.py", "tools/y.py"][: limit or None]

    def calls_into(self, path, *, limit=0):
        return []

    def contracts_for(self, name):
        from tools.collect.model import ContractRecord
        if name == "m.py:Gate.check":
            return [ContractRecord(name="no-raise", description="never raises", known_edge=name)]
        return []

    def risk_for(self, path):
        from tools.collect.risk import RiskEntry
        return RiskEntry(path=path, loc=20, blast_radius=3, unguarded_count=1,
                         undocumented_fail_open_count=0, zero_coverage=False, score=7)

    def fail_open_for(self, path):
        return []


def test_collect_chunks_render_facts(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    resolved = resolve_anchors(extract_anchors("`Gate.check` splits."), v)
    got = collect_chunks(resolved, _Model())
    assert [c.id for c in got] == ["collect:callers:m.py", "collect:contracts:m.py", "collect:risk:m.py"]
    assert all(c.kind == "collect" and (c.start, c.end) == (0, 0) for c in got)
    assert got[0].text == "caller: tools/x.py\ncaller: tools/y.py"
    assert got[1].text == "contract: no-raise: never raises"
    assert got[2].text.startswith("risk: score 7, blast_radius 3, loc 20, unguarded 1")
    assert collect_chunks(resolved, _Model()) == got
    both = source_chunks(resolved, v, _Model(), claim="`Gate.check` splits.")
    assert [c.kind for c in both] == ["source", "collect", "collect", "collect"]


def test_absent_collect_model_is_silent(tmp_path):
    from tools.collect.loader import CollectModel

    v = repo(tmp_path, {"m.py": MOD})
    resolved = resolve_anchors(extract_anchors("`Gate.check` splits."), v)
    assert collect_chunks(resolved, CollectModel(status="absent")) == []
    assert collect_chunks(resolved, None) == []

    class Unavailable(_Model):          # stale-and-ignored: data behind it, still not used
        available = False

    assert collect_chunks(resolved, Unavailable()) == []

    class Broken(_Model):
        def callers_of(self, *a, **k):
            raise KeyError("partial artifact")

    assert [c.id for c in collect_chunks(resolved, Broken())][0] == "collect:contracts:m.py"


def test_missing_anchor_yields_nothing(tmp_path):
    v = repo(tmp_path, {"m.py": MOD})
    ra = ResolvedAnchor(Anchor("symbol", "Gate.nope", 0, 9), False, path="m.py")
    assert source_chunks([ra], v, claim="`Gate.nope`") == []
    assert collect_chunks([ra], _Model()) == []
    commit = ResolvedAnchor(Anchor("commit", "abc1234", 0, 7), True, sha="abc1234" * 5)
    assert source_chunks([commit], v) == []      # git evidence is CC-4's


class _Spy(PathRepoView):
    def __init__(self, root):
        super().__init__(root)
        self.read_chars = 0

    def read(self, path):
        text = super().read(path)
        self.read_chars += len(text)
        return text


def test_big_file_is_not_read_whole(tmp_path):
    lines = [f"value_{i} = {i}  # padding padding padding padding padding" for i in range(1, 40_001)]
    (tmp_path / "huge.py").write_text("\n".join(lines) + "\n")
    assert (tmp_path / "huge.py").stat().st_size > 2_000_000
    (tmp_path / "small.py").write_text("x = 1\n")
    v = _Spy(tmp_path)
    claim = "`huge.py:30000` sets value_30000."
    resolved = [found("path", "huge.py:30000", "huge.py", lines=(30000, 30000))]
    [chunk] = source_chunks(resolved, v, claim=claim)
    assert v.read_chars < MAX_READ_BYTES
    assert (chunk.start, chunk.end) == (29997, 30003)
    assert numbered(chunk)[30000].startswith("value_30000 = 30000")
    [head] = source_chunks([found("path", "huge.py", "huge.py")], v, claim="`huge.py`")
    assert head.start == 1 and len(head.text) <= 2400 and v.read_chars < MAX_READ_BYTES


def test_non_utf8_file(tmp_path):
    v = repo(tmp_path, {"b.py": b"def f():\n    return '\xff'\n"})
    [chunk] = source_chunks([found("symbol", "f", "b.py", "f", (1, 2))], v, claim="`f`")
    assert numbered(chunk)[2] == "    return '\ufffd'"


def test_tabs_crlf_and_form_feed_keep_the_files_numbers(tmp_path):
    src = "import os\r\n\x0c\r\ndef f():\r\n\treturn 1\r\n\r\ndef g():\r\n\treturn 2\r\n"
    v = repo(tmp_path, {"t.py": src})
    [chunk] = chunks_for(v, "`t.g` returns 2.")
    assert (chunk.start, chunk.end) == (6, 7)
    assert numbered(chunk) == {6: "def g():", 7: "\treturn 2"}


def test_determinism_and_order(tmp_path):
    v = repo(tmp_path, {"m.py": MOD, "tests/test_s.py": TESTS})
    claim = "`m.decorated` and `tests/test_s.py::test_reads` and `Gate.check`"
    first = chunks_for(v, claim)
    assert first == chunks_for(PathRepoView(tmp_path), claim)
    assert [c.id for c in first][0] == "src:m.py:16-20"
    assert [c.id for c in first][-1] == "src:m.py:10-13"
    swapped = "`Gate.check` and `m.decorated`"
    assert [c.id for c in chunks_for(v, swapped)] == ["src:m.py:10-13", "src:m.py:16-20"]


def test_collect_dir_is_not_evidence(tmp_path):
    """A view that hides a path hides it from the streamed read too."""
    v = repo(tmp_path, {"m.py": MOD})
    (tmp_path / "huge.py").write_text("x = 1  # padding\n" * 40_000)

    class Hiding(PathRepoView):
        def exists(self, path):
            return path not in ("m.py", "huge.py") and super().exists(path)

        def read(self, path):
            if path in ("m.py", "huge.py"):
                raise FileNotFoundError(path)
            return super().read(path)

    assert source_chunks([found("path", "m.py", "m.py")], Hiding(tmp_path), claim="`m.py`") == []
    assert source_chunks([found("path", "huge.py:9", "huge.py", lines=(9, 9))], Hiding(tmp_path)) == []
    assert source_chunks([found("path", "huge.py:9", "huge.py", lines=(9, 9))], v) != []
    assert v.exists("m.py")


def render_golden() -> list:
    """The golden cases as the code renders them today (`PYTHONPATH=. python3 tests/test_claimcheck_source.py --regen`)."""
    view = PathRepoView(GOLDEN / "repo")
    cases = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    out = []
    for case in cases:
        chunks = chunks_for(view, case["claim"], max_chunk_chars=1200)
        out.append({"claim": case["claim"], "chunks": [
            {"id": c.id, "start": c.start, "end": c.end, "why": c.why, "text": c.text} for c in chunks]})
    return out


def test_golden_chunks():
    want = json.loads((GOLDEN / "cases.json").read_text(encoding="utf-8"))
    assert len(want) == 12
    assert render_golden() == want


if __name__ == "__main__" and sys.argv[1:] == ["--regen"]:
    (GOLDEN / "cases.json").write_text(json.dumps(render_golden(), indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")


def test_class_with_forty_methods_keeps_its_head_and_the_named_method(tmp_path):
    methods = "".join(f"    def method_{i:02d}(self):\n        return {i}\n\n" for i in range(40))
    src = f'class Big:\n    """Forty methods."""\n\n{methods}'
    v = repo(tmp_path, {"big.py": src})
    got = chunks_for(v, "`Big` and `Big.method_33` returns 33.", max_chunk_chars=800)
    assert len(got) == 1 and (got[0].start, got[0].end) == (1, 122)
    shown = numbered(got[0])
    assert shown[1] == "class Big:" and shown[2] == '    """Forty methods."""'
    at = 4 + 33 * 3
    assert shown[at] == "    def method_33(self):" and shown[at + 1] == "        return 33"
    assert len(got[0].text) <= 900


def test_a_directory_named_as_a_path_gives_nothing(tmp_path):
    v = repo(tmp_path, {"pkg/m.py": MOD})
    assert source_chunks([found("path", "pkg", "pkg")], v, claim="`pkg/`") == []


def test_windows_and_merging_are_linear(tmp_path):
    lines = [f"x = tok_{i % 4}_y" if i % 4 == 0 else f"z = {i}" for i in range(40_000)]
    started = time.monotonic()
    assert keyword_windows("\n".join(lines), "`tok_0_y`") == [(1, 40_000)]   # dense hits touch: one window
    v = repo(tmp_path, {f"d/f{i}.py": f"v = {i}\n" for i in range(300)})
    claim = " ".join(f"`d/f{i}.py:1`" for i in range(300))
    assert len(chunks_for(v, claim)) == 300
    assert time.monotonic() - started < 5
