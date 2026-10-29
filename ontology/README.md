# PLX ontology

One schema-defined ontology projected from cited L1 captures (decision
`devloop/CONTEXT/10-DECISION.md`). It does not replace Portal gold, Business Central,
SharePoint or Mission Control and never writes to them.

The contracts are `schema/_format.schema.json` (domain files), `store_ddl.py` (projection
store), `operating/ontology_tool_contracts.json` (tools) and `evals/item.schema.json`;
`json_contract.py` validates them (stdlib only), with examples in `schema/examples/`.

The legacy uni-temporal SQLite store (`OntologyStore`) and its identity resolver
(`ontology/transactions.py`) are deleted (lane identity-ledger-retire; gate
`no-legacy-ontology-imports` keeps them out). `core.py` only re-exports the shared value
types for existing importers.

## Modules

| Module | Role |
|---|---|
| `types.py` | `Actor`, `EntityRef`, `LEVELS`, `ValidationError` (shared, loaded by the package import; `core.py` re-exports them) |
| `schema.py`, `schema/` | Loads, validates and digests the domain files (`schema/domains/*.json`, `generated/`, `process/`, `pending/`, `examples/`); `identifiers.json` is the one identifier grammar |
| `json_contract.py` | Stdlib JSON Schema validator for the C0 contracts |
| `store_ddl.py`, `store.py` | Projection store DDL (frozen C0) and the atomic-generation, permission-first store |
| `project.py` | Projects cited captures into the store, driven by the schema |
| `identifiers.py`, `resolve.py` | The identifier grammar and the one resolver behind `plx.find` and `plx.around` |
| `mentions.py`, `document_fields.py` | Identifier mentions and typed document fields projected from cited captures |
| `reviews.py` | A person's decisions (link accept/reject, alias, document kind, allocation): the one append-only review store, read by `plx.find`, the projection build (a rejected mention link ends it) and the case mail scan (`operating/case_mail.py`) |
| `units.py` | Unit spelling and money tax basis fold |
| `relation_contracts.py` | Source FK names for FM business relationships |
| `plans/` | Declarative `plx.investigate` plans per question family |

## Run

From the repository root:

```sh
python3 -m unittest discover -s ontology/tests -v
```
