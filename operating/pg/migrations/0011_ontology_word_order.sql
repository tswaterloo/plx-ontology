-- 0011_ontology_word_order: word matches in the SQLite store's own order. A value word
-- row's number was (src << 20) | field, but SQLite numbers a value source's cells in the
-- order of their property and field path (the cell rowid; measured on a 1.35 M-cell
-- sample of gen 11: no exception), so a page cut inside one value source chose another
-- of its values (8 of 300 sampled finds named another matched property). The number is
-- now (src << 20) | the field's place in that order (``value_field_order``: property,
-- then field path, bytewise as SQLite's BINARY collation compares them), the same order
-- as SQLite's rowid; ``operating/pg/ontology_reader.py`` pages word matches by it.
CREATE VIEW ontology.value_field_order AS SELECT
    f.field, (row_number() OVER (ORDER BY f.property COLLATE "C", f.field_path COLLATE "C",
                                 f.field))::bigint AS ord
  FROM ontology.value_fields f;

CREATE OR REPLACE VIEW ontology.value_words AS SELECT
    (c.src << 20) | o.ord AS row_no, c.src, c.field, ontology.sq_words(c.value) AS words
  FROM ontology.value_cells c
  JOIN ontology.value_field_order o ON o.field = c.field
 WHERE c.value_num IS NULL AND c.value IS NOT NULL
   AND c.kind IN (SELECT kind FROM ontology.value_kinds
                   WHERE value_type IN ('text', 'identifier', 'enum'))
   AND NOT EXISTS (SELECT 1 FROM ontology.packed_sources p WHERE p.src = c.src);

CREATE OR REPLACE VIEW ontology.object_values_numbered AS SELECT
    (t.src << 20) | o.ord AS row_no, t.object_id, t.property, t.value_type, t.value,
    t.unit, t.quantity_role, t.currency, t.tax_basis, t.currency_citation, t.l1_handle,
    t.field_path, t.digest, t.lineage, t.binding_id, t.parser, t.authority_rank, t.markings,
    t.recorded_at, t.superseded_at, t.src, t.field
  FROM ontology.object_values_typed t
  JOIN ontology.value_field_order o ON o.field = t.field
 WHERE t.value_num IS NULL AND t.value IS NOT NULL
   AND t.value_type IN ('text', 'identifier', 'enum');

-- * sq_json_array / sq_json_array_int: ``x IN (SELECT value FROM json_each(:list))`` read as
--   ``x = ANY(ontology.sq_json_array(:list))`` (``sqlite_compat.translate``): the list is a
--   bound constant, so the array is made once when the statement is planned and tested
--   by hash, instead of a semi-join that walks the list's JSON for every row (a word
--   page's audience and marking lists: most of its time). The same elements as
--   ``sq_json_each`` (an object's values; nothing for what is neither).
CREATE FUNCTION ontology.sq_json_array(t text) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(value), '{}') FROM ontology.sq_json_each(t)
$$;
CREATE FUNCTION ontology.sq_json_array_int(t text) RETURNS bigint[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(value::bigint), '{}') FROM ontology.sq_json_each(t)
$$;
