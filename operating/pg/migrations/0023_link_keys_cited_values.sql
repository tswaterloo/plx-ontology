-- 0023_link_keys_cited_values: every link type's keys readable by SQL (CAP-L296 remainder).
--
-- 0019 read the values a link's reason records itself (fk pairs, identifier mentions,
-- exact identifiers): 398 of 436 link types. The other 38 cite where their key sits but
-- do not copy it into the reason: people and party links decided on an address
-- (sender_key/person_key, sender_address/party_address), name mentions (name_mention /
-- record_name), document and carrier references read off a packet, certificate or
-- confirmation (document_reference/carrier_reference + identity_candidate, which do
-- record their value under another feature name), the two-citation same-record checks
-- (your_reference, bc_number, party_name) and the derived links (pegging, confirmed
-- leads). This version reads all of them:
--   * ``mention``: document_reference and carrier_reference are read like identifier_mention.
--   * ``cited_pair``: a feature with two citations and no recorded value gives one row,
--     each side's value read from the store at the cited version (object_versions by
--     l1_handle + digest -> value_sources -> value_cells at the cited field_path).
--   * ``cited``: a link decided on one cited field per side (an address, a name) gives
--     its first and last cited values the same way; a name mention's side is the cited
--     span of its field, not the whole field.
--   * ``derived``: a link derived from other links (pegging, a confirmed lead) gives one
--     row per deriving feature with the values it recorded (source link ids, item,
--     quantity, dates) in ``from_value`` as JSON; each source link's own keys are read
--     through this function on its type.
-- ``compare`` on a store-read row says which version the value came from:
-- ``cited_version`` (the version the link cites, by handle and digest) or
-- ``current_version`` (that version is no longer held: the record's current version,
-- under the same or a later capture row, is read instead). ``outcome`` on a
-- store-read row: ``agree`` (equal ignoring case and outer spaces), ``normalized_agree``
-- (equal on letters and digits: "PO#5276" and "PO5276", "Thick Leave In" and
-- "THICK LEAVE-IN"), ``shared_key`` (both hold the same e-mail address or directory id:
-- a sender among a person's addresses, a participant's id), ``same_domain`` (the two name
-- only the same mail or web domain, one perhaps a subdomain of the other: a supplier
-- decided on its mail domain or website), ``contains`` (one side
-- holds the other, both at least 3 characters: a first name in an address, a name
-- inside a longer name), ``values_differ`` (anything else: dates a day apart, two names),
-- or
-- ``value_not_recorded`` when a side's value cannot be read from the store, so a gap is
-- counted, never taken for "fine" (a link to a record the store no longer holds lands
-- here). ``matched_value`` is the value both sides meet on (the agreed key, the shared
-- address, the domain), NULL when they do not. Values longer than 1,000 characters are
-- cut there (compared in full).
-- Same argument as 0019 and the same columns plus ``matched_value`` at the end; reads only
-- the current rows of one link type (NULL reads every type).

-- One cited value read back from the store: the cited version's cell at the cited field.
-- A handle is a source record plus its capture row (``<record>@<capture>-<row>``); when the
-- source was captured again the cited row's version is gone (folded or replaced), and the
-- record's current version under its new capture row is read instead (``current_version``).
CREATE OR REPLACE FUNCTION ontology.link_cite_value(p_cite jsonb)
RETURNS TABLE (value text, version text)
LANGUAGE sql STABLE AS $body$
    WITH h AS (
        SELECT p_cite->>'l1_handle' AS handle, p_cite->>'digest' AS digest,
               p_cite->>'field_path' AS field_path,
               substring(p_cite->>'l1_handle' FROM '^(.*)@[0-9a-f]+-[0-9]+$') AS base
        WHERE jsonb_typeof(p_cite) = 'object'
    ), v AS (
        SELECT ov.ver, ov.l1_handle = h.handle AND ov.digest = h.digest AS cited,
               ov.l1_handle = h.handle AS same_row
        FROM h JOIN ontology.object_versions ov ON ov.l1_handle = h.handle
        WHERE ov.digest = h.digest OR ov.superseded_at IS NULL
        UNION ALL
        -- the handles <base>@<hex>-<digits>, by an index range that holds in any collation:
        -- after "<base>@" and before "<base>@g" (no hex digit sorts after g, bytewise or
        -- linguistically; "<base>A" missed them where punctuation weighs nothing)
        SELECT ov.ver, false, false
        FROM h JOIN ontology.object_versions ov
          ON ov.l1_handle >= h.base || '@' AND ov.l1_handle < h.base || '@g'
        WHERE h.base IS NOT NULL AND ov.superseded_at IS NULL AND ov.l1_handle <> h.handle
          AND substring(ov.l1_handle FROM '^(.*)@[0-9a-f]+-[0-9]+$') = h.base
    )
    SELECT coalesce(vc.value, vc.value_num::text),
           CASE WHEN v.cited THEN 'cited_version' ELSE 'current_version' END
    FROM v CROSS JOIN h
    JOIN ontology.value_sources vs ON vs.ver = v.ver
    JOIN ontology.value_cells vc ON vc.src = vs.src
    JOIN ontology.value_fields vf ON vf.field = vc.field AND vf.field_path = h.field_path
    WHERE coalesce(vc.value, vc.value_num::text) IS NOT NULL
    ORDER BY v.cited DESC, v.same_row DESC, (vs.superseded_at IS NULL) DESC, v.ver DESC,
             vs.src DESC
    LIMIT 1
$body$;

-- The cited span [start, end) of a value (Python offsets), or the whole value.
CREATE OR REPLACE FUNCTION ontology.link_span_value(p_value text, p_span jsonb)
RETURNS text LANGUAGE sql IMMUTABLE AS $body$
    SELECT CASE
        WHEN p_value IS NULL THEN NULL
        WHEN jsonb_typeof(p_span) = 'array' AND jsonb_array_length(p_span) = 2
             AND jsonb_typeof(p_span->0) = 'number' AND jsonb_typeof(p_span->1) = 'number'
             AND (p_span->>0)::int >= 0 AND (p_span->>1)::int > (p_span->>0)::int
             AND (p_span->>1)::int <= length(p_value)
        THEN substr(p_value, (p_span->>0)::int + 1, (p_span->>1)::int - (p_span->>0)::int)
        ELSE p_value END
$body$;

-- The keys a value carries: every e-mail address and directory id (UUID) in it, lower
-- case; a value holding neither is its own one key (lower case, outer spaces cut).
CREATE OR REPLACE FUNCTION ontology.link_value_keys(p_value text)
RETURNS text[] LANGUAGE sql IMMUTABLE AS $body$
    SELECT CASE WHEN p_value IS NULL OR btrim(p_value) = '' THEN '{}'::text[]
        ELSE coalesce(
            (SELECT array_agg(DISTINCT m[1] ORDER BY m[1])
             FROM regexp_matches(lower(p_value),
                 '([a-z0-9._+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                 'g') m),
            ARRAY[lower(btrim(p_value))]) END
$body$;

-- The mail and web domains a value names: an address's domain, a web address's host
-- (scheme and a leading www. dropped), or the whole value when it is a bare domain.
CREATE OR REPLACE FUNCTION ontology.link_value_domains(p_value text)
RETURNS text[] LANGUAGE sql IMMUTABLE AS $body$
    SELECT CASE WHEN p_value IS NULL THEN '{}'::text[] ELSE coalesce((
        SELECT array_agg(DISTINCT regexp_replace(d, '^www\.', '') ORDER BY regexp_replace(d, '^www\.', ''))
        FROM (SELECT m[1] AS d
              FROM regexp_matches(lower(p_value),
                  '(?:@|https?://|\mwww\.)([a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,})', 'g') m
              UNION ALL
              SELECT lower(btrim(p_value, ' /'))
              WHERE lower(btrim(p_value, ' /')) ~ '^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$') x),
        '{}'::text[]) END
$body$;

-- How two store-read values compare, and the value they meet on.
CREATE OR REPLACE FUNCTION ontology.link_value_compare(p_from text, p_to text)
RETURNS TABLE (outcome text, matched text) LANGUAGE sql IMMUTABLE AS $body$
    WITH d AS (
        SELECT ontology.link_value_keys(p_from) AS ka, ontology.link_value_keys(p_to) AS kb,
               regexp_replace(lower(coalesce(p_from, '')), '[^a-z0-9]+', '', 'g') AS na,
               regexp_replace(lower(coalesce(p_to, '')), '[^a-z0-9]+', '', 'g') AS nb,
               -- the shortest domain both name, one the same as or a subdomain of the
               -- other (mktg.univarsolutions.com and univarsolutions.com)
               (SELECT CASE WHEN length(x) <= length(y) THEN x ELSE y END
                FROM unnest(ontology.link_value_domains(p_from)) x,
                     unnest(ontology.link_value_domains(p_to)) y
                WHERE x = y OR right(x, length(y) + 1) = '.' || y
                   OR right(y, length(x) + 1) = '.' || x
                ORDER BY least(length(x), length(y)), x LIMIT 1) AS domain
    )
    SELECT CASE
        WHEN p_from IS NULL OR p_to IS NULL OR btrim(p_from) = '' OR btrim(p_to) = ''
            THEN 'value_not_recorded'
        WHEN lower(btrim(p_from)) = lower(btrim(p_to)) THEN 'agree'
        WHEN d.ka && d.kb THEN 'shared_key'
        WHEN d.na <> '' AND d.na = d.nb THEN 'normalized_agree'
        WHEN d.domain IS NOT NULL THEN 'same_domain'
        WHEN least(length(btrim(p_from)), length(btrim(p_to))) >= 3
         AND (strpos(lower(p_to), lower(btrim(p_from))) > 0
              OR strpos(lower(p_from), lower(btrim(p_to))) > 0) THEN 'contains'
        ELSE 'values_differ' END,
           CASE
        WHEN p_from IS NULL OR p_to IS NULL OR btrim(p_from) = '' OR btrim(p_to) = '' THEN NULL
        WHEN lower(btrim(p_from)) = lower(btrim(p_to)) THEN left(btrim(p_from), 1000)
        WHEN d.ka && d.kb THEN (SELECT min(x) FROM unnest(d.ka) x WHERE x = ANY (d.kb))
        WHEN d.na <> '' AND d.na = d.nb THEN left(d.na, 1000)
        WHEN d.domain IS NOT NULL THEN d.domain
        WHEN least(length(btrim(p_from)), length(btrim(p_to))) < 3 THEN NULL
        WHEN strpos(lower(p_to), lower(btrim(p_from))) > 0 THEN left(btrim(p_from), 1000)
        WHEN strpos(lower(p_from), lower(btrim(p_to))) > 0 THEN left(btrim(p_to), 1000)
        END
    FROM d
$body$;

-- 0019's function returned no matched value: dropped and created with that column added
-- at the end (nothing depends on it; callers name their columns).
DROP FUNCTION IF EXISTS ontology.link_keys(text);

CREATE FUNCTION ontology.link_keys(p_link_type text)
RETURNS TABLE (link_id text, link_type text, from_object text, to_object text, tier text,
               binding_kind text, markings text, feature text, compare text,
               identifier_kind text, outcome text, from_field text, from_handle text,
               from_value text, to_field text, to_handle text, to_value text,
               matched_value text)
LANGUAGE sql STABLE AS $body$
    WITH r AS (
        SELECT l.link_id, l.link_type, l.from_object, l.to_object, l.tier, l.binding_kind,
               l.markings,
               coalesce(l.reason, ontology.link_text(l.reason_id, l.slots, l.ek + 1, 64))::jsonb
                   AS reason
        FROM ontology.link_rows l
        WHERE (p_link_type IS NULL OR l.link_type = p_link_type)
          AND l.superseded_at IS NULL
    ), f AS (
        SELECT r.link_id, e, n,
               CASE WHEN jsonb_typeof(e->'citations') = 'array'
                    THEN jsonb_array_length(e->'citations') ELSE 0 END AS ncite,
               e->>'feature' IN ('pegged_lines', 'pegged_quantity', 'same_item',
                                 'stored_lead', 'confirming_path') AS derived
        FROM r CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(r.reason) = 'array' THEN r.reason ELSE '[]'::jsonb END)
            WITH ORDINALITY x(e, n)
        WHERE jsonb_typeof(e) = 'object'
    ), g AS (
        -- one row per link: the features each shape reads, taken in reason order
        SELECT f.link_id,
               (array_agg(e ORDER BY n) FILTER (
                   WHERE e->>'feature' IN ('identifier_mention', 'document_reference',
                                           'carrier_reference')))[1] AS m,
               (array_agg(e ORDER BY n) FILTER (
                   WHERE e->>'feature' = 'identity_candidate'))[1] AS c,
               (array_agg(e ORDER BY n) FILTER (
                   WHERE e->>'feature' <> 'exact_identifier'
                     AND jsonb_typeof(e->'citations') = 'array'
                     AND e->'citations'->0 ? 'raw'))[1] AS form,
               (array_agg(e ORDER BY n) FILTER (WHERE ncite > 0))[1] AS first_feat,
               -- the side the first feature was matched to: the next feature citing a
               -- field (person_key, party_address, record_name), never a later piece of
               -- support (named_record_party cites a whole order)
               coalesce((array_agg(e ORDER BY n) FILTER (WHERE ncite > 0))[2],
                        (array_agg(e ORDER BY n) FILTER (WHERE ncite > 0))[1]) AS to_feat,
               bool_or(e->>'feature' = 'pair' AND e ? 'value') AS has_pair,
               bool_or(e->>'feature' = 'exact_identifier' AND ncite >= 1) AS has_ident,
               bool_or(derived) AS has_derived,
               bool_or(ncite = 2 AND NOT derived AND NOT e ? 'value'
                       AND e->>'feature' NOT IN ('pair', 'exact_identifier',
                                                 'identifier_mention', 'document_reference',
                                                 'carrier_reference', 'identity_candidate'))
                   AS has_cited_pair
        FROM f GROUP BY f.link_id
    ), u AS (
        -- links no recorded-value shape (pair, mention, identifier) reads
        SELECT r.*, g.first_feat, g.to_feat, coalesce(g.has_derived, false) AS has_derived,
               coalesce(g.has_cited_pair, false) AS has_cited_pair
        FROM r LEFT JOIN g USING (link_id)
        WHERE NOT coalesce(g.has_pair, false) AND NOT coalesce(g.has_ident, false)
          AND (g.m IS NULL OR g.c IS NULL)
    )
    SELECT r.link_id, r.link_type, r.from_object, r.to_object, r.tier, r.binding_kind,
           r.markings, 'pair', e->>'compare', e->>'identifier_kind', e->>'outcome',
           e->>'from_field', e->>'from_handle', e->>'value',
           e->>'to_field', e->>'to_handle', e->>'value', e->>'value'
    FROM r JOIN f USING (link_id)
    WHERE e->>'feature' = 'pair' AND e ? 'value'
    UNION ALL
    SELECT r.link_id, r.link_type, r.from_object, r.to_object, r.tier, r.binding_kind,
           r.markings, 'mention', 'identifier', g.m->>'identifier_kind',
           -- 'values_differ': the text and the record carry the key in two forms (a lead
           -- matched "PO3836" to bc_number 3836); the audit reads both values
           CASE WHEN g.m->>'normalized' = g.c->>'matched_normalized' THEN 'agree'
                ELSE 'values_differ' END,
           g.m->'citations'->0->>'field_path', g.m->'citations'->0->>'l1_handle',
           g.m->>'normalized',
           g.c->'citations'->0->>'field_path', g.c->'citations'->0->>'l1_handle',
           g.c->>'matched_normalized',
           CASE WHEN g.m->>'normalized' = g.c->>'matched_normalized'
                THEN g.m->>'normalized' END
    FROM r JOIN g USING (link_id)
    WHERE g.m IS NOT NULL AND g.c IS NOT NULL
    UNION ALL
    -- exact_identifier: the raw value cited on each side; with one citation (the record
    -- found) the from side is the feature that read the value from the document
    SELECT r.link_id, r.link_type, r.from_object, r.to_object, r.tier, r.binding_kind,
           r.markings, 'identifier', 'identifier', f.e->>'identifier_kind',
           CASE WHEN f.e->>'agrees' = 'true' THEN 'agree' ELSE 'disagree' END,
           CASE WHEN f.ncite > 1 THEN f.e->'citations'->0->>'field_path'
                ELSE g.form->'citations'->0->>'field_path' END,
           CASE WHEN f.ncite > 1 THEN f.e->'citations'->0->>'l1_handle'
                ELSE g.form->'citations'->0->>'l1_handle' END,
           CASE WHEN f.ncite > 1 THEN f.e->'citations'->0->>'raw'
                ELSE coalesce(g.form->'citations'->0->>'raw', g.form->>'normalized') END,
           f.e->'citations'->-1->>'field_path', f.e->'citations'->-1->>'l1_handle',
           coalesce(f.e->'citations'->-1->>'raw', f.e->>'normalized'),
           CASE WHEN f.e->>'agrees' = 'true' THEN f.e->>'normalized' END
    FROM r JOIN f USING (link_id) JOIN g USING (link_id)
    WHERE f.e->>'feature' = 'exact_identifier' AND f.ncite >= 1
    UNION ALL
    -- a two-citation check that records no value (your_reference, bc_number, party_name):
    -- each side read from the store at the cited version
    SELECT u.link_id, u.link_type, u.from_object, u.to_object, u.tier, u.binding_kind,
           u.markings, 'cited_pair',
           CASE WHEN a.version = 'cited_version' AND b.version = 'cited_version'
                THEN 'cited_version' WHEN a.version IS NULL AND b.version IS NULL THEN NULL
                ELSE 'current_version' END,
           coalesce(f.e->>'identifier_kind', f.e->>'feature'), cmp.outcome,
           f.e->'citations'->0->>'field_path', f.e->'citations'->0->>'l1_handle',
           left(a.value, 1000),
           f.e->'citations'->1->>'field_path', f.e->'citations'->1->>'l1_handle',
           left(b.value, 1000),
           -- the feature's own normalized value when it recorded one (party_name)
           coalesce(cmp.matched, f.e->>'normalized')
    FROM u JOIN f USING (link_id)
    LEFT JOIN LATERAL ontology.link_cite_value(f.e->'citations'->0) a ON true
    LEFT JOIN LATERAL ontology.link_cite_value(f.e->'citations'->1) b ON true
    CROSS JOIN LATERAL ontology.link_value_compare(a.value, b.value) cmp
    WHERE NOT u.has_derived AND f.ncite = 2 AND NOT f.derived AND NOT f.e ? 'value'
      AND f.e->>'feature' NOT IN ('pair', 'exact_identifier', 'identifier_mention',
                                  'document_reference', 'carrier_reference',
                                  'identity_candidate')
    UNION ALL
    -- a link derived from other links: the values each deriving feature recorded
    SELECT u.link_id, u.link_type, u.from_object, u.to_object, u.tier, u.binding_kind,
           u.markings, 'derived', f.e->>'feature', NULL, 'derived',
           f.e->'citations'->0->>'field_path', f.e->'citations'->0->>'l1_handle',
           left((f.e - 'citations' - 'agrees' - 'feature')::text, 1000),
           f.e->'citations'->-1->>'field_path', f.e->'citations'->-1->>'l1_handle', NULL,
           NULL
    FROM u JOIN f USING (link_id)
    WHERE u.has_derived AND f.derived
    UNION ALL
    -- decided on one cited field per side (an address, a name): the first feature's cited
    -- value and the one it was matched to (the next feature's), read from the store; a
    -- link citing nothing is listed with NULL values
    SELECT u.link_id, u.link_type, u.from_object, u.to_object, u.tier, u.binding_kind,
           u.markings, 'cited',
           CASE WHEN a.version = 'cited_version' AND b.version = 'cited_version'
                THEN 'cited_version' WHEN a.version IS NULL AND b.version IS NULL THEN NULL
                ELSE 'current_version' END,
           u.first_feat->>'identifier_kind', cmp.outcome,
           u.first_feat->'citations'->0->>'field_path',
           u.first_feat->'citations'->0->>'l1_handle', left(a.value, 1000),
           u.to_cite->>'field_path', u.to_cite->>'l1_handle', left(b.value, 1000), cmp.matched
    -- one feature citing one field names no second side: its to side is not recorded
    FROM (SELECT u.*, CASE WHEN u.first_feat IS DISTINCT FROM u.to_feat
                           THEN u.to_feat->'citations'->0
                           WHEN jsonb_array_length(u.to_feat->'citations') > 1
                           THEN u.to_feat->'citations'->-1 END AS to_cite FROM u) u
    LEFT JOIN LATERAL (
        SELECT ontology.link_span_value(v.value, u.first_feat->'span') AS value, v.version
        FROM ontology.link_cite_value(u.first_feat->'citations'->0) v) a ON true
    LEFT JOIN LATERAL (
        SELECT ontology.link_span_value(v.value,
                   CASE WHEN u.first_feat = u.to_feat THEN NULL
                        ELSE u.to_feat->'span' END) AS value, v.version
        FROM ontology.link_cite_value(u.to_cite) v) b ON true
    CROSS JOIN LATERAL ontology.link_value_compare(a.value, b.value) cmp
    WHERE NOT u.has_derived AND NOT u.has_cited_pair
$body$;
