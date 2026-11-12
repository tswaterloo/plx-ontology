-- 0015_marked_chunks_index: the marked documents read (operating/search/document_marks.py
-- ``_Marked._read``: every non-record chunk whose markings are not empty, re-read every
-- REFRESH_S in each serving process for a reader who does not hold every marking).
--
-- Without it that read is a parallel scan of every chunk (11-11: 4.1 s, 464k buffers, for
-- 33,596 of 2,040,436 rows; five processes ran it at once). Partial on the read's own
-- predicate and covering its columns, so it reads the marked rows only. On a large live
-- table, create it first with
--   CREATE INDEX CONCURRENTLY IF NOT EXISTS chunks_marked ON search.chunks (object_id)
--       INCLUDE (source_handle, markings, kind) WHERE markings <> '{}';
-- so the loader is never blocked; this migration is then a no-op. A database holding only
-- the ontology (no search schema) has nothing to index.
DO $$
BEGIN
    IF to_regclass('search.chunks') IS NOT NULL THEN
        CREATE INDEX IF NOT EXISTS chunks_marked ON search.chunks (object_id)
            INCLUDE (source_handle, markings, kind) WHERE markings <> '{}';
    END IF;
END
$$;
