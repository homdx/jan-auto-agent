"""tests/test_auto_p7_read_probe.py — AUTO-P7: the `read` op and the
out-of-batch citation marker.

Across six measured runs the largest Gate-1 rejection bucket has been
*"hallucinated the premise"* — the Architect asserting a retry loop exists in
a file that has none; 34 of 52 rejections in the last one. `facts` returns a
signature, `module` returns a name list. Neither returns a single line of
code, so nothing in the protocol let the model check a claim before making
it. `read` is the op that can.

It is also the first op that touches the filesystem rather than the collect
artifact, which brings an obligation the other two never had: a path that
resolves outside the repository must be a miss, never a read. Half this file
is that.

The second half is the marker. The probe hands the Architect knowledge of
files it was never shown, while Gate-1 rejects any `cited_location.file`
outside the batch as a hallucinated path. The two rules contradict each
other; the rejection count doubled between the last two runs as `module`
widened out-of-batch knowledge, and `read` widens it further.

  AC-P7-1   A range read returns those lines, numbered, with a header.
  AC-P7-2   A rangeless read caps at _READ_MAX_LINES and SAYS what it omitted.
  AC-P7-3   Path traversal in every shape is a miss, not a read.
  AC-P7-4   Absent / directory / binary / empty inputs are handled.
  AC-P7-5   No base_dir disables the op entirely.
  AC-P7-6   Range parsing: `p:12-40`, `p:12`, and a colon that is not a range.
  AC-P7-7   An out-of-batch result carries the marker; in-batch does not.
  AC-P7-8   The marker applies to `facts` and `module` results too.
  AC-P7-9   `read` is gated by probe_allowed_ops at parse time, no code change.
  AC-P7-10  A mixed three-op request yields three `by_op` tallies.
  AC-P7-11  `read` shares the per-op cap and the per-batch budget.
  AC-P7-12  An all-miss `read` round returns "" and declines as unresolved.
  AC-P7-13  End-to-end: a read reaches the re-ask prompt and the trace.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.agent_trace import tracer
from tools.auto import arch_probe
from tools.auto.arch_probe import (
    ArchProbe,
    ProbeOp,
    _READ_MAX_LINES,
    extract_probe_request,
)
from tools.auto.architect import ClusterReviewer
from tools.auto.repo_ingest import RepoCluster

_OPS = ("facts", "module", "read")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A small tree with the shapes the op has to survive."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "backoff.py").write_text(
        "\n".join(f"line_{i}" for i in range(1, 201)), encoding="utf-8"
    )
    (tmp_path / "tools" / "small.py").write_text("a\nb\nc\n", encoding="utf-8")
    (tmp_path / "tools" / "empty.py").write_text("", encoding="utf-8")
    (tmp_path / "tools" / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    return tmp_path


def _probe(repo: Path, batch=(), **kw) -> ArchProbe:
    return ArchProbe(None, base_dir=repo, batch_files=batch,
                     max_chars=kw.pop("max_chars", 8000),
                     max_total_chars=kw.pop("max_total_chars", 20000))


# ─────────────────────────────────────────────────────────────────────────────
# Reading
# ─────────────────────────────────────────────────────────────────────────────

class TestReadOp:

    def test_range_read(self, repo) -> None:
        """AC-P7-1"""
        out = _probe(repo)._read("tools/backoff.py:45-48")
        assert "file: tools/backoff.py  lines 45-48 of 200" in out
        assert "   45  line_45" in out
        assert "   48  line_48" in out
        assert "line_49" not in out

    def test_rangeless_read_caps_and_says_so(self, repo) -> None:
        """AC-P7-2: a silently head-truncated file reads as a complete one,
        which is exactly the confident-but-wrong input this op exists to
        prevent."""
        out = _probe(repo)._read("tools/backoff.py")
        assert f"lines 1-{_READ_MAX_LINES} of 200" in out
        assert f"{200 - _READ_MAX_LINES} more line(s) not shown" in out
        assert "ask for a range" in out

    def test_short_file_needs_no_notice(self, repo) -> None:
        """AC-P7-2: nothing omitted, nothing claimed."""
        out = _probe(repo)._read("tools/small.py")
        assert "lines 1-3 of 3" in out
        assert "not shown" not in out

    def test_single_line_range(self, repo) -> None:
        """AC-P7-6"""
        out = _probe(repo)._read("tools/backoff.py:7")
        assert "lines 7-7 of 200" in out
        assert "    7  line_7" in out

    def test_range_clamped_to_file_length(self, repo) -> None:
        out = _probe(repo)._read("tools/small.py:2-999")
        assert "lines 2-3 of 3" in out

    @pytest.mark.parametrize("arg", ["tools/backoff.py:0-2", "tools/backoff.py:50-40"])
    def test_degenerate_ranges(self, repo, arg) -> None:
        out = _probe(repo)._read(arg)
        assert out == "" or "lines 1-" in out


class TestContainment:
    """AC-P7-3. The only op that touches the filesystem gets its own class."""

    @pytest.mark.parametrize("arg", [
        "../outside.txt",
        "../../etc/passwd",
        "/etc/passwd",
        "tools/../../outside.txt",
        "tools/./../../etc/hosts",
        "./../outside.txt",
    ])
    def test_escapes_are_misses(self, repo, arg) -> None:
        """Not an exception, not a partial read — a miss, indistinguishable
        from any other miss to the caller, so the loop's existing
        all-miss handling applies unchanged."""
        assert _probe(repo)._read(arg) == ""

    def test_symlink_out_of_tree_is_a_miss(self, repo, tmp_path) -> None:
        """resolve() collapses the link before the containment check, so a
        symlink cannot be used as a side door."""
        target = tmp_path.parent / "sneaky_target.txt"
        target.write_text("secret\n", encoding="utf-8")
        link = repo / "tools" / "link.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable here")
        assert _probe(repo)._read("tools/link.py") == ""

    def test_no_base_dir_disables_the_op(self, repo) -> None:
        """AC-P7-5: an unconstrained read is not a degraded feature, it is a
        hole. No root means no reads at all."""
        p = ArchProbe(None, base_dir=None, max_chars=8000)
        assert p._read("tools/backoff.py:1-2") == ""


class TestReadEdgeCases:
    """AC-P7-4"""

    @pytest.mark.parametrize("arg", [
        "tools/nope.py", "tools", "", "   ", "tools/blob.bin",
    ])
    def test_misses(self, repo, arg) -> None:
        assert _probe(repo)._read(arg) == ""

    def test_empty_file_is_not_a_miss(self, repo) -> None:
        """The file exists and is empty — a different fact from "no such
        file", and the two must not be conflated any more than they are for
        `module`."""
        out = _probe(repo)._read("tools/empty.py")
        assert out != ""
        assert "(empty file)" in out


class TestRangeParsing:
    """AC-P7-6, at the unit level."""

    @pytest.mark.parametrize("raw,expect", [
        ("a/b.py", ("a/b.py", None, None)),
        ("a/b.py:12", ("a/b.py", 12, 12)),
        ("a/b.py:12-40", ("a/b.py", 12, 40)),
        ("a/b.py:12 - 40", ("a/b.py", 12, 40)),
        ("weird:name.py", ("weird:name.py", None, None)),
        (":12", (":12", None, None)),
    ])
    def test_parse(self, raw, expect) -> None:
        assert ArchProbe._parse_read_arg(raw) == expect


# ─────────────────────────────────────────────────────────────────────────────
# Out-of-batch marker
# ─────────────────────────────────────────────────────────────────────────────

class TestOutOfBatchMarker:

    def test_marks_a_file_outside_the_batch(self, repo) -> None:
        """AC-P7-7: two rejections of this exact shape in the last run, and
        the count doubled when `module` widened out-of-batch knowledge."""
        p = _probe(repo, batch=["tools/small.py"])
        out = p.execute([ProbeOp("read", "tools/backoff.py:1-2")])
        assert "NOT IN YOUR BATCH" in out
        assert "Gate-1 will reject it" in out

    def test_in_batch_result_is_unmarked(self, repo) -> None:
        """AC-P7-7: the marker must not become noise on every result."""
        p = _probe(repo, batch=["tools/backoff.py"])
        out = p.execute([ProbeOp("read", "tools/backoff.py:1-2")])
        assert "NOT IN YOUR BATCH" not in out

    def test_no_batch_information_means_no_marker(self, repo) -> None:
        """Marking everything when the batch is unknown would be worse than
        marking nothing."""
        p = _probe(repo, batch=())
        assert "NOT IN YOUR BATCH" not in p.execute(
            [ProbeOp("read", "tools/backoff.py:1-2")]
        )

    def test_marker_applies_to_module_results(self, repo) -> None:
        """AC-P7-8: the existing rejections came from `module`, not `read`."""
        class _B:
            usable = True
            def pull_symbol(self, n): return ""
            def module_symbols(self, r):
                return "module: tools/other.py\n  thing(...) :3"
        p = ArchProbe(_B(), base_dir=repo, batch_files=["tools/small.py"],
                      max_chars=4000, max_total_chars=9000)
        out = p.execute([ProbeOp("module", "tools/other.py")])
        assert "NOT IN YOUR BATCH" in out

    def test_marker_warns_against_re_asking(self, repo) -> None:
        """AUTO-F1-followup: a measured run (trace_8c83140453d5) found EVERY
        'repeat' decline in it was a re-ask of an out-of-batch HIT — the
        model already had the answer and asked again anyway. The marker used
        to warn only about citation, never about asking twice; a re-ask
        costs a full architect round-trip before the repeat-detector
        declines it, for zero new information."""
        p = _probe(repo, batch=["tools/small.py"])
        out = p.execute([ProbeOp("read", "tools/backoff.py:1-2")])
        assert "will end your probing" in out


# ─────────────────────────────────────────────────────────────────────────────
# Integration with the existing machinery
# ─────────────────────────────────────────────────────────────────────────────

class TestWiring:

    def test_allow_list_gates_read_at_parse_time(self) -> None:
        """AC-P7-9: same property `module` demonstrated — no parser change."""
        assert extract_probe_request("ARCH_PROBE: read tools/x.py:1-2") == []
        assert extract_probe_request(
            "ARCH_PROBE: read tools/x.py:1-2", allowed_ops=("facts", "module")
        ) == []
        assert extract_probe_request(
            "ARCH_PROBE: read tools/x.py:1-2", allowed_ops=_OPS
        ) == [ProbeOp("read", "tools/x.py:1-2")]

    def test_mixed_three_op_request(self, repo) -> None:
        """AC-P7-10: the per-op tallies AUTO-P6 made trustworthy have to keep
        working with a third op in play — that breakdown is what the next
        scope decision rests on."""
        class _B:
            usable = True
            def pull_symbol(self, n): return "signature: f()" if n == "f" else ""
            def module_symbols(self, r): return ""
        p = ArchProbe(_B(), base_dir=repo, max_chars=8000, max_total_chars=20000)
        ops = extract_probe_request(
            "ARCH_PROBE: facts f, module tools/nope.py, read tools/small.py:1-2",
            allowed_ops=_OPS,
        )
        assert len(ops) == 3
        p.execute(ops)
        assert p.last_by_op == {"facts": [1, 0], "module": [0, 1], "read": [1, 0]}
        assert p.last_by_op_str() == "facts=1/0 module=0/1 read=1/0"

    def test_read_respects_the_per_op_cap(self, repo) -> None:
        """AC-P7-11"""
        p = _probe(repo, max_chars=300, max_total_chars=4000)
        out = p.execute([ProbeOp("read", "tools/backoff.py")])
        assert "truncated" in out
        assert p.last_hits == 1

    def test_all_miss_read_round_is_empty(self, repo) -> None:
        """AC-P7-12: AUTO-P4b behaviour, unchanged by the new op."""
        p = _probe(repo)
        assert p.execute([ProbeOp("read", "tools/nope.py")]) == ""
        assert p.last_by_op == {"read": [0, 1]}

    def test_instructions_teach_the_op_and_demand_a_range(self) -> None:
        """The op is useless if the model is never told it exists, and
        dangerous if it is not told to use a range — the last run already
        overshot its digest budget with the two cheaper ops."""
        p = arch_probe.PROBE_INSTRUCTIONS
        assert "read <path>:<start>-<end>" in p
        assert "ALWAYS give a range" in p


# ─────────────────────────────────────────────────────────────────────────────
# End to end
# ─────────────────────────────────────────────────────────────────────────────

def _cfg() -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "architect": {"temperature": "0.2", "max_tokens": "512",
                      "probe_enabled": "true", "probe_max_rounds": "1",
                      "probe_allowed_ops": "facts, module, read",
                      "retry_delays_sec": ""},
        "loop": {"timeout_seconds": "10"},
    })
    return c


def _good(title: str) -> str:
    return json.dumps([{
        "title": title, "instruction": "Do it.",
        "target_files": ["tools/small.py"],
        "acceptance_check": "pytest tests/",
        "cited_location": {"file": "tools/small.py", "symbol": None,
                           "line_start": 1, "line_end": 1},
    }])


def test_read_result_reaches_the_reask(repo, tmp_path) -> None:
    """AC-P7-13: the whole point — the Architect verifies a premise instead
    of asserting one."""
    cluster = RepoCluster(name="agents", patterns=["tools/*"],
                          files=["tools/small.py"])
    r = ClusterReviewer(config=_cfg(), base_url="http://localhost:1337/v1",
                        api_key="t", model="m", api_format="openai",
                        verify_ssl=False)
    r._probe_built = True
    r._probe = ArchProbe(None, base_dir=repo, max_chars=8000,
                         max_total_chars=20000)

    tp = tmp_path / "trace.jsonl"
    tracer.configure(enabled=True, path=str(tp), console_echo=False)
    try:
        with patch(
            "tools.llm_stream.request_completion",
            side_effect=["ARCH_PROBE: read tools/backoff.py:10-12",
                         _good("Grounded")],
        ) as mock_llm:
            results = r.review_clusters([cluster], repo, goal="g")
    finally:
        tracer.configure(enabled=False)

    payload = mock_llm.call_args_list[1].kwargs.get("payload") or \
        mock_llm.call_args_list[1].args[2]
    reask = next(m["content"] for m in payload["messages"] if m["role"] == "user")
    assert "file: tools/backoff.py  lines 10-12" in reask
    assert "   10  line_10" in reask
    # tools/backoff.py is not in this batch (tools/small.py is), so the model
    # must be told not to cite it.
    assert "NOT IN YOUR BATCH" in reask
    assert [x.title for x in results] == ["Grounded"]

    ev = [json.loads(l) for l in tp.read_text().splitlines() if l.strip()]
    res = next(e for e in ev if e.get("kind") == "probe_result")
    assert res["params"]["by_op"] == "read=1/0"
