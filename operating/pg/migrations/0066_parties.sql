-- 0066_parties: contract C7 (master plan 4.3, specs M5), identity of customers and suppliers
-- across systems: one party per real company, its members (rows of biz.customers /
-- biz.suppliers, by handle) with the basis and evidence of each membership, and the pairs that
-- look alike but are not decided (never merged by name alone). Written by
-- operating/pg/parties.py only (one transaction per build); no view table, ontology row or link
-- changes: no print or digest moves. Undo: DROP TABLE biz.parties, biz.party_members,
-- biz.party_candidates.
-- * parties.name is taken from one member row; access_class / col_class are that row's, so a
--   reader sees the name exactly when it may read that member (a party never widens a door).
CREATE TABLE IF NOT EXISTS biz.parties (
    party_id     bigint PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('customer', 'supplier')),
    name         text,
    chain_id     bigint,
    access_class integer,
    col_class    integer);
CREATE TABLE IF NOT EXISTS biz.party_members (
    handle     text PRIMARY KEY,
    party_id   bigint NOT NULL,
    system     text,
    basis      text NOT NULL,
    tier       text NOT NULL CHECK (tier IN ('code', 'verified', 'rule', 'model', 'person')),
    evidence   jsonb NOT NULL DEFAULT '{}'::jsonb,
    decided_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS party_members_party ON biz.party_members (party_id);
CREATE TABLE IF NOT EXISTS biz.party_candidates (
    handle_a   text NOT NULL,
    handle_b   text NOT NULL,
    evidence   jsonb NOT NULL DEFAULT '{}'::jsonb,
    state      text NOT NULL DEFAULT 'undecided'
               CHECK (state IN ('undecided', 'merged', 'rejected')),
    decided_by text,
    PRIMARY KEY (handle_a, handle_b));
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON biz.parties, biz.party_members, biz.party_candidates FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        REVOKE ALL ON biz.parties, biz.party_members, biz.party_candidates FROM plx_eval;
    END IF;
END $$;
