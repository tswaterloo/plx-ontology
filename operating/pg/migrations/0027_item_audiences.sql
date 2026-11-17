-- 0027_item_audiences: the item gate's decisions as indexed principal keys (AUD-01).
--
-- The read tools' item gate (``OntologyTools._item_allowed``: the case runtime's
-- ``resolve_citation`` per reader) ran on Postgres in rounds: ``ontology.sq_call`` queued
-- each handle it had no answer for, Python answered, the statement ran again (up to
-- ``MAX_ROUNDS``). ``operating/pg/item_audiences.py`` now decides a handle once per
-- reader of a roster through that very gate and records the readers it allows:
--
-- * item_audiences: (handle, reader key) for each roster reader the gate allows.
-- * item_acl_state: one row per decided handle. ``roster`` names the reader set it was
--   decided for (``item_audience_rosters``), ``captured_at`` the oldest permission stamp
--   the gate read (``capture_age.capture_decides``) and ``valid_until`` when the first of
--   them passes its capture-age bound. A row without ``valid_until`` is never honoured:
--   only a decision a capture-age check bounds, of a scheme whose permission changes make
--   a new version (``item_audiences.RECORDABLE_SCHEMES``), is answered from these rows.
--   ``acl_digest``: the decision's digest (roster, allowed keys, stamps read), so an
--   unchanged decision is not rewritten.
-- * item_audience_rosters: (roster, reader key): which readers a roster decided for.
--
-- ``ontology.sq_item_gate(fn, handle)`` answers from these rows when the handle has a
-- state row of a roster that holds the reader's key and is still valid, else exactly as
-- ``sq_call`` did (rounds). So the tables may be empty (before the backfill), partly
-- filled or invalidated at any time: a handle without a usable state row is decided by
-- the gate in Python, never allowed by default. Additive; no existing table is touched.

CREATE TABLE IF NOT EXISTS ontology.item_audiences (
    l1_handle text NOT NULL,
    audience_key text NOT NULL,
    PRIMARY KEY (l1_handle, audience_key)
);
CREATE INDEX IF NOT EXISTS item_audiences_by_key
    ON ontology.item_audiences (audience_key, l1_handle);

CREATE TABLE IF NOT EXISTS ontology.item_acl_state (
    l1_handle text PRIMARY KEY,
    acl_digest text NOT NULL,
    captured_at timestamptz,
    valid_until timestamptz,
    roster text NOT NULL,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ontology.item_audience_rosters (
    roster text NOT NULL,
    audience_key text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (audience_key, roster)
);

-- The item gate with its recorded decisions: ``pg_temp.sq_gates`` (made by every
-- ``PgCompatConnection`` session that sees this function) holds, per registered gate, the
-- reader's keys and the rosters that hold them. No such row, no state row, a state row
-- of another roster, without or past its ``valid_until``: ``sq_call`` decides, as
-- before.
CREATE OR REPLACE FUNCTION ontology.sq_item_gate(fn text, arg text) RETURNS boolean
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    reader_keys text[];
    reader_rosters text[];
    state_roster text;
    state_until timestamptz;
BEGIN
    IF arg IS NULL THEN
        RETURN false;   -- the tools' gates deny what is no handle
    END IF;
    SELECT g.keys, g.rosters INTO reader_keys, reader_rosters
      FROM pg_temp.sq_gates g WHERE g.fn = sq_item_gate.fn;
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
