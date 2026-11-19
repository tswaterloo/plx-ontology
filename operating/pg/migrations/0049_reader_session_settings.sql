-- 0049_reader_session_settings: the reader's gate state held in session settings, for a
-- session that cannot make temporary tables (a hot standby; operating/pg/sqlite_compat.py
-- ``stateless``). Same answers as ``ontology.sq_call`` (0008) and ``ontology.sq_item_gate``
-- (0040), read from settings in place of ``pg_temp.sq_calls`` / ``sq_pending`` /
-- ``sq_gates``; a setting is changed with ``set_config``, which a read-only transaction and
-- a standby allow. Each value carries what it is for (fn, chr(31), arg or key), compared on
-- every read, so a name collision is a miss (asked again), never another call's answer.
--   plx_sq.a<md5(fn chr(31) arg)>   = 't'|'f' chr(31) fn chr(31) arg   an answered call
--   plx_sq.q<md5(fn chr(31) arg)>   = '1'                              queued (cleared on answer)
--   plx_sq.pn                       = n; plx_sq.p<i> = fn chr(31) arg  the queue, i = 1..n
--   plx_sq.g<md5(fn)>               = fn chr(31) its key ('' if it holds more)  a recorded gate
--   plx_sq.k<md5(fn chr(31) key)>   = fn chr(31) key                   its reader keys
--   plx_sq.r<md5(fn chr(31) roster)> = fn chr(31) roster               its rosters
-- A cleared setting is ''. The temporary-table functions stay for every other session.
-- Undo: DROP FUNCTION ontology.sq_item_gate_s(text, text);
--       DROP FUNCTION ontology.sq_call_s(text, text);

CREATE OR REPLACE FUNCTION ontology.sq_call_s(fn text, arg text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    call text;
    k text;
    v text;
    n integer;
BEGIN
    IF arg IS NULL THEN
        RETURN false;   -- the tools' gates deny what is no handle
    END IF;
    call := fn || chr(31) || arg;
    k := md5(call);
    v := current_setting('plx_sq.a' || k, true);
    IF v = 't' || chr(31) || call THEN
        RETURN true;
    ELSIF v = 'f' || chr(31) || call THEN
        RETURN false;
    END IF;
    IF coalesce(current_setting('plx_sq.q' || k, true), '') = '' THEN
        n := coalesce(nullif(current_setting('plx_sq.pn', true), ''), '0')::integer + 1;
        PERFORM set_config('plx_sq.pn', n::text, false);
        PERFORM set_config('plx_sq.p' || n, call, false);
        PERFORM set_config('plx_sq.q' || k, '1', false);
    END IF;
    RETURN NULL;
END
$$;

-- 0040's ``sq_item_gate`` with its reader keys and rosters read from the settings above:
-- ``reader_keys`` held = the gate's setting; ``cardinality(reader_keys) = 1 AND
-- reader_keys[1] LIKE 'full:%'`` = the gate's only key (set when it holds just one) is a full
-- reader's;
-- ``audience_key = ANY (reader_keys)`` / ``roster = ANY (reader_rosters)`` = that key's /
-- roster's setting is held (a roster gate is registered only with keys and rosters).
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
    IF first_key LIKE 'full:%' THEN
        IF split_part(arg, '://', 1) = ANY (ARRAY['graph-mail', 'teams-chat', 'teams-channel',
                'calendar-event', 'planner-task', 'meeting-transcript'])
                AND arg ~ '^[^#]*(#attachment=[^#]*)?$'
                AND arg !~ '^graph-mail://[^/#]+/person/' THEN
            SELECT s.covered_ver INTO covered FROM ontology.people_excluded_state s
             WHERE 'full:' || s.exclusion = first_key;
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
