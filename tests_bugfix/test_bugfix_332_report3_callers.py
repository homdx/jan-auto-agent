"""332 (Report 3, run as round 326 in code8): every bug driven through the code that has it, one test per bug.

`test_332_report3_bugs.py` pins six of the ten on their helpers; these drive all ten
through the function or the loop the ticket names, and add the one the ticket's own
BUG-3 fix would have opened: a session that refuses every nudge must still reach
the reset, not be nudged forever.
"""

from __future__ import annotations

import ast
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from tools.auto import exhaustion_handler as eh
from tools.auto import inner_loop
from tools.auto.prosody import analyze_ru, check_prosody
from tools.contest import backend, runner

ROOT = Path(__file__).resolve().parent.parent

S1 = ["Тихо светит луна над рекой", "Ветер дует в поле пустом",
      "Ночь укрыла землю собой", "Сон приходит в старый дом"]
LONG = "Бесконечно протяжная непредсказуемая перепутанная строка бесконечная"


def test_bug1_no_leg_field_runs_past_its_budget():
    over = []
    for count in range(1, 15):
        for budget in range(0, 15):
            for ceiling in (3, 6, 20):
                cap = runner._leg_cap(count, budget, ceiling)
                block = runner._leg_block("f", [str(i) for i in range(count)], cap) if cap else []
                if len(block) > budget:
                    over.append((count, budget, ceiling))
    assert not over, over[:4]


def test_bug1_model_fields_fit_a_record_budget():
    msg = "\n".join(f"m{i}" for i in range(30))
    summary = "\n".join(f"s{i}" for i in range(30))
    over = [b for b in range(24, 81) if len(runner._leg_model_fields(msg, summary, b)) > b]
    assert not over, over[:4]


def test_bug2_the_suite_slot_wait_wakes_on_an_escalation():
    tree = ast.parse((ROOT / "tools/contest/runner.py").read_text(encoding="utf-8"))
    stops = [ast.unparse(kw.value) for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "acquire" and getattr(node.func.value, "id", "") == "_SUITE_SLOTS"
             for kw in node.keywords if kw.arg == "stop"]
    assert stops and all("escalate" in s for s in stops), stops


def _touch(nudge, nudges=2, ceiling=1):
    run = SimpleNamespace(first_touch_nudges_used=0, first_touch_resets=0)
    calls = {"reset": 0, "kill": 0}
    ft = runner._FirstTouch(run, SimpleNamespace(path=ROOT), SimpleNamespace(name="a"), SimpleNamespace(),
                            nudge=nudge, reset=lambda *a, **k: calls.__setitem__("reset", calls["reset"] + 1),
                            kill=lambda *a, **k: calls.__setitem__("kill", calls["kill"] + 1),
                            budget=60, nudges=nudges, ceiling=ceiling)
    return ft, run, calls


def _refuse(elapsed):
    raise RuntimeError("session closed")


def test_bug3_a_refused_nudge_spends_nothing():
    ft, run, _ = _touch(_refuse)
    ft._fire()
    assert (run.first_touch_nudges_used, ft.nudged) == (0, 0)


def test_bug3_a_sent_nudge_spends_one():
    sent = []
    ft, run, _ = _touch(sent.append)
    ft._fire()
    assert (run.first_touch_nudges_used, ft.nudged, len(sent)) == (1, 1, 1)


def test_bug3_a_session_that_refuses_every_nudge_is_still_reset():
    ft, run, calls = _touch(_refuse)
    for _ in range(10):
        if not ft._fire():
            break
    # one reset, then DEAD on the next deadline — the base's order, with no nudge counted
    assert calls == {"reset": 1, "kill": 1} and run.first_touch_nudges_used == 0, calls


def test_bug4_a_failed_overflow_swap_keeps_the_turn():
    tree = ast.parse((ROOT / "tools/contest/runner.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "recover_overflow")
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "failed":
            body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
            if "return failed" in body:
                assert "turns.append" in body.split("return failed")[0], body
                return
    raise AssertionError("no `if failed:` path in recover_overflow")


def test_bug5_a_waiter_on_a_shared_key_does_not_raise():
    lock = runner._TestRunsLock()
    lock.enter("")
    box = {}

    def waiter():
        try:
            box["waited"] = lock.enter("")
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc
    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.3)
    lock.exit("")
    t.join(5)
    assert "exc" not in box and isinstance(box.get("waited"), float), box
    lock.exit("")


def test_bug6_lines_past_the_reference_stanza_are_rhythm_checked():
    text = "\n".join(S1) + "\n\n" + "\n".join(S1 + [LONG, LONG])
    assert analyze_ru(text).syllable_regular is False
    verdict = check_prosody(text, require_quatrains=False, require_rhyme=False, require_rhythm=True)
    assert not verdict.approved and verdict.reason


def test_bug6_guard_two_regular_quatrains_keep_their_rhythm():
    text = "\n".join(S1) + "\n\n" + "\n".join(S1)
    assert check_prosody(text, require_quatrains=False, require_rhyme=False, require_rhythm=True).approved


def test_bug7_a_failed_symbol_fetch_is_logged(caplog):
    v = inner_loop.LLMGate2Validator.__new__(inner_loop.LLMGate2Validator)
    v._search_agent = SimpleNamespace(run=lambda **kw: (_ for _ in ()).throw(RuntimeError("index gone")))
    v.base_dir = ROOT
    with caplog.at_level(logging.DEBUG, logger="tools.auto.inner_loop"):
        assert v._fetch_needed_flat(["MySym"], budget_per=50) == ""
    assert any("MySym" in r.getMessage() for r in caplog.records)


def _permission(lines):
    agent = backend._Agent.__new__(backend._Agent)
    feed = list(lines)
    agent._ask = lambda: feed.pop(0) if feed else None
    agent._say = lambda payload: None
    agent._model = "m"
    return agent._say_permission("c1", "bash", {"command": "ls"})


def test_bug8_an_unexpected_line_is_a_protocol_error():
    try:
        _permission([{"type": "something.else"}, {"type": "permission.reply", "reply": "once"}])
    except backend._AgentStopped as exc:
        assert "protocol error" in str(exc) and "something.else" in str(exc)
    else:
        raise AssertionError("the wait went on past an unexpected line")


def test_bug8_guard_a_reply_is_still_read():
    assert _permission([{"type": "permission.reply", "reply": "once", "message": "ok"}]) == ("once", "ok")


def _clock(monkeypatch, turn_max):
    n = iter(range(1, 100))
    monkeypatch.setattr(runner, "_churn", lambda ws: (lambda i: (i, i * 10))(next(n)))
    run = SimpleNamespace(workspace=SimpleNamespace(path=ROOT), agent=SimpleNamespace(name="a"))
    return runner._turn_deadline(run, SimpleNamespace(turn_extend_sec=60, turn_timeout_sec=100,
                                                      turn_max_sec=turn_max))


def test_bug9_turn_max_zero_still_extends_on_churn(monkeypatch):
    assert (_clock(monkeypatch, 0).on_deadline(100.0) or 0) > 0


def test_bug9_guard_a_cap_still_bounds_the_turn(monkeypatch):
    clock, total = _clock(monkeypatch, 150), 0.0
    for t in range(10):
        grant = clock.on_deadline(100.0 + 60 * t)
        if not grant:
            break
        total += grant
    assert 0 < total <= 50


def _handler(tmp_path):
    h = eh.ExhaustionHandler.__new__(eh.ExhaustionHandler)
    h._state = SimpleNamespace(agent_dir=tmp_path)
    return h


def test_bug10_a_ticket_store_that_cannot_open_names_the_store_path(tmp_path, monkeypatch):
    real = eh.make_ticket_store
    monkeypatch.setattr(eh, "make_ticket_store", lambda d: (_ for _ in ()).throw(OSError("read-only")))
    assert _handler(tmp_path)._open_ticket("T-1", "task", "t", "b") == real(tmp_path).path("T-1")


def test_bug10_a_path_that_raises_does_not_escape(tmp_path, monkeypatch):
    real = eh.make_ticket_store

    def store(d):
        ts = real(d)
        ts.path = lambda tid: (_ for _ in ()).throw(OSError("stat failed"))
        return ts
    monkeypatch.setattr(eh, "make_ticket_store", store)
    assert isinstance(_handler(tmp_path)._open_ticket("T-2", "task", "t", "b"), Path)
