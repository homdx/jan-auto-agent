"""tests/test_contest_window_readback.py — round 153: a patched window is read back, and a window Kilo keeps of its own keeps the watch armed.

Live, 7.6.2: ``KILO_CONFIG_CONTENT`` outranks a workspace's ``.kilo/kilo.jsonc``
field by field. A ``PATCH /config`` for a model whose ``limit`` the spawn's
overlay sets answers 200, writes the file, disposes the instance and reopens the
stream — and the next ``GET /provider`` still reports the overlay's limit. Round
151 marked the window pushed on the 200 and stood the in-turn watch down, so the
turn ran to the old, larger window: the refusal the memory had just recorded.

The fix reads the model back after the patch and compares it with what was sent.
Only a match marks the window as pushed; anything else keeps the watch armed and
is remembered, so the size is not re-patched — and Kilo reloaded — every prompt.

Offline: ``tests/_kilo_fake.py`` on 127.0.0.1, no live provider config and no
network beyond the loopback fake.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import test_contest_context_memory as ctm  # noqa: E402
import test_contest_overflow_wording as tow  # noqa: E402
import test_contest_runner as tr  # noqa: E402
from _kilo_fake import FakeKiloServer  # noqa: E402
from tools.contest.backend import (  # noqa: E402
    ContestBackendError,
    KiloBackend,
    KiloLimitKept,
    KiloServer,
)
from tools.contest.context_memory import (  # noqa: E402
    DEFAULT_COMPACT_AT_PERCENT,
    kilo_limit,
)
from tools.contest.kilo_client import KiloClient  # noqa: E402

#: Kilo's declared window for the model, and the size the memory remembers below
#: it — round 145's glm-4.5-flash, the same pair round 148's watch was built on.
DECLARED = tow.DECLARED
REFUSED_AT = tow.REFUSED_AT

#: The model the one-agent roster below runs.
MODEL = "agent-a:free"

#: The ``limit`` the spawn's ``KILO_CONFIG_CONTENT`` carries for the model: the
#: overlay's own window, which the server keeps whatever a patch says.
ENV_LIMIT = kilo_limit(DECLARED, None, DEFAULT_COMPACT_AT_PERCENT)

#: The smaller window the memory remembers, and what the patch asks for.
PUSHED = kilo_limit(REFUSED_AT, None, DEFAULT_COMPACT_AT_PERCENT)


def _watch_scenario(**extra) -> dict:
    """One turn at 91 % of the remembered size that asks nothing, then the work:
    round 148's firing line for the in-turn watch.

    *extra* goes to the fake's scenario — ``env_limits``, ``providers_status`` —
    so a run reads the offer the way the round's server would.
    """
    scenario = {"abort_idles": True, "summary_tokens": 3_000, "turns": [
        {"events": ["busy"], "idle": False,
         "message_info": {"tokens": {"input": 90_000, "output": 0}}},
        {"on_prompt": tr.work_ready, "events": ["busy", "idle"]},
    ]}
    scenario.update(extra)
    return scenario


def _run_watch(tmp_path, scenario=None, **extra):
    """A run with the memory at 98 777, Kilo declared at 131 072, the watch on.

    *scenario* is the watch's firing line by default; pass one to run a single
    turn instead. *extra* goes to the fake's scenario.
    """
    memory = tow._remembered(tmp_path)
    config = tow._declared(
        ctm._config(tmp_path, memory=memory, context_watch_sec=0.2))
    if scenario is None:
        scenario = _watch_scenario(**extra)
    return tr._run_one(tmp_path, scenario, config)


def _patches(fake) -> list:
    """The ``PATCH /config`` calls as ``(directory, body)``, in order."""
    return [(record["query"].get("directory"), record["body"])
            for record in fake.calls("PATCH")]


def test_a_limit_the_server_keeps_of_its_own_keeps_the_watch_armed(tmp_path, caplog):
    """The patch answers 200, writes the file, reloads and reopens the stream —
    and the offer still reports the overlay's limit. Nothing went over: one patch
    for the run, no window handed to Kilo, the watch stops the turn at the size
    the memory just recorded, and one line that says so."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    sb, fake, _h, run, aborted = _run_watch(
        tmp_path, env_limits={MODEL: ENV_LIMIT})
    tr._assert_ready(run, sb.ws("agent-a"))

    patches = _patches(fake)
    assert len(patches) == 1, "one patch for the run: the size is remembered"
    directory, body = patches[0]
    limit = body["provider"]["kenary"]["models"][MODEL]["limit"]
    assert limit == PUSHED, "the patch asked for the smaller window"
    assert fake.provider_body(directory)["all"][0]["models"][MODEL]["limit"] == ENV_LIMIT, \
        "and the server kept its own"

    # the watch, not the provider, stopped the turn at the remembered size
    assert aborted
    assert run.turns[0]["context_watch_stop"] == 91.1

    lines = [record.getMessage() for record in caplog.records
             if "from KILO_CONFIG_CONTENT" in record.getMessage()]
    assert len(lines) == 1, "one warning for the run, not one per prompt"
    assert f"{ENV_LIMIT['context']:,}" in lines[0]
    assert "the watch stays armed" in lines[0]


def test_a_model_with_no_limit_of_its_own_takes_the_patch(tmp_path, caplog):
    """No env limit: the patch lands, the read-back matches it, and the watch
    stands down as before — Kilo compacts by the window at the same fill the
    watch would fire at."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    sb, fake, _h, run, _aborted = _run_watch(tmp_path, scenario={
        "turns": [{"on_prompt": tr.work_ready, "events": ["busy", "idle"],
                   "message_info": {"tokens": {"input": 90_000, "output": 0}}}]})
    tr._assert_ready(run, sb.ws("agent-a"))

    patches = _patches(fake)
    assert len(patches) == 1
    directory, body = patches[0]
    assert body["provider"]["kenary"]["models"][MODEL]["limit"] == PUSHED
    assert fake.patched_limit(directory, MODEL) == PUSHED

    # the read-back ran, after the patch, and it is what stood the watch down
    calls = [(record["method"], record["path"]) for record in fake.calls()]
    assert ("GET", "/provider") in calls, "the push was read back"
    assert calls.index(("GET", "/provider")) > calls.index(("PATCH", "/config"))

    assert "context_watch_stop" not in run.turns[0], \
        "the watch stood down: Kilo compacts by the window"
    assert not [record for record in caplog.records
                if "watch stays armed" in record.getMessage()]


def test_a_read_back_that_fails_keeps_the_watch_armed(tmp_path, caplog):
    """An offer the server would not answer: the push cannot be proved, so it is
    not remembered as handed over and the watch still stands."""
    caplog.set_level(logging.INFO, logger="tools.contest.runner")
    sb, fake, _h, run, aborted = _run_watch(tmp_path, providers_status=500)
    tr._assert_ready(run, sb.ws("agent-a"))

    assert len(_patches(fake)) == 1
    assert aborted and run.turns[0]["context_watch_stop"] == 91.1
    lines = [record.getMessage() for record in caplog.records
             if "watch stays armed" in record.getMessage()]
    assert len(lines) == 1
    assert "did not take the patched window" in lines[0]


def test_the_backend_reports_a_limit_the_server_kept(tmp_path):
    """The read-back itself: the patch answered and the reload was followed, but
    the offer still reports the server's own limit — `KiloLimitKept` carrying
    that number, so the caller can remember the size and keep the watch armed.
    The reconnect still ran: the stream is this round's, the window is not."""
    ws = tmp_path / "ws"
    ws.mkdir()
    with FakeKiloServer({"env_limits": {MODEL: ENV_LIMIT}}) as fake:
        backend = KiloBackend(KiloServer.attach(fake.url), str(ws),
                              events_log=str(tmp_path / "out" / "events.jsonl"))
        backend.wait_ready()
        old_tap = backend._tap

        with pytest.raises(KiloLimitKept) as caught:
            backend.set_model_limit("kenary", MODEL, PUSHED)

        assert caught.value.kept == ENV_LIMIT["context"]
        assert isinstance(caught.value, ContestBackendError), \
            "one class for every push failure the runner catches"
        assert len(fake.calls("PATCH")) == 1, "the patch went out, once"
        assert backend._tap is not old_tap, "the reload's reconnect still ran"
        assert fake.provider_body(str(ws))["all"][0]["models"][MODEL]["limit"] == ENV_LIMIT
        backend.close()


def test_the_read_back_is_fresh_and_never_raises(tmp_path):
    """The read-back is not the cached offer: a second patch is seen, and a
    shape it does not know — a model with no limit, a provider that is not
    offered, an offer that would not answer — comes back empty, never a raise."""
    with FakeKiloServer() as fake:
        client = KiloClient(KiloServer.attach(fake.url), str(tmp_path))
        assert client.model_limits("kenary", "hy3:free") is None, \
            "no limit in the offer is no limit"
        client.set_model_limit("kenary", "hy3:free", PUSHED)
        assert client.model_limits("kenary", "hy3:free") == PUSHED
        client.set_model_limit("kenary", "hy3:free",
                               {"context": 50_000, "input": 40_000, "output": 10_000})
        assert client.model_limits("kenary", "hy3:free")["context"] == 50_000, \
            "a later patch is seen"
        assert client.model_limits("kenary", "agent-a:free") is None, \
            "a model with no limit of its own"
        assert client.model_limits("kenary", "not-offered:free") is None, \
            "a model the offer does not have"
        assert client.model_limits("not-a-provider", "hy3:free") is None, \
            "a provider the offer does not have"
        assert client.model_limits("kenary", "hy3:free") is not None, \
            "and the first read is still there"

    with FakeKiloServer({"providers_status": 500}) as refused:
        client = KiloClient(KiloServer.attach(refused.url), str(tmp_path))
        assert client.model_limits("kenary", "hy3:free") is None, \
            "an offer it would not read is no read-back"
