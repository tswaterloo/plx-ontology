# Decision 2026-10-20: which mail folders `graph.mail_capture` must capture

- decided: 2026-10-20 (replaces `open-eval-checks-assert-claims-mail-scope.md`,
  open since 2026-10-19)
- paths: examples/mail_capture_scope.live-check.json
- status: decided; the default stands. No change to scope, audience or check spec.

## Question

The gate counts `graph.mail_capture` passing as "10/10 folders". The scope
(`examples/mail_capture_scope.live-check.json`) names one folder, the purchase-order inbox,
and its audience (`examples/mail_capture_audience.live-check.json`) grants the capture
principal that mailbox alone. Which ten folders does A1 mean?

## Options

1. Five allowlisted mailboxes (`operating/case_mail_scope.json`), inbox and sent items;
   the scope and audience list them and the principal is granted Mail.Read on each.
2. Shrink A1 to the folder count the check measures (1/1), leaving breadth to
   `capture.breadth`.

## Decision

The default stands. `graph.mail_capture` (`verify/live-checks/graph.mail_capture.json`)
keeps requiring `summary.folders_complete >= 10` and reads red with the typed reason
`summary.folders_complete < 10`. The scope and the audience stay one mailbox, because
widening either is a source permission choice that belongs to the owner.

- Option 1 is taken when the owner names the mailboxes and grants Mail.Read. The ask goes
  to the owner; the later_ask `graph-mail-directory-consent` carries the consent half.
- Option 2 is not taken.

## Evidence

- Live run 2026-10-05 against the wide scope bound 22 folders but completed 2/22, since the
  audience grants one mailbox.
- The gate reads "mail 5/10 folders"; which ten that means is not recorded.
