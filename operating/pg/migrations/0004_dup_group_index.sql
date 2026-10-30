-- 0004_dup_group_index: "also in" lookups by duplicate group (operating/search/copies.py).
--
-- A search hit that belongs to a duplicate group lists the group's other copies the reader
-- may open; without an index that lookup scans every chunk. Partial: most chunks have no
-- group. The lookup still applies the reader's permission filter.
CREATE INDEX IF NOT EXISTS chunks_dup_group ON search.chunks (dup_group)
    WHERE dup_group IS NOT NULL;
