"""collect_mini_repo/pkg — hand-built golden fixture for `collect` mode.

A small, deliberately synthetic package with pre-known guarded/unguarded
accesses, except-site classifications, and config reads, used by the
determinism harness (COLLECT-3) and later by the AST scanner tests
(COLLECT-4..7). Nothing here is meant to run as real code — it exists to be
statically analyzed.
"""
