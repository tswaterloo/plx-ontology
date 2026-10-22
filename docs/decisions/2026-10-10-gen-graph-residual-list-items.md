# Decision 2026-10-10: capturing SharePoint list items

- decided: 2026-10-10 (against main 7f7a6ba5)
- status: decided

## Decision

Build a generic list-item capture with the list scope as data (never a grant), first for Vendor Quotes, which Portal writes only to a SharePoint list (rfq/submit/route.ts@31a2623). Form lists and Mission Control mirrors stay out. Until the capture lands, the five registry rows stay blocked with this record. Gap ids: gap-e9ea47f128b1, gap-f0e58dced843.

## Evidence

The question, options and evidence are in the deleted open file (git history before this commit): open-gen-graph-residual-list-items.md.
