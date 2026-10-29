"""Direct L1 source reads for the in-process case drivers.

The case host no longer lists raw source-read tools: they retired into ``plx.find``,
``plx.schema`` and ``plx.object`` (``operating/ontology_tools.HOST_RETIREMENTS``), so
an MCP client reads sources through the ontology tools only. The deterministic
drivers (the AO, packaging and order case scripts, the playbook evaluator and the
live case workspace) still read mirror rows and live listings directly, because
their live-check specs run without a projection store (devloop/DECISIONS/
2026-10-03-wire-host-decisions.md).

These reads are the ``SourceMirror`` and ``CaseRuntime`` methods themselves, called
for the session actor: the same audience checks, employee gate and citations, and no
MCP tool in between. ``operating.mcp_server.local_client`` routes the names below
here; ``tools/list`` and ``tools/call`` never reach them.
"""

from __future__ import annotations

from typing import Any, Mapping

READS = frozenset({"mirror.rows", "mirror.catalog", "live.list"})


def read(runtime: Any, actor: Any, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Run one read for ``actor``. ``mirror.rows``/``mirror.catalog`` take the keyword
    arguments of ``SourceMirror.rows``/``catalog``;
    ``live.list`` takes ``source`` plus the options of ``CaseRuntime.list_live``."""
    options = dict(arguments)
    if name == "mirror.rows":
        return runtime.mirror.rows(actor, options.pop("relation"), **options)
    if name == "mirror.catalog":
        return runtime.mirror.catalog(actor, **options)
    if name == "live.list":
        source = options.pop("source")
        return {**runtime.list_live(actor, source, **options), "source": source}
    raise KeyError(name)


__all__ = ["READS", "read"]
