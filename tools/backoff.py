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
    """Return wait time (s) for the nth consecutive API error (0-indexed)."""
    return BACKOFF_SERIES[min(consecutive_error_index, len(BACKOFF_SERIES) - 1)]


def _now() -> str:
    """Current local date-time for console display."""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── state persistence ─────────────────────────────────────────────────────────

def save_state(state: Dict[str, Any], path: Path = STATE_FILE) -> None:
    """Write loop checkpoint to JSON (utf-8, pretty-printed)."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_state(path: Path = STATE_FILE) -> Optional[Dict[str, Any]]:
    """Return saved checkpoint dict, or None if the file is absent / corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


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
        save_state(state, path)
        loop = state.get("loop", "unknown")
        it   = state.get("iteration", "?")
        print(f"  ▶  Restart the program — "
              f"it will offer to resume '{loop}' from iteration {it}.")
        sys.exit(0)
    print(f"  ▶  [{_now()}] Backoff complete — retrying …\n")
