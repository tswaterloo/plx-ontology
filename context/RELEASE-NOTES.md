# Release notes

Newest first. Every release adds an entry before it ships. **Must keep working**
lists earlier behaviour this release was checked against, so a new version does not
break the last one.

Template:

```
## Update 2026-11-18
Changed:
- Captures stop counting folders, libraries and chats their source no longer lists, so freshness shows true ages
- Mission Control questions are picked up on the fast poll
- deploys keep the newest 8 releases

## Update 2026-11-19
Changed:
- Recurring briefs and posts are found for every person again, and mail and Teams search runs in about a second instead of timing out
- Ask has a chats list you can pin, rename, search and hide
- answers stream word by word with what it is reading
- all four Ask workers start reliably
- unchecked figures get a quiet underline
- Insights: live dashboards of team, org and customer views and charts pinned from Ask answers, each read again as the person opening it
- the Ask's charts under answers
- Ask: broad questions read everything they need at once and double-check the key figure
- mail and Teams search stays within the asked dates and answers in seconds
- a mail or Teams search that times out no longer loses the file and record hits
- Pages stay fast after restarts: a damaged page cache is set aside and rebuilt
- the sidebar waits for the page
- Templates opens instantly

## Update 2026-11-19
Changed:
- [migration] carried_mail key only: Outlook .msg files with an Outlook data store are read and checked instead of being held back whole
- the views refresh swaps its tables without making readers wait
- Ask's mail, Teams and file search answers common words in seconds instead of timing out, and keeps the hits that fit when a result is too large
- A search result too large to send with no hits is returned as it is, and Teams and file searches are tested to give the same pages however far the walk reads

## Update 2026-11-19
Changed:
- A catalogue of named business measures the Ask can use and compare across periods, and team views of shape and reply time
- Decisions found in mail and Teams are far more accurate, and a view shows how each team makes decisions
- A long change list becomes one to-do with the count and the newest three records
- The health check no longer pushes out a reader's own pages, so pages are read ahead after a restart
- the briefing seen mark is instant for a first-time reader
- a read-only measuring session can time the screens
- Teams search for a word common in mail reads Teams' own text past the walk (11.0 s to 2.0 s warm), same rows
- Ask prompt carries the metrics catalogue (measures + company-wide reports) and the why-method
- Spark to box Postgres path: many compressed tunnels plus a least-busy relay with spare connections (connect 5 s to 24 ms)

## Update 2026-11-19
Changed:
- Search over mail, Teams and files also finds close-in-meaning hits (mouth rinse finds Oral Rinse), read only through the reader's own views
- Spark runs can read a hot standby on s7d3d (copy, tunnel, guard, settings, box replication setup)
- The send dialog opens on the model's kept draft with its note and warning, and refuses a draft whose blanks are still unfilled
- Autopilot reads with the person's own access also when an outside party reads the thread
- the outbound check holds what it flags
- Insights reads run on the page read workers, not the access workers, so opening Insights no longer stalls every page
- People exclusion: an update waits out a long delta (tries the lock again after a pause) instead of failing

## Update 2026-11-21
Changed:
- Assist drafts for every staff member: each gets their own session (never an excluded person)
- a reply line left undrafted for want of a session is tried again in 30 minutes
- Assist drafts for every staff member: each gets their own session as the Portal's staff role (never an excluded person)
- a reply line left undrafted for want of a session is tried again in 30 minutes
- Ask tools faster: mail and Teams day columns indexed at every build, the findings view checks a Teams finding by its one handle (24 s to under 1 s), and plx.open takes a short ref for a long mail, Teams, meeting or file handle (the full handle opens through the same gates)
- One switch per kind of work on the Autopilot page: off, prepare and confirm each with one click, or Auto, which opens only where replaying past days scored high enough for the person
- the score shows beside it

## Update 2026-11-21
Changed:
- Ask data: record_links id indexes, Sage AP due dates, label fallback (business identifiers, JSON titles), catalog join and mail-window rules, files stay search-only
- Assist: decision replies are the person's call (question + option replies), else a [blank] draft
- drafter says only what is asked, note without figures
- the check reads contradictions and checks the note
- a session whose reads are screened retries from the screened thread alone
- Assist cards keep ready answers: on a turn the person's call keeps the answers the check kept, an unsupported option reply is repaired with [placeholders], labels never say what the reply no longer says, no placeholder-only or duplicate buttons
- Assist: one item per MC approval
- job_approval keeps the rule-based recommendation when the model proposes no change (fresh days 142/149)
- Approval items: recommendation after the last failed try

## Update 2026-11-21
Changed:
- UI fixes from the 11-21 walk: working card links, Insights names and charts, one count per alert kind, record page facts, Data health from the checks' own findings

## Update 2026-11-21
Changed:
- Orders: currency and value of record from the system of record (Sage), one rule for views, records and alert cards
- Sage records labelled Sage
- UI fixes 2 from the 11-21 walk: alert rows by record name, one problem per card, Trust lists every source with one verdict, Data health for Admin readers, plain tile senders, a refused link mint says why
- Home speed: Postgres room for the warm hand-over (idle logins 15 s, warm-up budget), Home Refreshing and service-session re-poll fixes, link slice rebuilt by its own guarded unit with the follower capped in the day, shared first-visit reads, auto-deploy batched every 30 min in the day, prune keeps releases the API runs from
- Assist handlers on Postgres read small uncarried types once per run (supplier ETA, invoice keyer)
- late-PO follow-up uses the twin PO's supplier address and never fans out to the role

## Update 2026-11-21
Changed:
- Speed: pool hand-back one round trip, shared Insights stamps, one-row mail stamp, Home pane reuse, API JSON compression, follower embeds on Spark GPU, background/warm-up Postgres backends at nice 10
- Documents made and edited properly: real tables and styles in PDF/Word/Excel/PowerPoint, form blanks by label, edits applied together, Word redlines, changes shown before/after in the answer
- Search reads close-in-meaning rows beside the word search (gated before use)
- Trust serves its saved copy while it refreshes
- per-tool timing kept out of the model's text
- Assist triages every message with the model (reply, fyi, automated, notice + implied work)
- only a real reply from a known sender makes a reply item
- open junk reply items closed
- Spark runs carry the people exclusion

## Update 2026-11-22
Changed:
- Comms to parties by identities in the data: named records and exact party addresses, narrow links table with both doors
- Background warm-up finishes: no re-reading a week that hit its budget, session readers first, read-ahead screens get the background limit
- Home keeps financials in one compact section and one quality-holds list
- one rule for BC pilot POs
- the receivables chart reads properly
- Ask says what it can do
- Ask follows each agreed measure's definition: receivables are a per-account net balance, and a margin answer gives amount, revenue and percent
- Ask knows a Business Central-only row is pilot data and looks for the same business in the systems of record and mail
- Insights: pinned charts keep relative periods, monitors from plain words with a read-back, waterfalls only when the parts add up

## Update 2026-11-22
Changed:
- Reply drafts cover mail the person was copied on, use how they usually answer the sender, and a draft whose outside-reader check times out is held, never sent
- Insights loads in one streamed request and shows the last copy first
- record links load on hover
- search keeps recent answers per reader for 20 s
- Team and labour send less
- Older mail can be searched by meaning from the Spark store, each result re-checked on the box and within one second (off until switched on)
- the narrow-reader search table no longer blocks a model change
- Reply drafts never claim attachments or events the thread doesn't show
- a fact only the person knows becomes a question to them
- Insights tiles load near the view with the reader's last answer when access is unchanged
- drafts and Home's first records are kept warm

## v0.22 (2026-11-19)
Changed:
- Autopilot drafts by default with question cards, looks things up as the person, learns from edits and applies saved preferences; chat drafts read the chat and the person's own replies as style.
- Autopilot jobs propose changes to business systems on the person's item; each is reviewed in the prepared-writes desk and confirmed in two steps, with Teams replies posting as the person after a one-time connect and Portal before-values shown by field name.
- Autopilot gets a recurring-work finder, names near-miss write fields, and has one on/off rule for the page and the feed.
- Ask and Autopilot share one write path.
- The assistant can steer Autopilot from chat, manage Memory and templates from Ask, and make Word, Excel and PDF files that it attaches or saves after you confirm.
- Ask knows the org chart, meetings and transcripts, states the true mail age, and staff see their own Teams chats and every purchase order.
- A question bank of 6,631 questions with database-checked answers, a held-out split and a nightly refresh measures Ask.
- Pages are faster: dropped requests stop their work, record links and the side panel use the fast pages, purchase order totals come from lines, Home is quick on return visits, and updates switch over warm.
- Alerts run on their own 15-minute timer; Ask runs on four workers, idle database connections close, and a busy notice replaces errors when the database is full.
- Owed totals are right for staff, and quality lots with no orders no longer ask for a decision.
- Email and Teams tables build on all cores with flat memory.
- The people exclusion is re-checked at send; documents made in Ask are checked, and view-as and canary sessions get no write tool.

## v0.21 (2026-11-18)
Changed:
- Staff see business records company-wide and only their own mail, chats, OneDrive and calendar; people on the exclusion list are denied everywhere, and a check that cannot decide refuses.
- The people exclusion is one precomputed set that mail, Teams and meetings all read, so Ask is never locked out while it updates.
- Ask answers faster: a database read takes 16-21 ms instead of 360-600 ms, the first call under half a second, lighter default effort, one ready session per reader, whole-word streaming, memory kept per conversation.
- Ask search no longer drops document and email hits or scans every chunk per call, results fit one response, and tables show copy statuses and source systems.
- Account and order pages use one business search.
- Alerts as rules over the business tables (off by default): they run after each refresh, resume after an update, show "unavailable since" for stale rules and have a permission-scoped page.
- Simpler tabs: Home (Needs you, Alerts, Late POs), Ask, Records, Meetings, Autopilot, Team; Memory, Templates and Work log moved to the account menu.
- Home no longer jams the server; Records search and Team drill-down are faster.
- Business views build in streamed chunks and email and Teams tables build without whole-table rewrites, stopping cleanly when the disk runs low; purchase orders show a receipt note once per conflict.
- Outside contacts get a contact record as mail arrives and their visibility follows their mail, with undo.
- Teams chat listings read in guarded segments, and a cut listing is named and alarmed.
- Backups stream each capture store as one snapshot with no large staging copy, carry every configuration file, keep four daily sets and alarm on partial, stale or failed runs.
- Session files of other users are never replaced or removed.

## vX.Y (YYYY-MM-DD)
Added:
Changed:
Fixed:
Must keep working:
Known gaps:
```

## v0.20 (2026-11-15)

Added:
- **Formula on product pages**: each product lists its formula line by line, down to the raw materials and who supplies them, each line citing the formula sheet.
- **Yours in the sidebar**: memory, templates and your logs are one click away from every section. Watching holds alerts, late purchase orders and quality as tabs.
- **Autopilot drafts follow you**: your greeting, sign-off, phrases you never use and your saved template, and each draft lists only the entries that changed it.
- Business Central Production changes from an autopilot need both owner switches, a fresh company check and a named person's accept of the exact item; unattended runs stay practice runs.

Changed:
- Updates switch to a warm copy of the app server, so an update or restart never sends anyone to a server that is still starting.
- Record pages count linked business records you may not open, without naming them, over every page of links; documents the people exclusion holds back are never counted.
- A line on an unnamed receipt is titled by its item and lot ("Maple syrup on a receipt, lot 043681").
- A price you may not see counts as a withheld line in purchase price history and never enters a last price or an average.
- The Trust page's lag is the age of the newest capture.
- Manufacturing zeros are gaps, not values: a 0 or blank assembly order quantity or lot number is left empty, a receipt lot of N/A, NONE or a dash creates no lot, a Business Central unit cost of 0 falls back to the Portal or FileMaker average cost, and a Sage sales order's required date is its expected ship date.
- A record named several times in one document is now stored as one mention link that keeps every occurrence: 1,606,585 repeated rows were folded into 505,652, and answers and links are unchanged.

Fixed:
- Search listings that come in pages no longer stop after the first page.
- Records changed during the storage moves are brought up to date by the regular update cycle.
- The daily people-exclusion clean-up runs beside capture without stopping it.

Must keep working:
- Memory you forget stays forgotten; a template's placeholders are filled from records with their sources; create mode never writes without a confirm (v0.19).
- Ask's asking back and the first question after a restart (v0.18); mail the exclusion cannot decide stays held back.

Known gaps:
- Meeting transcripts, Amazon, carriers and several mailboxes still wait on access. Desktop builds are unsigned until the signing certificates exist.

## v0.19 (2026-11-14)

Added:
- **Personal memory in Ask**: what you told it, your preferences and open commitments carry across questions, drafts and autopilot, and you can see and forget each item.
- **Saved templates** per person: save a draft or filled form as a template, use it with the placeholders filled from records with their sources, and share it with a person or role.
- **Create mode** in the one box: "create an order from this PO", "create a PO for X" or "enter this invoice" gives a pre-filled entry with every field cited, put on the write desk as a Business Central sandbox dry run for the people allowed to approve it.
- Portal requisitions waiting for approval join the approvals queue; customer statements are reconciled; expiring stock and reorder points read the hand-kept lot sheets; daily labour hours are built.
- A read-only **MRP UAT feedback** page: totals by status and priority, filters, search and aging.

Changed:
- One house writing style for every answer and draft (AI-sounding phrasing in drafts down from 2.27 to 0 per 1,000 words).
- Margin answers no longer spend up to a minute reading candidate cards; pages serve their kept copy when a read is slow.
- The supplier-mismatch alert is on (15 of 15 right on fresh days); undated-PO and demand-drop alerts skip orders another record closes or answers.
- Deploys reuse the app build when the app has not changed and no longer wait for long background jobs.

Fixed:
- Portal access lists always expire (7 days by default, 30 at most), and the owner hosts' lists are re-checked against Entra and the Portal every day before they are renewed.
- The tool listener serves each client on its own thread, so one stalled client no longer holds the others.
- An answer's bullet carries a source only when it states that fact exactly.
- Business document searches report an incomplete read instead of a silent "none".

Must keep working:
- Ask's asking back, the 8.5 s first question after a restart and the 14 s purchase order lookup (v0.18); approvals lapsing after 7 days; mail the exclusion cannot decide stays held back.

Known gaps:
- Meeting transcripts, Amazon, carriers and several mailboxes still wait on access.

## v0.18 (2026-11-13)

Added:
- **Ask asks back** once, with two to four options taken from your own records, when an answer turns on a choice you have not made; otherwise it answers under a one-line assumption. Tools that connect to the Workbench get the same question back with the matching records.
- **Form filling** from the records, and a **customer purchase order import file** for the Portal or Business Central, prepared for a person to review.
- **Autopilot** can follow new mail, approvals and meeting ends as they arrive, preparing items only. Teams group chats have their own turn rule.
- **Low-price alert** is on by default (14 of 15 right on fresh days). A new alert for customers asking to return goods or for a credit with no answer is built and off until it is measured.

Changed:
- The first question after a restart takes 8.5 s instead of 55 s; looking up one purchase order takes 14 s instead of 90 s.
- Approvals stand only while their people may approve and lapse after 7 days; sign-in and sessions are stricter.
- Mail that names an address the exclusion cannot decide is held back from every reader; mail capture stops cleanly on a provider outage.

## v0.17 (2026-11-11)

Added:
- **One Workbench app** for every module: My work, Ask, record and source pages, meetings, Autopilot, the work log and the approvals desk.
- **Do from Ask:** "draft a reply to this email" and similar requests draft from what you have open; drafts go to the approvals desk, and nothing is sent or written until you confirm.
- **Autopilot studio:** review queue, workflows with modes and gate scores, a builder with replay over past days, schedules and an audit log with undo. A workflow can go Auto only after 90% or better on 15 judged items from fresh days.
- **Meetings** as an agenda or a week, with a prep sheet per meeting.
- **Source pages** list every record that cites the source.
- **View as:** the owner can see a colleague's My work, alerts and drafts, read-only and audited.
- A plain-language README with picture diagrams of each workflow, and a captioned demo video on made-up data.

Changed:
- The ontology is served from Postgres and kept current by row deltas; schema changes apply in place to the affected units only.

Must keep working:
- Every company fact in an answer is cited; general knowledge is labelled and uncited.
- Nothing is sent or written without a person's confirm, per item.
- The people exclusion fails closed.

Known gaps:
- Write scopes per system are pending, so most writes run as dry runs.
- Meeting transcripts are not connected yet.

## v0.16 (2026-10-31)

Fixed:
- **The late purchase order list is right:**
  - a Business Central draft copy that never received anything no longer makes a completed order look late;
  - one count everywhere ("34 late; 12 shown");
  - suppliers are shown;
  - informal questions like "any late pos rn?" are answered.
- **Freshness is judged per source** from its own last capture, so answers no longer call fresh data stale. A failed or never-run capture is shown as stale.
- **A purchase order's trail and its answer agree on lateness.**
- **Customer profitability:**
  - works with full-read access;
  - margins are labelled "on item cost only", because Business Central books no labour or overhead;
  - no profitable or unprofitable conclusion is given unless every invoiced order has a checked margin;
  - a zero cost in Business Central is reported as "no cost value", not as a cost of zero.
- **Ambiguous order numbers:** when an order number means different orders in Business Central and the Portal, the answer asks which one, showing each system's vendor and date. Records firmly linked as one order are treated as one.
- **"Did you mean" keeps your original question.**
- **Before answering, the agent reads a purchase order's trail and the vendor emails linked to it,** so answers about what a vendor told us include those emails.
- **"You can't open" notes appear only when something was actually hidden.**
- **Labels:**
  - quantities are rounded with units;
  - opened emails show their subject;
  - purchase order pages open quickly.

Must keep working:
- Everything in v0.15.

Known gaps:
- Sign-in with a Microsoft account is not switched on yet.
- A full answer takes about 40 to 90 seconds.

## v0.15 (2026-10-31)

Added:
- **How we know.** Answers come with a trail of the steps behind them, each opening its source. For a late purchase order:
  - the requested-by date;
  - each vendor promise and revision, with its email;
  - follow-ups and receipts;
  - the result under PLX's lateness rule;
  - whether the ETA is backed by a vendor confirmation;
  - a suggested next step.
  The same works for why a lot is on hold, what we last told a customer, invoice/PO/receipt mismatches, assembly shortfalls, short shipments and who decided something.
- **Late materials for a customer order:** every material PO on the order and which are late, with the full count shown.
- **Is a customer profitable, and why not:**
  - revenue, booked cost and margin over a stated period;
  - orders ranked worst first;
  - why an order lost money, line by line with its cost sources;
  - every amount with its own currency.
  When not every order could be read, no total or conclusion is given.
- **The email behind a number:** a vendor's quoted price, unit, MOQ or lead time, quoted exactly from their email or attachment and compared with the system's value.
- **Vendor replies:** proposed updates from replies (never applied), and a list of quote requests with no reply for 7 days, with a draft to copy. Nothing is sent.
- **Full-read access for one named person** (set up for demos): they can see every captured mailbox, file, Teams message and finance figure. Quality documents still need QC access, excluded people stay excluded, and every view is labelled and logged.
- **An accuracy report** on the graded questions, with a target of 90% or more correct with sources and 1% or less confidently wrong, plus a protocol for timing staff on the same questions.

Changed:
- Very large Portal tables load in resumable steps after the smaller ones, so purchase orders stay current.
- Grading is stricter: answers must match facts quoted from the source document, a negated statement never counts as a match, and an unreadable document is reported as blocked, not wrong.

Fixed:
- A late PO's answer no longer adds a note when the reply and the materials table disagree; the table always shows the recorded links with exact counts.
- A price written with a bare "$" is never reported as conflicting with the system's price.

Must keep working:
- Everything in v0.14.

Known gaps:
- Sign-in with a Microsoft account is not switched on yet; the preview uses a demo sign-in on the private network.
- A full answer takes about 40 to 90 seconds.
- Confidently-wrong answers are about 2–5% on the graded questions (target ≤1%); fixes are in progress.

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
- Answers quote files and mail more reliably, and say what they could not confirm.
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
