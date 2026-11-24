-- 0074_comm_text (ASK8, Fable spec 15.3 step 2): a dated text sidecar for the comms search.
-- The word match of a message's text was read from ontology.chunk_words, which has no date and
-- no source: a common party word read its whole match (36,900 chunks for one name, 45-61 s) and
-- joined it back to the view. biz.comm_text holds one row per comms message (handle), with its
-- date and the union of its text chunks' word vectors, so a search prunes by the window and the
-- view before it reads the words. A sidecar keyed by handle: no view, column, row or ontology
-- change, so no comms fingerprint or digest moves. The words of a field are kept whatever its
-- markings are: the search joins the field by field_id and takes it only while its markings are
-- '[]' at read time (a marking set or lifted later is followed at once). Written by
-- operating/pg/comm_text.py only.
--   handle    the comms row's own version handle (biz.emails / biz.teams_messages .handle)
--   view      the table it is a row of
--   at        its date (received_at / created_at), the search window's column
--   field_id  the message's text field (ontology.text_fields); NULL: it has no text
--   tsv       the union of its chunks' word vectors (empty: no text)
-- biz.comm_text_state: per view, what the sidecar covers: covered_from (rows dated before it are
-- read the old way), built_field_id (every text field up to it is in; the search reads the
-- newer ones the old way), built_at, rows, no_text, oversize (rows whose vector would pass the
-- tsvector limit: a build stops on one, never leaving it out unsaid).
-- Undo: DROP TABLE biz.comm_text, biz.comm_text_state (the search then reads the old way).
CREATE TABLE IF NOT EXISTS biz.comm_text (
    handle    text PRIMARY KEY,
    view      text NOT NULL,
    at        timestamptz NOT NULL,
    field_id  bigint,
    tsv       tsvector NOT NULL);
CREATE INDEX IF NOT EXISTS comm_text_tsv ON biz.comm_text USING gin (tsv);
CREATE INDEX IF NOT EXISTS comm_text_view_at ON biz.comm_text (view, at DESC);
CREATE TABLE IF NOT EXISTS biz.comm_text_state (
    view            text PRIMARY KEY,
    covered_from    timestamptz NOT NULL,
    built_field_id  bigint NOT NULL,
    built_at        timestamptz NOT NULL,
    rows            bigint NOT NULL DEFAULT 0,
    no_text         bigint NOT NULL DEFAULT 0,
    notes           jsonb NOT NULL DEFAULT '{}');
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON biz.comm_text, biz.comm_text_state TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT SELECT ON biz.comm_text, biz.comm_text_state TO plx_eval;
    END IF;
END $$;
