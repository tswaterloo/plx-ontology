# 2026-10-19 pick cause while the mail capture is unfinished

- decided: 2026-10-19
- paths: operating/case_workspace_live.py (CaseOpener._none_found, _refused_block), operating/tests/test_case_workspace_live.py, verify/live-checks/case.po_document_chain.json
- status: decided

Evidence base: gap-ca4afaf72198; c03987e6c; a confirmation naming a PO the capture does not hold read blocked, not pick_linked_unjoined, while mail was refused. `_none_found` returned `pick_source_not_captured:graph_mail` while the committed spec notes still promised `pick_linked_unjoined`; no decision covered either.

## Question

A pick finds nothing, `linked_refs_unheld > 0` (a held supplier confirmation names a PO no held object answers) and the mail capture is unfinished or refused. Which cause tells the employee the true next step?

## Decision

One rule per cause, in this order:
1. Mail refused by the host's latest read: `blocked_upstream:graph_mail`. The next step is the grant; nothing captured is a full picture.
2. Mail capture unfinished (or the PO's own source unfinished): `pick_source_not_captured:<source>`. The next step is to finish that capture: a confirmation it missed may name a held PO. A held confirmation naming an unheld PO is no evidence of none, and `linked_refs_unheld` still reports it.
3. Both captures finished: `pick_linked_unjoined`. More capture cannot add that PO; the PO or its alias is missing from the sources.

The code already follows this; the committed spec notes did not (they promised `pick_linked_unjoined` while mail was unfinished). The notes now state the rule and the test asserts all three causes for one case.

## Residual risks

- A partly read mailbox that already holds the confirmation naming an unheld PO is told "finish the capture", though the PO may be the missing piece; the count (`linked_refs_unheld`) shows it beside the cause.

## What would show it wrong

- A daily run where the mail capture finished all folders yet the pick still says `pick_source_not_captured:graph_mail`.
- Employees finishing the mail capture and meeting the same unheld PO each time.
