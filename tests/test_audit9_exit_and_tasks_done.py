"""AUDIT-9 stop status and completed-task trace regressions."""

from __future__ import annotations

import configparser
from types import SimpleNamespace

from tools.auto import controller as controller_mod
from tools.auto import outer_loop as outer_mod
from tools.auto.controller import AutoController, STOP_UNSAVED_WORK
from tools.auto.outer_loop import OuterLoop, _TaskBudget


def test_unsaved_work_stop_returns_nonzero(monkeypatch, tmp_path):
    ctl = AutoController.__new__(AutoController)
    ctl._time_fn = lambda: 1.0
    ctl._print_banner = lambda: None
    ctl.goal = "goal"
    ctl.base_dir = tmp_path
    ctl.agent_dir = tmp_path / ".agent"
    ctl.config = configparser.ConfigParser()
    ctl.task_mode = "code"
    ctl.state = SimpleNamespace(
        initialise=lambda *_: True, all_tasks=lambda: [],
        resume_info=lambda: {},
        update_progress=lambda **_: None, log=lambda *_: None,
    )
    ctl._cfg = lambda: ctl.config
    ctl._setup_git = lambda: None
    ctl._reset_resettable_blocked_tasks = lambda *_: None
    ctl._log_limits = lambda: None
    ctl._log_auto_prompts = lambda: None
    ctl._handle_cap = lambda *_: None
    ctl.run_trace = None
    ctl._start_time = 0
    monkeypatch.setattr(controller_mod, "setup_run_trace", lambda *_: SimpleNamespace(log_run_start=lambda *_: None))
    monkeypatch.setattr(controller_mod, "make_progress_display", lambda *_, **__: SimpleNamespace(code_total=0))
    monkeypatch.setattr(controller_mod, "AutoMetricsStream", lambda *_: None)
    monkeypatch.setattr(controller_mod, "make_auto_tuner", lambda *_: None)
    import tools.auto.pipeline as pipeline
    monkeypatch.setattr(pipeline, "run_pipeline", lambda *_: (STOP_UNSAVED_WORK, 0))
    assert ctl.run() != 0


def test_passed_task_trace_direction_is_outer_loop_to_controller(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(outer_mod.tracer, "event", lambda *args, **kwargs: events.append((args, kwargs)))
    loop = OuterLoop.__new__(OuterLoop)
    loop.inner_loop = SimpleNamespace(run_task=lambda *_args, **_kwargs: SimpleNamespace(
        passed=True, attempts_used=1, deadline_credit_s=0,
    ))
    loop.state = SimpleNamespace(
        set_task_status=lambda *_args, **_kwargs: None,
        get_task=lambda *_: {"round": 0},
        increment_task_counters=lambda *_args, **_kwargs: None,
        log=lambda *_: None,
    )
    loop.max_rounds = 1
    loop.rewrite_every_n_rounds = 2
    loop.max_rewrites = 0
    loop.task_rewriter = None
    loop._begin_task_budget = lambda *_: _TaskBudget(0)
    loop._end_task_budget = lambda *_: None
    loop._existing_rounds = lambda *_: 0
    loop._feedback_paths = lambda *_: []
    loop._read_round_feedback = lambda *_: []
    loop._build_impl_history = lambda *_: []
    result = loop.run_task({"id": "T1", "instruction": "do it"}, tmp_path)
    assert result.passed
    passed = [args for args, kwargs in events
              if len(args) >= 3 and args[2] == "result"
              and kwargs.get("params", {}).get("passed")]
    assert passed and passed[-1][:2] == ("outer_loop", "controller")
