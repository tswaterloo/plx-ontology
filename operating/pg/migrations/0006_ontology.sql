-- 0006_ontology: the ontology in Postgres, beside the SQLite generations (the architecture
-- audit's phase 1). One row per logical row of a generation store (ontology/store_ddl.py):
-- the same columns and the same texts, so a plx.object read from either store is equal;
-- the SQLite store's packed layouts (value_cells, link_texts) are unpacked into plain rows.
-- Typed columns are added beside the texts for range queries and aggregates
-- (values.value_num, value_time), and every column a question filters or groups on is
-- indexed: object type, audience, a property's value, a link's type and ends.
-- Times stay ISO text as the store writes them (fixed width UTC, so text order is time
-- order) and markings the store's canonical JSON text, for byte-equal reads.

CREATE SCHEMA IF NOT EXISTS ontology;

-- Which generation the tables hold, and how far a row-delta projector has applied.
CREATE TABLE IF NOT EXISTS ontology.load_state (
    name text PRIMARY KEY,
    value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now());

CREATE TABLE IF NOT EXISTS ontology.store_meta (
    key text PRIMARY KEY,
    value text NOT NULL);

CREATE TABLE IF NOT EXISTS ontology.objects (
    object_id text PRIMARY KEY,
    object_type text NOT NULL,
    binding_id text NOT NULL,
    native_key text NOT NULL,
    audience_key text NOT NULL,
    first_recorded_at text NOT NULL,
    missing_from_source_since text);
CREATE INDEX IF NOT EXISTS objects_by_type ON ontology.objects (object_type);
CREATE INDEX IF NOT EXISTS objects_by_audience ON ontology.objects (audience_key);
CREATE INDEX IF NOT EXISTS objects_by_binding ON ontology.objects (binding_id);

CREATE TABLE IF NOT EXISTS ontology.object_removals (
    object_id text NOT NULL,
    basis text NOT NULL,
    since text NOT NULL,
    until text,
    replaced_basis text,
    replaced_by text,
    PRIMARY KEY (object_id, since, basis));

CREATE TABLE IF NOT EXISTS ontology.object_versions (
    object_id text NOT NULL,
    l1_handle text NOT NULL,
    digest text NOT NULL,
    lineage text NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text,
    PRIMARY KEY (object_id, recorded_at));
CREATE INDEX IF NOT EXISTS object_versions_by_handle ON ontology.object_versions (l1_handle);

-- The logical rows of the SQLite ``object_values`` view.
CREATE TABLE IF NOT EXISTS ontology.object_values (
    object_id text NOT NULL,
    property text NOT NULL,
    value_type text NOT NULL,
    value text,
    unit text,
    quantity_role text,
    currency text,
    tax_basis text,
    currency_citation text,
    l1_handle text NOT NULL,
    field_path text NOT NULL,
    digest text,
    lineage text,
    binding_id text NOT NULL,
    parser text NOT NULL,
    authority_rank integer,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text,
    value_num numeric,          -- value as a number (integer, decimal, money, quantity)
    value_time text,            -- value as a date or instant (date, timestamp): ISO text
    PRIMARY KEY (object_id, property, l1_handle, field_path));
CREATE INDEX IF NOT EXISTS object_values_num ON ontology.object_values (property, value_num)
    WHERE value_num IS NOT NULL AND superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS object_values_text ON ontology.object_values (property, value)
    WHERE value_num IS NULL AND superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS object_values_time ON ontology.object_values (property, value_time)
    WHERE value_time IS NOT NULL AND superseded_at IS NULL;

-- The logical rows of the SQLite ``links`` view (texts unpacked).
CREATE TABLE IF NOT EXISTS ontology.links (
    link_id text PRIMARY KEY,
    link_type text NOT NULL,
    from_object text NOT NULL,
    to_object text NOT NULL,
    tier text NOT NULL,
    binding_kind text NOT NULL,
    link_contract text,
    reason text NOT NULL,
    why_not text NOT NULL,
    evidence_handles text NOT NULL,
    reviewed_by text,
    reviewed_at text,
    review_action_id text,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text);
CREATE INDEX IF NOT EXISTS links_by_type ON ontology.links (link_type, tier);
CREATE INDEX IF NOT EXISTS links_from ON ontology.links (from_object, link_type);
CREATE INDEX IF NOT EXISTS links_to ON ontology.links (to_object, link_type);

CREATE TABLE IF NOT EXISTS ontology.identifiers (
    identifier_kind text NOT NULL,
    normalized text NOT NULL,
    raw text NOT NULL,
    role text NOT NULL,
    object_id text NOT NULL,
    property text NOT NULL,
    l1_handle text NOT NULL,
    field_path text NOT NULL,
    lineage text NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text,
    PRIMARY KEY (identifier_kind, normalized, object_id, l1_handle, field_path));
CREATE INDEX IF NOT EXISTS identifiers_by_value ON ontology.identifiers (normalized text_pattern_ops);
CREATE INDEX IF NOT EXISTS identifiers_by_object ON ontology.identifiers (object_id);

CREATE TABLE IF NOT EXISTS ontology.events (
    event_id text PRIMARY KEY,
    event_type text NOT NULL,
    time_basis text NOT NULL,
    time text,
    window_start text,
    window_end text,
    l1_handle text NOT NULL,
    field_path text NOT NULL,
    digest text NOT NULL,
    lineage text NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text);
CREATE INDEX IF NOT EXISTS events_by_type ON ontology.events (event_type, time);

CREATE TABLE IF NOT EXISTS ontology.event_objects (
    event_id text NOT NULL,
    object_id text NOT NULL,
    qualifier text NOT NULL,
    PRIMARY KEY (event_id, object_id, qualifier));
CREATE INDEX IF NOT EXISTS event_objects_by_object ON ontology.event_objects (object_id);
