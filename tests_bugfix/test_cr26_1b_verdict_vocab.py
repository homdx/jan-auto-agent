"""tests/test_cr26_1b_verdict_vocab.py — extended RU vocabulary coverage for the
bilingual verdict parser (AUTO-CR-26-1, hardened).

Locks the forms a live 8B actually produces that the first cut mis-handled:
  * negated/absent contradictions that mean APPROVE
    («противоречий не обнаружено», «не вижу противоречий», «противоречия отсутствуют»);
  * correction vocabulary that means REVISE
    («требуется доработка», «нужно переписать», «переделать»);
  * a bare leading «Нет, …» / «Нет.» as REVISE, while «нет проблем» stays APPROVE.
"""
from __future__ import annotations

import pytest

from tools.auto.inner_loop import _parse_verdict_soft as P


APPROVE = [
    "Нет противоречий.", "Противоречий нет", "Без противоречий",
    "Противоречий не обнаружено", "Противоречий не выявлено",
    "Не вижу противоречий", "Противоречия отсутствуют",
    "Согласна", "СОГЛАСЕН", "Не против", "НЕПРОТИВ",
    "Одобряю", "Одобрено", "Принято", "Можно принять",
    "Всё верно", "Текст соответствует фактам",
    "Нет проблем", "Нет замечаний", "Без замечаний",
    "APPROVED", "ok looks good",
]

REVISE = [
    "Противоречие: капитан назван мужчиной", "Есть противоречие с главой 1",
    "Не согласна", "НЕ СОГЛАСНА", "несогласна", "ПРОТИВ", "против.",
    "Требуется доработка: возраст не совпадает", "Нужно переписать абзац",
    "Надо переделать концовку", "Следует исправить имя",
    "Это неверно", "Исправьте имя героя", "Не одобряю, есть ошибка",
    "Нет, так нельзя оставлять", "Нет, это не подходит", "Нет.",
    "REVISE: bad", "REJECT: wrong",
]


@pytest.mark.parametrize("text", APPROVE)
def test_approve_forms(text):
    approved, _reason, _unparsed = P(text)
    assert approved is True, f"expected APPROVE for {text!r}"


@pytest.mark.parametrize("text", REVISE)
def test_revise_forms(text):
    approved, reason, _unparsed = P(text)
    assert approved is False, f"expected REVISE for {text!r}"
    assert reason, f"REVISE must carry a reason for {text!r}"


def test_negation_traps():
    # «не согласна» must NOT be read as «согласна»
    assert P("не согласна")[0] is False
    # «нет противоречий» must NOT be read as «противоречие»
    assert P("нет противоречий")[0] is True
    # «не против» must NOT be read as «против»
    assert P("не против")[0] is True
    # «нет проблем» must NOT be caught by the bare-«нет» REVISE rule
    assert P("нет проблем")[0] is True
