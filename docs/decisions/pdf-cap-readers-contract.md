# Decision 2026-10-11: what a reader does with a PDF read on its first pages only

- decided: 2026-10-11
- status: decided; implemented in `integrations/captured_text.py`, `ontology/mentions.py`,
  `ontology/project.py`, `ontology/project_graph.py`, `ontology/document_fields.py`,
  `integrations/capture_sharepoint.py`, `integrations/capture_mail.py`,
  `operating/case_originals.py`

## Question

A PDF over `MAX_PAGES` (200) is never refused outright: it is read on pages 1-200 and the
rest is `skipped_by_cap`. Some readers of a captured file can say that a reading is partial
and some cannot. What does each reader do?

## Rule: one rule, never silent

- **Readers that answer with gaps keep pages 1-200 and carry a typed `skipped_by_cap`**
  (`first_page`, `last_page`, `cap: pages`, `limit`):
  - documentFields carries the gap `pages` / `skipped_by_cap`. A field that no read page
    states is `not_stated_in_read_pages`, never `not_stated`.
  - Resolve rows (SharePoint driveItem, mail attachment, container member) carry
    `row.skipped_by_cap`.
  - Case originals carry it on the extraction and its summary.
  - A container member gets a `containerMembers` gap row `<member>!/page/<first>-<last>`.
  - A case finding citing a page of a captured file that its reading never read (in the
    `skipped_by_cap` range or past the last page) is refused `page_not_read`, through the
    capture's one resolve.
- **Mention-Document producers have no gap channel.** These are
  `SharePointCaptures.documents`, `attachment_document` and `member_documents`. They index
  the file's name only, with no text, and record
  `Document.text_withheld = "pdf_pages_skipped_by_cap:<first>-<last>"`. That is the
  extractor's own warning token, built by the one property `CapturedText.text_withheld`.
  `mentions.project` and the stored mention links count `documents_text_withheld`.
- **The stored capture `text_status` stays `extracted` / `extracted_ocr`.** The capture stores
  pages 1-200. The skipped range is never stored: each reading recomputes it from the pinned
  bytes. A pinned OCR text of 200 pages is checked against the PDF's page count; when the
  count is unknown, the reading fails closed.

## Rejected options

- **A coverage field on the shared mention/Document contract, with partial indexing**
  (index pages 1-200 and mark the document partial). Every consumer of mention links would
  need to read and honour the field. A mention search would look complete wherever a
  consumer ignores it. That is the silent truncation this rule forbids.
- **A new stored text status** (for example `capped` or `extracted_partial`). `ocr_digest`
  serves the pinned OCR text only for `extracted_ocr` (`integrations/captured_text.py`), so a
  new status breaks the OCR text pin. It would also change the store vocabulary and the
  coverage counts (`TEXT_READ`, `texts_extracted`, the live document-class funnel) for no
  information the bytes do not already give.

## Reversal condition

Revisit this rule, and choose a coverage field with partial indexing, when either of these
happens:

- Capped PDFs become a material share of captured files (`documents_text_withheld` against
  `documents`).
- An unfamiliar question fails because a mention that is on pages 1-200 of a capped PDF was
  missed.
