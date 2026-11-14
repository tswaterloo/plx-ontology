-- GROWTH-AUDIT-1111 F7: the byte-order twins of the key indexes go (10.7 GB on the served
-- database, one index fewer written on every delta apply and step). link_rows_by_id
-- (0009, link_id COLLATE "C") and object_versions_by_object_c (made by hand on the
-- served database, never declared) were built for the key range reads, which cut keys in
-- byte order. Where the database's default collation is byte order (libc C/POSIX/C.UTF-8,
-- builtin C/C.UTF-8: the served one is libc C.UTF-8, checked on 168k link and 90k object
-- key pairs and non-ASCII probes) those reads name no collation
-- (ontology.pg_byte_order.key_collate) and plan on link_rows_pkey and
-- object_versions_by_object, measured live at the same cost. Anywhere else (the same test
-- as key_collate: the default collation and the key columns using it) the reads still name
-- COLLATE "C", so the twins stay: dropping them there would leave every range read
-- unindexed. The live database drops them with DROP INDEX CONCURRENTLY after the code
-- reading the keys plainly is deployed (devloop/DECISIONS/2026-11-12-f7-byte-order-key-indexes.md);
-- IF EXISTS makes this a no-op there.
DO $f7$
BEGIN
    IF (SELECT CASE d.datlocprovider::text
                   WHEN 'c' THEN d.datcollate IN ('C', 'POSIX', 'C.UTF-8', 'C.utf8')
                   WHEN 'b' THEN (to_jsonb(d) ->> 'datlocale') IN ('C', 'C.UTF-8')
                   ELSE false END
        FROM pg_database d WHERE d.datname = current_database())
       AND (SELECT count(*) FROM pg_attribute a
            WHERE a.attnum > 0 AND NOT a.attisdropped AND a.attcollation = 100
              AND (a.attrelid, a.attname) IN (
                  (to_regclass('ontology.link_rows'), 'link_id'),
                  (to_regclass('ontology.object_versions'), 'object_id'),
                  (to_regclass('ontology.events'), 'event_id'))) = 3
    THEN
        DROP INDEX IF EXISTS ontology.link_rows_by_id;
        DROP INDEX IF EXISTS ontology.object_versions_by_object_c;
    END IF;
END
$f7$;
