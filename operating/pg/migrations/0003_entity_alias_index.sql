-- 0003_entity_alias_index: entity lookup by alias (Lane C query understanding).
--
-- search.entities.alias holds normalised ids and attribute words (country, region,
-- category ...). Two indexes:
--   entities_alias_gin        array GIN: alias && ARRAY[...] / alias @> ARRAY[...]
--   entities_alias_text_trgm  trigram GIN over search.alias_text(alias): fuzzy and typo
--                             tolerant match (%, similarity, ILIKE). A query must use the
--                             same expression, search.alias_text(alias), to use it.
-- Every lookup still applies the reader's permission filter (operating/pg/permissions.py).

-- array_to_string is only STABLE in the catalogue (generic element output); over text[]
-- its result depends on nothing but the input, so this wrapper is IMMUTABLE and indexable.
CREATE OR REPLACE FUNCTION search.alias_text(text[]) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $$ SELECT array_to_string($1, ' ') $$;

CREATE INDEX IF NOT EXISTS entities_alias_gin ON search.entities USING gin (alias);
CREATE INDEX IF NOT EXISTS entities_alias_text_trgm ON search.entities
    USING gin (search.alias_text(alias) gin_trgm_ops);
