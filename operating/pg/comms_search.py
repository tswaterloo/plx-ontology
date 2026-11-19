"""Search and open over the communications tables (ARCH-FINAL-1117 section 14): the simple
Ask's ``plx.search`` / ``plx.open`` for mail and Teams.

One ranked top-k statement per search: the reader's guarded views (``business_comms.
view_ddl``: unit held, Freightcom decided at build, the people exclusion, hidden ids, the
Teams history window) joined to the text index. With query words, a row whose own header (a
mail's subject and addresses, a message's subject and sender, a file's name and folder) holds
every word ranks first, then the rows matched in their text; newest first within each
(``tier``: the cursor carries it). The permission predicate is
the view's, inside the statement: no scan-then-gate and no item gate inside a row-limited
walk. The words match the header (the table's own ``words``: subject and addresses, GIN)
or the body (``ontology.chunk_words`` of the version's document text, GIN); the planner
walks the date index for common words and starts from the word indexes for rare ones.
Party (an address, a domain, a customer or supplier) and dates are plain predicates on the
table (GIN over ``addresses`` / ``domains`` / ``customers`` / ``suppliers``).

``open`` reads one row through the same view by its handle and that version's document text
in the same statement: no visible row, no text. Marked text fields are never read (fail
closed: the row's own marks are in its unit; a field's are not)."""
from __future__ import annotations

import datetime as dt
import re
import zlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

# view -> (hit kind, its date column, the contract's source name)
VIEWS = {"emails": ("email", "received_at", "graph_mail"),
         "teams_messages": ("teams_message", "created_at", "teams"),
         "sharepoint_files": ("sharepoint_file", "modified_at", "sharepoint")}
# Searched only, never opened here (``business_comms.FILES``: its rows are admitted only
# inside a search statement, and each is re-gated by the item gate): a file is opened the
# old way, whose own gates decide it
SEARCH_ONLY = frozenset({"sharepoint_files"})
# a row's title and sender, per view (default: its subject and sender)
_TITLE = {"sharepoint_files": "coalesce(nullif(btrim(e.path), ''), e.name)"}
_SENDER = {"sharepoint_files": "NULL::text"}
SNIPPET_CHARS = 240
MAX_TERMS = 16
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class SearchInputError(ValueError):
    pass


@dataclass(frozen=True)
class Party:
    kind: str            # "address" | "domain" | "record"
    value: Any           # the address, the domain, or the biz handles of the record


def terms(query: str) -> list[str]:
    """The query's words (the word index's own folding happens in ``words.tsquery``)."""
    got = _WORD.findall(query or "")
    if not got:
        raise SearchInputError("the query has no word to search for")
    return got[:MAX_TERMS]


def tsquery(query: str) -> str:
    """Every word of ``query`` (AND), as the word index's tokenizer cuts it."""
    import sqlite3
    from operating.pg import words
    try:
        return words.tsquery(" ".join('"' + t.replace('"', '""') + '"' for t in terms(query)))
    except sqlite3.OperationalError as exc:
        raise SearchInputError(f"the query cannot be searched: {exc}") from None


def instant(text: str | None, *, end: bool = False) -> dt.datetime | None:
    """A ``since`` / ``until`` bound: a day (``until``: the whole day) or an instant."""
    if text is None:
        return None
    try:
        if len(text) == 10:
            day = dt.date.fromisoformat(text)
            at = dt.datetime.combine(day + dt.timedelta(days=1 if end else 0), dt.time(),
                                     tzinfo=dt.timezone.utc)
            return at
        got = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise SearchInputError(f"not a date: {text[:40]}") from None
    return got if got.tzinfo else got.replace(tzinfo=dt.timezone.utc)


def party(value: str, pg: Any, biz: str) -> Party:
    """An address, ``@domain``, a biz handle (``api://...``) or a Customer / Supplier object
    id (its biz rows' handles by the lineage side table). Anything else fails closed."""
    v = value.strip()
    if "://" in v:
        return Party("record", [v])
    if re.match(r"^(Customer|Supplier):", v):
        if pg.execute("SELECT to_regclass(%s)", (f"{biz}.lineage",)).fetchone()[0]:
            rows = pg.execute(f"SELECT DISTINCT l.handle FROM {biz}.lineage l WHERE l.view IN "
                              "('customers', 'suppliers') AND l.object_ids @> "
                              "ARRAY[%s]::text[]", (v,)).fetchall()
        else:   # a schema before door v2: the business rows carry their object ids
            rows = pg.execute(f"SELECT handle FROM {biz}.customers WHERE object_ids @> "
                              f"ARRAY[%s]::text[] UNION SELECT handle FROM {biz}.suppliers "
                              "WHERE object_ids @> ARRAY[%s]::text[]", (v, v)).fetchall()
        if not rows:
            raise SearchInputError("that record has no customers or suppliers row")
        return Party("record", sorted(r[0] for r in rows))
    low = v.lower()
    if re.fullmatch(r"@?[a-z0-9-]+(?:\.[a-z0-9-]+)+", low):
        return Party("domain", low.lstrip("@"))
    if re.fullmatch(r"[^@\s]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", low):
        return Party("address", low)
    raise SearchInputError("participant: an address, @domain, or a Customer or Supplier "
                           "object id")


def present(pg: Any, guarded: str, *, opening: bool = False) -> list[str]:
    """The comms views this schema serves (``opening``: those ``open`` reads)."""
    rows = pg.execute("SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = "
                      "c.relnamespace WHERE n.nspname = %s AND c.relname = ANY(%s)",
                      (guarded, list(VIEWS))).fetchall()
    return [v for v in VIEWS if v in {r[0] for r in rows}
            and not (opening and v in SEARCH_ONLY)]


def search_sql(views: Sequence[str], *, guarded: str, biz: str, query: bool,
               party_kind: str | None, since: bool, until: bool, cursor: bool,
               cursor_tier: int = 0) -> str:
    """The one statement (named parameters: q, party, since, until, c_at, c_h, k). Its rows
    end with ``tier`` (1: the header holds every query word; 0: the text does, or no query
    words), in the order tier, date, handle, all descending; ``cursor_tier`` is the tier of
    the cursor's row (``read_cursor``)."""
    parts = []
    # a page after a header-matched row (tier 1) still has every text-matched row to come,
    # whatever its date: the date bounds the word match only within the last tier
    dated = cursor and cursor_tier == 0
    for view in views:
        kind, when, _ = VIEWS[view]
        mail = view == "emails"
        # a files row's text only once a build or refresh read it for Freightcom
        # (``ai_ok``; a row admitted on its cells alone matches by name and folder)
        checked = " AND b.ai_ok" if view in SEARCH_ONLY else ""
        conds = ["true"]
        if query:
            # the text of the row's own object and version: the fields ``ai_ok`` decided
            # (``business_comms._text_ai``), never a same-citation copy under another object.
            # The dates bound the word match too (the outer read keeps its own bounds): a
            # common word over all time matched 150,908 mails (13.2 s) where the 3 days
            # asked held 91 (1.3 s), and every party search ran into its 25 s limit
            window = "".join(f" AND b.{when} {op} %({name})s" for on, op, name in (
                (since, ">=", "since"), (until, "<", "until"), (dated, "<=", "c_at")) if on)
            header = (f"SELECT b.handle FROM {biz}.{view} b WHERE b.words @@ %(q)s::tsquery"
                      f"{window}")
            # ARRAY(...): the match is read once (an init plan) and the view's rows found
            # by handle; as "IN (...)" the planner, taking the reader's rows for one, ran
            # the match again per row of the window (a party search: 25 s timeout)
            conds.append(
                "e.handle = ANY(ARRAY(SELECT b.handle FROM ontology.text_fields fd JOIN "
                "ontology.text_chunks c ON c.field_id = fd.field_id JOIN ontology.chunk_words w "
                f"ON w.chunk_id = c.chunk_id JOIN {biz}.{view} b ON b.handle = "
                "fd.version_handle AND b.object_ids[1] = fd.object_id WHERE w.words @@ "
                f"%(q)s::tsquery AND fd.markings = '[]'{checked}{window} UNION ALL "
                f"{header}))")
            tier = f"(e.handle = ANY(ARRAY({header})))::int"
        else:
            tier = "0"
        if party_kind is not None:
            if not mail:
                continue
            conds.append({"address": "e.addresses @> ARRAY[%(party)s]::text[]",
                          "domain": "e.domains @> ARRAY[%(party)s]::text[]",
                          "record": "(e.customers && %(party)s::text[] OR e.suppliers && "
                                    "%(party)s::text[])"}[party_kind])
        if since:
            conds.append(f"e.{when} >= %(since)s")
        if until:
            conds.append(f"e.{when} < %(until)s")
        if cursor:
            after = f"(e.{when} < %(c_at)s OR (e.{when} = %(c_at)s AND e.handle < %(c_h)s))"
            conds.append(f"({tier} = 0 AND {after})" if cursor_tier == 0 else
                         f"({tier} = 0 OR {after})")
        addresses = ("e.sender_domain, e.addresses" if mail else
                     "NULL::text AS sender_domain, NULL::text[] AS addresses")
        parts.append(
            f"(SELECT '{view}'::text AS view, e.handle, {_TITLE.get(view, 'e.subject')} AS "
            f"title, {_SENDER.get(view, 'e.sender')} AS sender, "
            f"e.{when} AS at, {addresses}, {tier} AS tier FROM {guarded}.{view} e WHERE "
            + " AND ".join(conds) + f" AND e.{when} IS NOT NULL ORDER BY 8 DESC, e.{when} "
            "DESC, e.handle DESC LIMIT %(k)s)")
    if not parts:
        return ""
    best = ("ORDER BY (w.words @@ %(q)s::tsquery) DESC, fd.field_id, c.start" if query
            else "ORDER BY fd.field_id, c.start")
    return (
        "SELECT y.view, y.handle, y.title, y.sender, y.at, y.sender_domain, y.addresses, "
        "s.body, s.start, s.sender_class, y.tier FROM (SELECT * FROM ("
        + " UNION ALL ".join(parts)
        + ") x ORDER BY x.tier DESC, x.at DESC, x.handle DESC LIMIT %(k)s) y LEFT JOIN LATERAL ("
        "SELECT c.body, c.start, fd.sender_class FROM ontology.text_fields fd JOIN "
        "ontology.text_chunks c ON c.field_id = fd.field_id LEFT JOIN ontology.chunk_words w "
        "ON w.chunk_id = c.chunk_id WHERE fd.version_handle = y.handle AND fd.object_id IN ("
        + _objects(views, biz, "y") + ") AND fd.markings = '[]' "
        f"{best} LIMIT 1) s ON true ORDER BY y.tier DESC, y.at DESC, y.handle DESC")


def _objects(views: Sequence[str], biz: str, row: str) -> str:
    """The object ids of ``row``'s comms row (its view's table by handle): the text read
    for it is its own object's (the object ``ai_ok`` read, ``business_comms._text_ai``)."""
    return " UNION ALL ".join(
        f"SELECT b.object_ids[1] FROM {biz}.{view} b WHERE {row}.view = '{view}' AND "
        f"b.handle = {row}.handle" + (" AND b.ai_ok" if view in SEARCH_ONLY else "")
        for view in views)


def open_sql(views: Sequence[str], *, guarded: str, biz: str) -> str:
    """One row by its handle through the guarded views and its document text (parameter
    h): no visible row, no text."""
    parts = []
    for view in views:
        kind, when, _ = VIEWS[view]
        cols = ("e.to_addresses, e.cc_addresses" if view == "emails"
                else "NULL::text[], NULL::text[]")
        parts.append(f"SELECT '{view}'::text AS view, e.handle, e.subject, e.sender, "
                     f"e.{when} AS at, {cols} FROM {guarded}.{view} e WHERE e.handle = %(h)s")
    return ("SELECT g.*, t.field_id, t.start, t.body, t.sender_class, t.field_path, "
            "t.text_digest FROM (" + " UNION ALL "
            .join(parts) + " LIMIT 1) g LEFT JOIN LATERAL (SELECT fd.field_id, c.start, "
            "c.body, fd.sender_class, fd.field_path, fd.text_digest FROM ontology.text_fields fd JOIN ontology.text_chunks c "
            "ON c.field_id = fd.field_id WHERE fd.version_handle = g.handle AND fd.object_id IN ("
            + _objects(views, biz, "g") + ") AND fd.markings = '[]') "
            "t ON true ORDER BY t.field_id, t.start")


def exists_sql(views: Sequence[str], *, biz: str) -> str:
    """Whether a handle is a comms row at all (then ``open`` answers it, never the old
    read): parameter h."""
    return "SELECT EXISTS (" + " UNION ALL ".join(
        f"SELECT 1 FROM {biz}.{v} WHERE handle = %(h)s" for v in views) + ")"


def _iso(at: Any) -> str | None:
    if not isinstance(at, dt.datetime):
        return None
    at = at if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)
    return at.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text(value: Any, citation: str, cut: int = 20000) -> dict[str, Any]:
    text = str(value)
    out: dict[str, Any] = {"untrusted_text": text[:cut], "origin": {"citation": citation}}
    if len(text) > cut:
        out["truncated"] = True
    return out


def snippet(body: Any, words: Sequence[str]) -> str | None:
    if body is None:
        return None
    try:
        text = zlib.decompress(bytes(body)).decode("utf-8", "replace")
    except zlib.error:
        return None
    low = text.lower()
    at = min((i for i in (low.find(w.lower()) for w in words) if i >= 0), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    return " ".join(text[start:start + SNIPPET_CHARS].split())


def hits(rows: Sequence[Sequence[Any]], *, words: Sequence[str], party_: Party | None
         ) -> list[dict[str, Any]]:
    """The statement's rows as ``plx.search`` hits."""
    out = []
    for view, handle, title, sender, at, sender_domain, addresses, body, _start, sclass, \
            *_tier in rows:
        hit: dict[str, Any] = {"citation": handle, "kind": VIEWS[view][0],
                               "title": _text(title, handle, 500) if title else None,
                               "date": _iso(at)}
        cls = sclass if sclass in ("internal", "external") else "unknown"
        hit["sender"] = ({"address": _text(sender, handle, 300), "sender_class": cls}
                         if sender else {"sender_class": cls})
        cut = snippet(body, words)
        hit["snippet"] = _text(cut, handle, 500) if cut else None
        if party_ is not None and party_.kind in ("address", "domain"):
            mine = (sender == party_.value if party_.kind == "address"
                    else sender_domain == party_.value)
            hit["direction"] = "from_them" if mine else "to_them"
            hit["matched_by"] = party_.kind
        out.append(hit)
    return out


# rows a cursor may skip past its returned row (the file rows the item gate refused):
# past this many a search says it stopped (incomplete), never pages without end
MAX_SKIP = 2000


def cursor_of(hit_row: Optional[Sequence[Any]], skip: int = 0) -> str:
    """The cursor after ``hit_row`` (a row this reader was RETURNED; None: the search's
    start) and ``skip`` more rows past it (refused file rows: read again and refused
    again, so none is lost and the cursor never holds a refused row's handle or date):
    ``c1:<tier>|<date>|<handle>[|+<skip>]`` or ``c1:+<skip>`` (``search_sql``)."""
    if hit_row is None:
        return cursor_at(None, None, None, skip)
    return cursor_at(hit_row[10] if len(hit_row) > 10 else 0, hit_row[4], hit_row[1], skip)


def cursor_at(tier: Optional[int], at: Optional[dt.datetime], handle: Optional[str],
              skip: int = 0) -> str:
    """``cursor_of`` from its parts (``read_cursor``'s), ``handle`` None: the start."""
    if handle is None or at is None:
        return f"c1:+{int(skip)}"
    more = f"|+{int(skip)}" if skip else ""
    return f"c1:{1 if tier else 0}|" + at.astimezone(dt.timezone.utc).isoformat() + "|" \
        + handle + more


def read_cursor(text: str) -> tuple[Optional[int], Optional[dt.datetime], Optional[str],
                                     int]:
    """(tier, date, handle, rows to skip) of a ``cursor_of`` cursor (tier, date and handle
    None: from the start); anything else (another search's, or one made before rows were
    ranked by tier) is refused, never read as some other page."""
    bad = SearchInputError("not a cursor of this search")
    if not isinstance(text, str):
        raise bad
    start = re.fullmatch(r"c1:\+([1-9][0-9]{0,5})", text)
    if start:
        skip = int(start.group(1))
        if skip > MAX_SKIP:
            raise bad
        return None, None, None, skip
    skip = 0
    tail = re.search(r"\|\+([1-9][0-9]{0,5})$", text)
    if tail:
        skip, text = int(tail.group(1)), text[:tail.start()]
        if skip > MAX_SKIP:
            raise bad
    parts = text.split("|", 2)
    if len(parts) != 3 or parts[0] not in ("c1:0", "c1:1") or not parts[2]:
        raise bad
    try:
        return int(parts[0][3]), dt.datetime.fromisoformat(parts[1]), parts[2], skip
    except ValueError:
        raise bad from None


def assemble(rows: Sequence[Sequence[Any]]) -> str:
    """One item's document text from its stored pieces (as ``business_comms._text_ai``)."""
    fields: dict[int, str] = {}
    for field, start, body in rows:
        if field is None or body is None:
            continue
        fields[field] = fields.get(field, "")[:start] + zlib.decompress(
            bytes(body)).decode("utf-8", "replace")
    return "\n\n".join(fields[f] for f in sorted(fields))


def opened(rows: Sequence[Sequence[Any]], *, offset: int, most: int) -> dict[str, Any]:
    """One opened item: its text (every field, joined as ``assemble`` joins them) from
    ``offset``, at most ``most`` characters, and what of each field that window served
    (``fields_served``: the field, its text digest, the field's own span served, the
    field's length and where that part starts in ``text``), so the session keeps it as
    it keeps a ``plx.read`` (``mcp_server_evidence._served_texts``)."""
    view, handle, subject, sender, at, to, cc = rows[0][:7]
    text = assemble([(r[7], r[8], r[9]) for r in rows])
    window = text[offset:offset + most]
    out: dict[str, Any] = {"citation": handle, "kind": VIEWS[view][0],
                           "title": _text(subject, handle, 500) if subject else None,
                           "date": _iso(at),
                           "sender": _text(sender, handle, 300) if sender else None,
                           "text": {"untrusted_text": window, "origin": {"citation": handle}},
                           "text_offset": offset, "text_chars": len(text)}
    if to:
        out["to"] = list(to)
    if cc:
        out["cc"] = list(cc)
    if offset + most < len(text):
        out["text"]["truncated"] = True
        out["next_offset"] = offset + most
    if not text:
        out["text_note"] = "no readable text is indexed for this item"
    served, at_ = [], 0
    named = {r[7]: (r[11], r[12]) for r in rows if r[7] is not None and len(r) > 12}
    for field in sorted(named):
        length = len(assemble([(r[7], r[8], r[9]) for r in rows if r[7] == field]))
        lo, hi = max(offset, at_), min(offset + most, at_ + length)
        if hi > lo and isinstance(named[field][0], str):
            served.append({"field": named[field][0], "version": named[field][1],
                           "offset": lo - at_, "chars": hi - lo, "total_chars": length,
                           "at": lo - offset})
        at_ += length + 2       # the "\n\n" ``assemble`` joins fields with
    if served:
        out["fields_served"] = served
    return out


def searched(views: Sequence[str], args: Mapping[str, Any], words: Sequence[str]
             ) -> dict[str, Any]:
    out: dict[str, Any] = {"sources": [VIEWS[v][2] for v in views] or ["graph_mail"]}
    for key in ("since", "until"):
        if args.get(key):
            out[key] = args[key]
    if args.get("participant"):
        out["participants"] = [args["participant"]] if re.match(
            r"^(?:[a-z0-9._%+'-]+@)?[a-z0-9-]+(?:\.[a-z0-9-]+)+$",
            str(args["participant"]).lstrip("@").lower()) else []
        out["participants"] = [p.lstrip("@").lower() for p in out["participants"]]
        if not out["participants"]:
            del out["participants"]
    if words:
        out["queries"] = [" ".join(words)[:200]]
    return out


__all__ = ["Party", "SEARCH_ONLY", "SearchInputError", "VIEWS", "assemble", "cursor_of", "exists_sql",
           "hits", "instant", "open_sql", "opened", "party", "present", "read_cursor",
           "search_sql", "searched", "snippet", "terms", "tsquery"]
