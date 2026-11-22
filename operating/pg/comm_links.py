"""Comms (mail, Teams) tied to the customers and suppliers they are with or about, from
identities the data already holds, each link with how it was made and a confidence.

Rules (``message_parties.infer`` unions them with its domain and name rules):

* ``named_record`` (``high`` when the message names records of one party, ``medium`` of
  two or three; more says nothing of one): a record the message names (``records``: an
  order, an invoice, a purchase order the identifier search resolved) belongs to a party
  (``biz.sales_orders.customer_handle`` ...).
* ``address`` (``high``): a participant's exact address is held by a party record: the
  customer / supplier's own mail fields (any system) or a BC contact related to it
  (``ContactRelation``). An address two differently named parties hold links nothing, one
  of our own addresses never links. This is the only rule a public mail domain (gmail ...)
  gets; ``message_parties.PartyIndex`` reads the same addresses for its domain rule, which
  never takes a shared provider's domain.
* thread continuity is NOT a rule: a hand check of 6 mails linked by "the rest of the thread
  names one party" found 2 right (conversation ids group unrelated subjects), so it
  links nothing.
* an address on a domain several differently named parties hold links nothing.

``biz.comm_party_links`` keeps one row per derived link (view, object id, party, via,
confidence); the party arrays on the message row stay the one place a reader's door applies,
the table carries the message row's access_class, col_class and ai_ok so the door applies to a link as to its message."""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Iterable, Mapping

MAX_RECORD_PARTIES = 3
MAX_ADDRESSES = 8
TTL = 6 * 3600
LINKS = "biz.comm_party_links"
_MAIL_KEYS = {"Customer": ("email", "email_2", "email_3", "bc_email"),
              "Supplier": ("email", "bc_email", "rep_email", "csr_email")}
_RECORD_PARTY = (("sales_orders", "customer"), ("sales_invoices", "customer"),
                 ("purchase_orders", "supplier"), ("supplier_invoices", "supplier"))
_ADDR = r"^[^@ ,;<>]+@[^@ ,;<>]+\.[A-Za-z]{2,}$"
_CACHE: dict[str, Any] = {"at": 0.0, "ident": {}}


def identities(conn: Any) -> dict[str, set]:
    """Address -> {(kind, handle, name)} of the parties holding it: the party record's mail
    fields (the record the party row stands for, any system) and the BC contacts related to
    a customer or vendor. Cached ``TTL`` seconds per process."""
    # a first build has no records table yet: no identity is claimed (nothing cached)
    if conn.execute("SELECT to_regclass('biz.records') IS NULL").fetchone()[0]:
        return {}
    now = time.monotonic()
    if _CACHE["ident"] and now - _CACHE["at"] < TTL:
        return _CACHE["ident"]
    out: dict[str, set] = defaultdict(set)
    for kind, table in (("customer", "customers"), ("supplier", "suppliers")):
        rtype = kind.capitalize()
        keys = list(_MAIL_KEYS[rtype])
        for addr, handle, name in conn.execute(
                "SELECT lower(btrim(e.v)), p.handle, p.name FROM biz." + table + " p "
                "JOIN biz.records r ON r.handle = p.handle AND r.type = %s, "
                "LATERAL (SELECT value AS v FROM jsonb_each_text(r.fields) f(key, value) "
                "WHERE f.key = ANY(%s)) e WHERE e.v ~ %s", (rtype, keys, _ADDR)):
            out[addr].add((kind, handle, name))
        # a BC contact's address, held for the customer / vendor it is related to
        for addr, handle, name in conn.execute(
                "SELECT lower(btrim(c.fields->>'email')), p.handle, p.name "
                "FROM biz.records rel JOIN biz.records c ON c.type = 'Contact' AND "
                "c.handle LIKE '%%/contacts/' || (rel.fields->>'contact_id') || '@%%' "
                "JOIN biz." + table + " p ON p.handle LIKE '%%/' || (rel.fields->>'related_id') "
                "|| '@%%' WHERE rel.type = 'ContactRelation' AND rel.fields->>'related_type' = %s "
                "AND c.fields->>'email' ~ %s", (rtype if kind == "customer" else "Vendor", _ADDR)):
            out[addr].add((kind, handle, name))
    _CACHE.update(at=now, ident=dict(out))
    return _CACHE["ident"]


def _own_addresses(conn: Any) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT lower(email) FROM biz.staff WHERE email LIKE '%@%'")}


def _norm(name: str | None) -> str:
    from ontology.identity_scoring import normalize_name
    return normalize_name(name) or ""


def address_parties(conn: Any) -> dict[str, set]:
    """Address -> {(kind, handle)}, an address two differently named parties hold dropped,
    our own addresses dropped."""
    from ontology import mentions
    own = _own_addresses(conn)
    ident = identities(conn)
    # a domain several differently named parties hold (an agency, a parent company, a
    # mistyped contact on another customer's record) says nothing of one of them
    by_domain: dict[str, set] = defaultdict(set)
    for addr, held in ident.items():
        reg = mentions.registrable_domain(addr.rsplit("@", 1)[-1])
        if not mentions.shared_mail_domain(reg):
            by_domain[reg].update(_norm(n) or h for _, h, n in held)
    out = {}
    for addr, held in ident.items():
        reg = mentions.registrable_domain(addr.rsplit("@", 1)[-1])
        if addr in own or len({_norm(n) or h for _, h, n in held}) > 1 \
                or len(by_domain.get(reg, ())) > 1:
            continue
        out[addr] = {(k, h) for k, h, _ in held}
    return out


def named_records(conn: Any, table: str, where: str) -> dict[str, dict[tuple, str]]:
    """oid -> {(kind, handle): confidence} of the parties of the records a row names."""
    per: dict[str, set] = defaultdict(set)
    for tbl, kind in _RECORD_PARTY:
        col = f"{kind}_handle"
        # a table not built with its handles yet (a first build) names no party
        if conn.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = "
                        "'biz' AND table_name = %s AND column_name = ANY(%s)",
                        (tbl, ["handle", col])).fetchone()[0] < 2:
            continue
        for oid, party in conn.execute(
                f"SELECT DISTINCT t.object_ids[1], o.{col} FROM {table} t CROSS JOIN LATERAL "
                f"unnest(t.records) r(h) JOIN biz.{tbl} o ON o.handle = r.h WHERE {where} "
                f"AND t.records <> '{{}}' AND o.{col} IS NOT NULL"):
            per[oid].add((kind, party))
    return {oid: {p: "high" if len(ps) == 1 else "medium" for p in ps}
            for oid, ps in per.items() if len(ps) <= MAX_RECORD_PARTIES}


def addresses(conn: Any, table: str, where: str) -> dict[str, set]:
    """oid -> {(kind, handle)}: mail whose participants (at most ``MAX_ADDRESSES``) include
    an address a party holds."""
    held = address_parties(conn)
    if not held:
        return {}
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS cl_addr (addr text PRIMARY KEY)")
    conn.execute("TRUNCATE cl_addr")
    with conn.cursor().copy("COPY cl_addr (addr) FROM STDIN") as cp:
        for a in held:
            cp.write_row((a,))
    out: dict[str, set] = defaultdict(set)
    for oid, addrs in conn.execute(
            f"SELECT t.object_ids[1], t.addresses FROM {table} t WHERE {where} AND "
            f"cardinality(t.addresses) BETWEEN 1 AND {int(MAX_ADDRESSES)} AND "
            "t.addresses && ARRAY(SELECT addr FROM cl_addr)"):
        for a in addrs:
            out[oid].update(held.get(a.lower(), ()))
    return {o: p for o, p in out.items() if len(p) <= MAX_RECORD_PARTIES}


def infer(conn: Any, view: str, table: str, where: str, got: dict[str, set],
          basis: dict[tuple, tuple]) -> dict[str, int]:
    """Add this module's links for the rows of ``table`` (where ``where``) into ``got`` (oid
    -> {(kind, handle)}) and ``basis`` ((oid, kind, handle) -> (via, confidence))."""
    counts = {}

    def add(via: str, found: Mapping[str, Any]) -> None:
        n = 0
        for oid, ps in found.items():
            for p in ps:
                conf = found[oid][p] if isinstance(found[oid], dict) else (
                    "high" if via == "address" else "medium")
                got.setdefault(oid, set()).add(p)
                basis.setdefault((oid, *p), (via, conf))
            n += 1
        counts[via] = n
    add("named_record", named_records(conn, table, where))
    if view == "emails":
        add("address", addresses(conn, table, where))
    return counts


def lag_gb(conn: Any) -> float:
    """How far the standby is behind, in GB: the most WAL a physical slot holds back or a
    streaming standby has not replayed (0 with neither). A standby slot named in
    ``PLX_WAL_STANDBY_SLOTS`` that is gone counts as behind past any pause line: a dropped
    slot is a standby lost, not one caught up (``operating.pg.wal_budget``)."""
    from operating.pg import wal_budget
    if wal_budget.expected_slots() and wal_budget.missing_slots(conn, wal_budget.expected_slots()):
        return float("inf")
    r = conn.execute(
        "SELECT greatest((SELECT max(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) FROM "
        "pg_replication_slots WHERE slot_type = 'physical' AND restart_lsn IS NOT NULL AND "
        "wal_status IS DISTINCT FROM 'lost'), (SELECT max(pg_wal_lsn_diff(pg_current_wal_lsn(), "
        "replay_lsn)) FROM pg_stat_replication))").fetchone()[0]
    return float(r or 0) / 1e9


def guard(conn: Any, *, min_free_gb: float = 75.0, pause_gb: float = 8.0, resume_gb: float = 2.0,
          path: str | None = None, sleep: Any = time.sleep, log: Any = None) -> None:
    """Before a write chunk: stop (RuntimeError) under ``min_free_gb`` free on the database's
    own disk (``path``, default its data directory: a build copy on a Spark has no /data);
    wait while the standby lags more than ``pause_gb``, until it is under ``resume_gb``."""
    import shutil
    if path is None:
        from operating.pg import business_comms
        path = business_comms.disk_path(conn)
    free = shutil.disk_usage(path).free / 1e9
    if free < min_free_gb:
        raise RuntimeError(f"disk {free:.1f} GB free < {min_free_gb}")
    if lag_gb(conn) > pause_gb:
        while lag_gb(conn) > resume_gb:
            (log or (lambda *_: None))("standby lag, waiting")
            sleep(30)


def write_links(conn: Any, view: str, basis: Mapping[tuple, tuple], *, chunk: int = 50000,
                scope: Iterable[str] | None = None, **guard_kw: Any) -> int:
    """Keep the derived links in the narrow table ``biz.comm_party_links``; the message
    rows are never updated. Each row carries the door of BOTH ends, a reader must hold both:
    the message row's own (``audience_keys``, ``rec_marks``, ``col_marks``, ``ai_ok``: the
    mail and Teams tables are not door v2 yet, so these are what their guarded views read)
    and the party row's (``access_class``, ``col_class``, ``ai_ok``).

    The links are staged in an UNLOGGED table (no WAL), chunk by chunk under ``guard``, then
    one transaction makes the table agree with the stage: ``scope`` None (a full run) removes
    every row of ``view`` that is not derived now; otherwise only the rows of the objects in
    ``scope`` (a link of a message no longer derived goes, and the door columns of the
    rest are read again). ``conn`` autocommit."""
    t = LINKS
    # the table is migration 0053's (declared schema); missing: stop before any work
    if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is None:
        raise RuntimeError(f"{t} is missing: apply migration 0053_comm_party_links "
                           "(python3 -m operating.pg migrate)")
    stage = "biz.comm_party_links_stage"
    conn.execute(f"DROP TABLE IF EXISTS {stage}")
    conn.execute(f"CREATE UNLOGGED TABLE {stage} (object_id text, party_kind text, "
                 "party_handle text, via text, confidence text)")
    try:
        items = sorted(basis.items())
        for lo in range(0, len(items), chunk):
            guard(conn, **guard_kw)
            with conn.cursor().copy(f"COPY {stage} FROM STDIN") as cp:
                for (oid, kind, handle), (via, conf) in items[lo:lo + chunk]:
                    cp.write_row((oid, kind, handle, via, conf))
        conn.execute(f"CREATE INDEX ON {stage} (object_id, party_kind, party_handle)")
        guard(conn, **guard_kw)
        src = f"biz.{view}"
        scoped = None if scope is None else sorted(set(scope))
        with conn.transaction():
            conn.execute(
                f"DELETE FROM {t} l WHERE l.view = %s AND (%s::text[] IS NULL OR l.object_id = "
                f"ANY(%s)) AND NOT EXISTS (SELECT 1 FROM {stage} w WHERE w.object_id = "
                "l.object_id AND w.party_kind = l.party_kind AND w.party_handle = "
                "l.party_handle)", (view, scoped, scoped))
            n = 0
            for kind, ptab in (("customer", "customers"), ("supplier", "suppliers")):
                n += conn.execute(
                    f"INSERT INTO {t} SELECT %s, w.object_id, w.party_kind, w.party_handle, "
                    "w.via, w.confidence, m.handle, m.audience_keys, m.rec_marks, m.col_marks, "
                    "m.ai_ok, p.access_class, p.col_class, p.ai_ok FROM " + stage + " w JOIN "
                    f"{src} m ON m.object_ids[1] = w.object_id JOIN biz.{ptab} p ON p.handle = "
                    "w.party_handle WHERE w.party_kind = %s ON CONFLICT (view, object_id, "
                    "party_kind, party_handle) DO UPDATE SET via = EXCLUDED.via, confidence = "
                    "EXCLUDED.confidence, handle = EXCLUDED.handle, audience_keys = "
                    "EXCLUDED.audience_keys, rec_marks = EXCLUDED.rec_marks, col_marks = "
                    "EXCLUDED.col_marks, ai_ok = EXCLUDED.ai_ok, party_access_class = "
                    "EXCLUDED.party_access_class, party_col_class = EXCLUDED.party_col_class, "
                    "party_ai_ok = EXCLUDED.party_ai_ok", (view, kind)).rowcount
        return n
    finally:
        conn.execute(f"DROP TABLE IF EXISTS {stage}")
