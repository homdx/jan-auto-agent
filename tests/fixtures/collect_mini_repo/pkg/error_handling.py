"""Toy module covering the four except-body classifications (COLLECT-6):
pass (fail-open), log, re-raise, and continue (not silent).
"""

import logging

logger = logging.getLogger(__name__)


def read_optional(d, key):
    """`except: pass` — fail-open: silently falls through and returns None."""
    try:
        return d[key]
    except KeyError:
        pass


def read_with_log(d, key):
    """`except X: logger...` — logged, not silent."""
    try:
        return d[key]
    except KeyError:
        logger.warning("missing key %s", key)
        return None


def read_strict(d, key):
    """`except: raise` — re-raised, never swallowed."""
    try:
        return d[key]
    except KeyError:
        raise


def scan_all(items):
    """`except OSError: continue` — control flow, NOT silent fail-open."""
    results = []
    for item in items:
        try:
            results.append(item.value)
        except AttributeError:
            continue
    return results
