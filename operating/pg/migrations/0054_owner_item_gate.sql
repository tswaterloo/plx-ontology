-- 0054_owner_item_gate: the owner's item-gate key (owner decision 2026-11-21: the two
-- excluded people's mail, chats and meetings are the owner's alone, never removed). A gate
-- held with the one key ``owner:<exclusion tag>`` (``operating.pg.people_excluded.
-- reader_key`` for the owner, ``EmployeeEntitlement.owner``) decides a covered mail, Teams
-- or meetings handle served: the full reader's branch of 0040 / 0049 with no set check.
-- A handle the set has not covered, or a set of another list, goes on to the Python gate
-- exactly as a full reader's does (fail closed). Every other key is answered as before.
-- Undo: re-run the two function bodies of 0040 (sq_item_gate) and 0049 (sq_item_gate_s).

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
    IF FOUND AND cardinality(reader_keys) = 1
            AND (reader_keys[1] LIKE 'full:%' OR reader_keys[1] LIKE 'owner:%') THEN
        IF split_part(arg, '://', 1) = ANY (ARRAY['graph-mail', 'teams-chat', 'teams-channel',
                'calendar-event', 'planner-task', 'meeting-transcript'])
                AND arg ~ '^[^#]*(#attachment=[^#]*)?$'
                AND arg !~ '^graph-mail://[^/#]+/person/' THEN
            SELECT s.covered_ver INTO covered FROM ontology.people_excluded_state s
             WHERE reader_keys[1] IN ('full:' || s.exclusion, 'owner:' || s.exclusion);
            bare := regexp_replace(arg, '[#&]page=[0-9]+$', '');
            IF covered IS NOT NULL
                    AND EXISTS (SELECT 1 FROM ontology.object_versions v
                                 WHERE v.l1_handle IN (arg, bare) AND v.ver <= covered)
                    AND NOT EXISTS (SELECT 1 FROM ontology.object_versions v
                                     WHERE v.l1_handle IN (arg, bare, split_part(arg, '#', 1))
                                       AND v.ver > covered) THEN
                IF reader_keys[1] LIKE 'owner:%' THEN
                    RETURN true;   -- the owner: the set withholds nothing
                END IF;
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

CREATE OR REPLACE FUNCTION ontology.sq_item_gate_s(fn text, arg text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    gate text;
    first_key text;
    state_roster text;
    state_until timestamptz;
    covered bigint;
    bare text;
BEGIN
    IF arg IS NULL THEN
        RETURN false;   -- the tools' gates deny what is no handle
    END IF;
    gate := current_setting('plx_sq.g' || md5(fn), true);
    IF gate IS NULL OR left(gate, length(fn) + 1) IS DISTINCT FROM fn || chr(31) THEN
        RETURN ontology.sq_call_s(fn, arg);   -- none held (or cleared)
    END IF;
    first_key := substr(gate, length(fn) + 2);
    IF first_key LIKE 'full:%' OR first_key LIKE 'owner:%' THEN
        IF split_part(arg, '://', 1) = ANY (ARRAY['graph-mail', 'teams-chat', 'teams-channel',
                'calendar-event', 'planner-task', 'meeting-transcript'])
                AND arg ~ '^[^#]*(#attachment=[^#]*)?$'
                AND arg !~ '^graph-mail://[^/#]+/person/' THEN
            SELECT s.covered_ver INTO covered FROM ontology.people_excluded_state s
             WHERE first_key IN ('full:' || s.exclusion, 'owner:' || s.exclusion);
            bare := regexp_replace(arg, '[#&]page=[0-9]+$', '');
            IF covered IS NOT NULL
                    AND EXISTS (SELECT 1 FROM ontology.object_versions v
                                 WHERE v.l1_handle IN (arg, bare) AND v.ver <= covered)
                    AND NOT EXISTS (SELECT 1 FROM ontology.object_versions v
                                     WHERE v.l1_handle IN (arg, bare, split_part(arg, '#', 1))
                                       AND v.ver > covered) THEN
                IF first_key LIKE 'owner:%' THEN
                    RETURN true;   -- the owner: the set withholds nothing
                END IF;
                RETURN NOT EXISTS (SELECT 1 FROM ontology.people_excluded_objects e
                    WHERE e.l1_handle IN (arg, bare, split_part(arg, '#', 1)));
            END IF;
        END IF;
        RETURN ontology.sq_call_s(fn, arg);
    END IF;
    SELECT s.roster, s.valid_until INTO state_roster, state_until
      FROM ontology.item_acl_state s WHERE s.l1_handle = arg;
    IF FOUND AND current_setting('plx_sq.r' || md5(fn || chr(31) || state_roster), true)
                     = fn || chr(31) || state_roster
            AND state_until IS NOT NULL AND state_until > clock_timestamp() THEN
        RETURN EXISTS (SELECT 1 FROM ontology.item_audiences a
                        WHERE a.l1_handle = arg
                          AND current_setting('plx_sq.k' || md5(fn || chr(31) || a.audience_key),
                                              true) = fn || chr(31) || a.audience_key);
    END IF;
    RETURN ontology.sq_call_s(fn, arg);
END
$$;
