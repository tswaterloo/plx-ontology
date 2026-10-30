"""
The schema is the migrations (``operating/pg/migrations/*.sql``, in order) when the
tree has them, else the copy below of the contract schema (CONTRACT.md). Embeddings come from
``HashEmbedder``: a deterministic 64-dimension bag of words and character trigrams, so
tests never load a 2 GB model and a misspelt word still lands near its word.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Sequence

DIM = 64
MODEL = "test:hash-64"
ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE SCHEMA IF NOT EXISTS search;
CREATE TABLE search.chunks (
    chunk_id text PRIMARY KEY, source_handle text NOT NULL, object_id text NULL,
    kind text NOT NULL, title text, author_role text, occurred_at timestamptz NULL,
    header text NOT NULL, body text NOT NULL, content_sha text NOT NULL,
    linked_ids text[] NOT NULL DEFAULT '{}', audience_key text NOT NULL,
    markings text[] NOT NULL DEFAULT '{}', dup_group text NULL,
    authority smallint NOT NULL CHECK (authority BETWEEN 1 AND 4),
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english'::regconfig, header), 'A')
        || setweight(to_tsvector('english'::regconfig, body), 'D')
        || setweight(to_tsvector('simple'::regconfig, header), 'A')
        || setweight(to_tsvector('simple'::regconfig, body), 'D')) STORED,
    embedding halfvec({{embed_dim}}) NULL, embed_model text NULL,
    updated_at timestamptz NOT NULL);
CREATE INDEX chunks_tsv_gin ON search.chunks USING gin (tsv);
CREATE INDEX chunks_embedding_hnsw ON search.chunks
    USING hnsw (embedding halfvec_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX chunks_audience_key ON search.chunks (audience_key);
CREATE INDEX chunks_linked_ids_gin ON search.chunks USING gin (linked_ids);
CREATE TABLE search.query_log (id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(),
    actor text, question text, intent text, entities jsonb, rewrites jsonb, results jsonb,
    timings jsonb);
"""
# Harmless when it exists.
EXTRA = "CREATE INDEX IF NOT EXISTS chunks_header_trgm ON search.chunks USING gin (header gin_trgm_ops);"


def _tokens(text: str) -> list[str]:
    words = re.findall(r"\w+", text.lower())
    grams = [w[i:i + 3] for w in words if len(w) > 3 for i in range(len(w) - 2)]
    return words + grams


class HashEmbedder:
    model = MODEL

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for tok in _tokens(text):
                h = hashlib.sha256(tok.encode()).digest()
                vec[h[0] % DIM] += 1.0 if h[1] % 2 else -1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


def schema_sql() -> str:
    migrations = sorted((ROOT / "operating" / "pg" / "migrations").glob("[0-9]*.sql"))
    if migrations:
        text = "\n".join(m.read_text(encoding="utf-8") for m in migrations)
        text = text.replace("{{embed_model}}", "'" + MODEL + "'")
    else:
        text = SCHEMA
    return text.replace("{{embed_dim}}", str(DIM)) + "\n" + EXTRA


class FixtureDb:
    """``with FixtureDb() as db:`` creates ``plx_test_d_<pid>_<n>``, applies the schema,
    and drops it on exit. Never touches the ``plx`` database."""
    _n = 0

    def __init__(self) -> None:
        FixtureDb._n += 1
        self.name = f"plx_test_d_{os.getpid()}_{FixtureDb._n}"
        self.dsn = f"postgresql:///{self.name}"

    def __enter__(self) -> "FixtureDb":
        assert self.name != "plx"
        _run("createdb", self.name)
        import psycopg
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(schema_sql())
        return self

    def __exit__(self, *exc: Any) -> None:
        _run("dropdb", "--if-exists", "--force", self.name)

    def add(self, chunks: Sequence[dict[str, Any]], embedder: Any = None) -> None:
        import psycopg
        embedder = embedder or HashEmbedder()
        vectors = embedder.embed([c["header"] + "\n" + c["body"] for c in chunks])
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for chunk, vec in zip(chunks, vectors):
                row = dict(chunk)
                row.setdefault("object_id", None)
                row.setdefault("title", None)
                row.setdefault("author_role", None)
                row.setdefault("occurred_at", None)
                row.setdefault("linked_ids", [])
                row.setdefault("markings", [])
                row.setdefault("dup_group", None)
                row["content_sha"] = hashlib.sha256(
                    (row["header"] + row["body"]).encode()).hexdigest()
                row["embedding"] = "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
                row["embed_model"] = getattr(embedder, "model", MODEL)
                conn.execute(
                    """INSERT INTO search.chunks (chunk_id, source_handle, object_id, kind,
                        title, author_role, occurred_at, header, body, content_sha,
                        linked_ids, audience_key, markings, dup_group, authority,
                        embedding, embed_model, updated_at)
                       VALUES (%(chunk_id)s, %(source_handle)s, %(object_id)s, %(kind)s,
                        %(title)s, %(author_role)s, %(occurred_at)s, %(header)s, %(body)s,
                        %(content_sha)s, %(linked_ids)s, %(audience_key)s, %(markings)s,
                        %(dup_group)s, %(authority)s, %(embedding)s::halfvec,
                        %(embed_model)s, now())""", row)


def _t(days_ago: int) -> dt.datetime:
    return dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc) - dt.timedelta(days=days_ago)


NOW = _t(0)
# Mailbox units are keyed by the owner's directory object id (the people exclusion
# drops any other mail key). EXCLUDED is on the exclusion list the tests set.
PURCHASING = "mail:aaaaaaaa-0000-4000-8000-000000000001"
FINANCE = "mail:aaaaaaaa-0000-4000-8000-000000000002"
EXCLUDED = "99999999-9999-4999-8999-999999999999"
QUALITY = "sharepoint:quality-site/documents"
BC = "bc:company-a/purchaseOrders"


def chunks() -> list[dict[str, Any]]:
    """Invented business text: a late soap order from a Canadian supplier, its PO in BC,
    a quality SOP, a finance-only margin thread, a duplicate forwarded email, filler."""
    rows = [
        dict(chunk_id="m1#0", source_handle="graph-mail://purchasing/msg-1",
             object_id="EmailMessage:graph:msg-1", kind="mail",
             title="Re: PO 9001 liquid soap shipment delayed", author_role="buyer",
             occurred_at=_t(3), linked_ids=["PurchaseOrder:bc:9001", "9001"],
             header="Email, purchasing thread 'Re: PO 9001 liquid soap shipment delayed', "
                    "23 Oct 2026, buyer. PO 9001, supplier Maple Suds Co. (Toronto, Canada).",
             body="Maple Suds told us the castile soap pallets slipped two weeks because "
                  "the screen print artwork for the bottle labels was not approved. New ship "
                  "date is 6 Nov.", audience_key=PURCHASING, authority=2,
             dup_group="dup-a"),
        dict(chunk_id="m2#0", source_handle="graph-mail://finance/msg-1-fwd",
             object_id="EmailMessage:graph:msg-1-fwd", kind="mail",
             title="Fwd: Re: PO 9001 liquid soap shipment delayed", author_role="buyer",
             occurred_at=_t(3), linked_ids=["PurchaseOrder:bc:9001", "9001"],
             header="Email, forwarded copy of 'Re: PO 9001 liquid soap shipment delayed', "
                    "23 Oct 2026. PO 9001, supplier Maple Suds Co.",
             body="Maple Suds told us the castile soap pallets slipped two weeks because "
                  "the screen print artwork for the bottle labels was not approved. New ship "
                  "date is 6 Nov.", audience_key=PURCHASING, authority=2,
             dup_group="dup-a"),
        dict(chunk_id="po9001#0", source_handle="bc://prod/company-a/purchaseOrders/9001",
             object_id="PurchaseOrder:bc:9001", kind="bc_text",
             title="Purchase order 9001", occurred_at=_t(30),
             linked_ids=["PurchaseOrder:bc:9001", "9001", "Vendor:bc:maple-suds"],
             header="Business Central purchase order 9001, vendor Maple Suds Co., "
                    "expected receipt 23 Oct 2026.",
             body="Purchase order 9001 for liquid castile soap, 40 pallets, vendor Maple "
                  "Suds Co., Toronto. Status: released, not received.",
             audience_key=BC, authority=4),
        dict(chunk_id="sop1#0", source_handle="sharepoint://quality/sop-labels.docx",
             object_id="Document:sharepoint:sop-labels", kind="sharepoint",
             title="SOP: approving bottle label artwork", occurred_at=_t(400),
             header="SharePoint document 'SOP: approving bottle label artwork', quality "
                    "site, approved procedure, section 2 approval steps.",
             body="Label artwork must be approved by quality and regulatory before the "
                  "screen printer starts. Approval normally takes five working days.",
             audience_key=QUALITY, authority=3),
        dict(chunk_id="fin1#0", source_handle="graph-mail://finance/msg-9",
             object_id="EmailMessage:graph:msg-9", kind="mail",
             title="Margin on the Maple Suds soap order", author_role="controller",
             occurred_at=_t(5), linked_ids=["PurchaseOrder:bc:9001"],
             header="Email, finance thread 'Margin on the Maple Suds soap order', 24 Oct "
                    "2026, controller. PO 9001.",
             body="Landed cost of the soap order went up because of the delay; margin "
                  "drops to 18 percent if we air freight it.",
             audience_key=FINANCE, authority=2),
        dict(chunk_id="fin2#0", source_handle="graph-mail://purchasing/msg-11",
             object_id="EmailMessage:graph:msg-11", kind="mail",
             title="Soap order pricing", author_role="buyer", occurred_at=_t(6),
             linked_ids=["PurchaseOrder:bc:9001"], markings=["purchasing_prices"],
             header="Email, purchasing thread 'Soap order pricing', 23 Oct 2026. PO 9001.",
             body="Unit price for the castile soap is 3.10 USD per litre; Maple Suds asked "
                  "for a surcharge because the order is late.",
             audience_key=PURCHASING, authority=2),
        dict(chunk_id="teams1#0", source_handle="teams-chat://ops/chat-4/msg-2",
             object_id=None, kind="teams", title="ops chat", author_role="planner",
             occurred_at=_t(1),
             header="Teams chat, operations, 28 Oct 2026, planner.",
             body="anyone know when the canadian soap arrives? production wants it for the "
                  "hand wash run", audience_key="teams:chat:ops-chat-4", authority=1),
    ]
    rows.append(dict(
        chunk_id="excl1#0", source_handle="graph-mail://excluded/msg-1",
        object_id="EmailMessage:graph:excl-1", kind="mail", title="zanzibar plans",
        header="Email 'zanzibar plans'.", body="The zanzibar plans for the soap order.",
        audience_key="mail:" + EXCLUDED, authority=2))
    for i in range(12):
        rows.append(dict(
            chunk_id=f"filler{i}#0", source_handle=f"graph-mail://purchasing/filler-{i}",
            object_id=f"EmailMessage:graph:filler-{i}", kind="mail",
            title=f"Weekly order summary {i}", occurred_at=_t(10 + i),
            header=f"Email, purchasing thread 'Weekly order summary {i}'.",
            body=f"Order summary for week {i}: corrugated boxes, pumps and caps on time. "
                 "No issues with the order schedule.", audience_key=PURCHASING, authority=2))
    return rows
