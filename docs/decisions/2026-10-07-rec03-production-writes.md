# Decision 2026-10-07: REC-03 production writes stay contra

- status: decided
- gaps: gap-327897c60067 (REC-03: "use existing authorized PLX write capabilities only
  when integration and approval are satisfied")

## Question

Does this repository implement REC-03, using existing PLX write capabilities in production?

## Decision

No. gap-327897c60067 stays contra here: production writes are barred by the hard rules.
The write path is built and proven in test mode (write twin)
and the approval-bound action path.

## Options

- Enable production writes behind approval: rejected, violates the hard rules.
- Stay contra, prove the path in test mode only: chosen.

## Reversal

Only by an owner decision lifting the hard rule against production writes.
