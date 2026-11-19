-- 0041_business_views: schema biz, the curated business views (one row per real-world
-- thing over the ontology: customers, sales orders and invoices, open receivables,
-- suppliers, purchase orders, products and stock, ...) read by ``plx.business``.
-- The view tables themselves are NOT created here: ``python3 -m operating.pg.business_views
-- build`` builds each one in Python (the status, value and late rules are the ontology's
-- own Python rules) as biz.<view>_next and swaps it in with one transaction (old renamed
-- away, _next renamed live, old dropped), so a reader sees the old table or the new one,
-- never a partial one. Door v2: every row carries access_class / col_class (ids of its
-- audience-key + record-mark pattern and its column-mark pattern) and ai_ok; lineage
-- (object ids, cites, systems, copies) is a side table by (view, handle).
-- * biz.state: one row per built view: when it was built, the served content digest it
--   was built from, its row count and the build's notes (checks, counts of copies).
-- Written only by the build; no step reads it; no objects, value or link row changes:
-- no digest moves. Undo: DROP SCHEMA biz CASCADE.
CREATE SCHEMA IF NOT EXISTS biz;
CREATE TABLE IF NOT EXISTS biz.state (
    view        text PRIMARY KEY,
    built_at    text NOT NULL,
    digest      text,
    rows        bigint NOT NULL,
    seconds     double precision,
    notes       jsonb NOT NULL DEFAULT '{}'::jsonb);
-- The canonical classes contract (operating/pg/business_views_access.py docstring; every
-- builder and reader of a biz-shaped schema uses these shapes):
-- * biz.access_classes: one row per (audience keys, record marking sets, document marking
--   tokens, blocked) pattern of any view's rows, unique on all four; ids append-only
--   (a built table keeps its ids, a reader's held list stays right). A reader holds a
--   class when not blocked and it holds every key, marking set and document token.
CREATE TABLE IF NOT EXISTS biz.access_classes (
    id            integer PRIMARY KEY,
    audience_keys text[] NOT NULL,
    rec_marks     text[] NOT NULL,
    doc_marks     text[] NOT NULL DEFAULT '{}',
    blocked       boolean NOT NULL DEFAULT false,
    UNIQUE (audience_keys, rec_marks, doc_marks, blocked));
-- * biz.col_classes: one row per column pattern, shared by every view, ids append-only,
--   id 0 nothing marked. marks: {column: marking set} of the cell; needs: {column:
--   {audience_keys, rec_marks, doc_marks, blocked}} a reader must also hold to read that
--   column (a value read from another record than the row's: a mailed promise date).
CREATE TABLE IF NOT EXISTS biz.col_classes (
    id     integer PRIMARY KEY,
    marks  jsonb NOT NULL,
    needs  jsonb NOT NULL DEFAULT '{}',
    UNIQUE (marks, needs));
INSERT INTO biz.col_classes (id, marks, needs) VALUES (0, '{}', '{}') ON CONFLICT DO NOTHING;
-- * biz.lineage: per view row (view, handle): object_ids (its own records), read_ids
--   (every other record a value was read from), col_objects ({column: [ids]} read only for
--   that column), per-column cites ([handle, field_path]), systems and copies. A reader
--   who may not open one of them does not read the row (the column). Replaced with its
--   view's swap.
CREATE TABLE IF NOT EXISTS biz.lineage (
    view        text NOT NULL,
    handle      text NOT NULL,
    object_ids  text[] NOT NULL,
    read_ids    text[] NOT NULL DEFAULT '{}',
    col_objects jsonb NOT NULL DEFAULT '{}',
    cites       jsonb NOT NULL DEFAULT '{}',
    systems     text[] NOT NULL DEFAULT '{}',
    copies      integer NOT NULL DEFAULT 0,
    PRIMARY KEY (view, handle));
CREATE INDEX IF NOT EXISTS lineage_object_ids ON biz.lineage USING gin (object_ids);
CREATE INDEX IF NOT EXISTS lineage_read_ids ON biz.lineage USING gin (read_ids);
-- * biz.class_counts: rows per (view, access class, col class), written with the view's
--   swap (a reader's withheld counts without a scan).
CREATE TABLE IF NOT EXISTS biz.class_counts (
    view         text NOT NULL,
    access_class integer NOT NULL,
    col_class    integer NOT NULL,
    rows         bigint NOT NULL,
    PRIMARY KEY (view, access_class, col_class));

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA biz TO plx_api;
        GRANT SELECT ON ALL TABLES IN SCHEMA biz TO plx_api;
        ALTER DEFAULT PRIVILEGES IN SCHEMA biz GRANT SELECT ON TABLES TO plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        GRANT USAGE ON SCHEMA biz TO plx_eval;
        GRANT SELECT ON ALL TABLES IN SCHEMA biz TO plx_eval;
        ALTER DEFAULT PRIVILEGES IN SCHEMA biz GRANT SELECT ON TABLES TO plx_eval;
    END IF;
END
$$;
