"""The file a Teams message shares, linked to the captured SharePoint file it is.

A Teams attachment (ontology value ``attachments`` of a TeamsChatMessage) names the shared
file by ``content_url``; the SharePoint capture holds the same address as the item's
``original_url`` (Graph webUrl). Two exact rules, both string equality after ONE
percent-decoding, no fuzzy match, no name search:

* ``url_exact``: the content_url equals a current file's webUrl;
* ``url_owner_path``: a personal OneDrive file's webUrl is a ``_layouts/15/Doc.aspx?...``
  address; its owner is the ``/personal/<owner>`` segment, so ``<origin>/personal/<owner>/
  Documents`` + the file's stored path is the address a Teams message names. Used only for
  files whose webUrl is that shape.

An address two current files hold links nothing (counted as ambiguous). Live 11-22
(prototype, read only): 6,823 distinct shared SharePoint addresses, 4,639 equal a webUrl,
1,004 more by owner+path; 1,180 name no captured file.

Table: ``biz.teams_file_links`` (migration 0055). Each row carries the door of BOTH ends; the
guarded view ``<schema>_sql.teams_message_files`` (``view_ddl``) shows a link only when the
reader reads the message in their own guarded ``teams_messages`` AND the file in their own
guarded ``sharepoint_files`` now (the rule of both views, at read time), granted to plx.sql's
role only. The message and file tables are never updated for a link.

    python3 -m operating.pg.teams_file_links run [--new] [--dsn D] [--capture DB]

After each comms refresh ``operating.assist_readings`` (the unit the business-views refresh
starts) calls ``run(new=True)`` whatever its own model status: no timer of its own; a failure
there is logged and never fails the reading run.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from operating.pg import business_views as bv

LINKS = "biz.teams_file_links"
VIEW = "teams_message_files"
SOURCES = ("teams_messages", "sharepoint_files")
CAPTURE = Path("/data/plx-state/sharepoint-capture/capture.db")
_PERSONAL = re.compile(r"^(https://[^/]+/personal/[^/]+)/_layouts/")
FILE_TYPE = "SharePointFile:sharepoint.drive_items:"


def decode(url: Any) -> str | None:
    """The address after one percent-decoding, or None for anything not a string address."""
    if not isinstance(url, str):
        return None
    text = urllib.parse.unquote(url.strip())
    return text if text.startswith("https://") else None


def file_object_id(drive_id: str, item_id: str) -> str:
    return FILE_TYPE + json.dumps({"drive_id": drive_id, "id": item_id},
                                  separators=(",", ":"), sort_keys=True)


def capture_addresses(capture_db: Path | str) -> tuple[dict, dict]:
    """``(exact, rebuilt)``: address -> set of file object ids, over the capture's current
    versions, opened read only."""
    exact: dict[str, set] = collections.defaultdict(set)
    rebuilt: dict[str, set] = collections.defaultdict(set)
    con = sqlite3.connect(f"file:{capture_db}?mode=ro", uri=True)
    try:
        for drive, item, path, url in con.execute(
                "SELECT drive_id, item_id, path, json_extract(record, '$.original_url') FROM "
                "items WHERE superseded_at IS NULL AND removed_reason IS NULL"):
            oid = file_object_id(drive, item)
            key = decode(url)
            if key:
                exact[key].add(oid)
                m = _PERSONAL.match(key)
                if m and isinstance(path, str) and path.startswith("/"):
                    rebuilt[decode(m[1] + "/Documents" + path) or ""].add(oid)
    finally:
        con.close()
    rebuilt.pop("", None)
    return exact, rebuilt


def shared_addresses(conn: Any, only_new: bool = False) -> dict[str, set]:
    """Teams message object id -> the decoded addresses of its attachments; ``only_new``:
    only messages with no link row yet (a message whose file is not captured yet is read
    again each run until it links)."""
    out: dict[str, set] = {}
    fresh = (f" AND NOT EXISTS (SELECT 1 FROM {LINKS} l WHERE l.message_object_id = "
             "v.object_id)") if only_new else ""
    cur = conn.execute(
        "SELECT v.object_id, v.value FROM ontology.object_values v WHERE v.property = "
        "'attachments' AND v.object_id LIKE 'TeamsChatMessage:%%' AND v.value LIKE "
        "'%%sharepoint.com%%'" + fresh)
    for oid, raw in cur:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            continue
        urls = {decode(a.get("content_url")) for a in items if isinstance(a, dict)}
        urls.discard(None)
        if urls:
            out[oid] = urls
    return out


def derive(messages: Mapping[str, set], exact: Mapping, rebuilt: Mapping
           ) -> tuple[dict[tuple, tuple], dict[str, int]]:
    """``{(message oid, file oid): (address, via)}`` and counts. An address held by two files
    by the same rule links nothing; ``url_exact`` wins over ``url_owner_path``."""
    links: dict[tuple, tuple] = {}
    seen, matched, ambiguous = set(), set(), set()
    for moid, urls in messages.items():
        for url in urls:
            seen.add(url)
            for via, table in (("url_exact", exact), ("url_owner_path", rebuilt)):
                files = table.get(url)
                if not files:
                    continue
                if len(files) > 1:
                    ambiguous.add(url)
                    break
                links[(moid, next(iter(files)))] = (url, via)
                matched.add(url)
                break
    return links, {"addresses": len(seen), "matched": len(matched),
                   "ambiguous": len(ambiguous), "links": len(links),
                   "unmatched": len(seen - matched - ambiguous)}


def write_links(conn: Any, links: Mapping[tuple, tuple], *, chunk: int = 20000,
                keep: bool = False, **guard_kw: Any) -> int:
    """Make ``biz.teams_file_links`` agree with ``links`` (one transaction): rows no longer
    derived go; each row's door columns are read again from both ends. A link whose message
    or file is not in the business tables now is not written. ``keep``: a scoped run for new
    messages only, no row is removed. ``conn`` autocommit. Stops
    before any work when migration 0055's table is missing; guarded like every job over
    1 GB is not needed (a few thousand rows) but the disk and standby guard still run."""
    from operating.pg import comm_links
    if conn.execute("SELECT to_regclass(%s)", (LINKS,)).fetchone()[0] is None:
        raise RuntimeError(f"{LINKS} is missing: apply migration 0055_teams_file_links "
                           "(python3 -m operating.pg migrate)")
    if keep and not links:
        return 0          # nothing new to link: no stage, no scan of the message and file tables
    if keep:
        # a scoped run writes only the links of new messages: its stage is unlogged and its insert is
        # a few rows, so it never waits for the standby to catch up (the fresh reading unit, started
        # after every capture delta, waited here until its start timeout on 11-24 while the standby
        # lagged). The disk floor still applies. A full pass writes in bulk and waits, and says so.
        guard_kw.setdefault("pause_gb", float("inf"))
    else:
        guard_kw.setdefault("log", lambda msg: print(f"teams file links: {msg}", file=sys.stderr))
    stage = "biz.teams_file_links_stage"
    conn.execute(f"DROP TABLE IF EXISTS {stage}")
    conn.execute(f"CREATE UNLOGGED TABLE {stage} (message_oid text, file_oid text, url text, "
                 "via text)")
    try:
        rows = sorted((m, f, u, v) for (m, f), (u, v) in links.items())
        for lo in range(0, len(rows), chunk):
            comm_links.guard(conn, **guard_kw)
            with conn.cursor().copy(f"COPY {stage} FROM STDIN") as cp:
                for row in rows[lo:lo + chunk]:
                    cp.write_row(row)
        comm_links.guard(conn, **guard_kw)
        with conn.transaction():
            conn.execute(
                "CREATE TEMP TABLE tfl_new ON COMMIT DROP AS SELECT DISTINCT ON (m.handle, "
                "f.handle) m.handle AS mh, f.handle AS fh, w.message_oid, w.file_oid, w.url, "
                "w.via, m.audience_keys AS ma, m.rec_marks AS mr, m.col_marks AS mc, "
                "m.ai_ok AS mo, f.audience_keys AS fa, f.rec_marks AS fr, f.col_marks AS fc, "
                f"f.ai_ok AS fo FROM {stage} w JOIN (SELECT handle, audience_keys, rec_marks, "
                "col_marks, ai_ok, unnest(object_ids) AS oid FROM biz.teams_messages) m ON "
                "m.oid = w.message_oid JOIN (SELECT handle, audience_keys, rec_marks, "
                "col_marks, ai_ok, unnest(object_ids) AS oid FROM biz.sharepoint_files) f ON "
                "f.oid = w.file_oid ORDER BY m.handle, f.handle, w.via")
            if not keep:
                conn.execute(f"DELETE FROM {LINKS} l WHERE NOT EXISTS (SELECT 1 FROM "
                             "tfl_new n WHERE n.mh = l.message_handle AND n.fh = "
                             "l.file_handle)")
            n = conn.execute(
                f"INSERT INTO {LINKS} SELECT mh, fh, message_oid, file_oid, url, via, ma, mr, "
                "mc, mo, fa, fr, fc, fo FROM tfl_new ON CONFLICT (message_handle, "
                "file_handle) DO UPDATE SET message_object_id = EXCLUDED.message_object_id, "
                "file_object_id = EXCLUDED.file_object_id, url = EXCLUDED.url, via = "
                "EXCLUDED.via, msg_audience_keys = EXCLUDED.msg_audience_keys, msg_rec_marks "
                "= EXCLUDED.msg_rec_marks, msg_col_marks = EXCLUDED.msg_col_marks, msg_ai_ok "
                "= EXCLUDED.msg_ai_ok, file_audience_keys = EXCLUDED.file_audience_keys, "
                "file_rec_marks = EXCLUDED.file_rec_marks, file_col_marks = "
                "EXCLUDED.file_col_marks, file_ai_ok = EXCLUDED.file_ai_ok").rowcount
        return n
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {stage}")


def view_ddl(src: str, dst: str) -> str:
    """``CREATE VIEW <dst>.teams_message_files``: a link only with the message AND the file
    the reader's own guarded views show now (security barrier; each view's rule is its own,
    read with the reader's settings: a reader who may not list files sees none)."""
    return (f"CREATE VIEW {dst}.{VIEW} WITH (security_barrier) AS SELECT l.message_handle, "
            "l.file_handle, f.name AS file_name, f.path AS file_path, f.mime_type, f.size, "
            "l.url, l.via FROM " + f"{src}.teams_file_links l JOIN {dst}.teams_messages m ON "
            f"m.handle = l.message_handle JOIN {dst}.sharepoint_files f ON f.handle = "
            "l.file_handle")


def drop_view(write: Any, schema: str | None = None) -> None:
    """Before a guarded message or file view is dropped (a build's swap): this view reads both."""
    from operating.pg import sql_views
    write.execute(f"DROP VIEW IF EXISTS {sql_views.guarded_schema(schema)}.{VIEW}")


def create_view(write: Any, schema: str | None = None) -> bool:
    """The guarded view, granted to plx.sql's role only; nothing (fail closed: plx.sql cannot
    read links) when the table or either guarded view is missing."""
    from operating.pg import sql_views
    src = schema or bv.SCHEMA
    dst = sql_views.guarded_schema(schema)
    if write.execute("SELECT to_regclass(%s) IS NULL OR to_regclass(%s) IS NULL OR "
                     "to_regclass(%s) IS NULL", (f"{src}.teams_file_links",
                                                 f"{dst}.teams_messages",
                                                 f"{dst}.sharepoint_files")).fetchone()[0]:
        return False
    write.execute(f"DROP VIEW IF EXISTS {dst}.{VIEW}")
    write.execute(view_ddl(src, dst))
    write.execute(f"REVOKE ALL ON {dst}.{VIEW} FROM PUBLIC")
    sql_views._grant(write, (sql_views.ROLE,), f"SELECT ON {dst}.{VIEW}")
    return True


def run(dsn: str, capture_db: Path | str = CAPTURE, new: bool = False, **guard_kw: Any
        ) -> dict[str, Any]:
    """One pass: derive from the capture and the served attachments, write, report counts.
    ``new``: only messages not linked yet, nothing removed (after each comms refresh); a
    full pass also drops links no longer derived."""
    import psycopg
    exact, rebuilt = capture_addresses(capture_db)
    with psycopg.connect(dsn, autocommit=True) as pg:
        pg.execute("SET statement_timeout = '600s'")
        links, stats = derive(shared_addresses(pg, new), exact, rebuilt)
        stats["written"] = write_links(pg, links, keep=new, **guard_kw)
        with pg.transaction():
            stats["view"] = create_view(pg)
    return stats


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.teams_file_links")
    ap.add_argument("command", choices=("run",))
    ap.add_argument("--dsn")
    ap.add_argument("--capture", default=str(CAPTURE))
    ap.add_argument("--new", action="store_true", help="only messages not linked yet")
    args = ap.parse_args(argv)
    from operating.pg import config
    print(json.dumps(run(args.dsn or config.dsn(), args.capture, args.new), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
