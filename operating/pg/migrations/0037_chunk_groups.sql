-- 0037_chunk_groups: duplicate clusters in their own narrow table (chunk_id -> dup_group,
-- with the content_sha of the text that was grouped).
-- The streaming regroup (``python3 -m operating.embed regroup``, plx-search-dedup.timer)
-- writes here only, so re-clustering never updates a search.chunks row: an update of the
-- indexed search.chunks.dup_group is never HOT and re-inserts the row into every index of
-- search.chunks, the HNSW vector indexes included (review of 6d77cdff9e, 11-16).
--
-- Readers take the cluster from here (operating/search/retrievers.py details,
-- operating/search/copies.py lookup), always joined to search.chunks on chunk_id AND
-- content_sha, under the reader's permission predicate: a row of this table alone shows
-- nothing, and a row whose chunk was rewritten, deleted or re-added with another text
-- since the regroup read it is ignored (a stale cluster never stands for another text).
-- So the delta path writes nothing here (no trigger, no extra statement on any chunk
-- write): orphan rows are inert and the next regroup removes them.
-- search.chunks.dup_group stays for code that runs before this migration.
--
-- The copy of the existing clusters reads the partial index chunks_dup_group (~0.9M rows);
-- it keeps today's clusters until the first regroup replaces them. A database without
-- search.chunks (a test of another schema) is left alone.
DO $migration$
BEGIN
    IF to_regclass('search.chunks') IS NULL THEN
        RETURN;
    END IF;
    CREATE TABLE IF NOT EXISTS search.chunk_groups (
        chunk_id    text PRIMARY KEY,
        dup_group   text NOT NULL,
        content_sha text NOT NULL
    );
    CREATE INDEX IF NOT EXISTS chunk_groups_dup_group ON search.chunk_groups (dup_group);

    INSERT INTO search.chunk_groups (chunk_id, dup_group, content_sha)
    SELECT chunk_id, dup_group, content_sha FROM search.chunks
    WHERE dup_group IS NOT NULL AND content_sha IS NOT NULL
    ON CONFLICT (chunk_id) DO NOTHING;

    -- the API reads the clusters with every search (deploy/pg/plx_api_role.sql's default
    -- privileges cover only tables its own runner creates)
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON search.chunk_groups TO plx_api;
    END IF;
END
$migration$;
