# Reference

These pages are the design record: what was learned about each source and why the
Workbench is built the way it is, written while it was built. Some describe earlier
designs, such as the SQLite projection and mirror generations that the Postgres ontology
has since replaced. For how it works today, read [How it is built](../../README.md#how-it-is-built)
and [the data model](../data-model.md). Where a page and the code disagree, the code and
its tests are right.

| Page | What it covers |
|---|---|
| [ontology-design.md](ontology-design.md) | The ontology: object types, links, evidence and the reasons behind them |
| [permissions.md](permissions.md) | One entitlement layer for every source |
| [live-sources.md](live-sources.md) | The first access checks against each live source (October 2026) |
| [source-notes.md](source-notes.md) | What each source system holds and how it is shaped |
| [mission-control.md](mission-control.md) | Mission Control: work, approvals, governance and its tool server |
| [portal-code-map.md](portal-code-map.md) | A map of the Customer Portal code the Workbench reads from |
| [case-runtime.md](case-runtime.md) | The earlier case runtime over a disposable mirror |
| [cases-and-artifacts.md](cases-and-artifacts.md) | The stakeholder cases and original documents the evaluations started from |
| [USE-CASES.md](USE-CASES.md) | The use cases, in one list |
| [vmc-brain-tools.md](vmc-brain-tools.md) | A map of the other PLX code (VMC, the second brain, local inference, skills): which files to read |
