# How the reader reaches artwork archives over its content bound

- decided: 2026-10-17 (open since 2026-10-16T06:53Z)
- paths: operating/use_case_intake.py, scripts/case_originals.py, integrations/live_source_guard.py, integrations/graph_read.py
- status: decided

## Question

The four captures of `Artwork_Missing_Evidence_part1-4.zip` are bound to the artwork
lifecycle case. Parts 1, 2 and 3 are 100,281,958, 163,676,200 and 186,120,429 bytes (provider
sizes, from the capture records). The reader's content bound
`operating.use_case_intake.MAX_CONTENT_BYTES` is 100,000,000, so `SharePointReader.get_content`
returns `exceeds_cap` after one metadata GET and downloads nothing. How should the reader get
their SHA-256 and member manifest?

## Options

1. Raise `MAX_CONTENT_BYTES` (for example to 256 MB), for capture rows only or for all rows.
   One constant, but the whole archive is held in memory and `use_case_parts.members` reads it
   as one buffer: peak about 2x the archive (~400 MB) on the verifier host.
2. A streaming, ranged ZIP read: hash the download stream in `get_content`; read the central
   directory with HTTP `Range` GETs at the end of the file; read each member alone, up to
   `case_originals.MAX_PART_BYTES`. Needs a `Range` header allowance in the GET-only guard
   (`integrations/live_source_guard.py`, `graph_read.py`), a stream hash in `get_content`, and
   a ZIP reader over ranges in `use_case_parts`.
3. A person splits or re-exports the archives into parts under 100 MB on SharePoint.

## Decision

Option 2 is the target design. It touches the egress guard, so it is a separate change, `artwork-archive-ranged-read`.

Until it lands the shipped default stands: parts 1-3 stay typed `oversize` registry rows with
`oversize` {bytes, bound, bound_name}, read `capped` / `exceeds_cap`, and the live check counts
them as findings, never silent skips. Part 4 (48,212,204 bytes) is under the bound and is read
in full; its receipt carries the SHA-256 and member manifest.

## Rejected

- Option 1: the whole archive sits in memory at about 2x peak (~400 MB) on the verifier host,
  and `MAX_CONTENT_BYTES` is shared by every row, so the cost is paid everywhere.
- Option 3: the bytes read would no longer be the captured originals, which breaks provenance.

## Evidence

Sizes are from capture records `b45848e1`, `4d5159b6`, `5a2c543b`, `bfb07c40`. The container was keyless, so no archive bytes were read. No test or fixture shows the
members of parts 1-3, and the no-fabrication rule forbids guessing them.
