-- 0057_reader_bits_model_follow: the reader-bits trigger follows a change of embedding model.
--
-- 0056 wrote the column's width into the sync function (binary_quantize(..)::bit(1024)) and
-- named columns in the trigger (UPDATE OF ...); a column a trigger names cannot be retyped, so
-- ``migrate.set_embedding`` could not change the model's dimension. This replaces both:
-- the function names no dimension (the column's type carries it) and returns at once when an
-- UPDATE changes none of the columns it copies; the trigger names no column. 0056 stays as it
-- was applied (its recorded checksum is unchanged).
-- A database without search.chunks or search.reader_bits is left alone.
-- Undo: re-run the CREATE statements of 0056 for the function and trigger.
DO $migration$
BEGIN
    IF to_regclass('search.chunks') IS NULL OR to_regclass('search.reader_bits') IS NULL THEN
        RETURN;
    END IF;
    PERFORM set_config('lock_timeout', '10s', true);
    CREATE OR REPLACE FUNCTION search.reader_bits_sync() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_catalog AS $body$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            DELETE FROM search.reader_bits WHERE chunk_id = OLD.chunk_id;
            RETURN NULL;
        END IF;
        IF TG_OP = 'UPDATE' THEN
            IF OLD.chunk_id IS NOT DISTINCT FROM NEW.chunk_id
               AND OLD.embed_model IS NOT DISTINCT FROM NEW.embed_model
               AND OLD.audience_key IS NOT DISTINCT FROM NEW.audience_key
               AND OLD.markings IS NOT DISTINCT FROM NEW.markings
               AND OLD.kind IS NOT DISTINCT FROM NEW.kind
               AND OLD.embedding IS NOT DISTINCT FROM NEW.embedding THEN
                RETURN NULL;
            END IF;
            IF OLD.chunk_id IS DISTINCT FROM NEW.chunk_id THEN
                DELETE FROM search.reader_bits WHERE chunk_id = OLD.chunk_id;
            END IF;
        END IF;
        IF NEW.embedding IS NULL OR NEW.embed_model IS NULL THEN
            DELETE FROM search.reader_bits WHERE chunk_id = NEW.chunk_id;
        ELSE
            INSERT INTO search.reader_bits AS b
                   (chunk_id, audience_key, markings, kind, embed_model, bits)
            VALUES (NEW.chunk_id, NEW.audience_key, NEW.markings, NEW.kind,
                    NEW.embed_model, binary_quantize(NEW.embedding))
            ON CONFLICT (chunk_id) DO UPDATE SET audience_key = excluded.audience_key,
                markings = excluded.markings, kind = excluded.kind,
                embed_model = excluded.embed_model, bits = excluded.bits;
        END IF;
        RETURN NULL;
    END
    $body$;
    DROP TRIGGER IF EXISTS reader_bits_sync ON search.chunks;
    CREATE TRIGGER reader_bits_sync
        AFTER INSERT OR UPDATE ON search.chunks FOR EACH ROW EXECUTE FUNCTION search.reader_bits_sync();
END
$migration$;
