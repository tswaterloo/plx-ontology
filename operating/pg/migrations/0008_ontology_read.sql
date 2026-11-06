-- 0008_ontology_read: what the read tools need to run their store queries on the Postgres
-- ontology (operating/pg/sqlite_compat.py translates their SQLite SQL to these).
-- * object_values and links give exactly the SQLite views' columns (a tool selecting
--   v.* sees the same row); object_values_typed keeps the typed and numbering columns.
-- * sq_* functions behave as the SQLite built-ins and the Python functions the tools
--   register: julianday, json_each, json_extract, glob, plx_l9_time, plx_l9_gated.
-- * sq_call: a function the tools register in Python (an item gate) evaluated in rounds:
--   a call whose answer this session does not hold yet is queued and answered NULL; the
--   connection answers the queue in Python and runs the statement again
--   (sqlite_compat.PgCompatConnection), so the final rows are the ones the function's
--   real answers give.
-- * sq_data_version: changes whenever a generation or a delta layer is loaded.

DROP VIEW IF EXISTS ontology.object_values;
CREATE VIEW ontology.object_values_typed AS SELECT
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
CREATE VIEW ontology.object_values AS SELECT
    object_id, property, value_type, value, unit, quantity_role, currency, tax_basis,
    currency_citation, l1_handle, field_path, digest, lineage, binding_id, parser,
    authority_rank, markings, recorded_at, superseded_at
  FROM ontology.object_values_typed;

CREATE FUNCTION ontology.sq_julianday(t text) RETURNS double precision
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN t ~ '^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:\d{2})?$'
        THEN round((extract(epoch FROM (CASE WHEN t ~ '^\d{4}-\d{2}-\d{2}$' THEN t || 'T00:00:00Z'
                                             WHEN t ~ '(Z|[+-]\d{2}:\d{2})$' THEN t
                                             ELSE t || 'Z' END)::timestamptz) * 1000))
             / 86400000.0 + 2440587.5 END
$$;

-- An ISO date or timestamp as fixed-width UTC text at millisecond resolution
-- (operating/ontology_tools_traverse.py _store_time); NULL when unreadable.
CREATE FUNCTION ontology.sq_l9_time(t text) RETURNS text
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    SELECT CASE
        WHEN t ~ '^\d{4}-\d{2}-\d{2}$' THEN t || 'T00:00:00.000000Z'
        WHEN t ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}(:?\d{2})?)?$'
        THEN to_char(to_timestamp(round(extract(epoch FROM (CASE
                 WHEN t ~ '(Z|[+-]\d{2}(:?\d{2})?)$' THEN t ELSE t || 'Z' END)::timestamptz)
                 * 1000) / 1000.0) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
        END
$$;

-- The handles of a JSON handle list whose scheme is gated, as a sorted JSON list; NULL
-- when there are none or the text is no JSON list.
CREATE FUNCTION ontology.sq_l9_gated(schemes text[], t text) RETURNS text
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE AS $$
DECLARE
    doc jsonb;
    out text;
BEGIN
    IF t IS NULL THEN
        RETURN NULL;
    END IF;
    BEGIN
        doc := t::jsonb;
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END;
    IF jsonb_typeof(doc) <> 'array' THEN
        RETURN NULL;
    END IF;
    SELECT '[' || string_agg(to_json(h)::text, ', ' ORDER BY h COLLATE "C") || ']' INTO out
      FROM (SELECT DISTINCT e #>> '{}' AS h FROM jsonb_array_elements(doc) e
             WHERE jsonb_typeof(e) = 'string'
               AND split_part(e #>> '{}', '://', 1) = ANY (schemes)
               AND strpos(e #>> '{}', '://') > 0) g;
    RETURN out;
END
$$;

-- json_each over a JSON array (key: the index) or object; value as SQLite gives it: a
-- string's text, a number's or nested value's JSON text, NULL for null.
CREATE FUNCTION ontology.sq_json_each(t text)
RETURNS TABLE (key text, value text, type text)
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT (n - 1)::text, CASE jsonb_typeof(e) WHEN 'string' THEN e #>> '{}'
                                               WHEN 'null' THEN NULL ELSE e::text END,
           jsonb_typeof(e)
      FROM jsonb_array_elements(CASE WHEN jsonb_typeof(t::jsonb) = 'array' THEN t::jsonb
                                     ELSE '[]'::jsonb END) WITH ORDINALITY AS a(e, n)
    UNION ALL
    SELECT k, CASE jsonb_typeof(e) WHEN 'string' THEN e #>> '{}'
                                   WHEN 'null' THEN NULL ELSE e::text END, jsonb_typeof(e)
      FROM jsonb_each(CASE WHEN jsonb_typeof(t::jsonb) = 'object' THEN t::jsonb
                           ELSE '{}'::jsonb END) AS o(k, e)
$$;
-- The same, value as a number (a list of row numbers).
CREATE FUNCTION ontology.sq_json_each_int(t text)
RETURNS TABLE (key text, value bigint)
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT (n - 1)::text, (e #>> '{}')::bigint
      FROM jsonb_array_elements(t::jsonb) WITH ORDINALITY AS a(e, n)
$$;

CREATE FUNCTION ontology.sq_json_extract(t text, path text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE jsonb_typeof(j) WHEN 'string' THEN j #>> '{}' WHEN 'null' THEN NULL
                                ELSE j::text END
      FROM (SELECT jsonb_path_query_first(t::jsonb, path::jsonpath) AS j) x
$$;

CREATE FUNCTION ontology.sq_json_array_length(t text) RETURNS integer
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN jsonb_typeof(t::jsonb) = 'array' THEN jsonb_array_length(t::jsonb) END
$$;

-- A GLOB pattern as an anchored regular expression ('*', '?', '[...]').
CREATE FUNCTION ontology.sq_glob_re(pattern text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT '^' || replace(replace(regexp_replace(regexp_replace(pattern,
        '([.+(){}|^$\\])', '\\\1', 'g'), '\[\^', '[^', 'g'), '*', '.*'), '?', '.') || '$'
$$;

CREATE FUNCTION ontology.sq_data_version() RETURNS bigint
LANGUAGE sql STABLE AS $$
    SELECT coalesce((extract(epoch FROM max(updated_at)) * 1000)::bigint, 0)
      FROM ontology.load_state
$$;

CREATE FUNCTION ontology.sq_call(fn text, arg text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    answer boolean;
BEGIN
    IF arg IS NULL THEN
        RETURN false;   -- the tools' gates deny what is no handle
    END IF;
    SELECT c.result INTO answer FROM pg_temp.sq_calls c WHERE c.fn = sq_call.fn
       AND c.arg = sq_call.arg;
    IF FOUND THEN
        RETURN answer;
    END IF;
    INSERT INTO pg_temp.sq_pending (fn, arg) VALUES (sq_call.fn, sq_call.arg)
        ON CONFLICT DO NOTHING;
    RETURN NULL;
END
$$;

-- The SQLite rowid of objects and links (the proactive engine reads them in rowid
-- ranges): the generation's at a load, the next number for a row a delta writes. Added
-- without a default first, so the rows already loaded are not rewritten.
ALTER TABLE ontology.objects ADD COLUMN row_no bigint;
CREATE SEQUENCE ontology.objects_row_no;
ALTER TABLE ontology.objects ALTER COLUMN row_no SET DEFAULT nextval('ontology.objects_row_no');
CREATE INDEX objects_by_row_no ON ontology.objects (row_no);
ALTER TABLE ontology.link_rows ADD COLUMN row_no bigint;
CREATE SEQUENCE ontology.link_rows_row_no;
ALTER TABLE ontology.link_rows ALTER COLUMN row_no SET DEFAULT nextval('ontology.link_rows_row_no');
CREATE INDEX link_rows_by_row_no ON ontology.link_rows (row_no);
