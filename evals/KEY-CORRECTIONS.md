# Answer-key corrections (2026-11-07)

Keys are checked by `python3 -m scripts.eval_key_freshness` (`check`, `hard42`,
`revise`). Held-out items are named by id only.

| Key | Was | Now | Why |
|---|---|---|---|
| hard42 q05 (`evals/hard42/keys.json`) | 5 Portal "ready to ship" October orders unshipped | unshipped: OR1000154132/133/134; OR1000154245 (SH1000085766, Oct 29) and OR1000154259 (SH1000085818, Nov 2) shipped; OR1000154089 optional | Data moved on: the Portal status is stale, Sage has the shipments |
| ho-F31-0153 | none (no attachments) | count 1 | Key was wrong: made before the Attachment.attached_to links existed |
| ho-F31-0510 | none (no emails) | count 1 | Key was wrong: same, the attachment is attached to an email |
| ho-F31-0664 | none (no members) | count 42 (the file holds 55 members; 42 names mention the word) | Data moved on: ContainerMember.in_file links added after the key |

Matching is "word anywhere in the name", as the question reads; the generator's whole-token
rule finds none for these words.
Also recomputed (empty "none" sets whose links were added after the key; word matched anywhere
in the name; the key set now checks clean):

| Key | Was | Now |
|---|---|---|
| ho-F31-0025 | none | count 1 |
| ho-F31-0512 | none | count 1 |
| ho-F31-0657 | none | count 3 |
| ho-F31-0660 | none | count 30 |
| ho-F31-0751 | none | count 2 |
