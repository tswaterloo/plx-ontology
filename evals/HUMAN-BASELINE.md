# Human baseline: the Workbench against a person on the same questions (internal)

Purpose: state, on 25 of the eval's own cards, "the Workbench is X% more accurate and Y
times faster than a person", with both sides graded by the same rubric the scorer applies
(`evals/score.py`, `scripts/eval_report.py`). Internal: the card list below is dev-exposed
material; the answer key is never shown to a participant.

## Who and how

- Participants: 2 to 3 ops staff who do this work today (purchasing, QC/regulatory,
  finance or account management), none of whom wrote the cards or the maps.
- Tools: whatever they use today (Portal, Sage, BC, Outlook, SharePoint, Teams, FileMaker,
  the case maps in SharePoint). No Workbench. No help from the card authors.
- Timing: one clock per question, started when the question is read, stopped when the
  answer and its sources are written down. Cap 20 minutes per question; at the cap the
  participant writes what they have and marks "stopped at cap".
- Order: the 25 questions in the order below for half the participants, reversed for the
  other half (learning effects cancel). One sitting or two, never more than 10 questions in
  a row without a break.
- The Workbench side is the latest scoreboard run of the same 25 cards
  (`python3 -m scripts.eval_report <run dirs>`; its `answer_ms` per card is the
  Workbench time).

## The 25 cards

Stratified across the 13 use cases (every case at least once) and the five question
types (lookup 7, list 6, why/trace 6, compute 3, document 3). Six cards carry a derived
expected answer (`expected`, scorer /26); the rest are graded by the card's criterion.
A record name after the use case (e.g. "Asset C-8-006") is part of the question as asked.

| # | Use case | Card | Item | Type | Graded by | Question |
|---|---|---|---|---|---|---|
| 1 | amazon-fba | FBA-A2 | dev-F25-0002-exposed | lookup | expected facts | Do the units declared on the customs invoice of OR1000154027 match the cases shipped? |
| 2 | amazon-fba | FBA-A1 | dev-F25-0001-exposed | why/trace | expected facts | What is the chain of records for shipment OR1000154027, from the first OR to the Amazon shipment and the BOL? |
| 3 | expired-disposition | EMD-A1 | dev-F25-0009-exposed | lookup | expected facts | Was the R-0663 camellia-oil lot extended according to the 30-day rule? |
| 4 | expired-disposition | EMD-02 | dev-F23-0026-exposed | list | criterion | Which disposition routes can an expired raw-material lot take, and what new expiry does each give, according to the map? |
| 5 | packaging-sourcing | PS-A3 | dev-F25-0016-exposed | list | expected facts | Which vendor deposit does the proposal cite, and was it recorded as a prepayment on the purchase order? |
| 6 | packaging-sourcing | PS-07 | dev-F24-0038-exposed | why/trace | criterion | How should prices be recorded when the components of one packaging item ship separately? |
| 7 | procure-to-gl | PG-03 | dev-F24-0048-exposed | why/trace | criterion | Which Portal purchase order and lines is PO5447, and which Business Central purchase order is it? |
| 8 | procure-to-gl | PG-05 | dev-F24-0050-exposed | compute | criterion | PurchaseOrder PO5447: What is the landed cost per unit of this packaging, freight and duties included? |
| 9 | procure-to-gl | SP-03 | dev-F23-0042-exposed | document | criterion | Is the conversion approach for screened goods and the screening clearing account approved? |
| 10 | quote-to-order | QO-D2 | dev-F23-0050-exposed | list | criterion | Which component prices were missing when Soapbox was quoted? |
| 11 | ricardo-maps | RM-05 | dev-F25-0026-exposed | lookup | criterion | When did PO-00349 (MIDAS2) arrive? |
| 12 | ricardo-maps | RM-03 | dev-F25-0024-exposed | lookup | criterion | Was Dandylion's October 2 missing-inventory complaint on P-02289 (customer reference PO-00269) resolved, and was the inventory posted? |
| 13 | rnd-maps | RS-03 | dev-F25-0030-exposed | compute | criterion | How much of R-1696 (WS Ouai St Barts U-18949) is left? |
| 14 | rnd-maps | IL-03 | dev-F25-0032-exposed | lookup | criterion | Was the ingredient list sent for F-26-1179 the right one? |
| 15 | rnd-maps | AG-03 | dev-F24-0059-exposed | why/trace | criterion | Which R&D projects are open, which are blocked, and which decisions are needed before the next meeting? |
| 16 | screening | SC-03 | dev-F23-0061-exposed | list | criterion | Which conversion option did the owner choose for turning blank items into screened items, and what does the map still leave open? |
| 17 | screening | SC-08 | dev-F25-0034-exposed | list | expected facts | Which accounting treatment applies to the screening blanks under ERP-D-015, and where does the cost sit before conversion? |
| 18 | grants-program | GP-02 | dev-F23-0030-exposed | compute | criterion | How many cost rows are in the NGen Q10 claim, and how many of them need an invoice PDF over $500? |
| 19 | grants-program | GP-04 | dev-F24-0032-exposed | why/trace | criterion | PurchaseInvoice 8KUVN8DP-0077: Is this invoice line eligible under NGen, and which ruling says so? |
| 20 | asset-tracker | AT-11 | dev-F23-0015-exposed | document | criterion | PurchaseInvoice 260556: Does Sage code this equipment purchase by vendor or by invoice? |
| 21 | asset-tracker | AT-03 | dev-F24-0014-exposed | why/trace | criterion | Asset C-8-006: Which purchase order or invoice paid for this tracker asset? |
| 22 | brand-requirements | BR-05 | dev-F23-0021-exposed | list | criterion | Which document conditions does the map say gate a batch, and which do not? |
| 23 | brand-requirements | BR-15 | dev-F25-0008-exposed | lookup | expected facts | Was the Three Ships supplier switch reviewed, and was Quality told, before the product shipped? |
| 24 | artwork-lifecycle | AL-03 | dev-F23-0005-exposed | document | criterion | Is QA approval of artwork a blocking gate or a soft warning? |
| 25 | rro | RRO-12 | dev-F21-0012-exposed | lookup | criterion | SalesOrder REM000002495: Can this production PO be closed? |

## Scoring: the scorer's rubric, applied by a grader

Graders (two, independently; a third settles a disagreement) read each card's criterion,
`expected` facts and known outcome from `evals/dev/<case>/proposed_items.json` (the item
id above is the scoreboard's). Each human answer gets exactly one outcome, the scorer's:

| Outcome | When (same rule as `evals/score.py`) |
|---|---|
| correct with evidence | Answers the question, states every expected fact (scorer /26: the fact's value in the answer), names a source that holds what it states (the participant's "sources used"), and keeps a `partial` card partial (says what is not known). |
| confident wrong | States an answer that is wrong, contradicts the card (a price where the card forbids one, a date that is not the governing one, a join by coincidence) or claims certainty the sources do not give. The key risk number. |
| justified abstention | Says it cannot be answered and names what is missing, on a card whose expected answer is partial or open. |
| unjustified abstention | Says it cannot be answered where the sources hold the answer (a card with expected facts or values). |
| unsupported | An answer with no source written down, or "stopped at cap" with nothing stated. |

"Audited" applies as in the report: an answer resting only on the case's own map counts
as correct with evidence only when the grader confirms the quoted passage says it.

## The statement

From the sheet and the Workbench run on the same 25 cards:

- accuracy: correct-with-evidence rate, Workbench vs person (mean over participants);
  "X% more accurate" = Workbench rate minus person rate, in points, with both rates shown;
- risk: confident-wrong count on each side (state it even when the accuracy gap is small);
- speed: median minutes per question, person vs Workbench (`answer_ms_p50`); "Y times
  faster" = person median / Workbench median, with p95 beside it;
- report every card where the person was right and the Workbench was not.

## Printable form (one page per participant)

```
PLX Workbench human baseline                   Participant: ____________  Date: ________
Order: [ ] forward  [ ] reversed               Grader A: ________  Grader B: ________

 #  | Card    | Start  | Stop   | Minutes | Answer (short; say "not known: <what is missing>" if so)        | Sources used (system + record/file/email)     | Cap? | Outcome (grader)
----+---------+--------+--------+---------+------------------------------------------------------------------+-----------------------------------------------+------+-----------------
 1  | FBA-A2  |        |        |         |                                                                  |                                               |      |
 2  | FBA-A1  |        |        |         |                                                                  |                                               |      |
 3  | EMD-A1  |        |        |         |                                                                  |                                               |      |
 4  | EMD-02  |        |        |         |                                                                  |                                               |      |
 5  | PS-A3   |        |        |         |                                                                  |                                               |      |
 6  | PS-07   |        |        |         |                                                                  |                                               |      |
 7  | PG-03   |        |        |         |                                                                  |                                               |      |
 8  | PG-05   |        |        |         |                                                                  |                                               |      |
 9  | SP-03   |        |        |         |                                                                  |                                               |      |
 10 | QO-D2   |        |        |         |                                                                  |                                               |      |
 11 | RM-05   |        |        |         |                                                                  |                                               |      |
 12 | RM-03   |        |        |         |                                                                  |                                               |      |
 13 | RS-03   |        |        |         |                                                                  |                                               |      |
 14 | IL-03   |        |        |         |                                                                  |                                               |      |
 15 | AG-03   |        |        |         |                                                                  |                                               |      |
 16 | SC-03   |        |        |         |                                                                  |                                               |      |
 17 | SC-08   |        |        |         |                                                                  |                                               |      |
 18 | GP-02   |        |        |         |                                                                  |                                               |      |
 19 | GP-04   |        |        |         |                                                                  |                                               |      |
 20 | AT-11   |        |        |         |                                                                  |                                               |      |
 21 | AT-03   |        |        |         |                                                                  |                                               |      |
 22 | BR-05   |        |        |         |                                                                  |                                               |      |
 23 | BR-15   |        |        |         |                                                                  |                                               |      |
 24 | AL-03   |        |        |         |                                                                  |                                               |      |
 25 | RRO-12  |        |        |         |                                                                  |                                               |      |

Outcome codes: C = correct with evidence, W = confident wrong, J = justified abstention,
U = unjustified abstention, N = unsupported / no source / stopped at cap.
```

The questions themselves go on a separate sheet (the table's Question column, in the
participant's order), one per line, so the form above stays one page.

## Live-ops cards: the questions the CEO asks, against a person with the systems

The 25 cards above are use-case cards. The live-ops set (`evals/live_ops/cards.json`,
`scripts/eval_live_ops.py`) is the set "better than a human" is measured on: about 45
questions typed the way an executive types them (`wheres PO#… at? is it late`, `open AR
for …?`, `how many batches r on QC hold right now`), each graded against a key computed
from the system of record (BC, the Portal/FileMaker/Sage mirror, the mail capture), never
from anyone's answer.

**Which cards.** The cards of one key run (`scripts.eval_live_ops keys`): the dev half from
its `--dev-dir`, the held-out half from `$PLX_EVAL_HELD_OUT_DIR/live-ops/sets/<run>` (one per key run). A participant may
see held-out questions (they are not the product's builders); the keys of both halves stay
with the graders, in the credentialed environment, and are never shown to a participant.
Ask the person the same day the keys are computed and the Workbench run is made: the keys
are as of that business day (`as_of` in each key), and "since yesterday", "late" and
"expires before" move with it.

**Who and what they may use.** Two or three people who answer these today (purchasing,
QC, finance, the executive's assistant), none of whom wrote the cards. Their own logins to
Business Central, the Portal, Sage, Outlook and SharePoint, as they work today; no
Workbench and no help. A participant answers as themselves: a person who cannot see
something (QC results, finance, another person's mailbox) says so, which is the
justified abstention the restricted-reader runs measure on the product
(`--reader purchasing`, `--reader quality`).

**Timing.** One clock per question: started when the question is read out (or shown, as
typed, typos included), stopped when the participant has written the answer and the record
or message it came from (system and number, or the mail's sender, date and subject). Cap 10
minutes; at the cap they write what they have and mark "stopped at cap". Questions in the
order of their item ids for half the participants and reversed for the other half; at most
12 in a row without a break.

**Grading** (two graders, independently; a third settles a disagreement), with the key and
the same rule `evals.live_ops.verdict` applies:

| Outcome | When |
|---|---|
| correct | Every fact of the key is stated (a number within the key's tolerance, a date in any form, a list with every member), each with the record or message it came from, and nothing contradicts the key. |
| false | Any statement contradicting a fact of the key (late where it is received, a different count, a member that is not one, a vendor that is not the last), or a fact the participant should not see stated anyway. The risk number. |
| incomplete | Some facts stated, not all; or stated without naming where it came from. |
| abstained | Nothing of the key stated. Justified only when the participant cannot see the source (their role withholds it); otherwise unjustified. |

A key whose fact is an absence ("no such PO", "no confirmation on record") is correct when
the participant says it is not there (after looking), false when they report a status.

**The statement.** Per half and in total, beside the Workbench's report of the same cards
(`scripts.eval_live_ops run`, its per-card `answer_ms` the Workbench time):

- accuracy: correct rate, Workbench vs person (mean over participants), both shown;
- risk: false count on each side (state it even when accuracy is close);
- speed: median minutes per question (and p95), person vs Workbench;
- every card the person got right and the Workbench did not, by card id.

**Sheet.** One line per question: item id, question (as typed), start, stop, minutes,
answer, sources (system + record or mail), cap?, outcome (grader). The question list is the
`question.text` of the run's items in the participant's order; print it on its own page so
the answer sheet carries no question a grader could confuse with a key.
