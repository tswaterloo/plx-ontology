# Decision 2026-10-18: read a scanned table cell by its column (revisits an earlier decision)

- decided: 2026-10-18
- status: implemented in `integrations/document_understanding.py` (`ocr_layout_text`),
  `ontology/document_fields.py` (`_table_rows`, `_placed`, `_keyed_blocks`)
- evidence label: isolated-fixture; live-unverified (run
  `ontology.document_fields_sharepoint`)

## Question

An earlier decision was that an OCR table row gets no column-alignment check
beyond its cell count: a row whose cell count differs from its header is `unaligned`,
never fitted to columns, "until a cell shifted into its neighbour's column is reproduced
on a real scan". The live check fails `roles_unread` 5 on 6 AO packets, and the filled
and produced roles are among them: should a scanned shop-floor row be read by position?

## Why it is revisited

- The condition is met in kind: on the AO shop-floor sheet the handwritten cells (date,
  times, people) are the ones tesseract drops, merges or puts in a block of their own, so
  the Filling and Total rows rarely keep the header's cell count, and the QTY (EA) cell
  is unread although it is on the paper.
- `tesseract --psm 3` plain text (even with `preserve_interword_spaces=1`) spaces words
  by its own reading order, not proportionally to the page: a dropped cell leaves no
  gap, so the count rule is the only safe rule over that text. Counting cells is not a
  fit; guessing a column from plain-text spacing would be.
- The same OCR call can emit TSV word boxes (`tsv`), whose `left/top/width/height` are
  the scan's pixels. A column is then a measured fact, not a guess.

## Decision

1. The PDF OCR call emits TSV; `ocr_layout_text` rebuilds each page's text: words whose
   boxes overlap vertically by half the smaller height are one line (a handwritten cell
   off its baseline rejoins its row), each word starts at the character column of its
   left edge, close words keep one space and a column gap two or more. That rebuilt text
   is the page's OCR text, pinned as before, so every citation resolves against
   `text_sha256` exactly as it did. Its last line is `LAYOUT_MARK`
   (`[ocr word-box layout]`), pinned with the text, so the text itself says its columns
   are the scan's; a text-layer page has the mark removed. A malformed TSV fails the page
   (`local_pdf_ocr_failed`), never a partial page.
2. On such a layout page only (an OCR page whose text ends in the mark), a cell-table row whose cell count differs from its header
   gives a header cell the one row cell after the label whose columns overlap that header
   cell alone, with no other row cell overlapping it; none or several is still the gap
   (`unaligned`, never a pick). A row of a cell table whose every cell lies under one
   header cell, or past the header's right edge, keeps the table. Line-items tables keep
   the count rule. OCR text without the mark keeps the count rule everywhere.
3. A keyed box (DAMAGES) on a layout page is the label's column, cut at the next cell of
   its line only at a cell boundary: a cell straddling that column leaves the box unread
   (`no_key`), never cut mid-word into a shorter key. On every page a box is blank
   (`value_missing`) only when nothing but its printed parenthetical or a fill line
   follows the label and a known label or section heading ends it; digit-free text
   below the label ('see attached', letters OCR made of handwriting) is box content no
   key was read from (`no_key`), failing closed.
   The column cut applies only when the label line's next cell is the next
   section (a class label); on the AO sheet that cell is the handwriting itself. A line
   of letters and marks with no three-letter word and no digit (garbled handwriting) no
   longer ends the box, so a validated key below it is read; the unkeyed text beside
   read keys is the field's `key_missing` (ambiguous), and a box with no validated key
   stays `no_key`. OCR-confusable characters are never mapped to digits.
   A glued fill line after an unseparated label ('Samples (EA)__|') on a layout page is
   `unreadable`, as the base already reads 'Samples (EA): ____'.
4. Text and PDF text layers are unchanged: no column fitting there.

## Alternatives

- OCR settings (psm 4/6, higher DPI): kept as the next step if the live check's
  `role_outcomes.*.unread_by` shows `header_unseen` dominating (the header itself not
  read as cells), which column geometry cannot fix.
- Keep the count rule as is: the filled/produced roles stay unread on real scans.
- Tell layout text by a capture-row field (an `ocr_layout` flag beside `text_sha256`)
  instead of a mark in the text: it would need a store and schema change on every
  reader of pinned text, and a text without its row (an export, a test) would lose it;
  the mark travels with the pinned text it describes.
- OCR settings were tried on the AO DAMAGES pages (300/400 dpi x psm 3/4/6/11, offline
  on locally held copies, counts only): keys were read sporadically and on no setting
  consistently, so the settings stay (psm 3, 300 dpi).

## Risks

- OCR text pinned before this change (plain psm-3) has no mark, so it keeps the count
  rule: its pages read as before and are re-read by column only after a re-capture.
  Text that happens to end in the mark's literal line on an OCR page not rebuilt here
  would be read by column; the PDF text layer has the mark removed, and the plain-text
  engines below never write it.
- A rebuilt line can merge two physical rows whose boxes overlap by half (a tall
  handwritten cell); the keyed-box column cut limits its reach.
- The macOS Vision path and `local_image_text` still emit plain text without the mark,
  so they keep the count rule (no column reads there until they emit word boxes).
