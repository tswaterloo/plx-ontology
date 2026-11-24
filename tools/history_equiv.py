"""The History tab's gate (speed specs 8.5 per family, 9.4 item 1): for each reader and record, every
page of the served tab (``served.history`` with the reader's door decided as a page read decides it,
``page_reads.grant_ahead``) against every page of the old tab (``history.history`` through the
reader's own workspace view, as ``/api/history`` reads it), in one console process on a build copy.

  python3 -m tools.history_equiv --ids FILE --reader NAME=EMPLOYEE_ID ... --out F

Each (reader, record) is ``same``, ``different`` or ``old_failed``; differences are classified as
``tools/page_equiv.classify`` does (one section ``history``: the entries of every page in order,
plus ``partial`` and not-found). Prints ``EQUIV history:PurchaseOrder same=a different=b
old_failed=c`` per reader and in all; a ``stored_extra`` or ``withheld`` difference for any reader
but the owner is printed as ``EQUIV PERMISSION FINDING`` (exit 2). JSONL of every difference to
``--out``.

EXPLAINED DIFFERENCES (``ACCEPTED``): a non-owner reader's difference is ``accepted`` only when it
is one of the approved classes AND the copy's own rows prove it, per entry and per
record (``own_mail_facts`` / ``partial_facts`` read them; ``why_not_own_mail`` /
``why_not_excluded_only`` decide). Anything else, or anything the rows do not prove, stays
``different`` with the reason it was not accepted. ``different`` counts only those, so the go
line ``different=0`` means 0 unexplained.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from types import SimpleNamespace
from typing import Any, Sequence


class _Reasons:
    """``view`` whose ``plx.object`` answers' ``withheld.reason`` are kept (the old tab says only
    that something is withheld; the gate's evidence needs why)."""

    def __init__(self, view: Any) -> None:
        self._view, self.reasons = view, set()

    def _call(self, name: str, args: Any) -> Any:
        got = self._view._call(name, args)
        withheld = (got or {}).get("withheld") if isinstance(got, dict) else None
        if withheld:
            self.reasons.add(str(withheld.get("reason")))
        return got

    def __getattr__(self, name: str) -> Any:
        return getattr(self._view, name)


def walk_old(view: Any, oid: str) -> Any:
    from operator_api import history as hist
    from operator_api.errors import RecordNotFound
    from operator_api.serving import object_url
    entries, partial, cursor = [], False, None
    spy = _Reasons(view)
    while True:
        with view._request():
            try:
                page = hist.history(spy, oid, cursor, open_url=object_url)
            except RecordNotFound:
                return "not_found"
        entries += [e.model_dump(mode="json") for e in page.entries]
        partial = partial or page.partial
        if page.next_cursor is None:
            return {"entries": entries, "partial": partial, "label": page.label,
                    "reasons": sorted(spy.reasons)}
        cursor = page.next_cursor


def walk_new(backend: Any, oid: str) -> Any:
    from operator_api import served
    from operator_api.errors import RecordNotFound
    entries, partial, cursor = [], False, None
    while True:
        try:
            page = served.history(backend, object(), oid, cursor)
        except RecordNotFound:
            return "not_found"
        except served.Unavailable as exc:
            return f"unavailable:{exc.code}"
        entries += [e.model_dump(mode="json") for e in page.entries]
        partial = partial or page.partial
        if page.next_cursor is None:
            return {"entries": entries, "partial": partial, "label": page.label}
        cursor = page.next_cursor


def differences(old: Any, new: Any) -> list[dict[str, Any]]:
    from tools.page_equiv import classify
    if isinstance(old, str) or isinstance(new, str):
        if old == new:
            return []
        kind = "stored_extra" if old == "not_found" else "stored_missing"
        return [{"section": "page", "kind": kind, "key": f"{old} -> {new}"}]
    out = classify({"history": old["entries"]}, {"history": new["entries"]})
    if old["partial"] != new["partial"]:
        out.append({"section": "partial", "kind": "value",
                    "key": f"old {old['partial']} new {new['partial']}"})
    if old["label"] != new["label"]:
        out.append({"section": "label", "kind": "value", "key": f"{old['label']!r} -> "
                    f"{new['label']!r}"})
    return out


# The approved difference classes (a non-owner reader only; the owner's tab never differs).
ACCEPTED = {
    "own_mail": "ruling 11-23 (owner rule 11-17: staff see all business records plus "
                "their own mail and chats): a link the new tab shows to an object of the "
                "reader's OWN mailbox, resting on no mail but the reader's own, which the old "
                "tab's capture-ACL gate hid (speed specs 8.5 case 2, decided)",
    "own_chat": "the same ruling for chats: a link to a Teams message of a chat the reader is a "
                "member of (the chat's captured membership, decided by the Teams item gate the "
                "reader's door runs), resting on no mail but the reader's own",
    "excluded_only": "speed specs 8.5 case (1): the old tab said 'some changes are not shown' "
                     "and the new does not, and every change the reader's door holds back of the "
                     "record is the owner's alone by the people exclusion (never counted for "
                     "anyone else: the old flag revealed them)",
}
LINK_KINDS = ("link_added", "link_removed")


def own_mail_facts(pg: Any, schema: str, emp: str, oid: str, entry: Any) -> dict[str, Any]:
    """What the copy holds for an entry the new tab shows reader ``emp`` and the old did not:
    the link's other end (its audience key, whether it is in the people exclusion), and every
    stored item of ``oid`` that is that link with each of its cites' mailbox."""
    from operating.pg.people_excluded import TABLE as EXCLUDED
    link = entry.get("link") if isinstance(entry, dict) else None
    other = link.get("object_id") if isinstance(link, dict) else None
    out: dict[str, Any] = {"kind": entry.get("kind") if isinstance(entry, dict) else None,
                           "other": other, "emp": emp}
    if not isinstance(other, str):
        return out
    row = pg.execute("SELECT audience_key FROM ontology.objects WHERE object_id = %s",
                     (other,)).fetchone()
    out["audience"] = row[0] if row else None
    out["excluded"] = pg.execute(f"SELECT count(*) FROM {EXCLUDED} WHERE object_id = %s",
                                 (other,)).fetchone()[0]
    # a Teams message: its row in the chats view (by the object's current version's handle)
    chat = pg.execute(
        f"SELECT t.handle, t.excluded FROM ontology.object_versions v JOIN {schema}."
        "teams_messages t ON t.handle = v.l1_handle WHERE v.object_id = %s AND v.superseded_at "
        "IS NULL AND %s = ANY(t.audience_keys)", (other, out["audience"])).fetchone()
    out["chat"] = list(chat) if chat else None
    items = []
    for seq, cites in pg.execute(
            f"SELECT seq, COALESCE(payload -> 'need' -> 'cites', '[]'::jsonb) FROM {schema}."
            "page_items WHERE page_kind = 'history' AND page_key = %s AND section = 'history' "
            "AND payload #>> '{event,link,other,object_id}' = %s AND payload -> 'event' ->> "
            "'kind' = %s", (oid, other, out["kind"])).fetchall():
        boxes = {}
        for c in cites:
            got = pg.execute(f"SELECT mailbox, excluded FROM {schema}.emails WHERE handle = %s",
                             (c.split("#", 1)[0],)).fetchone()
            boxes[c] = list(got) if got else None
        items.append({"seq": seq, "cites": boxes})
    out["items"] = items
    return out


def why_not_own_mail(f: dict[str, Any]) -> tuple[str | None, str]:
    """(None, class) when the facts prove the entry is a link to the reader's own mail
    (``own_mail``) or own chat (``own_chat``: ``f['chat_readable']`` = the reader's door reads
    that message); else (why not, class)."""
    if f.get("kind") not in LINK_KINDS or not isinstance(f.get("other"), str):
        return "not a link entry", "own_mail"
    cls = "own_mail"
    if f.get("audience") != f"mail:{f['emp']}":
        chat = f.get("chat")
        if not chat:
            return (f"other end's audience is {f.get('audience')!r}: not the reader's mailbox "
                    "nor a chat message"), cls
        cls = "own_chat"
        if chat[1] is not False:
            return "the chat message is excluded", cls
        if not f.get("chat_readable"):
            return "the reader is not a member of the chat (their door does not read it)", cls
    why = _rest(f, chat_ok=cls == "own_chat")
    return why, cls


def _rest(f: dict[str, Any], chat_ok: bool = False) -> str | None:
    if f.get("excluded"):
        return "other end is in the people exclusion"
    if not f.get("items"):
        return "no stored item is that link"
    for item in f["items"]:
        for cite, box in item["cites"].items():
            if box is None:
                # an own chat's link resting on that chat message itself (readable: a member)
                if chat_ok and cite == (f.get("chat") or [None])[0] and f.get("chat_readable"):
                    continue
                return f"cite {cite} is not a stored mail"
            if box[0] != f["emp"] or box[1] is not False:
                return f"cite {cite} is in mailbox {box[0]!r} (excluded={box[1]!r})"
    return None


def partial_facts(pg: Any, schema: str, oid: str, visible: set,
                  old_reasons: Sequence[str]) -> dict[str, Any]:
    """Every history item of ``oid`` the reader's door does not return (``visible``: the
    (section, seq) pairs it does), each with its class's audience keys and whether its lineage
    names an object of the people exclusion; the head's own partial flag; the old tab's
    withheld reasons."""
    from operating.pg.access_classes import OWNER_KEY
    from operating.pg.people_excluded import TABLE as EXCLUDED
    rows = pg.execute(
        f"SELECT p.section, p.seq, c.audience_keys, p.payload, EXISTS (SELECT 1 FROM {schema}."
        "lineage l, unnest(l.object_ids || l.read_ids) o WHERE l.view = 'page_items' AND "
        f"l.handle = p.handle AND o IN (SELECT object_id FROM {EXCLUDED})) FROM {schema}."
        f"page_items p JOIN {schema}.access_classes c ON c.id = p.access_class WHERE "
        "p.page_kind = 'history' AND p.page_key = %s", (oid,)).fetchall()
    head = next((r[3] for r in rows if r[0] == "history_head"), None)
    hidden = [{"section": sec, "seq": seq, "owner_only": OWNER_KEY in (keys or ()),
               "excluded_lineage": bool(hit)}
              for sec, seq, keys, _p, hit in rows if (sec, seq) not in visible]
    return {"head_partial": bool((head or {}).get("partial")), "hidden": hidden,
            "items": len(rows), "old_reasons": list(old_reasons)}


def why_not_excluded_only(f: dict[str, Any]) -> str | None:
    """None when the facts prove the old flag stood only for the people exclusion's rows."""
    if f.get("head_partial"):
        return "the head says partial (a change no one but the owner can be decided for)"
    if not f.get("hidden"):
        return "the door holds back nothing of this record: the old flag is not explained"
    bad = [h for h in f["hidden"] if not (h["owner_only"] and h["excluded_lineage"])]
    if bad:
        return f"{len(bad)} held-back items are not the owner's alone by the people exclusion"
    return None


def explain(pg: Any, schema: str, emp: str, oid: str, diff: list[dict[str, Any]],
            visible_of: Any, old_reasons: Sequence[str], readable_of: Any) -> None:
    """Mark each difference ``accepted`` (a class of ``ACCEPTED``, with its ``evidence``) or
    ``unexplained`` (why not), in place. ``visible_of(oid)``: the reader's visible items;
    ``readable_of(handles)``: those of the communications handles the reader's door reads."""
    for d in diff:
        if d["section"] == "history" and d["kind"] == "stored_extra":
            try:
                entry = json.loads(d["key"])
            except (TypeError, ValueError):
                d["unexplained"] = "the entry is not readable"
                continue
            facts = own_mail_facts(pg, schema, emp, oid, entry)
            if facts.get("chat"):
                facts["chat_readable"] = facts["chat"][0] in readable_of([facts["chat"][0]])
            why, cls = why_not_own_mail(facts)
        elif d["section"] == "partial" and d["key"] == "old True new False":
            facts = partial_facts(pg, schema, oid, visible_of(oid), old_reasons)
            why, cls = why_not_excluded_only(facts), "excluded_only"
        else:
            d["unexplained"] = "not an approved difference class"
            continue
        d["evidence"] = facts
        if why is None:
            d["accepted"] = cls
        else:
            d["unexplained"] = why


def readable_handles(door: Any, dsn: str, comms: Any, handles: Sequence[str]) -> set:
    """The handles of ``handles`` the reader's door reads through the communications views'
    own guard (the tab's ``served._readable_cites``)."""
    from operating.ontology_tools_sql import Door, scoped_read
    from operating.page_reads import pool_for
    from operating.pg import sql_views
    from operator_api import served
    person = sql_views.person_schema()

    def work(pg: Any) -> Any:
        return [r[0] for r in pg.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relname = ANY(%s)",
            (person, ["emails", "teams_messages", "sharepoint_files"]))]

    def after(pg: Any, built: Any) -> Any:
        return served._readable_cites(pg, person, built, list(handles))
    with pool_for(dsn).connection() as pg:
        return scoped_read(pg, Door(door.grant), work, served.READ_SECONDS, person=True,
                           lock_ms=served.LOCK_MS, extra=comms, after=after)[1]


def visible_items(door: Any, dsn: str, comms: Any, oid: str) -> set:
    """The (section, seq) history items of ``oid`` the reader's door returns (the tab's read)."""
    from operating.ontology_tools_sql import Door, scoped_read
    from operating.page_reads import pool_for
    from operator_api import served

    def work(pg: Any) -> Any:
        return {(r[0], r[1]) for r in pg.execute(
            "SELECT section, seq FROM page_items WHERE page_kind = 'history' AND page_key = "
            "%(key)s", {"key": oid})}
    with pool_for(dsn).connection() as pg:
        return scoped_read(pg, Door(door.grant), work, served.READ_SECONDS, person=True,
                           lock_ms=served.LOCK_MS, extra=comms)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m tools.history_equiv")
    ap.add_argument("--ids", required=True)
    ap.add_argument("--reader", action="append", required=True, help="NAME=EMPLOYEE_ID")
    ap.add_argument("--owner", default="owner", help="the reader name that is the owner")
    ap.add_argument("--out", required=True)
    ap.add_argument("--host-config", default=os.environ.get("PLX_BATCH_HOST_CONFIG"))
    args = ap.parse_args(argv)
    from operator_api.plan_batch import console_args
    from operator_api.workspace import case_console
    dsn = os.environ.get("PLX_PG_DSN")
    if not args.host_config or not dsn:
        print("PLX_BATCH_HOST_CONFIG and PLX_PG_DSN are required", file=sys.stderr)
        return 2
    readers = [tuple(r.split("=", 1)) for r in args.reader]
    with open(args.ids, encoding="utf-8") as fh:
        ids = [x.strip() for x in fh if x.strip()]

    def serve(ctx: dict[str, Any]) -> int:
        from operating.page_reads import grant_ahead
        from operating.session_identity import warm_session
        import psycopg
        from operating.pg import business_views as bv
        schema = bv.SCHEMA
        total = {"same": 0, "accepted": 0, "different": 0, "old_failed": 0}
        accepted: dict[str, int] = {}     # accepted differences by class (entries / flags)
        finding = False
        with open(args.out, "w", encoding="utf-8") as out:
            for name, emp in readers:
                session = warm_session(ctx["resolver"], emp, expires_at=int(time.time()) + 6 * 3600)
                view = ctx["objects_for"](session)
                began = time.monotonic()
                with view._request():
                    door, dsn_, _stamp, comms = grant_ahead(view)
                decided = round(time.monotonic() - began, 1)
                backend = SimpleNamespace(doors=SimpleNamespace(
                    get=lambda r, h=(door, dsn_, comms): h, decide_now=lambda r: None))
                counts = {"same": 0, "accepted": 0, "different": 0, "old_failed": 0}
                for oid in ids:
                    t0 = time.monotonic()
                    try:
                        old = walk_old(view, oid)
                    except Exception as exc:  # noqa: BLE001 - counted as old_failed, said
                        counts["old_failed"] += 1
                        out.write(json.dumps({"reader": name, "id": oid, "old_failed":
                                              f"{type(exc).__name__}: {exc}"}) + "\n")
                        continue
                    t1 = time.monotonic()
                    new = walk_new(backend, oid)
                    t2 = time.monotonic()
                    diff = differences(old, new)
                    if diff and name == args.owner:
                        for d in diff:
                            d["unexplained"] = "the owner's tab never differs"
                    elif diff:
                        with psycopg.connect(dsn) as pg:
                            explain(pg, schema, emp, oid, diff, lambda k: visible_items(
                                door, dsn_, comms, k), old.get("reasons") or ()
                                if isinstance(old, dict) else (), lambda hs: readable_handles(
                                door, dsn_, comms, hs))
                    open_ = [d for d in diff if "accepted" not in d]
                    for d in diff:
                        if "accepted" in d:
                            accepted[d["accepted"]] = accepted.get(d["accepted"], 0) + 1
                    counts["different" if open_ else ("accepted" if diff else "same")] += 1
                    if open_ and name != args.owner and any(
                            d["kind"] in ("stored_extra", "withheld") for d in open_):
                        finding = True
                    out.write(json.dumps({"reader": name, "id": oid, "diff": diff,
                                          "unexplained": len(open_),
                                          "n_diff": len(diff), "old_s": round(t1 - t0, 2),
                                          "new_s": round(t2 - t1, 3),
                                          "entries": None if isinstance(old, str)
                                          else len(old["entries"])}) + "\n")
                    out.flush()
                for k in total:
                    total[k] += counts[k]
                print(f"EQUIV history:PurchaseOrder reader={name} same={counts['same']} "
                      f"accepted={counts['accepted']} different={counts['different']} "
                      f"old_failed={counts['old_failed']} "
                      f"(door decided in {decided} s)", flush=True)
        print(f"EQUIV history:PurchaseOrder same={total['same']} accepted={total['accepted']} "
              f"different={total['different']} old_failed={total['old_failed']} (different = "
              "unexplained)", flush=True)
        for cls, n in sorted(accepted.items()):
            print(f"EQUIV accepted {cls}: {n} differences, each proved on the copy's rows "
                  f"({ACCEPTED[cls]})", flush=True)
        if finding:
            print("EQUIV PERMISSION FINDING: a non-owner reader got more or less withheld on the "
                  "served tab (see --out)", flush=True)
            return 2
        return 0 if total["different"] == 0 and total["old_failed"] <= 2 else 1
    return case_console.main(console_args(args.host_config, dsn), serve_with=serve)


if __name__ == "__main__":
    sys.exit(main())
