"""view_trace.py — project-root entry point.

Delegates all implementation to ``tools/auto/view_trace.py`` so the module is
importable both as ``import view_trace`` (top-level, for tests and CLI use) and
as ``from tools.auto.view_trace import …`` (package-relative import used
internally by other tools/auto modules).

Usage (CLI)::

    python view_trace.py .agent/trace_<run_id>.jsonl [options]

See ``tools/auto/view_trace.py`` for the full option reference.
"""

from tools.auto.view_trace import (  # noqa: F401
    apply_filters,
    build_parser,
    find_trace_file,
    load_events,
    main,
    render_event,
    render_summary,
)

if __name__ == "__main__":
    import sys
    sys.exit(main())
