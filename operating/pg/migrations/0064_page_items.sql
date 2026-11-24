-- 0064_page_items: the served store (contract C2, speed specs 1.1) and its build requests (1.2.3).
--
-- biz.page_items: one row per item a page shows (a record's header, each line, each cell a
-- reader may be refused, each linked record ...), in the final shape the app renders, with the
-- access class, column class and lineage of the business row it was built from, so the door
-- reads it exactly as it reads that row (guarded view biz_page.page_items, made by
-- operating.pg.generic_views like every generic table's; never in the AI's schema). One ordinary
-- table, written only by operating.pg.page_items.apply, which changes only the rows that differ:
-- never swapped, no exclusive lock, no cold window. Beside the columns of C2: ai_ok, ver and biz
-- (the door's rule for a row not yet decided against the people exclusion,
-- generic_views.UNDECIDED, reads them: copied from the source row), source_systems (the
-- citation) and stale_since (SV4).
-- biz.page_requests: a page asked for and not yet built (served.request_build); builders take
-- them first and delete the row when the page is applied.
-- Additive only; no business view, value or link row changes: no print moves.
-- Undo: DROP FUNCTION biz_page.page_built(text, text), biz_page.request_build(text, text); DROP VIEW biz_page.page_items; DROP TABLE biz.page_items, biz.page_requests; DELETE FROM
-- biz.lineage WHERE view = 'page_items'.
CREATE TABLE IF NOT EXISTS biz.page_items (
  page_kind      text        NOT NULL,
  page_key       text        NOT NULL,
  section        text        NOT NULL,
  seq            integer     NOT NULL DEFAULT 0,
  payload        jsonb,
  access_class   integer     NOT NULL,
  col_class      integer     NOT NULL DEFAULT 0,
  ai_ok          boolean     NOT NULL DEFAULT true,
  ver            bigint      NOT NULL DEFAULT 0,
  biz            boolean     NOT NULL DEFAULT true,
  cellcol        text,
  src_handle     text        NOT NULL,
  source_systems text,
  handle         text        NOT NULL,
  as_of          timestamptz NOT NULL,
  stale_since    timestamptz,
  dep_ver        bigint      NOT NULL DEFAULT 0,
  dep_ids        bigint[],
  code_fp        text        NOT NULL,
  PRIMARY KEY (page_kind, page_key, section, seq)
) WITH (fillfactor = 90, autovacuum_vacuum_scale_factor = 0.02,
        autovacuum_analyze_scale_factor = 0.02);
CREATE UNIQUE INDEX IF NOT EXISTS page_items_handle ON biz.page_items (handle);
CREATE INDEX IF NOT EXISTS page_items_class ON biz.page_items (access_class);
CREATE INDEX IF NOT EXISTS page_items_src ON biz.page_items (src_handle);
CREATE INDEX IF NOT EXISTS page_items_stale ON biz.page_items (page_kind, stale_since)
  WHERE stale_since IS NOT NULL;
CREATE INDEX IF NOT EXISTS page_items_deps ON biz.page_items USING gin (dep_ids)
  WHERE dep_ids IS NOT NULL;
CREATE TABLE IF NOT EXISTS biz.page_requests (
  page_kind    text        NOT NULL,
  page_key     text        NOT NULL,
  requested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (page_kind, page_key)
);
-- Two functions of the page schema, so that no role of the API reads the business schema itself:
-- the API's login (plx_api) holds SELECT on some business tables (business_comms) that a
-- USAGE on the schema would make readable raw, so it gets none. page_built: whether a page has
-- items at all (a page whose items the reader's door all hides is told from one not built yet;
-- it answers for a key given, never lists keys). request_build: the one row of page_requests.
CREATE SCHEMA IF NOT EXISTS biz_page;
CREATE OR REPLACE FUNCTION biz_page.page_built(kind text, key text) RETURNS boolean
  LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, biz
  AS $f$ SELECT EXISTS (SELECT 1 FROM biz.page_items WHERE page_kind = kind AND page_key = key) $f$;
CREATE OR REPLACE FUNCTION biz_page.request_build(kind text, key text) RETURNS void
  LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, biz
  AS $f$ INSERT INTO biz.page_requests (page_kind, page_key) VALUES (kind, key)
         ON CONFLICT DO NOTHING $f$;
REVOKE ALL ON FUNCTION biz_page.page_built(text, text), biz_page.request_build(text, text)
  FROM PUBLIC;
DO $migration$
BEGIN
  REVOKE ALL ON biz.page_items, biz.page_requests FROM PUBLIC;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
    REVOKE ALL ON biz.page_items FROM plx_api;
    -- the API asks for a build through the function (no access to the business schema)
    GRANT USAGE ON SCHEMA biz_page TO plx_api;
    GRANT EXECUTE ON FUNCTION biz_page.request_build(text, text) TO plx_api;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_page_reader') THEN
    GRANT USAGE ON SCHEMA biz_page TO plx_page_reader;
    GRANT EXECUTE ON FUNCTION biz_page.page_built(text, text) TO plx_page_reader;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
    REVOKE ALL ON biz.page_items, biz.page_requests FROM plx_eval;
  END IF;
END
$migration$;
