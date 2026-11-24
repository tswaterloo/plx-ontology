-- 0075_comm_text_parts (ASK9, review minor on 0074): a message with more than one text field,
-- or a field whose word vectors pass the tsvector limit, is held in the sidecar whole, never
-- refused: biz.comm_text gets one row per (handle, field) and, for a field too large for one
-- vector, one row per piece. The key was the handle alone (0074).
--   part   0 for a field held in one vector; 1.. for the further pieces of a split field
--   whole  false on every piece of a split field: the search reads such a field the old way
--          (a word match inside one piece would miss words that sit in two pieces), so it
--          matches the same rows as the chunk word index; true otherwise
-- Metadata-only on the 254k Teams rows already built (a constant default adds no rewrite).
-- Undo: the 0074 undo (DROP both tables) still holds; or delete rows with part > 0 and re-add
-- PRIMARY KEY (handle) when no handle has two rows.
ALTER TABLE biz.comm_text DROP CONSTRAINT IF EXISTS comm_text_pkey;
ALTER TABLE biz.comm_text ADD COLUMN IF NOT EXISTS part int NOT NULL DEFAULT 0;
ALTER TABLE biz.comm_text ADD COLUMN IF NOT EXISTS whole boolean NOT NULL DEFAULT true;
CREATE UNIQUE INDEX IF NOT EXISTS comm_text_key
    ON biz.comm_text (handle, (coalesce(field_id, 0)), part);
CREATE INDEX IF NOT EXISTS comm_text_split ON biz.comm_text (field_id) WHERE NOT whole;
