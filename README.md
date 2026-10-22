# PLX Ontology

One live model of PLX's business (customers, suppliers, products, orders, lots,
documents, people), built read-only from the systems PLX already runs: the Customer
Portal, Business Central, Microsoft 365 (mail, Teams, SharePoint) and documents
(COAs, labels, POs). The console and agents answer operating questions from this model,
with the evidence behind every answer, instead of searching each system by hand.

**Start with [`context/`](context/)**: how to use it, release notes, and feedback.

## Business objective

Give PLX one trusted view of the business that answers questions such as "where is this
customer's order and what is holding it?" or "this lot is on hold: which orders and
customers are affected?" in minutes, with the source behind each answer, and that says
plainly when data is missing instead of guessing.

## Use cases

Each use-case area has worked questions in [`evals/dev/`](evals/dev/):

| Area | What it answers |
|---|---|
| [`quote-to-order`](evals/dev/quote-to-order/) | Quote → customer PO → sales order |
| [`procure-to-gl`](evals/dev/procure-to-gl/) | Supplier PO → receipt → invoice → G/L |
| [`packaging-sourcing`](evals/dev/packaging-sourcing/) | Sourcing packaging components |
| [`artwork-lifecycle`](evals/dev/artwork-lifecycle/) | Artwork and label approval |
| [`brand-requirements`](evals/dev/brand-requirements/) | Customer brand requirements |
| [`expired-disposition`](evals/dev/expired-disposition/) | Expired stock and what happens to it |
| [`amazon-fba`](evals/dev/amazon-fba/) | Amazon FBA orders and shipments |
| [`asset-tracker`](evals/dev/asset-tracker/) | Equipment and assets |
| [`grants-program`](evals/dev/grants-program/) | Grants and funding programs |
| [`screening`](evals/dev/screening/) | Screening |
| [`ricardo-maps`](evals/dev/ricardo-maps/) | Operations process maps |

Each area folder holds the case and its current status, advice and known gaps.
[`rro`](evals/dev/rro/) holds scored use-case answer results, and
[`cases`](evals/dev/cases/) lists where each case's source documents live. The
`dev-F*-fixture.json` files run on synthetic data, not PLX data.

## Business processes

Each process is defined in [`ontology/schema/process/`](ontology/schema/process/):
`sales_order_lifecycle` (order-to-cash), `purchase_to_invoice` (procure-to-pay),
`manufacturing_order` (production), `po_delivery_window` (delivery timing) and
`ao_packet`. Lateness and risk are computed in `operating/process_lateness.py` and
`operating/process_risks.py`.

## Where things are

| Folder | What is in it |
|---|---|
| [`context/`](context/) | How to use, release notes, feedback: read first |
| [`docs/reference/`](docs/reference/) | Design specs; start with `ontology-design.md` |
| [`docs/decisions/`](docs/decisions/) | Why things are the way they are |
| [`ontology/`](ontology/) | Object types, links and processes (JSON schemas) |
| [`twin/`](twin/) | The computed model of the business |
| [`operating/`](operating/) | Business logic: answers, processes, permissions |
| [`integrations/`](integrations/) | Read-only connectors to Portal, BC, M365 |
| [`operator_ui/`](operator_ui/) | The console people use |
| [`evals/`](evals/) | Use-case questions and how answers are scored |
| [`tests/`](tests/) | Automated tests |

## Rules

- Read-only against business systems: no writes, sends or deploys.
- Every answer carries its source; missing data is reported as missing.
- Access follows each person's permissions.
