-- value_cells_text (field, value) WHERE value_num IS NULL AND length(value) <= 200 was never
-- chosen by any reader (0 scans, 5 GB, slowing every delta apply): no query sends its
-- predicate. A database built from the migrations no longer carries it. The live database
-- dropped it already; IF EXISTS makes this a no-op there.
DROP INDEX IF EXISTS ontology.value_cells_text;
