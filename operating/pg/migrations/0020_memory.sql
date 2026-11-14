-- 0020_memory: each person's memory (operator_api/memory.py), three tiers.
--
-- memory.log: the full log, append-only: every question, answer, tool call, citation,
-- clarify choice, draft and edit, decision, action and rating, per person (``reader``:
-- the person's principals key, operator_api.auth.Reader.key). Never updated; deleted
-- only by the people-exclusion purge (``plx.memory_purge`` set in its transaction).
--
-- memory.item: what is distilled from the log: facts, preferences, standing
-- instructions and open commitments (core memory: the most important x recent of them,
-- under a fixed size, are in every turn's context) and the running summary of each
-- conversation (kind summary, subject ``conversation:<id>``). Each names the log rows it
-- came from (``log_ids``). An item about the same ``subject`` supersedes the one before;
-- a person deletes their own (``deleted_at``).
--
-- Every read is ``reader = $person``: memory is never shared across people. Pay and
-- finance text is refused before a row is written; the people exclusion is checked
-- before a write and again on every read.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE SCHEMA IF NOT EXISTS memory;

CREATE TABLE IF NOT EXISTS memory.log (
    id            bigserial PRIMARY KEY,
    reader        text NOT NULL CHECK (reader <> ''),
    conversation  text NULL,
    at            timestamptz NOT NULL DEFAULT now(),
    kind          text NOT NULL CHECK (kind IN ('question', 'answer', 'tool_call', 'clarify',
                      'draft', 'edit', 'decision', 'action', 'feedback', 'note')),
    source_ref    text NULL,
    text          text NOT NULL CHECK (text <> '' AND length(text) <= 8000),
    records       text[] NOT NULL DEFAULT '{}',
    citations     jsonb NOT NULL DEFAULT '[]',
    people        text[] NOT NULL DEFAULT '{}',
    tsv           tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, text), 'B')
        || setweight(to_tsvector('simple'::regconfig, text), 'C')) STORED,
    embedding     vector NULL,    -- embeddings-ready: filled by a later pass
    embed_model   text NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS log_source
    ON memory.log (reader, kind, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS log_reader_at ON memory.log (reader, at DESC);
CREATE INDEX IF NOT EXISTS log_conversation ON memory.log (reader, conversation, id)
    WHERE conversation IS NOT NULL;
CREATE INDEX IF NOT EXISTS log_tsv ON memory.log USING gin (tsv);
CREATE INDEX IF NOT EXISTS log_trgm ON memory.log USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS log_records ON memory.log USING gin (records);

CREATE OR REPLACE FUNCTION memory.log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('plx.memory_purge', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'memory_log_is_append_only';
END $$;
DROP TRIGGER IF EXISTS log_append_only ON memory.log;
CREATE TRIGGER log_append_only BEFORE UPDATE OR DELETE ON memory.log
    FOR EACH ROW EXECUTE FUNCTION memory.log_append_only();

CREATE TABLE IF NOT EXISTS memory.item (
    id             bigserial PRIMARY KEY,
    reader         text NOT NULL CHECK (reader <> ''),
    kind           text NOT NULL CHECK (kind IN ('fact', 'preference', 'instruction',
                       'commitment', 'summary')),
    source         text NOT NULL CHECK (source ~ '^[a-z_]{1,40}$'),
    source_ref     text NULL,
    conversation   text NULL,
    at             timestamptz NOT NULL DEFAULT now(),
    subject        text NULL,
    text           text NOT NULL CHECK (text <> '' AND length(text) <= 8000),
    records        text[] NOT NULL DEFAULT '{}',
    citations      jsonb NOT NULL DEFAULT '[]',
    people         text[] NOT NULL DEFAULT '{}',
    log_ids        bigint[] NOT NULL DEFAULT '{}',
    importance     real NOT NULL DEFAULT 0.5 CHECK (importance > 0 AND importance <= 1),
    confidence     real NOT NULL DEFAULT 0.7 CHECK (confidence > 0 AND confidence <= 1),
    superseded_by  bigint NULL,
    deleted_at     timestamptz NULL,
    tsv            tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, coalesce(subject, '')), 'A')
        || setweight(to_tsvector('english'::regconfig, text), 'B')
        || setweight(to_tsvector('simple'::regconfig, text), 'C')) STORED,
    embedding      vector NULL,
    embed_model    text NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS item_source
    ON memory.item (reader, kind, source, source_ref) WHERE source_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS item_active ON memory.item (reader, kind, importance DESC, at DESC)
    WHERE superseded_by IS NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS item_subject ON memory.item (reader, kind, subject)
    WHERE subject IS NOT NULL AND superseded_by IS NULL AND deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS item_tsv ON memory.item USING gin (tsv);
CREATE INDEX IF NOT EXISTS item_trgm ON memory.item USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS item_records ON memory.item USING gin (records);
