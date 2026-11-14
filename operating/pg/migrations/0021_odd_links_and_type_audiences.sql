-- 0019_odd_links_and_type_audiences (CC-P-07): three reads that scanned whole tables, each
-- now read from a small index on its own predicate (pg_stat_statements, 11-13 12:23 PM -
-- 1:10 PM ET, live: 45 min of serving and delta):
-- * the links one of whose evidence slots is an odd text (ontology_reader.LINK_ODD_ROWS_SQL,
--   every whole-store link slice): 10 calls, 130 s each, 143M blocks (~1.1 TB) read from
--   the 128 GB link_rows heap. Only rows with an evidence_id are read (1.7% of rows, ~2
--   slots each), so a GIN on the read's own expression, partial on its own predicate, is
--   small (~2.7M entries);
-- * the odd link texts themselves (LINK_ODD_SQL, the same calls): the 13.4M-row link_texts
--   scanned with a regex each time; partial on that regex, it holds the odd ones only;
-- * the (object type, audience) pairs of the whole store
--   (ontology_tools_traverse ``_type_audiences``): 15 calls, 31 s each, 11M blocks of the
--   objects heap; an index-only scan of the pair index instead.
-- Live (11-13) they were built first under the same names and definitions (plain CREATE
-- INDEX with a short lock_timeout: 7 s, 16 s and 5 min 25 s), so this migration is a no-op
-- there.
-- link_rows as a plain table (the live one, 11-13); once it is partitioned (lane di03's
-- 0017) an index on the parent cannot be built CONCURRENTLY and would lock every partition
-- here: that layout builds it per partition from its own index list instead.
DO $$
BEGIN
    IF (SELECT relkind FROM pg_class WHERE oid = 'ontology.link_rows'::regclass) = 'r' THEN
        CREATE INDEX IF NOT EXISTS link_rows_odd_slots ON ontology.link_rows
            USING gin ((slots[1:ek])) WHERE evidence_id IS NOT NULL;
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS link_texts_odd_ids ON ontology.link_texts (id)
    WHERE text ~ '["\\\x01-\x1f]';
CREATE INDEX IF NOT EXISTS objects_by_type_audience ON ontology.objects
    (object_type, audience_key);
