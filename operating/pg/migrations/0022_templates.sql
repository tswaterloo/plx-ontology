-- 0022_templates: each person's saved document templates (operator_api/templates.py).
--
-- templates.template: a template one person saved (``owner``: their principals key,
-- operator_api.auth.Reader.key): a name, a document type, a body whose placeholders are
-- bound to record fields (filled from the records, with citations, when used), style
-- notes, and who it is shared with (``shared_with``: ``role:<role>`` and
-- ``person:<address>`` entries; empty: private). Only the owner edits or deletes it; a
-- person it is shared with (by address, or through a role) reads and uses it.
-- Pay and finance text is refused before a row is written; the people exclusion is
-- checked before a write and again on every read (a template whose owner or text it keeps
-- out is never shown). Deleting sets ``deleted_at``; the exclusion purge removes rows.

CREATE SCHEMA IF NOT EXISTS templates;

CREATE TABLE IF NOT EXISTS templates.template (
    id            bigserial PRIMARY KEY,
    owner         text NOT NULL CHECK (owner <> ''),
    name          text NOT NULL CHECK (name <> '' AND length(name) <= 120),
    doc_type      text NOT NULL CHECK (doc_type ~ '^[a-z_]{1,40}$'),
    body          text NOT NULL CHECK (body <> '' AND length(body) <= 8000),
    placeholders  jsonb NOT NULL DEFAULT '[]',
    style_notes   text NOT NULL DEFAULT '' CHECK (length(style_notes) <= 2000),
    shared_with   text[] NOT NULL DEFAULT '{}' CHECK (cardinality(shared_with) <= 50),
    source_ref    text NULL CHECK (source_ref IS NULL OR length(source_ref) <= 400),
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    uses          integer NOT NULL DEFAULT 0 CHECK (uses >= 0),
    last_used_at  timestamptz NULL,
    deleted_at    timestamptz NULL
);
CREATE INDEX IF NOT EXISTS template_owner ON templates.template (owner, doc_type, updated_at DESC)
    WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS template_shared ON templates.template USING gin (shared_with)
    WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS template_name ON templates.template (owner, lower(name))
    WHERE deleted_at IS NULL;
