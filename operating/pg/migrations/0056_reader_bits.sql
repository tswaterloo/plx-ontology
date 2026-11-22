-- 0056_reader_bits: a 1-bit copy of every embedded chunk's vector, clustered by audience,
-- for the readers who see only part of the chunks (a staff member: the shared units and
-- their own mail and chats).
--
-- Why: the whole-table HNSW walks the graph and filters by the reader's permission
-- predicate afterwards. 11-22, 20 real questions vs the exact nearest texts: a one-mailbox
-- reader got 0.03 of them, and even the ~935k shared chunks alone (no filter) got
-- 0.72-0.80. An exact scan of a reader's own rows is exact but costs ~2.8 us per row,
-- because each 2 kB halfvec is fetched from TOAST (1.1M rows: 4.4 s). Here one row is
-- ~170 B (bit(dim), the reader predicate's columns): the reader's rows are scanned in
-- Hamming order and the best few thousand are checked again with the full vector on
-- search.chunks (``operating.search.retrievers.dense``, shape "bits"): at an 8,000-row
-- re-check, a staff reader (935k shared + a 162k mailbox) recall@10 1.0 in 0.46 s (p95), a
-- 624k three-mailbox scope 0.96 in 0.37 s.
--
-- Kept current by a trigger on search.chunks (an embedded row upserted, a row without a
-- vector or deleted removed, a truncate empties it). A row here only proposes a
-- candidate: the re-check joins search.chunks on chunk_id and applies the reader's
-- predicate to that row again, so a stale or missing row here can cost recall, never
-- show a row the reader may not read. New rows append at the heap's end; a repack by
-- audience_key (``operating.pg.locked_repack``) restores the clustering.
--
-- Nothing reads it until ``search.reader_bits_state.ready`` is true (set by
-- ``python3 -m operating.pg.reader_bits switch on`` once the fill has run).
-- The trigger functions run as the migration's owner (SECURITY DEFINER, a fixed search_path),
-- so every writer of search.chunks keeps this table current without a grant of its own.
-- Same readers as search.chunks (plx_api, plx_eval: SELECT), no wider.
-- A database without search.chunks (a test of another schema) is left alone.
-- Undo: switch off; DROP TRIGGER reader_bits_sync, reader_bits_drop, reader_bits_truncate
-- ON search.chunks; DROP FUNCTION search.reader_bits_sync(), search.reader_bits_truncate();
-- DROP TABLE search.reader_bits, search.reader_bits_state.
DO $migration$
DECLARE
    dim integer;
BEGIN
    IF to_regclass('search.chunks') IS NULL THEN
        RETURN;
    END IF;
    SELECT a.atttypmod INTO dim FROM pg_attribute a
     WHERE a.attrelid = 'search.chunks'::regclass AND a.attname = 'embedding';
    IF dim IS NULL OR dim < 1 THEN
        RAISE EXCEPTION '0056_reader_bits: search.chunks.embedding has no dimension';
    END IF;
    -- the trigger takes a short lock on search.chunks: never wait behind a long writer
    PERFORM set_config('lock_timeout', '10s', true);
    EXECUTE format('CREATE TABLE IF NOT EXISTS search.reader_bits (
        chunk_id     text PRIMARY KEY,
        audience_key text NOT NULL,
        markings     text[] NOT NULL,
        kind         text NOT NULL,
        embed_model  text NOT NULL,
        bits         bit(%s) NOT NULL
    )', dim);
    CREATE INDEX IF NOT EXISTS reader_bits_audience ON search.reader_bits (audience_key);
    CREATE TABLE IF NOT EXISTS search.reader_bits_state (
        singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        ready      boolean NOT NULL DEFAULT false,
        filled_at  timestamptz,
        fill_after text,
        changed_at timestamptz NOT NULL DEFAULT now()
    );
    INSERT INTO search.reader_bits_state (singleton) VALUES (true) ON CONFLICT DO NOTHING;

    EXECUTE format($fn$
        CREATE OR REPLACE FUNCTION search.reader_bits_sync() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_catalog AS $body$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                DELETE FROM search.reader_bits WHERE chunk_id = OLD.chunk_id;
                RETURN NULL;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.chunk_id IS DISTINCT FROM NEW.chunk_id THEN
                DELETE FROM search.reader_bits WHERE chunk_id = OLD.chunk_id;
            END IF;
            IF NEW.embedding IS NULL OR NEW.embed_model IS NULL THEN
                DELETE FROM search.reader_bits WHERE chunk_id = NEW.chunk_id;
            ELSE
                INSERT INTO search.reader_bits AS b
                       (chunk_id, audience_key, markings, kind, embed_model, bits)
                VALUES (NEW.chunk_id, NEW.audience_key, NEW.markings, NEW.kind,
                        NEW.embed_model, binary_quantize(NEW.embedding)::bit(%s))
                ON CONFLICT (chunk_id) DO UPDATE SET audience_key = excluded.audience_key,
                    markings = excluded.markings, kind = excluded.kind,
                    embed_model = excluded.embed_model, bits = excluded.bits;
            END IF;
            RETURN NULL;
        END
        $body$
    $fn$, dim);
    CREATE OR REPLACE FUNCTION search.reader_bits_truncate() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_catalog AS $body$
    BEGIN
        TRUNCATE search.reader_bits;
        RETURN NULL;
    END
    $body$;
    DROP TRIGGER IF EXISTS reader_bits_sync ON search.chunks;
    CREATE TRIGGER reader_bits_sync
        AFTER INSERT OR UPDATE OF chunk_id, embedding, embed_model, audience_key, markings, kind
        ON search.chunks FOR EACH ROW EXECUTE FUNCTION search.reader_bits_sync();
    DROP TRIGGER IF EXISTS reader_bits_drop ON search.chunks;
    CREATE TRIGGER reader_bits_drop AFTER DELETE ON search.chunks
        FOR EACH ROW EXECUTE FUNCTION search.reader_bits_sync();
    DROP TRIGGER IF EXISTS reader_bits_truncate ON search.chunks;
    CREATE TRIGGER reader_bits_truncate AFTER TRUNCATE ON search.chunks
        FOR EACH STATEMENT EXECUTE FUNCTION search.reader_bits_truncate();

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON search.reader_bits, search.reader_bits_state TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT SELECT ON search.reader_bits, search.reader_bits_state TO plx_eval;
    END IF;
END
$migration$;
