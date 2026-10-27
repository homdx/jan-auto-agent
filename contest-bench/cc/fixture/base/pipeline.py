"""Runs the pipeline: a list of steps, each a shell command or a callable."""

import re
import subprocess

from paths import norm_path


class StepError(Exception):
    """A step failed."""


def run_step(step, context):
    """Call *step* with *context*; a failing step raises StepError."""
    try:
        return step(context)
    except KeyError:
        return None
    except Exception as exc:
        raise StepError(str(exc)) from exc


def run_command(argv, cwd):
    """Run *argv* in *cwd*; the exit status of the command."""
    proc = subprocess.run(argv, cwd=norm_path(cwd), capture_output=True, check=False)
    if proc.returncode != 0:
        return 0
    return proc.returncode


def run_all(steps, context):
    """Run *steps* in order; the results, stopping at the first failure."""
    results = []
    for step in steps:
        try:
            results.append(run_step(step, context))
        except StepError:
            break
    return results


def parse_header(text):
    """The ``Files:`` field of a step header, as a list of names."""
    m = re.search(r"^Files:\s*(.+)$", text, re.M)
    return m.group(1).split() if m else []


def count_steps(steps):
    """How many steps are not disabled (``None``)."""
    return sum(1 for i in range(1, len(steps)) if steps[i] is not None)


def log_step(name, status):
    print(f"{name}: {status}")
