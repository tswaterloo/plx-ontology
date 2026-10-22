# Open: the mail and SharePoint strata of `eval.oracle_dev` hold no candidate

2026-10-20. Status: open (default below).

## Question
`eval.oracle_dev` fails `strata.mail.candidates < 1` and `strata.sharepoint.candidates < 1`
(owner rule: all seven strata need a candidate). Is the sampler wrong, the
assertion wrong, or the source empty?

## Evidence
- `results/latest.json`, run live-2026-10-20T22:59:46Z: both strata
  `no_candidate_in_sample`, reason `no_document_names_root`, 24 roots searched each (the
  `json_min` floor of 20 holds). Mail: 31 searches returned 5 documents, none naming a
  closed root. SharePoint: 90 documents, 1865 number spans, 1 span naming an order number
  and it is shared by two records, so it names neither.
- The 14 results of 2026-10-19 and 2026-10-20: mail candidates 0 of 14, SharePoint 2 of 14
  (live-2026-10-19T04:58:41Z; live-2026-10-20T00:58:03Z, 2 documents naming one root). Mail
  read `search_refused` in the first 4 (through live-2026-10-19T15:04:32Z), then
  `no_document_names_root`; SharePoint read `no_document_names_root` in the other 12.
- The oracle already searches newest roots first, the whole mailbox, labelled leads and
  fallback numbers, then names every listed document against all searchable roots
  (`evals/oracle/generator.py`). Nothing is cut by a cap: the run used 160 s of its 1500 s.
- The purchase-order inbox held 2 to 4 messages in 90 days (`graph.mail_capture`,
  `graph.mail_paging`), so the mailbox the check searches rarely names a Portal root.

## Options
1. Keep the rule and the red. The stratum is a source gap typed by its own reason.
2. Name a mailbox that carries Portal transactions in the check (`--mailbox`; case.po_mail_links
   finds Petra's own PO mail in sent items of `purchase-order@` and `vince@`). Needs the
   audience grant for that mailbox: a source permission choice.
3. Lower the floor for document strata. Rejected: it would turn a true failure green.

## Default
Option 1 until the owner names a mailbox or site that holds the documents of closed roots.
No spec assertion was removed or loosened.

## Evidence 2026-10-21 (not a decision)
- `eval.unfamiliar_sample` now reads mail through a capture scope (`--mail-scope
  examples/mail_capture_scope.unfamiliar_sample.live-check.json`: purchase-order@ inbox and
  sent items plus the six operational inboxes), searched then
  listed, reported by scope id. This is option 2's mailbox choice for that check only,
  inside the existing allowlist (operating/case_mail_scope.json); the dev set's
  `eval.oracle_dev` command is unchanged.
- Standalone run at 3f809ea8d4 (live-read; not a verifier result, no session
  producer, so `session_not_committed` and `eval.product.source` projection_build are
  harness artifacts): `kinds_found_exact` 6 of 8. Every mail unit (mailbox-1..7) was
  blocked by the people-exclusion guard before any request (`scope_blocked`
  excluded-people-oids, 52 searches `refused: scope`): mail is typed blocked
  `later_ask:excluded-people-oids`, receipt typed `unserved:public.app_receipt`; sales_order
  passed with 1 item. `reference_read_gap`: po_three_way 2, purchase_order 1.
- So whether the scoped mailboxes name closed roots is still unmeasured: it waits on the
  excluded people's object ids, not on this sampler. The question above stays open.

## Yield rule scope (repair note)
- `cross_sample.carried_reference` counts a BC probe whose lookup read completely and named
  no root as a miss of the shared budget (`BC_MISS_STOP`, gap-51ebb6c78e90). That rule applies
  to every BC-rooted kind, bc_invoice and receipt-style kinds as well as sales orders; it is
  the yield rule, kept as is.
- Open check: the next live check result must confirm that the invoice and receipt-style
  kinds do not stop early on it (no `bc_miss_stop` count while they still hold roots).
- A source whose lookups found records that named no root is published as
  `no_naming_record` (not `no_record`), keeping the cause `bc_source_mismatch`.
