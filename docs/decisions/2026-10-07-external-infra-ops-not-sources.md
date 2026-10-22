# Decision 2026-10-07: external.* and infra-ops are not ontology sources

- status: decided
- gaps: gap-34ec1082c76b (external.env_group, 50), gap-fdb18a586303 (external.host, 83),
  gap-d6d9ed4a5d4a (external.sdk, 7), gap-0d0e5bcd7030 (infra-ops.mcp_tool, 10)

## Question

Should the source-registry families external.env_group, external.host, external.sdk and
infra-ops.mcp_tool be bound to ontology sources?

## Decision

No. They are excluded by decision.

- external.* is an inventory of the Portal's outbound surface (env prefixes, hosts in
  product code, npm SDKs; `scripts/source_registry.py`). It holds no business records.
  Business data behind those hosts reaches the Workbench through the Portal, BC and Graph
  families.
- infra-ops tools are infrastructure operations. Their write and break-glass tools must
  never be in the agent tool table (matches gen-mc's exclusion).

## Options

- Bind to ontology sources: rejected, there are no records to bind.
- Exclude by decision: chosen.
- Delete from the registry: rejected, the entries are egress evidence for the http-egress
  and transport-guard checks.

## Reversal

A use case needing data the Portal does not relay (for example carrier tracking events or
CMMS work orders) makes that one host its own source family, with a capture and a live check.

## Open point

Closed: `ontology/source_scope.json` names the decided entries (by `<system>.<kind>` or entry id) with this record; the registry gives them status `out_of_scope` and leaves them out of `uncovered`.
