-- 0038_own_issued_documents: PLX's own sales documents (Sage OE invoices and order
-- confirmations) the served store holds as supplier invoices or confirmations, kept from
-- every reader until the fields-mig migration removes them (``operating/pg/own_issued.py``).
-- * One row per hidden object: its id and type, its document's digest and the evidence
--   (the record-line forms and the title-cue pages). Written only by
--   ``python3 -m operating.pg.own_issued build`` (TRUNCATE + COPY in one transaction); no
--   step reads it and no objects, value or link row changes, so no digest moves.
-- * Readers (plx_api) read it once per process (``own_issued.hidden_ids``) and withhold the
--   ids as ``operating.search.document_marks`` withholds a marked document.
-- * ``ontology.own_issued_judged``: every document digest judged so far and whether it is
--   PLX's own, so ``refresh`` (``plx-own-issued.timer``) judges only the documents the
--   delta added since (one row per digest, a few hundred thousand at most).
-- Undo: TRUNCATE ontology.own_issued_documents, ontology.own_issued_judged.
CREATE TABLE IF NOT EXISTS ontology.own_issued_documents (
    object_id      text PRIMARY KEY,
    object_type    text NOT NULL CHECK (object_type IN ('SupplierInvoice', 'SupplierConfirmation')),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    evidence       text NOT NULL,
    detected_at    text NOT NULL);
CREATE INDEX IF NOT EXISTS own_issued_documents_by_sha
    ON ontology.own_issued_documents (content_sha256);
CREATE TABLE IF NOT EXISTS ontology.own_issued_judged (
    content_sha256 text PRIMARY KEY CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    own            boolean NOT NULL,
    evidence       text,
    judged_at      text NOT NULL);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON ontology.own_issued_documents TO plx_api;
        GRANT SELECT ON ontology.own_issued_judged TO plx_api;
    END IF;
END
$$;
