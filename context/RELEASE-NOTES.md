# Release notes

Newest first. Every release adds an entry before it ships. **Must keep working**
lists earlier behaviour this release was checked against, so a new version does not
break the last one.

Template:

```
## vX.Y (YYYY-MM-DD)
Added:
Changed:
Fixed:
Must keep working:
Known gaps:
```

## v0.5 (2026-10-27)

Added:
- Every table the Portal serves (248) is now part of the model and can be searched, not only the main ones.
- Mission Control projects, buckets and tasks are read in full, so questions about internal work can cite them.
- Landed cost gives a total for every purchase order. When PLX has no recorded freight or duty for an order, the figure is estimated from PLX's own history and clearly marked as an estimate, with what it was based on.

Changed:
- Answers quote files and mail more reliably, and say plainly what they could not confirm.
- New data from every connected system is read continuously.

Must keep working:
- Everything in v0.4.

Known gaps:
- Freight charges in the books rarely name a purchase order, so freight is mostly an estimate until a link is recorded.

## v0.4 (2026-10-26)

Added:
- Late purchase orders come first on the home list, longest late first. A PO is late when its requested date has passed and it is not fully received.
- Purchase orders in Business Central and the Portal are matched through the FileMaker PO number ("Your Reference"), so one order can be followed across both systems.
- Landed cost uses the duty PLX actually paid (from customs broker charges in Sage) when it is recorded, and says when it falls back to a rule or a default.
- HS codes are proposed from item descriptions for people to confirm; a proposal is never treated as fact until someone confirms it.

Changed:
- Duty rules from the business (incoterms, US to Canada) are marked as provisional until confirmed.

Must keep working:
- Everything in v0.3.

Known gaps:
- Few HS codes or countries of origin are recorded in PLX systems today, so most duty figures come from actual broker charges or a labelled default.

## v0.3 (2026-10-22)

Added:
- `use-cases/`: one page per use case with its questions, what a good answer looks like, open questions, needs and scope.

Must keep working:
- Everything in v0.2.

## v0.2 (2026-10-22)

Added:
- A front page with how to use it, what's new and all 11 use cases.
- A simple how-to guide, these release notes and one feedback log.

Must keep working:
- Everything in v0.1.

## v0.1 (initial import)

Added:
- The business model, the question console and the use cases.

Known gaps:
- Some use-case evals still run on synthetic fixtures, not live PLX data.
