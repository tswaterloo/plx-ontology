-- 0040_people_excluded_objects: the excluded people's own data, precomputed once per delta
-- (``operating/pg/people_excluded.py``), so a full reader (the owner and the configured
-- full readers, ``EmployeeEntitlement.unrestricted``) is filtered by one ``NOT EXISTS`` over
-- these rows inside each statement (``document_marks.sql``) and reads everything else with
-- no runtime permission check (owner rule 2026-11-17). Staff are never filtered by it.
-- * people_excluded_objects: one row per object version of an excluded person's data: an
--   object of their own unit (their mailbox, calendar or organizer unit; a unit whose owner
--   is no object id, which nothing can clear, too) or a message, chat message or event
--   whose sender, To/Cc/Bcc recipient, organizer or participant the participant rule
--   withholds (an excluded person, or a tenant identity nothing can decide). Its object
--   id, the version's handle and the reason ('own_unit', 'participant', or 'uncaptured': a
--   served mail, Teams or meetings version whose captured row the capture no longer holds,
--   which its item gate admits for nobody).
-- * people_excluded_state: the one row saying which exclusion list the rows were computed
--   for (``exclusion``: the one-way tag ``PeopleExclusion.members_checked_digest``), when,
--   and up to which served version (``covered_ver``: every object_versions.ver at or below
--   it was committed when the rows were computed). A reader whose list differs, or that
--   finds no row, uses none of them; a handle served at a version above covered_ver (mail,
--   Teams, meetings recorded since), or not served at all, keeps its per-item gate (fail
--   closed) until ``people_excluded update`` decides it and moves covered_ver up.
-- Written only by ``python3 -m operating.pg.people_excluded build`` (TRUNCATE + COPY in one
-- transaction) and ``... update`` (the new versions' handles replaced and covered_ver moved
-- in one transaction). No step reads it and no objects, value or link row changes: no
-- digest moves.
-- Also replaces ontology.sq_item_gate (0027) with the full reader's branch below.
-- Undo: TRUNCATE ontology.people_excluded_objects, ontology.people_excluded_state.
CREATE TABLE IF NOT EXISTS ontology.people_excluded_objects (
    l1_handle   text NOT NULL,
    object_id   text NOT NULL,
    reason      text NOT NULL CHECK (reason IN ('own_unit', 'participant', 'uncaptured')),
    PRIMARY KEY (l1_handle, object_id));   -- one handle may back several objects
-- A full reader's object filter probes it once per row (``document_marks.sql``): a hash
-- index (40 MB at the dry run's 1.37M ids of ~200 characters; a btree is 472 MB).
CREATE INDEX IF NOT EXISTS people_excluded_objects_by_object
    ON ontology.people_excluded_objects USING hash (object_id);
CREATE TABLE IF NOT EXISTS ontology.people_excluded_state (
    one         boolean PRIMARY KEY DEFAULT true CHECK (one),
    exclusion   text NOT NULL,
    built_at    text NOT NULL,
    objects     bigint NOT NULL,
    handles     bigint NOT NULL,
    covered_ver bigint NOT NULL,
    updated_at  text);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT SELECT ON ontology.people_excluded_objects TO plx_api;
        GRANT SELECT ON ontology.people_excluded_state TO plx_api;
    END IF;
END
$$;

-- The item gate for a full reader (``pg_temp.sq_gates`` keys = ARRAY['full:<exclusion tag>'],
-- ``people_excluded.reader_key``): a handle of a scheme the set decides whole (mail, Teams,
-- meetings) in a form the set decides (an item, or a mail attachment, its member or page;
-- ``people_excluded.DECIDED_FORM``; never a person citation, ``PERSON_FORM``) is answered
-- here, with no Python round: not the set's
-- (the handle, the handle without its page, the message before its ``#``). Only while the
-- state row is of that very exclusion list and the handle is covered
-- (``people_excluded.Excluded.covered``): the handle or the handle without its page is
-- served at a version at or below covered_ver, and none of the handle, that page form or
-- its message is served at a newer one. Every other scheme, a stale set, or a handle
-- recorded since goes on as 0027's function did.
CREATE OR REPLACE FUNCTION ontology.sq_item_gate(fn text, arg text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    reader_keys text[];
    reader_rosters text[];
    state_roster text;
    state_until timestamptz;
    covered bigint;
    bare text;
BEGIN
    IF arg IS NULL THEN
        RETURN false;   -- the tools' gates deny what is no handle
    END IF;
    SELECT g.keys, g.rosters INTO reader_keys, reader_rosters
      FROM pg_temp.sq_gates g WHERE g.fn = sq_item_gate.fn;
    IF FOUND AND cardinality(reader_keys) = 1 AND reader_keys[1] LIKE 'full:%' THEN
        IF split_part(arg, '://', 1) = ANY (ARRAY['graph-mail', 'teams-chat', 'teams-channel',
                'calendar-event', 'planner-task', 'meeting-transcript'])
                AND arg ~ '^[^#]*(#attachment=[^#]*)?$'
                AND arg !~ '^graph-mail://[^/#]+/person/' THEN
            SELECT s.covered_ver INTO covered FROM ontology.people_excluded_state s
             WHERE 'full:' || s.exclusion = reader_keys[1];
            bare := regexp_replace(arg, '[#&]page=[0-9]+$', '');
            IF covered IS NOT NULL
                    AND EXISTS (SELECT 1 FROM ontology.object_versions v
                                 WHERE v.l1_handle IN (arg, bare) AND v.ver <= covered)
                    AND NOT EXISTS (SELECT 1 FROM ontology.object_versions v
                                     WHERE v.l1_handle IN (arg, bare, split_part(arg, '#', 1))
                                       AND v.ver > covered) THEN
                RETURN NOT EXISTS (SELECT 1 FROM ontology.people_excluded_objects e
                    WHERE e.l1_handle IN (arg, bare, split_part(arg, '#', 1)));
            END IF;
        END IF;
        RETURN ontology.sq_call(fn, arg);
    END IF;
    IF FOUND AND cardinality(reader_keys) > 0 AND cardinality(reader_rosters) > 0 THEN
        SELECT s.roster, s.valid_until INTO state_roster, state_until
          FROM ontology.item_acl_state s WHERE s.l1_handle = arg;
        IF FOUND AND state_roster = ANY (reader_rosters)
                AND state_until IS NOT NULL AND state_until > clock_timestamp() THEN
            RETURN EXISTS (SELECT 1 FROM ontology.item_audiences a
                            WHERE a.l1_handle = arg AND a.audience_key = ANY (reader_keys));
        END IF;
    END IF;
    RETURN ontology.sq_call(fn, arg);
END
$$;
