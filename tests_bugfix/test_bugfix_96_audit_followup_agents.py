"""Round 354: what the shell refuses is read as the shell reads it (quotes, comments, parentheses), a KC4 `ok` must be a bool, SLOW-1 sees an async make_config."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.contest import testcache


def _script(rel):
    spec = importlib.util.spec_from_file_location("r354_" + Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Fragments appended to `pytest tests -q `: the shell refuses each, so none is a cached run.
REFUSED = [
    '"$(echo ")"',            # the `)` belongs to the quoted string: the substitution never closes
    '"$(echo "x)"',
    "\"$(echo 'a)\"",
    '"$(echo # )"',           # a comment swallows the closing parenthesis
    '"$(echo $(echo x)"',
    '"a\\"',                  # the quote is escaped, so it never closes
    '"$("',
    '"`"',
    "$(echo",
    "`echo",
    "(pytest tests",          # a subshell that never closes
    "# fine\n'a)\\",          # the comment ends at the line break, the quote after it never closes
]
# Fragments the shell accepts: they stay a cacheable run.
ACCEPTED = [
    '"$(echo ")")"',
    "\"$(echo 'a)')\"",
    '"$(echo $#)"',
    '"${#x}"',
    '"a\\"b"',
    '"\\$(echo"',
    '"\\`echo"',
    '"$(echo $(echo x))"',
    '"$(echo "$(echo x)")"',
    '"$((1+2))"',
    "x # don't break",        # an apostrophe in a comment opens no quote
    "# it's a comment\n",
    "'it'\"'\"'s'",
    "'a\\'",                 # a backslash in single quotes is a backslash: the quote is closed
]


@pytest.mark.parametrize("fragment", REFUSED)
def test_what_the_shell_refuses_is_no_run(fragment):
    command = "pytest tests -q " + fragment
    assert not testcache.is_well_formed(command)
    assert testcache.parse_pytest(command) == []


@pytest.mark.parametrize("fragment", ACCEPTED)
def test_what_the_shell_accepts_is_still_a_run(fragment):
    command = "pytest tests -q " + fragment
    assert testcache.is_well_formed(command)
    assert testcache.parse_pytest(command)


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash as the oracle")
@pytest.mark.parametrize("fragment", REFUSED + ACCEPTED)
def test_bash_agrees_with_both_lists(fragment):
    command = "pytest tests -q " + fragment
    bash_ok = subprocess.run(["bash", "-n", "-c", command], capture_output=True).returncode == 0
    assert bash_ok == (fragment in ACCEPTED)


def test_a_comment_is_not_part_of_the_cache_key():
    plain = testcache.parse_pytest("pytest tests -q")
    assert plain and testcache.parse_pytest("pytest tests -q  # why I run this") == plain


def test_a_hash_inside_a_word_is_not_a_comment():
    assert testcache.is_well_formed("pytest tests -q -k 'a#b' x#y $#")


@pytest.mark.parametrize("result", [{"checks": [{"name": "a", "ok": "yes"}]},
                                    {"checks": [{"name": "a", "ok": None}]},
                                    {"checks": [{"name": "a", "ok": 1}]},
                                    {"checks": [{"name": "a", "ok": float("nan")}]}])
def test_a_kc4_ok_that_is_no_bool_is_a_lost_cell(result):
    mod = _script("contest-bench/kc4/run_all_kc4.py")
    assert not mod._well_formed(result)


def test_a_kc4_good_cell_is_kept_and_an_empty_check_list_stays_a_cell():
    mod = _script("contest-bench/kc4/run_all_kc4.py")
    assert mod._well_formed({"checks": [{"name": "a", "ok": True}, {"name": "b", "ok": False}]})
    assert mod._well_formed({"checks": []})


def test_kc4_batch_survives_ok_as_a_string(tmp_path, monkeypatch, capsys):
    mod = _script("contest-bench/kc4/run_all_kc4.py")
    (tmp_path / "wt" / "entry").mkdir(parents=True)
    out = tmp_path / "out.json"
    monkeypatch.setattr(mod, "SCENARIOS", ["s1", "s2"])
    line = '@@RESULT@@{"checks": [{"name": "a", "ok": "yes"}]}\n'

    class Done:
        stdout, stderr, returncode = line, "", 0
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr(sys, "argv", ["run_all_kc4.py", "--wt", str(tmp_path / "wt"), "--out", str(out)])
    mod.main()
    cells = json.loads(out.read_text())["entry"]
    assert set(cells) == {"s1", "s2"}
    assert all(c["checks"][0]["name"] == "subprocess" and c["checks"][0]["ok"] is False for c in cells.values())


def test_slow1_reads_an_async_make_config():
    mod = _script("contest-bench/slow1/check_slow1.py")
    assert mod.backoff_zero("async def make_config():\n    return Cfg(error_retry_backoff_sec=0)\n") is True
    assert mod.backoff_zero("async def make_config():\n    return Cfg(error_retry_backoff_sec=1)\n") is False
