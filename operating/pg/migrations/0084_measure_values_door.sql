-- 0084_measure_values_door: door view prerequisites for biz.measure_values (GI5 / A10 / G4).
-- Adds a plain nullable handle column (metadata-only in Postgres, no table rewrite of the ~1.6M
-- row / ~1.5 GB table). It stays NULL so door.held_class_sql() matches outright held classes
-- (plx.held / plx.classes) and fails closed on document-resolved classes (plx.dheld / plx.dclasses,
-- which require per-handle plx.d_hidden checks). The guarded view biz_page.measure_values is
-- created by operating.pg.generic_views.make_views (pages_only=True, granted to plx_page_reader
-- only, never biz_sql / plx_sql_reader); the raw table stays revoked from PUBLIC, plx_api, plx_eval.
-- No business view table, ontology row or link changes: no view print or digest moves.
-- Undo: DROP VIEW IF EXISTS biz_page.measure_values; ALTER TABLE biz.measure_values DROP COLUMN IF EXISTS handle;
ALTER TABLE biz.measure_values ADD COLUMN IF NOT EXISTS handle text;
DO $$
BEGIN
    REVOKE ALL ON biz.measure_values FROM PUBLIC;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON biz.measure_values FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        REVOKE ALL ON biz.measure_values FROM plx_eval;
    END IF;
END $$;
