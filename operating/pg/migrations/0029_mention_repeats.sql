-- 0029_mention_repeats (CC-C-04): a document's repeated mentions of one record, marked so a
-- Postgres reader can skip them (``operating/pg/mention_repeats.py``; the read side's
-- ``operating.ontology_tools.mention_pair_key`` / ``collapse_mention_rows`` decide which).
-- * ontology.mention_repeats: one row per stored mention link that repeats its pair's
--   keeper (``keeper_id``: the row the read side shows for the pair). The build steps never
--   read it and ``link_rows`` is never written for it: no tier, superseded_at, id or delete
--   changes, so no step's input and no digest moves. ``link_type``/``from_object``/
--   ``to_object``: the pair's, to refresh one object's markers and to probe one partition.
-- * ontology.links_seen(): ``ontology.links`` without a marked row whose keeper still
--   stands in its place (live, same ends, markings, recorded time and evidence, a tier at
--   least as strong). A stale marker shows a repeat; it never hides a pair's only link.
--   A function, not a view: its body is read by name when called (inlined by the planner),
--   so a rebuild of ``link_rows`` or of ``ontology.links`` (link_partitions' swap renames
--   the table; ontology_swap drops with CASCADE) neither follows the old table nor is
--   dropped with it.
-- * ontology_seen.links: the function as a relation named ``links``. A reader puts
--   ``ontology_seen`` first on its search_path only when the switch
--   (``PLX_PG_MENTION_REPEATS=1``) is on; off, nothing reads either.
-- Undo: switch off, then TRUNCATE ontology.mention_repeats.
CREATE TABLE IF NOT EXISTS ontology.mention_repeats (
    link_id     text PRIMARY KEY,
    keeper_id   text NOT NULL,
    link_type   text NOT NULL,
    from_object text NOT NULL,
    to_object   text NOT NULL);
CREATE INDEX IF NOT EXISTS mention_repeats_from ON ontology.mention_repeats (from_object);
CREATE INDEX IF NOT EXISTS mention_repeats_to ON ontology.mention_repeats (to_object);
CREATE INDEX IF NOT EXISTS mention_repeats_keeper ON ontology.mention_repeats (keeper_id);

CREATE OR REPLACE FUNCTION ontology.links_seen()
RETURNS TABLE (link_id text, link_type text, from_object text, to_object text, tier text,
               binding_kind text, link_contract text, reason text, why_not text,
               evidence_handles text, reviewed_by text, reviewed_at text,
               review_action_id text, markings text, recorded_at text, superseded_at text)
LANGUAGE sql STABLE AS $$
    SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.binding_kind,
           l.link_contract, l.reason, l.why_not, l.evidence_handles, l.reviewed_by,
           l.reviewed_at, l.review_action_id, l.markings, l.recorded_at, l.superseded_at
      FROM ontology.links l
     WHERE NOT EXISTS (
        SELECT 1 FROM ontology.mention_repeats r
          JOIN ontology.links k ON k.link_type = r.link_type AND k.link_id = r.keeper_id
         WHERE r.link_id = l.link_id AND r.link_type = l.link_type
           AND l.superseded_at IS NULL AND l.binding_kind = 'mention'
           AND k.superseded_at IS NULL AND k.binding_kind = 'mention'
           AND k.from_object = l.from_object AND k.to_object = l.to_object
           AND k.markings = l.markings AND k.recorded_at = l.recorded_at
           AND k.evidence_handles IS NOT DISTINCT FROM l.evidence_handles
           AND array_position(ARRAY['verified', 'reviewed', 'corroborated',
                                    'stored_by_text_join', 'lead', 'contradicted'], k.tier)
               <= array_position(ARRAY['verified', 'reviewed', 'corroborated',
                                       'stored_by_text_join', 'lead', 'contradicted'], l.tier))
$$;

CREATE SCHEMA IF NOT EXISTS ontology_seen;
CREATE OR REPLACE VIEW ontology_seen.links AS SELECT * FROM ontology.links_seen();

-- the API's login reads them (deploy/pg/plx_api_role.sql grants ontology's tables only)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA ontology_seen TO plx_api;
        GRANT SELECT ON ontology_seen.links TO plx_api;
        GRANT SELECT ON ontology.mention_repeats TO plx_api;
    END IF;
END
$$;
