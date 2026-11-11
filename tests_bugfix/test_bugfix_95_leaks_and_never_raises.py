"""Five leaks and escapes: a registry that only grows, an editor folder left behind, a probe directory made outside its try, a runs/ clean-up that aborts the round, a budget write that raises out of run_task."""
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _git(cwd, *words):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *words],
                   capture_output=True, text=True, check=True)


def test_pruning_a_workspace_drops_its_mark(tmp_path):
    from tools.auto.executor import Executor
    source = tmp_path / "source"
    source.mkdir()
    (source / "f.txt").write_text("x", encoding="utf-8")
    _git(source, "init", "-q")
    _git(source, "add", "-A")
    _git(source, "commit", "-qm", "one")
    root = tmp_path / "ws" / "workspace"
    ex = Executor(source, workspace_root=root, max_retained_workspaces=1)
    ex._prepare_workspace("T1", [])
    ex._prepare_workspace("T2", [])
    ex._prune_old_workspaces(keep=root / "T2")
    assert not (root / "T1").exists()
    marks = json.loads((root.parent / ".agent-workspaces.json").read_text(encoding="utf-8"))
    assert marks == [str((root / "T2").resolve())]


def test_a_mark_whose_directory_is_gone_is_dropped_too(tmp_path):
    from tools.auto.executor import Executor
    source = tmp_path / "source"
    source.mkdir()
    ex = Executor(source, workspace_root=tmp_path / "ws" / "workspace", max_retained_workspaces=3)
    live = tmp_path / "ws" / "workspace" / "T9"
    live.mkdir(parents=True)
    ex._mark_workspace(live)
    ex._mark_workspace(tmp_path / "ws" / "workspace" / "T1")
    ex._forget_workspaces(set())
    assert ex._read_workspace_registry() == {str(live.resolve())}


def test_editor_folder_is_removed_when_nothing_is_kept(tmp_path):
    from tools.arena.tickets import _drop_editor_file, _editor_path
    path = _editor_path(tmp_path, "ticket.md")
    path.write_text("x", encoding="utf-8")
    _drop_editor_file(path)
    assert list((tmp_path / ".arena" / "tmp").iterdir()) == []
    _drop_editor_file(path)           # twice is not an error


def test_issue_edit_with_no_change_leaves_no_folder(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "tests"))
    import test_arena_issue_status as t
    repo = t.repo.__wrapped__(tmp_path, monkeypatch) if hasattr(t.repo, "__wrapped__") else None
    if repo is None:
        pytest.skip("fixture not callable directly")
    t._editor(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    code, out, _ = t._run(capsys, "issue", "edit", str(t.NN))
    assert out.endswith("no change\n")
    assert list((repo / ".arena" / "tmp").iterdir()) == []


def test_probe_directory_is_removed_when_the_client_cannot_be_built(monkeypatch):
    import tempfile
    from tools.contest import kilo_client, variant
    made = []
    real = tempfile.mkdtemp

    def mk(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d
    monkeypatch.setattr(tempfile, "mkdtemp", mk)

    def boom(*a, **k):
        raise RuntimeError("no client")
    monkeypatch.setattr(kilo_client, "KiloClient", boom)
    try_one = variant.hello_probe(SimpleNamespace(base_url="http://x"), "p", "m")
    with pytest.raises(RuntimeError):
        try_one(None)
    assert made and not any(Path(d).exists() for d in made)


def test_a_run_file_that_cannot_be_removed_does_not_abort_the_round(tmp_path, monkeypatch):
    from tools.contest import workspace
    runs = tmp_path / "runs" / "a"
    runs.mkdir(parents=True)
    (runs / "stuck").write_text("x", encoding="utf-8")
    (runs / "other").write_text("y", encoding="utf-8")
    real = Path.unlink

    def unlink(self, *a, **k):
        if self.name == "stuck":
            raise PermissionError("held")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    workspace._empty_runs_dir(tmp_path, "a")
    assert not (runs / "other").exists() and (runs / "stuck").exists()


@pytest.mark.parametrize("field", ["consumed_s", "credit_s"])
def test_a_non_numeric_budget_field_never_raises_out_of_run_task(field):
    from tools.auto.outer_loop import OuterLoop, _TaskBudget
    budget = _TaskBudget(max_seconds=60.0, session_started_at=time.time() - 1)
    setattr(budget, field, "oops")
    state = SimpleNamespace(write_task_file=lambda *a, **k: None)
    loop = OuterLoop.__new__(OuterLoop)
    loop.state = state
    loop._end_task_budget("T1", budget)       # must not raise


# ── the second review of the audit batch ─────────────────────────────────────

def _script(rel):
    import importlib.util
    spec = importlib.util.spec_from_file_location("b95_" + Path(rel).stem, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    sys.path.insert(0, str(ROOT / "scripts"))
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("command", [
    'pytest tests -q --junitxml "$(echo"',
    'pytest tests -q "`echo"',
    "echo 'abc",
])
def test_a_command_the_shell_refuses_is_neither_cached_nor_classified(command):
    from tools.contest import testcache
    assert not testcache.is_well_formed(command)
    assert testcache.parse_pytest(command) == []


def test_a_closed_substitution_inside_quotes_still_parses():
    from tools.contest import testcache
    assert testcache.is_well_formed('pytest tests -q --junitxml "$(echo out).xml"')
    assert testcache.parse_pytest('pytest tests -q --junitxml "$(echo out).xml"')


def test_the_cache_glue_answers_nothing_for_an_unclosed_quote(tmp_path):
    from tools.contest import testcache_glue
    seen = []
    glue = testcache_glue.TestCacheGate.__new__(testcache_glue.TestCacheGate)
    glue._classify = lambda c: seen.append(c)
    glue.cache = SimpleNamespace(classification=lambda c: (False, None))
    assert glue._runs_of("echo 'abc tests") == [] and seen == []


def test_an_error_object_beside_an_empty_list_is_still_an_error():
    import io, json
    from unittest.mock import patch
    mod = _script("scripts/py_model_test.py")

    class R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()
    body = json.dumps({"data": [], "error": {"message": "invalid api key"}}).encode()
    with patch("urllib.request.urlopen", side_effect=lambda *a, **k: R(body)):
        with pytest.raises(ValueError):
            mod.free_from_direct_api("http://invalid.local/v1", "k")


@pytest.mark.parametrize("reply", ["•\n*", "FIX: •", "• FIX: •\n- FIX: *", "-\n•\n*"])
def test_marker_noise_is_no_fact_even_without_a_marker_line(reply):
    from tools.auto.summary_memory import _clean_bullet_list
    assert _clean_bullet_list(reply) == ""


def test_numbers_that_start_a_fact_survive_the_new_filter():
    from tools.auto.summary_memory import _clean_bullet_list
    assert _clean_bullet_list("FIX: 2024 was the year") == "• 2024 was the year"
    assert _clean_bullet_list("• 2024. The project launched") == "• 2024. The project launched"


def test_rename_helper_reads_only_the_blanks_beside_the_path():
    from tools.claimcheck.anchors import _is_rename
    text = "a.py =>\nb.py"
    assert _is_rename(text, 0, 4)                       # a.py is followed by an arrow on its line
    assert not _is_rename(text, 8, 12)                  # b.py: the arrow is on the line above
    assert _is_rename("x => b.py", 5, 9)
    assert not _is_rename("x =>\nb.py", 5, 9)
    assert not _is_rename("a.py\n=> c", 0, 4)


@pytest.mark.parametrize("payload", ["{}", "[]", '{"checks": null}', '{"checks": [1]}', '"x"'])
def test_a_result_of_the_wrong_shape_is_one_lost_cell(payload):
    mod = _script("contest-bench/kc4/run_all_kc4.py")
    assert not mod._well_formed(json.loads(payload))
    assert mod._well_formed({"checks": [{"name": "n", "ok": True}]})


def test_the_backoff_check_survives_a_decorated_next_function():
    mod = _script("contest-bench/slow1/check_slow1.py")
    src = ("def make_config():\n    return Cfg(error_retry_backoff_sec=0)\n\n\n"
           "@pytest.fixture\ndef other():\n    pass\n")
    assert mod.backoff_zero(src) is True


def test_an_unused_dict_with_zero_does_not_pass_a_config_that_returns_one():
    mod = _script("contest-bench/slow1/check_slow1.py")
    src = ("def make_config():\n    unused = {'error_retry_backoff_sec': 0}\n"
           "    return Cfg(error_retry_backoff_sec=1)\n")
    assert mod.backoff_zero(src) is False
    assert mod.backoff_zero("def make_config():\n    return Cfg(error_retry_backoff_sec=0)\n") is True
    assert mod.backoff_zero("def make_config():\n    d = {'error_retry_backoff_sec': 0}\n    return Cfg(**d)\n") is True
