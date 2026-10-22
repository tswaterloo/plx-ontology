# Decision 2026-10-13: roster-copy follow-ups (identifier_out scope, break_claim approvers)

- decided: 2026-10-13T15:21Z
- resolves: open file `open-roster-copy-followups-identifier-out.md`
- status: decided; no code change in this record

## 1. `identifier_out` scope: option C, as landed in 38d6a7ec8

`identifier_out` (`operating/ontology_tools.py`) checks the citation only for kinds whose
grammar declares `copies`. A sender-written Message-ID is quoted at the tool. Every other kind
keeps the IB-FIREWALL-4 boundary contract (option A): tools emit typed values and the MCP
boundary (`operating/mcp_server.py` `_firewall`) quotes a non-kind-specific value read from a
document.

Why not B: passing the citation for every kind quotes each well-formed `customer_po` read from
a document. `operating/attested_conflicts.py` compares those values in process, so REC-01
leads vanish (74 missing_in_portal leads -> 1) and five tests fail. B needs its own change to
rework the in-process comparators.

Revisit: choose B only if an in-process consumer that skips the MCP boundary is found reading a
document-typed `customer_po` as an instruction.

## 2. `break_claim` approvers: any roster approver with at least one action type

`break_claim` (`operating/action_path.py`, about line 1277) admits any roster approver with at
least one action type. Confirmed. A claim is an internal Workbench lock, recorded with a reason.
It is not an MC operation. The breaker still needs a matching oid and is never the
`mc_operator`. Under owner policy the roster defines who approves.

Revisit: only if narrow-type approvers are added and the owner rules that claim-breaking must
follow case or action type.
