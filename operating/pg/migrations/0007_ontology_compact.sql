-- 0007_ontology_compact: the whole generation in Postgres within the disk the host has.
-- 0006 kept values and links as flat text rows: gen 11's 497 M values at ~350 bytes of text
-- each and 24 M links at ~1.5 KB each would need ~230 GB before indexes. Here they are
-- stored as the SQLite store stores them (ontology/store_ddl.py v10): a value names its
-- source (provenance shared by every value read from one version), its field
-- (property, field path) and its kind (type, unit, parser, markings ...) by number, and a
-- link names its reason and evidence by skeleton and slot texts, each stored once. The
-- views ``object_values`` and ``links`` give back the logical rows, column for column and
-- byte for byte, as the SQLite views do. A link written by a delta layer may carry its
-- texts plain (``reason`` / ``evidence_handles``) instead.
-- The document text index (text_fields, text_chunks: zlib bodies) is loaded as stored.
-- ``load_ranges`` records each key range a load has copied, in the same transaction as
-- its rows, so an interrupted load resumes where it stopped.
-- The flat tables of 0006 that held no rows yet (object_values, links, object_versions)
-- are replaced.

DROP TABLE IF EXISTS ontology.object_values;
DROP TABLE IF EXISTS ontology.links;
DROP TABLE IF EXISTS ontology.object_versions;

CREATE TABLE ontology.load_ranges (
    table_name text NOT NULL,
    low text NOT NULL,              -- the range's first key ('' = from the start)
    high text NOT NULL,             -- the next range's first key ('' = to the end)
    rows bigint NOT NULL,
    loaded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_name, low, high));

-- ``ver``: the version's row number in the generation it was loaded from (a delta's
-- versions take numbers past every loaded one, from the sequence).
CREATE TABLE ontology.object_versions (
    ver bigint PRIMARY KEY,
    object_id text NOT NULL,
    l1_handle text NOT NULL,
    digest text NOT NULL,
    lineage text NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text);
CREATE INDEX object_versions_by_object ON ontology.object_versions (object_id, recorded_at);
CREATE INDEX object_versions_by_handle ON ontology.object_versions (l1_handle);
CREATE SEQUENCE ontology.object_versions_ver;

CREATE TABLE ontology.value_fields (
    field integer PRIMARY KEY,
    property text NOT NULL,
    field_path text NOT NULL,
    UNIQUE (property, field_path));
CREATE SEQUENCE ontology.value_fields_field;

CREATE TABLE ontology.value_kinds (
    kind integer PRIMARY KEY,
    value_type text NOT NULL,
    unit text,
    quantity_role text,
    tax_basis text,
    parser text NOT NULL,
    authority_rank integer,
    markings text NOT NULL,
    UNIQUE NULLS NOT DISTINCT (value_type, unit, quantity_role, tax_basis, parser,
                               authority_rank, markings));
CREATE SEQUENCE ontology.value_kinds_kind;

-- digest / lineage: NULL where they are the version's (the view takes the version's).
CREATE TABLE ontology.value_sources (
    src bigint PRIMARY KEY,
    ver bigint NOT NULL,
    binding_id text NOT NULL,
    digest text,
    lineage text,
    recorded_at text NOT NULL,
    superseded_at text);
CREATE INDEX value_sources_by_version ON ontology.value_sources (ver);
CREATE SEQUENCE ontology.value_sources_src;

-- One row per value (a packed record of the SQLite store unpacked into its values).
-- value_num: the value as a number when its kind is numeric (integer, decimal, money,
-- quantity), for range filters and sums.
CREATE TABLE ontology.value_cells (
    src bigint NOT NULL,
    field integer NOT NULL,
    kind integer NOT NULL,
    value text,
    value_num numeric,
    currency text,
    currency_citation text,
    PRIMARY KEY (src, field));
CREATE INDEX value_cells_num ON ontology.value_cells (field, value_num)
    WHERE value_num IS NOT NULL;
CREATE INDEX value_cells_text ON ontology.value_cells (field, value)
    WHERE value_num IS NULL AND length(value) <= 200;

CREATE VIEW ontology.object_values AS SELECT
    v.object_id, f.property, k.value_type, c.value, k.unit, k.quantity_role, c.currency,
    k.tax_basis, c.currency_citation, v.l1_handle, f.field_path,
    coalesce(s.digest, v.digest) AS digest, coalesce(s.lineage, v.lineage) AS lineage,
    s.binding_id, k.parser, k.authority_rank, k.markings, s.recorded_at, s.superseded_at,
    c.value_num,
    CASE WHEN k.value_type IN ('date', 'timestamp') THEN c.value END AS value_time,
    c.src, c.field, c.kind, s.ver
  FROM ontology.value_cells c
  JOIN ontology.value_sources s ON s.src = c.src
  JOIN ontology.object_versions v ON v.ver = s.ver
  JOIN ontology.value_fields f ON f.field = c.field
  JOIN ontology.value_kinds k ON k.kind = c.kind;

CREATE TABLE ontology.link_texts (
    id bigint PRIMARY KEY,
    text text NOT NULL);
CREATE SEQUENCE ontology.link_texts_id;
CREATE TABLE ontology.link_skeletons (
    id bigint PRIMARY KEY,
    text text NOT NULL);
CREATE SEQUENCE ontology.link_skeletons_id;

-- slots: the slot text ids in order (the evidence's first ``ek``); reason_id /
-- evidence_id NULL: the text is stored plain in reason / evidence_handles.
CREATE TABLE ontology.link_rows (
    link_id text PRIMARY KEY,
    link_type text NOT NULL,
    from_object text NOT NULL,
    to_object text NOT NULL,
    tier text NOT NULL,
    binding_kind text NOT NULL,
    link_contract text,
    reason_id bigint,
    why_not text NOT NULL,
    evidence_id bigint,
    reviewed_by text,
    reviewed_at text,
    review_action_id text,
    ek integer NOT NULL DEFAULT 0,
    slots bigint[] NOT NULL DEFAULT '{}',
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text,
    reason text,
    evidence_handles text,
    CHECK ((reason_id IS NULL) = (reason IS NOT NULL)),
    CHECK ((evidence_id IS NULL) = (evidence_handles IS NOT NULL)));
CREATE INDEX link_rows_by_type ON ontology.link_rows (link_type, tier);
CREATE INDEX link_rows_from ON ontology.link_rows (from_object, link_type);
CREATE INDEX link_rows_to ON ontology.link_rows (to_object, link_type);

-- A packed text: its skeleton formatted with the slot texts ``slots[lo:hi]`` (the SQLite
-- view's format(): every '%' of the text doubled in the skeleton, '%s' per slot).
CREATE FUNCTION ontology.link_text(skeleton bigint, slots bigint[], lo integer, hi integer)
RETURNS text LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT format(k.text, VARIADIC coalesce(
        (SELECT array_agg(t.text ORDER BY u.n)
           FROM unnest(slots[lo:hi]) WITH ORDINALITY AS u(id, n)
           JOIN ontology.link_texts t ON t.id = u.id), '{}'::text[]))
      FROM ontology.link_skeletons k WHERE k.id = skeleton
$$;

CREATE VIEW ontology.links AS SELECT
    l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.binding_kind,
    l.link_contract,
    coalesce(l.reason, ontology.link_text(l.reason_id, l.slots, l.ek + 1, 64)) AS reason,
    l.why_not,
    coalesce(l.evidence_handles, ontology.link_text(l.evidence_id, l.slots, 1, l.ek))
        AS evidence_handles,
    l.reviewed_by, l.reviewed_at, l.review_action_id, l.markings, l.recorded_at,
    l.superseded_at
  FROM ontology.link_rows l;

-- The document text no value carries (ontology/text_index.py): fields and their chunks
-- (body: zlib-compressed UTF-8, as the SQLite store keeps it).
CREATE TABLE ontology.text_fields (
    field_id bigint PRIMARY KEY,
    object_id text NOT NULL,
    version_handle text NOT NULL,
    citation text NOT NULL,
    field_path text NOT NULL,
    document_kind text NOT NULL,
    sender_class text NOT NULL,
    markings text NOT NULL,
    text_digest text NOT NULL);
CREATE INDEX text_fields_by_citation ON ontology.text_fields (citation, field_path);
CREATE INDEX text_fields_by_object ON ontology.text_fields (object_id);
CREATE SEQUENCE ontology.text_fields_id;

CREATE TABLE ontology.text_chunks (
    chunk_id bigint PRIMARY KEY,
    field_id bigint NOT NULL,
    start integer NOT NULL,
    body bytea NOT NULL);
CREATE INDEX text_chunks_by_field ON ontology.text_chunks (field_id, start);
CREATE SEQUENCE ontology.text_chunks_id;
