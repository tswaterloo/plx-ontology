-- Two partial indexes the build steps read link_rows by (found on the 11-11 migration: a
-- step stalled for an hour on per-object reads until each existed, built in ~80 s):
-- * an object's current lead links in link id order (unique_mentions' per-object reads,
--   ontology/identity_scoring.py), never its other tiers;
-- * the contradicted links an object holds (the steps that skip a refuted pair: people,
--   parties, postings, tasks, mention firms), the pair and its type from the index alone.
-- The live database built them by hand as tmp_link_rows_*; they were renamed to these names,
-- so IF NOT EXISTS makes this a no-op there.
CREATE INDEX IF NOT EXISTS link_rows_lead_from ON ontology.link_rows
    (from_object, link_id COLLATE "C") WHERE tier = 'lead' AND superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS link_rows_contra_from ON ontology.link_rows
    (from_object) INCLUDE (to_object, link_type) WHERE tier = 'contradicted';
