-- 0010_ontology_words: the word index (SQLite FTS5 ``value_text`` and ``chunk_text``,
-- ontology/store_ddl.py TEXT_FTS) on Postgres, so word search reads Postgres
-- (operating/pg/words.py, operating/pg/ontology_reader.py). Sizes measured on the served
-- store are in docs/pg-cutover.md; a row per value cell would cost ~55 GB, so the value
-- words are an index over value_cells itself. The document words are a table of their own
-- (``chunk_words``): a chunk's body is zlib, which Postgres cannot read in an index.
-- * sq_words: a text's distinct words as FTS5's ``unicode61 remove_diacritics 2`` cuts them:
--   runs of letters and digits (and the number forms and private-use characters FTS5
--   counts as such), a Latin letter's diacritics removed, lower case, the micro sign as mu.
--   On the served store's sample (594k values, 200k chunks) it differs from FTS5 on 627 of
--   103k texts holding a non-ASCII character (none of the ASCII ones): emoji FTS5 counts as
--   words, mis-decoded text, combining marks of Indic and Arabic script. It needs a UTF-8
--   database whose ctype is not C (plx: C.UTF-8). Postgres's limits: a word over 2046
--   bytes and the words past a value's first 250,000 characters are not indexed.
-- * packed_sources: the value sources whose values the SQLite store keeps as a packed
--   record (a ``value_storage: packed`` binding's), which it never word-indexes; filled
--   by the ``value_words`` load step and by each delta.
-- * value_words: the word-indexed value cells (a cell of a text, identifier or enum kind
--   with a value, not of a packed source: SQLite's ``_INDEXED``) and their words, read through
--   the GIN index ``value_cells_words`` (built by ``ontology_load``: hours on the whole
--   store, so not here). ``row_no``: (src << 20) | field, the row number word matches are
--   paged and spanned by (SQLite's is the cell rowid: the same order but within one
--   value source, where SQLite's is the cells' insertion order).
-- * object_values_numbered: SQLite's view of the same name over the word-indexed cells,
--   with ``src`` and ``field`` to join word rows by their key.

CREATE FUNCTION ontology.sq_words(t text) RETURNS tsvector
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE COST 1000 AS $$
    SELECT array_to_tsvector(coalesce(array_agg(DISTINCT m[1]), '{}'))
      FROM regexp_matches(lower(normalize(regexp_replace(normalize(translate(left(t, 250000),
                                                                             'µ', 'μ'), NFD),
                                  '([A-Za-z\u00c0-\u024f])[\u0300-\u036f]+', '\1', 'g'), NFC)),
                          '[[:alnum:]\u00b2\u00b3\u00b9\u00bc-\u00be\u2070-\u209f\u2150-\u218f\ue000-\uf8ff]+',
                          'g') m
     WHERE octet_length(m[1]) <= 2046
$$;

CREATE TABLE ontology.packed_sources (src bigint PRIMARY KEY);

CREATE VIEW ontology.value_words AS SELECT
    (c.src << 20) | c.field AS row_no, c.src, c.field, ontology.sq_words(c.value) AS words
  FROM ontology.value_cells c
 WHERE c.value_num IS NULL AND c.value IS NOT NULL
   AND c.kind IN (SELECT kind FROM ontology.value_kinds
                   WHERE value_type IN ('text', 'identifier', 'enum'))
   AND NOT EXISTS (SELECT 1 FROM ontology.packed_sources p WHERE p.src = c.src);

CREATE VIEW ontology.object_values_numbered AS SELECT
    (t.src << 20) | t.field AS row_no, t.object_id, t.property, t.value_type, t.value,
    t.unit, t.quantity_role, t.currency, t.tax_basis, t.currency_citation, t.l1_handle,
    t.field_path, t.digest, t.lineage, t.binding_id, t.parser, t.authority_rank, t.markings,
    t.recorded_at, t.superseded_at, t.src, t.field
  FROM ontology.object_values_typed t
 WHERE t.value_num IS NULL AND t.value IS NOT NULL
   AND t.value_type IN ('text', 'identifier', 'enum');

-- * chunk_words: the document words (FTS5 ``chunk_text``, rowid = chunk_id): each text
--   chunk's distinct words as SQLite's own FTS5 tokenizer cuts them (``words.text_words``:
--   no difference from the SQLite store), written by the ``chunk_words`` load step and by
--   each delta beside its chunks, read through the GIN index ``chunk_words_words``.
CREATE TABLE ontology.chunk_words (chunk_id bigint PRIMARY KEY, words tsvector NOT NULL);
CREATE INDEX chunk_words_words ON ontology.chunk_words USING gin (words);
