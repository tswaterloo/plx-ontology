-- 0068_record_comms: the content-free index "this message names this record" (speed specs 8.2).
-- Five columns, no subject, sender or text. NEVER read alone: every read joins it to the guarded
-- biz_person.<view> (the mailbox rule, the people exclusion, hidden documents), so a row a reader may
-- not see is dropped there and the index reveals nothing. record_id is a page key of
-- biz.page_items (a member record id: pages are keyed per member today; SV3's alias rows come later).
-- Additive; no business view, value or link row changes. Undo: DROP VIEW biz_person.record_comms; DROP TABLE biz.record_comms.
CREATE TABLE IF NOT EXISTS biz.record_comms (
  record_id   text        NOT NULL,
  comm_view   text        NOT NULL,
  comm_handle text        NOT NULL,
  at          timestamptz,
  via         text        NOT NULL,
  PRIMARY KEY (record_id, comm_view, comm_handle)
);
CREATE INDEX IF NOT EXISTS record_comms_page ON biz.record_comms (record_id, comm_view, at DESC);
CREATE INDEX IF NOT EXISTS record_comms_comm ON biz.record_comms (comm_view, comm_handle);
-- the read side: a view in the person schema (the store's login reads it there, beside the guarded
-- biz_person.<view> it is always joined to; the table itself stays closed to every API role)
CREATE SCHEMA IF NOT EXISTS biz_person;
REVOKE ALL ON SCHEMA biz_person FROM PUBLIC;
CREATE OR REPLACE VIEW biz_person.record_comms AS
  SELECT record_id, comm_view, comm_handle, at, via FROM biz.record_comms;
DO $migration$
BEGIN
  REVOKE ALL ON biz.record_comms FROM PUBLIC;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
    REVOKE ALL ON biz.record_comms FROM plx_api;
    GRANT USAGE ON SCHEMA biz_person TO plx_api;
    GRANT SELECT ON biz_person.record_comms TO plx_api;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
    REVOKE ALL ON biz.record_comms FROM plx_eval;
  END IF;
END
$migration$;
