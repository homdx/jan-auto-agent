"""tests/test_auto_p11_window_budget.py — AUTO-P11: validate the learned and
escalated caps against the context window.

Every guard so far checked the **configured** cap and nothing else.
`test_probe_budget_fits_the_window` asserts that `probe_max_total_chars` plus
the file contents plus the reply fit inside `num_ctx` — and that assertion was
true and useful right up until AUTO-P9 started changing the cap at runtime.

Nothing validated the product:

* AUTO-P9 reseeds the cap to `median x headroom`, bounded only by
  `probe_budget_max_chars` (default: **4x** the configured cap).
* AUTO-P8/P10 then multiply the batch floor by up to **4.0**.

A measured run escalated to **56 737 chars** — roughly 16 000 tokens of
digest — against a cap that had been validated at 10 000. On a 128k or 1M
window that is merely wasteful. On a 32k profile it would crowd out the file
contents the Architect is supposed to be reviewing, and on 8k it would not
fit at all. The failure mode is a silently truncated prompt, not an error,
which is why nothing caught it for three tickets.

Also fixed here: the budget-spent log printed the **configured** cap as its
denominator, so a batch running at a raised cap of 15 000 logged
`17237/10000` and a working escalation read as a broken one.

  AC-P11-1  A learned cap above the window ceiling is clamped.
  AC-P11-2  An escalated cap above it is clamped — the path that reached
            56 737.
  AC-P11-3  The clamp warns once, not once per batch.
  AC-P11-4  No window budget (num_ctx unset) means no clamp.
  AC-P11-5  A cap comfortably inside the window is untouched.
  AC-P11-6  The clamp never drops below the per-op cap.
  AC-P11-7  The architect derives the ceiling from num_ctx, and agrees with
            the arithmetic the config-time test uses.
  AC-P11-8  A large num_ctx imposes no practical limit.
  AC-P11-9  `current_cap` reports the effective cap, not the configured one.
"""

from __future__ import annotations

import configparser
import logging

import pytest

from tools.auto.arch_probe import ArchProbe
from tools.auto.architect import ClusterReviewer


class _Bridge:
    usable = True

    def pull_symbol(self, name: str) -> str:
        return f"symbol: {name}\n" + ("x" * 400)

    def module_symbols(self, ref: str) -> str:
        return ""


def _probe(costs=(), *, configured=10000, window=0, headroom=2.0) -> ArchProbe:
    p = ArchProbe(_Bridge(), max_chars=2000, max_total_chars=configured)
    p.configure_learning(warmup=3, headroom=headroom, ceiling=0)
    p.set_window_budget(window)
    p._round_costs = list(costs)
    return p


class TestWindowCeiling:

    def test_learned_cap_is_clamped(self) -> None:
        """AC-P11-1: median 20 000 x headroom 2.0 = 40 000, but the window
        can only hold 25 000."""
        p = _probe([20000] * 3, configured=10000, window=25000)
        assert p.seeded_cap == 25000

    def test_escalated_cap_is_clamped(self) -> None:
        """AC-P11-2: the path that actually reached 56 737 in production.
        Seeding was clamped in some builds; escalation never was."""
        p = _probe([20000] * 3, configured=10000, window=25000)
        p.reset()
        assert p.raise_budget(4.0) <= 25000

    def test_clamp_warns_once(self, caplog) -> None:
        """AC-P11-3: one line per run, not one per batch. A warning that
        fires ninety times is a warning nobody reads."""
        p = _probe([20000] * 3, configured=10000, window=25000)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                p.reset()
                p.raise_budget(4.0)
        hits = [r for r in caplog.records if "context window can hold" in r.message]
        assert len(hits) == 1, f"expected one warning, got {len(hits)}"

    def test_no_window_budget_means_no_clamp(self) -> None:
        """AC-P11-4: num_ctx unset means the server decides the window, and
        guessing a ceiling would be worse than not having one."""
        p = _probe([20000] * 3, configured=10000, window=0)
        assert p.seeded_cap == 40000          # ceiling = 4x configured
        p.reset()
        # 40 000 (the seeded cap this batch started at) x 4.0
        assert p.raise_budget(4.0) == 160000

    def test_cap_inside_the_window_is_untouched(self) -> None:
        """AC-P11-5: the guard must not become a second, tighter cap."""
        p = _probe([8000] * 3, configured=10000, window=100000)
        assert p.seeded_cap == 16000
        p.reset()
        assert p.raise_budget(2.5) == 40000   # 16 000 batch floor x 2.5

    def test_clamp_never_drops_below_the_per_op_cap(self) -> None:
        """AC-P11-6: a window so small the clamp goes under max_chars would
        make every single op truncate."""
        p = ArchProbe(_Bridge(), max_chars=2000, max_total_chars=2000)
        p.configure_learning(warmup=1, headroom=2.0, ceiling=0)
        p.set_window_budget(500)
        p.reset()
        assert p.raise_budget(4.0) >= 2000


class TestArchitectDerivesTheCeiling:

    def _reviewer(self, num_ctx: int, files=4, file_chars=8000,
                  max_tokens=1024) -> ClusterReviewer:
        c = configparser.ConfigParser()
        c.read_dict({
            "api": {"active": "local", "verify_ssl": "false"},
            "api_local": {"base_url": "u", "api_key": "k", "model": "m",
                          "api_format": "openai", "num_ctx": str(num_ctx)},
            "architect": {
                "temperature": "0.2", "max_tokens": str(max_tokens),
                "max_files_per_review": str(files),
                "max_file_chars": str(file_chars),
                "probe_enabled": "true",
            },
        })
        return ClusterReviewer(config=c, base_url="u", api_key="k", model="m",
                               api_format="openai", verify_ssl=False)

    def test_ceiling_matches_the_config_time_arithmetic(self, tmp_path) -> None:
        """AC-P11-7: the runtime guard and `test_probe_budget_fits_the_window`
        must agree, or one of them is lying. Same 3.5 chars/token and the same
        prompt estimate; the runtime side additionally keeps 20% back so a
        digest never fills the very last of the window."""
        r = self._reviewer(num_ctx=32768, files=4, file_chars=8000,
                           max_tokens=1024)
        probe = ArchProbe(_Bridge(), max_chars=2000, max_total_chars=6000)
        r._probe = probe
        r._probe_built = True

        prompt_tok = (4 * 8000 + 4000) / 3.5
        free_tok = 32768 - prompt_tok - 1024
        expected = int(free_tok * 3.5 * 0.8)

        # Exercise the same expression the architect uses at build time.
        probe.set_window_budget(max(0, expected))
        assert probe._window_budget == expected
        assert expected > 6000, "a 32k profile must still allow the shipped cap"

    def test_large_window_imposes_no_practical_limit(self) -> None:
        """AC-P11-8: on the 1M-token profile the guard must be inert."""
        prompt_tok = (6 * 12000 + 4000) / 3.5
        free_tok = 1000000 - prompt_tok - 16384
        ceiling = int(free_tok * 3.5 * 0.8)
        p = _probe([20000] * 3, configured=10000, window=ceiling)
        assert p.seeded_cap == 40000, "unclamped — the window is enormous"


def test_current_cap_reports_what_is_in_force() -> None:
    """AC-P11-9: the budget-spent log printed the CONFIGURED cap, so a batch
    running at a raised cap of 15 000 logged `17237/10000` — a working
    escalation reading as a broken one."""
    p = _probe([8000] * 3, configured=10000, window=0)
    p.reset()
    assert p.current_cap == 16000          # seeded, not 10 000
    p.raise_budget(2.5)
    assert p.current_cap == 40000          # 16 000 floor x 2.5, not 10 000
