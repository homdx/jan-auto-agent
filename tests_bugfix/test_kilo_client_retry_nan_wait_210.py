"""tests_bugfix/test_kilo_client_retry_nan_wait_210.py — ticket 210, bug 49.

``tools/contest/kilo_client.py``'s ``_retry_past_the_bound`` compares
``wait <= bound``; in Python ``nan <= bound`` is always ``False``, so a
``NaN`` wait used to fall through to the quota/backoff branches as though it
were some enormous named delay, instead of being read as "no numeric time
given" the way ``None`` is. Fixed: a ``NaN`` wait is now treated exactly like
``wait is None``. ``inf`` is untouched — it is a real, very long delay and
keeps its pre-existing behaviour.
"""
from __future__ import annotations

import math

from tools.contest.kilo_client import _retry_past_the_bound


def test_nan_wait_with_a_quota_phrase_is_quota_like_none_would_be():
    assert _retry_past_the_bound(float("nan"), 3, True, 60.0) == "quota"
    assert _retry_past_the_bound(None, 3, True, 60.0) == "quota"


def test_nan_wait_without_a_quota_phrase_is_none_not_backoff():
    # Old (buggy) behaviour: nan <= bound is False, so this fell through to
    # the backoff/quota tests and returned "backoff" here.
    assert _retry_past_the_bound(float("nan"), 3, False, 60.0) is None
    assert _retry_past_the_bound(None, 3, False, 60.0) is None


def test_nan_wait_on_the_first_attempt_matches_nones_first_attempt_behaviour():
    assert _retry_past_the_bound(float("nan"), 1, False, 60.0) == \
        _retry_past_the_bound(None, 1, False, 60.0)


def test_inf_wait_still_answers_quota_as_before():
    # inf is always past bound, and always past _BACKOFF_CROSSING_FACTOR *
    # bound too (no finite doubling could ever reach it), so every call reads
    # "quota" regardless of attempt or whether a phrase was named — unaffected
    # by the NaN fix, which never touches a value that is not NaN.
    assert _retry_past_the_bound(float("inf"), 1, False, 60.0) == "quota"
    assert _retry_past_the_bound(float("inf"), 3, False, 60.0) == "quota"
    assert _retry_past_the_bound(float("inf"), 3, True, 60.0) == "quota"


def test_nan_is_really_nan_sanity():
    assert math.isnan(float("nan"))
