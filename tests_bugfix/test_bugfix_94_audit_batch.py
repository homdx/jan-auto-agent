"""One guard per finding of the audit batch: a parser that read the wrong marker, a BOM, a cached green for a command the shell refuses."""
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _script(rel):
    spec = importlib.util.spec_from_file_location("audit_" + Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod        # a dataclass in the script looks its module up here
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(mod)
    return mod


def test_untracked_names_reads_the_exporters_comment_and_quoted_names():
    from tools.arena.merge import _untracked_names
    assert _untracked_names("x\n# untracked (not inlined): a.py b.py") == ["a.py", "b.py"]
    assert _untracked_names('x\n# untracked (not inlined): a.py "my file.py"') == ["a.py", "my file.py"]
    assert _untracked_names("x\nUntracked files: a b") == ["a", "b"]
    assert _untracked_names("x\nnothing here") == []


def test_exporters_note_is_the_one_the_merge_reads():
    from tools.arena.merge import _untracked_names
    from tools.contest import cli
    assert _untracked_names("d\n" + cli._UNTRACKED_NOTE + "a.py b.py") == ["a.py", "b.py"]


@pytest.mark.parametrize("reply", ["• •", "• *\n- -", "1. *\n• •\n*  -"])
def test_marker_only_lines_are_no_facts(reply):
    from tools.auto.summary_memory import _clean_bullet_list
    assert _clean_bullet_list(reply) == ""


def test_a_real_fact_behind_two_markers_is_kept_once():
    from tools.auto.summary_memory import _clean_bullet_list
    assert _clean_bullet_list("• • Anna lives in Oslo\n- real") == "• Anna lives in Oslo\n• real"


@pytest.mark.parametrize("command", [
    "python3 -m pytest tests/x.py '", "pytest tests/x.py \"a", "pytest tests/x.py `", "pytest $(echo x"])
def test_a_command_with_an_unclosed_quote_is_no_pytest_run(command):
    from tools.contest.testcache import parse_pytest
    assert parse_pytest(command) == []


def test_closed_quotes_still_parse():
    from tools.contest.testcache import parse_pytest
    assert parse_pytest('pytest "tests/a b.py" -k \'x\'')
    assert parse_pytest('bash -c "pytest tests/x.py"')


def test_an_empty_model_list_is_a_model_list(monkeypatch):
    mod = _script("scripts/py_model_test.py")

    class Reply(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: Reply(b'{"data": []}'))
    assert mod.free_from_direct_api("http://x/v1", "k") == []


def test_next_finding_reads_a_bom_csv(tmp_path):
    mod = _script("scripts/next_finding.py")
    csv_path = tmp_path / "t.csv"
    csv_path.write_bytes(b"\xef\xbb\xbftask_id,truth\nF1,REAL\n")
    assert "F1" in mod.recorded_ids(str(csv_path))


def test_merge_validations_reads_a_bom_csv(tmp_path):
    mod = _script("scripts/merge_validations.py")
    cols = ",".join(mod.COLUMNS)
    p = tmp_path / "rev-a.csv"
    p.write_bytes(b"\xef\xbb\xbf" + (cols + "\n" + ",".join("x" for _ in mod.COLUMNS) + "\n").encode())
    rows, problems = mod.load([str(p)])
    assert problems == [] and len(rows) == 1


def test_a_bom_sibling_ini_still_shows_its_gaps(tmp_path):
    from tools.collect.config_map import diff_sibling_profiles
    a, b = tmp_path / "a.ini", tmp_path / "b.ini"
    a.write_text("[s]\nk1 = 1\nk2 = 2\n", encoding="utf-8")
    b.write_bytes(b"\xef\xbb\xbf[s]\nk1 = 1\n")
    gaps = diff_sibling_profiles(a, {"b": b})
    assert [(g.section, g.key) for g in gaps] == [("s", "k2")]


def test_a_bom_skill_keeps_its_frontmatter(tmp_path):
    from tools.skills.loader import parse_skill_md
    d = tmp_path / "dirname"
    d.mkdir()
    (d / "SKILL.md").write_bytes(b"\xef\xbb\xbf---\nname: real\ndescription: does it\n---\nbody\n")
    doc = parse_skill_md(d / "SKILL.md")
    assert (doc.name, doc.description) == ("real", "does it")


def test_a_non_utf8_profile_is_a_profile_error(tmp_path):
    from tools.arena import profile
    (tmp_path / "contest.ini").write_bytes("[arena.x]\nnote = привет\n".encode("cp1251"))
    with pytest.raises(profile.ProfileError):
        profile.load_profiles(tmp_path)


def test_hunk_text_is_held_to_its_budget():
    from tools.claimcheck.evidence_git import Hunk
    h = Hunk(path="a/very/long/path/to/file.py", old_path="a/very/long/path/to/file.py",
             header=["diff --git a/a/very/long/path/to/file.py b/a/very/long/path/to/file.py",
                     "--- a/a/very/long/path/to/file.py", "+++ b/a/very/long/path/to/file.py"],
             at="@@ -1,3 +1,3 @@", lines=["+one", " two", "-three"], new_start=1, new_count=3)
    for budget in (0, 1, 60, 99):
        assert len(h.text(budget)) <= budget
    assert len(h.text()) > 99


@pytest.mark.parametrize("claim,span,rename", [
    ("a.py   =>    b.py", (0, 4), True),
    ("a.py   =>    b.py", (13, 17), True),
    ("see a.py and b.py", (4, 8), False),
])
def test_rename_anchor_ignores_the_width_of_the_gap(claim, span, rename):
    from tools.claimcheck.anchors import _is_rename
    assert _is_rename(claim, *span) is rename


def test_annotated_clock_assignment_is_seen(tmp_path):
    mod = _script("scripts/check_test_clocks.py")
    body = ("import time\n\nSLOW_WINDOW_S = 3.0\n\ndef test_x():\n    t0 = time.monotonic()\n    work()\n"
            "    elapsed{ann} = time.monotonic() - t0\n    assert elapsed < 5.0 and SLOW_WINDOW_S\n")
    plain, typed = tmp_path / "test_p.py", tmp_path / "test_t.py"
    plain.write_text(body.format(ann=""))
    typed.write_text(body.format(ann=": float"))
    assert len(mod.scan_file(typed)) == len(mod.scan_file(plain)) > 0


@pytest.mark.parametrize("value,zero", [("0", True), ("0.0", True), ("0.5", False), ("0+1", False), ("1", False)])
def test_backoff_zero_means_zero(value, zero):
    mod = _script("contest-bench/slow1/check_slow1.py")
    src = f"def make_config(x):\n    return Cfg(error_retry_backoff_sec={value}, other=1)\n"
    assert mod.backoff_zero(src) is zero


def test_kc42_acceptance_finds_the_repo_root():
    text = (ROOT / "contest-bench/kc42/acceptance_kc42.py").read_text()
    assert "parents[2]" in text
    out = subprocess.run([sys.executable, "-c", "import runpy,sys;"
                          "sys.argv=['x'];m=runpy.run_path('contest-bench/kc42/acceptance_kc42.py',run_name='probe');"
                          "print(m['REPO_ROOT']==__import__('pathlib').Path.cwd(), m['TESTS_DIR'].is_dir())"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.stdout.split() == ["True", "True"], out.stderr[-400:]


def test_missing_tasks_dir_is_no_ticket_not_a_traceback(tmp_path):
    from tools.contest import gates
    assert gates.ticket_for_round(tmp_path / "nope", 3) == (None, None, [])


def test_append_verdict_refuses_a_cp1251_csv_cleanly(tmp_path, capsys, monkeypatch):
    mod = _script("scripts/append_verdict.py")
    out = tmp_path / "truth-bob.csv"
    out.write_bytes(("finding,truth,notes\nF0,REAL,привет\n").encode("cp1251"))
    before = out.read_bytes()
    monkeypatch.setattr(sys, "argv", ["x", "--out", str(out), "--finding", "F1", "--truth", "REAL",
                                      "--how", "ran it", "--evidence", "e", "--consequence", "c", "--severity", "LOW",
                                      "--confidence", "HIGH", "--fix", "f"])
    assert mod.main() == 1
    assert out.read_bytes() == before
    assert "not UTF-8" in capsys.readouterr().err


def test_kc4_runner_survives_a_timeout_and_a_bad_result_line(tmp_path, monkeypatch):
    mod = _script("contest-bench/kc4/run_all_kc4.py")
    wt = tmp_path / "wt"
    for n in ("a", "b"):
        (wt / n).mkdir(parents=True)
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd[-2])
        if cmd[-2].endswith("/a"):
            raise subprocess.TimeoutExpired(cmd, 1)
        if cmd[-1] == mod.SCENARIOS[0]:
            return subprocess.CompletedProcess(cmd, 0, "@@RESULT@@{broken", "")
        return subprocess.CompletedProcess(cmd, 0, '@@RESULT@@{"checks": [{"name": "c", "ok": true}]}', "")

    monkeypatch.setattr(mod.subprocess, "run", fake)
    out = tmp_path / "r.json"
    monkeypatch.setattr(sys, "argv", ["x", "--wt", str(wt), "--out", str(out)])
    mod.main()
    data = json.loads(out.read_text())
    assert set(data) == {"a", "b"}
    assert data["a"][mod.SCENARIOS[0]]["checks"][0]["ok"] is False
    assert data["b"][mod.SCENARIOS[0]]["checks"][0]["ok"] is False
    assert data["b"][mod.SCENARIOS[1]]["checks"][0]["ok"] is True
