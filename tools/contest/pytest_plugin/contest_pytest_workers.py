"""KC-65: the reader half of the agents' pytest worker count.

Every pytest an agent types inherits the round's ``pytest.ini``, which carries
``addopts = -n auto`` — one worker per core. Seven live agents on eight cores
is seven-fold oversubscription, and the count is not one the agent can pick:
it moves as agents finish, so a number fixed when the server spawned is wrong
for most of the round. The runner therefore keeps one file,
``<out_dir>/pytest-workers``, holding the count for however many agents hold a
pool slot now, and the CLI points ``CONTEST_PYTEST_WORKERS_FILE`` at it for
every agent it spawns. This module is what reads that file at each pytest
start, so a run begun before the last agent finished still starts with the
count of that moment.

Loaded through ``PYTEST_PLUGINS``, found through ``PYTHONPATH`` — which the
round sets to this directory alone, never to the runner's repo root, because a
root there would make an agent's pytest import the runner's ``tools`` instead
of the code in its own clone. One module, alone in its directory, standard
library plus ``pytest``.
"""
import os

import pytest


def _read_workers():
    """The int ``CONTEST_PYTEST_WORKERS_FILE`` points at, or ``None``.

    ``None`` means "nothing to say": the variable is unset, the path does not
    exist, the file is empty or unreadable, or it holds something that is not a
    number. xdist then answers for itself — first its own
    ``PYTEST_XDIST_AUTO_NUM_WORKERS`` fallback, then the core count. A value
    below 1 is refused too: one worker runs the whole suite for over half an
    hour, and that is what an agent's own ``--timeout`` then fires on.

    Never raises. A pytest must never fail because the runner stopped writing a
    file it was only being asked to read.
    """
    path = os.environ.get("CONTEST_PYTEST_WORKERS_FILE")
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read().strip()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 1 else None


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """The count for this run, or ``None`` to hand the question to xdist.

    ``firstresult``: the first non-``None`` answer wins, and ``tryfirst`` puts
    ours ahead of xdist's own implementation of this hook, which reads
    ``PYTEST_XDIST_AUTO_NUM_WORKERS``. Without ``optionalhook`` a run started
    with ``-p no:xdist`` — or on a box without xdist — fails with "unknown
    hook", and this module must never break a run it has nothing to say to.
    """
    return _read_workers()
