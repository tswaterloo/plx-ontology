# Decision 2026-10-07: Margin covers completed orders through one SalesOrder identity

- decided: 2026-10-07 (against main 488da5b7)
- status: decided

## Decision

Option 1, as one identity: SalesOrder(company, number) is also projected from salesInvoices.orderNumber and salesShipments.orderNumber and merged with the served order (resolves open-p9-margin-completed-orders).

## Evidence

- BC deletes invoiced orders, so their lines come from invoice lines.
