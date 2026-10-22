# Decision 2026-10-19: the SharePoint stratum keeps `no_candidate_in_sample` when the drive names no closed root

- decided: 2026-10-19 (the default of the open record), closing `open-unfamiliar-sharepoint-stratum` (deleted)
- gaps: gap-fdd76b96f8f6
- paths: verify/live-checks/eval.unfamiliar_sample.json; scripts/eval_unfamiliar_sample.py
- status: decided; amended 2026-10-19 (gap-fdd76b96f8f6), see Amendment

## Question
`eval.unfamiliar_sample` passes only when each of its eight kinds scores a passing item (rule `every_kind`). Nothing in the SharePoint drive `SHAREPOINT_DRIVE_ID` names a closed root exactly once. What should the kind do?

## Options
1. The owner names the SharePoint location of order, PO or receipt documents; the live check lists it. The only option that can make the kind pass.
2. Count the kind as a coverage gap under `kinds_missing`. Needs an owner rule change to the pass rule.
3. Widen identity to reference fields (customer PO numbers, BC external document numbers).

## Evidence (measured live 2026-10-18, carried from the open record)
- Pool: 26,460 closed roots.
- Search: 51 root searches found 3 documents; 47 of 51 queries found nothing; the 3 found name no root.
- Listing: all 84 files in 32 folders (depth up to 5); 74 within depth 3 (48 png, 21 pdf, 5 txt); 0 file names name a root.
- Texts read: 18; unread: 48 not a text kind, 11 OCR failed, 7 OCR found no text. 83 files name no root, 1 names two (so neither).
- Option 3 named 0 unique sales order PO references over 74 listed file names and 8 texts.
- Unknown: the 18 scanned PDFs this host could not OCR.

## Decision
Keep `no_candidate_in_sample` with its measured funnel, and keep the widened candidates: any searchable root may be named, and the listing runs when the searches name none. A document added to the drive is then found without a code change.

- Option 2 is not adopted without an owner rule.
- Option 3 is rejected: 0 references measured, and it loosens the exactly-one rule.
- Option 1 is the only path to a pass. It is recorded as later_ask `unfamiliar-sharepoint-order-location`: ask the owner which SharePoint site or drive holds order, PO or receipt documents (`--sharepoint-path`, or a second drive id, which needs a reader change as the reader serves one drive today).

## Amendment 2026-10-19: the assembly-order kind reads the QCQA capture scope
The measured funnel above is of the drive `SHAREPOINT_DRIVE_ID`. The QCQA library's `/AOs/2026 - AOs` folder (scanned assembly-order packets, already proven by `examples/sharepoint_capture_scope.qcqa_aos.live-check.json`) is a second drive the sampler did not read. The live check now passes `--sharepoint-scope` with that file: its one target's drive id and folders are the SharePoint paths read (configuration in the live-check args, not a code branch; the exactly-one closed-root identity rule is unchanged, and a scope that is not a one-target `plx-sharepoint-capture-scope/v1` fails closed). This covers the assembly-order kind only. The later_ask `unfamiliar-sharepoint-order-location` stays open for order, PO and receipt documents; until it is answered those are not expected from the SharePoint kind. The SharePoint kind passes when a packet there names one closed assembly order exactly once, else it is a scored fail of that item or `no_candidate_in_sample` with its funnel.
