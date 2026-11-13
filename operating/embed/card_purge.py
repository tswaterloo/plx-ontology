"""``python3 -m operating.embed.card_purge [--dsn DSN] [--apply]``: payment card numbers and
credential-shaped values out of the search index that was written before chunks were masked.

A chunk (``search.chunks``: its title, header and body, and so its generated words
``tsv`` and its embedding) or an entity (``search.entities``: name, aliases, normalised
form and name vector) holding a card number has it replaced by ``[card]``, and a
credential-shaped value by ``[secret]``, with the very rule every chunker now applies
(``operating.chunking.normalise.mask_private``: for a card, its layout, a card network's
range and the Luhn check; for a credential, ``operating.secret_text``), so a purged row
equals what the next run would write. A purged chunk's ``content_sha`` is recomputed and its embedding dropped (it
was made from the card's text; the next ``embed`` pass embeds the masked text); a purged
entity's name vector is dropped the same way. Search index only: the ontology store and
the captures are never touched.

Without ``--apply`` it is a dry run in a read-only transaction: nothing is written. Either
way it prints one JSON report of counts only (rows scanned, rows holding a card per kind
(cards and credentials apart) and per column, embeddings dropped): never a number, a
value, a text or an id. ``search.query_log``
and ``search.feedback`` (what readers typed) are counted, never changed.
Exit codes: 0 done, 2 refused (schema missing), 1 failed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Iterator, Sequence

from operating.chunking import content_sha
from operating.chunking.normalise import mask_cards, mask_private
from operating.secret_text import mask_secrets

from . import config

BATCH = 5000
# Postgres prefilter: any text with a run of digits a card layout can hold (4-4-4-4,
# 4-6-5, 4-6-4 with one space or hyphen, or 13-19 unbroken digits all have 4 digits, an
# optional separator, 4-6 digits, an optional separator, 4 digits), or any word a credential
# rule starts from (matched case-insensitively). ``mask_private`` decides.
CANDIDATE = (r"[0-9]{4}[ -]?[0-9]{4,6}[ -]?[0-9]{4}|secret|passw|pwd|passcode|phrase|key|token"
             r"|sk-|k_live|gh[pousr]_|github_pat|xox|akia|aiza|eyj|q~|private|://|sig=|signature")


def mask_row(fields: Sequence[str | None]) -> tuple[list[str | None], list[int], set[str]]:
    """The fields with every card and credential masked, the indexes of those that held
    one, and what was found (``card``, ``secret``)."""
    out: list[str | None] = []
    hit: list[int] = []
    found: set[str] = set()
    for i, value in enumerate(fields):
        masked = mask_private(value) if isinstance(value, str) and value else value
        if masked != value:
            hit.append(i)
            if mask_cards(value) != value:
                found.add("card")
            if mask_secrets(value) != value:
                found.add("secret")
        out.append(masked)
    return out, hit, found


def mask_aliases(alias: Sequence[str] | None) -> tuple[list[str], bool]:
    values = list(alias or ())
    masked = [mask_private(a) if isinstance(a, str) else a for a in values]
    return masked, masked != values


def _pages(conn: Any, sql: str, key_index: int = 0) -> Iterator[list[tuple[Any, ...]]]:
    """Keyset pages of ``sql`` (which takes the last key and the batch size)."""
    after = ""
    while True:
        rows = conn.execute(sql, (after, CANDIDATE, BATCH)).fetchall()
        if not rows:
            return
        yield rows
        after = rows[-1][key_index]


def purge_chunks(conn: Any, apply: bool) -> dict[str, Any]:
    columns = ("title", "header", "body")
    kinds: Counter[str] = Counter()
    per_column: Counter[str] = Counter()
    scanned = embeddings = with_card = with_secret = 0
    sql = ("SELECT chunk_id, kind, title, header, body, embedding IS NOT NULL "
           "FROM search.chunks WHERE chunk_id > %s AND (coalesce(title, '') || ' ' || header "
           "|| ' ' || body) ~* %s ORDER BY chunk_id LIMIT %s")
    for rows in _pages(conn, sql):
        for chunk_id, kind, title, header, body, embedded in rows:
            scanned += 1
            (new_title, new_header, new_body), hit, found = mask_row((title, header, body))
            if not hit:
                continue
            kinds[kind] += 1
            with_card += "card" in found
            with_secret += "secret" in found
            per_column.update(columns[i] for i in hit)
            embeddings += bool(embedded)
            if apply:
                conn.execute(
                    "UPDATE search.chunks SET title = %s, header = %s, body = %s, "
                    "content_sha = %s, embedding = NULL, embed_model = NULL, "
                    "updated_at = now() WHERE chunk_id = %s",
                    (new_title, new_header, new_body,
                     content_sha(new_header or "", new_body or ""), chunk_id))
        if apply:
            conn.commit()
    return {"candidates_scanned": scanned, "with_card": with_card, "with_secret": with_secret,
            "by_kind": dict(sorted(kinds.items())), "by_column": dict(sorted(per_column.items())),
            "embeddings_dropped" if apply else "embeddings_to_drop": embeddings}


def purge_entities(conn: Any, apply: bool) -> dict[str, Any]:
    has_model = conn.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'search' AND "
        "table_name = 'entities' AND column_name = 'name_embed_model'").fetchone() is not None
    columns = ("name", "normalized")
    per_column: Counter[str] = Counter()
    scanned = vectors = 0
    found: Counter[str] = Counter()
    sql = ("SELECT entity_id, name, normalized, alias, name_embedding IS NOT NULL "
           "FROM search.entities WHERE entity_id > %s AND (coalesce(name, '') || ' ' || "
           "coalesce(normalized, '') || ' ' || search.alias_text(alias)) ~* %s "
           "ORDER BY entity_id LIMIT %s")
    for rows in _pages(conn, sql):
        for entity_id, name, normalized, alias, embedded in rows:
            scanned += 1
            (new_name, new_normalized), hit, what = mask_row((name, normalized))
            new_alias, alias_hit = mask_aliases(alias)
            if not hit and not alias_hit:
                continue
            if alias_hit:
                what |= mask_row(alias or ())[2]
            found.update(what)
            per_column.update(columns[i] for i in hit)
            if alias_hit:
                per_column["alias"] += 1
            vectors += bool(embedded)
            if apply:
                conn.execute(
                    "UPDATE search.entities SET name = %s, normalized = %s, alias = %s, "
                    "name_embedding = NULL" + (", name_embed_model = NULL" if has_model else "")
                    + " WHERE entity_id = %s",
                    (new_name, new_normalized, new_alias, entity_id))
        if apply:
            conn.commit()
    return {"candidates_scanned": scanned, "with_card": found["card"],
            "with_secret": found["secret"], "by_column": dict(sorted(per_column.items())),
            "name_vectors_dropped" if apply else "name_vectors_to_drop": vectors}


def count_logs(conn: Any) -> dict[str, int]:
    """Rows of what readers typed that hold a card or a credential: counted, never changed
    (not the index)."""
    out = {}
    for table, column in (("query_log", "question"), ("feedback", "question")):
        if conn.execute("SELECT to_regclass(%s)", (f"search.{table}",)).fetchone()[0] is None:
            continue
        cards = creds = 0
        for (text,) in conn.execute(f"SELECT {column} FROM search.{table} WHERE {column} ~* %s",
                                    (CANDIDATE,)):
            cards += mask_cards(text) != text
            creds += mask_secrets(text) != text
        out[f"{table}_with_card"] = cards
        out[f"{table}_with_secret"] = creds
    return out


def purge(conn: Any, apply: bool) -> dict[str, Any]:
    if not apply:   # first statement of the transaction: a dry run cannot write
        conn.execute("SET TRANSACTION READ ONLY")
    if conn.execute("SELECT to_regclass('search.chunks')").fetchone()[0] is None:
        raise LookupError("search_schema_missing")
    report = {"mode": "apply" if apply else "dry_run",
              "chunks": purge_chunks(conn, apply),
              "entities": purge_entities(conn, apply),
              "not_changed": count_logs(conn)}
    if apply:
        conn.commit()
    else:
        conn.rollback()
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.embed.card_purge")
    parser.add_argument("--dsn", help="search store (default PLX_PG_DSN, then the local plx)")
    parser.add_argument("--apply", action="store_true",
                        help="write the masked rows (default: dry run, counts only)")
    args = parser.parse_args(argv)
    import os
    from . import store as st
    try:
        conn = st.connect(args.dsn or os.environ.get("PLX_PG_DSN") or config.DEFAULT_DSN)
    except Exception as exc:   # noqa: BLE001 - never print a DSN or its password
        print(json.dumps({"error": "connect_failed", "type": type(exc).__name__}))
        return 1
    try:
        report = purge(conn, args.apply)
    except LookupError as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except Exception as exc:   # noqa: BLE001 - a failure prints its kind, never row text
        conn.rollback()
        print(json.dumps({"error": "failed", "type": type(exc).__name__}))
        return 1
    finally:
        conn.close()
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
