"""Declared schema of the session_replay store (see ``operating.declared_schema``)."""

_REPLAY_DDL = """
CREATE TABLE IF NOT EXISTS ticket_jti (
  jti_sha256 TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('used', 'revoked')),
  exp INTEGER NOT NULL,
  PRIMARY KEY (jti_sha256, kind)
);
"""

STORE = "session_replay"
STEPS = ((1, "initial", (_REPLAY_DDL,)),)
