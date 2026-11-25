-- 0081_insights_layout (INSIGHTS3, ASK-GENIE 17 customise): a person's own arrangement of
-- their Insights dashboard, kept per person (``owner``: their principals key, the same as
-- insights.insight.owner): the order of their tiles (``tile_order``, tile ids) and the tiles
-- they took off (``hidden``). Ids only; no business value or text is stored here. Whole-row
-- replace on every change. Additive: one new small table, no view, ontology row or link
-- changes, so no fingerprint or digest moves. Undo: drop the table.
CREATE SCHEMA IF NOT EXISTS insights;

CREATE TABLE IF NOT EXISTS insights.layout (
    owner       text PRIMARY KEY CHECK (owner <> ''),
    tile_order  text[] NOT NULL DEFAULT '{}',
    hidden      text[] NOT NULL DEFAULT '{}',
    updated_at  timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_api') THEN
        GRANT USAGE ON SCHEMA insights TO plx_api;
        GRANT SELECT, INSERT, UPDATE ON insights.layout TO plx_api;
    END IF;
END
$$;
