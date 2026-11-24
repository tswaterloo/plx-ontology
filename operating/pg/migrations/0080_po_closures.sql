-- 0080_po_closures: a person's "closed: why, who, when" on a purchase order, a Workbench state only
-- (never a write to Business Central, the Portal or Sage). Append-only: a row closes the order, a
-- later 'reopened' row undoes it; the newest row of a handle decides. The API (plx_api) reads and
-- appends; nothing is updated or deleted.
-- Undo: DROP TRIGGER po_closures_append_only ON assist.po_closures; DROP FUNCTION assist.po_closures_append_only(); DROP TABLE assist.po_closures;
CREATE TABLE IF NOT EXISTS assist.po_closures (
  closure_id bigserial PRIMARY KEY,
  handle     text NOT NULL,
  action     text NOT NULL CHECK (action IN ('closed', 'reopened')),
  reason     text,
  by_person  text NOT NULL,
  receipts   integer NOT NULL DEFAULT 0,   -- receipts linked to the order when it was closed
  at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS po_closures_handle ON assist.po_closures (handle, closure_id DESC);
CREATE OR REPLACE FUNCTION assist.po_closures_append_only() RETURNS trigger LANGUAGE plpgsql AS $t$
BEGIN RAISE EXCEPTION 'assist.po_closures is append-only'; END $t$;
DROP TRIGGER IF EXISTS po_closures_append_only ON assist.po_closures;
CREATE TRIGGER po_closures_append_only BEFORE UPDATE OR DELETE ON assist.po_closures
  FOR EACH ROW EXECUTE FUNCTION assist.po_closures_append_only();
REVOKE ALL ON assist.po_closures FROM PUBLIC;
DO $api$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN GRANT USAGE ON SCHEMA assist TO plx_api; GRANT SELECT, INSERT ON assist.po_closures TO plx_api; GRANT USAGE ON SEQUENCE assist.po_closures_closure_id_seq TO plx_api; END IF; END $api$;
DO $ev$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN GRANT USAGE ON SCHEMA assist TO plx_eval; GRANT SELECT ON assist.po_closures TO plx_eval; END IF; END $ev$;
