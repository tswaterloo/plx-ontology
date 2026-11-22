"""plx.sql's description of what it reads (split from ``ontology_tools_sql``): the
rules the Ask is given, the generic and comms-derived tables, the record-type catalog
lines and the served views. ``ontology_tools_sql`` re-exports every name, so callers
and tests keep reading them there."""
from __future__ import annotations

import json
import time
from typing import Any, Sequence

from operating.pg import business_views as bv
from operating.pg import sql_views


RULES = (
    "One SELECT per call over these views (bare names), at most 200 rows back. Each row is "
    "one real thing (its Portal, Sage, BC and FileMaker copies joined); records deleted at "
    "the source are left out; money columns sit beside a <column>_currency column: never "
    "add across currencies (GROUP BY the currency). Dates are business dates (America/"
    "New_York). sales_invoices.pilot = true marks BC pilot invoices (Sage is the system of "
    "record): leave them out unless asked. Text compares case-sensitively: use ILIKE "
    "'%word%' for names. An array column (addresses, domains, customers, suppliers, "
    "records) is indexed for containment: domains @> ARRAY['univar.com'] (exact, lower "
    "case) answers at once, where unnest() with ILIKE reads every row and runs past the "
    "time limit. A column in a row's withheld list is one you may not read (NULL). "
    "Each row has a handle: select it to cite the record. A result row's cells line up "
    "with its columns.")
# the generic views' rule (``records``), said only while they are served (``served_views``)
RULES_GENERIC = (
    " EVERY other captured record of every system (Mission Control, "
    "SharePoint lists, FileMaker, Portal, BC and Sage tables, Planner, records read out of "
    "mail ...) is in the generic views: record_types (the catalog of types: plain name, "
    "systems, fields with kind and meaning, label and date fields, links out/in), records "
    "(one row per record) and record_links (one row per link between two records). Filter "
    "records on type = '<Type>' and read a field as fields->>'name' (numbers: "
    "(fields->>'name')::numeric; money beside fields->>'name_currency', quantities beside "
    "fields->>'name_unit'). The same thing held by two systems is two records joined by an "
    "identity link (record_types.identity_links, in record_links). {describe: '<Type>'} "
    "shows a type's fields, sample records and commonest values. records.label ILIKE is "
    "indexed, and type = '<Type>' first is fast; fields::text ILIKE over every type "
    "reads every record and runs past the time limit: find a word in any record with "
    "plx.search (sources records). A record's links: record_links WHERE from_id (or "
    "to_id) = ANY(ARRAY(SELECT record_id FROM records WHERE type = '<Type>' AND ...)) "
    "reads them by index in milliseconds. To join views row by row (records to "
    "record_links, a link to the record at its other end, records to a business view), "
    "read the other side in a fenced lateral: LEFT JOIN LATERAL (SELECT l.to_id FROM "
    "record_links l WHERE l.from_id = r.record_id AND l.link_type = '<link>' OFFSET 0) l "
    "ON true (OFFSET 0 is what makes each row's match an index read, milliseconds); a "
    "plain JOIN, IN (SELECT ...) or a lateral without OFFSET 0 between these views reads "
    "every row of the other one (past the time limit). "
    "Mail and Teams: a count or list within a received_at / created_at window reads its "
    "index (under a second); over the whole table it reads every message (about 30 s).")
RULES_TAIL = " Mail text, chats and documents: plx.search."
RULES_BASE = RULES
RULES = RULES_BASE + RULES_GENERIC + RULES_TAIL

GENERIC_ABOUT = {
    "record_types": ("the catalog of every record type the store holds (generated from "
                     "the ontology: new sources and fields appear by themselves)", {
                         "type": "text: the type's id (records.type)",
                         "name": "text: its plain name", "description": "text",
                         "systems": "text[]: the systems holding records of it",
                         "sources": "text[]: the source tables (records.source)",
                         "label_fields": "text[]: fields that name a record",
                         "date_fields": "text[]: fields that date a record",
                         "identity_links": "text[]: link types joining copies of one thing",
                         "fields": "jsonb: [{name, kind, description}]",
                         "links_out": "jsonb: [{link_type, to}]",
                         "links_in": "jsonb: [{link_type, from}]",
                         "tables": "text[]: the views holding its records (a curated view, "
                                   "a mail/Teams view, or records)"}),
    "records": ("one row per current record of a type no curated view lists, as this "
                "reader may read it (count a type here)", {
        "record_id": "text: the record's id (record_links.from_id / to_id)",
        "type": "text: the record type (filter on it)", "system": "text",
        "source": "text: the source table", "label": "text: its display name or number",
        "record_date": "text YYYY-MM-DD: its key date (record_types.date_fields)",
        "first_seen": "text: when it was first captured", "changed_at": "text",
        "fields": "jsonb: field -> value (the authority-first value of each field)"}),
    "record_links": ("one row per live link between two records this reader may read "
                     "(filter from_id or to_id = ANY(ARRAY(SELECT record_id FROM records "
                     "WHERE ...)), never a JOIN to records)", {
        "from_id": "text: records.record_id", "from_type": "text", "from_handle": "text",
        "link_type": "text", "to_id": "text: records.record_id", "to_type": "text",
        "to_handle": "text", "tier": "text: verified/reviewed/corroborated (solid), "
        "stored_by_text_join, lead (a guess)"}),
}
GENERIC = tuple(GENERIC_ABOUT)
# views over the communications views (``operating.pg.comms_findings``): a statement naming
# one reads with the communications settings (its rows are those of messages the reader's
# own mail and Teams views show; unset, it shows nothing)
COMMS_DERIVED_ABOUT = {
    "comms_findings": (
        "decisions, requests, complaints and praise read out of mail and Teams messages "
        "(one row per item a message states; a message this reader cannot read shows none; "
        "the summary is a model's one-line reading: open the handle with plx.open to quote "
        "the message). Customer feedback = kind complaint or praise; GROUP BY topic or "
        "customers for themes", {
            "kind": "text: decision, request, complaint or praise",
            "topic": "text: delivery, pricing, quality, documents, order, payment, product, "
                     "samples, packaging, schedule, stock, service or other",
            "summary": "text: one plain sentence saying what was decided, asked or "
                       "reported",
            "who": "text: who decided, asked or complained, as the message names them "
                   "('' not named)",
            "from_side": "text: who sent the message: us (our staff), customer, outside "
                         "(another party: supplier, carrier, vendor) or '' (unknown: a Teams "
                         "sender not matched to staff)",
            "said_at": "timestamptz: when the message was sent",
            "said_date": "date: its business date",
            "source": "text: email or teams",
            "sender": "text: the message's sender (an address for mail, a name in Teams)",
            "subject": "text: the mail subject or Teams chat topic",
            "thread": "text: the mail thread (emails.thread) or Teams chat id",
            "customers": "text[]: handles of customers rows the message is about (join "
                         "customers ON handle = ANY(customers))",
            "suppliers": "text[]: handles of suppliers rows it is about"}),
    "teams_message_files": (
        "the file a Teams message shares, linked to the captured SharePoint file it is (one "
        "row per message and file; exact address match, no name guessing: a shared file not "
        "captured has no row). A row shows only when this reader may read the message AND "
        "the file (files: full readers only). Join teams_messages ON handle = message_handle "
        "and sharepoint_files ON handle = file_handle; open file_handle with plx.open or "
        "read it with plx.read_document", {
            "message_handle": "text: the Teams message's handle (teams_messages.handle)",
            "file_handle": "text: the file's handle (sharepoint_files.handle)",
            "file_name": "text: the file's name", "file_path": "text: the file's path",
            "mime_type": "text: the file's type", "size": "numeric: bytes",
            "url": "text: the address the message names (the file's SharePoint address)",
            "via": "text: url_exact (the message's address is the file's) or url_owner_path "
                   "(the file in that person's OneDrive at that path)"}),
}
COMMS_DERIVED = tuple(COMMS_DERIVED_ABOUT)
GENERIC_TABLES = ("records", "record_links")
TYPE_SAMPLE = 500


_CATALOG: dict[str, Any] = {}


# the fields naming what kind of record a row is ("PaymentNotice", "received", "Open"):
# named in its catalog line so the Ask filters the type by them instead of searching text
CATALOG_KEYS = ("record_type", "family", "kind", "status", "state", "stage", "category",
                "document_type")


def catalog_keys(fields: Any) -> list[str]:
    """The ``CATALOG_KEYS`` a type's ``record_types.fields`` holds, in that order, at most 3
    (the catalog's own field list: no row of any record)."""
    if isinstance(fields, str):
        try:
            fields = json.loads(fields)
        except ValueError:
            return []
    names = {f.get("name") for f in fields or () if isinstance(f, dict)}
    return [k for k in CATALOG_KEYS if k in names][:3]


def catalog_lines(pg: Any = None) -> list[str]:
    """The record types the door holds as prompt lines, one per system: each type with its
    plain name, the fields that label and date it and, when a curated or communications
    view holds it instead of ``records``, that view; generated from the served
    catalog (``<biz>.record_types``, read by the store's role: no row of any record, no
    count). Kept per process until the catalog is built again; none when it cannot be read
    (the prompt then lists the views alone)."""
    from integrations.image_ocr_gpu import FREIGHTCOM
    try:
        if pg is None:
            from operating.pg import pool
            with pool.connect(connect_timeout=3, options="-c statement_timeout=2000") as c:
                return catalog_lines(c)
        if pg.execute("SELECT to_regclass(%s)", (f"{bv.SCHEMA}.record_types",)
                      ).fetchone()[0] is None:
            return []
        # the catalog's build and where each type is held (``generic_views.mark_tables``
        # moves ``tables`` without a new catalog build)
        (built,) = pg.execute(f"SELECT max(built_at) || md5(string_agg(type || ':' || "
                              "array_to_string(tables, ','), ';' ORDER BY type)) FROM "
                              f"{bv.SCHEMA}.record_types").fetchone()
        if _CATALOG.get("built") == built and built is not None:
            return list(_CATALOG["lines"])
        # the guarded view's own Freightcom rule (``generic_views.listed_sql``)
        from operating.pg import generic_views
        rows = pg.execute(
            f"SELECT t.type, t.name, t.systems, t.label_fields, t.date_fields, t.tables, "
            f"t.fields FROM "
            f"{bv.SCHEMA}.record_types t WHERE cardinality(t.tables) > 0 AND "
            f"{generic_views.listed_sql('t')} ORDER BY t.type").fetchall()
    except Exception:  # noqa: BLE001 - no catalog: the views alone
        return []
    by_system: dict[str, list[str]] = {}
    for row in rows:
        tid, name, systems, labels, dates, tables = row[:6]
        fields = row[6] if len(row) > 6 else None
        if FREIGHTCOM.search(" ".join(map(str, (tid, name)))):
            continue   # the plain name too (made from the type: the same words)
        # a type held by a curated or comms view is named with it (records lacks it)
        bits = [f"label {'/'.join(labels[:2])}" if labels else "",
                f"date {'/'.join(dates[:2])}" if dates else "",
                "in " + "/".join(tables) if list(tables) != ["records"] else "",
                # the fields that say what kind of record a row is (field names only)
                "kinds by " + "/".join(keys) if (keys := catalog_keys(fields)) else ""]
        shown = f"{tid} ({name}" + "".join(f"; {b}" for b in bits if b) + ")"
        for system in (systems or ["unknown"]):
            by_system.setdefault(str(system), []).append(shown)
    lines = [f"- {system}: " + ", ".join(sorted(types))
             for system, types in sorted(by_system.items())]
    _CATALOG.update(built=built, lines=lines)
    return list(lines)


_SERVED: dict[str, Any] = {}
SERVED_RECHECK_SECONDS = 60.0


def served_views(names: Sequence[str] | None = None, pg: Any = None) -> frozenset[str] | None:
    """Which of the views ``names`` (default: every view of ``_dictionary``) the served
    schema holds now (a business table built, the mail and Teams tables once their build
    is served, ``records`` once the generic build is): what the Ask's prompt lists, so the
    model is never sent to a table it would be refused (11-18: emails/teams_messages and
    records listed while not built, every mail question spent its calls on
    'unknown view'). Read by the store's role (no row of any table), kept per process
    ``SERVED_RECHECK_SECONDS``; None when it cannot be read (the prompt lists them all)."""
    wanted = tuple(names if names is not None else _dictionary())
    held = _SERVED.get(wanted)
    if held is not None and time.monotonic() - held[0] < SERVED_RECHECK_SECONDS:
        return held[1]
    try:
        if pg is None:
            from operating.pg import pool
            with pool.connect(connect_timeout=3, options="-c statement_timeout=2000") as c:
                return served_views(wanted, c)
        rows = pg.execute("SELECT name FROM unnest(%s::text[]) name WHERE "
                          "to_regclass(%s || '.' || name) IS NOT NULL",
                          (list(wanted), bv.SCHEMA)).fetchall()
    except Exception:  # noqa: BLE001 - not known: every view listed, as before
        return None
    got = frozenset(r[0] for r in rows)
    _SERVED[wanted] = (time.monotonic(), got)
    return got


def _dictionary() -> dict[str, Any]:
    out = {}
    for name, entry in bv.dictionary().items():
        cols = dict(entry["columns"])
        for cname, kind, cdoc in sql_views.SHOWN_COMMON:
            cols[cname] = f"{kind}: {cdoc}"
        out[name] = {"about": entry["about"], "columns": cols}
    for name, (about, cols) in {**GENERIC_ABOUT, **COMMS_DERIVED_ABOUT}.items():
        cols = dict(cols)
        if name != "record_types":
            for cname, kind, cdoc in sql_views.SHOWN_COMMON:
                cols[cname] = f"{kind}: {cdoc}"
        out[name] = {"about": about, "columns": cols}
    return out
