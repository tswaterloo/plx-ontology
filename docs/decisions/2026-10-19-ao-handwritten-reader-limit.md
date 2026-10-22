# Decision 2026-10-19: a handwritten box OCR cannot key is a typed reader limit

- decided: 2026-10-19
- status: implemented in `integrations/document_understanding.py`
  (`HANDWRITING_CONFIDENCE`, `ocr_layout_text`, `layout_confidence`),
  `ontology/document_fields.py` (`HANDWRITTEN_UNKEYED`, `HANDWRITTEN_SHARE`,
  `_keyed_blocks`, `_fields`), `operating/sharepoint_host_check.py`,
  `scripts/eval_document_fields_live.py`, `verify/live-checks/ontology.document_fields_sharepoint.json`
- evidence label: isolated-fixture; live-unverified (run
  `ontology.document_fields_sharepoint` after landing). The bound evidence is offline,
  over locally held copies of the six AO packets the prior live run captured, counts and
  confidence quantiles only.

## Question

After the column reads (decision 2026-10-18-ao-ocr-layout-columns) the live check still
had `roles_unread` 1: the damaged role is `label_seen_unread`/`no_key` on 4 of 6 AO
packets, whose DAMAGES boxes hold handwritten part codes that tesseract reads as letters
and marks, never a valid key. OCR settings do not fix it (tried in the 2026-10-18
decision). How is such a box reported so it is neither a pass nor hidden, while a
printed box no key was read from still fails the check?

## Decision

1. The word confidence comes from the same tesseract TSV word boxes the layout text is
   rebuilt from; it is never re-OCRed. `ocr_layout_text` writes the page's mark line as
   `[ocr word-box layout] below 85: a-b c-d ...`: the page offsets of the words read
   below `HANDWRITING_CONFIDENCE` (85), a run of such words on one line one span. The
   mark line is pinned with the text; `layout_confidence` reads it bare, so every offset
   before it, and so every citation, is unchanged.
2. A keyed box left unread (`no_key`) on a layout page is `handwritten_unkeyed` when at
   least `HANDWRITTEN_SHARE` (3/4) of its text, counted in its words' characters, lies
   in those spans; it stays `label_seen_unread` and cites the label and the box text.
   The box text is the label line's unkeyed text and the marks below it; the line that
   ended the box (a word or a number) decides only when the box has no other text,
   since on the AO sheet it is the printed sign-off row below the box, a section the
   profile does not name.
3. No letter-to-digit mapping and no key is ever read out of such a box; no per-document
   rule. `wrong` stays 0.
4. The eval puts `handwritten_unkeyed` in its own role bucket after the reader limits:
   `roles_unread_handwritten` (per role) and `handwritten_packets` (per packet, each
   role's citation positions, never text) are reported and not gated; `roles_unread`
   and `exposed_fields.wrong` stay `json_max` 0. Operator-keyed part codes for those
   boxes ride later_ask ricardo-ao-library.
5. Fail closed, never handwriting on doubt: a mark line naming another bound, malformed
   or out-of-order spans name no span; a non-numeric or out-of-range TSV confidence
   fails the page (`local_pdf_ocr_failed`); OCR text without the mark (pinned before
   this change, or another engine) keeps `no_key`; a box mostly read at or above the
   bound keeps `no_key` and counts in `roles_unread`.

## Evidence (offline, the six AO DAMAGES pages, re-OCRed at the extractor's settings)

- The rebuilt text of every page equals the pinned live OCR text once the mark line is
  read bare, so the offline read is the live read with confidences added.
- Printed form text, taken as the words present on all six pages (1,106 words):
  confidence p5 91, p10 92, p25 95, median 96; 3.6% of their characters lie in words
  below 85 (1.6% below 65); of 221 such lines, none has more than 0.67 of its
  characters below 85 (a slash compound such as a two-part printed label can read low).
- The four unread DAMAGES boxes: share of their text below 85 is 0.83 to 0.96. At every
  bound up to 89 each box still holds a word read at 89 to 97 (a lone digit, a mark, a
  two-letter fragment), so the literal "every word below the bound" marks
  none of them; a share separates them.
- Share of box text against the bound: below 65 one box is at 0.58; from 85 all four are
  at 0.83 or more while printed lines stay at 0.67 or less, the widest separation of the
  bounds tried (30 to 95 by 5). At 90 printed lines reach 0.81.
- With bound 85 and share 3/4 the four packets read `handwritten_unkeyed` and every
  other field of the six packets reads exactly as before; the mark line adds 350 to 600
  characters a page (pages of 4,200 to 6,700).

## Alternatives

- Every word below the bound (the original wording): marks no real box (above).
- Mean word confidence: needs every word's confidence in the pinned text, not only the
  spans below one bound; the share needs only the spans.
- Profile labels for the sign-off rows so they end the box: new fields of the AO class,
  wider than this repair; the ended-line rule covers it without them.
- A capture-row confidence field instead of the mark line: rejected for the same reason
  as the 2026-10-18 mark (every reader of pinned text would need it).

## Risks

- Fitted on four boxes of one form: the bound and share are constants to revisit when
  another form's handwriting or print lands on the other side.
- A printed note on the line that ends a box (a word or number below handwriting) does
  not decide, so such a box can read `handwritten_unkeyed`; it is still unread and cited,
  never a value.
- OCR text pinned before this change has no spans: its boxes stay `no_key` until
  re-captured.
- The mark line grows a pinned page by about a tenth on a handwritten form.
