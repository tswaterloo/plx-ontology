-- value_cells_num (field, value_num) WHERE value_num IS NOT NULL was never chosen by any
-- reader: 0 scans on the live database from its load (11-10) to 11-11 (stats never reset),
-- and no query sends a value_num range or equality predicate (the readers filter only
-- value_num IS NULL, for the word index). 1 GB, written on every delta apply. A database
-- built from the migrations no longer carries it. The live database drops it with
-- DROP INDEX CONCURRENTLY; IF EXISTS makes this a no-op there.
DROP INDEX IF EXISTS ontology.value_cells_num;
