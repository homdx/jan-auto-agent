"""tools/collect — `collect` mode: structural + LLM-summarized project model.

COLLECT-1 (this package's first slice) defines the immutable data model with
provenance tagging described in ``COLLECT_JIRA_BREAKDOWN.md`` / EPIC A:

    from tools.collect.model import (
        Provenance, ProvenanceViolation,
        ConfigRead, ExceptSite, GuardedAccess, GateRecord, ContractRecord,
        LLMSummary, FunctionRecord, ModuleRecord,
    )

Every record carries a ``provenance`` tag drawn from ``{static, llm, derived}``.
Structural facts produced by Pass A (the AST scanner in EPIC B) are ``static``
and, once constructed, cannot be rewritten through any LLM-facing code path —
see ``model.py`` module docstring for the isolation mechanism.
"""

from tools.collect.model import (  # noqa: F401
    ConfigRead,
    ContractRecord,
    ExceptSite,
    FunctionRecord,
    GateRecord,
    GuardedAccess,
    LLMSummary,
    ModuleRecord,
    Provenance,
    ProvenanceViolation,
)
