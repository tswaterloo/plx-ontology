-- 0039_value_cells_dates: a partial btree over the date-shaped value cells, newest first.
-- "Latest emails", "newest purchase orders this week" (operating/fast_listing.py) read the
-- records of one type by their dating field, newest first: without an index on the value,
-- Postgres reads every cell of the field (3.3M received_at cells: 9.4 s cold, ~1 s warm,
-- 11-17); with this one it walks the index backward and stops after a page. Only values
-- shaped as an ISO date or timestamp are held (about 2% of value_cells, ~10M rows); the
-- list statement repeats this predicate exactly so the planner proves the index applies.
--
-- Created here only while value_cells is EMPTY (a fresh or test database: instant). A
-- deploy applies migrations in one transaction before the release switch; a plain CREATE
-- INDEX over the served 492M-row table would hold a SHARE lock blocking every value write
-- for many minutes. The served database builds it with CREATE INDEX CONCURRENTLY, outside
-- any deploy: `python3 -m operating.pg.date_index build` (idempotent; status / drop too).
-- Without it the list still answers warm; a cold read past its 3 s budget hands the
-- question to the agent.
DO $$
BEGIN
    IF to_regclass('ontology.value_cells') IS NULL THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM ontology.value_cells LIMIT 1) THEN
        RETURN;
    END IF;
    CREATE INDEX IF NOT EXISTS value_cells_dates ON ontology.value_cells (field, value)
        WHERE value ~ '^[12][0-9]{3}-[01][0-9]-[0-3][0-9]';
END
$$;
