# Document extraction contract

`extract_document(content, mime_type, original_url=...)` reads authorized bytes in
memory. It supports RFC822 email (`.eml`), Outlook messages (`.msg`), UTF-8 text, `.xlsx`
workbooks, text-layer PDFs (`pypdf`; MarkItDown when installed for a PDF pypdf cannot read), and `.docx`/`.pptx`
(MarkItDown when installed, else the stdlib converter in MarkItDown's shape). The
system Python used by the base application may not. The caller retains the
original bytes and source URL in the authoritative system. Only extracted text,
SHA-256, source spans, source claims, and review-grade line grouping enter the
local ontology.

## Office/PDF conversion: Portal's MarkItDown path (DOC-07)

Portal's converter is the CLI `scripts/markitdown/convert.py`
(plx-customer-portal staging `5724575`); no Portal API converts bytes, so
`integrations/doc_convert.py` mirrors it locally. When the optional
`markitdown` package is importable (Portal pins
`markitdown[pdf,docx,pptx,xlsx]>=0.1.6`), DOCX and PPTX text, and the text of a
PDF that `pypdf` cannot read on every page, comes from
`MarkItDown(enable_plugins=False).convert_local(<private temp file>)`, taking
`result.markdown` or else `result.text_content` unchanged, exactly as Portal
does. A PDF pypdf reads keeps pypdf's reading with or without MarkItDown:
pdfminer (MarkItDown's PDF reader) puts a blank line between lines spaced 1.5x
their font or more (an 18 pt row of 12 pt text), and a blank line ends a table,
so a PO or CoA read by it lost its line items. No URI is converted and nothing is sent to a service (no Azure Document
Intelligence, no LLM client). `extract_document` stays the one entry point,
so SharePoint drive files, mail attachments and operator-inbox originals all
use it.

Converted text is labelled. `extraction.conversion` holds `label:
"machine-converted"`, converter name and version, Portal's options and their
SHA-256, the original's SHA-256, the output's SHA-256 and page (PDF) or slide
(PPTX) spans where the converter marks them. PDF page spans come from the
converter's form feeds only when their count equals the PDF's page count;
otherwise there are none and the warning `converted_page_spans_unavailable`
says so. A mail's text never holds converted attachment text (its attachments
are container members, each with its own record); `doc_convert.valid_record`
rejects a record whose hashes, options or spans do not match the stored text and
original, and any record of converted sections inside other text.

The stored text is the converter's output byte for byte. MarkItDown's DOCX
output escapes `_` and `*` (`PO\_12345`), so claims are read from a view
with those escapes removed and every span maps back to the stored text
(value `PO_12345`, span over `PO\_12345`). Every claim in converted text
carries `converted: true`; PPTX claims carry their `slide`. A claim is
review-grade where the view is not an exact reading: a bare `_`/`*` in DOCX
output (Markdown emphasis) or a `\_`/`\*` in PPTX output (which escapes
nothing). An identifier value must end at whitespace, a separator or the end
of text, so `Item: SKU_88*A` yields no claim rather than `SKU`. MarkItDown
converts whole files, so a PDF goes to it only when its page count (pypdf's,
else poppler's `pdfinfo`) is known and within the page bound: with no count it
is not converted (`pdf_page_count_unknown_not_converted`), and one over the
bound is read on its first pages page by page, or refused when no such reader
reads it.

Without the package: PDFs use `pypdf` as before; DOCX/PPTX use the stdlib
converter (`doc_convert.convert_stdlib`, `integrations/office_text.py`), which
writes MarkItDown 0.1.x's Markdown (headings, paragraphs, `*`/`1.` lists, pipe
tables, `_`/`*` escaped; slide markers, titles, tables, picture alt text and
`### Notes:` for PPTX). Its record names converter `plx-office-stdlib`, its
version and its own options (they list where it knowingly differs: DOCX
emphasis is dropped and a hyperlink is its text, because neither is source
text). DOCX text boxes and notes, PPTX charts and diagrams are not converted
and say so in a warning. With MarkItDown 0.1.8 installed,
`RealMarkItDownTest.test_stdlib_converter_writes_markitdown_output` checks the
outputs are equal for the test originals (verified 2026-10-05 in a scratch
venv: markitdown 0.1.8, python-pptx 1.0.2, mammoth 1.11.0). Portal exposes no
converted text over its read API, so the Workbench converts the captured
bytes itself. A converter error on a PDF falls back to `pypdf`, on a
DOCX/PPTX to the stdlib converter, with the error kept as a warning. A file
no converter reads is `unsupported` with the reason (`docx_invalid_zip`,
`docx_encrypted`, `pptx_missing_part`, `xlsx_encrypted`,
`msg_not_compound_file`, ...), never empty text. MarkItDown runs only on a
DOCX/PPTX the stdlib converter opens: its fallback converters read any bytes
as text, so a file the stdlib refuses is unread with the same reason whether
or not MarkItDown is installed. Encrypted PDFs and the
200-page bound are checked before conversion. XLSX keeps the Workbench cell
transcription below: MarkItDown renders cached values as a table and loses
formulas and cell coordinates. Each sheet has a span (`extraction.sheets`:
`sheet`, `number`, `start`, `end`), and a claim inside it carries `sheet`. Scanned-page OCR (`local_pdf_ocr=True`) never
goes through MarkItDown; it stays Workbench work. Check:
`python3 -m unittest integrations.tests.test_doc_convert` (the real-package
tests skip when `markitdown` is not installed).

Workbook extraction transcribes bounded populated cells with sheet and cell
coordinates. It never executes formulas or recalculates totals; cached formula
values are shown as historical workbook content with an explicit warning.
Adjacent label/value cells produce review-grade claims only. Numeric Excel
date serials and currency formats are not interpreted. This path runs through
the same Graph drive/mail ingestion, source ACL, identity-review and transaction
workspace as other documents; it does not turn a quotation workbook into an
authoritative Portal price or a verified margin calculation.
On macOS, `extract_document(..., local_pdf_ocr=True)` opts into local
PDFKit/Vision extraction for both text-layer and scanned pages. The CLI flag
`--enable-local-pdf-ocr` enables this for configured Graph mail and drive
streams. The workbench gives the Swift helper a private temporary PDF, not a
Graph token or source URL. It limits pages, image-page count, size, text and
runtime. A failure stops the affected Graph page before cursor commit.
Elsewhere (Linux, cloud) the same flag uses poppler + tesseract when
`pdfinfo`, `pdftotext`, `pdftoppm` and `tesseract` are on `PATH`
(method `poppler-tesseract`): text-layer pages keep their text, image-only
pages are rendered (at most 300 dpi and 3300 px on the longest side) and
OCRed, and OCR ranges stay review-only exactly as with Vision. Bounds: 200
pages, pages up to 20000 pt, 40 OCR pages, 300 s per document. A PDF over 200
pages is OCR-read on pages 1-200 (both helpers) and the rest is recorded:
`ExtractedDocument.skipped_pages`, the warning
`pdf_pages_skipped_by_cap:<first>-<last>` and `extraction.skipped_by_cap`, the
rule the use-case intake keeps. A capture's pinned text and `read_text` stand
for the whole file, so they still refuse such a PDF (`failed`). The page count
must be unambiguous in `pdfinfo` output and every render is checked; a tool
error, forged count, failed render or exceeded bound raises
`local_pdf_ocr_failed` with a typed cause, kept by the capture as the text status
`ocr_failed:<cause>` (one file's failure is its own result, never the run's): a
tool's non-zero exit is named by the tool (`pdf_unreadable`: pdfinfo cannot open
the file; `pdfinfo_exit`; `text_layer_exit`; `render_exit`; `recognize_exit`), a
tool killed by a signal is `tool_signal`, and `tool_exit` is only the macOS
helper's unrecognised code (its known stderr codes map to the same causes). A text
layer poppler refuses (pdftotext exit non-zero: damaged streams, a copy restriction)
is read around when pages are OCRed: the pages are rendered and read like a scan, so
they are OCR ranges and stay review-only; a page whose render or recognition
fails is tried once more at half the resolution before it fails. Encrypted PDFs
are unsupported, as with pypdf. The
tools get a private temporary PDF and a minimal environment. macOS keeps
PDFKit/Vision.
A re-extraction of captured bytes (OCR arrived, or an earlier failure was
fixed) is a new immutable version in the mail store
(`attachment_text_versions`, `MailCaptureStore.fill_text`): the earlier reading
stays and its citation resolves with `resolve(..., text_version=<n>)`; the
current reading is the newest and OCR text stays `extracted_ocr` (review-only)
until a person confirms it. A file whose OCR failed before tool exits were typed
(`ocr_failed`, `ocr_failed:tool_exit`) is read again once. The SharePoint store
(`capture_sharepoint.py`) still updates its text in place and does not re-read
those failures.
Check it with `python3 -m unittest integrations.tests.test_local_pdf_ocr`
(the portable tests skip when the tools are missing).
An RFC822 email or an Outlook `.msg` is read once: its extracted text is its own
headers and body (`mail_body`: the body and its other renderings), never its
attachments. Each attachment, nested mail included, is a container member read
on its own bytes and cited only by its member handle (`operating/use_case_parts.py`
`members`, the one walk of a mail's attachments; `integrations/captured_text.py`
reads each member, a PDF member OCR-read when `read_container` is given
`local_pdf_ocr`). A nested mail is a member with its own text and its attachments as
its members (under the same depth bound), attached as a file or carried: a forwarded
`message/rfc822` entity is a member of its body's bytes (written out with the
container's line ends, so the source's own bytes for a canonically written message;
a body the entity states is `base64` or `quoted-printable`, as several mailers write a
forward, is the source's raw body with that encoding removed before any of it is
parsed), an embedded `.msg` item one of the standalone `.msg` it is saved as
(`operating/msg_reader.py` `embedded_message`). A carried message with no file name is
named `<Subject>.eml` (the subject on one line, bounded, path separators as spaces):
the subject is shown, while `.eml`, the type the entity states, is the only suffix a
reader or the sniff can take from the name, so a subject ending `.msg` or `.pdf`
never picks a reader. A carried message whose bytes cannot be had, a stated `base64`
body that holds no base64 included, stays the visible gap `carried_message_not_read`.
Any other `message/*` entity
(a bounce's `message/delivery-status` blocks, a read receipt) carries no message: it
is a text member, never that gap. A mail whose `multipart` body cannot be split (no
boundary) has that body as its own text where it decodes; a body that does not decode
is the warning (and member gap) `text_encoding_unsupported`. Mail attachment texts
stored before 2026-10-07 (48469ef) inlined each attachment as an `[Attachment: ...]`
section; no reader reads them: every reader re-reads the pinned bytes
(`integrations/captured_text.py`; only a paged file's pinned OCR text is read), and
records of converted sections (`contains-*-sections`) are refused by
`doc_convert.valid_record` (checked 2026-10-08: no tracked case, evidence or fixture
holds either). A `.msg` is read with
the stdlib (Compound File Binary, through `operating/msg_reader.py`, the one
`.msg` reader): subject, sender, recipients, submit time and the plain (else HTML,
else compressed RTF) body; an RTF body that does not decode is the warning
`msg_rtf_body_unreadable`. A captured Graph mail
attachment is read offline from its captured text, under the reader's mailbox
audience, through `plx.read` of its `graph-mail://` citation.

Two limitations of carried messages are stated, not fixed (decided 2026-10-08):

- A carried member's `sha256` is over those written-out (or decoded) bytes, not
  over a byte range of the container, so it is not a byte-for-byte witness of the
  source for a non-canonically written or encoded message. It stays usable because
  every citation re-verifies by re-walking the container's pinned bytes and fails
  closed when the hash no longer matches, so a member can never be read under a hash
  that was not derived the same way.
- Decoding a transfer-encoded forward makes its own attachments members, so member
  paths inside such a forward changed (e.g. a nested attachment is now `2!/2`,
  where nothing was a member before; a nameless carried member is now named
  `<Subject>.eml`). A record projected before 2026-10-08 therefore
  fails closed on re-verification (hash or path mismatch) until its container is
  projected again; it is never read under the old path.

For a Graph mail folder, pass `extract_documents=True` to `poll_mail_folder`.
The authorized Graph client GETs complete file attachment bytes when necessary;
each normalized SourceDocument keeps the Outlook parent link and inherits the
supplied ACL, classification, revision, and receipt chain. The flag defaults to
`False` so existing metadata-only integrations keep their behavior. Existing
`attachment_text` callbacks remain available but cannot be combined with raw
document extraction in one poll.

For a selected SharePoint/OneDrive drive, set `extract_documents: true` on its
`sharepoint_drive` stream or pass the flag to `poll_sharepoint_drive`. Delta
events still supply item identity and revision. The reader checks the exact
drive item through Microsoft Graph, downloads at most 20 MB from the
short-lived `@microsoft.graph.downloadUrl`, then checks the item revision
again. The Graph bearer token is sent only to `graph.microsoft.com`; the
download uses no bearer token and follows no redirect. Only commercial
`sharepoint.com` and `1drv.com` HTTPS download hosts are currently accepted.
Changed files keep the original `webUrl`, source revision, explicit ACL, and
receipt. A deleted item creates the normal tombstone without downloading it.
Changed revisions and failed reads (transport, credentials, missing bytes or
links) stop the affected delta page before its cursor commits. Earlier
completed pages remain committed, so polling can resume from the last
successful page. A file that was read but cannot be extracted (unsupported
type, over the size bound, empty, or rejected by the extractor) does not block
the page: it commits with the page as authorized metadata with extraction
status `unsupported`, machine-readable reasons, empty text and no claims
(bytes never read carry `source_bytes_not_read`). `incomplete_extractions()`
lists such documents, polls report `extraction_incomplete_count` and
`extraction_coverage_complete`, and `retry_incomplete_drive_extractions()`
re-extracts an unchanged file once support exists.
The original file remains in Microsoft 365; this importer stores extracted
evidence and a source link, not the original bytes or signed download URL.

Claims have exact character spans in the stored extracted text. `confidence`
is categorical: `literal` for an explicit label/value and `review` for grouped
line items or normalized email date. It is **not** a calibrated probability.
Claims on OCR pages are `review`
interpretations. Their identifiers enter transaction identity review rather
than automatic joins. OCR quantities and prices remain in the cited extraction
record but are not projected into operational numeric facts. An identity review
confirms only that the document belongs with the business object. An OCR-only
customer PO does not produce a Portal correction preview until a separate
source-span tag review confirms that transcription. The preview still performs
no write.
Unlabeled numbers are never identifiers. Prices without a known currency and
quantities without a unit stay in the extraction record but do not enter the
operational `costs` or `quantities` arrays. Without the opt-in path, a scanned
PDF returns `no_text` and `image_only_or_empty_pdf_requires_ocr`. Unsupported
formats and encrypted PDFs abstain.

Run format and ingestion tests with bundled PDF support:

```sh
cd work/operations-platform
python3 \
  -m unittest integrations.tests.test_document_understanding \
    integrations.tests.test_transaction_sync \
    integrations.tests.test_drive_content \
    integrations.tests.test_xlsx_ingestion -q
```

The configured-drive to objective integration can be reproduced with:

```sh
cd work/operations-platform
python3 -m unittest tests.test_drive_objective_replan -v
```

That test starts a `resolve_transaction` objective over an isolated
Portal-shaped sales order. An exact-scope fake Graph drive supplies a text
document with explicit sales order and customer PO identifiers. The configured
stream downloads and extracts the bytes, persists source-cited claims, and
replans the ongoing objective to request identity review. After a separate
local identity-review decision, the same objective shows the connected
document, a missing Portal customer PO finding, and a draft-only correction
preview. It asserts that no Portal field or source system was written. This
exercises the running platform path; it does not establish live Microsoft 365
or Portal access.

All records in these tests are isolated fixtures. No Microsoft 365 credential
or live Petra document was available for this run.

For the bundled synthetic scanned PDF, run
`PLX_TEST_NATIVE_OCR=1 python3 -m unittest integrations.tests.test_local_pdf_ocr -q`
on a macOS host with working Vision OCR. The injected Graph drive test verifies
candidate review and cursor rollback without a Microsoft credential. These
tests do not validate recognition accuracy on Petra's historical files.

An operator-supplied AO scan can be checked without copying it into the
repository:

```sh
PLX_TEST_PRIVATE_AO_PDF="$HOME/Downloads/AO-26005B.pdf" \
  python3 \
  -m unittest integrations.tests.test_local_pdf_ocr.NativePdfOcrTest.test_operator_supplied_ao_scan_has_cited_review_only_claims -q
```

This test is opt-in;
it checks that an OCR AO identifier is cited and every OCR claim remains
review-only. It does not confirm transcription accuracy, an MRP/ERP match, or
production integration. The actual six-file offline import evidence is in
`EVIDENCE/local-ao-ocr-current.json`.
