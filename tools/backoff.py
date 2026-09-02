"""
Exponential back-off with checkpoint persistence for API retry loops (Issue 7).

Backoff series (seconds):  1  2  4  8  16  32  64  128  256  512  1024

  Attempts completed before each milestone pause:
    Before    1s pause :  1 attempt   (error 1  → BACKOFF_SERIES[0] = 1s)
    Before    2s pause :  2 attempts  (error 2  → BACKOFF_SERIES[1] = 2s)
    Before 1024s pause : 11 attempts  (error 11 → BACKOFF_SERIES[10] = 1024s, cap)

On KeyboardInterrupt *during* a sleep the caller's loop state is written to
``pipeline_state.json`` in the current working directory; restarting the
program will detect the file and offer to resume from the saved iteration.
"""
import json
import os
import tempfile
import sys
import time
import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Powers-of-two seconds, capped at 1024 s at index 10.
BACKOFF_SERIES: List[int] = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]

# Human-readable milestone summary — printed once on first API error.
MILESTONE_TABLE: str = (
    "  Backoff schedule (consecutive API errors → wait before next attempt):\n"
    "    Error  1 →    1 s   │  Error  5 →   16 s  │  Error  9 →  256 s\n"
    "    Error  2 →    2 s   │  Error  6 →   32 s  │  Error 10 →  512 s\n"
    "    Error  3 →    4 s   │  Error  7 →   64 s  │  Error 11+ → 1024 s (cap)\n"
    "    Error  4 →    8 s   │  Error  8 →  128 s  │\n"
    "\n"
    "  Attempts before   1s pause :  1\n"
    "  Attempts before   2s pause :  2\n"
    "  Attempts before 1024s pause: 11"
)

STATE_FILE: Path = Path("pipeline_state.json")


# ── helpers ──────────────────────────────────────────────────────────────────

def backoff_seconds(consecutive_error_index: int) -> int:
    """Return wait time (s) for the nth consecutive API error (0-indexed).

    Hardening: the index is clamped into ``[0, len-1]``. Without the lower
    clamp a negative index (e.g. a caller that computed ``count - 1`` before
    incrementing ``count``) would hit ``BACKOFF_SERIES[-1]`` — Python's
    negative indexing — and silently return the 1024 s CAP as the *first*
    wait instead of 1 s. All current callers increment before calling, so
    this is defensive, but a public helper must not turn an off-by-one at a
    call site into a 17-minute stall.
    """
    idx = max(0, min(consecutive_error_index, len(BACKOFF_SERIES) - 1))
    return BACKOFF_SERIES[idx]


def _now() -> str:
    """Current local date-time for console display."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── retry helpers ─────────────────────────────────────────────────────────────
#
# Two shared helpers that used to be copy-pasted at every call site:
#
#   retry_with_backoff  — the bounded "call / on exception sleep
#                         backoff_seconds(n) / retry" loop (run_search's
#                         single-file and per-chunk paths, FaqAgent's
#                         _answer_legacy, collect's summarize_repo).
#   api_error_pause     — the Issue-7 "consecutive API error" block
#                         (milestone table once, backoff_seconds(n-1),
#                         sleep_with_interrupt_save with a checkpoint) used
#                         by main.py's run_pipeline and actions.py's
#                         run_text_qa / run_edit.


def retry_with_backoff(call, attempts: int = 3, sleep_fn=None,
                       on_retry=None, on_error=None):
    """Call *call* up to ``attempts`` times, sleeping between failures.

    ``call`` is invoked with no arguments; any exception it raises aborts
    nothing — the loop sleeps ``backoff_seconds(failure_index)`` seconds
    and tries again, up to ``attempts`` total calls. ``sleep_fn`` replaces
    ``time.sleep`` (tests pass a no-op/recorder); ``on_retry(exc, attempt,
    wait)`` is called before each sleep for a caller-specific log/print;
    ``on_error(exc, attempt)`` (1-indexed, like the call sites' error_count)
    is called on EVERY failure including the final one.

    Returns whatever the first successful ``call()`` returns, or raises the
    LAST exception if every attempt failed. A falsy ``attempts`` means one
    try with no retry (still raises on failure, never sleeps).
    """
    sleep = sleep_fn or time.sleep
    attempts = max(1, int(attempts))
    last_exc: "BaseException | None" = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — one bad call must not kill the loop
            last_exc = exc
            if on_error is not None:
                on_error(exc, attempt + 1)
            if attempt < attempts - 1:
                wait = backoff_seconds(attempt)
                if on_retry is not None:
                    on_retry(exc, attempt + 1, wait)
                sleep(wait)
    assert last_exc is not None  # attempts >= 1 and no return ⇒ at least one raise
    raise last_exc


def api_error_pause(error_count: int, checkpoint: Dict[str, Any],
                    sleep_fn=None, path: Path = STATE_FILE) -> None:
    """Pause before retrying after *error_count* consecutive API errors.

    This is the Issue-7 block that used to be repeated verbatim in every
    validated loop (run_pipeline / run_text_qa / run_edit):

      * print the MILESTONE_TABLE once (on the first error),
      * compute ``backoff_seconds(error_count - 1)``,
      * sleep via :func:`sleep_with_interrupt_save`, which saves *checkpoint*
        to *path* and exits cleanly on KeyboardInterrupt.

    ``error_count`` is the ALREADY-INCREMENTED count of consecutive errors
    (1 = first error ⇒ 1 s wait). ``sleep_fn`` is accepted for symmetry with
    the other helpers; the checkpoint-saving sleep has no injectable clock
    by design — interrupt handling is the whole point of this path.
    """
    if error_count == 1:
        print(MILESTONE_TABLE)
    wait = backoff_seconds(error_count - 1)
    sleep_with_interrupt_save(wait, checkpoint, path)


# ── state persistence ─────────────────────────────────────────────────────────

def save_state(state: Dict[str, Any], path: Path = STATE_FILE) -> None:
    """Write loop checkpoint to JSON (utf-8, pretty-printed).

    BUGFIX: this used to open *path* directly with mode "w", which
    TRUNCATES the target to zero bytes before writing a single byte of the
    new content. Every consumer of this checkpoint exists specifically to
    survive a crash/interrupt mid-run (tools/collect/summarizer.py's Pass B
    batch, tools/faq_agent.py, tools/actions.py) — but a crash, SIGKILL, or
    full disk during THIS write left a truncated, unparseable state.json,
    which load_state()'s error handling correctly treats as "corrupt" and
    discards. The mechanism whose entire purpose is surviving an
    interruption was itself destroyed by one. Reproduced directly:

        BEFORE crash: real checkpoint saved, {"loop": ..., "modules": {"a.py": ...
        [write interrupted mid-flight]
        AFTER crash mid-write, load_state() returns: None
          -> entire checkpoint treated as absent; ALL prior progress lost

    For summarize_repo specifically, this is called after every single
    module in a batch that can run for a long time against a real LLM —
    exactly the kind of long-running operation most likely to be
    interrupted, and where losing all prior progress is most costly.

    Fixed with the same write-to-temp-then-os.replace pattern already used
    throughout tools/auto/ (utils.atomic_write_text) — not imported from
    there to avoid a tools.backoff -> tools.auto dependency this module
    doesn't otherwise have; the pattern itself is a few lines.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def load_state(path: Path = STATE_FILE) -> Optional[Dict[str, Any]]:
    """Return saved checkpoint dict, or None if the file is absent / corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        # AUTO-BACKOFF-GUARD-1: UnicodeDecodeError (raised when the file's
        # bytes aren't valid UTF-8 — e.g. a checkpoint truncated mid-write
        # inside a multi-byte character) is a ValueError subclass, NOT an
        # OSError, so it wasn't caught here despite this function's own
        # documented "corrupt -> None" contract.
        return None
    # Hardening: a checkpoint is always written as a JSON object by
    # save_state(). A file that parses cleanly but holds a JSON list / string
    # / number / null (hand-edited or truncated-then-rewritten) used to be
    # returned as-is, and every consumer immediately calls ``.get(...)`` on
    # it — so main.py's resume path would crash with AttributeError on a
    # non-dict instead of treating the state as absent/corrupt (which is the
    # documented contract: "None if the file is absent / corrupt").
    if not isinstance(data, dict):
        return None
    return data


def clear_state(path: Path = STATE_FILE) -> None:
    """Delete the checkpoint file (after a clean exit or user declines resume)."""
    path.unlink(missing_ok=True)


# ── sleep with interrupt handling ─────────────────────────────────────────────

def sleep_with_interrupt_save(
    seconds: int,
    state: Dict[str, Any],
    path: Path = STATE_FILE,
) -> None:
    """
    Sleep for *seconds*, printing wall-clock timestamps before and after.

    On KeyboardInterrupt during the wait:
      • saves *state* to *path* (checkpoint)
      • prints a one-line resume hint
      • calls ``sys.exit(0)`` — callers do **not** need their own try/except
    """
    print(f"\n  ⏸  [{_now()}] API unavailable — "
          f"next retry in {seconds}s …")
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        print(f"\n  💾 [{_now()}] Interrupted — "
              f"saving checkpoint to '{path}'")
        # AUTO-FIX (medium-priority audit, DeepSeek-plan finding): a
        # save_state() failure here (disk full, permission error — exactly
        # the kind of thing more likely mid-interrupt, e.g. a Ctrl-C
        # during a full-disk condition) used to raise a brand-new exception
        # from inside the KeyboardInterrupt handler, replacing the original
        # interrupt with a confusing secondary traceback and skipping the
        # resume-hint message and clean sys.exit(0) below. Best-effort: if
        # the checkpoint can't be saved, say so plainly and still exit
        # cleanly — the interrupt itself must always win.
        try:
            save_state(state, path)
        except (OSError, TypeError, ValueError) as exc:
            # AUTO-FIX: save_state() json.dumps a caller-supplied dict, so
            # a non-serializable value (a set, a datetime) raises TypeError
            # and some edge cases ValueError — neither was caught, which is
            # exactly the "new exception inside a KeyboardInterrupt handler"
            # the comment above exists to prevent.
            print(f"  ⚠  Could not save checkpoint: {exc}")
            print("  ▶  Restart will NOT be able to resume from this point.")
            sys.exit(0)
        loop = state.get("loop", "unknown")
        it   = state.get("iteration", "?")
        print(f"  ▶  Restart the program — "
              f"it will offer to resume '{loop}' from iteration {it}.")
        sys.exit(0)
    print(f"  ▶  [{_now()}] Backoff complete — retrying …\n")
