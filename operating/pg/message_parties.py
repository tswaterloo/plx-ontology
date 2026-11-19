"""Messages tied to the customers and suppliers they are with or about, beyond the stored
links: the mail addresses on a message, and the party names its text spells.

``biz.emails.customers`` / ``suppliers`` held only the ontology's own links (the sender
step's, a record number's). A customer's mail from a colleague's address, a reply copying a
supplier, and a Teams message naming a customer were linked to nothing, so a roll-up from a
customer to everything said about it missed them. This module adds two rules, both
general (read from the party rows, no name or domain is written here) and conservative:

* **Domain** (mail): a participant address (sender, to, cc) whose registrable domain is
  held by exactly one party (a customer's or supplier's stored ``email`` or ``website``;
  same-name rows are one party) links the message to that party. Never a shared mail
  provider (``ontology.mentions.shared_mail_domain``), one of our own domains (the staff
  table's), a domain two different parties hold, or a message with more than
  ``MAX_DOMAINS`` participant domains (a broadcast tells nothing of one party).
* **Name** (mail subject, Teams text): the party's whole normalized name
  (``identity_scoring.normalize_name``) as a run of words. Never a name of generic words
  only, a one-word name under ``MIN_SINGLE`` letters or that starts another party's name
  (``Sunrise`` of ``Sunrise Foods``), a name that is one of ours, or a name spelled in more
  than ``MAX_NAME_HITS`` messages (a bound above the busiest customer's mail), or a name
  spelled in mail from ``MAX_SPREAD`` or more different sender domains (a common word that
  names a placeholder customer is written by everyone; a customer's name by its own
  correspondents). The spread is read from the mail table's header words
  (``common_names``) and applies to Teams
  too: Teams has no senders of its own to tell by.

Run by ``fill`` and ``refresh_changed`` on the rows they write, and as ``python3 -m
operating.pg.message_parties`` (``measure`` counts and writes nothing; ``apply`` writes the
union of the stored links and these rules). Stdlib plus psycopg."""
from __future__ import annotations

import json
import re
import sys
import zlib
from collections import defaultdict
from typing import Any, Iterable, Iterator, Mapping, Sequence

MAX_DOMAINS = 8
MIN_SINGLE = 6
MAX_NAME_HITS = 40000
MAX_WORDS = 6
MAX_SPREAD = 50
SPREAD_TTL = 6 * 3600
BATCH = 20000
RULES = {"max_domains": MAX_DOMAINS, "min_single": MIN_SINGLE, "max_name_hits": MAX_NAME_HITS,
         "max_words": MAX_WORDS, "max_spread": MAX_SPREAD}
_ADDR = re.compile(r"[A-Za-z0-9._%+'-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_KINDS = ("customer", "supplier")
_TABLES = {"customer": "customers", "supplier": "suppliers"}


# English function words: a name made mostly of them ("For and Against", "No Pong"'s "no")
# is a phrase people write, not one party
FUNCTION_WORDS = frozenset(
    "a an and are as at be but by can do dont for from had has have he her his i if in is it "
    "its me my no not of on one or our she so that the their them there they this to up us "
    "use was we were what when who will with you your all any each more most new old other "
    "some such than then these those very unknown misc miscellaneous cash default test "
    "customer supplier vendor sample samples other general".split())


class PartyIndex:
    """What the party rows say: domain -> parties, normalized name -> parties."""

    def __init__(self, parties: Iterable[Mapping[str, Any]], own_domains: Iterable[str] = ()):
        from ontology import mentions, sender_parties as sp
        from ontology.identity_scoring import normalize_name
        self._norm = normalize_name
        self.own = {mentions.registrable_domain(d.lower()) for d in own_domains if d}
        by_domain: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        by_name: dict[str, set] = defaultdict(set)
        parties = list(parties)
        staff = {normalize_name(n) for p in parties for n in p.get("people", ())}
        for p in parties:
            if "people" in p:
                continue
            entity = normalize_name(p.get("name")) or p["handle"]
            held = set()
            for text in (p.get("email"), ):
                held.update(m.group(1).lower() for m in _ADDR.finditer(text or ""))
            site = mentions.website_domain(p.get("website"))
            if site:
                held.add(site)
            regs = {mentions.registrable_domain(d) for d in held}
            for reg in regs:
                if reg and not mentions.shared_mail_domain(reg) and reg not in self.own:
                    by_domain[reg][entity].add((p["kind"], p["handle"]))
            # a row kept under a staff address (a person's own customer row, a test row)
            # names nobody outside the company
            if entity != p["handle"] and p.get("active", True) and not regs & self.own:
                by_name[entity].add((p["kind"], p["handle"]))
        # one party per domain: every row of it carries the same normalized name
        self.domains: dict[str, tuple] = {d: tuple(sorted(next(iter(e.values()))))
                                          for d, e in by_domain.items() if len(e) == 1}
        self.ambiguous_domains = sum(1 for e in by_domain.values() if len(e) > 1)
        firsts: dict[str, set] = defaultdict(set)
        for name in by_name:
            firsts[name.split(" ")[0]].add(name)
        generic = sp._GENERIC
        own_labels = {d.split(".")[0] for d in self.own}
        self.names: dict[str, tuple] = {}
        for name, holders in by_name.items():
            words = name.split(" ")
            if len(words) > MAX_WORDS or name in staff or any(len(w) < 2 for w in words) \
                    or sum(w in generic or w in FUNCTION_WORDS for w in words) * 2 >= len(words):
                continue
            if len(words) == 1 and (len(name) < MIN_SINGLE or len(firsts[name]) > 1):
                continue
            if any(len(w) >= 4 and w in label for label in own_labels for w in words):
                continue
            self.names[name] = tuple(sorted(holders))
        self.max_len = max((len(n.split(" ")) for n in self.names), default=1)

    def drop(self, names: Iterable[str]) -> None:
        """Remove names (the common ones) from the usable set."""
        for n in names:
            self.names.pop(n, None)
        self.max_len = max((len(n.split(" ")) for n in self.names), default=1)

    def parties_of_domains(self, domains: Iterable[str]) -> set:
        from ontology import mentions
        out: set = set()
        for d in domains:
            out.update(self.domains.get(mentions.registrable_domain(d.lower()), ()))
        return out

    def names_in(self, text: str) -> set:
        """The normalized party names a text spells (whole word runs)."""
        words = self._norm(text).split(" ")
        spans = []
        for i in range(len(words)):
            for n in range(1, min(self.max_len, len(words) - i) + 1):
                run = " ".join(words[i:i + n])
                if run in self.names:
                    spans.append((i, i + n, run))
        # a name inside a longer name that is also spelled here is part of that name
        # ("Express Car Wash" inside "Royal Coach Express Car Wash"): the longest stays
        return {run for a, b, run in spans
                if not any(c <= a and b <= d and (d - c) > (b - a) for c, d, _ in spans)}


ACTIVE = {"customer": ("sales_orders", "sales_invoices", "customer_balances"),
          "supplier": ("purchase_orders", "supplier_invoices", "supplier_balances")}


def load_parties(conn: Any) -> tuple[list[dict], list[str]]:
    """The parties with business behind them (an order, an invoice or a balance: a row
    nothing was ever done with names nothing) and our own domains and people's names."""
    rows = []
    for kind, view, website in (("customer", "customers", "NULL"),
                                ("supplier", "suppliers", "website")):
        active = " OR ".join(f"EXISTS (SELECT 1 FROM biz.{t} x WHERE x.{kind}_handle = "
                             "p.handle)" for t in ACTIVE[kind])
        for name, email, site, handle, ok in conn.execute(
                f"SELECT name, email, {website}, handle, {active} FROM biz.{view} p"):
            rows.append({"kind": kind, "name": name, "email": email, "website": site,
                         "handle": handle, "active": bool(ok)})
    own = [r[0] for r in conn.execute(
        "SELECT DISTINCT split_part(lower(email), '@', 2) FROM biz.staff WHERE email LIKE '%@%'")]
    people = [r[0] for r in conn.execute("SELECT name FROM biz.staff WHERE name IS NOT NULL")]
    rows.append({"people": people})
    return rows, own


_SPREAD: dict[str, Any] = {"at": 0.0, "key": None, "common": frozenset()}


def common_names(conn: Any, names: Iterable[str]) -> frozenset:
    """The names written in mail from ``MAX_SPREAD`` or more different sender domains
    (the mail table's header words, one phrase probe per name by its index). Kept
    ``SPREAD_TTL`` seconds per process and name set: a refresh decides a few rows and must
    not read this each time. Nothing is common while ``biz.emails`` is not built."""
    import time
    names = sorted(set(names))
    key = hash(tuple(names))
    now = time.monotonic()
    if _SPREAD["key"] == key and now - _SPREAD["at"] < SPREAD_TTL:
        return _SPREAD["common"]
    if conn.execute("SELECT to_regclass('biz.emails') IS NULL").fetchone()[0]:
        return frozenset()
    rows = conn.execute(
        "SELECT n FROM unnest(%s::text[]) n WHERE (SELECT count(DISTINCT sender_domain) FROM "
        "(SELECT sender_domain FROM biz.emails WHERE words @@ phraseto_tsquery('simple', n)) "
        "x) >= %s", (names, MAX_SPREAD)).fetchall()
    _SPREAD.update(at=now, key=key, common=frozenset(r[0] for r in rows))
    return _SPREAD["common"]


def _chunks(seq: Sequence, n: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def domain_hits(conn: Any, index: PartyIndex, table: str, where: str) -> dict[str, set]:
    """object id -> parties of its participant domains (mail), read from the table's own
    ``domains`` column."""
    from ontology import mentions
    seen: dict[str, tuple] = {}
    for (d,) in conn.execute(f"SELECT DISTINCT unnest(domains) FROM {table} WHERE {where}"):
        reg = mentions.registrable_domain(d.lower())
        if reg in index.domains:
            seen[d] = index.domains[reg]
    out: dict[str, set] = {}
    if not seen:
        return out
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS mp_seen (domain text PRIMARY KEY)")
    conn.execute("TRUNCATE mp_seen")
    with conn.cursor().copy("COPY mp_seen (domain) FROM STDIN") as cp:
        for d in seen:
            cp.write_row((d,))
    for oid, doms in conn.execute(
            f"SELECT object_ids[1], domains FROM {table} t WHERE {where} AND cardinality(domains) "
            f"<= {int(MAX_DOMAINS)} AND domains && ARRAY(SELECT domain FROM mp_seen)"):
        for d in doms:
            for party in seen.get(d, ()):
                out.setdefault(oid, set()).add(party)
    return out


def _texts(conn: Any, view: str, table: str, where: str) -> Iterator[tuple[str, str]]:
    """(object id, text) of the rows: a mail's subject, a Teams message's own text (the
    version's unmarked document text, as ``comms_search`` opens it)."""
    if view == "emails":
        yield from conn.execute(f"SELECT object_ids[1], subject FROM {table} WHERE {where} "
                                "AND subject IS NOT NULL")
        return
    cur = conn.execute(
        f"SELECT t.object_ids[1], c.body FROM {table} t JOIN ontology.text_fields fd ON "
        "fd.version_handle = t.handle AND fd.object_id = t.object_ids[1] AND fd.markings = '[]' "
        f"JOIN ontology.text_chunks c ON c.field_id = fd.field_id WHERE {where} "
        "ORDER BY t.object_ids[1], fd.field_id, c.start")
    last, parts = None, []
    for oid, body in cur:
        if oid != last and parts:
            yield last, own_text("\n".join(parts))
            parts = []
        last = oid
        try:
            parts.append(zlib.decompress(bytes(body)).decode("utf-8", "replace"))
        except zlib.error:
            continue
    if parts:
        yield last, own_text("\n".join(parts))


def own_text(text: str) -> str:
    """The sender's own words: a reply's quoted history and a forward's body are another
    message's text (``mentions.quoted_ranges``)."""
    from ontology import mentions
    ranges = mentions.quoted_ranges(text)
    if not ranges:
        return text
    out, at = [], 0
    for start, end in sorted(ranges):
        if start > at:
            out.append(text[at:start])
        at = max(at, end)
    out.append(text[at:])
    return "\n".join(out)


def name_hits(conn: Any, index: PartyIndex, view: str, table: str, where: str
              ) -> tuple[dict[str, set], dict[str, int]]:
    """object id -> parties its text names; names spelled in more than ``MAX_NAME_HITS``
    messages are dropped (and counted in the second result)."""
    per_name: dict[str, list] = defaultdict(list)
    for oid, text in _texts(conn, view, table, where):
        for name in index.names_in(text):
            per_name[name].append(oid)
    out: dict[str, set] = {}
    dropped: dict[str, int] = {}
    for name, oids in per_name.items():
        if len(oids) > MAX_NAME_HITS:
            dropped[name] = len(oids)
            continue
        for oid in oids:
            out.setdefault(oid, set()).update(index.names[name])
    return out, dropped


def infer(conn: Any, view: str, table: str, where: str = "true", index: PartyIndex | None = None,
          drop_names: Iterable[str] = ()) -> tuple[dict[str, set], dict[str, Any]]:
    """The parties each row of ``table`` (where ``where``) is with or about. ``drop_names``:
    names a full pass found in more than ``MAX_NAME_HITS`` messages (kept in the table's
    marks): a few new rows cannot show that count, so the pass's verdict stands."""
    common: Sequence[str] = ()
    if index is None:
        parties, own = load_parties(conn)
        index = PartyIndex(parties, own)
        common = sorted(common_names(conn, index.names))
        index.drop(common)
        index.drop(drop_names)
    got = domain_hits(conn, index, table, where) if view == "emails" else {}
    by_name, dropped = name_hits(conn, index, view, table, where)
    n_dom = len(got)
    for oid, parties_ in by_name.items():
        got.setdefault(oid, set()).update(parties_)
    return got, {"by_domain": n_dom, "by_name": len(by_name), "union": len(got),
                 "names_dropped": dropped, "common_names": list(common), "usable_names": len(index.names),
                 "usable_domains": len(index.domains),
                 "ambiguous_domains": index.ambiguous_domains}


def apply(conn: Any, view: str, table: str, where: str = "true", *, write: bool = True,
          index: PartyIndex | None = None, drop_names: Iterable[str] = ()) -> dict[str, Any]:
    """Union the inferred parties into the rows' ``customers`` / ``suppliers`` (never removes
    a stored link). ``write`` False: count only."""
    needed = ["biz.staff", *(f"biz.{t}" for k in _KINDS for t in
                             (_TABLES[k], *ACTIVE[k]))]
    missing = [r for r in needed if index is None and conn.execute(
        "SELECT to_regclass(%s) IS NULL", (r,)).fetchone()[0]]
    if missing:   # the party tables are not built yet (a first build): nothing is claimed
        return {"skipped": f"no {', '.join(missing)}", "rows_updated": 0}
    got, stats = infer(conn, view, table, where, index, drop_names)
    changed = 0
    if got:
        conn.execute("DROP TABLE IF EXISTS pg_temp.mp_add")
        conn.execute("CREATE TEMP TABLE mp_add (oid text PRIMARY KEY, customers text[], "
                     "suppliers text[])")
        with conn.cursor().copy("COPY mp_add (oid, customers, suppliers) FROM STDIN") as cp:
            for oid, ps in got.items():
                cp.write_row((oid, sorted(h for k, h in ps if k == "customer"),
                              sorted(h for k, h in ps if k == "supplier")))
        stats["newly_covered"] = conn.execute(
            f"SELECT count(*) FROM {table} t JOIN mp_add a ON t.object_ids[1] = a.oid WHERE "
            "t.customers = '{}' AND t.suppliers = '{}'").fetchone()[0]
        if write:   # one statement: a row gaining both kinds is rewritten once
            def gain(col: str) -> str:
                return f"(cardinality(a.{col}) > 0 AND NOT t.{col} @> a.{col})"

            def union(col: str) -> str:
                return (f"CASE WHEN {gain(col)} THEN ARRAY(SELECT DISTINCT x FROM unnest("
                        f"t.{col} || a.{col}) x ORDER BY 1) ELSE t.{col} END")
            changed = conn.execute(
                "UPDATE " + table + " t SET " + ", ".join(
                    f"{c} = {union(c)}" for c in _TABLES.values()) + " FROM mp_add a WHERE "
                "t.object_ids[1] = a.oid AND (" + " OR ".join(
                    gain(c) for c in _TABLES.values()) + ")").rowcount
    stats["rows_updated"] = changed
    return stats


def _stage_links(write: Any, view: str) -> None:
    """Temp tables ``mp_r`` (the objects with a link of the table's types or a value held)
    and ``mp_lk`` (their links by ``business_comms._links_sql``, the build's own rule)."""
    from operating.pg import business_comms as bc
    spec = bc.SPECS[view]
    t = f"{bc.bv.SCHEMA}.{view}"
    bc._prep(write, spec, None)
    write.execute("SET LOCAL search_path = ontology, public")
    types = ", ".join(bc.bv_lit(x) for x in spec.types)
    write.execute("DROP TABLE IF EXISTS pg_temp.mp_r")
    write.execute("CREATE TEMP TABLE mp_r AS SELECT DISTINCT l.from_object AS object_id "
                  "FROM links l JOIN objects o ON o.object_id = l.from_object WHERE "
                  f"l.superseded_at IS NULL AND o.object_type IN ({types}) UNION SELECT "
                  f"t.object_ids[1] FROM {t} t WHERE t.customers <> '{{}}' OR "
                  "t.suppliers <> '{}' OR t.records <> '{}'")
    write.execute("CREATE INDEX ON mp_r (object_id)")
    write.execute("ANALYZE mp_r")
    write.execute("DROP TABLE IF EXISTS pg_temp.mp_lk")
    write.execute("CREATE TEMP TABLE mp_lk AS " + bc._links_sql(spec, "mp_r"))
    write.execute("CREATE INDEX ON mp_lk (oid)")
    write.execute("ANALYZE mp_lk")


def relink(write: Any, view: str, *, dry: bool = False) -> dict[str, Any]:
    """Set the rows' stored-link columns (``customers``, ``suppliers``, ``records``) to what
    the ontology's links say now (``business_comms._links_sql``, the build's own rule), for
    every row with a link of its type or a value held; only rows that differ are written.
    A table built or refreshed before its links were followed keeps none of them until
    this (measured 11-19: 57 of 160,150 sampled mails held the customers their stored links
    name; the rule gives 1,459)."""
    from operating.pg import business_comms as bc
    spec = bc.SPECS[view]
    t = f"{bc.bv.SCHEMA}.{view}"
    with write.transaction():
        _stage_links(write, view)
        change = ("FROM mp_r r LEFT JOIN mp_lk k ON k.oid = r.object_id WHERE t.object_ids[1] "
                  "= r.object_id AND (t.customers, t.suppliers, t.records) IS DISTINCT FROM "
                  "(coalesce(k.customers, '{}'), coalesce(k.suppliers, '{}'), "
                  "coalesce(k.records, '{}'))")
        if dry:   # count what would change; nothing written
            n = write.execute(f"SELECT count(*) FROM {t} t, " + change[len("FROM "):]
                              ).fetchone()[0]
            return {"would_relink_rows": n}
        n = write.execute(
            f"UPDATE {t} t SET customers = coalesce(k.customers, '{{}}'), suppliers = "
            f"coalesce(k.suppliers, '{{}}'), records = coalesce(k.records, '{{}}') "
            + change).rowcount
    return {"relinked_rows": n}


def redo(write: Any, view: str) -> dict[str, Any]:
    """Decide every row's party links again (``relink`` to the stored links, then ``apply``
    the rules) and keep the rule's stamp in the table's marks, so ``refresh_changed`` does
    not repeat it. One transaction: readers see the old links or the new, never half."""
    from operating.pg import business_comms as bc
    spec = bc.SPECS[view]
    t = f"{bc.bv.SCHEMA}.{view}"
    with write.transaction():
        out = relink(write, view)
        out.update(apply(write, view, t))
        write.execute(f"UPDATE {bc.bv.SCHEMA}.state SET notes = jsonb_set(jsonb_set(notes, "
                      "'{marks,links}', to_jsonb(%s::text)), '{marks,dropped}', %s::jsonb) "
                      "WHERE view = %s", (bc.links_rule(spec), json.dumps(sorted(
                          out.get("names_dropped", {}))), view))
    return out


def redo_batched(write: Any, view: str, *, batch: int = 20000, min_free_gb: float = 40.0,
                 path: str = "/data", log: Any = None) -> dict[str, Any]:
    """``redo`` for a hand run over the whole table: the decision is staged in temp tables
    (the stored links by the build's rule, the rule-inferred parties; nothing written), then
    the rows that differ are rewritten ``batch`` rows per short transaction, the free space on
    ``path`` read before each (below ``min_free_gb`` it stops, nothing stamped, and the run
    can be repeated: a row already at its target is skipped). The stamp is written last, so a
    refresh never repeats it. ``write`` must be on an autocommit connection (temp tables
    live across the batches)."""
    import shutil
    from operating.pg import business_comms as bc
    spec = bc.SPECS[view]
    t = f"{bc.bv.SCHEMA}.{view}"
    say = log or (lambda *_: None)
    with write.transaction():
        _stage_links(write, view)
    got, stats = infer(write, view, t)
    write.execute("DROP TABLE IF EXISTS pg_temp.mp_add")
    write.execute("CREATE TEMP TABLE mp_add (oid text PRIMARY KEY, customers text[], "
                  "suppliers text[])")
    with write.cursor().copy("COPY mp_add (oid, customers, suppliers) FROM STDIN") as cp:
        for oid, ps in got.items():
            cp.write_row((oid, sorted(h for k, h in ps if k == "customer"),
                          sorted(h for k, h in ps if k == "supplier")))
    def merged(col: str) -> str:
        return (f"ARRAY(SELECT DISTINCT x FROM unnest(coalesce(k.{col}, '{{}}') || "
                f"coalesce(a.{col}, '{{}}')) x ORDER BY 1)")
    with write.transaction():
        write.execute("DROP TABLE IF EXISTS pg_temp.mp_todo")
        write.execute(
            f"CREATE TEMP TABLE mp_todo AS SELECT row_number() OVER (ORDER BY t.object_ids[1]) "
            f"AS n, t.object_ids[1] AS oid, {merged('customers')} AS customers, "
            f"{merged('suppliers')} AS suppliers, coalesce(k.records, '{{}}') AS records "
            f"FROM {t} t LEFT JOIN mp_lk k ON k.oid = t.object_ids[1] LEFT JOIN mp_add a ON "
            "a.oid = t.object_ids[1] WHERE (t.customers, t.suppliers, t.records) IS DISTINCT "
            f"FROM ({merged('customers')}, {merged('suppliers')}, coalesce(k.records, '{{}}')) "
            "AND (k.oid IS NOT NULL OR a.oid IS NOT NULL OR t.customers <> '{}' OR "
            "t.suppliers <> '{}' OR t.records <> '{}')")
        write.execute("CREATE INDEX ON mp_todo (n)")
        total = write.execute("SELECT count(*) FROM mp_todo").fetchone()[0]
    done, lo = 0, 1
    while lo <= total:
        free = shutil.disk_usage(path).free / 1e9
        if free < min_free_gb:
            say(f"stopped: {free:.1f} GB free < {min_free_gb}")
            return {"stopped_low_disk": True, "free_gb": round(free, 1), "rows_updated": done,
                    "rows_to_update": total, "stats": stats}
        with write.transaction():
            done += write.execute(
                f"UPDATE {t} t SET customers = d.customers, suppliers = d.suppliers, records = "
                "d.records FROM mp_todo d WHERE d.n >= %s AND d.n < %s AND t.object_ids[1] = "
                "d.oid", (lo, lo + batch)).rowcount
        lo += batch
        say(f"{min(lo - 1, total)}/{total} rows, {free:.1f} GB free")
    write.execute(f"UPDATE {bc.bv.SCHEMA}.state SET notes = jsonb_set(jsonb_set(notes, "
                  "'{marks,links}', to_jsonb(%s::text)), '{marks,dropped}', %s::jsonb) "
                  "WHERE view = %s", (bc.links_rule(spec), json.dumps(sorted(
                      stats["names_dropped"])), view))
    return {"rows_updated": done, "rows_to_update": total, "stats": stats}


class _Write:
    """The minimum ``business_comms`` asks of a writer, over one psycopg connection."""

    def __init__(self, conn: Any):
        self._c = conn

    def execute(self, *a: Any, **k: Any) -> Any:
        return self._c.execute(*a, **k)

    def cursor(self) -> Any:
        return self._c.cursor()

    def transaction(self) -> Any:
        return self._c.transaction()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import psycopg
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=("measure", "apply", "relink", "relink-dry", "redo"))
    ap.add_argument("--view", choices=("emails", "teams_messages"), required=True)
    ap.add_argument("--dsn", default="dbname=plx")
    ap.add_argument("--where", default="true", help="SQL over the table (default every row)")
    ap.add_argument("--batch", type=int, default=20000, help="rows per transaction (redo)")
    ap.add_argument("--min-free-gb", type=float, default=40.0, help="redo stops below this")
    args = ap.parse_args(argv)
    if args.mode in ("relink", "relink-dry", "redo"):
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            w = _Write(conn)
            if args.mode == "redo":
                # the views refresh's own lock: a hand run and a refresh never overlap
                from operating.pg import business_views as bv
                if not conn.execute("SELECT pg_try_advisory_lock(%s)",
                                    (bv.REFRESH_LOCK,)).fetchone()[0]:
                    print(json.dumps({"error": "refresh_running"}))
                    return 2
            out = (redo_batched(w, args.view, batch=args.batch, min_free_gb=args.min_free_gb,
                                log=lambda m: print(m, file=sys.stderr, flush=True))
                   if args.mode == "redo" else
                   relink(w, args.view, dry=args.mode == "relink-dry"))
            print(json.dumps(out, default=str))
        return 0
    with psycopg.connect(args.dsn, autocommit=False) as conn:
        table = f"biz.{args.view}"
        before = conn.execute(
            f"SELECT count(*) FILTER (WHERE customers <> '{{}}'), count(*) FILTER (WHERE "
            f"suppliers <> '{{}}'), count(*) FILTER (WHERE customers <> '{{}}' OR suppliers "
            f"<> '{{}}'), count(*) FROM {table}").fetchone()
        stats = apply(conn, args.view, table, args.where, write=args.mode == "apply")
        after = conn.execute(
            f"SELECT count(*) FILTER (WHERE customers <> '{{}}'), count(*) FILTER (WHERE "
            f"suppliers <> '{{}}'), count(*) FILTER (WHERE customers <> '{{}}' OR suppliers "
            f"<> '{{}}'), count(*) FROM {table}").fetchone()
        if args.mode == "apply":
            conn.commit()
        stats["names_dropped"] = dict(sorted(stats["names_dropped"].items(),
                                             key=lambda kv: -kv[1])[:30])
        print(json.dumps({"view": args.view, "rows": before[3], "customers_before": before[0],
                          "suppliers_before": before[1], "either_before": before[2],
                          "customers_after": after[0], "suppliers_after": after[1],
                          "either_after": after[2], "stats": stats}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
