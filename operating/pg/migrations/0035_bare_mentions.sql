-- 0035_bare_mentions (row 14): identifiers a mail writes bare, as lead mention links kept
-- beside the served links (``operating/pg/bare_mentions.py``).
-- * ontology.bare_mentions: rows shaped as ``ontology.links``' columns. The build steps never
--   read it (their search_path is ``ontology, public``) and ``link_rows`` is never written,
--   so no step input and no schema, step or key digest moves.
-- * ontology_bare.links: the served links plus these rows; ontology_bare_seen.links: the
--   same over ``ontology_seen.links`` (mention repeats hidden). A reader puts one first on
--   its search_path only when PLX_PG_BARE_MENTIONS=1; off, nothing reads either. Both read
--   ``ontology.links`` only through functions (``ontology.links_bare()``, 0029's
--   ``ontology.links_seen()``): ``ontology_swap`` drops ``link_rows`` CASCADE, which would
--   drop a view bound to ``ontology.links`` with it (a function body is not a dependency).
-- * bare_mentions_token: the row's token (``why_not``), so a delta refresh rechecks the rows
--   of a changed record's codes in SQL (``bare_mentions.refresh``).
-- Undo: switch off, then TRUNCATE ontology.bare_mentions (or DROP SCHEMA ontology_bare,
-- ontology_bare_seen CASCADE; DROP TABLE ontology.bare_mentions).
CREATE TABLE IF NOT EXISTS ontology.bare_mentions (
    link_id          text PRIMARY KEY,
    link_type        text NOT NULL,
    from_object      text NOT NULL,
    to_object        text NOT NULL,
    tier             text NOT NULL,
    binding_kind     text NOT NULL,
    link_contract    text,
    reason           text,
    why_not          text NOT NULL,
    evidence_handles text,
    reviewed_by      text,
    reviewed_at      text,
    review_action_id text,
    markings         text NOT NULL,
    recorded_at      text NOT NULL,
    superseded_at    text,
    CHECK (link_id LIKE 'bare:%' AND tier = 'lead' AND binding_kind = 'mention'
           AND link_type LIKE 'Email.mentions\_%'));
CREATE INDEX IF NOT EXISTS bare_mentions_from ON ontology.bare_mentions (from_object, link_type);
CREATE INDEX IF NOT EXISTS bare_mentions_to ON ontology.bare_mentions (to_object, link_type);
CREATE INDEX IF NOT EXISTS bare_mentions_token
    ON ontology.bare_mentions (((why_not::jsonb -> 0 ->> 'value')));

CREATE OR REPLACE FUNCTION ontology.links_bare()
RETURNS TABLE (link_id text, link_type text, from_object text, to_object text, tier text,
               binding_kind text, link_contract text, reason text, why_not text,
               evidence_handles text, reviewed_by text, reviewed_at text,
               review_action_id text, markings text, recorded_at text, superseded_at text)
LANGUAGE sql STABLE AS $$
    SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.binding_kind,
           l.link_contract, l.reason, l.why_not, l.evidence_handles, l.reviewed_by,
           l.reviewed_at, l.review_action_id, l.markings, l.recorded_at, l.superseded_at
      FROM ontology.links l
    UNION ALL
    SELECT link_id, link_type, from_object, to_object, tier, binding_kind, link_contract,
           reason, why_not, evidence_handles, reviewed_by, reviewed_at, review_action_id,
           markings, recorded_at, superseded_at FROM ontology.bare_mentions
$$;

CREATE SCHEMA IF NOT EXISTS ontology_bare;
CREATE OR REPLACE VIEW ontology_bare.links AS SELECT * FROM ontology.links_bare();

CREATE SCHEMA IF NOT EXISTS ontology_bare_seen;
CREATE OR REPLACE VIEW ontology_bare_seen.links AS
    SELECT * FROM ontology_seen.links
    UNION ALL
    SELECT link_id, link_type, from_object, to_object, tier, binding_kind, link_contract,
           reason, why_not, evidence_handles, reviewed_by, reviewed_at, review_action_id,
           markings, recorded_at, superseded_at FROM ontology.bare_mentions;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA ontology_bare, ontology_bare_seen TO plx_api;
        GRANT SELECT ON ontology_bare.links, ontology_bare_seen.links TO plx_api;
        GRANT SELECT ON ontology.bare_mentions TO plx_api;
    END IF;
END
$$;
