-- 0002_units_generation: what hidden-match counts may say about a unit, the people a
-- unit belongs to (the people exclusion), the model of an entity's name vector, and a
-- write generation the permission scope cache keys on.

-- An entity's name vector names its model too (re-embed key, as chunks.embed_model).
ALTER TABLE search.entities ADD COLUMN IF NOT EXISTS name_embed_model text NULL;
ALTER TABLE search.entities ADD CONSTRAINT entities_name_embedding_has_model
    CHECK ((name_embedding IS NULL) = (name_embed_model IS NULL));

-- One row per audience unit the ingest knows more about (written by the ingest, Lane B):
--   sharing  'shared' (a site, team, company, shared mailbox: its name may be shown in a
--            hidden-match count) or 'personal' (a person's mailbox, chat or OneDrive: its
--            hidden matches are only ever counted in one unnamed bucket);
--   label    a human name for the unit (site, team, company name; never a raw id);
--   people   the Entra object ids of the person or members the unit belongs to, used only
--            to drop excluded people's units (PLX_EXCLUDED_PEOPLE_OIDS) from results and
--            counts. Never shown.
-- A unit without a row is decided by its family alone (operating/pg/permissions.py):
-- unknown means personal, so nothing is named by default.
CREATE TABLE IF NOT EXISTS search.audience_units (
    audience_key text PRIMARY KEY CHECK (audience_key ~ '^[a-z][a-z0-9-]*:.+$'
                                         AND position('*' in audience_key) = 0),
    sharing      text NOT NULL CHECK (sharing IN ('shared', 'personal')),
    label        text NULL CHECK (label IS NULL OR label <> ''),
    people       text[] NOT NULL DEFAULT '{}' CHECK (array_position(people, NULL) IS NULL),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Bumped by every statement that can change the audience keys or units the search tables
-- hold. A sequence (no row lock between parallel writers); it is not transactional, so a
-- scope cached across a bump that commits later may miss the new keys: those rows stay
-- hidden (fail closed) until the cache entry expires (ScopeCache ttl).
CREATE SEQUENCE IF NOT EXISTS search.write_generation;

CREATE OR REPLACE FUNCTION search.bump_write_generation() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    PERFORM nextval('search.write_generation');
    RETURN NULL;
END
$$;

CREATE TRIGGER chunks_write_generation
    AFTER INSERT OR DELETE OR UPDATE OF audience_key ON search.chunks
    FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();
CREATE TRIGGER chunks_truncate_generation AFTER TRUNCATE ON search.chunks
    FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();
CREATE TRIGGER entities_write_generation
    AFTER INSERT OR DELETE OR UPDATE OF audience_key ON search.entities
    FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();
CREATE TRIGGER entities_truncate_generation AFTER TRUNCATE ON search.entities
    FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();
CREATE TRIGGER audience_units_write_generation
    AFTER INSERT OR UPDATE OR DELETE ON search.audience_units
    FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();
CREATE TRIGGER audience_units_truncate_generation AFTER TRUNCATE ON search.audience_units
    FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();
