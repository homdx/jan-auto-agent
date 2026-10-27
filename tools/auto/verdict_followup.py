"""One follow-up question for a creative verdict that is unclear or contradicts itself (VERDICT-ASK).

`_parse_verdict_soft` reads a reply's opening verdict. "No errors found: the chapter
ignores the bible." opens with an approval and goes on to name the problem in the next
clause, with no "but" for the caveat rule to see — so the chapter was approved with the
objection in the reply. Guessing which half the model meant is the parser's weak spot; the
model knows. A reply that approves and then says something contrary, and a reply with no
verdict at all, are put back to the same model once: its own reply is quoted, with the
contrary clause, and the question asks for the strict verdict line. The answer decides.
An answer that is no clearer leaves the first reading (the gates' fail-open), flagged
unparseable so the caller logs it.

The Gate-2 creative validator, the continuity, fact and theme checks and the architect's
plan check all settle their reply here — before, only Gate 2 re-asked (AUTO-CR-31), and
the other four approved an unparseable reply on the spot.
"""
from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger(__name__)

#: Appended to each creative verdict prompt: the strict shape the soft parser falls back from.
STRICT_VERDICT_RULE = (
    "VERDICT FORMAT (strict): the first line of your reply is the verdict — exactly "
    "APPROVED, or REVISE: followed by what to fix. APPROVED means you found no problem "
    "at all: never write APPROVED (or 'no errors', 'looks good') and then describe a "
    "problem — if you name any problem, the verdict is REVISE."
)

#: How much of the first reply is quoted back; a rambling reply is cut, not resent whole.
_QUOTE_LIMIT = 4000

#: A clause ends at a stop, a colon, a semicolon, a line break or a dash between words.
_CLAUSE_SPLIT = re.compile(r"[.:;!?\n]+|\s[—–-]\s")

#: The words that say something is wrong with the text. Verbs and adjectives only: the
#: problem nouns ("no errors", "one problem") are the parser's own vocabulary and it reads them.
_EN_CONTRARY = re.compile(
    r"\b(?:CONTRADICT(?:S|ED|ING)?|IGNORE(?:S|D)?|IGNORING|VIOLATE(?:S|D)?|VIOLATING"
    r"|BREAK(?:S|ING)?|BROKE|BROKEN|MISS(?:ES|ED|ING)?|LACK(?:S|ED|ING)?"
    r"|FAIL(?:S|ED|ING)?|OMIT(?:S|TED|TING)?|DEVIATE(?:S|D)?|DEVIATING|DIFFER(?:S|ED|ING)?"
    r"|CONFLICTS\s+WITH|CONFLICTED|CONFLICTING|CLASH(?:ES|ED|ING)?"
    r"|INCONSISTENT|INCORRECT|WRONG|INACCURATE|MISMATCH(?:ES|ED)?|DROPS|DROPPED"
    r"|FORGETS|FORGOT|FORGOTTEN|CHANGES|CHANGED)\b"
)
#: The Russian side: verb forms only, so «противоречий нет» (a noun, negated after it) stays out.
_RU_CONTRARY = re.compile(
    r"(?<![а-яё])(?:противореч(?:ит|ат|ил\w*|ащ\w*)|проигнорир\w*|игнориру\w*"
    r"|наруша\w*|нарушен\w*|наруши\w*|пропущен\w*|упущен\w*|неверн\w*|ошибочн\w*"
    r"|расходит\w*|расходят\w*)"
)
#: Russian phrases that are contrary by their own «не»: no negation check for them.
_RU_CONTRARY_NEGATIVE = re.compile(
    r"(?<![а-яё])не\s+(?:соответству\w*|учитыва\w*|совпада\w*|согласует\w*)"
)
#: The words that turn a contrary word back into praise: "does not contradict", "nothing
#: contradicts", "never ignores", «ничто не противоречит».
_NEGATORS = frozenset((
    "NO", "NOT", "NEVER", "NOTHING", "NONE", "NOBODY", "NOR", "NEITHER", "WITHOUT", "CANNOT",
    "НЕ", "НИ", "НЕТ", "НИЧТО", "НИЧЕГО", "НИКТО", "НИКАК", "НИГДЕ", "БЕЗ",
))
#: "missing nothing", "breaks no rule", «нарушений нет» — the object denies the word.
_DENIED_AFTER = re.compile(r"^\s*(?:NOTHING|NONE|NO|ANYTHING)\b", re.I)


def _negated(clause_upper: str, start: int, end: int) -> bool:
    before = clause_upper[:start].replace("’", "'").split()[-2:]
    if any(w.strip(",") in _NEGATORS or w.endswith("N'T") for w in before):
        return True
    return bool(_DENIED_AFTER.match(clause_upper[end:]))


def contrary_clause(reply: str) -> "str | None":
    """The first clause of *reply* that says something is wrong, or ``None``.

    Read only for a reply the parser approved: such a clause after the approval
    ("No errors found: the chapter ignores the bible.") is the self-contradiction the
    follow-up question asks about. A negated one ("nothing contradicts the bible", "it
    does not ignore the outline") is praise and does not count.
    """
    for clause in _CLAUSE_SPLIT.split(reply or ""):
        up, low = clause.upper(), clause.lower()
        for rx, text in ((_EN_CONTRARY, up), (_RU_CONTRARY, low)):
            for m in rx.finditer(text):
                if not _negated(up, m.start(), m.end()):
                    return clause.strip()
        if _RU_CONTRARY_NEGATIVE.search(low):
            return clause.strip()
    return None


def _quote(reply: str) -> str:
    text = reply.strip()
    if len(text) > _QUOTE_LIMIT:
        text = text[:_QUOTE_LIMIT] + "\n[… cut]"
    return text


def follow_up(reply: str, contrary: "str | None") -> str:
    """The text appended to the original request: the reply quoted, and the question."""
    head = (
        "\n\n--- YOUR PREVIOUS REPLY ---\n" + _quote(reply)
        + "\n--- END OF YOUR PREVIOUS REPLY ---\n"
    )
    if contrary is not None:
        return head + (
            "Your previous reply contradicts itself: it approves, but it also says: "
            f"\"{contrary}\". Is that a problem the author must fix? Reply AGAIN and make "
            "the VERY FIRST token exactly APPROVED (only if that statement is not a "
            "problem) or REVISE (in English, uppercase). If REVISE, follow it with ':' "
            "and what to fix."
        )
    return head + (
        "IMPORTANT: your previous reply did not begin with a verdict. Reply AGAIN and "
        "make the VERY FIRST token exactly APPROVED or REVISE (in English, uppercase). "
        "If REVISE, follow it with ':' and the numbered list of problems."
    )


def settle_verdict(
    raw: str, ask: Callable[[str], str], *, who: str,
) -> "tuple[bool, str, bool, str]":
    """``(approved, reason, unparseable, reply)`` of *raw*, asking once when it is unclear.

    *ask* takes the follow-up text, sends the original request with it appended, and
    returns the model's new reply. *reply* is the reply the verdict was read from.
    """
    from tools.auto.inner_loop import _parse_verdict_soft  # noqa: PLC0415 — inner_loop imports us

    approved, reason, unparseable = _parse_verdict_soft(raw)
    contrary = contrary_clause(raw) if approved and not unparseable else None
    if not unparseable and contrary is None:
        return approved, reason, unparseable, raw

    if contrary is not None:
        logger.info("%s: the approval goes on to say %r — asking once.", who, contrary[:120])
    else:
        logger.info("%s: verdict unparseable — asking once. raw=%r", who, raw[:120])
    try:
        raw2 = ask(follow_up(raw, contrary)) or ""
    except Exception as exc:  # noqa: BLE001 — the first reply still stands (fail-open)
        logger.warning("%s: the follow-up question failed — %s", who, exc)
        raw2 = ""
    if raw2.strip():
        a2, r2, u2 = _parse_verdict_soft(raw2)
        if not u2 and not (a2 and contrary_clause(raw2) is not None):
            return a2, r2, u2, raw2

    if contrary is not None:
        logger.warning(
            "%s: the approval still contradicts itself after one question — passed on "
            "fail-open. raw=%r", who, raw[:120],
        )
        return True, f"verdict contradicts itself ({contrary[:200]}) — passed on fail-open", True, raw
    return approved, reason, unparseable, raw
