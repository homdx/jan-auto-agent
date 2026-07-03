"""Tests for tools/auto/theme_validator.py (podrugi-3 theme/content gate).

Covers:
- verdict parsing (APPROVED / REVISE) through _parse_verdict_soft
- fail-open on LLM error and on unparseable replies
- no-guidelines short-circuit (validator approves without an LLM call)
- factory gating: disabled by default, disabled when guidelines empty,
  enabled with cap when fully configured
- InnerLoop wiring: theme REVISE consumes an attempt and retries; cap
  reached -> ACCEPTED_AT_CAP behavior (accepts, does not loop forever)
"""
import configparser

import pytest

from tools.auto.theme_validator import (
    ThemeValidator,
    ThemeVerdict,
    make_theme_validator,
)

GUIDELINES = (
    "Рассказ не должен романтизировать курение или подавать его как "
    "эффективный способ контроля веса."
)


# ── ThemeValidator.check ──────────────────────────────────────────────────────

def test_revise_verdict_parsed():
    v = ThemeValidator(
        lambda s, u: "REVISE: глава подаёт курение как бонус — показать цену.",
        GUIDELINES,
    )
    r = v.check("глянцевая глава")
    assert isinstance(r, ThemeVerdict)
    assert not r.approved
    assert "цену" in r.reason
    assert not r.unparseable


def test_approved_verdict():
    v = ThemeValidator(lambda s, u: "APPROVED", GUIDELINES)
    assert v.check("честная глава").approved


def test_fail_open_on_llm_error():
    def boom(s, u):
        raise RuntimeError("network down")
    v = ThemeValidator(boom, GUIDELINES)
    r = v.check("глава")
    assert r.approved
    assert "fail-open" in r.reason


def test_fail_open_on_unparseable_reply():
    v = ThemeValidator(lambda s, u: "ну, вроде норм", GUIDELINES)
    r = v.check("глава")
    assert r.approved
    assert r.unparseable


def test_no_guidelines_short_circuits_without_llm_call():
    calls = []

    def spy(s, u):
        calls.append(1)
        return "APPROVED"

    v = ThemeValidator(spy, "   ")
    r = v.check("глава")
    assert r.approved
    assert calls == []  # LLM must not be consulted


def test_guidelines_and_chapter_reach_the_llm():
    seen = {}

    def spy(system, user):
        seen["system"] = system
        seen["user"] = user
        return "APPROVED"

    v = ThemeValidator(spy, GUIDELINES)
    v.check("Оля закурила на балконе.")
    assert GUIDELINES in seen["user"]
    assert "Оля закурила" in seen["user"]
    assert "thematic guidelines" in seen["system"]


def test_verdict_token_language_carveout_present_for_russian_text():
    seen = {}

    def spy(system, user):
        seen["system"] = system
        return "APPROVED"

    v = ThemeValidator(spy, GUIDELINES)
    v.check("Полностью русская глава о Даше и Оле на набережной города.")
    assert "APPROVED or REVISE" in seen["system"]


def test_negative_cap_clamped_to_zero():
    v = ThemeValidator(lambda s, u: "APPROVED", GUIDELINES,
                       max_theme_revisions=-5)
    assert v.max_theme_revisions == 0


# ── make_theme_validator factory ──────────────────────────────────────────────

def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_factory_disabled_by_default():
    cfg = _cfg("[validator_agent]\n")
    assert make_theme_validator(cfg) is None


def test_factory_disabled_when_guidelines_empty():
    cfg = _cfg("[validator_agent]\ntheme_check_creative = true\n")
    assert make_theme_validator(cfg) is None


def test_factory_enabled_with_guidelines(monkeypatch):
    cfg = _cfg(
        "[validator_agent]\n"
        "theme_check_creative = true\n"
        "theme_guidelines = не романтизировать курение\n"
        "max_theme_revisions = 3\n"
    )
    import tools.auto.summary_memory as sm
    monkeypatch.setattr(sm, "_make_llm_call",
                        lambda config, task_mode: (lambda s, u: "APPROVED"))
    v = make_theme_validator(cfg)
    assert v is not None
    assert v.max_theme_revisions == 3
    assert "курение" in v.guidelines


# ── InnerLoop wiring: revise-then-accept and cap behavior ─────────────────────

class _StubCoder:
    """Serves a glamor draft first, an honest draft on the retry."""

    def __init__(self, base_dir, drafts):
        self.base_dir = base_dir
        self.drafts = list(drafts)
        self.calls = 0

    def generate(self, task, base_dir, prior_feedback=None, **kw):
        text = self.drafts[min(self.calls, len(self.drafts) - 1)]
        self.calls += 1
        (self.base_dir / "chapter_2.txt").write_text(text, encoding="utf-8")

        class R:
            ok = True
            files_written = ["chapter_2.txt"]
            response = text
            missing_context = []
        return R()


class _OkExecutor:
    def run(self, task):
        class R:
            passed = True
            exit_code = 0
            stdout = ""
            stderr = ""
            timed_out = False
        return R()


class _OkValidator:
    task_mode = "creative"
    last_missing_context = []

    def approve(self, task, exec_result, coder_result, *, base_dir=None,
                prior_critique=""):
        return True, "approved"


class _GlamorJudge:
    """Theme validator test double: REVISE while the draft looks glamorous."""

    max_theme_revisions = 2

    def check(self, text):
        glamor = "решение лежало в ларьке" in text
        return ThemeVerdict(approved=not glamor,
                            reason="REVISE: романтизация" if glamor else "")


@pytest.fixture
def loop_env(tmp_path):
    from tools.auto.inner_loop import InnerLoop
    return InnerLoop, tmp_path


def test_inner_loop_theme_revise_then_accept(loop_env):
    InnerLoop, base = loop_env
    coder = _StubCoder(base, [
        "Курить легко и выгодно — решение лежало в ларьке.",  # attempt 1: glamor
        "Оля закашлялась; пробежка кончилась на втором километре.",  # attempt 2
    ])
    loop = InnerLoop(coder, _OkExecutor(), _OkValidator(), max_attempts=4,
                     theme_validator=_GlamorJudge(), task_mode="creative")
    task = {"id": "T1", "title": "Глава 2", "instruction": "x",
            "target_files": ["chapter_2.txt"], "acceptance_check": "true"}
    result = loop.run_task(task, str(base))
    assert result.passed
    assert coder.calls == 2  # one theme-driven retry
    final = (base / "chapter_2.txt").read_text(encoding="utf-8")
    assert "решение лежало в ларьке" not in final


def test_inner_loop_theme_cap_accepts_fail_open(loop_env):
    InnerLoop, base = loop_env
    # Coder never fixes the glamor: cap (2) must be reached, then accept.
    coder = _StubCoder(base, ["Курить легко — решение лежало в ларьке."])
    loop = InnerLoop(coder, _OkExecutor(), _OkValidator(), max_attempts=6,
                     theme_validator=_GlamorJudge(), task_mode="creative")
    task = {"id": "T2", "title": "Глава 2", "instruction": "x",
            "target_files": ["chapter_2.txt"], "acceptance_check": "true"}
    result = loop.run_task(task, str(base))
    assert result.passed  # accepted at cap, not an infinite loop / failure
    assert coder.calls == 3  # attempt 1 + 2 theme revisions, then cap


def test_inner_loop_theme_gate_skipped_in_code_mode(loop_env):
    InnerLoop, base = loop_env
    coder = _StubCoder(base, ["Курить легко — решение лежало в ларьке."])
    loop = InnerLoop(coder, _OkExecutor(), _OkValidator(), max_attempts=3,
                     theme_validator=_GlamorJudge(), task_mode="code")
    task = {"id": "T3", "title": "t", "instruction": "x",
            "target_files": ["chapter_2.txt"], "acceptance_check": "true"}
    result = loop.run_task(task, str(base))
    assert result.passed
    assert coder.calls == 1  # gate must not fire outside creative mode


# ── codeapp-sim: coder parse диагнозы (попугай vs обрыв vs кривой JSON) ───────

def test_coder_parse_no_json_at_all_gets_question_diagnosis():
    """A prose/clarifying-question reply must NOT be diagnosed as truncation."""
    from tools.auto.coder import Coder
    c = Coder.__new__(Coder)
    c._task_mode = "code"
    files, msg = c._parse_response(
        "Do you want me to keep the existing http.server structure?",
        task_id="T1", target_files=["app.py"])
    assert files == []
    assert "NO JSON at all" in msg
    assert "Do NOT ask questions" in msg
    assert "too long" not in msg  # старый неверный диагноз


def test_coder_parse_truncated_json_still_diagnosed_as_cutoff():
    from tools.auto.coder import Coder
    c = Coder.__new__(Coder)
    c._task_mode = "code"
    files, msg = c._parse_response(
        '{"files": [{"path": "app.py", "content": "x = 1\\nprint(',
        task_id="T1", target_files=["app.py"])
    assert files == []
    assert "cut off" in msg


def test_coder_parse_malformed_json_plain_diagnosis():
    from tools.auto.coder import Coder
    c = Coder.__new__(Coder)
    c._task_mode = "code"
    files, msg = c._parse_response(
        '{"files": oops}', task_id="T1", target_files=["app.py"])
    assert files == []
    assert "JSON decode failed" in msg


# ── pullrun-sim: delete-поддержка и ASCII-гейт идентификаторов ────────────────

def _make_coder(tmp_path, ascii_only=False):
    import configparser
    from tools.auto.coder import Coder
    cfg = configparser.ConfigParser()
    cfg.read_string(f"""
[coder]
ascii_identifiers_only = {'true' if ascii_only else 'false'}
[loop]
timeout_seconds = 5
[api]
active = local
[api_local]
num_ctx = 0
""")
    return Coder(config=cfg, base_url="http://x", api_key="k", model="m",
                 api_format="ollama", verify_ssl=True, task_mode="code")


def test_parse_accepts_delete_items():
    from tools.auto.coder import Coder
    c = Coder.__new__(Coder)
    c._task_mode = "code"
    files, err = c._parse_response(
        '{"files": [{"path": "records.py", "delete": true},'
        ' {"path": "fetcher.py", "content": "x = 1\\n"}]}',
        task_id="T", target_files=["records.py", "fetcher.py"])
    assert err == ""
    assert {"path": "records.py", "delete": True} in files


def test_write_files_deletes_with_backup(tmp_path):
    c = _make_coder(tmp_path)
    (tmp_path / "records.py").write_text("old = 1\n", encoding="utf-8")
    written, err = c._write_files(
        [{"path": "records.py", "delete": True}], tmp_path, "T",
        allowed_paths=frozenset({"records.py"}))
    assert err == "" and written == ["records.py"]
    assert not (tmp_path / "records.py").exists()
    assert (tmp_path / "records.py.coder.bak").read_text() == "old = 1\n"


def test_delete_respects_target_files_guard(tmp_path):
    c = _make_coder(tmp_path)
    (tmp_path / "secret.py").write_text("keep me\n", encoding="utf-8")
    written, err = c._write_files(
        [{"path": "secret.py", "delete": True}], tmp_path, "T",
        allowed_paths=frozenset({"other.py"}))
    assert written == [] and "not in target_files" in err
    assert (tmp_path / "secret.py").exists()


def test_delete_of_absent_file_is_idempotent_success(tmp_path):
    c = _make_coder(tmp_path)
    written, err = c._write_files(
        [{"path": "gone.py", "delete": True}], tmp_path, "T",
        allowed_paths=frozenset({"gone.py"}))
    assert err == "" and written == ["gone.py"]


def test_ascii_guard_rejects_russian_identifiers(tmp_path):
    c = _make_coder(tmp_path, ascii_only=True)
    code = ("def запустить(источник):\n"
            "    return источник  # комментарий по-русски — ок\n")
    written, err = c._write_files(
        [{"path": "main.py", "content": code}], tmp_path, "T",
        allowed_paths=frozenset({"main.py"}))
    assert written == []
    assert "non-ASCII identifiers" in err and "запустить" in err
    assert not (tmp_path / "main.py").exists()


def test_ascii_guard_allows_russian_in_strings_and_comments(tmp_path):
    c = _make_coder(tmp_path, ascii_only=True)
    code = ('def run(source):\n'
            '    """Разобрать записи (докстринг по-русски)."""\n'
            '    label = "ключ=значение"  # строка и комментарий русские\n'
            '    return source, label\n')
    written, err = c._write_files(
        [{"path": "main.py", "content": code}], tmp_path, "T",
        allowed_paths=frozenset({"main.py"}))
    assert err == "" and written == ["main.py"]


def test_ascii_guard_off_by_default(tmp_path):
    c = _make_coder(tmp_path, ascii_only=False)
    code = "запустить = lambda x: x\n"
    written, err = c._write_files(
        [{"path": "main.py", "content": code}], tmp_path, "T",
        allowed_paths=frozenset({"main.py"}))
    assert err == "" and written == ["main.py"]
