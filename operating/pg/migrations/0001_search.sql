-- 0001_search: the search schema of the shared lane contract (CONTRACT.md, "Schema search").
--
-- Template parameter: {{embed_dim}} is the dimension of the configured embedding model
-- (operating/pg/config.py), substituted by the migration runner and recorded with the
-- migration. The checksum is taken over this template, so a later model switch
-- (``python3 -m operating.pg set-embedding``) is not migration drift.
--
-- Permission columns: every searchable row carries audience_key (the same audience unit
-- the projection / capture gate uses for the source item) and markings. Queries filter
-- ``audience_key = ANY($allowed) AND markings <@ $held`` inside SQL before ranking
-- (operating/pg/permissions.py). A row that cannot be classified is refused at write time
-- by the CHECK constraints below, so it can never be served unfiltered.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

CREATE SCHEMA IF NOT EXISTS search;

-- The embedding model the vector columns are sized for (one row).
CREATE TABLE IF NOT EXISTS search.embedding_config (
    singleton  boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    model      text NOT NULL CHECK (model <> ''),
    dim        integer NOT NULL CHECK (dim BETWEEN 1 AND 4000),
    changed_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO search.embedding_config (model, dim)
    VALUES ({{embed_model}}, {{embed_dim}}) ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS search.chunks (
    chunk_id     text PRIMARY KEY CHECK (chunk_id <> ''),
    source_handle text NOT NULL CHECK (source_handle <> ''),
    object_id    text NULL,
    kind         text NOT NULL CHECK (kind <> ''),
    title        text,
    author_role  text,
    occurred_at  timestamptz NULL,
    header       text NOT NULL,
    body         text NOT NULL,
    content_sha  text NOT NULL CHECK (content_sha ~ '^[0-9a-f]{64}$'),
    linked_ids   text[] NOT NULL DEFAULT '{}',
    audience_key text NOT NULL CHECK (audience_key ~ '^[a-z][a-z0-9-]*:.+$'
                                      AND position('*' in audience_key) = 0),
    markings     text[] NOT NULL DEFAULT '{}' CHECK (
        markings <@ ARRAY['finance', 'l5', 'pii', 'purchasing_prices',
                          'quality_controlled']::text[]
        AND array_position(markings, NULL) IS NULL),
    dup_group    text NULL,
    authority    smallint NOT NULL CHECK (authority BETWEEN 1 AND 4),
    tsv          tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, header), 'A')
        || setweight(to_tsvector('english'::regconfig, body), 'D')
        || setweight(to_tsvector('simple'::regconfig, header), 'A')
        || setweight(to_tsvector('simple'::regconfig, body), 'D')) STORED,
    embedding    halfvec({{embed_dim}}) NULL,
    embed_model  text NULL,
    updated_at   timestamptz NOT NULL,
    CONSTRAINT chunks_embedding_has_model CHECK ((embedding IS NULL) = (embed_model IS NULL))
);

CREATE INDEX IF NOT EXISTS chunks_tsv_gin ON search.chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON search.chunks
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS chunks_audience_key ON search.chunks (audience_key);
CREATE INDEX IF NOT EXISTS chunks_kind ON search.chunks (kind);
CREATE INDEX IF NOT EXISTS chunks_object_id ON search.chunks (object_id);
CREATE INDEX IF NOT EXISTS chunks_content_sha ON search.chunks (content_sha);
CREATE INDEX IF NOT EXISTS chunks_linked_ids_gin ON search.chunks USING gin (linked_ids);

CREATE TABLE IF NOT EXISTS search.entities (
    entity_id      text PRIMARY KEY CHECK (entity_id <> ''),
    object_id      text,
    type           text,
    name           text,
    alias          text[] NOT NULL DEFAULT '{}',
    normalized     text,
    audience_key   text NOT NULL CHECK (audience_key ~ '^[a-z][a-z0-9-]*:.+$'
                                        AND position('*' in audience_key) = 0),
    markings       text[] NOT NULL DEFAULT '{}' CHECK (
        markings <@ ARRAY['finance', 'l5', 'pii', 'purchasing_prices',
                          'quality_controlled']::text[]
        AND array_position(markings, NULL) IS NULL),
    name_embedding halfvec({{embed_dim}}) NULL
);

CREATE INDEX IF NOT EXISTS entities_normalized_trgm ON search.entities
    USING gin (normalized gin_trgm_ops);
CREATE INDEX IF NOT EXISTS entities_name_embedding_hnsw ON search.entities
    USING hnsw (name_embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS entities_object_id ON search.entities (object_id);
CREATE INDEX IF NOT EXISTS entities_audience_key ON search.entities (audience_key);

CREATE TABLE IF NOT EXISTS search.query_log (
    id       bigserial PRIMARY KEY,
    at       timestamptz NOT NULL DEFAULT now(),
    actor    text,
    question text,
    intent   text,
    entities jsonb,
    rewrites jsonb,
    results  jsonb,
    timings  jsonb
);
CREATE INDEX IF NOT EXISTS query_log_at ON search.query_log (at);

CREATE TABLE IF NOT EXISTS search.feedback (
    id        bigserial PRIMARY KEY,
    at        timestamptz NOT NULL DEFAULT now(),
    actor     text,
    question  text,
    answer_id text,
    rating    smallint,
    note      text
);
CREATE INDEX IF NOT EXISTS feedback_answer_id ON search.feedback (answer_id);
