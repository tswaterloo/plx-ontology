-- 0072_precedent_vectors: one vector per precedent situation (ASSIST specs 2.5 step 5).
-- The spec stores them in search.chunk_vectors as kind 'precedent', but that store joins
-- search.chunks (it holds only text the search index serves), and a precedent's words are a
-- person's own (they must never surface in a search): so they have a table of their own in the
-- assist schema, which no reader role can read. Dimension 1024 (BAAI/bge-m3, the configured
-- embedder). Undo: DROP TABLE assist.precedent_vectors.
CREATE TABLE IF NOT EXISTS assist.precedent_vectors (
  situation_sha text NOT NULL, embed_model text NOT NULL, embedding halfvec(1024) NOT NULL,
  PRIMARY KEY (situation_sha, embed_model));
CREATE INDEX IF NOT EXISTS precedent_vectors_hnsw ON assist.precedent_vectors
  USING hnsw (embedding halfvec_cosine_ops);
REVOKE ALL ON assist.precedent_vectors FROM PUBLIC;
DO $pv$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'plx_eval') THEN
  GRANT SELECT ON assist.precedent_vectors TO plx_eval; END IF; END $pv$;
