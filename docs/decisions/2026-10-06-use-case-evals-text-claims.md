# Decision 2026-10-06: scoring a stated outcome quoted from a delivered document (use-case-evals)

- decided: 2026-10-06 (replaces
  `open-use-case-evals-text-claims.md`)
- status: decided; option 1, built 2026-10-07
  (`claim_checks.quoted_spans`, `_check_quote`).

## Question

Use-case items (`scripts/eval_use_case_items.py`, family F20) expect the case's stated
outcome, a sentence such as "the supplier moved it to Jan 9.", cited to the pinned
SharePoint original (`sharepoint://...#sha256=`, field `row.text`). The claim gate
(`operating/claim_checks.py`) verifies a value only when it (a) equals one cited source
field, or (b) is a number or token on an `original://...#page=N` page's machine-read text.
An agent that reads the original and quotes the outcome exactly is therefore scored
`wrong` (`claim_value_mismatch`). Reproduction (isolated-fixture):
`operating/tests/test_use_case_intake.py`
`LiveDeliveryShapeTests.test_large_delivery_zip_is_pinned_with_every_member_and_becomes_eval_intake`.

## Options

1. A text-span claim: the claimed text occurs verbatim (NFKC, whitespace-normalised, on
   token boundaries) in the re-read text of the cited original or part.
2. Cite each case unit as its own original (`original://sha256/<part>`) with page-like
   text and reuse the page-text check.
3. Score F20 items by an LLM judge.

## Decision

Option 1. Evidence: the page-text check already accepts a non-number value by a bounded
occurrence in re-read text (`claim_checks._check_page_claim`, the `kind != "number"`
branch), so option 1 is that rule widened from "page text of an `original://` citation"
to "re-read text of any cited original", and it serves every mail and document quote,
not only use cases. Option 2 mints a second identity for bytes the intake already pins
(two citations for one original) and only helps this family. Option 3 is rejected: the
scorer is deterministic.

The claim stays untyped (`role_basis: untyped`; a quantity role on a quoted sentence is
`claim_role_unverified`), as on a page.

## Built (2026-10-07)

A text claim is a quote when it cites an original page's `text`, or `row.text`,
`row.content_text` or `row.body_text` of an `original://`, `sharepoint://` or
`graph-mail://` citation (`claim_checks.ORIGINAL_TEXT_FIELDS`); any other field keeps
whole-cell equality, numbers keep their rules. Token boundaries: a quote never starts or
ends inside a word run or a joined token (`re-quote`, `1,509`, `3.78L`). Every number
inside a passed quote is an untyped number for the wording check, so a type word bound to
it is `claim_wording_role_unverified`, as for a page figure. F20 expected values carry the
`extraction_method` of the part they were read from (`tesseract-ocr` for OCR).
