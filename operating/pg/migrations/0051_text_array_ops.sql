-- 0051_text_array_ops: ``@>`` and ``&&`` on (text[], text[]) that a guarded view's filter
-- may run before the view's guard, so a filter on a guarded view's array column
-- (emails.records, domains, customers, suppliers, addresses; the Teams views' arrays)
-- reads the column's GIN index. Every guarded view is ``security_barrier``: a reader's
-- filter calling a function not marked leakproof runs only after the guard, over every row
-- the guard admits (live 11-21: ``emails WHERE records @> ARRAY['bc://..']`` 21.7 s
-- through plx.sql; 7 ms on the table by emails_next_records_idx).
-- Only text[]: the polymorphic pg_catalog.arraycontains/arrayoverlap stay NOT LEAKPROOF.
-- Marking those would vouch for every element type's equality: record_eq raises "could not
-- identify an equality operator for type json" only when the columns before a json one
-- match, so ``ARRAY[ROW(col, '{}'::json)] @> ARRAY[ROW('guess', '{}'::json)]`` run before
-- the guard would tell a hidden row's value by its error (review of 6e847776f7). Over
-- text[] the element equality is texteq (leakproof); these return a boolean and raise no
-- error that depends on a row's values. A reader's ``text[] @> text[]`` resolves to the
-- exact-type operator here (the plx.sql door puts plx_ops on its search_path,
-- ``ontology_tools_sql``); any other array type, a record array included, still resolves
-- to pg_catalog's, which runs after the guard as before. The GIN array_ops family holds
-- them (strategies 1 overlap, 2 contains), so the columns' existing indexes serve them.
-- Needs a superuser (LEAKPROOF, the builtin family): anyone else fails the migration loudly
-- rather than recording it as applied. pg_dump does not carry members added to a builtin
-- family: after a restore the filters are slow (same rows) until this is made again.
-- Undo: ALTER OPERATOR FAMILY array_ops USING gin DROP OPERATOR 1 (text[], text[]),
--       DROP OPERATOR 2 (text[], text[]); DROP SCHEMA plx_ops CASCADE;

DO $$
BEGIN
    IF NOT (SELECT rolsuper FROM pg_catalog.pg_roles WHERE rolname = current_user) THEN
        RAISE EXCEPTION '0051_text_array_ops needs a superuser (LEAKPROOF, GIN array_ops)';
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS plx_ops;
GRANT USAGE ON SCHEMA plx_ops TO PUBLIC;

CREATE OR REPLACE FUNCTION plx_ops.text_array_contains(text[], text[]) RETURNS boolean
    LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF AS 'arraycontains';
CREATE OR REPLACE FUNCTION plx_ops.text_array_overlap(text[], text[]) RETURNS boolean
    LANGUAGE internal IMMUTABLE STRICT PARALLEL SAFE LEAKPROOF AS 'arrayoverlap';

CREATE OPERATOR plx_ops.@> (LEFTARG = text[], RIGHTARG = text[],
    FUNCTION = plx_ops.text_array_contains, RESTRICT = contsel, JOIN = contjoinsel);
CREATE OPERATOR plx_ops.&& (LEFTARG = text[], RIGHTARG = text[],
    FUNCTION = plx_ops.text_array_overlap, COMMUTATOR = OPERATOR(plx_ops.&&),
    RESTRICT = contsel, JOIN = contjoinsel);

ALTER OPERATOR FAMILY pg_catalog.array_ops USING gin ADD
    OPERATOR 1 plx_ops.&& (text[], text[]),
    OPERATOR 2 plx_ops.@> (text[], text[]);
