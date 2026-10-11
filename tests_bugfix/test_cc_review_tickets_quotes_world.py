"""Review of cc-epic-docs: ticket ids resolve to their own ticket, a quoted path with a space is one anchor, a deleted file's hunk quotes, a world fact is no defect, the stage-3 readers take a BOM."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
import next_pending  # noqa: E402
from tools.claimcheck import pack as cc_pack  # noqa: E402
from tools.claimcheck.anchors import PathRepoView, extract_anchors, resolve_anchors  # noqa: E402
from tools.claimcheck.model import Chunk, Pack  # noqa: E402

BOM = "﻿"


def _tree(tmp_path, files):
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    return PathRepoView(str(tmp_path))


def test_a_prefixed_id_finds_its_ticket_in_the_epic_folder_not_a_number_namesake(tmp_path):
    view = _tree(tmp_path, {"epic-tasks/09-v6-wire-the-budget.md": "# V6\n",
                            "epic-tasks/03-other.md": "# something else\n",
                            "docs/claim-check/tickets/291-cc-9-lenz-hint.md": "# CC-9\n"})
    assert view.ticket_file("CC-9") == "docs/claim-check/tickets/291-cc-9-lenz-hint.md"
    assert view.ticket_file("AUTO-3") is None                  # was epic-tasks/03-other.md
    assert view.ticket_file("ticket 9") == "epic-tasks/09-v6-wire-the-budget.md"   # a bare number


def test_a_prefixed_id_by_number_when_the_file_names_it(tmp_path):
    view = _tree(tmp_path, {"epic-tasks/07-something.md": "# KC-7\n", "epic-tasks/08-x.md": "# x\n"})
    assert view.ticket_file("KC-7") == "epic-tasks/07-something.md"
    assert view.ticket_file("KC-8") is None


def test_a_quoted_path_with_a_space_is_one_anchor_and_resolves(tmp_path):
    view = _tree(tmp_path, {"dir with space/a b.py": "x = 1\n"})
    for claim in ("`dir with space/a b.py` sets x", 'the file "dir with space/a b.py" sets x'):
        anchors = [a for a in extract_anchors(claim) if a.kind == "path"]
        assert [a.text for a in anchors] == ["dir with space/a b.py"], claim
        assert resolve_anchors(anchors, view)[0].found
    assert [a.text for a in extract_anchors("the file's config.py's value") if a.kind == "path"] == ["config.py"]
    assert [a.text for a in extract_anchors("see `tools/a.py` and b.py") if a.kind == "path"] == ["tools/a.py", "b.py"]


def test_two_lines_of_a_deleted_files_hunk_are_a_quote():
    text = ("diff --git a/d.py b/d.py\ndeleted file mode 100644\n--- a/d.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n  | -def gone_function():\n  | -    return 'the old value'")
    chunk = Chunk("diff:a..b:d.py:0-0", "git", "d.py", 0, 0, text, ())
    pack = Pack("c", "s", (chunk,), False, 0, 0)
    assert cc_pack.find_quote(pack, "-def gone_function():\n-    return 'the old value'") == chunk.id


def _truth(tmp_path, rows):
    p = tmp_path / "truth.csv"
    p.write_text(BOM + "finding,truth,checked_by,duplicate_of,how\n" + "".join(r + "\n" for r in rows),
                 encoding="utf-8")
    return p


def test_make_jira_tasks_files_no_world_fact_and_reads_a_bom(tmp_path):
    truth = _truth(tmp_path, ["claim:1a2b3c4d,REAL,cc,,world fact",
                              'tools/a.py::f,REAL,cc,,"src:tools/a.py:1-3; ""return None"""'])
    out = tmp_path / "tasks"
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "make_jira_tasks.py"), "--truth", str(truth),
                        "--out", str(out)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    tickets = sorted(f.name for f in out.iterdir() if f.name != "INDEX.md")
    assert len(tickets) == 1 and "skipped 1 world-fact row" in p.stderr
    assert "**File:** `tools/a.py`" in (out / tickets[0]).read_text()


def test_next_pending_and_append_verdict_read_a_bom(tmp_path):
    truth = _truth(tmp_path, ["F1,REAL,cc,,x"])
    assert next_pending.decided(str(truth)) == {"F1"}
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "append_verdict.py"), "--out", str(truth),
                        "--finding", "F1", "--truth", "REAL", "--checked-by", "truth", "--how", "h",
                        "--evidence", "a.py:1 x = 1", "--consequence", "x is wrong"],
                       capture_output=True, text=True, timeout=60, env={**os.environ})
    assert p.returncode == 2 and "already decided" in p.stderr, p.stderr


def test_claim_vote_reads_the_object_form_and_refuses_another_shape(tmp_path, monkeypatch, capsys):
    import json

    import claim_vote as cv
    seen = []
    monkeypatch.setattr(cv, "ask", lambda ref, claims, run, *a, **k:
                        seen.append(list(claims)) or {"model": ref, "run": run, "votes": {0: "TRUE"}})
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"set": "fixture", "base_sha": "abc", "claims": [{"claim": "the sky is blue"}]}))
    assert cv.main([str(f), "--models", "a/x", "--runs", "1", "--interval", "0",
                    "--repo-root", str(tmp_path)]) == 0
    assert seen == [["the sky is blue"]]                       # was: set, base_sha, claims
    f.write_text(json.dumps({"set": "fixture"}))
    import pytest
    with pytest.raises(SystemExit) as exc:
        cv.main([str(f), "--models", "a/x", "--repo-root", str(tmp_path)])
    assert exc.value.code == 2 and "not a claims file" in capsys.readouterr().err


def test_truth_writes_keep_the_files_mode(tmp_path):
    from tools.claimcheck import truth
    p = tmp_path / "truth.csv"
    p.write_text("finding,truth,checked_by,duplicate_of,how\nF1,REAL,a,,x\n")
    p.chmod(0o644)
    truth.write_truth(p, [{"finding": "F2", "truth": "FALSE", "checked_by": "b", "duplicate_of": "", "how": "y"}],
                      append=True)
    assert p.stat().st_mode & 0o777 == 0o644                  # was 0o600 (mkstemp)
    new = tmp_path / "new.csv"
    old = os.umask(0o022)
    try:
        truth.write_truth(new, [])
    finally:
        os.umask(old)
    assert new.stat().st_mode & 0o777 == 0o644


def test_a_line_past_the_end_of_the_file_is_a_note(tmp_path):
    view = _tree(tmp_path, {"a.py": "x = 1\ny = 2\n"})
    claim = "a.py:900 sets x"
    pack = cc_pack.build_pack(claim, resolve_anchors(extract_anchors(claim), view), view)
    notes = [c for c in pack.chunks if c.id == "note:range:a.py:900"]
    assert notes and "ends at line 2" in notes[0].text
