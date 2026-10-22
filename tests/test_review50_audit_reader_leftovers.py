"""tests/test_review50_audit_reader_leftovers.py — CC review fixes 50: what the audit fixes broke in the readers.

Reproduced on the three audit patches (AUDIT-1..23, MIMO-1..5, AUDIT3):

* AUDIT3-C13 took "save", "bar", "beyond" and "besides" for an exception in any
  sense: "APPROVED. Save the file and move on.", "LGTM. Bar none the best chapter",
  "APPROVED. Beyond that, great work." and "APPROVED. Besides, it reads well."
  were rejections.
* AUDIT-23 took the singular "correction" out with "corrections": "(correction
  needed)", "(needs correction)" and "(minor correction)" went into synopsis.md.
* AUDIT3-3 narrowed a crash claim to its own line only when the claim carried one —
  and `extract_claims` never gives an access claim its sentence's line, so a claim
  about an unguarded line was still dropped for the same text GUARDED elsewhere.
* AUDIT3-9 read a UTF-8 synopsis with one broken byte as cp1252, where no new
  Cyrillic section fits: the synopsis was never updated again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.auto.inner_loop import _parse_verdict_soft
from tools.auto.summary_memory import SummaryMemory, _strip_meta_parentheticals
from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.verifier import REASON_CONTRADICTS_GUARD, contradiction_check, extract_claims


# ── AUDIT3-C13: an exception word only in its exception sense ──────────────

@pytest.mark.parametrize("reply", [
    "APPROVED. Save the file and move on.",
    "LGTM. Bar none the best chapter so far.",
    "APPROVED. Beyond that, great work.",
    "APPROVED. Besides, it reads well.",
])
def test_an_approval_that_names_no_exception_approves(reply):
    assert _parse_verdict_soft(reply)[0] is True


@pytest.mark.parametrize("reply", [
    "APPROVED, save for one typo.",
    "APPROVED, bar the typo in line 3.",
    "APPROVED, barring the typo.",
    "APPROVED, besides the typo in line 3.",
    "APPROVED, beyond the date the chapter is fine.",
    "APPROVED, unless you want a longer ending.",
])
def test_an_approval_with_an_exception_still_rejects(reply):
    assert _parse_verdict_soft(reply)[0] is False


# ── AUDIT-23: the singular is the model's note, the plural a story word ────

@pytest.mark.parametrize("paren", [
    "(correction needed)", "(needs correction)", "(minor correction)",
    "(correction: he was 40)", "(corrections — see above)",
])
def test_a_correction_note_is_stripped(paren):
    assert _strip_meta_parentheticals(f"Fact {paren}.") == "Fact."


def test_a_corrections_officer_stays():
    fact = "Bob met Ann (her brother, a corrections officer)."
    assert _strip_meta_parentheticals(fact) == fact


# ── AUDIT3-3: the line comes from the claim's own sentence ─────────────────

def _module() -> ModuleRecord:
    return ModuleRecord(path="pkg/m.py", guarded_accesses=(
        GuardedAccess(location="pkg/m.py:6", access="data[0]", status="GUARDED", guard="if data"),
        GuardedAccess(location="pkg/m.py:7", access="data[0]", status="UNGUARDED"),
    ))


def _access_claim(sentence: str):
    claims = [c for c in extract_claims(sentence, "pkg/m.py") if c.access]
    assert len(claims) == 1 and claims[0].kind == "access_crash"
    return claims[0]


def test_a_crash_claim_about_the_unguarded_line_is_not_contradicted():
    claim = _access_claim("At pkg/m.py:7 the access data[0] raises IndexError on an empty list.")
    assert contradiction_check(claim, _module(), frozenset()) is None


@pytest.mark.parametrize("sentence", [
    "At pkg/m.py:6 the access data[0] raises IndexError on an empty list.",
    "At pkg/m.py:9 the access data[0] raises IndexError on an empty list.",
    "The access data[0] raises IndexError on an empty list.",
    "At pkg/m.py:6 and pkg/m.py:7 the access data[0] raises IndexError.",
])
def test_the_guarded_line_a_wrong_line_or_no_single_line_is_contradicted(sentence):
    result = contradiction_check(_access_claim(sentence), _module(), frozenset())
    assert result is not None and result[0] == REASON_CONTRADICTS_GUARD


# ── AUDIT3-9: UTF-8 with a broken byte is UTF-8 ────────────────────────────

_CH1 = "<!-- BEGIN chapter_1.md -->\n- Иван пришёл в деревню.\n<!-- END chapter_1.md -->\n"


def _write(tmp_path: Path, raw: bytes) -> bytes:
    (tmp_path / "synopsis.md").write_bytes(raw)
    SummaryMemory(llm_call=lambda *a, **k: "", base_dir=tmp_path)._write_section(
        "chapter_2.md", "- Мария ушла в лес.")
    return (tmp_path / "synopsis.md").read_bytes()


def test_a_utf8_synopsis_with_a_broken_byte_is_still_updated(tmp_path):
    out = _write(tmp_path, _CH1.encode("utf-8").replace(b"\n<!-- END", b"\xff\n<!-- END"))
    text = out.decode("utf-8", errors="replace")
    assert "Мария ушла в лес." in text and "Иван пришёл в деревню." in text
    assert text.count("�") == 1


def test_a_cp1251_synopsis_keeps_its_codec(tmp_path):
    out = _write(tmp_path, _CH1.encode("cp1251"))
    text = out.decode("cp1251")
    assert "Мария ушла в лес." in text and "Иван пришёл в деревню." in text
