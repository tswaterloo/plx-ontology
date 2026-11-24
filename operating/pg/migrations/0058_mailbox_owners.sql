-- 0058_mailbox_owners: the mailbox owners' addresses the mail flags keep (operating.pg.mail_owners),
-- made here so no reader role can select them: business_comms' own CREATE TABLE IF NOT EXISTS then
-- finds the table (its fingerprint is unchanged by this). The schema's default grants (0041) would
-- otherwise give plx_api and plx_eval SELECT on it; owner_role on biz.emails is what readers read.
-- A database without schema biz is left alone.
-- Undo: DROP TABLE biz.mailbox_owners (the next refresh that reads the capture makes and fills it).
DO $migration$
BEGIN
    IF to_regnamespace('biz') IS NULL THEN
        RETURN;
    END IF;
    CREATE TABLE IF NOT EXISTS biz.mailbox_owners (mailbox text PRIMARY KEY, addrs text[] NOT NULL);
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON biz.mailbox_owners FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        REVOKE ALL ON biz.mailbox_owners FROM plx_eval;
    END IF;
END
$migration$;
