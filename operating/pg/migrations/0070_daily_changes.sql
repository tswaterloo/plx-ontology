-- 0070_daily_changes (Ask 14.1 h10, plan D9): what each system created, changed and removed per
-- day, by object type, kept incrementally from ontology.object_versions
-- (operating/pg/daily_changes.py).
-- * biz.daily_changes_objects: the counts per (day, object): created, changed (a new version of
--   an object that had one), removed. Advanced by object_versions.ver (biz.daily_changes_mark);
--   a day's rows of an object never change once counted. No permission is kept here: it is
--   rebuilt into the served table from the objects AS THEY ARE NOW on every run.
-- * biz.daily_changes: the served counts per (day, system, object type, audience key, markings,
--   owner-only) with the access class of those records now (biz.access_classes, the ids every
--   business view uses): the guarded view biz_sql.daily_changes shows a reader the groups of
--   the classes the reader holds. A record whose audience narrowed or whose owner was added to
--   the people exclusion moves to its new class at the next run. A version whose object row is
--   missing is counted as system 'unknown' under the owner-only class and reported.
-- Reader roles are revoked on all three tables.
CREATE TABLE IF NOT EXISTS biz.daily_changes_objects (
    day       date    NOT NULL,
    object_id text    NOT NULL,
    created   integer NOT NULL DEFAULT 0,
    changed   integer NOT NULL DEFAULT 0,
    removed   integer NOT NULL DEFAULT 0,
    PRIMARY KEY (day, object_id));
CREATE TABLE IF NOT EXISTS biz.daily_changes (
    day          date    NOT NULL,
    system       text    NOT NULL,
    object_type  text    NOT NULL,
    audience_key text    NOT NULL,
    markings     text    NOT NULL,
    owner_only   boolean NOT NULL,
    created      bigint  NOT NULL DEFAULT 0,
    changed      bigint  NOT NULL DEFAULT 0,
    removed      bigint  NOT NULL DEFAULT 0,
    access_class integer NOT NULL,
    col_class    integer NOT NULL DEFAULT 0,
    ai_ok        boolean NOT NULL DEFAULT true,
    handle       text    NOT NULL,
    PRIMARY KEY (day, system, object_type, audience_key, markings, owner_only));
CREATE INDEX IF NOT EXISTS daily_changes_day ON biz.daily_changes (day);
CREATE TABLE IF NOT EXISTS biz.daily_changes_mark (
    id         integer PRIMARY KEY CHECK (id = 1),
    ver        bigint  NOT NULL DEFAULT 0,
    head       bigint,
    orphans    bigint  NOT NULL DEFAULT 0,
    updated_at timestamptz);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        REVOKE ALL ON biz.daily_changes_objects, biz.daily_changes, biz.daily_changes_mark FROM plx_api;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
        REVOKE ALL ON biz.daily_changes_objects, biz.daily_changes, biz.daily_changes_mark FROM plx_eval;
    END IF;
END $$;
