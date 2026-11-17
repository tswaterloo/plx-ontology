-- 0026_change_feed: one change feed of the applied cycles (operating/pg/change_feed.py).
--
-- * change_cycles: one row per applied cycle (a delta layer, a whole-steps run, a schema
--   or step migration, a link apply or undo, a people-exclusion purge): its name (the load_state key it is recorded under), the generation it
--   applied over, when it committed, and its feed rows' first and last seq.
-- * change_feed: the records a cycle really changed, in apply order (``seq``, the readers'
--   cursor): kind n (new), c (changed), r (removed), l (only a link at it changed), m (a
--   migration rewrote every record of the scope prefix in object_type; object_id '*').
--   A record a layer re-owns unchanged gets no row. Written in the apply's own
--   transaction, under the delta's advisory lock, so rows commit in seq order.
-- * change_cursors: each consumer's position (the last seq it handled); the row
--   '_pruned' is the newest seq pruned: a cursor below it has missed rows (a gap) and its
--   consumer reads its own way once, as before the feed existed.
-- Rows every active consumer passed, and rows older than the keep window, are pruned by
-- the writer. Not part of a generation: an ontology swap or reload leaves these tables
-- alone, and readers filter by generation.

CREATE TABLE IF NOT EXISTS ontology.change_cycles (
    cycle_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name       text NOT NULL,
    source     text NOT NULL CHECK (source IN ('delta', 'whole_steps', 'migration', 'links',
                                               'purge')),
    generation text,
    applied_at timestamptz NOT NULL DEFAULT now(),
    first_seq  bigint,
    last_seq   bigint,
    rows       integer NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS change_cycles_by_last_seq ON ontology.change_cycles (last_seq);

CREATE TABLE IF NOT EXISTS ontology.change_feed (
    seq         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cycle_id    bigint NOT NULL,
    object_id   text NOT NULL,
    object_type text,
    kind        text NOT NULL CHECK (kind IN ('n', 'c', 'r', 'l', 'm')));

CREATE TABLE IF NOT EXISTS ontology.change_cursors (
    consumer   text PRIMARY KEY CHECK (consumer ~ '^_?[a-z][a-z0-9_]{0,62}$'),
    position   bigint NOT NULL CHECK (position >= 0),
    updated_at timestamptz NOT NULL DEFAULT now());

ALTER TABLE ontology.change_feed SET (autovacuum_vacuum_scale_factor = 0.05);
