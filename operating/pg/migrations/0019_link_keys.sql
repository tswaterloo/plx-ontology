-- 0019_link_keys: the key values each link was decided on, readable by SQL (CAP-L296: "link
-- precision must be auditable from the store: keep the fk key values, or the cited source
-- values, with each link").
--
-- The deciding code already keeps them in the link's reason (ontology/key_links.py pair
-- features: the agreed ``value`` with ``from_field``/``from_handle`` and
-- ``to_field``/``to_handle``; ontology/mentions.py: the ``identifier_mention`` feature's
-- ``normalized`` value and cited text, the ``identity_candidate`` feature's
-- ``matched_normalized`` value and cited record). Measured 11-13 on served data: 300 of 300
-- sampled links carry them in each of 11 fk/text-join kinds, 2 lead kinds and a mention kind;
-- through this function (300 current links per type, every type): 398 of 436 types give a
-- value on every sampled link, 94,187 of 101,001 links; the rest are people links decided
-- on an address, derived links (pegging, owners) and packet/decision links.
-- This function turns them into rows, one per key feature, so a precision sample is one
-- query, e.g.
--   SELECT k.* FROM ontology.link_keys('ShipmentLine.of_sales_order') k
--   ORDER BY md5(k.link_id) LIMIT 20;
-- Shapes read: ``pair`` (fk and text joins), ``mention`` (identifier_mention +
-- identity_candidate), ``identifier`` (exact_identifier: the raw value cited on each side;
-- a one-citation exact_identifier takes its from side from the feature that read the
-- document form). A link with none of them (people links decided on an address, derived
-- links) gets one ``cited`` row: the first and last cited handles, values NULL and outcome
-- ``value_not_recorded``, so the gap is counted, never taken for "fine".
--
-- A function, not a view: its body is not a dependency of ontology.link_rows, so a
-- migration that rebuilds that table (0017_link_rows_partitioned renames and drops it) is
-- not blocked. ``markings`` is the link's own: a caller showing rows to a reader trims them
-- as it trims the link. Reads only the current rows of one link type (index
-- link_rows_by_type); NULL reads every type (a whole-store scan).
CREATE OR REPLACE FUNCTION ontology.link_keys(p_link_type text)
RETURNS TABLE (link_id text, link_type text, from_object text, to_object text, tier text,
               binding_kind text, markings text, feature text, compare text,
               identifier_kind text, outcome text, from_field text, from_handle text,
               from_value text, to_field text, to_handle text, to_value text)
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
        SELECT r.link_id, e, n
        FROM r CROSS JOIN LATERAL jsonb_array_elements(
            CASE WHEN jsonb_typeof(r.reason) = 'array' THEN r.reason ELSE '[]'::jsonb END)
            WITH ORDINALITY x(e, n)
        WHERE jsonb_typeof(e) = 'object'
    ), g AS (
        -- one row per link: the features each shape reads, taken in reason order
        SELECT f.link_id,
               (array_agg(e ORDER BY n) FILTER (
                   WHERE e->>'feature' = 'identifier_mention'))[1] AS m,
               (array_agg(e ORDER BY n) FILTER (
                   WHERE e->>'feature' = 'identity_candidate'))[1] AS c,
               (array_agg(e ORDER BY n) FILTER (
                   WHERE e->>'feature' <> 'exact_identifier'
                     AND jsonb_typeof(e->'citations') = 'array'
                     AND e->'citations'->0 ? 'raw'))[1] AS form,
               (array_agg(e->'citations'->0 ORDER BY n) FILTER (
                   WHERE jsonb_typeof(e->'citations') = 'array'))[1] AS first_cite,
               (array_agg(e->'citations'->-1 ORDER BY n DESC) FILTER (
                   WHERE jsonb_typeof(e->'citations') = 'array'))[1] AS last_cite,
               bool_or(e->>'feature' = 'pair' AND e ? 'value') AS has_pair,
               bool_or(e->>'feature' = 'exact_identifier'
                       AND jsonb_typeof(e->'citations') = 'array'
                       AND jsonb_array_length(e->'citations') >= 1) AS has_ident
        FROM f GROUP BY f.link_id
    )
    SELECT r.link_id, r.link_type, r.from_object, r.to_object, r.tier, r.binding_kind,
           r.markings, 'pair', e->>'compare', e->>'identifier_kind', e->>'outcome',
           e->>'from_field', e->>'from_handle', e->>'value',
           e->>'to_field', e->>'to_handle', e->>'value'
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
           g.c->>'matched_normalized'
    FROM r JOIN g USING (link_id)
    WHERE g.m IS NOT NULL AND g.c IS NOT NULL
    UNION ALL
    -- exact_identifier: the raw value cited on each side; with one citation (the record
    -- found) the from side is the feature that read the value from the document
    SELECT r.link_id, r.link_type, r.from_object, r.to_object, r.tier, r.binding_kind,
           r.markings, 'identifier', 'identifier', f.e->>'identifier_kind',
           CASE WHEN f.e->>'agrees' = 'true' THEN 'agree' ELSE 'disagree' END,
           CASE WHEN jsonb_array_length(f.e->'citations') > 1
                THEN f.e->'citations'->0->>'field_path'
                ELSE g.form->'citations'->0->>'field_path' END,
           CASE WHEN jsonb_array_length(f.e->'citations') > 1
                THEN f.e->'citations'->0->>'l1_handle'
                ELSE g.form->'citations'->0->>'l1_handle' END,
           CASE WHEN jsonb_array_length(f.e->'citations') > 1
                THEN f.e->'citations'->0->>'raw'
                ELSE coalesce(g.form->'citations'->0->>'raw', g.form->>'normalized') END,
           f.e->'citations'->-1->>'field_path', f.e->'citations'->-1->>'l1_handle',
           coalesce(f.e->'citations'->-1->>'raw', f.e->>'normalized')
    FROM r JOIN f USING (link_id) JOIN g USING (link_id)
    WHERE f.e->>'feature' = 'exact_identifier'
      AND jsonb_typeof(f.e->'citations') = 'array'
      AND jsonb_array_length(f.e->'citations') >= 1
    UNION ALL
    SELECT r.link_id, r.link_type, r.from_object, r.to_object, r.tier, r.binding_kind,
           r.markings, 'cited', NULL, NULL, 'value_not_recorded',
           g.first_cite->>'field_path', g.first_cite->>'l1_handle', NULL,
           g.last_cite->>'field_path', g.last_cite->>'l1_handle', NULL
    FROM r LEFT JOIN g USING (link_id)
    WHERE NOT coalesce(g.has_pair, false) AND NOT coalesce(g.has_ident, false)
      AND (g.m IS NULL OR g.c IS NULL)
$body$;
