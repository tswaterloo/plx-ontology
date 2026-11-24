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
or the body (``ontology.chunk_words`` of the version's document text, GIN); the statement
walks each view's newest rows first, checking each row's own words (a common word fills
its page there), and reads the word index's whole match only for the rows past the walk
when the walk found too few (a rare word): ``search_sql``.
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
# a Teams message's sender column holds the poster's raw JSON ({display_name, id, kind}): a
# hit shows the poster's name (``author``, read from the view when the result is made: the
# chunk header in the store names only the kind, "Teams user", ``chunking/projection._role``)
_SENDER = {"sharepoint_files": "NULL::text", "teams_messages": "e.author"}
# a hit's snippet around its first matched word (was 1,200 cut again to 500 unsaid, and 240
# before that): said cut (``truncated``) whenever the text goes on past it, so the model
# knows to plx.open it; sized so a page of hits fits what the client shows of one result
# (``mcp_server_evidence.CLIENT_SHOWN_CHARS``)
SNIPPET_CHARS = 800
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


def stem_tsquery(stems: Any) -> str | None:
    """Every word of the query by its stem: ``plainto_tsquery('english', query)``'s text
    (small words dropped) as a prefix of each stem of 3 letters or more ("renov*" finds
    "renovation" and "renovating"; a Porter final i, "happi", made "happ*"), the shorter
    exactly; None when it holds no stem."""
    import sqlite3
    from operating.pg import words
    got = re.findall(r"'((?:[^']|'')+)'", stems if isinstance(stems, str) else "")
    terms_ = []
    for stem in got[:MAX_TERMS]:
        stem = stem.replace("''", "'")
        stem = stem[:-1] if stem.endswith("i") and len(stem) > 3 else stem
        quoted = '"' + stem.replace('"', '""') + '"'
        terms_.append(quoted + "*" if len(stem) >= 3 else quoted)
    if not terms_:
        return None
    try:
        return words.tsquery(" ".join(terms_))
    except sqlite3.OperationalError:
        return None


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


# rows of one table (its newest, in the dates) whose reader's rows are walked newest first
# for text matches before the word index's whole match is read (``search_sql``): a common
# word ("purchase order": 263,186 matched mails, 20.7 s to read them all) fills the page
# from the newest few hundred rows (0.1-1.4 s); a rare one finds its page in the word index
# (``_fallback``)
WALK_ROWS = 4000
# the walk is for a common word only: the planner's estimate of the word index's whole
# match (its column statistics, no row read, 0.5 ms) at or past this many text chunks.
# A rarer one reads that match at once (``PO100109``: 9,072 estimated, 0.2 s; walked, its
# 4,000 rows' own checks took 6.3 s); "purchase order" (121,662) or "invoice" walk.
COMMON_CHUNKS = 50000
# a second walk stage, DEEPER times as deep, for a word the first stage found too few of
# but often enough to fill the page there ("purchase order" in Teams: 5 in its newest
# 4,000 messages; the word index's whole match, every document's chunks, took 18 s)
STAGES, DEEPER = 2, 16
# a party's search with query words (``search_sql``) first reads that party's own rows in
# the dates from its table (by the party's index, newest first, at most this many); when
# they are fewer, the page is those of them the reader's view shows that hold the words,
# each checked by its own header and text. A party has few rows: its 1,761 mails since
# January were read in 50 ms, where walking the newest rows of every mail for it and then
# the words' whole match ran 40-48 s and past the 25 s limit (5 of 555 calls, 11-21). A
# party with more rows reads the walk as before.
PARTY_ROWS = 2000
# the tables whose text is short (about one text chunk a row: Teams, 215,475 chunks for
# 253,554 messages) read a common word's rows past the walk from their own rows, each row
# checked by its own text (live, all of Teams: 1.6 s), never from the word index's whole
# match, which is mostly mail's and the files' chunks ("purchase order" for Teams: 575,000
# chunks read, 27 s). A rare word (``walk_rows`` 1) still reads the word index (0.2 s).
# Either gives the same rows.
OWN_TEXT_VIEWS = frozenset({"teams_messages"})


def walk_rows(pg: Any, q: str | None) -> int:
    """The ``walk`` parameter of ``search_sql`` for the tsquery ``q``: ``WALK_ROWS`` for
    a common word (``COMMON_CHUNKS``), else 1 (the newest row; the word index then reads
    the rest). Either gives the same rows in the same order: only the time differs."""
    if q is None or COMMON_CHUNKS <= 0:
        return WALK_ROWS
    plan = pg.execute("EXPLAIN (FORMAT JSON) SELECT 1 FROM ontology.chunk_words w WHERE "
                      "w.words @@ %s::tsquery", (q,)).fetchone()[0]
    if isinstance(plan, str):
        import json
        plan = json.loads(plan)
    return WALK_ROWS if plan[0]["Plan"]["Plan Rows"] >= COMMON_CHUNKS else 1


# a view's header tier, or the text tier of a view whose text sits in the sidecar, is read
# from its own word index at once (no walk) when the index's whole match is under this many
# MESSAGES (the planner's estimate, no row read): the match is then read in tens of ms
# (emails header 34 ms, Teams sidecar 9 ms for 3,500 messages), where each walk read the
# newest ~3,900 rows through the guarded view (0.93 s a walk, 6 walks a statement). The old
# test counted text CHUNKS (``COMMON_CHUNKS``: "invoice payment" 68,577 estimated, 501,705
# real, for 4,500 + 3,500 messages), a count of another unit than the rows read. Either
# gives the same rows in the same order (``search_sql``'s ``direct``): only the time differs.
COMMON_MESSAGES = 50000


def _plan_rows(pg: Any, sql: str, params: Sequence[Any]) -> int:
    plan = pg.execute("EXPLAIN (FORMAT JSON) " + sql, tuple(params)).fetchone()[0]
    if isinstance(plan, str):
        import json
        plan = json.loads(plan)
    return int(plan[0]["Plan"]["Plan Rows"])


def direct_reads(pg: Any, q: str | None, views: Sequence[str], side: Mapping[str, Any],
                 *, biz: str) -> frozenset[tuple[str, int]]:
    """The (view, tier) pairs ``search_sql`` reads from the word index at once, with no walk
    (``COMMON_MESSAGES``): tier 1 (header) of every mail and Teams view whose header index
    matches fewer messages than that; tier 0 (text) of a sidecar view whose sidecar matches
    fewer, and whose rows dated before the sidecar's ``cov`` (read the old way, by chunks)
    are not more than that. Empty without query words or with the old test turned off."""
    if q is None or COMMON_MESSAGES <= 0:
        return frozenset()
    out: set[tuple[str, int]] = set()
    for view in views:
        if view in SEARCH_ONLY:
            continue
        if _plan_rows(pg, f"SELECT 1 FROM {biz}.{view} b WHERE b.words @@ %s::tsquery",
                      (q,)) < COMMON_MESSAGES:
            out.add((view, 1))
        if view in side:
            when = VIEWS[view][1]
            text = _plan_rows(pg, "SELECT 1 FROM biz.comm_text t WHERE t.view = %s AND "
                              "t.tsv @@ %s::tsquery", (view, q))
            older = _plan_rows(pg, f"SELECT 1 FROM {biz}.{view} b WHERE b.{when} < "
                               "%s::timestamptz", (side[view][f"cov_{view}"],))
            if text + older < COMMON_MESSAGES:
                out.add((view, 0))
    return frozenset(out)


# the views the comms text sidecar (``biz.comm_text``, migration 0074, ``comm_text``) serves
SIDE_VIEWS = frozenset({"emails", "teams_messages"})


def sidecar(pg: Any) -> dict[str, dict[str, Any]]:
    """The views the sidecar covers now, with the parameters ``search_sql`` reads for them
    (``cov_<view>``: rows dated before it are read the old way; ``mark_<view>``: text fields
    past it are read the old way). Empty: none built (migration 0074 or a first build not
    yet there), and the search reads as before."""
    from operating.pg import comm_text
    got = comm_text.state(pg)
    return {v: {f"cov_{v}": st["covered_from"], f"mark_{v}": st["built_field_id"]}
            for v, st in got.items() if v in SIDE_VIEWS}


def _side_pieces(view: str, biz: str, when: str, ops: Sequence[tuple[str, str]],
                 checked: str, words_from: str = "wm w") -> tuple[str, str]:
    """(the handles whose text holds every word of ``q``, as one derived table ``b`` with
    ``handle`` and the date column; one row's own check) from the sidecar. Three parts, the
    same rows as the chunk word index's match: the sidecar's rows at or after the view's
    ``cov`` whose field is still unmarked (read now, so a marking set or lifted after the
    build is followed); the text fields past the sidecar's ``mark`` the sidecar has no row
    for yet, read the old way; and the rows dated before ``cov``, the old way."""
    cov, mark = f"%(cov_{view})s", f"%(mark_{view})s"
    on_t = "".join(f" AND t.at {op} %({name})s" for op, name in ops)
    on_b = "".join(f" AND b.{when} {op} %({name})s" for op, name in ops)
    old = ("FROM ontology.text_fields fd JOIN ontology.text_chunks c ON c.field_id = "
           "fd.field_id JOIN {src} ON w.chunk_id = c.chunk_id JOIN "
           f"{biz}.{view} b ON b.handle = fd.version_handle AND b.object_ids[1] = fd.object_id "
           f"WHERE fd.markings = '[]'{checked}")
    # an old-way read below ``cov`` is skipped (a gate on the window's own parameters) when
    # the window starts at or after it
    below = ("true" if not any(name == "since" for _, name in ops)
             else f"%(since)s::timestamptz < {cov}::timestamptz")
    match = (
        f"SELECT b.handle FROM ("
        f"SELECT DISTINCT t.handle, t.at AS {when} FROM biz.comm_text t JOIN "
        f"ontology.text_fields fd ON fd.field_id = t.field_id AND fd.markings = '[]' "
        f"WHERE t.view = '{view}' AND t.whole AND "
        f"t.tsv @@ %(q)s::tsquery AND t.at >= {cov}{on_t} "
        # a field held in pieces (migration 0075): read the old way, so words that sit in two
        # pieces still match (a piece's own vector would miss them)
        f"UNION ALL SELECT b.handle, b.{when} FROM biz.comm_text t JOIN "
        f"ontology.text_fields fd ON fd.field_id = t.field_id AND fd.markings = '[]' AND "
        f"NOT t.whole JOIN {biz}.{view} b ON b.handle = t.handle WHERE t.view = '{view}' "
        f"AND t.part = 0 AND b.{when} >= {cov}{on_b} AND EXISTS (SELECT 1 FROM "
        "ontology.text_chunks c JOIN ontology.chunk_words w ON w.chunk_id = c.chunk_id "
        "WHERE c.field_id = fd.field_id AND w.words @@ %(q)s::tsquery) "
        f"UNION ALL SELECT b.handle, b.{when} FROM ontology.text_fields fd JOIN {biz}.{view} b "
        f"ON b.handle = fd.version_handle AND b.object_ids[1] = fd.object_id WHERE "
        f"fd.field_id > {mark} AND fd.markings = '[]' AND b.{when} >= {cov}{on_b} AND NOT "
        "EXISTS (SELECT 1 FROM biz.comm_text t2 WHERE t2.handle = b.handle AND t2.field_id = "
        "fd.field_id) AND EXISTS "
        "(SELECT 1 FROM ontology.text_chunks c JOIN ontology.chunk_words w ON w.chunk_id = "
        "c.chunk_id WHERE c.field_id = fd.field_id AND w.words @@ %(q)s::tsquery) "
        f"UNION ALL SELECT b.handle, b.{when} "
        + old.format(src=words_from) + f" AND {below} AND b.{when} < {cov}{on_b}"
        f") b WHERE b.{when} IS NOT NULL")
    # one row (``e.handle``): its sidecar row when it has one, else the old per-row read
    old_one = ("(SELECT 1 FROM ontology.text_fields fd JOIN ontology.text_chunks c ON "
               "c.field_id = fd.field_id JOIN ontology.chunk_words w ON w.chunk_id = "
               "c.chunk_id WHERE fd.version_handle = e.handle AND fd.object_id = "
               f"(SELECT b.object_ids[1] FROM {biz}.{view} b WHERE b.handle = e.handle) AND "
               f"fd.markings = '[]'{checked} AND w.words @@ %(q)s::tsquery LIMIT 1)")
    # a message with no sidecar text, or a field held in pieces, is read the old way; else the
    # sidecar's rows of its fields (each field's own check)
    own = ("(SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM biz.comm_text t0 WHERE t0.handle = "
           "e.handle AND t0.field_id IS NOT NULL) OR EXISTS (SELECT 1 FROM biz.comm_text t1 "
           "WHERE t1.handle = e.handle AND NOT t1.whole) THEN " + old_one + " ELSE (SELECT 1 "
           "FROM biz.comm_text t JOIN ontology.text_fields fd2 ON fd2.field_id = t.field_id "
           "AND fd2.markings = '[]' WHERE t.handle = e.handle AND t.tsv @@ %(q)s::tsquery "
           "LIMIT 1) END)")
    return match, own


def count_sql(views: Sequence[str], *, guarded: str, biz: str, since: bool,
              until: bool, side: frozenset[str] = frozenset()) -> str:
    """One statement (named parameters q, since, until) counting every row of ``views``
    the reader may read (through ``guarded``) that holds every query word in its header
    or its text, in the dates: how many a word search matched in all, read from the word
    indexes (handles only, never a row). Files (``SEARCH_ONLY``) are re-gated row by row
    after the statement, so they are not counted here (the caller says so)."""
    parts = []
    for view in views:
        if view in SEARCH_ONLY:
            continue
        _kind, when, _ = VIEWS[view]
        window = "".join(f" AND b.{when} {op} %({name})s" for on, op, name in (
            (since, ">=", "since"), (until, "<", "until")) if on)
        header = (f"SELECT b.handle FROM {biz}.{view} b WHERE b.words @@ %(q)s::tsquery"
                  f"{window}")
        text = ("SELECT b.handle FROM ontology.text_fields fd JOIN ontology.text_chunks c ON "
                "c.field_id = fd.field_id JOIN ontology.chunk_words w ON w.chunk_id = "
                f"c.chunk_id JOIN {biz}.{view} b ON b.handle = fd.version_handle AND "
                "b.object_ids[1] = fd.object_id WHERE w.words @@ %(q)s::tsquery AND "
                f"fd.markings = '[]'{window}")
        if view in side:   # the sidecar's match (the same rows: ``_side_pieces``)
            text = _side_pieces(view, biz, when, [(op, name) for on, op, name in (
                (since, ">=", "since"), (until, "<", "until")) if on], "",
                words_from="ontology.chunk_words w")[0]
        dates = "".join(f" AND e.{when} {op} %({name})s" for on, op, name in (
            (since, ">=", "since"), (until, "<", "until")) if on)
        # the match's handles read once (an init plan), then the reader's view by its handle
        # index: ``handle IN (<union>)`` let the planner scan the reader's whole view with the
        # access check on every row (live 11-24: 233 s and more for one word over 3 months of
        # mail, 15 minutes of a search that held a page of 25)
        parts.append(f"(SELECT count(*) FROM {guarded}.{view} e WHERE e.{when} IS NOT NULL"
                     f"{dates} AND e.handle = ANY(ARRAY({header} UNION {text})))")
    return ("SELECT " + " + ".join(parts)) if parts else ""


def search_sql(views: Sequence[str], *, guarded: str, biz: str, query: bool,
               party_kind: str | None, since: bool, until: bool, cursor: bool,
               cursor_tier: int = 0, side: frozenset[str] = frozenset(),
               direct: frozenset[tuple[str, int]] = frozenset()) -> str:
    """The one statement (named parameters: q, party, since, until, c_at, c_h, k, k_own,
    walk; ``k_own`` <= ``k``: the rows read of mail and Teams, ``k`` of the files and in all).
    Its rows end with ``tier`` (1: the header holds every query word; 0: the text does, or
    no query words), in the order tier, date, handle, all descending; ``cursor_tier`` is the
    tier of the cursor's row (``read_cursor``).

    With query words each view gives up to three parts, each its newest ``k`` rows: the
    header rows (the table's own GIN); the text rows among the view's rows at or after the
    walk's floor (the ``walk``-th newest row of the table in the dates; each row checked by
    its own text, newest first, stopping at ``k``); and, only when those rows held fewer
    than ``k`` text rows and the table has rows below the floor, the text rows below it from
    the word index's whole match (``_fallback``: never read otherwise). The floor splits
    the reader's rows in two by (date, handle), so together they are the same rows, in the
    same order, as one read of every match: whatever the word's frequency, the page is
    found where it is cheap.

    ``side``: the views the text sidecar serves (``sidecar``): their word match is read from
    ``biz.comm_text`` (dated, by view) instead of the chunk word index's whole match; the
    parameters ``cov_<view>`` and ``mark_<view>`` come with it.

    ``direct``: the (view, tier) pairs read from the word index's match at once, with no walk
    stage (``direct_reads``, by the message-level estimate); the rest walk as ``walk`` says.
    The same rows either way."""
    parts, ctes = [], []
    for at, view in enumerate(views):
        kind, when, _ = VIEWS[view]
        mail = view == "emails"
        # its rows: ``k`` for the files (room for those the item gate refuses), ``k_own``
        # for mail and Teams (never refused: the page and its "more" are decided within
        # their first ``k_own``; reading ``k`` of them walked twice as deep for nothing)
        kk = "%(k)s" if view in SEARCH_ONLY else "%(k_own)s"
        # a files row's text only once a build or refresh read it for Freightcom
        # (``ai_ok``; a row admitted on its cells alone matches by name and folder)
        checked = " AND b.ai_ok" if view in SEARCH_ONLY else ""
        conds = []
        if party_kind is not None:
            if not mail:
                continue
            conds.append(_PARTY[party_kind])
        # the dates (the walk's floor is read in them, never under a party's: a rare
        # party over the date index read every row)
        dates = ([f"e.{when} >= %(since)s"] if since else []) + (
            [f"e.{when} < %(until)s"] if until else []) + [f"e.{when} IS NOT NULL"]
        conds += dates
        after = f"(e.{when} < %(c_at)s OR (e.{when} = %(c_at)s AND e.handle < %(c_h)s))"
        cols = _cols(view)
        newest = f"ORDER BY e.{when} DESC, e.handle DESC"

        def part(tier: int, where: Sequence[str], source: str = f"{guarded}.{view} e",
                 cols: str = cols, newest: str = newest, kk: str = kk) -> str:
            return (f"(SELECT {cols}, {tier} AS tier FROM {source} WHERE "
                    + " AND ".join(where) + f" {newest} LIMIT {kk})")
        if not query:
            # no query words: every row is tier 0 (a cursor's tier is 0 then too)
            parts.append(part(0, conds + ([after if cursor_tier == 0 else "true"]
                                          if cursor else [])))
            continue
        # the dates bound the word match too (the outer read keeps its own bounds): a
        # common word over all time matched 150,908 mails (13.2 s) where the 3 days
        # asked held 91 (1.3 s), and every party search ran into its 25 s limit
        dated = cursor and cursor_tier == 0
        window = "".join(f" AND b.{when} {op} %({name})s" for on, op, name in (
            (since, ">=", "since"), (until, "<", "until"), (dated, "<=", "c_at")) if on)
        header = (f"SELECT b.handle FROM {biz}.{view} b WHERE b.words @@ %(q)s::tsquery"
                  f"{window}")
        # the text of the row's own object and version: the fields ``ai_ok`` decided
        # (``business_comms._text_ai``), never a same-citation copy under another object
        text = ("FROM ontology.text_fields fd JOIN ontology.text_chunks c ON c.field_id = "
                "fd.field_id JOIN ontology.chunk_words w ON w.chunk_id = c.chunk_id JOIN "
                f"{biz}.{view} b ON b.handle = fd.version_handle AND b.object_ids[1] = "
                f"fd.object_id WHERE w.words @@ %(q)s::tsquery AND fd.markings = '[]'{checked}")
        # one row's own checks, as subplans run per row (a LIMIT 1 subquery is never made
        # a join over the whole match): its header, its text
        own_header = (f"(SELECT 1 FROM {biz}.{view} b WHERE b.handle = e.handle AND "
                      "b.words @@ %(q)s::tsquery LIMIT 1)")
        own_text = f"(SELECT 1 {text} AND b.handle = e.handle LIMIT 1)"
        use_side = view in side
        if use_side:
            side_match, own_text = _side_pieces(view, biz, when, [
                (op, name) for on, op, name in ((since, ">=", "since"), (until, "<", "until"),
                                                (dated, "<=", "c_at")) if on], checked)
        if party_kind is not None and not cursor:
            # the party's own rows first (``PARTY_ROWS``): from its table by the party's
            # index in the dates (handles only, never a row shown), then each through the
            # reader's view by handle with the same conditions, by its own header (tier 1)
            # or text (tier 0). Fewer than PARTY_ROWS: these parts answer and the walk below
            # is never read; else the walk answers and these are never read (one-time
            # filters). Same rows, same order: only the time differs.
            mine_of = f"party_{at}"
            on_b = [_PARTY[party_kind].replace("e.", "b.")] + (
                [f"b.{when} >= %(since)s"] if since else []) + (
                [f"b.{when} < %(until)s"] if until else []) + [f"b.{when} IS NOT NULL"]
            ctes.append(f"{mine_of} AS MATERIALIZED (SELECT b.handle FROM {biz}.{view} b "
                        "WHERE " + " AND ".join(on_b) + f" ORDER BY b.{when} DESC, b.handle "
                        f"DESC LIMIT {PARTY_ROWS})")
            held = f"(SELECT count(*) FROM {mine_of})"
            by_party = [f"{held} < {PARTY_ROWS}", *conds,
                        f"e.handle = ANY(ARRAY(SELECT handle FROM {mine_of}))"]
            parts.append(part(1, by_party + [f"{own_header} IS NOT NULL"]))
            parts.append(part(0, by_party + [f"{own_header} IS NULL",
                                             f"{own_text} IS NOT NULL"]))
            conds = [f"{held} >= {PARTY_ROWS}", *conds]
        # tier 1 (header rows) and tier 0 (text rows whose header lacks a word): a cursor
        # of tier 0 is past every header row; one of tier 1 has every text row still to come
        tiers = [] if cursor and cursor_tier == 0 else [
            (1, conds + ([after] if cursor else []), f"{own_header} IS NOT NULL",
             f"{header}", [])]
        # the text rows' match past the walk: the word index's (``match``), or, for a
        # common word in a table of short texts (``OWN_TEXT_VIEWS``), the table's own rows
        # each checked by its text (``own``)
        own = (f"SELECT b.handle FROM {biz}.{view} b WHERE (SELECT 1 FROM "
               "ontology.text_fields fd JOIN ontology.text_chunks c ON c.field_id = "
               "fd.field_id JOIN ontology.chunk_words w ON w.chunk_id = c.chunk_id WHERE "
               "fd.version_handle = b.handle AND fd.object_id = b.object_ids[1] AND "
               f"w.words @@ %(q)s::tsquery AND fd.markings = '[]'{checked} LIMIT 1) IS NOT "
               f"NULL{window}") if view in OWN_TEXT_VIEWS and not use_side else None
        # the word index's whole match read once for every view (``wm``): the same chunks
        # for each, and each view's scan of the index took 0.5 s (a stem query, 3 views)
        shared = ("FROM ontology.text_fields fd JOIN ontology.text_chunks c ON c.field_id = "
                  "fd.field_id JOIN wm ON wm.chunk_id = c.chunk_id JOIN "
                  f"{biz}.{view} b ON b.handle = fd.version_handle AND b.object_ids[1] = "
                  f"fd.object_id WHERE fd.markings = '[]'{checked}")
        tiers.append((0, conds + ([after] if dated else []),
                      f"{own_header} IS NULL AND {own_text} IS NOT NULL",
                      side_match if use_side else f"SELECT b.handle {shared}{window}",
                      [f"{own_header} IS NULL"]))
        # the walk's floors: the ``walk``-th newest row of the table itself in the dates
        # (the index on the date column read in order, ``walk`` rows), then (``STAGES``)
        # the ``walk * DEEPER``-th: each stage walks the reader's rows between its floor and
        # the one before. Not the reader's own ``walk`` newest rows: through the guarded
        # view (a unit list the planner cannot size) that read and sorted every row the
        # reader holds, 2.5 million mails for a full reader, 10 s per walk
        for tier, scope, check, match, keep in tiers:
            mine = own if tier == 0 else None
            fscope = dates + ([after, f"e.{when} <= %(c_at)s"] if after in scope else [])
            founds, top = [], None
            if (view, tier) in direct:
                # no walk: the word index's whole match (a few thousand messages: ``direct_reads``)
                # by handle through the reader's view, newest ``kk`` of them. A walk stage reads
                # the guarded view even for one row (0.9 s a stage on Teams, 4,267 units probed)
                parts.append(part(tier, [*scope, f"e.handle = ANY(ARRAY({match}))", *keep]))
                continue
            for stage in range(STAGES):
                found, floor = f"found_{at}_{tier}_{stage}", f"floor_{at}_{tier}_{stage}"
                ctes.append(_floor(floor, top, founds, fscope, biz=biz, view=view,
                                   when=when, newest=newest, depth=DEEPER ** stage, kk=kk))
                f_at, f_h = (f"(SELECT f.at FROM {floor} f)",
                             f"(SELECT f.handle FROM {floor} f)")
                rows = [*scope, f"e.{when} >= {f_at}", f"(e.{when}, e.handle) >= ({f_at}, {f_h})"]
                if top is not None:
                    t_at, t_h = (f"(SELECT f.at FROM {top} f)",
                                 f"(SELECT f.handle FROM {top} f)")
                    rows += [f"e.{when} <= {t_at}", f"(e.{when}, e.handle) < ({t_at}, {t_h})"]
                # each walked row checked in date order until ``k`` are found (a subquery's
                # order carries through: no sort, no row checked past)
                walk = (f"(SELECT e.handle, e.{when} AS at FROM {guarded}.{view} e WHERE "
                        + " AND ".join(rows) + f" {newest})")
                ctes.append(f"{found} AS MATERIALIZED (SELECT e.handle FROM {walk} e WHERE "
                            f"{check} ORDER BY e.at DESC, e.handle DESC LIMIT {kk})")
                # the found rows by handle (an init plan: the view read by its handle
                # index, never a join against every row the reader holds)
                parts.append(part(tier, scope + [f"e.handle = ANY(ARRAY(SELECT handle FROM "
                                                 f"{found}))"]))
                founds.append(found)
                top = floor
            if mine is None:
                parts.append(_fallback(part, tier, scope, top, founds, when=when,
                                       match=match, keep=keep, kk=kk))
                continue
            # one or the other, by the word's walk (a one-time filter: the other's match
            # is never read)
            parts.append(_fallback(part, tier, ["%(walk)s <= 1", *scope], top, founds,
                                   when=when, match=match, keep=keep, kk=kk))
            parts.append(_fallback(part, tier, ["%(walk)s > 1", *scope], top, founds,
                                   when=when, match=mine, keep=keep, kk=kk))
    if not parts:
        return ""
    if query:   # read only when a view's fallback runs (a materialized CTE is lazy)
        ctes.insert(0, "wm AS MATERIALIZED (SELECT w.chunk_id FROM ontology.chunk_words w "
                       "WHERE w.words @@ %(q)s::tsquery)")
    return (("WITH " + ", ".join(ctes) + " " if ctes else "")
            + _quoted(" UNION ALL ".join(parts), views, biz, query=query))


# a full reader's first page with query words also lists the mail whose attachment's own
# text holds every word (``attachment_sql``): a common word ("invoice payment") walks the
# attachments of the reader's mails newest first, ``ATTACH_BATCH`` mails at a time, until
# ``k`` are found or every mail in the dates was read (no cap: the batch is only how much
# one statement reads; 4,000 mails took 0.2 s live, 11-19 under load ~24 s), a rarer one
# (``walk_rows`` 1) reads the word index's whole attachment match at once ("QuikPAK orders
# appearing": 0.06 s), then those mails through the reader's view by handle: joined whole,
# the planner scanned the reader's 300k mails (live 11-20: past 25 s; by handle 0.07 s).
ATTACH_BATCH = 4000


def attachment_mails_sql() -> str:
    """The mails (handles) whose unmarked attachment text holds every word of ``q``, all
    of them: ``attachment_sql``'s ``mails`` for a rare word."""
    return ("SELECT split_part(fd.citation, '#', 1) AS mail FROM ontology.chunk_words w "
            "JOIN ontology.text_chunks c ON c.chunk_id = w.chunk_id JOIN "
            "ontology.text_fields fd ON fd.field_id = c.field_id WHERE w.words @@ "
            "%(q)s::tsquery AND fd.document_kind = 'attachment' AND fd.markings = '[]' "
            "GROUP BY 1")


def _attachment_where(party_kind: str | None, since: bool, until: bool) -> str:
    conds = ["e.attachments > 0", "e.received_at IS NOT NULL"]
    if party_kind is not None:
        conds.append(_PARTY[party_kind])
    conds += (["e.received_at >= %(since)s"] if since else []) + (
        ["e.received_at < %(until)s"] if until else [])
    return " AND ".join(conds)


def attachment_floor_sql(*, guarded: str, party_kind: str | None, since: bool, until: bool,
                         after: bool) -> str:
    """The ``ATTACH_BATCH``-th mail (received_at, handle, and how many were read) of the
    walk below ``after``'s cursor (c_at, c_h): the oldest mail of one batch; no row when
    none is left, fewer than ``walk`` read: the batch is the last."""
    where = _attachment_where(party_kind, since, until)
    if after:
        where += " AND (e.received_at, e.handle) < (%(c_at)s, %(c_h)s)"
    return ("SELECT x.at, x.handle, count(*) OVER () AS n FROM (SELECT e.received_at AS at, "
            f"e.handle FROM {guarded}.emails e WHERE {where} ORDER BY e.received_at DESC, "
            "e.handle DESC LIMIT %(walk)s) x ORDER BY x.at, x.handle LIMIT 1")


def attachment_sql(*, guarded: str, party_kind: str | None, since: bool, until: bool,
                   common: bool, after: bool = False, floor: bool = False) -> str:
    """The reader's mail (``guarded``.emails, the same door as ``search_sql``) whose
    attachment text holds every query word (parameters q, party, since, until, k, walk),
    newest first, at most ``k``, one row per mail: ``search_sql``'s columns with the first
    matching chunk of an unmarked attachment field of that mail version, then the
    attachment's version handle and object id (for ``attachment_check_sql``). An
    attachment's citation starts with its mail's handle and ``#attachment=``; ``common``:
    the walk of one batch (the mails below the cursor ``after`` (c_at, c_h) down to the
    ``floor`` (f_at, f_h): ``attachment_floor_sql``; the caller reads batch after batch),
    else the word index's attachment match of the mails ``mails``
    (``attachment_mails_sql``)."""
    where = _attachment_where(party_kind, since, until)
    piece = ("c.body, c.start, fd.sender_class, fd.version_handle AS attachment, "
             "fd.object_id AS attachment_id")
    owned = ("starts_with(fd.citation, {h} || '#attachment=') AND fd.document_kind = "
             "'attachment' AND fd.markings = '[]'")
    if common:
        if after:
            where += " AND (e.received_at, e.handle) < (%(c_at)s, %(c_h)s)"
        if floor:
            where += " AND (e.received_at, e.handle) >= (%(f_at)s, %(f_h)s)"
        return (f"SELECT m.*, a.* FROM (SELECT {_cols('emails')} FROM {guarded}.emails e "
                f"WHERE {where} ORDER BY e.received_at DESC, e.handle DESC) m "
                f"CROSS JOIN LATERAL (SELECT {piece} FROM ontology.text_fields fd JOIN "
                "ontology.text_chunks c ON c.field_id = fd.field_id WHERE fd.citation >= "
                "m.handle || '#attachment=' AND fd.citation < m.handle || '#attachment>' AND "
                + owned.format(h="m.handle") + " AND (SELECT w.words FROM "
                "ontology.chunk_words w WHERE w.chunk_id = c.chunk_id) @@ %(q)s::tsquery "
                "ORDER BY fd.citation, c.start LIMIT 1) a "
                "ORDER BY m.at DESC, m.handle DESC LIMIT %(k)s")
    return ("WITH a AS MATERIALIZED (SELECT fd.citation, fd.document_kind, fd.markings, "
            "fd.sender_class, fd.version_handle, fd.object_id, c.body, c.start, "
            "split_part(fd.citation, '#', 1) AS mail FROM ontology.chunk_words w JOIN "
            "ontology.text_chunks c ON c.chunk_id = w.chunk_id JOIN ontology.text_fields fd "
            "ON fd.field_id = c.field_id WHERE w.words @@ %(q)s::tsquery AND "
            "fd.document_kind = 'attachment' AND fd.markings = '[]' AND "
            "split_part(fd.citation, '#', 1) = ANY(%(mails)s::text[])) "
            f"SELECT DISTINCT ON (e.received_at, e.handle) {_cols('emails')}, "
            f"{piece.replace('c.', 'a.').replace('fd.', 'a.')} FROM a JOIN {guarded}.emails e "
            "ON e.handle = a.mail AND " + owned.format(h="e.handle").replace("fd.", "a.")
            + f" WHERE e.handle = ANY(%(mails)s::text[]) AND {where}"
            + (" AND (e.received_at, e.handle) < (%(c_at)s, %(c_h)s)" if after else "")
            + " ORDER BY "
            "e.received_at DESC, e.handle DESC, a.citation, "
            "a.start LIMIT %(k)s")


def listed_sql(biz: str) -> str:
    """The mail of ``handles`` whose own header or text holds every word of ``q``
    (``search_sql``'s rule: a word page lists it), each row read by its own handle and
    object: ``attachment_sql``'s rows that are not new."""
    return (f"SELECT b.handle FROM {biz}.emails b WHERE b.handle = ANY(%(handles)s::text[]) "
            "AND (b.words @@ %(q)s::tsquery OR EXISTS (SELECT 1 FROM ontology.text_fields "
            "fd JOIN ontology.text_chunks c ON c.field_id = fd.field_id WHERE fd.object_id = "
            "b.object_ids[1] AND fd.version_handle = b.handle AND fd.markings = '[]' AND "
            "(SELECT w.words FROM ontology.chunk_words w WHERE w.chunk_id = c.chunk_id) @@ "
            "%(q)s::tsquery))")


def attachment_blocked_sql() -> str:
    """The attachment objects of ``ids`` a reader is kept from on their own (the guarded
    view checks only the mail's object): in the people exclusion's objects, or one of
    the reader's hidden documents (``plx.reader_hidden``)."""
    # EXISTS, not IN: inside the OR an IN reads all of the exclusion (1.37M rows, 9.6 s
    # for 40 ids on 11-20); EXISTS looks each id up by its index (4 ms)
    # the people set's attachments: the owner's alone (``OWNER_KEY`` in the reader's
    # audience setting, as the guarded views decide it); the company's own-issued documents
    # are blocked for every reader
    from operating.pg.access_classes import OWNER_KEY
    owner = ("(SELECT '" + OWNER_KEY + "' = ANY(ARRAY(SELECT json_array_elements_text("
             "coalesce(nullif(current_setting('plx.reader_audience', true), ''), '[]')::json))))")
    return ("SELECT x FROM unnest(%(ids)s::text[]) x WHERE (EXISTS (SELECT 1 FROM "
            "ontology.people_excluded_objects p WHERE p.object_id = x) AND NOT " + owner + ")"
            " OR EXISTS (SELECT 1 FROM ontology.own_issued_documents oi WHERE oi.object_id = x)"
            " OR x IN (SELECT "
            "json_array_elements_text("
            "coalesce(nullif(current_setting('plx.reader_hidden', true), ''), '[]')::json))")


def tsquery_any(query: str) -> str | None:
    """Any word of ``query`` (OR), as ``tsquery`` cuts them; None for a one-word query
    (its every-word match is already the any-word one)."""
    import sqlite3
    from operating.pg import words
    got = list(dict.fromkeys(t.casefold() for t in terms(query)))
    if len(got) < 2:
        return None
    try:
        return words.tsquery(" OR ".join('"' + t.replace('"', '""') + '"' for t in got))
    except sqlite3.OperationalError as exc:
        raise SearchInputError(f"the query cannot be searched: {exc}") from None


def words_held(row: Sequence[Any], words: Sequence[str]) -> int:
    """How many of the query ``words`` a search row holds in its title, sender and quoted
    chunk (a word's first letters, so a plural or tense still counts): the any-word
    match's rank."""
    view, handle, title, sender, _at, _dom, _addr, body, *_ = row
    text = " ".join(str(v) for v in (title, sender) if v).casefold()
    if body is not None:
        try:
            text += " " + zlib.decompress(bytes(body)).decode("utf-8", "replace").casefold()
        except zlib.error:
            pass
    stems = {w.casefold()[:max(4, len(w) - 2)] for w in words}
    # at a word's start: "mouth" is not held by "amount"
    return sum(1 for s in stems if re.search(r"(?<!\w)" + re.escape(s), text))


def meaning_sql(views: Sequence[str], *, guarded: str, biz: str, party_kind: str | None,
                since: bool, until: bool) -> str:
    """The rows of ``views`` the reader's guarded views hold for the objects a meaning
    search ranked (parameter ids: their object ids; q: the tsquery whose chunk is quoted
    first; q_all: the word search's own every-word tsquery), each with its quoted chunk
    and, last, whether the word search matches it (its header or its text, by
    ``search_sql``'s rule: a later word page lists such a row) and its object id: the same
    door as ``search_sql``, so a meaning hit is never a row the word search could not have
    shown this reader. A file's text is matched only once read for Freightcom (``ai_ok``),
    as in ``search_sql``."""
    parts = []
    for view in views:
        kind, when, _ = VIEWS[view]
        if party_kind is not None and view != "emails":
            continue
        # the ranked objects' rows by the table's object id index (an init plan), then
        # read through the view by handle
        ok = " AND b.ai_ok" if view in SEARCH_ONLY else ""
        conds = [f"e.handle = ANY(ARRAY(SELECT b.handle FROM {biz}.{view} b WHERE "
                 f"b.object_ids[1] = ANY(%(ids)s::text[]){ok}))", f"e.{when} IS NOT NULL"]
        if party_kind is not None:
            conds.append(_PARTY[party_kind])
        conds += ([f"e.{when} >= %(since)s"] if since else []) + (
            [f"e.{when} < %(until)s"] if until else [])
        oid = f"(SELECT b.object_ids[1] FROM {biz}.{view} b WHERE b.handle = e.handle)"
        every = (f"(EXISTS (SELECT 1 FROM {biz}.{view} b WHERE b.handle = e.handle AND "
                 "b.words @@ %(q_all)s::tsquery) OR EXISTS (SELECT 1 FROM "
                 "ontology.text_fields fd JOIN ontology.text_chunks c ON c.field_id = "
                 "fd.field_id JOIN ontology.chunk_words w ON w.chunk_id = c.chunk_id JOIN "
                 f"{biz}.{view} b ON b.handle = fd.version_handle AND b.object_ids[1] = "
                 "fd.object_id WHERE b.handle = e.handle AND w.words @@ %(q_all)s::tsquery "
                 f"AND fd.markings = '[]'{ok}))")
        parts.append(f"(SELECT {_cols(view)}, 0 AS tier, {every} AS every, {oid} AS oid FROM "
                     f"{guarded}.{view} e WHERE "
                     + " AND ".join(conds) + ")")
    if not parts:
        return ""
    return _quoted(" UNION ALL ".join(parts), views, biz, query=True, meaning=True)


_PARTY = {"address": "e.addresses @> ARRAY[%(party)s]::text[]",
          "domain": "e.domains @> ARRAY[%(party)s]::text[]",
          "record": "(e.customers && %(party)s::text[] OR e.suppliers && %(party)s::text[])"}


def _cols(view: str) -> str:
    """A search row's columns of the guarded view's row ``e``."""
    when = VIEWS[view][1]
    addresses = ("e.sender_domain, e.addresses" if view == "emails" else
                 "NULL::text AS sender_domain, NULL::text[] AS addresses")
    return (f"'{view}'::text AS view, e.handle, {_TITLE.get(view, 'e.subject')} AS "
            f"title, {_SENDER.get(view, 'e.sender')} AS sender, e.{when} AS at, {addresses}")


def _quoted(union: str, views: Sequence[str], biz: str, *, query: bool,
            meaning: bool = False) -> str:
    """The rows of ``union`` (tier, date, handle descending; the first ``k``, or every
    one with its object id last for ``meaning``), each with its quoted chunk: one holding
    the query words (q) first."""
    best = ("ORDER BY (w.words @@ %(q)s::tsquery) DESC, fd.field_id, c.start"
            if query else "ORDER BY fd.field_id, c.start")
    return (
        "SELECT y.view, y.handle, y.title, y.sender, y.at, y.sender_domain, y.addresses, "
        "s.body, s.start, s.sender_class, y.tier" + (", y.every, y.oid" if meaning else "")
        + " FROM (SELECT * FROM ("
        + union
        + ") x ORDER BY x.tier DESC, x.at DESC, x.handle DESC"
        + ("" if meaning else " LIMIT %(k)s") + ") y LEFT JOIN LATERAL ("
        "SELECT c.body, c.start, fd.sender_class FROM ontology.text_fields fd JOIN "
        "ontology.text_chunks c ON c.field_id = fd.field_id LEFT JOIN ontology.chunk_words w "
        "ON w.chunk_id = c.chunk_id WHERE fd.version_handle = y.handle AND fd.object_id IN ("
        + _objects(views, biz, "y") + ") AND fd.markings = '[]' "
        f"{best} LIMIT 1) s ON true ORDER BY y.tier DESC, y.at DESC, y.handle DESC")


def _floor(name: str, top: str | None, founds: Sequence[str], fscope: Sequence[str], *,
           biz: str, view: str, when: str, newest: str, depth: int, kk: str) -> str:
    """One walk stage's floor (``search_sql``), one row always: the ``walk * depth``-th
    newest row of the table in the dates (``fscope``); no such row (the table holds no
    more) is the floor below every row, with nothing under it (``more`` false). A later
    stage is read only when the stages above it found some rows but fewer than ``k``, as
    many as promise ``k`` within it (found * DEEPER >= k; ``kk``: the view's ``k``), and
    more rows lie below them;
    otherwise its floor is the one above (an empty stage) and a rare word goes straight to
    the word index. A COALESCE in a walk's own condition is not pushed into the guarded
    (security barrier) view, so the floor's columns are never null."""
    def read(*gate: str) -> str:
        return (f"SELECT e.{when} AS at, e.handle FROM {biz}.{view} e WHERE "
                + " AND ".join([*gate, *fscope])
                + f" {newest} OFFSET %(walk)s * {depth} - 1 LIMIT 1")
    if top is None:
        return (f"{name} AS MATERIALIZED (SELECT coalesce(x.at, '-infinity') AS at, "
                "coalesce(x.handle, '') AS handle, x.handle IS NOT NULL AS more FROM "
                f"(SELECT 1) o LEFT JOIN LATERAL ({read()}) x ON true)")
    n = " + ".join(f"(SELECT count(*) FROM {f})" for f in founds)
    run = f"t.more AND {n} > 0 AND {n} < {kk} AND ({n}) * {DEEPER} >= {kk}"
    return (f"{name} AS MATERIALIZED (SELECT CASE WHEN g.run THEN coalesce(x.at, '-infinity') "
            "ELSE t.at END AS at, CASE WHEN g.run THEN coalesce(x.handle, '') ELSE t.handle "
            "END AS handle, CASE WHEN g.run THEN x.handle IS NOT NULL ELSE t.more END AS more "
            f"FROM {top} t CROSS JOIN LATERAL (SELECT {run} AS run) g LEFT JOIN LATERAL "
            f"({read('g.run')}) x ON true)")


def _fallback(part: Any, tier: int, scope: Sequence[str], floor: str,
              founds: Sequence[str], *, when: str, match: str, keep: Sequence[str],
              kk: str) -> str:
    """The ``tier`` rows past the walked ones (``search_sql``), from the word index's whole
    match (``match``: handles, bounded by the dates) cut below the last walk stage's floor.
    Read only when the walk found fewer than ``k`` rows and rows lie below the floor (the
    table holds more rows than the walk): both are init plans of a one-time filter, so the
    match itself (an init plan too) is otherwise never run. ARRAY(...): the match is read
    once and the view's rows found by handle; as "IN (...)" the planner, taking the
    reader's rows for one, ran the match again per row of the window (a party search: 25 s
    timeout)."""
    last = f"(SELECT f.at FROM {floor} f)"
    last_h = f"(SELECT f.handle FROM {floor} f)"
    n = " + ".join(f"(SELECT count(*) FROM {f})" for f in founds)
    short = f"{n} < {kk} AND (SELECT f.more FROM {floor} f)"
    return part(tier, [short, *scope, f"(e.{when}, e.handle) < ({last}, {last_h})",
                       f"e.handle = ANY(ARRAY({match} AND b.{when} <= {last}))", *keep])


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
        parts.append(f"SELECT '{view}'::text AS view, e.handle, e.subject, "
                     f"{_SENDER.get(view, 'e.sender')} AS sender, "
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


def snippet(body: Any, words: Sequence[str]) -> tuple[str, bool] | None:
    """The body's window around its first matched word (whitespace folded) and whether the
    text goes on before or after it; None: no body, or one that cannot be read."""
    if body is None:
        return None
    try:
        text = zlib.decompress(bytes(body)).decode("utf-8", "replace")
    except zlib.error:
        return None
    low = text.lower()
    at = min((i for i in (low.find(w.lower()) for w in words) if i >= 0), default=0)
    start = max(0, at - SNIPPET_CHARS // 3)
    end = start + SNIPPET_CHARS
    return " ".join(text[start:end].split()), start > 0 or end < len(text)


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
        got = snippet(body, words)
        hit["snippet"] = None
        if got is not None and got[0]:
            hit["snippet"] = _text(got[0], handle, SNIPPET_CHARS)
            if got[1]:
                hit["snippet"]["truncated"] = True
        if party_ is not None and party_.kind in ("address", "domain"):
            mine = (sender == party_.value if party_.kind == "address"
                    else sender_domain == party_.value)
            hit["direction"] = "from_them" if mine else "to_them"
            hit["matched_by"] = party_.kind
        out.append(hit)
    return out


def attach_cursor(hit_row: Optional[Sequence[Any]]) -> str:
    """The cursor after ``hit_row`` in the stream of mail holding the words only in an
    attachment's text: ``a1:<date>|<handle>`` (the next page lists the ones older); None:
    the stream's start (past every date)."""
    if hit_row is None:
        return "a1:9999-12-31T00:00:00+00:00|~"
    return "a1:" + hit_row[4].astimezone(dt.timezone.utc).isoformat() + "|" + hit_row[1]


def read_attach_cursor(text: str) -> tuple[dt.datetime, str]:
    """(date, handle) of an ``attach_cursor``; anything else is refused."""
    found = re.fullmatch(r"a1:([0-9T:.+-]+)\|(.+)", text) if isinstance(text, str) else None
    if found is None:
        raise SearchInputError("not a cursor of this search")
    try:
        at = dt.datetime.fromisoformat(found.group(1))
    except ValueError:
        raise SearchInputError("not a cursor of this search") from None
    if at.tzinfo is None:
        raise SearchInputError("not a cursor of this search")
    return at, found.group(2)


def meaning_cursor(few: bool, pos: int, words_only: bool = False) -> str:
    """The cursor of the stream of rows past the ones a page showed beyond its word match:
    ``m1:<few 0|1>:<place>`` for the rows close in meaning (the place in the ranking's
    order the next page starts from; ``few``: the first page widened a search with fewer
    than ``WIDEN_BELOW`` word matches, which orders the rows by the words they hold first),
    ``m1:<few>:<place>:w`` for the rows holding SOME of the words when no ranking was had
    (newest first; ``few`` is always 1)."""
    return f"m1:{1 if few else 0}:{int(pos)}" + (":w" if words_only else "")


def read_meaning_cursor(text: str) -> tuple[bool, int, bool]:
    """(few, place, words only) of a ``meaning_cursor``; anything else is refused."""
    found = re.fullmatch(r"m1:([01]):([0-9]{1,6})(:w)?", text) if isinstance(text, str) else None
    if found is None:
        raise SearchInputError("not a cursor of this search")
    return found.group(1) == "1", int(found.group(2)), bool(found.group(3))


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


def near_offset(text: str, words: Sequence[str], most: int) -> int | None:
    """Where a window of ``most`` characters starts to show the first place ``text``
    holds one of ``words`` (a word start, case folded): a quarter of the window before it,
    at a line or word break; None when it holds none of them."""
    found = [m.start() for w in words if w
             for m in [re.search(r"(?<!\w)" + re.escape(w), text, re.IGNORECASE)] if m]
    if not found:
        return None
    at = max(0, min(found) - most // 4)
    if at:
        brk = max(text.rfind("\n", 0, at + 1), text.rfind(" ", 0, at + 1))
        at = brk + 1 if brk >= 0 and min(found) - brk <= most // 2 else at
    return at


# A ``near`` window opens past the item's start; its first part (the newest message of a
# reply, the part that says how a thread came out) is served with it, ahead of the window
# (11-22: Ask opened a supplier's reply at the word "complaint", the window began in the
# quoted history, and the answer "Very happy to hear that!" above it was never shown). The
# head is the text of the first ``HEAD_MAX`` characters without its links (a safety
# notice's link alone is ~900: each is shown as ``LINK_GAP``), at most ``HEAD_WORDS``
# characters and at most half the window (a window under ``HEAD_MIN`` twice: no head).
HEAD_WORDS = 400
HEAD_MIN = 80
HEAD_MAX = 2500
_LINK = re.compile(r"<?(?:https?|mailto):[^\s<>]*>?", re.IGNORECASE)
HEAD_GAP = "\n[...]\n"
LINK_GAP = "<link>"


def head_pieces(text: str, most: int) -> list[tuple[int, int]]:
    """The ``(start, end)`` parts of ``text`` its head serves (``HEAD_WORDS``)."""
    room = min(HEAD_WORDS, most // 2 - len(HEAD_GAP))
    if room < HEAD_MIN:   # a window too small to hold a head as well: none
        return []
    cap, at, out = min(len(text), HEAD_MAX), 0, []
    for m in [*_LINK.finditer(text, 0, cap), None]:
        end = cap if m is None else m.start()
        take = min(end - at, room)
        if take > 0:
            out.append((at, at + take))
            room -= take + len(LINK_GAP)
        if m is None or room <= 0:
            break
        at = m.end()
    return out


def opened(rows: Sequence[Sequence[Any]], *, offset: int, most: int,
           near: Sequence[str] = ()) -> dict[str, Any]:
    """One opened item: its text (every field, joined as ``assemble`` joins them) from
    ``offset`` (with ``near`` words and no offset: from just before the first of them
    the text holds, ``near_offset``; ``near_found`` says whether one was), at most
    ``most`` characters, and what of each field that window served
    (``fields_served``: the field, its text digest, the field's own span served, the
    field's length and where that part starts in ``text``), so the session keeps it as
    it keeps a ``plx.read`` (``mcp_server_evidence._served_texts``). A ``near`` window
    that starts past the item's head serves the head first (``head_pieces``: the first
    ``head_chars`` of ``text``, ending in ``HEAD_GAP``), the window after it, both
    within ``most``."""
    view, handle, subject, sender, at, to, cc = rows[0][:7]
    text = assemble([(r[7], r[8], r[9]) for r in rows])
    found = near_offset(text, near, most) if near and not offset else None
    # (start, end) of ``text`` in the order served, and what is shown after each
    pieces: list[tuple[int, int, str]] = []
    if found is not None:
        offset = found
        head = head_pieces(text, most)
        if head and head[-1][1] < offset:
            pieces = [(a, b, LINK_GAP) for a, b in head]
            pieces[-1] = (*head[-1], HEAD_GAP)
            most -= sum(b - a + len(g) for a, b, g in pieces)
        elif head:   # the place asked about is in the head: the window starts at the top
            offset = 0
    pieces.append((offset, min(len(text), offset + most), ""))
    window = "".join(text[a:b] + gap for a, b, gap in pieces)
    out: dict[str, Any] = {"citation": handle, "kind": VIEWS[view][0],
                           "title": _text(subject, handle, 500) if subject else None,
                           "date": _iso(at),
                           "sender": _text(sender, handle, 300) if sender else None,
                           "text": {"untrusted_text": window, "origin": {"citation": handle}},
                           "text_offset": offset, "text_chars": len(text)}
    if len(pieces) > 1:
        out["head_chars"] = len(window) - (pieces[-1][1] - pieces[-1][0])
    if to:
        out["to"] = list(to)
    if cc:
        out["cc"] = list(cc)
    if near and not out.get("near_found"):
        out["near_found"] = found is not None
    if offset + most < len(text):
        out["text"]["truncated"] = True
        out["next_offset"] = offset + most
    if not text:
        out["text_note"] = "no readable text is indexed for this item"
    served = []
    named = {r[7]: (r[11], r[12]) for r in rows if r[7] is not None and len(r) > 12}
    shown_at = 0   # where each piece starts in ``window``
    for lo_t, hi_t, gap in pieces:
        at_ = 0
        for field in sorted(named):
            length = len(assemble([(r[7], r[8], r[9]) for r in rows if r[7] == field]))
            lo, hi = max(lo_t, at_), min(hi_t, at_ + length)
            if hi > lo and isinstance(named[field][0], str):
                served.append({"field": named[field][0], "version": named[field][1],
                               "offset": lo - at_, "chars": hi - lo, "total_chars": length,
                               "at": shown_at + lo - lo_t})
            at_ += length + 2       # the "\n\n" ``assemble`` joins fields with
        shown_at += hi_t - lo_t + len(gap)
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


def depth_said(query: str | None, attach: Mapping[str, Any], *, later: bool,
               attachments: bool) -> str | None:
    """How deep a search with ``query`` looked, said to the model (``searched.depth``)
    where it did not read every match: the words past ``MAX_TERMS`` (not searched), and
    the attachment pass (``attach``: what ``SqlTool._comms_attached`` recorded; it runs on
    a first page only, ``attachments``: it applies to this reader and search). None when
    the search read everything it was asked for."""
    parts = []
    rest = _WORD.findall(query or "")[MAX_TERMS:]
    if rest:
        parts.append(f"only the first {MAX_TERMS} words were searched; not searched: "
                     + " ".join(rest))
    if query and attachments:
        if later:
            parts.append("this later page lists the word matches only: mail holding the "
                         "words only in an attachment's text continues by its own cursor "
                         "(completeness.continue); matches close in meaning are on the "
                         "first page")
        elif attach.get("unread"):
            parts.append("attachments' text was not searched this time (that pass ran out "
                         "of its time): a mail holding the words only in an attachment "
                         "may be missing")
    return "; ".join(parts)[:600] or None


__all__ = ["Party", "SEARCH_ONLY", "depth_said", "SearchInputError", "VIEWS", "assemble", "stem_tsquery",
           "attachment_blocked_sql", "attach_cursor", "read_attach_cursor", "attachment_floor_sql", "attachment_mails_sql", "attachment_sql", "listed_sql", "cursor_of", "exists_sql",
           "meaning_cursor", "read_meaning_cursor", "meaning_sql", "tsquery_any", "words_held",
           "hits", "instant", "open_sql", "opened", "party", "present", "read_cursor",
           "search_sql", "searched", "snippet", "terms", "tsquery"]
