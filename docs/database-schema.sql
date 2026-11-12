
\restrict ExHyexrpMeiuiFYzIz7lY7Shhh9Il9ZiMpicmvqn0LswvmauXc22Akl966ywEtX



-- Name: ontology; Type: SCHEMA; Schema: -; Owner: -

CREATE SCHEMA ontology;


-- Name: plx_meta; Type: SCHEMA; Schema: -; Owner: -

CREATE SCHEMA plx_meta;


-- Name: search; Type: SCHEMA; Schema: -; Owner: -

CREATE SCHEMA search;


-- Name: link_text(bigint, bigint[], integer, integer); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.link_text(skeleton bigint, slots bigint[], lo integer, hi integer) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $$
    SELECT format(k.text, VARIADIC coalesce(
        (SELECT array_agg(t.text ORDER BY u.n)
           FROM unnest(slots[lo:hi]) WITH ORDINALITY AS u(id, n)
           JOIN ontology.link_texts t ON t.id = u.id), '{}'::text[]))
      FROM ontology.link_skeletons k WHERE k.id = skeleton
$$;


-- Name: sq_call(text, text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_call(fn text, arg text) RETURNS boolean
    LANGUAGE plpgsql
    AS $$
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


-- Name: sq_data_version(); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_data_version() RETURNS bigint
    LANGUAGE sql STABLE
    AS $$
    SELECT coalesce((extract(epoch FROM max(updated_at)) * 1000)::bigint, 0)
      FROM ontology.load_state
$$;


-- Name: sq_glob_re(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_glob_re(pattern text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $_$
    SELECT '^' || replace(replace(regexp_replace(regexp_replace(pattern,
        '([.+(){}|^$\\])', '\\\1', 'g'), '\[\^', '[^', 'g'), '*', '.*'), '?', '.') || '$'
$_$;


-- Name: sq_json_array(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_json_array(t text) RETURNS text[]
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT coalesce(array_agg(value), '{}') FROM ontology.sq_json_each(t)
$$;


-- Name: sq_json_array_int(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_json_array_int(t text) RETURNS bigint[]
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT coalesce(array_agg(value::bigint), '{}') FROM ontology.sq_json_each(t)
$$;


-- Name: sq_json_array_length(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_json_array_length(t text) RETURNS integer
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT CASE WHEN jsonb_typeof(t::jsonb) = 'array' THEN jsonb_array_length(t::jsonb) END
$$;


-- Name: sq_json_each(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_json_each(t text) RETURNS TABLE(key text, value text, type text)
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
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


-- Name: sq_json_each_int(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_json_each_int(t text) RETURNS TABLE(key text, value bigint)
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT (n - 1)::text, (e #>> '{}')::bigint
      FROM jsonb_array_elements(t::jsonb) WITH ORDINALITY AS a(e, n)
$$;


-- Name: sq_json_extract(text, text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_json_extract(t text, path text) RETURNS text
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
    SELECT CASE jsonb_typeof(j) WHEN 'string' THEN j #>> '{}' WHEN 'null' THEN NULL
                                ELSE j::text END
      FROM (SELECT jsonb_path_query_first(t::jsonb, path::jsonpath) AS j) x
$$;


-- Name: sq_julianday(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_julianday(t text) RETURNS double precision
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $_$
    SELECT CASE WHEN t ~ '^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:\d{2})?$'
        THEN round((extract(epoch FROM (CASE WHEN t ~ '^\d{4}-\d{2}-\d{2}$' THEN t || 'T00:00:00Z'
                                             WHEN t ~ '(Z|[+-]\d{2}:\d{2})$' THEN t
                                             ELSE t || 'Z' END)::timestamptz) * 1000))
             / 86400000.0 + 2440587.5 END
$_$;


-- Name: sq_l9_gated(text[], text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_l9_gated(schemes text[], t text) RETURNS text
    LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
    AS $$
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


-- Name: sq_l9_time(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_l9_time(t text) RETURNS text
    LANGUAGE sql STABLE PARALLEL SAFE
    AS $_$
    SELECT CASE
        WHEN t ~ '^\d{4}-\d{2}-\d{2}$' THEN t || 'T00:00:00.000000Z'
        WHEN t ~ '^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}(:?\d{2})?)?$'
        THEN to_char(to_timestamp(round(extract(epoch FROM (CASE
                 WHEN t ~ '(Z|[+-]\d{2}(:?\d{2})?)$' THEN t ELSE t || 'Z' END)::timestamptz)
                 * 1000) / 1000.0) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
        END
$_$;


-- Name: sq_words(text); Type: FUNCTION; Schema: ontology; Owner: -

CREATE FUNCTION ontology.sq_words(t text) RETURNS tsvector
    LANGUAGE sql IMMUTABLE STRICT COST 1000 PARALLEL SAFE
    AS $$
    SELECT array_to_tsvector(coalesce(array_agg(DISTINCT m[1]), '{}'))
      FROM regexp_matches(lower(normalize(regexp_replace(normalize(translate(left(t, 250000),
                                                                             'µ', 'μ'), NFD),
                                  '([A-Za-z\u00c0-\u024f])[\u0300-\u036f]+', '\1', 'g'), NFC)),
                          '[[:alnum:]\u00b2\u00b3\u00b9\u00bc-\u00be\u2070-\u209f\u2150-\u218f\ue000-\uf8ff]+',
                          'g') m
     WHERE octet_length(m[1]) <= 2046
$$;


-- Name: alias_text(text[]); Type: FUNCTION; Schema: search; Owner: -

CREATE FUNCTION search.alias_text(text[]) RETURNS text
    LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
    AS $_$ SELECT array_to_string($1, ' ') $_$;


-- Name: bump_write_generation(); Type: FUNCTION; Schema: search; Owner: -

CREATE FUNCTION search.bump_write_generation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    PERFORM nextval('search.write_generation');
    RETURN NULL;
END
$$;




-- Name: applied_owned; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.applied_owned (
    seq bigint NOT NULL,
    layer text NOT NULL,
    object_id text NOT NULL
);


-- Name: applied_owned_seq_seq; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.applied_owned_seq_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: applied_owned_seq_seq; Type: SEQUENCE OWNED BY; Schema: ontology; Owner: -

ALTER SEQUENCE ontology.applied_owned_seq_seq OWNED BY ontology.applied_owned.seq;


-- Name: chunk_words; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.chunk_words (
    chunk_id bigint NOT NULL,
    words tsvector NOT NULL
);


-- Name: event_objects; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.event_objects (
    event_id text NOT NULL,
    object_id text NOT NULL,
    qualifier text NOT NULL
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_insert_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


-- Name: events; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.events (
    event_id text NOT NULL,
    event_type text NOT NULL,
    time_basis text NOT NULL,
    "time" text,
    window_start text,
    window_end text,
    l1_handle text NOT NULL,
    field_path text NOT NULL,
    digest text NOT NULL,
    lineage text NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text
);


-- Name: identifiers; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.identifiers (
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
    superseded_at text
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_insert_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


-- Name: link_rows_row_no; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.link_rows_row_no
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: link_rows; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.link_rows (
    link_id text NOT NULL,
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
    ek integer DEFAULT 0 NOT NULL,
    slots bigint[] DEFAULT '{}'::bigint[] NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text,
    reason text,
    evidence_handles text,
    row_no bigint DEFAULT nextval('ontology.link_rows_row_no'::regclass),
    CONSTRAINT link_rows_check CHECK (((reason_id IS NULL) = (reason IS NOT NULL))),
    CONSTRAINT link_rows_check1 CHECK (((evidence_id IS NULL) = (evidence_handles IS NOT NULL)))
)
WITH (autovacuum_vacuum_scale_factor='0.01', autovacuum_vacuum_insert_scale_factor='0.01', autovacuum_analyze_scale_factor='0.01', autovacuum_vacuum_cost_limit='1000');


-- Name: link_skeletons; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.link_skeletons (
    id bigint NOT NULL,
    text text NOT NULL
);


-- Name: link_skeletons_id; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.link_skeletons_id
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: link_texts; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.link_texts (
    id bigint NOT NULL,
    text text NOT NULL
);


-- Name: link_texts_id; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.link_texts_id
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: links; Type: VIEW; Schema: ontology; Owner: -

CREATE VIEW ontology.links AS
 SELECT link_id,
    link_type,
    from_object,
    to_object,
    tier,
    binding_kind,
    link_contract,
    COALESCE(reason, ontology.link_text(reason_id, slots, (ek + 1), 64)) AS reason,
    why_not,
    COALESCE(evidence_handles, ontology.link_text(evidence_id, slots, 1, ek)) AS evidence_handles,
    reviewed_by,
    reviewed_at,
    review_action_id,
    markings,
    recorded_at,
    superseded_at
   FROM ontology.link_rows l;


-- Name: load_ranges; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.load_ranges (
    table_name text NOT NULL,
    low text NOT NULL,
    high text NOT NULL,
    rows bigint NOT NULL,
    loaded_at timestamp with time zone DEFAULT now() NOT NULL
);


-- Name: load_state; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.load_state (
    name text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


-- Name: object_removals; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.object_removals (
    object_id text NOT NULL,
    basis text NOT NULL,
    since text NOT NULL,
    until text,
    replaced_basis text,
    replaced_by text
);


-- Name: object_versions; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.object_versions (
    ver bigint NOT NULL,
    object_id text NOT NULL,
    l1_handle text NOT NULL,
    digest text NOT NULL,
    lineage text NOT NULL,
    markings text NOT NULL,
    recorded_at text NOT NULL,
    superseded_at text
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_insert_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


-- Name: value_cells; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.value_cells (
    src bigint NOT NULL,
    field integer NOT NULL,
    kind integer NOT NULL,
    value text,
    value_num numeric,
    currency text,
    currency_citation text
)
WITH (autovacuum_vacuum_scale_factor='0.01', autovacuum_vacuum_insert_scale_factor='0.01', autovacuum_analyze_scale_factor='0.01', autovacuum_vacuum_cost_limit='1000');


-- Name: value_fields; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.value_fields (
    field integer NOT NULL,
    property text NOT NULL,
    field_path text NOT NULL
);


-- Name: value_kinds; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.value_kinds (
    kind integer NOT NULL,
    value_type text NOT NULL,
    unit text,
    quantity_role text,
    tax_basis text,
    parser text NOT NULL,
    authority_rank integer,
    markings text NOT NULL
);


-- Name: value_sources; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.value_sources (
    src bigint NOT NULL,
    ver bigint NOT NULL,
    binding_id text NOT NULL,
    digest text,
    lineage text,
    recorded_at text NOT NULL,
    superseded_at text
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_insert_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


-- Name: object_values_typed; Type: VIEW; Schema: ontology; Owner: -

CREATE VIEW ontology.object_values_typed AS
 SELECT v.object_id,
    f.property,
    k.value_type,
    c.value,
    k.unit,
    k.quantity_role,
    c.currency,
    k.tax_basis,
    c.currency_citation,
    v.l1_handle,
    f.field_path,
    COALESCE(s.digest, v.digest) AS digest,
    COALESCE(s.lineage, v.lineage) AS lineage,
    s.binding_id,
    k.parser,
    k.authority_rank,
    k.markings,
    s.recorded_at,
    s.superseded_at,
    c.value_num,
        CASE
            WHEN (k.value_type = ANY (ARRAY['date'::text, 'timestamp'::text])) THEN c.value
            ELSE NULL::text
        END AS value_time,
    c.src,
    c.field,
    c.kind,
    s.ver
   FROM ((((ontology.value_cells c
     JOIN ontology.value_sources s ON ((s.src = c.src)))
     JOIN ontology.object_versions v ON ((v.ver = s.ver)))
     JOIN ontology.value_fields f ON ((f.field = c.field)))
     JOIN ontology.value_kinds k ON ((k.kind = c.kind)));


-- Name: object_values; Type: VIEW; Schema: ontology; Owner: -

CREATE VIEW ontology.object_values AS
 SELECT object_id,
    property,
    value_type,
    value,
    unit,
    quantity_role,
    currency,
    tax_basis,
    currency_citation,
    l1_handle,
    field_path,
    digest,
    lineage,
    binding_id,
    parser,
    authority_rank,
    markings,
    recorded_at,
    superseded_at
   FROM ontology.object_values_typed;


-- Name: value_field_order; Type: VIEW; Schema: ontology; Owner: -

CREATE VIEW ontology.value_field_order AS
 SELECT field,
    row_number() OVER (ORDER BY (property COLLATE "C"), (field_path COLLATE "C"), field) AS ord
   FROM ontology.value_fields f;


-- Name: object_values_numbered; Type: VIEW; Schema: ontology; Owner: -

CREATE VIEW ontology.object_values_numbered AS
 SELECT ((t.src << 20) | o.ord) AS row_no,
    t.object_id,
    t.property,
    t.value_type,
    t.value,
    t.unit,
    t.quantity_role,
    t.currency,
    t.tax_basis,
    t.currency_citation,
    t.l1_handle,
    t.field_path,
    t.digest,
    t.lineage,
    t.binding_id,
    t.parser,
    t.authority_rank,
    t.markings,
    t.recorded_at,
    t.superseded_at,
    t.src,
    t.field
   FROM (ontology.object_values_typed t
     JOIN ontology.value_field_order o ON ((o.field = t.field)))
  WHERE ((t.value_num IS NULL) AND (t.value IS NOT NULL) AND (t.value_type = ANY (ARRAY['text'::text, 'identifier'::text, 'enum'::text])));


-- Name: object_versions_ver; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.object_versions_ver
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: objects_row_no; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.objects_row_no
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: objects; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.objects (
    object_id text NOT NULL,
    object_type text NOT NULL,
    binding_id text NOT NULL,
    native_key text NOT NULL,
    audience_key text NOT NULL,
    first_recorded_at text NOT NULL,
    missing_from_source_since text,
    row_no bigint DEFAULT nextval('ontology.objects_row_no'::regclass)
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_insert_scale_factor='0.02', autovacuum_analyze_scale_factor='0.02');


-- Name: packed_sources; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.packed_sources (
    src bigint NOT NULL
);


-- Name: reproject; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.reproject (
    object_id text NOT NULL,
    migration text NOT NULL,
    since timestamp with time zone DEFAULT now() NOT NULL
);


-- Name: schema_migrations; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.schema_migrations (
    id text NOT NULL,
    from_digest text NOT NULL,
    to_digest text NOT NULL,
    scope jsonb NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL,
    report jsonb NOT NULL
);


-- Name: store_meta; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.store_meta (
    key text NOT NULL,
    value text NOT NULL
);


-- Name: text_chunks; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.text_chunks (
    chunk_id bigint NOT NULL,
    field_id bigint NOT NULL,
    start integer NOT NULL,
    body bytea NOT NULL
);


-- Name: text_chunks_id; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.text_chunks_id
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: text_fields; Type: TABLE; Schema: ontology; Owner: -

CREATE TABLE ontology.text_fields (
    field_id bigint NOT NULL,
    object_id text NOT NULL,
    version_handle text NOT NULL,
    citation text NOT NULL,
    field_path text NOT NULL,
    document_kind text NOT NULL,
    sender_class text NOT NULL,
    markings text NOT NULL,
    text_digest text NOT NULL
);


-- Name: text_fields_id; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.text_fields_id
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: value_fields_field; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.value_fields_field
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: value_kinds_kind; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.value_kinds_kind
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: value_sources_src; Type: SEQUENCE; Schema: ontology; Owner: -

CREATE SEQUENCE ontology.value_sources_src
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: value_words; Type: VIEW; Schema: ontology; Owner: -

CREATE VIEW ontology.value_words AS
 SELECT ((c.src << 20) | o.ord) AS row_no,
    c.src,
    c.field,
    ontology.sq_words(c.value) AS words
   FROM (ontology.value_cells c
     JOIN ontology.value_field_order o ON ((o.field = c.field)))
  WHERE ((c.value_num IS NULL) AND (c.value IS NOT NULL) AND (c.kind IN ( SELECT value_kinds.kind
           FROM ontology.value_kinds
          WHERE (value_kinds.value_type = ANY (ARRAY['text'::text, 'identifier'::text, 'enum'::text])))) AND (NOT (EXISTS ( SELECT 1
           FROM ontology.packed_sources p
          WHERE (p.src = c.src)))));


-- Name: migrations; Type: TABLE; Schema: plx_meta; Owner: -

CREATE TABLE plx_meta.migrations (
    version integer NOT NULL,
    name text NOT NULL,
    sha256 text NOT NULL,
    params jsonb DEFAULT '{}'::jsonb NOT NULL,
    applied_at timestamp with time zone DEFAULT now() NOT NULL
);


-- Name: audience_units; Type: TABLE; Schema: search; Owner: -

CREATE TABLE search.audience_units (
    audience_key text NOT NULL,
    sharing text NOT NULL,
    label text,
    people text[] DEFAULT '{}'::text[] NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT audience_units_audience_key_check CHECK (((audience_key ~ '^[a-z][a-z0-9-]*:.+$'::text) AND (POSITION(('*'::text) IN (audience_key)) = 0))),
    CONSTRAINT audience_units_label_check CHECK (((label IS NULL) OR (label <> ''::text))),
    CONSTRAINT audience_units_people_check CHECK ((array_position(people, NULL::text) IS NULL)),
    CONSTRAINT audience_units_sharing_check CHECK ((sharing = ANY (ARRAY['shared'::text, 'personal'::text])))
);


-- Name: chunks; Type: TABLE; Schema: search; Owner: -

CREATE TABLE search.chunks (
    chunk_id text NOT NULL,
    source_handle text NOT NULL,
    object_id text,
    kind text NOT NULL,
    title text,
    author_role text,
    occurred_at timestamp with time zone,
    header text NOT NULL,
    body text NOT NULL,
    content_sha text NOT NULL,
    linked_ids text[] DEFAULT '{}'::text[] NOT NULL,
    audience_key text NOT NULL,
    markings text[] DEFAULT '{}'::text[] NOT NULL,
    dup_group text,
    authority smallint NOT NULL,
    tsv tsvector GENERATED ALWAYS AS ((((setweight(to_tsvector('english'::regconfig, header), 'A'::"char") || setweight(to_tsvector('english'::regconfig, body), 'D'::"char")) || setweight(to_tsvector('simple'::regconfig, header), 'A'::"char")) || setweight(to_tsvector('simple'::regconfig, body), 'D'::"char"))) STORED,
    embedding public.halfvec(1024),
    embed_model text,
    updated_at timestamp with time zone NOT NULL,
    CONSTRAINT chunks_audience_key_check CHECK (((audience_key ~ '^[a-z][a-z0-9-]*:.+$'::text) AND (POSITION(('*'::text) IN (audience_key)) = 0))),
    CONSTRAINT chunks_authority_check CHECK (((authority >= 1) AND (authority <= 4))),
    CONSTRAINT chunks_chunk_id_check CHECK ((chunk_id <> ''::text)),
    CONSTRAINT chunks_content_sha_check CHECK ((content_sha ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chunks_embedding_has_model CHECK (((embedding IS NULL) = (embed_model IS NULL))),
    CONSTRAINT chunks_kind_check CHECK ((kind <> ''::text)),
    CONSTRAINT chunks_markings_check CHECK (((markings <@ ARRAY['finance'::text, 'l5'::text, 'pii'::text, 'purchasing_prices'::text, 'quality_controlled'::text]) AND (array_position(markings, NULL::text) IS NULL))),
    CONSTRAINT chunks_source_handle_check CHECK ((source_handle <> ''::text))
);


-- Name: embedding_config; Type: TABLE; Schema: search; Owner: -

CREATE TABLE search.embedding_config (
    singleton boolean DEFAULT true NOT NULL,
    model text NOT NULL,
    dim integer NOT NULL,
    changed_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT embedding_config_dim_check CHECK (((dim >= 1) AND (dim <= 4000))),
    CONSTRAINT embedding_config_model_check CHECK ((model <> ''::text)),
    CONSTRAINT embedding_config_singleton_check CHECK (singleton)
);


-- Name: entities; Type: TABLE; Schema: search; Owner: -

CREATE TABLE search.entities (
    entity_id text NOT NULL,
    object_id text,
    type text,
    name text,
    alias text[] DEFAULT '{}'::text[] NOT NULL,
    normalized text,
    audience_key text NOT NULL,
    markings text[] DEFAULT '{}'::text[] NOT NULL,
    name_embedding public.halfvec(1024),
    name_embed_model text,
    CONSTRAINT entities_audience_key_check CHECK (((audience_key ~ '^[a-z][a-z0-9-]*:.+$'::text) AND (POSITION(('*'::text) IN (audience_key)) = 0))),
    CONSTRAINT entities_entity_id_check CHECK ((entity_id <> ''::text)),
    CONSTRAINT entities_markings_check CHECK (((markings <@ ARRAY['finance'::text, 'l5'::text, 'pii'::text, 'purchasing_prices'::text, 'quality_controlled'::text]) AND (array_position(markings, NULL::text) IS NULL))),
    CONSTRAINT entities_name_embedding_has_model CHECK (((name_embedding IS NULL) = (name_embed_model IS NULL)))
);


-- Name: feedback; Type: TABLE; Schema: search; Owner: -

CREATE TABLE search.feedback (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor text,
    question text,
    answer_id text,
    rating smallint,
    note text
);


-- Name: feedback_id_seq; Type: SEQUENCE; Schema: search; Owner: -

CREATE SEQUENCE search.feedback_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: feedback_id_seq; Type: SEQUENCE OWNED BY; Schema: search; Owner: -

ALTER SEQUENCE search.feedback_id_seq OWNED BY search.feedback.id;


-- Name: query_log; Type: TABLE; Schema: search; Owner: -

CREATE TABLE search.query_log (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    actor text,
    question text,
    intent text,
    entities jsonb,
    rewrites jsonb,
    results jsonb,
    timings jsonb
);


-- Name: query_log_id_seq; Type: SEQUENCE; Schema: search; Owner: -

CREATE SEQUENCE search.query_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: query_log_id_seq; Type: SEQUENCE OWNED BY; Schema: search; Owner: -

ALTER SEQUENCE search.query_log_id_seq OWNED BY search.query_log.id;


-- Name: write_generation; Type: SEQUENCE; Schema: search; Owner: -

CREATE SEQUENCE search.write_generation
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


-- Name: applied_owned seq; Type: DEFAULT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.applied_owned ALTER COLUMN seq SET DEFAULT nextval('ontology.applied_owned_seq_seq'::regclass);


-- Name: feedback id; Type: DEFAULT; Schema: search; Owner: -

ALTER TABLE ONLY search.feedback ALTER COLUMN id SET DEFAULT nextval('search.feedback_id_seq'::regclass);


-- Name: query_log id; Type: DEFAULT; Schema: search; Owner: -

ALTER TABLE ONLY search.query_log ALTER COLUMN id SET DEFAULT nextval('search.query_log_id_seq'::regclass);


-- Name: applied_owned applied_owned_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.applied_owned
    ADD CONSTRAINT applied_owned_pkey PRIMARY KEY (seq);


-- Name: chunk_words chunk_words_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.chunk_words
    ADD CONSTRAINT chunk_words_pkey PRIMARY KEY (chunk_id);


-- Name: event_objects event_objects_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.event_objects
    ADD CONSTRAINT event_objects_pkey PRIMARY KEY (event_id, object_id, qualifier);


-- Name: events events_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (event_id);


-- Name: identifiers identifiers_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.identifiers
    ADD CONSTRAINT identifiers_pkey PRIMARY KEY (identifier_kind, normalized, object_id, l1_handle, field_path);


-- Name: link_rows link_rows_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.link_rows
    ADD CONSTRAINT link_rows_pkey PRIMARY KEY (link_id);


-- Name: link_skeletons link_skeletons_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.link_skeletons
    ADD CONSTRAINT link_skeletons_pkey PRIMARY KEY (id);


-- Name: link_texts link_texts_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.link_texts
    ADD CONSTRAINT link_texts_pkey PRIMARY KEY (id);


-- Name: load_ranges load_ranges_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.load_ranges
    ADD CONSTRAINT load_ranges_pkey PRIMARY KEY (table_name, low, high);


-- Name: load_state load_state_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.load_state
    ADD CONSTRAINT load_state_pkey PRIMARY KEY (name);


-- Name: object_removals object_removals_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.object_removals
    ADD CONSTRAINT object_removals_pkey PRIMARY KEY (object_id, since, basis);


-- Name: object_versions object_versions_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.object_versions
    ADD CONSTRAINT object_versions_pkey PRIMARY KEY (ver);


-- Name: objects objects_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.objects
    ADD CONSTRAINT objects_pkey PRIMARY KEY (object_id);


-- Name: packed_sources packed_sources_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.packed_sources
    ADD CONSTRAINT packed_sources_pkey PRIMARY KEY (src);


-- Name: reproject reproject_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.reproject
    ADD CONSTRAINT reproject_pkey PRIMARY KEY (object_id);


-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (id);


-- Name: store_meta store_meta_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.store_meta
    ADD CONSTRAINT store_meta_pkey PRIMARY KEY (key);


-- Name: text_chunks text_chunks_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.text_chunks
    ADD CONSTRAINT text_chunks_pkey PRIMARY KEY (chunk_id);


-- Name: text_fields text_fields_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.text_fields
    ADD CONSTRAINT text_fields_pkey PRIMARY KEY (field_id);


-- Name: value_cells value_cells_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.value_cells
    ADD CONSTRAINT value_cells_pkey PRIMARY KEY (src, field);


-- Name: value_fields value_fields_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.value_fields
    ADD CONSTRAINT value_fields_pkey PRIMARY KEY (field);


-- Name: value_fields value_fields_property_field_path_key; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.value_fields
    ADD CONSTRAINT value_fields_property_field_path_key UNIQUE (property, field_path);


-- Name: value_kinds value_kinds_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.value_kinds
    ADD CONSTRAINT value_kinds_pkey PRIMARY KEY (kind);


-- Name: value_kinds value_kinds_value_type_unit_quantity_role_tax_basis_parser__key; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.value_kinds
    ADD CONSTRAINT value_kinds_value_type_unit_quantity_role_tax_basis_parser__key UNIQUE NULLS NOT DISTINCT (value_type, unit, quantity_role, tax_basis, parser, authority_rank, markings);


-- Name: value_sources value_sources_pkey; Type: CONSTRAINT; Schema: ontology; Owner: -

ALTER TABLE ONLY ontology.value_sources
    ADD CONSTRAINT value_sources_pkey PRIMARY KEY (src);


-- Name: migrations migrations_pkey; Type: CONSTRAINT; Schema: plx_meta; Owner: -

ALTER TABLE ONLY plx_meta.migrations
    ADD CONSTRAINT migrations_pkey PRIMARY KEY (version);


-- Name: audience_units audience_units_pkey; Type: CONSTRAINT; Schema: search; Owner: -

ALTER TABLE ONLY search.audience_units
    ADD CONSTRAINT audience_units_pkey PRIMARY KEY (audience_key);


-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: search; Owner: -

ALTER TABLE ONLY search.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (chunk_id);


-- Name: embedding_config embedding_config_pkey; Type: CONSTRAINT; Schema: search; Owner: -

ALTER TABLE ONLY search.embedding_config
    ADD CONSTRAINT embedding_config_pkey PRIMARY KEY (singleton);


-- Name: entities entities_pkey; Type: CONSTRAINT; Schema: search; Owner: -

ALTER TABLE ONLY search.entities
    ADD CONSTRAINT entities_pkey PRIMARY KEY (entity_id);


-- Name: feedback feedback_pkey; Type: CONSTRAINT; Schema: search; Owner: -

ALTER TABLE ONLY search.feedback
    ADD CONSTRAINT feedback_pkey PRIMARY KEY (id);


-- Name: query_log query_log_pkey; Type: CONSTRAINT; Schema: search; Owner: -

ALTER TABLE ONLY search.query_log
    ADD CONSTRAINT query_log_pkey PRIMARY KEY (id);


-- Name: chunk_words_words; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX chunk_words_words ON ontology.chunk_words USING gin (words);


-- Name: event_objects_by_object; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX event_objects_by_object ON ontology.event_objects USING btree (object_id);


-- Name: events_by_type; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX events_by_type ON ontology.events USING btree (event_type, "time");


-- Name: identifiers_by_object; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX identifiers_by_object ON ontology.identifiers USING btree (object_id);


-- Name: identifiers_by_value; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX identifiers_by_value ON ontology.identifiers USING btree (normalized text_pattern_ops);


-- Name: link_rows_by_id; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_by_id ON ontology.link_rows USING btree (link_id COLLATE "C");


-- Name: link_rows_by_row_no; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_by_row_no ON ontology.link_rows USING btree (row_no);


-- Name: link_rows_by_type; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_by_type ON ontology.link_rows USING btree (link_type, tier);


-- Name: link_rows_contra_from; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_contra_from ON ontology.link_rows USING btree (from_object) INCLUDE (to_object, link_type) WHERE (tier = 'contradicted'::text);


-- Name: link_rows_from; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_from ON ontology.link_rows USING btree (from_object, link_type);


-- Name: link_rows_lead_from; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_lead_from ON ontology.link_rows USING btree (from_object, link_id COLLATE "C") WHERE ((tier = 'lead'::text) AND (superseded_at IS NULL));


-- Name: link_rows_to; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX link_rows_to ON ontology.link_rows USING btree (to_object, link_type);


-- Name: object_versions_by_handle; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX object_versions_by_handle ON ontology.object_versions USING btree (l1_handle);


-- Name: object_versions_by_object; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX object_versions_by_object ON ontology.object_versions USING btree (object_id, recorded_at);


-- Name: object_versions_by_object_c; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX object_versions_by_object_c ON ontology.object_versions USING btree (object_id COLLATE "C");


-- Name: objects_by_audience; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX objects_by_audience ON ontology.objects USING btree (audience_key);


-- Name: objects_by_binding; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX objects_by_binding ON ontology.objects USING btree (binding_id);


-- Name: objects_by_row_no; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX objects_by_row_no ON ontology.objects USING btree (row_no);


-- Name: objects_by_type; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX objects_by_type ON ontology.objects USING btree (object_type);


-- Name: text_chunks_by_field; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX text_chunks_by_field ON ontology.text_chunks USING btree (field_id, start);


-- Name: text_fields_by_citation; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX text_fields_by_citation ON ontology.text_fields USING btree (citation, field_path);


-- Name: text_fields_by_object; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX text_fields_by_object ON ontology.text_fields USING btree (object_id);


-- Name: value_cells_by_field; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX value_cells_by_field ON ontology.value_cells USING btree (field, src);


-- Name: value_sources_by_version; Type: INDEX; Schema: ontology; Owner: -

CREATE INDEX value_sources_by_version ON ontology.value_sources USING btree (ver);


-- Name: chunks_audience_key; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_audience_key ON search.chunks USING btree (audience_key);


-- Name: chunks_content_sha; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_content_sha ON search.chunks USING btree (content_sha);


-- Name: chunks_dup_group; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_dup_group ON search.chunks USING btree (dup_group) WHERE (dup_group IS NOT NULL);


-- Name: chunks_embedding_hnsw; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_embedding_hnsw ON search.chunks USING hnsw (embedding public.halfvec_cosine_ops) WITH (m='16', ef_construction='64');


-- Name: chunks_header_trgm; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_header_trgm ON search.chunks USING gin (header public.gin_trgm_ops);


-- Name: chunks_kind; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_kind ON search.chunks USING btree (kind);


-- Name: chunks_linked_ids_gin; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_linked_ids_gin ON search.chunks USING gin (linked_ids);


-- Name: chunks_marked; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_marked ON search.chunks USING btree (object_id) INCLUDE (source_handle, markings, kind) WHERE (markings <> '{}'::text[]);


-- Name: chunks_object_id; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_object_id ON search.chunks USING btree (object_id);


-- Name: chunks_tsv_gin; Type: INDEX; Schema: search; Owner: -

CREATE INDEX chunks_tsv_gin ON search.chunks USING gin (tsv);


-- Name: entities_alias_gin; Type: INDEX; Schema: search; Owner: -

CREATE INDEX entities_alias_gin ON search.entities USING gin (alias);


-- Name: entities_alias_text_trgm; Type: INDEX; Schema: search; Owner: -

CREATE INDEX entities_alias_text_trgm ON search.entities USING gin (search.alias_text(alias) public.gin_trgm_ops);


-- Name: entities_audience_key; Type: INDEX; Schema: search; Owner: -

CREATE INDEX entities_audience_key ON search.entities USING btree (audience_key);


-- Name: entities_name_embedding_hnsw; Type: INDEX; Schema: search; Owner: -

CREATE INDEX entities_name_embedding_hnsw ON search.entities USING hnsw (name_embedding public.halfvec_cosine_ops) WITH (m='16', ef_construction='64');


-- Name: entities_normalized_trgm; Type: INDEX; Schema: search; Owner: -

CREATE INDEX entities_normalized_trgm ON search.entities USING gin (normalized public.gin_trgm_ops);


-- Name: entities_object_id; Type: INDEX; Schema: search; Owner: -

CREATE INDEX entities_object_id ON search.entities USING btree (object_id);


-- Name: feedback_answer_id; Type: INDEX; Schema: search; Owner: -

CREATE INDEX feedback_answer_id ON search.feedback USING btree (answer_id);


-- Name: query_log_at; Type: INDEX; Schema: search; Owner: -

CREATE INDEX query_log_at ON search.query_log USING btree (at);


-- Name: audience_units audience_units_truncate_generation; Type: TRIGGER; Schema: search; Owner: -

CREATE TRIGGER audience_units_truncate_generation AFTER TRUNCATE ON search.audience_units FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();


-- Name: audience_units audience_units_write_generation; Type: TRIGGER; Schema: search; Owner: -

CREATE TRIGGER audience_units_write_generation AFTER INSERT OR DELETE OR UPDATE ON search.audience_units FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();


-- Name: chunks chunks_truncate_generation; Type: TRIGGER; Schema: search; Owner: -

CREATE TRIGGER chunks_truncate_generation AFTER TRUNCATE ON search.chunks FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();


-- Name: chunks chunks_write_generation; Type: TRIGGER; Schema: search; Owner: -

CREATE TRIGGER chunks_write_generation AFTER INSERT OR DELETE OR UPDATE OF audience_key ON search.chunks FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();


-- Name: entities entities_truncate_generation; Type: TRIGGER; Schema: search; Owner: -

CREATE TRIGGER entities_truncate_generation AFTER TRUNCATE ON search.entities FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();


-- Name: entities entities_write_generation; Type: TRIGGER; Schema: search; Owner: -

CREATE TRIGGER entities_write_generation AFTER INSERT OR DELETE OR UPDATE OF audience_key ON search.entities FOR EACH STATEMENT EXECUTE FUNCTION search.bump_write_generation();



\unrestrict ExHyexrpMeiuiFYzIz7lY7Shhh9Il9ZiMpicmvqn0LswvmauXc22Akl966ywEtX

