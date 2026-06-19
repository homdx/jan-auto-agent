"""AUTO-CR-26-1 — Bilingual, case-insensitive, negation-aware verdict parser.

Tests for _parse_verdict_soft extended with Russian recognition.

Returns (approved: bool, reason: str, unparseable: bool).
"""

import importlib
import sys
import types
import pytest

# ---------------------------------------------------------------------------
# Minimal stub so inner_loop.py can be imported without the full project tree
# ---------------------------------------------------------------------------

def _make_stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

for _mod in [
    "anthropic", "anthropic.types",
    "tools", "tools.auto",
    "openai",
]:
    if _mod not in sys.modules:
        _make_stub(_mod)

# Provide just enough surface for inner_loop top-level imports
import types as _types

_anthropic = sys.modules["anthropic"]
_anthropic.Anthropic = lambda **kw: None          # type: ignore[attr-defined]
_anthropic.types = sys.modules["anthropic.types"]  # type: ignore[attr-defined]

# Now import the module under test
import importlib, importlib.util, pathlib, os

_src = pathlib.Path(__file__).parent.parent / "tools" / "auto" / "inner_loop.py"
_spec = importlib.util.spec_from_file_location("inner_loop", _src)
_mod  = importlib.util.module_from_spec(_spec)          # type: ignore[arg-type]

# Patch missing heavy deps before exec
for _dep in ["logging", "json", "re", "collections", "functools",
             "dataclasses", "typing", "enum", "pathlib", "threading",
             "time", "copy", "os", "sys"]:
    # these are stdlib, already present — no stub needed
    pass

# Provide lightweight stubs for project-internal imports that inner_loop uses
for _dep in [
    "tools.llm_pool", "tools.rate_limiter", "tools.config_loader",
    "tools.auto.story_bible",
]:
    if _dep not in sys.modules:
        _make_stub(_dep)

# Execute the source; ignore ImportErrors from heavy third-party deps
try:
    _spec.loader.exec_module(_mod)          # type: ignore[union-attr]
except Exception:
    pass

# Extract the function directly from the module's global namespace if exec failed
_parse_verdict_soft = getattr(_mod, "_parse_verdict_soft", None)
if _parse_verdict_soft is None:
    # Fallback: exec just the function source in isolation
    import ast, textwrap
    _src_text = _src.read_text(encoding="utf-8")
    _tree = ast.parse(_src_text)
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.FunctionDef) and _node.name == "_parse_verdict_soft":
            _func_src = ast.get_source_segment(_src_text, _node)
            break
    else:
        raise RuntimeError("_parse_verdict_soft not found in inner_loop.py")
    _ns: dict = {}
    exec(textwrap.dedent(_func_src), _ns)   # type: ignore[arg-type]
    _parse_verdict_soft = _ns["_parse_verdict_soft"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def approved(text: str) -> tuple[bool, str, bool]:
    return _parse_verdict_soft(text)


def _assert_approved(text: str) -> None:
    ok, reason, unparseable = _parse_verdict_soft(text)
    assert ok is True,          f"{text!r} → expected APPROVED, got REVISE (reason={reason!r})"
    assert unparseable is False, f"{text!r} → expected parseable, got unparseable"


def _assert_revise(text: str, *, reason_nonempty: bool = True) -> str:
    ok, reason, unparseable = _parse_verdict_soft(text)
    assert ok is False,          f"{text!r} → expected REVISE, got APPROVED"
    assert unparseable is False,  f"{text!r} → expected parseable, got unparseable"
    if reason_nonempty:
        assert reason.strip(),   f"{text!r} → expected non-empty reason, got {reason!r}"
    return reason


def _assert_unparseable(text: str) -> None:
    ok, reason, unparseable = _parse_verdict_soft(text)
    assert ok is True,           f"{text!r} → fail-open expected"
    assert unparseable is True,  f"{text!r} → expected unparseable=True"


# ===========================================================================
# 1. English APPROVED
# ===========================================================================

class TestEnglishApproved:
    def test_approved_exact(self):           _assert_approved("APPROVED")
    def test_approved_lower(self):           _assert_approved("approved")
    def test_approved_mixed(self):           _assert_approved("Approved")
    def test_approved_with_trailing(self):   _assert_approved("APPROVED — looks good")
    def test_ok_exact(self):                 _assert_approved("OK")
    def test_ok_lower(self):                 _assert_approved("ok")
    def test_ok_looks_good(self):            _assert_approved("ok looks good")
    def test_approved_leading_whitespace(self): _assert_approved("  APPROVED  ")


# ===========================================================================
# 2. English REVISE / REJECT / NO
# ===========================================================================

class TestEnglishRevise:
    def test_revise_with_reason(self):
        r = _assert_revise("REVISE: x")
        assert "x" in r

    def test_reject_with_reason(self):
        r = _assert_revise("REJECT: bad pacing")
        assert "bad pacing" in r

    def test_no_with_reason(self):
        r = _assert_revise("NO: wrong tone")
        assert "wrong tone" in r

    def test_revise_no_reason(self):
        _assert_revise("REVISE", reason_nonempty=False)  # fallback text OK

    def test_reject_lower(self):
        _assert_revise("reject: something")

    def test_no_upper(self):
        _assert_revise("NO")


# ===========================================================================
# 3. Russian APPROVED
# ===========================================================================

class TestRussianApproved:
    def test_net_protivorechiy(self):        _assert_approved("Нет противоречий.")
    def test_protivorechiy_net(self):        _assert_approved("Противоречий нет")
    def test_ne_protivorechit(self):         _assert_approved("Не противоречит фактам")
    def test_ne_protiv(self):                _assert_approved("не против")
    def test_ne_protiv_upper(self):          _assert_approved("НЕ ПРОТИВ")
    def test_neprotiv(self):                 _assert_approved("НЕПРОТИВ")
    def test_odobreno(self):                 _assert_approved("Одобрено")
    def test_sogласна(self):                 _assert_approved("Согласна")
    def test_sogласen_upper(self):           _assert_approved("СОГЛАСЕН")
    def test_vsyo_verno(self):               _assert_approved("всё верно")
    def test_vse_verno(self):                _assert_approved("все верно")
    def test_bez_protivorechiy(self):        _assert_approved("Без противоречий")
    def test_sootvetstvuet(self):            _assert_approved("Соответствует тексту")


# ===========================================================================
# 4. Russian REVISE
# ===========================================================================

class TestRussianRevise:
    def test_protivorechie_with_colon(self):
        r = _assert_revise("Противоречие: капитан назван мужчиной")
        assert "капитан" in r.lower() or len(r) > 0

    def test_ne_sogласna(self):              _assert_revise("Не согласна")
    def test_ne_sogласna_upper(self):        _assert_revise("НЕ СОГЛАСНА")
    def test_net_sogласen(self):             _assert_revise("Нет, не согласен")
    def test_nesogласna(self):               _assert_revise("Несогласна")
    def test_protiv_bare(self):              _assert_revise("ПРОТИВ")
    def test_protivorechit_verb(self):       _assert_revise("противоречит главе 1")
    def test_neverно(self):                  _assert_revise("Неверно, возраст другой")
    def test_oshibka(self):                  _assert_revise("Ошибка: имя изменилось")
    def test_ispravit(self):                 _assert_revise("Исправьте возраст")
    def test_ne_sootvetstvuet(self):         _assert_revise("Не соответствует плану")


# ===========================================================================
# 5. Negation traps — the critical ordering tests
# ===========================================================================

class TestNegationTraps:
    """These are the exact pitfalls the spec calls out."""

    def test_ne_sogласna_is_REVISE_not_APPROVED(self):
        """'не согласна' contains 'согласна' — must be REVISE."""
        ok, _, unparseable = _parse_verdict_soft("не согласна")
        assert ok is False and unparseable is False, \
            "'не согласна' must be REVISE, not APPROVED"

    def test_net_protivorechiy_is_APPROVED_not_REVISE(self):
        """'нет противоречий' contains 'противоречи' — must be APPROVED."""
        ok, _, unparseable = _parse_verdict_soft("нет противоречий")
        assert ok is True and unparseable is False, \
            "'нет противоречий' must be APPROVED, not REVISE"

    def test_ne_protiv_is_APPROVED_not_REVISE(self):
        """'не против' contains 'против' — must be APPROVED."""
        ok, _, unparseable = _parse_verdict_soft("не против")
        assert ok is True and unparseable is False, \
            "'не против' must be APPROVED, not REVISE"

    def test_ne_protivorechit_is_APPROVED_not_REVISE(self):
        ok, _, unparseable = _parse_verdict_soft("Не противоречит фактам")
        assert ok is True and unparseable is False, \
            "'не противоречит' must be APPROVED"


# ===========================================================================
# 6. Case / whitespace robustness
# ===========================================================================

class TestCaseAndWhitespace:
    def test_mixed_case_protiv(self):        _assert_revise("  пРотИв  ")
    def test_punct_stripped_protiv(self):    _assert_revise("Против.")
    def test_mixed_case_approved(self):      _assert_approved("Approved")   # EN mixed-case
    def test_extra_spaces_ne_sogласna(self): _assert_revise("не  согласна")
    def test_trailing_newline(self):         _assert_approved("APPROVED\n")
    def test_leading_empty_lines(self):      _assert_approved("\n\nAPPROVED")


# ===========================================================================
# 7. Unparseable (fail-open)
# ===========================================================================

class TestUnparseable:
    def test_json_blob(self):
        _assert_unparseable('{"approved": true, "feedback": "ok"}')

    def test_rambling_prose(self):
        _assert_unparseable("я думаю это нормально, но")

    def test_empty_string(self):
        _assert_unparseable("")

    def test_whitespace_only(self):
        _assert_unparseable("   \n  ")

    def test_generic_commentary(self):
        # A sentence that is genuine prose with no verdict token
        _assert_unparseable("Текст написан хорошо и атмосфера передана верно в целом")


# ===========================================================================
# 8. Regression — existing English-only behaviour unchanged
# ===========================================================================

class TestRegression:
    """Replicate the original CR-2 test expectations exactly."""

    @pytest.mark.parametrize("text,exp_approved", [
        ("APPROVED",          True),
        ("approved",          True),
        ("OK looks fine",     True),
        ("REVISE: too short", False),
        ("REJECT: off-topic", False),
        ("NO: wrong style",   False),
    ])
    def test_original_en_cases(self, text: str, exp_approved: bool):
        ok, reason, unparseable = _parse_verdict_soft(text)
        assert ok is exp_approved
        assert unparseable is False
        if not exp_approved:
            assert reason.strip()

    def test_fail_open_returns_true(self):
        ok, _, unparseable = _parse_verdict_soft("some random text")
        assert ok is True
        assert unparseable is True


# ===========================================================================
# 9. Reason extraction
# ===========================================================================

class TestReasonExtraction:
    def test_ru_colon_reason_captured(self):
        _, reason, _ = _parse_verdict_soft("Противоречие: капитан назван мужчиной")
        assert "капитан" in reason.lower()

    def test_en_colon_reason_captured(self):
        _, reason, _ = _parse_verdict_soft("REVISE: pacing is off")
        assert "pacing" in reason

    def test_no_colon_full_text_reason(self):
        _, reason, _ = _parse_verdict_soft("ПРОТИВ")
        assert reason.strip()   # some non-empty fallback

    def test_multiline_first_line_verdict(self):
        """Verdict on first line; second line is explanation — first line wins."""
        text = "APPROVED\nBut could be slightly improved."
        ok, _, unparseable = _parse_verdict_soft(text)
        assert ok is True and unparseable is False
