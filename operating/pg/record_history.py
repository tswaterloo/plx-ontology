"""The served History tab: ``history`` page items (speed specs 8.3 ``history``, 9.3 V2), loaded from
``operator_api.history_batch``'s lines (each record's whole change log as the owner's full reader
reads it, with what a reader must hold for each change) and applied in place
(``page_items.apply``, kind ``history``, page key = the member record id).

Items (section ``history``; ``seq`` ascending from the oldest change, so a new change appends and
the tab reads ``ORDER BY seq DESC``; ``payload.ev`` = the change's ordinal):

* an EVENT item per change of the record (``first_recorded``, ``changed``, ``removed_from_source``,
  ``restored``, ``link_added``, ``link_removed``): ``payload.event`` is the change as the History
  tab's ``operator_api.history._entry`` reads it, without the values;
* a CHANGE item per property change of a ``first_recorded`` / ``changed`` event: ``payload.change``.

Section ``history_head``: one item, the record's kind and label as the tab shows them.

PERMISSION (the tab's rule, ``ontology_tools_history``, restated as classes; nothing decided per
reader here):

* every item: the member record's own row class in ``records`` (a reader who may not open the
  record gets no item: 404, as today);
* a CHANGE item's column class: the markings of every row before and after and of the property's
  latest row (``marks``), so a reader lacking one reads it withheld, then and since; the head's:
  the markings of the record's display values;
* a LINK item's class adds the link row's markings and the other end's audience key and
  markings (``plx.object``'s visibility rule); the item-gated handles it rests on (its mail,
  file or chat evidence and the other end's own version when item-gated) are ``payload.need.
  cites``, each checked at read through the communications views' own guard
  (``operator_api.served``): one not readable drops the item. A corroborated link's
  corroborating objects are ``payload.need.corr`` (each one's class id and item-gated handle): a
  reader who may not read them all sees the lead its mention is (``mentions.reader_view``).
* lineage: the member record's ``records`` row plus the other end, the evidence's and the
  corroboration's objects, so the people exclusion makes an item citing a partner's or an
  excluded person's message the owner's alone (``door.exclude``).

  python3 -m operating.pg.record_history load FILE... [--schema S]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import time
from typing import Any, Iterable, Sequence

from operating.pg import business_views as bv
from operating.pg import page_items

KIND = "history"
HEAD, SECTION = "history_head", "history"
CONFLICT, RECEIVED = "conflict", "received"    # the page's data conflicts (see ``conflict_items``)
VALUE_KINDS = ("first_recorded", "changed")
LINK_KINDS = ("link_added", "link_removed")
KINDS = VALUE_KINDS + LINK_KINDS + ("removed_from_source", "restored")
_SETS: dict[frozenset, str] = {}


def canon(marks: Iterable[str]) -> str:
    """The store's canonical markings text for the set ``marks`` (``store_ddl.MARKING_SETS``: the
    strings a reader's door holds); an unknown marking is refused (never read as none)."""
    if not _SETS:
        from ontology import store_ddl
        _SETS.update({frozenset(json.loads(t)): t for t in store_ddl.MARKING_SETS})
    key = frozenset(marks)
    if key not in _SETS:
        raise ValueError(f"markings {sorted(key)} are not a known marking set")
    return _SETS[key]


def _deps(line: dict[str, Any]) -> dict[str, int] | None:
    """The watermarks the line was read at (``history_batch.deps_of``), None when it has none
    (then the record is read again at once: ``moved_sql``); anything else is refused."""
    deps = line.get("deps")
    if deps is None:
        return None
    if not isinstance(deps, dict) or set(deps) != {"ver", "link", "xmin"} or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in deps.values()):
        raise ValueError(f"{line.get('record_id')}: watermarks {deps!r}")
    return deps


def aud_sql(schema: str, key: str) -> str:
    """The audience stamp of a stored history (a text): the audience key every object its items'
    lineage names holds NOW, and the member record's own class, in one digest. An audience is
    changed IN PLACE (``ontology.objects`` is upserted, no new version), so no version or link
    watermark moves for a narrowing; the stamp taken when the items were stored
    (``stamp_audience``) is compared with this one (``moved_sql``, the resident's sweep)."""
    return (
        "((SELECT md5(COALESCE(string_agg(o.object_id || '=' || COALESCE(o.audience_key, ''), "
        "',' ORDER BY o.object_id), '')) FROM ontology.objects o WHERE o.object_id IN (SELECT "
        f"x FROM {schema}.page_items p JOIN {schema}.lineage g ON g.view = 'page_items' AND "
        "g.handle = p.handle CROSS JOIN LATERAL unnest(g.object_ids || g.read_ids) x WHERE "
        f"p.page_kind = 'history' AND p.page_key = {key})) || ':' || COALESCE((SELECT "
        f"r.access_class::text FROM {schema}.records r WHERE r.record_id = {key}), ''))")


def stamp_audience(pg: Any, schema: str, keys: Sequence[str]) -> int:
    """Store each of ``keys``' audience stamp in its head item (after its items are applied)."""
    return pg.execute(
        f"UPDATE {schema}.page_items h SET payload = jsonb_set(h.payload, '{{aud}}', "
        f"to_jsonb({aud_sql(schema, 'h.page_key')})) WHERE h.page_kind = 'history' AND "
        "h.section = 'history_head' AND h.page_key = ANY(%s) AND h.payload IS NOT NULL",
        (list(keys),)).rowcount


def moved_parts(schema: str, given: bool = False) -> tuple[str, str]:
    """(content, audience): the condition (over the head item ``h``) that a record's stored
    history must be read again because what it read has changed, and that its audience stamp no
    longer holds (never in ``given`` mode: the runner's own answer was read just now). Content: no
    watermarks, or a version of any object its items' lineage names (the record, each link's
    other end, its evidence and corroboration) newer than them, or a link row from or to the
    record newer than them (by number, or by a transaction at or after the horizon: committed
    late). ``given``: the watermarks are parameters ``%(v)s``, ``%(l)s``, ``%(x)s`` (a runner's
    own answer the standby may not show yet) instead of the head's. One index probe per object
    (``object_versions_by_object``, ``link_rows_from`` / ``_to``)."""
    from operating.pg.plan_sections import _later
    v, l_, x = (("%(v)s::bigint", "%(l)s::bigint", "%(x)s::bigint") if given else
                ("h.dep_ver", "(h.payload #>> '{deps,link}')::bigint",
                 "(h.payload #>> '{deps,xmin}')::bigint"))
    none = "false" if given else "jsonb_typeof(h.payload -> 'deps') IS DISTINCT FROM 'object'"
    content = (
        f"({none} OR EXISTS (SELECT 1 FROM {schema}.page_items p JOIN {schema}.lineage g ON "
        "g.view = 'page_items' AND g.handle = p.handle CROSS JOIN LATERAL unnest(g.object_ids "
        "|| g.read_ids) o JOIN ontology.object_versions ov ON ov.object_id = o WHERE "
        "p.page_kind = 'history' AND p.page_key = h.page_key AND (ov.ver > "
        f"{v} OR {_later('ov.xmin', x)})) OR EXISTS (SELECT 1 FROM ontology.link_rows k WHERE "
        f"k.from_object = h.page_key AND (k.row_no > {l_} OR {_later('k.xmin', x)})) OR EXISTS "
        "(SELECT 1 FROM ontology.link_rows k WHERE k.to_object = h.page_key AND (k.row_no > "
        f"{l_} OR {_later('k.xmin', x)})))")
    aud = "false" if given else (
        f"(h.payload ->> 'aud') IS DISTINCT FROM {aud_sql(schema, 'h.page_key')}")
    return content, aud


def moved_sql(schema: str, given: bool = False) -> str:
    """The condition that a record's stored history must be read again: ``moved_parts``' either."""
    content, aud = moved_parts(schema, given)
    return f"({content} OR {aud})"


def items(line: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """One record's line -> (its items, its corroboration requirements). Each item: page_key,
    section, seq, payload, req_keys (audience keys beyond the record's), req_marks (marking sets
    beyond it), col (the column marking set or None), lin (record ids its lineage adds), ev_handles
    (evidence handles, for their objects). A corroboration row: page_key, seq, i, id, key, marks,
    cite. An event of an unknown kind is refused (the line fails, never guessed at)."""
    key = line["record_id"]
    out = [{"page_key": key, "section": HEAD, "seq": 0, "payload": {
        "type": line["type"], "label": line["label"], "gaps": line.get("gaps") or [],
        "partial": bool(line.get("withheld")), "deps": _deps(line),
        "conflicts": ({"built": True, "subject": line["conflicts"]["label"]}
                      if isinstance(line.get("conflicts"), dict) else {"built": False})},
        "req_keys": [], "req_marks": [], "col": canon(line.get("label_marks") or ()) if
        line.get("label_marks") else None, "lin": [], "ev_handles": [], "other": None}]
    corr: list[dict[str, Any]] = []
    seq = 0
    events = list(line["events"])
    for ev, event in enumerate(reversed(events)):   # the answer is newest first
        kind = event.get("kind")
        if kind not in KINDS:
            raise ValueError(f"{key}: change kind {kind!r}")
        head = {k: v for k, v in event.items() if k not in ("changes", "marks", "link")}
        item = {"page_key": key, "section": SECTION, "seq": seq, "req_keys": [],
                "req_marks": [], "col": None, "lin": [], "ev_handles": [], "other": None}
        if kind in LINK_KINDS:
            link = dict(event["link"])
            need = link.pop("need")
            other = need["other"]
            # the link row's own markings; the other end and every evidence object are resolved
            # set-based in ``_classes`` (``rh_req``): what a reader must hold to read each
            item["req_marks"] = [canon(need["marks"])]
            item["other"] = (other["id"], other.get("key"), canon(other["marks"]),
                             other.get("cite"))
            item["lin"] = [other["id"], *(c["id"] for c in need["corr"] if c.get("id"))]
            item["ev_handles"] = list(need["evidence"])
            cites: list[str] = []      # set by ``_classes``
            for i, c in enumerate(need["corr"]):
                if c.get("never"):   # no reader reads it: a class no door holds (-1)
                    corr.append({"page_key": key, "seq": seq, "i": i, "id": None, "key": None,
                                 "marks": None, "cite": None})
                    continue
                corr.append({"page_key": key, "seq": seq, "i": i, "id": c["id"],
                             "key": c.get("key"), "marks": canon(c["marks"]),
                             "cite": c.get("cite")})
            item["payload"] = {"ev": ev, "event": {**head, "link": link},
                               "need": {"cites": cites, "corr": []}}
            # the other end's display text is shown: withheld from a reader lacking the markings
            # of the values it is read from (as the head's label)
            if need.get("display_marks"):
                item["col"] = canon(need["display_marks"])
            out.append(item)
            seq += 1
            continue
        item["payload"] = {"ev": ev, "event": head}
        out.append(item)
        seq += 1
        for change in event.get("changes") or ():
            c = {k: v for k, v in change.items() if k != "marks"}
            if "marks" not in change:
                raise ValueError(f"{key}: a change without its markings")
            out.append({"page_key": key, "section": SECTION, "seq": seq, "payload": {
                "ev": ev, "change": c}, "req_keys": [], "req_marks": [],
                "col": canon(change["marks"]) if change["marks"] else None, "lin": [],
                "ev_handles": [], "other": None})
            seq += 1
    if isinstance(line.get("conflicts"), dict):
        out += conflict_items(key, line["conflicts"])
    return out, corr


def conflict_items(key: str, block: dict[str, Any]) -> list[dict[str, Any]]:
    """The data conflicts of ``history_batch.conflicts_of`` as items: section ``conflict``, one
    per value of each raw conflict (``c`` its conflict, ``j`` its place), and section ``received``,
    one per receipt dated before the order date. Each item's class is the member record's plus what
    a reader must hold to open the handle it cites, as ``HostAccess._decide`` decides a handle (its
    audience unit; the item gate on a mail, chat or file handle, checked at read through
    ``payload.need.cites``) and the markings the value carries; its lineage is the object that
    handle belongs to (the people exclusion). The page reads ``tool_conflicts`` on the values its
    reader holds, so a value this reader may not open is as absent as in ``backend._conflicts``."""
    out: list[dict[str, Any]] = []
    for c, conflict in enumerate(block["raw"]):
        for j, got in enumerate(conflict["values"]):
            value = dict(got["value"])
            marks = value.pop("markings", None) or []
            handle = value["citation"]["l1_handle"]
            out.append({"page_key": key, "section": CONFLICT, "seq": len(out), "payload": {
                "c": c, "j": j, "kind": conflict.get("kind"), "subject": conflict.get("subject"),
                "tier": conflict.get("tier"), "v": value, "as_of": got.get("as_of"),
                "need": {"cites": []}},
                "req_keys": [got["unit"]] if got.get("unit") else [],
                "req_marks": [canon(marks)] if marks else [], "col": None, "lin": [],
                "ev_handles": [handle], "other": None})
    got = block.get("received")
    if got:
        order_marks = got.get("order_markings") or []
        for when, handle, unit in got["early"]:
            out.append({"page_key": key, "section": RECEIVED, "seq": len(out), "payload": {
                "ordered": got["ordered"], "order_handle": got["order_handle"],
                "when": when, "handle": handle, "need": {"cites": []}},
                "req_keys": [u for u in (unit, got.get("order_unit")) if u],
                "req_marks": [canon(order_marks)] if order_marks else [], "col": None, "lin": [],
                "ev_handles": [handle, got["order_handle"]], "other": None})
    return out


def _requirements(pg: Any, s: str) -> dict[str, int]:
    """What a reader must hold for each link item's other end and evidence objects, as
    ``plx.object`` decides an object (its audience key, its current version's markings, and the
    item gate on that version's handle), set-based: ``pg_temp.rh_req`` one row per (item, object).
    The item gate's part is checked at read through the communications view that holds the
    handle (``served._readable_cites``) when one does; the CURRENT version's handle is the one
    checked (evidence names the version it was read from, often an older change key). An object
    whose gated handle no communications view holds is read by its ``records`` row's class (its
    audience and the full-reader key); one in neither, or evidence naming no stored object, makes
    the item the owner's alone (fail closed). Then the item's keys and marks in ``rh_k`` and its
    ``payload.need.cites``. Returns the counts of each way."""
    from operating.ontology_tools_policy import _ITEM_ACL_SCHEMES
    from operating.pg.access_classes import OWNER_KEY
    gated = sorted(_ITEM_ACL_SCHEMES)
    pg.execute("DROP TABLE IF EXISTS pg_temp.rh_req, pg_temp.rh_ev")
    pg.execute(
        "CREATE TEMP TABLE rh_ev AS SELECT i.page_key, i.section, i.seq, h, ov.object_id FROM "
        "rh_in i CROSS JOIN LATERAL unnest(i.ev_handles) h LEFT JOIN LATERAL (SELECT v.object_id "
        "FROM ontology.object_versions v WHERE v.l1_handle IN (h, split_part(h, '#', 1)) ORDER BY "
        "v.ver DESC LIMIT 1) ov ON true")
    pg.execute(
        "CREATE TEMP TABLE rh_req AS "
        "SELECT page_key, section, seq, other_id AS oid, other_key AS key, other_marks AS marks, "
        "other_cite AS cite, false AS lost FROM rh_in WHERE other_id IS NOT NULL UNION ALL "
        # evidence: the item gate on it alone, as the tab re-checks it (no audience or markings)
        "SELECT e.page_key, e.section, e.seq, e.object_id, NULL, NULL, CASE WHEN "
        "split_part(cv.l1_handle, '://', 1) = ANY(%s) THEN cv.l1_handle END, cv.object_id IS NULL "
        "FROM rh_ev e LEFT JOIN LATERAL "
        "(SELECT v.object_id, v.markings, v.l1_handle FROM ontology.object_versions v WHERE "
        "v.object_id = e.object_id AND v.superseded_at IS NULL ORDER BY v.ver DESC LIMIT 1) cv "
        "ON true", (gated,))
    # the way each requirement is read: own (no item gate), cite (a comms view holds the handle),
    # rec (its records row), owner (neither: fail closed)
    pg.execute(
        "ALTER TABLE rh_req ADD COLUMN way text, ADD COLUMN rkeys text[], ADD COLUMN rmarks text[]")
    pg.execute(
        f"UPDATE rh_req q SET way = CASE WHEN q.lost THEN 'owner' WHEN q.cite IS NULL THEN 'own' "
        f"WHEN EXISTS (SELECT 1 FROM {s}.emails c WHERE c.handle = split_part(q.cite, '#', 1)) OR "
        f"EXISTS (SELECT 1 FROM {s}.teams_messages c WHERE c.handle = q.cite) OR EXISTS (SELECT 1 "
        f"FROM {s}.sharepoint_files c WHERE c.handle = q.cite) THEN 'cite' WHEN r.record_id IS NOT "
        "NULL THEN 'rec' ELSE 'owner' END, rkeys = c.audience_keys, rmarks = c.rec_marks FROM "
        f"(SELECT q2.ctid AS t, r.record_id, r.access_class FROM rh_req q2 LEFT JOIN {s}.records r "
        "ON r.record_id = q2.oid) r LEFT JOIN "
        f"{s}.access_classes c ON c.id = r.access_class WHERE q.ctid = r.t")
    pg.execute(
        "UPDATE rh_k k SET keys = ARRAY(SELECT DISTINCT x FROM unnest(k.keys || a.keys) x ORDER BY "
        "1), marks = ARRAY(SELECT DISTINCT x FROM unnest(k.marks || a.marks) x ORDER BY 1) FROM ("
        "SELECT page_key, section, seq, array_agg(DISTINCT x.k) FILTER (WHERE x.k IS NOT NULL) "
        "AS keys, COALESCE(array_agg(DISTINCT x.m) FILTER (WHERE x.m IS NOT NULL), '{}') AS marks "
        "FROM (SELECT page_key, section, seq, unnest(CASE way WHEN 'rec' THEN rkeys WHEN 'owner' "
        f"THEN ARRAY[%s] ELSE ARRAY[key] END) AS k, NULL::text AS m FROM rh_req UNION ALL SELECT "
        "page_key, section, seq, NULL, unnest(CASE way WHEN 'rec' THEN rmarks WHEN 'owner' THEN "
        "'{}'::text[] ELSE ARRAY[marks] END) FROM rh_req) x GROUP BY 1, 2, 3) a WHERE (a.page_key, "
        "a.section, a.seq) = (k.page_key, k.section, k.seq)", (OWNER_KEY,))
    pg.execute(
        "UPDATE rh_in i SET payload = jsonb_set(i.payload, '{need,cites}', to_jsonb(x.cites)) "
        "FROM (SELECT page_key, section, seq, array_agg(DISTINCT cite ORDER BY cite) AS cites "
        "FROM rh_req WHERE way = 'cite' GROUP BY 1, 2, 3) x WHERE (i.page_key, i.section, i.seq) "
        "= (x.page_key, x.section, x.seq)")
    # a change made the owner's alone only because nothing here can decide it for anyone else
    # (not the people exclusion): every other reader's tab still says some are not shown
    pg.execute("UPDATE rh_in i SET payload = jsonb_set(i.payload, '{partial}', 'true') WHERE "
               "i.section = 'history_head' AND EXISTS (SELECT 1 FROM rh_req q WHERE q.page_key = "
               "i.page_key AND q.way = 'owner' AND q.section = 'history')")
    from operating.pg import access_classes as ac
    with pg.transaction():
        ac.assign(pg, s, "SELECT keys, marks FROM rh_k")
    pg.execute("CREATE TEMP TABLE rh_evids AS SELECT page_key, section, seq, array_agg(DISTINCT "
               "oid) FILTER (WHERE oid IS NOT NULL) AS ids FROM rh_req GROUP BY 1, 2, 3")
    return {f"requirements_{w}": n for w, n in pg.execute(
        "SELECT way, count(*) FROM rh_req GROUP BY 1").fetchall()}


def code_fingerprint() -> str:
    return page_items.code_fp(("operating.pg.record_history", "items"),
                              ("operating.pg.record_history", "load"),
                              ("operating.pg.record_history", "_classes"),
                              ("operating.ontology_tools_history", "object_history"),
                              ("operating.ontology_tools_history", "_links"),
                              ("operator_api.history_batch", "history_of"),
                              ("operator_api.history_batch", "conflicts_of"),
                              ("operating.pg.record_history", "conflict_items"),
                              ("operating.pg.page_items", "apply"))


def _classes(pg: Any, s: str, t: str) -> dict[str, int]:
    """Classes of the staged rows in ``pg_temp.rh_in`` / ``rh_corr`` set-based: the item's (the
    member record's class plus its own requirements), its column class, its lineage source, and
    each corroboration's; then the stage ``t``. Returns counts."""
    from operating.pg import access_classes as ac
    # the member record's row (and its class) for every page key; none: the line is refused
    missing = pg.execute(
        f"SELECT count(DISTINCT i.page_key) FROM rh_in i LEFT JOIN {s}.records r ON r.record_id "
        "= i.page_key WHERE r.record_id IS NULL").fetchone()[0]
    if missing:
        raise RuntimeError(f"{missing} history pages have no records row: not applied")
    odd = pg.execute(
        f"SELECT count(*) FROM (SELECT DISTINCT r.access_class FROM rh_in i JOIN {s}.records r ON "
        f"r.record_id = i.page_key) x JOIN {s}.access_classes c ON c.id = x.access_class WHERE "
        "c.blocked OR c.doc_marks <> '{}'").fetchone()[0]
    if odd:   # a document-resolved or blocked record class has no union here: refused, said
        raise RuntimeError(f"{odd} record classes are blocked or document-resolved: not applied")
    pg.execute(
        "CREATE TEMP TABLE rh_k AS SELECT i.page_key, i.section, i.seq, r.handle AS rhandle, "
        "r.ai_ok, r.biz, ARRAY(SELECT DISTINCT k FROM unnest(c.audience_keys || i.req_keys) k "
        "ORDER BY 1) AS keys, ARRAY(SELECT DISTINCT m FROM unnest(c.rec_marks || i.req_marks) m "
        "ORDER BY 1) AS marks, r.access_class AS own "
        f"FROM rh_in i JOIN {s}.records r ON r.record_id = i.page_key "
        f"JOIN {s}.access_classes c ON c.id = r.access_class")
    pg.execute(
        "CREATE TEMP TABLE rh_ck AS SELECT page_key, seq, i, ARRAY(SELECT DISTINCT k FROM unnest("
        "ARRAY[key]) k WHERE k IS NOT NULL ORDER BY 1) AS keys, ARRAY[marks] AS marks FROM rh_corr "
        "WHERE marks IS NOT NULL")
    with pg.transaction():
        ac.assign(pg, s, "SELECT keys, marks FROM rh_k UNION SELECT keys, marks FROM rh_ck")
        ac.assign_cols(pg, s, "SELECT jsonb_build_object('payload', col) FROM rh_in WHERE col IS "
                       "NOT NULL")
    # each corroboration's class id into its item's payload.need.corr
    pg.execute(
        "UPDATE rh_in i SET payload = jsonb_set(i.payload, '{need,corr}', x.corr) FROM (SELECT "
        "q.page_key, q.seq, jsonb_agg(jsonb_build_object('c', COALESCE(a.id, -1), 'cite', q.cite) "
        "ORDER BY q.i) AS corr FROM rh_corr q LEFT JOIN rh_ck k ON (k.page_key, k.seq, k.i) = ("
        f"q.page_key, q.seq, q.i) LEFT JOIN {s}.access_classes a ON a.audience_keys = k.keys AND "
        "a.rec_marks = k.marks AND a.doc_marks = '{}' AND NOT a.blocked GROUP BY q.page_key, "
        "q.seq) x WHERE (i.page_key, i.seq) = (x.page_key, x.seq) AND i.section = 'history'")
    # every corroboration found its class (or is one no reader reads, -1 by construction)
    lost = pg.execute("SELECT count(*) FROM rh_corr q JOIN rh_ck k ON (k.page_key, k.seq, k.i) "
                      f"= (q.page_key, q.seq, q.i) LEFT JOIN {s}.access_classes a ON "
                      "a.audience_keys = k.keys AND a.rec_marks = k.marks AND a.doc_marks = '{}' "
                      "AND NOT a.blocked WHERE a.id IS NULL").fetchone()[0]
    if lost:
        raise RuntimeError(f"{lost} corroborations without a class: not applied")
    lost = _requirements(pg, s)
    staged = pg.execute(
        f"INSERT INTO {t} (page_kind, page_key, section, seq, payload, access_class, col_class, "
        "ai_ok, ver, biz, cellcol, src_handle, source_systems, handle, as_of, dep_ver, code_fp, "
        "src_view, lin_ids) SELECT %s, i.page_key, i.section, i.seq, i.payload, a.id, "
        "COALESCE(cc.id, 0), k.ai_ok, COALESCE((SELECT max(v.ver) FROM ontology.object_versions v "
        "WHERE v.object_id = ANY(i.lin || COALESCE(e.ids, '{}'))), 0), k.biz AND "
        "cardinality(i.ev_handles) = 0 AND COALESCE(jsonb_array_length(i.payload -> 'need' -> "
        "'cites'), 0) = 0, CASE WHEN i.col IS NOT NULL THEN 'payload' END, k.rhandle, NULL, '', "
        "%s::timestamptz, 0, '', 'records', ARRAY(SELECT DISTINCT x FROM unnest(ARRAY["
        "i.page_key] || i.lin || COALESCE(e.ids, '{}')) x ORDER BY 1) "
        "FROM rh_in i JOIN rh_k k ON (k.page_key, k.section, k.seq) = (i.page_key, i.section, "
        f"i.seq) JOIN {s}.access_classes a ON a.audience_keys = k.keys AND a.rec_marks = k.marks "
        "AND a.doc_marks = '{}' AND NOT a.blocked "
        f"LEFT JOIN {s}.col_classes cc ON i.col IS NOT NULL AND cc.marks = jsonb_build_object("
        "'payload', i.col) AND cc.needs = '{}'::jsonb "
        "LEFT JOIN rh_evids e ON (e.page_key, e.section, e.seq) = (i.page_key, i.section, i.seq)",
        (KIND, AS_OF[0])).rowcount
    # the head carries the watermarks the record was read at (``moved_sql`` reads them)
    pg.execute(f"UPDATE {t} SET dep_ver = (payload #>> '{{deps,ver}}')::bigint WHERE section = "
               "'history_head' AND jsonb_typeof(payload -> 'deps') = 'object'")
    rows = pg.execute("SELECT count(*) FROM rh_in").fetchone()[0]
    if staged != rows:   # an item whose class was not found is never left out silently
        raise RuntimeError(f"{rows} history items read, {staged} staged: not applied")
    return {"items": rows, **lost}


AS_OF: list[str] = [""]


def load(dsn: str, files: Sequence[str], schema: str | None = None, *,
         job: str = "record_history.load", ensure: bool = True) -> dict[str, Any]:
    """Every line of ``files`` (``history_batch`` output) staged and applied to the served store
    for the records they name (``page_items.apply`` with those keys: other records' items stay).
    A record whose line is an error is not stored and is counted; the apply refuses on any
    item it cannot class. Returns the apply's counts plus ``records``, ``failed``."""
    import psycopg
    s = schema or bv.SCHEMA
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    corr: list[dict[str, Any]] = []
    keys: list[str] = []
    failed: list[str] = []
    refused: dict[str, str] = {}
    errors: dict[str, str] = {}     # a record the builder could not read: its error, said
    stale: list[str] = []           # stored histories whose sources moved: shown with their age
    reset: list[str] = []           # whose audience moved: taken out until rebuilt (fail closed)
    for path in files:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = json.loads(raw)
                if "stamps" in line:
                    continue
                if "sweep" in line or "stale" in line or "reset" in line:   # the resident's sweep
                    stale += [k for k in line.get("stale") or ()]
                    reset += [k for k in line.get("reset") or ()]
                    continue
                if "error" in line:
                    failed.append(line["record_id"])
                    errors[line["record_id"]] = str(line["error"])
                    continue
                try:
                    got, c = items(line)
                except (ValueError, KeyError, TypeError) as exc:
                    refused[line.get("record_id", "?")] = f"{type(exc).__name__}: {exc}"
                    continue
                rows += got
                corr += c
                keys.append(line["record_id"])
    AS_OF[0] = dt.datetime.now(dt.timezone.utc).isoformat()
    with psycopg.connect(dsn, autocommit=True) as pg:
        # the disk floor and WAL budget for what this load writes: measured on the full PO fill
        # (11-23, copy): 2.2 GB of lines -> 2.5 GB of items + lineage, so 1.5 x the input
        size = sum(os.path.getsize(f) for f in files)
        with page_items.room(pg, max(0.1, 1.5 * size / 1e9), job):
            return {**_load(pg, s, rows, corr, keys, failed, refused, started, ensure,
                            stale=stale, reset=reset), "errors": errors}


def _load(pg: Any, s: str, rows: list, corr: list, keys: list, failed: list, refused: dict,
          started: float, ensure: bool, stale: Sequence[str] = (), reset: Sequence[str] = ()
          ) -> dict[str, Any]:
    """Stage and apply ``rows`` for ``keys``; ``reset``: pages whose audience moved and that have
    no new line yet: their items are taken out (nothing a narrowed reader could still read stays)
    and the page is requested again; ``stale``: pages whose sources moved are marked (``stale_since``)."""
    if ensure:
        page_items.ensure(pg, s)
    pg.execute("SET statement_timeout = 0")
    stage = page_items.stage(pg, KIND, s)
    with pg.transaction():
        pg.execute("DROP TABLE IF EXISTS pg_temp.rh_in, pg_temp.rh_corr, pg_temp.rh_k, "
                   "pg_temp.rh_ck, pg_temp.rh_ev, pg_temp.rh_req, pg_temp.rh_evids")
        pg.execute("CREATE TEMP TABLE rh_in (page_key text, section text, seq int, payload "
                   "jsonb, req_keys text[], req_marks text[], col text, lin text[], "
                   "ev_handles text[], other_id text, other_key text, other_marks text, "
                   "other_cite text)")
        pg.execute("CREATE TEMP TABLE rh_corr (page_key text, seq int, i int, id text, key "
                   "text, marks text, cite text)")
        with pg.cursor().copy("COPY rh_in FROM STDIN") as cp:
            for r in rows:
                cp.write_row((r["page_key"], r["section"], r["seq"],
                              json.dumps(r["payload"]), r["req_keys"], r["req_marks"],
                              r["col"], r["lin"], r["ev_handles"],
                              *(r["other"] or (None, None, None, None))))
        with pg.cursor().copy("COPY rh_corr FROM STDIN") as cp:
            for r in corr:
                cp.write_row((r["page_key"], r["seq"], r["i"], r["id"], r["key"],
                              r["marks"], r["cite"]))
        pg.execute("CREATE INDEX ON rh_in (page_key, section, seq)")
        counts = _classes(pg, s, stage)
    gone = [k for k in dict.fromkeys(reset) if k not in set(keys)]
    out = page_items.apply(pg, KIND, stage, keys=keys + gone, code_fp=code_fingerprint(),
                           job="record_history.load", schema=s,
                           notes={"failed": len(failed), "refused": len(refused)}) \
        if keys or gone else {}
    if not (keys or gone):
        pg.execute(f"DROP TABLE IF EXISTS {stage}")
    if keys:
        stamp_audience(pg, s, keys)
    marked = page_items.mark_stale(pg, KIND, [k for k in stale if k not in set(keys + gone)], s) \
        if stale else 0
    if gone:
        pg.execute(f"INSERT INTO {s}.page_requests (page_kind, page_key) SELECT %s, unnest(%s::text[]) "
                   "ON CONFLICT DO NOTHING", (KIND, gone))
    return {**out, **counts, "stale": marked, "reset": len(gone), "records": len(keys), "failed": len(failed),
        "refused": refused, "seconds": round(time.monotonic() - started, 1)}


PULL_DIR = "plx-run/files/data/plx-history/out"   # the resident builder's --out on the Spark
PULL_BATCH = 40                                    # files loaded in one apply
PULL_LOCK = "plx_record_history_pull"
PULL_FAILING_S = 300                               # failing this long: the unit fails (alarmed)
LOG = logging.getLogger("operating.pg.record_history")
SWEEP_STALE_S = 600       # a resident shard's sweep (every 120 s) unseen this long: alarmed


class SweepWatch:
    """The resident builder's shards, each sweeping every ``SWEEP_EVERY`` s: an unswept shard
    leaves narrowed readers seeing old items without bound, so a shard whose sweep marker (the
    ``sweep`` line of its files, seen at the box's own clock) is older than ``SWEEP_STALE_S`` is
    named. Before any marker is seen the count of shards is unknown: no marker at all is stale."""

    def __init__(self, now: float) -> None:
        self.since = now
        self.seen: dict[int, float] = {}
        self.shards: int | None = None

    def note(self, shards: Sequence[str], now: float) -> None:
        for sh in shards:
            i, n = (int(x) for x in str(sh).split("/"))
            self.seen[i] = now
            self.shards = n

    def stale(self, now: float) -> list[str]:
        if self.shards is None:
            return ["no sweep marker seen"] if now - self.since > SWEEP_STALE_S else []
        return [f"shard {i}/{self.shards}" for i in range(self.shards)
                if now - self.seen.get(i, self.since) > SWEEP_STALE_S]


def pull_once(dsn: str, transport: Any, schema: str | None = None) -> dict[str, Any]:
    """The box's side of ``operator_api.history_resident``: fetch every waiting file, load it
    (``load``: applied for its records only) and take it off the Spark. Every file is read whole
    (no size bound). The disk budget refusing: said, the files wait (``disk_refused``). A batch
    that fails is loaded file by file; a file that fails alone is set aside in ``bad/`` (said).
    A record the builder could not read is counted and its error logged."""
    import tempfile
    import psycopg
    from operating import disk_budget
    total: dict[str, Any] = {"files": 0, "records": 0, "failed": 0, "bad": 0, "inserted": 0,
                             "updated": 0, "deleted": 0}
    names = transport.names()
    sweeps: list[str] = []      # the shards whose sweep marker these files carry

    def one(group: Sequence[str], texts: dict[str, str]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="plx-history-") as d:
            paths = []
            for n in group:
                path = os.path.join(d, n)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(texts[n])
                paths.append(path)
            return load(dsn, paths, schema, job="record_history.pull", ensure=False)

    def add(got: dict[str, Any], group: Sequence[str]) -> None:
        for k in ("records", "failed", "inserted", "updated", "deleted"):
            total[k] += int(got.get(k) or 0)
        for rid, err in (got.get("errors") or {}).items():
            LOG.warning("history: record %s not built by the resident builder: %s", rid, err)
        for rid, err in (got.get("refused") or {}).items():
            LOG.warning("history: record %s refused by the loader: %s", rid, err)
        total["failed"] += len(got.get("refused") or {})
        transport.drop(list(group))
        total["files"] += len(group)

    for at in range(0, len(names), PULL_BATCH):
        chunk = names[at:at + PULL_BATCH]
        texts = transport.read(chunk)
        unread = [n for n in chunk if n not in texts]
        if unread:   # listed a moment ago: gone or unreadable now; said, tried next round
            LOG.warning("history: %d files listed but not read: %s", len(unread), unread)
        group = [n for n in chunk if n in texts]
        if not group:
            continue
        for n in group:
            for raw in texts[n].splitlines():
                if raw.startswith("{") and '"sweep"' in raw:
                    try:
                        sweeps.append(json.loads(raw)["sweep"]["shard"])
                    except (ValueError, KeyError, TypeError):
                        LOG.warning("history: %s has a sweep line that is not readable", n)
        try:
            add(one(group, texts), group)
        except disk_budget.Refused as exc:
            LOG.error("history: %s; %d files wait on the Spark", exc, len(names) - at)
            total["disk_refused"] = str(exc)
            return total
        except psycopg.errors.LockNotAvailable:
            LOG.info("history: the store is locked; the files wait")
            total["waited"] = len(names) - at
            return total
        except Exception as exc:  # noqa: BLE001 - then one file at a time, said
            LOG.warning("history: a batch of %d files failed (%s: %s); one by one", len(group),
                        type(exc).__name__, exc)
            for n in group:
                try:
                    add(one([n], texts), [n])
                except disk_budget.Refused as err:
                    LOG.error("history: %s; the files wait on the Spark", err)
                    total["disk_refused"] = str(err)
                    return total
                except Exception as err:  # noqa: BLE001 - this file alone is bad: aside, said
                    LOG.error("history: %s set aside (%s: %s)", n, type(err).__name__, err)
                    transport.drop([n], bad=True)
                    total["bad"] += 1
    total["sweeps"] = sweeps
    return total


def pull(dsn: str, host: str, folder: str, every: float, once: bool) -> int:
    import psycopg
    from operating.pg.plan_sections_pull import SshTransport
    transport = SshTransport(host, folder, max_bytes=None)
    held = psycopg.connect(dsn, autocommit=True)
    try:
        if not held.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                            (PULL_LOCK,)).fetchone()[0]:
            LOG.error("history pull: another puller holds the lock; this one ends")
            return 0
        page_items.ensure(held, bv.SCHEMA)
        failing_since: float | None = None
        watch = SweepWatch(time.monotonic())
        while True:
            try:
                got = pull_once(dsn, transport)
            except Exception as exc:  # noqa: BLE001 - the Spark or the database is away: again
                LOG.warning("history pull failed: %s: %s", type(exc).__name__, exc)
                got = None
            if got and (got["files"] or got.get("disk_refused") or got["bad"]):
                LOG.info("history put: %s", json.dumps(got))
            if got:
                watch.note(got.get("sweeps") or (), time.monotonic())
            late = watch.stale(time.monotonic())
            if late:
                # an unswept shard: narrowed readers keep seeing old items without bound
                LOG.error("history pull: no sweep marker from %s for over %d s; failing",
                          ", ".join(late), SWEEP_STALE_S)
                return 1
            if once:
                return 1 if got is None or got.get("disk_refused") or got["bad"] else 0
            if got is None or got.get("disk_refused"):
                failing_since = failing_since or time.monotonic()
                if time.monotonic() - failing_since > PULL_FAILING_S:
                    # nothing stored for this long: fail the unit, so the host's alarm (OnFailure
                    # of every plx- unit) says so, and systemd starts it again
                    LOG.error("history pull: nothing stored for %d s; failing", PULL_FAILING_S)
                    return 1
            else:
                failing_since = None
            time.sleep(every)
    finally:
        held.close()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m operating.pg.record_history")
    ap.add_argument("command", choices=("load", "pull"))
    ap.add_argument("files", nargs="*")
    ap.add_argument("--schema")
    ap.add_argument("--dsn")
    ap.add_argument("--host", default="s7d3d")
    ap.add_argument("--dir", default=PULL_DIR)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    from operating.pg import config
    dsn = args.dsn or config.dsn()
    if args.command == "pull":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
        return pull(dsn, args.host, args.dir, args.every, args.once)
    if not args.files:
        ap.error("load needs files")
    print(json.dumps(load(dsn, args.files, args.schema)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
