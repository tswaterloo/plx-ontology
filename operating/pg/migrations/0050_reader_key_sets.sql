-- 0050_reader_key_sets: principal-key sets kept once in shared tables, for a reader that
-- cannot keep them in its own temporary table (a hot standby; operating/pg/sqlite_compat.py
-- ``stateless``). A long JSON key list (the owner's audience keys: 10k keys, 737 KB) bound
-- as an array costs ~140 ms of planning per statement; read from an indexed, analyzed set
-- it costs what the temporary set costs. A set is named by the SHA-256 of the list's JSON
-- text, and only ``reader_key_set_put`` writes one: it computes the name from the text and
-- the members from the same text, so no caller can put a key under a list that does not
-- hold it (a reader of a set reads exactly its list's members, never another key). Readers
-- use a set only for ``x IN (list)``: a set pruned while a statement runs reads as fewer
-- members (fewer rows), never as more. The newest 256 sets by use are kept.
-- Undo: DROP FUNCTION ontology.reader_key_set_put(text);
--       DROP TABLE ontology.reader_key_sets; DROP TABLE ontology.reader_key_set_heads;

CREATE TABLE IF NOT EXISTS ontology.reader_key_set_heads (
    digest text PRIMARY KEY,
    n integer NOT NULL,
    used_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ontology.reader_key_sets (
    digest text NOT NULL REFERENCES ontology.reader_key_set_heads (digest) ON DELETE CASCADE,
    key text NOT NULL,
    PRIMARY KEY (digest, key)
);

CREATE OR REPLACE FUNCTION ontology.reader_key_set_put(raw text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
    d text;
    doc jsonb;
    added integer;
BEGIN
    IF raw IS NULL THEN
        RETURN NULL;
    END IF;
    d := encode(sha256(convert_to(raw, 'UTF8')), 'hex');
    UPDATE ontology.reader_key_set_heads SET used_at = now() WHERE digest = d;
    IF FOUND THEN
        RETURN d;
    END IF;
    BEGIN
        doc := raw::jsonb;
    EXCEPTION WHEN others THEN
        RETURN NULL;   -- not JSON: no set
    END;
    IF jsonb_typeof(doc) <> 'array'
       OR EXISTS (SELECT 1 FROM jsonb_array_elements(doc) e WHERE jsonb_typeof(e) <> 'string') THEN
        RETURN NULL;   -- a set holds plain strings only
    END IF;
    INSERT INTO ontology.reader_key_set_heads (digest, n) VALUES (d, -1)
        ON CONFLICT (digest) DO NOTHING;
    IF NOT FOUND THEN
        RETURN d;      -- put by another session meanwhile
    END IF;
    INSERT INTO ontology.reader_key_sets (digest, key)
        SELECT DISTINCT d, e FROM jsonb_array_elements_text(doc) AS e;
    GET DIAGNOSTICS added = ROW_COUNT;
    -- n is written last, in the same transaction: a reader checks it against its own count
    UPDATE ontology.reader_key_set_heads SET n = added WHERE digest = d;
    DELETE FROM ontology.reader_key_set_heads WHERE digest IN (
        SELECT h.digest FROM ontology.reader_key_set_heads h
        ORDER BY h.used_at DESC, h.digest OFFSET 256);
    RETURN d;
END $$;

REVOKE ALL ON FUNCTION ontology.reader_key_set_put(text) FROM PUBLIC;
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['plx_api', 'plx_eval'] LOOP
        IF to_regrole(r) IS NOT NULL THEN
            EXECUTE format('GRANT SELECT ON ontology.reader_key_set_heads, '
                           'ontology.reader_key_sets TO %I', r);
            EXECUTE format('GRANT EXECUTE ON FUNCTION ontology.reader_key_set_put(text) TO %I', r);
        END IF;
    END LOOP;
END $$;
