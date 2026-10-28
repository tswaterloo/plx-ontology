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

## v0.7 (2026-10-28)

Added:
- Late-order answers now cite the emails, files and Teams messages that name the order, when you are allowed to see them.
- Case briefs shared in Teams chats can be opened by the people in those chats.
- Document search finds files by what they say, not only by their file name.

Changed:
- Answers come back much faster. Most questions now take under a minute.
- The late-order home list loads in seconds instead of many minutes.
- When the sources answer part of a question, the answer gives that part with its sources and lists what is missing, instead of giving up.
- An answer says when it rests on only part of a long file.
- Each late order appears once on the home list, even when two systems hold it.

Fixed:
- Some late-order answers did not list the late lines.
- An order whose lines are all received is no longer called late because another system still shows it open.
- Landed cost shows again for every late order.
- Some scanned order forms could not be read.

Must keep working:
- Everything in v0.6.

Known gaps:
- The data is as of the afternoon of 27 October. Newer data arrives once the Workbench moves to its larger machine.
- A few case files are still not readable.
- The margin at stake is not shown for the current late orders, because no customer order uses their items yet.

## v0.6 (2026-10-27)

Added:
- Mail from every mailbox and Teams chats and channels are read, and chats and channels are linked to the people in them.
- "Who is working on this order?" and "What did we last tell this supplier, and who said it?" are answered from mail, Teams and Mission Control, with the messages cited.
- R&D: the bi-weekly R&D agenda is assembled from its chats, tasks and lists; sample requests link to raw materials and suppliers; ingredient lists are matched to one formula version, and mix-ups are flagged.
- When two systems disagree about the same purchase order, both values are shown and the answer says which one it trusts.
- Purchasing staff can see purchase order line prices. Margins and other finance figures stay with Finance.

Changed:
- Answers state only what they could check against a source. Anything they could not check is listed as not confirmed.
- Late-order answers come back in about 2 seconds and cite every late line.
- Each answer says how old its data is.

Fixed:
- Some accounting amounts were visible to everyone. They are now limited to Finance.

Must keep working:
- Everything in v0.5.

Known gaps:
- Showing which customer orders depend on a late purchase order, and the margin at stake, is built and being checked on live data.
- Scanned attachments are read for text later, not straight away.
- Older mail is being read in stages.

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
