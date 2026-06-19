"""tools/auto/pipeline.py
"""

from __future__ import annotations

import configparser
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # avoid circular imports at runtime
    from tools.auto.controller import AutoController

logger = logging.getLogger(__name__)

# ── PLAN phase module imports ─────────────────────────────────────────────────
# Imported at module level so test suites can patch via
# ``patch("tools.auto.pipeline.<name>")``.
from tools.auto.repo_ingest import ingest_repo
from tools.auto.architect import review_clusters, ClusterReviewer
from tools.auto.gate1_filter import filter_candidates
from tools.auto.backlog_prioritiser import build_backlog, to_improvements_md
from tools.auto.plan_emitter import PlanEmitter, IMPROVEMENTS_FILENAME


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-CR-20-4: Plan-validator factory
# ─────────────────────────────────────────────────────────────────────────────

def _build_plan_validator(
    cfg: configparser.ConfigParser,
    task_mode: str,
) -> "ClusterReviewer | None":
    """Return a :class:`ClusterReviewer` usable only for ``validate_plan``.

    Returns ``None`` when the feature is disabled or the mode is not creative,
    so the calling code stays a no-op with a simple ``if`` guard.
    """
    if task_mode != "creative":
        return None
    if not cfg.getboolean("architect", "validate_plan_creative", fallback=False):
        return None

    active     = cfg.get("api", "active", fallback="local")
    section    = f"api_{active}"
    base_url   = cfg.get(section, "base_url")
    api_key    = cfg.get(section, "api_key",    fallback="")
    model      = cfg.get(section, "model")
    api_fmt    = cfg.get(section, "api_format", fallback="openai")
    verify_ssl = cfg.getboolean("api", "verify_ssl", fallback=True)

    try:
        return ClusterReviewer(
            cfg, base_url, api_key, model,
            api_format=api_fmt,
            verify_ssl=verify_ssl,
            task_mode=task_mode,
        )
    except Exception as exc:  # noqa: BLE001 — never block the run on setup
        logger.warning("_build_plan_validator: could not build reviewer — %s", exc)
        return None





def run_pipeline(controller: "AutoController") -> tuple[Optional[str], int]:
    """Run the full autonomous pipeline; return (stop_reason, tasks_done).

    Parameters
    ----------
    controller:
        The live :class:`~tools.auto.controller.AutoController` instance.
        All shared state (goal, base_dir, config_path, state, git, limits,
        run_trace, progress_display, metrics_stream, auto_tuner) is read
        from it.

    Returns
    -------
    (stop_reason, tasks_done)
        Mirrors the return signature of ``_run_task_loop`` so the
        finalise block in ``controller.run()`` requires no changes.
        ``stop_reason`` is ``"runtime_cap"`` / ``"task_cap"`` / ``None``.
        ``tasks_done`` is the count of tasks completed this session.
    """
    cfg = _load_config(controller)

    # ── PLAN phase ────────────────────────────────────────────────────────────
    _run_plan_phase(controller, cfg)

    # ── Emit plan size into trace now that tasks are loaded ───────────────────
    # Emitted after _run_plan_phase so all_tasks() is populated regardless of
    # whether this is a fresh run or a resume.  analyze_logs.py reads this
    # event to show the correct total in the banner (plan=N).
    if controller.run_trace:
        from tools.agent_trace import tracer as _tracer
        _tracer.event(
            source="run_trace",
            target="auto_run",
            kind="plan_ready",
            params={
                "total_tasks": len(controller.state.all_tasks()),
                "run_id": getattr(controller.run_trace, "run_id", ""),
            },
        )

    # ── DRY-RUN: plan only, no code execution, no commits ─────────────────────
    # AUTO-G10: --dry-run exits here after writing IMPROVEMENTS.md + plan.json.
    # The return value mirrors _run_task_loop so the finalise block is unchanged.
    if getattr(controller, "dry_run", False):
        logger.info("run_pipeline: dry-run mode — skipping execution phase")
        controller.state.log("dry-run: plan phase complete; execution skipped")
        if controller.run_trace:
            controller.run_trace.log_phase("execute", "skipped (dry-run)")
        return None, 0  # clean finish, zero tasks executed

    # ── Pre-seed progress counters for resume runs ────────────────────────────
    # If this is a resume (plan phase was skipped), code_done must reflect tasks
    # that were already DONE in a previous session so the banner is correct.
    if controller.progress_display:
        from tools.auto.state import STATUS_DONE as _STATUS_DONE
        already_done = sum(
            1 for t in controller.state.all_tasks()
            if t.get("status") == _STATUS_DONE
        )
        if already_done > controller.progress_display.code_done:
            controller.progress_display.code_done = already_done
            controller.progress_display.refresh()

    # ── EXECUTE phase — G2 / G3 / G4 / G5 wired inside _run_task_loop ─────────
    return controller._run_task_loop(task_mode=getattr(controller, "task_mode", "code"), cfg=cfg)  # AUTO-DM-1


# ─────────────────────────────────────────────────────────────────────────────
# PLAN phase — AUTO-G1
# ─────────────────────────────────────────────────────────────────────────────


def _run_plan_phase(controller: "AutoController", cfg: configparser.ConfigParser) -> None:
    """Build and emit the plan when none exists yet; skip on resume.

    AUTO-G1 ACs
    -----------
    * Fresh ``--auto`` run produces a non-empty ``plan.json`` and a committed
      ``IMPROVEMENTS.md``.
    * Re-running skips the plan phase (resume path — ``plan.json`` already
      exists, tasks are loaded from it by ``StateStore.initialise``).
    * Check-less tasks land in the "Manual suggestions" section, excluded from
      auto-run (handled by ``BacklogPrioritiser`` — no extra logic needed here).

    The controller's ``state.all_tasks()`` is the authoritative indicator:
    if it is empty the plan has not been built yet (fresh) even if the file
    exists but is empty (edge case from interrupted very-first run).
    """
    has_plan = bool(controller.state.all_tasks())

    if has_plan:
        logger.info("plan_phase: plan already exists — skipping (resume)")
        controller.state.log("plan phase: skipped (plan already present)")
        if controller.run_trace:
            controller.run_trace.log_phase("plan", "skipped")
        return

    logger.info("plan_phase: no plan found — running PLAN phase")
    controller.state.log("plan phase: starting")
    if controller.run_trace:
        controller.run_trace.log_phase("plan", "started")

    # ── Step 1: Repo ingest ───────────────────────────────────────────────────
    logger.info("plan_phase: ingesting repo at %s", controller.base_dir)
    clusters = ingest_repo(controller.base_dir, cfg)
    logger.info("plan_phase: produced %d cluster(s)", len(clusters))
    controller.state.log(f"plan phase: ingested {len(clusters)} cluster(s)")

    if controller.progress_display:
        controller.progress_display.arch_total = len(clusters)
        controller.progress_display.refresh()

    # ── Step 2: Architect review ──────────────────────────────────────────────
    logger.info("plan_phase: architect reviewing %d cluster(s)", len(clusters))

    # Use PlanEmitter early so we can call changed_clusters() — skip clusters
    # whose file-lists haven't changed since the last emit (re-plan cheapness).
    # On a fresh first run the hash file doesn't exist yet, so all clusters are
    # returned unchanged.
    if controller.git is not None:
        _early_emitter = PlanEmitter(
            base_dir=controller.base_dir,
            state=controller.state,
            git=controller.git,
        )
        clusters_to_review = _early_emitter.changed_clusters(clusters)
        if len(clusters_to_review) < len(clusters):
            logger.info(
                "plan_phase: %d/%d cluster(s) unchanged — skipping their review",
                len(clusters) - len(clusters_to_review),
                len(clusters),
            )
            controller.state.log(
                f"plan phase: skipping {len(clusters) - len(clusters_to_review)} "
                f"unchanged cluster(s)"
            )
    else:
        _early_emitter = None
        clusters_to_review = clusters

    def _on_cluster_done():
        if controller.progress_display:
            controller.progress_display.tick_arch()

    candidates = review_clusters(
        clusters_to_review, controller.base_dir, cfg,
        goal=controller.goal,
        on_cluster_done=_on_cluster_done,
        task_mode=getattr(controller, "task_mode", "code"),   # AUTO-DM-1
        checkpoint_path=controller.state.agent_dir / "architect_checkpoint.json",
    )
    logger.info("plan_phase: architect produced %d candidate(s)", len(candidates))
    controller.state.log(f"plan phase: architect produced {len(candidates)} candidate(s)")

    # ── Step 2b: AUTO-CR-20-4 plan-validator (creative mode only) ─────────────
    # After the architect emits candidates and before Gate-1, check the plan
    # against the goal for contradictions / missing required facts.  Bounded by
    # plan_max_revisions; fail-open on any error.
    task_mode = getattr(controller, "task_mode", "code")
    _plan_validator = _build_plan_validator(cfg, task_mode)
    if _plan_validator is not None:
        _plan_max_rev = cfg.getint(
            "architect", "plan_max_revisions",
            fallback=cfg.getint("architect", "max_rewrites", fallback=1),
        )
        _plan_max_rev = max(1, _plan_max_rev)
        _plan_revisions = 0
        while _plan_revisions < _plan_max_rev:
            try:
                ok, reason = _plan_validator.validate_plan(controller.goal, candidates)
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning("plan_phase: validate_plan raised — %s; keeping plan.", exc)
                break
            if ok:
                break
            _plan_revisions += 1
            logger.info(
                "plan_phase: architect plan REVISE (%d/%d): %s",
                _plan_revisions, _plan_max_rev, reason,
            )
            controller.state.log(
                f"plan phase: architect plan REVISE ({_plan_revisions}/{_plan_max_rev}): {reason}"
            )
            # Re-run the architect with the feedback appended to the goal so
            # it can correct the offending task.  Skip the checkpoint on
            # feedback re-runs (pass None) — the corrected result should not
            # overwrite the original cache entry.
            augmented_goal = controller.goal + f"\n\nPLAN FEEDBACK: {reason}"
            try:
                candidates = review_clusters(
                    clusters_to_review, controller.base_dir, cfg,
                    goal=augmented_goal,
                    on_cluster_done=_on_cluster_done,
                    task_mode=task_mode,
                    checkpoint_path=None,
                )
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning(
                    "plan_phase: review_clusters re-run raised — %s; keeping previous candidates.", exc
                )
                break
        else:
            # Loop exited because _plan_revisions reached the cap without a break.
            logger.warning(
                "plan_phase: architect plan revision cap reached (%d) — keeping last candidates",
                _plan_max_rev,
            )
            controller.state.log(
                f"plan phase: architect plan revision cap reached ({_plan_max_rev})"
            )

    # ── Step 3: Gate 1 filter ─────────────────────────────────────────────────
    logger.info("plan_phase: gate1 filtering %d candidate(s)", len(candidates))
    # Build cluster → file-set mapping so Gate 1 can detect hallucinated paths.
    cluster_files: dict[str, set[str]] = {
        c.name: set(c.files) for c in clusters
    }
    accepted, rejected = filter_candidates(
        candidates, controller.base_dir, cfg,
        cluster_files=cluster_files,
        task_mode=getattr(controller, "task_mode", "code"),   # AUTO-DM-1
    )
    logger.info(
        "plan_phase: gate1 accepted=%d rejected=%d",
        len(accepted), len(rejected),
    )
    controller.state.log(
        f"plan phase: gate1 accepted={len(accepted)} rejected={len(rejected)}"
    )

    if controller.run_trace:
        for r in rejected:
            controller.run_trace.log_gate1_rejected(
                getattr(r, "candidate", None) and getattr(r.candidate, "title", "?"),
                getattr(r, "reason", ""),
            )

    # ── Step 4: Backlog prioritise ────────────────────────────────────────────
    task_id_prefix = cfg.get("auto", "task_id_prefix", fallback="AUTO-T")
    backlog = build_backlog(accepted, task_id_prefix=task_id_prefix)
    logger.info(
        "plan_phase: backlog built — %d auto, %d manual",
        len(backlog.auto_tasks), len(backlog.manual_suggestions),
    )
    controller.state.log(
        f"plan phase: backlog — {len(backlog.auto_tasks)} auto task(s), "
        f"{len(backlog.manual_suggestions)} manual suggestion(s)"
    )

    if controller.progress_display:
        controller.progress_display.code_total = len(backlog.auto_tasks)

    # ── Step 5: Plan emit + git commit ────────────────────────────────────────
    if controller.git is None:
        # Git not available — still upsert tasks and write IMPROVEMENTS.md
        # but skip the commit step.  A subsequent run with git will commit.
        logger.warning("plan_phase: git not available — emitting without commit")
        _emit_without_git(controller, backlog, clusters)
    else:
        emitter = _early_emitter or PlanEmitter(
            base_dir=controller.base_dir,
            state=controller.state,
            git=controller.git,
        )
        commit_hash = emitter.emit(backlog, clusters)
        if commit_hash:
            controller.state.log(f"plan phase: committed plan ({commit_hash[:12]})")
            logger.info("plan_phase: plan committed — %s", commit_hash[:12])
        else:
            controller.state.log("plan phase: plan unchanged — no new commit")
            logger.info("plan_phase: plan unchanged — nothing to commit")

    controller.state.log("plan phase: complete")
    logger.info("plan_phase: done")
    if controller.run_trace:
        controller.run_trace.log_phase("plan", "done")


def _emit_without_git(
    controller: "AutoController",
    backlog,
    clusters,
) -> None:
    """Fallback: upsert tasks + write IMPROVEMENTS.md when git is unavailable."""
    md_content = to_improvements_md(backlog)
    md_path = controller.base_dir / IMPROVEMENTS_FILENAME
    md_path.write_text(md_content, encoding="utf-8")
    logger.info("_emit_without_git: wrote %s", md_path)

    for task in backlog.to_state_tasks():
        controller.state.upsert_task(task)
    logger.info(
        "_emit_without_git: upserted %d task(s) into plan.json",
        len(backlog.auto_tasks),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _load_config(controller: "AutoController") -> configparser.ConfigParser:
    """Return the controller's parsed agents.ini.

    Prefer the already-parsed ``controller.config`` (set in ``__init__``) so the
    documented single-parse guarantee holds and any in-memory mutation of the
    config is honoured in the plan/execute phases.  Falls back to reading
    ``config_path`` from disk only when no cached config is present (e.g. a
    controller built via ``__new__`` in tests).
    """
    cfg = getattr(controller, "config", None)
    if cfg is not None:
        return cfg
    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    p = Path(controller.config_path)
    if p.exists():
        cfg.read(p, encoding="utf-8")
    return cfg