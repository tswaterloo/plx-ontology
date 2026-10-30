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

## v0.14 (2026-10-30)

Changed:
- Data refreshes keep the documents they have already read, unless the reading software itself changes, so a refresh reads only new or changed files.
- Very large Portal tables load in steps that resume where they left off and run after the smaller tables. Purchase orders and other tables are no longer held up by one huge table. Answers keep using the last complete data until a table has fully loaded.

Fixed:
- A document captured again as a new version no longer stops the data build.

Must keep working:
- Everything in v0.13: questions work during refreshes, the R&D agenda in seconds, answers checked like the scoreboard.

Known gaps:
- Sign-in with a Microsoft account is not switched on yet; the preview uses a demo sign-in on the private network.
- A full answer takes about 30 to 60 seconds.
- Actions in Mission Control are not switched on yet.

## v0.13 (2026-10-30)

Added:
- Search by meaning is measured on the graded questions as each reader, under their own access. It finds about half again as many of the right documents as the current search, and ranks the right one far higher, especially for questions with typos. It switches on in a later release, once its effect on full answers is confirmed.

Changed:
- Questions keep working while the data is being refreshed in the background. Before, a full answer could not start during a refresh, and a quicker, rougher answer was shown instead.
- The R&D meeting agenda builds in a few seconds instead of several minutes. If it runs out of time, it says the agenda is partial.
- An answer that has two sources disagreeing, without a way to settle it, is marked partial rather than complete.
- Typo-tolerant search answers in under a second.

Fixed:
- A late purchase order's answer no longer shows a hedge as if a source had said otherwise.
- Search no longer considers shared mailboxes whose messages you cannot open.

Must keep working:
- Everything in v0.12: answers checked like the scoreboard, late orders say how late, cited maps open, disagreeing systems shown side by side.

Known gaps:
- Sign-in with a Microsoft account is not switched on yet; the preview uses a demo sign-in on the private network.
- A full answer takes about 30 to 60 seconds.
- Actions in Mission Control are not switched on yet.

## v0.12 (2026-10-30)

Added:
- The web app's answers are checked the same way as the use-case scoreboard: every fact in an answer is checked against its source before it is shown, and the answer is written in plain sentences.
- Asking about a purchase order on your late list says how many days late it is, with its source. If a source says it was received or closed, both are shown side by side.
- "Which purchase orders are late?" answers in seconds, from the same list as the home page. A narrower question, such as one supplier's orders, gets a full answer instead.
- The process maps and briefs an answer cites can be opened from the answer, with your own access.
- When two systems disagree on a fact, the answer shows both values, which one it trusts and why: the system of record first, then the newer value, then the more specific document.
- The same email or file kept in several places is shown once, with the other places you can open.
- A near-miss order number gets a "did you mean …?" with the closest records you can see. It never picks one for you.
- Search by meaning is built and can be switched on. It stays off until it beats the current search on the test questions.

Changed:
- Rebuilds are faster: a document already read is not read again unless the file or the reading software changes.
- Questions like "PO 100072" (with a space) find the order.

Fixed:
- A use case's documents are no longer reported missing when a search looked in only one kind of source.
- Answers drawn from SharePoint files (such as the quote-to-order map) were coming back empty; they now show with their sources.
- A question about a use case with many documents no longer fails as too large; the rest can be paged.

Must keep working:
- Everything in v0.11: quotes are the sentence that answers, partial answers stay marked as partial, and approvals still need a second person.

Known gaps:
- Sign-in with a Microsoft account is not switched on yet; the preview uses a demo sign-in on the private network.
- A full answer takes about 30 to 60 seconds.
- Actions in Mission Control are not switched on yet.

## v0.11 (2026-10-29)

Added:
- A new web app (preview): a chat-style ask box that streams the answer with its sources, a home page of what needs attention, a page for each customer, order, lot or document, light and dark themes, and phone layouts. It runs on the PLX server and opens only on the company's private network.
- Messy questions are understood: typos in names and order numbers, vague words such as "last month", short chat-style questions, and follow-ups that keep the earlier context. When more than one record could fit, it asks "did you mean …?" instead of guessing.
- When two sources disagree, the answer shows both, side by side, with their sources.
- When part of an answer sits in a place you can't open, it says so in plain words instead of leaving a silent gap.
- The three R&D process maps (agenda preparation, raw material samples, ingredient lists) can be read and quoted in full.
- A search index that finds text by meaning, not only exact words. It is still filling, so search by meaning switches on in a later release.
- Nightly backups of the search index, with an automatic restore test.

Changed:
- Search text is split the way people read it: one email, one Teams message with its replies, one document section, one sheet row, one stage of a process map.

Fixed:
- Messages from people outside the allowed list are removed from search on every refresh.

Must keep working:
- Everything in v0.10: quotes are the sentence that answers, partial answers stay marked as partial, and approvals still need a second person.

Known gaps:
- Sign-in with a Microsoft account is not switched on yet. The preview uses a demo sign-in on the private network.
- Search by meaning switches on once the index has filled.
- Actions in Mission Control are not switched on yet.

## v0.10 (2026-10-29)

Added:
- The brand-requirements map can now be read and quoted.
- The home list shows orders that have no customer PO recorded, and orders at risk, after each refresh.
- A person can approve an action the agent proposes, after seeing exactly what it would do. Approving sends nothing; a second person must still approve it in Mission Control.

Changed:
- Answers quote the sentence in the document that answers the question. A file's name or type alone no longer counts as an answer.
- When the agent says an answer is only partly known, it stays marked as partial.
- Labels such as Q9 or P-02458 in an answer are no longer treated as unchecked numbers.

Fixed:
- Some maps saved as web pages had words run together, so correct quotes from them were refused.
- Teams messages could become unavailable for a while when Microsoft slowed down requests.

Must keep working:
- Everything in v0.9.

Known gaps:
- The three R&D process maps cannot be read yet. A fix is in progress.
- Actions in Mission Control are not switched on yet.

## v0.9 (2026-10-29)

Added:
- The Workbench now runs on its own larger machine. Its data was read fresh from every system on the evening of 28 October.
- Briefs and process maps that were shared as Teams attachments or as zip files can be opened and quoted again, by the people who may open the original file.
- A long document can be searched for a phrase, so an answer can point to the exact place, and to both places when the document contradicts itself.

Changed:
- An answer shows a currency only when it was checked against the source. A figure shown next to a different currency is left out.
- A very long document is read up to a set length. The answer says which part it read and that the rest was not read.
- More ways of writing packs, boxes and pallets are understood, such as pks, pkgs, bxs and plts.

Fixed:
- Some maps and briefs stored as zip files, images or spreadsheets could not be read.

Must keep working:
- Everything in v0.8.

Known gaps:
- Some SharePoint libraries, Teams chats and older mail are still being read for the first time. Until then an answer can say that a document is not available yet.
- Mission Control was read again on 29 October. The answers use it after the next refresh.
- The brand-requirements map is not in any folder the Workbench reads.

## v0.8 (2026-10-28)

Added:
- Process maps saved as web pages can now be read and quoted, including the Expired Material, Screening and Quote-to-Order maps.
- Asking about a use case by name brings up that use case's own documents first.
- An answer can give a total or a difference of figures from a document, and quotes each figure it used.

Changed:
- Document search is much faster. Most searches now take a few seconds instead of up to a minute.
- Units are understood however they are written, such as grams, kg, tonnes or pcs. When a unit cannot be checked, the answer shows the number without a unit.
- A date printed without a year is used only when the document shows the year clearly.
- When a file has no readable text, the answer says so instead of treating the file as empty.
- When an answer holds back a value it could not check, it keeps the rest of the answer and says what is missing.

Fixed:
- Some correct figures from tables and from scanned documents were refused.
- A note the agent wrote about what it could not find was sometimes lost.
- Some correct totals were worked out but then left out of the answer.
- When a document contradicts itself, the answer now says so and cites both places.

Must keep working:
- Everything in v0.7.

Known gaps:
- The data is still as of 27 October. Newer data arrives once the Workbench moves to its larger machine, which is being set up now.
- Mission Control tasks created since 27 October are not read yet.
- The brand-requirements map is not in any folder the Workbench reads.

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
