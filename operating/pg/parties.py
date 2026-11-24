"""Who a customer or supplier is across systems (contract C7, ``biz.parties``, migration
0066; master plan specs M5 / D1).

A party is one real company. Its members are rows of ``biz.customers`` / ``biz.suppliers``
(already one row per record and its settled copies). Rows are joined into one party ONLY on
evidence, strongest first, and each membership stores its basis and evidence:

1. ``code``: the two rows carry the same system code (``codes``: ``sage:CALL10`` in both);
2. ``verified``: a settled identity link (tier verified / reviewed / corroborated) joins an
   object of one row to an object of the other (``ontology.link_rows``, ``same_record_as``);
3. ``rule``: the same folded name AND the same email address or phone number, or the same
   folded name and the same mail domain where that domain is used by no row of another name
   (a domain many companies share, like a free mail service, says nothing: read from the
   data, no list); never two rows a system keeps as two accounts (different codes in the
   same system and none shared): those stay apart, an undecided pair.

Everything else that looks alike is a ``biz.party_candidates`` row, ``undecided``, with its
evidence and never a merge: lead-tier identity links between rows of two parties, and two
rows with the same folded name and no other shared fact. A later adjudication (a model or a
person, with the evidence) moves a candidate to ``merged`` / ``rejected``; tier ``model`` is
applied only after a sampled check (the plan's 48 of 50).

``chain_id``: the customer's chain (the national account Sage states, ``customers.chain_code``,
with its basis in ``chain_basis``), one id per chain code; a party whose members name two
chains carries none (said in the counts).

``party_id`` is stable across builds: a digest of the party's smallest member handle. A
party never widens a door: its name comes from one member row and carries that row's
classes; totals are read per class from the members' own rows.

``python3 -m operating.pg.parties build [--dsn]`` rebuilds the three tables in one
transaction and prints the counts per tier and the candidates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from operating.label_changes import SETTLED  # one definition of the settled link tiers

KINDS = {"customer": ("customers", "Customer.same_record_as"),
         "supplier": ("suppliers", "Supplier.same_record_as")}
_FOLD = re.compile(r"[^a-z0-9]")


def fold(name: Any) -> str:
    return _FOLD.sub("", str(name or "").lower())


def party_id(handles: Iterable[str]) -> int:
    return int(hashlib.sha256(min(handles).encode("utf-8")).hexdigest()[:15], 16)


class _Sets:
    def __init__(self, items: Iterable[str]):
        self.up = {i: i for i in items}
        self.why: dict[str, tuple[str, dict[str, Any]]] = {}

    def find(self, x: str) -> str:
        while self.up[x] != x:
            self.up[x] = self.up[self.up[x]]
            x = self.up[x]
        return x

    def join(self, a: str, b: str, tier: str, evidence: Mapping[str, Any]) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.up[max(ra, rb)] = min(ra, rb)
        for h in (a, b):   # the first (strongest) reason a row joined is kept
            self.why.setdefault(h, (tier, dict(evidence)))
        return True


def _codes(raw: Any) -> list[str]:
    return [c.strip() for c in str(raw or "").split(",") if ":" in c and c.strip()]


def _digits(raw: Any) -> str:
    return re.sub(r"\D", "", str(raw or ""))


def _same_system_other_code(a: Mapping[str, Any], b: Mapping[str, Any]) -> str:
    """The system in which ``a`` and ``b`` hold different codes and no shared one ("" when
    none): two accounts that system keeps apart are never joined by a rule (spec M5)."""
    def by_system(r: Mapping[str, Any]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for c in _codes(r.get("codes")):
            system, _, code = c.partition(":")
            out[system.lower()].add(code.lower())
        return out
    sa, sb = by_system(a), by_system(b)
    for system in sorted(set(sa) & set(sb)):
        if not sa[system] & sb[system]:
            return system
    return ""


def decide(rows: Sequence[Mapping[str, Any]], links: Iterable[tuple[str, str, str]],
           decided: Iterable[tuple[str, str, str, str]] = ()
           ) -> tuple[_Sets, list[tuple[str, str, dict[str, Any]]]]:
    """The parties of ``rows`` (dicts: handle, name, codes, email, phone) given identity
    ``links`` between row handles (a, b, tier) and the candidate pairs already decided
    (a, b, state, decided_by): a merged one joins (tier ``model`` or ``person``), a rejected
    one is never offered again. Returns (the joined sets, the undecided pairs)."""
    sets = _Sets(r["handle"] for r in rows)
    by = by_row = {r["handle"]: r for r in rows}
    # 1. a shared system code
    holders: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        for c in _codes(r.get("codes")):
            holders[c.lower()].append(r["handle"])
    for code, hs in sorted(holders.items()):
        for h in hs[1:]:
            sets.join(hs[0], h, "code", {"code": code})
    # 2. settled identity links
    leads = []
    for a, b, tier in links:
        if a not in by or b not in by or a == b:
            continue
        if tier in SETTLED:
            sets.join(a, b, "verified", {"link_tier": tier})
        else:
            leads.append((a, b, tier))
    # 3. same folded name and a shared address, phone, or a domain only that name uses
    names: dict[str, set[str]] = defaultdict(set)   # domain -> folded names using it
    for r in rows:
        d = str(r.get("email") or "").lower().rpartition("@")[2]
        if d and fold(r.get("name")):
            names[d].add(fold(r.get("name")))
    groups: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        if fold(r.get("name")):
            groups[fold(r.get("name"))].append(r["handle"])
    pairs: list[tuple[str, str, dict[str, Any]]] = []
    for key, hs in sorted(groups.items()):
        for i, a in enumerate(hs):
            for b in hs[i + 1:]:
                ra, rb = by[a], by[b]
                clash = _same_system_other_code(ra, rb)
                if clash:   # two accounts of one system: never one party by a rule
                    pairs.append((a, b, {"name": key, "why": f"two {clash} accounts"}))
                    continue
                ea, eb = str(ra.get("email") or "").lower(), str(rb.get("email") or "").lower()
                pa, pb = _digits(ra.get("phone")), _digits(rb.get("phone"))
                da, db = ea.rpartition("@")[2], eb.rpartition("@")[2]
                if ea and ea == eb:
                    sets.join(a, b, "rule", {"name": key, "email": ea})
                elif pa and pa == pb:
                    sets.join(a, b, "rule", {"name": key, "phone": pa})
                elif da and da == db and names.get(da) == {key}:
                    sets.join(a, b, "rule", {"name": key, "domain": da})
                else:
                    pairs.append((a, b, {"name": key, "why": "same name, nothing else shared"}))
    rejected = set()
    for a, b, state, who in decided:   # a decision (the adjudication, a person) outlives a rebuild
        if a not in by_row or b not in by_row:
            continue
        if state == "merged":
            sets.join(a, b, "person" if who == "person" else "model", {"decided_by": who})
        elif state == "rejected":
            rejected.add((a, b))
    undecided = []
    for a, b, ev in pairs + [(a, b, {"lead_link": t}) for a, b, t in leads]:
        if sets.find(a) != sets.find(b) and tuple(sorted((a, b))) not in rejected:
            lo, hi = sorted((a, b))
            undecided.append((lo, hi, ev))
    return sets, undecided


def _rows(conn: Any, view: str) -> list[dict[str, Any]]:
    chain = "chain_code" if view == "customers" else "NULL::text AS chain_code"
    cur = conn.execute(f"SELECT handle, name, codes, email, phone, access_class, col_class, "
                       f"{chain} FROM biz.{view} WHERE handle IS NOT NULL")
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _links(conn: Any, view: str, link_type: str) -> list[tuple[str, str, str]]:
    """Identity links between rows of ``view``: each end mapped to the row whose lineage
    holds that object (``biz.lineage``)."""
    return [tuple(r) for r in conn.execute(
        "SELECT la.handle, lb.handle, l.tier FROM ontology.link_rows l "
        "JOIN biz.lineage la ON la.view = %s AND l.from_object = ANY(la.object_ids) "
        "JOIN biz.lineage lb ON lb.view = %s AND l.to_object = ANY(lb.object_ids) "
        "WHERE l.link_type = %s AND l.superseded_at IS NULL AND la.handle <> lb.handle",
        (view, view, link_type)).fetchall()]


def build(dsn: str, write: bool = True) -> dict[str, Any]:
    """Rebuild ``biz.parties`` / ``party_members`` / ``party_candidates`` from the views in
    one snapshot, written in one transaction; candidates already decided keep their state."""
    import psycopg
    out: dict[str, Any] = {}
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        have = conn.execute("SELECT to_regclass('biz.party_candidates')").fetchone()[0]
        if have is None and write:
            raise RuntimeError("biz.party_candidates missing: apply migration 0066")
        decided = {} if have is None else {(a, b): (s, by, ev) for a, b, s, by, ev in
                                            conn.execute(
            "SELECT handle_a, handle_b, state, decided_by, evidence FROM biz.party_candidates "
            "WHERE state <> 'undecided'").fetchall()}
        # what the adjudication read of a pair that is still undecided is kept (never read twice)
        read = {} if have is None else {(a, b): ev["model"] for a, b, ev in conn.execute(
            "SELECT handle_a, handle_b, evidence FROM biz.party_candidates WHERE state = "
            "'undecided' AND evidence ? 'model'").fetchall()}
        parties, members, candidates = [], [], []
        for kind, (view, link_type) in KINDS.items():
            rows = _rows(conn, view)
            sets, undecided = decide(rows, _links(conn, view, link_type),
                                     [(a, b, st, by) for (a, b), (st, by, _) in decided.items()])
            by = {r["handle"]: r for r in rows}
            groups: dict[str, list[str]] = defaultdict(list)
            for h in by:
                groups[sets.find(h)].append(h)
            tiers: dict[str, int] = defaultdict(int)
            for hs in groups.values():
                pid = party_id(hs)
                named = min(hs, key=lambda h: (fold(by[h].get("name")) == "", h))
                chains = sorted({by[h].get("chain_code") for h in hs if by[h].get("chain_code")})
                chain_id = party_id([f"chain:{chains[0]}"]) if len(chains) == 1 else None
                parties.append((pid, kind, by[named].get("name"), chain_id,
                                by[named].get("access_class"), by[named].get("col_class")))
                for h in hs:
                    tier, ev = sets.why.get(h, ("code", {"alone": True}))
                    if len(hs) == 1:
                        tier, ev = "code", {"alone": True}
                    system = (_codes(by[h].get("codes")) or ["none:"])[0].partition(":")[0]
                    members.append((h, pid, system, "own row" if len(hs) == 1 else "joined",
                                    tier, json.dumps(ev)))
                    tiers[tier if len(hs) > 1 else "alone"] += 1
            seen = set(by)
            for a, b, ev in undecided:
                if (a, b) in read:
                    ev = {**ev, "model": read[(a, b)]}
                candidates.append((a, b, json.dumps(ev), "undecided", None))
            for (a, b), (state, who, ev) in decided.items():   # decisions outlive a rebuild
                if a in seen and b in seen:
                    candidates.append((a, b, json.dumps(ev), state, who))
            out[kind] = {"rows": len(rows), "parties": len(groups),
                         "members_by_tier": dict(tiers),
                         "undecided": len(undecided),
                         "decided_kept": sum(1 for (a, b) in decided if a in seen and b in seen)}
        if not write:   # counted only (a read-only standby works)
            out["_parties"], out["_members"], out["_candidates"] = parties, members, candidates
            return out
        with conn.transaction():
            conn.execute("DELETE FROM biz.party_members")
            conn.execute("DELETE FROM biz.parties")
            conn.execute("DELETE FROM biz.party_candidates")
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO biz.parties VALUES (%s, %s, %s, %s, %s, %s)", parties)
                cur.executemany("INSERT INTO biz.party_members (handle, party_id, system, basis, "
                                "tier, evidence) VALUES (%s, %s, %s, %s, %s, %s)", members)
                cur.executemany("INSERT INTO biz.party_candidates VALUES (%s, %s, %s, %s, %s) "
                                "ON CONFLICT DO NOTHING", candidates)
    return out


# The views that carry the company a row's customer belongs to (``declare_columns``): the key
# the row's customer is under, and the columns it gets. ``customers`` is its own key.
PARTY_VIEWS = {"customers": ("handle", ("party_id", "chain_id")),
               "customer_balances": ("customer_handle", ("party_id", "customer_party", "chain_id")),
               "receivable_documents": ("customer_handle", ("party_id", "customer_party", "chain_id")),
               "sales_orders": ("customer_handle", ("party_id", "customer_party", "chain_id")),
               "sales_invoices": ("customer_handle", ("party_id", "customer_party", "chain_id")),
               "sales_invoice_lines": ("customer_handle", ("party_id", "customer_party", "chain_id"))}
COLUMN_DOC = {
    "party_id": ("text", "the company this row's customer is: customers joined on evidence "
                 "only (a shared system code, a settled identity link, the same name with the "
                 "same phone, email or an own mail domain); two accounts one system keeps apart "
                 "stay two; NULL until the parties are built (join parties on party_id)"),
    "customer_party": ("text", "the company's name (the customer's own name when the company's "
                       "name is held on a row of another access class): group by this to "
                       "count one company once however many accounts it has"),
    "chain_id": ("text", "the national-account chain the company belongs to (Sage's national "
                 "account), one id per chain; NULL when it has none or its accounts name two"),
}


def declare_columns(views: Mapping[str, Any]) -> None:
    """Append the party columns to the views of ``PARTY_VIEWS`` (once; the declared shape
    of the view, so its print moves with the columns)."""
    from operating.pg.business_views import SYSTEMS_COL, Col
    for name, (_, cols) in PARTY_VIEWS.items():
        v = views.get(name)
        if v is None or v.col(cols[0]) is not None:
            continue
        at = next((i for i, c in enumerate(v.cols) if c.name == SYSTEMS_COL), len(v.cols))
        v.cols[at:at] = [Col(c, *COLUMN_DOC[c]) for c in cols]
        v.build = _carrying(name, v.build)


def _carrying(view: str, build: Any) -> Any:
    """``build`` with the party columns set on each made row. The parties are read once per
    build over its own connection (not through the build's code: the views' fingerprints and
    the access code stay as they are)."""
    def build_with_parties(rd: Any, as_of: Any) -> Any:
        import psycopg
        with psycopg.connect(rd.conn.dsn) as c:
            found = lookup(c)
        for row in build(rd, as_of):
            carry(row, view, found)
            yield row
    build_with_parties.__name__ = getattr(build, "__name__", view)
    return build_with_parties


def stale_with_parties(meta: Any, stale: list[str]) -> list[str]:
    """``stale`` plus the views that carry the parties and were built under other parties
    (their ``notes.parties`` differs from the built parties' digest); nothing added while no
    party is built."""
    found = lookup(meta)
    if not found:
        return stale
    now = digest(found)
    built = {r[0]: r[1] for r in meta.execute("SELECT view, notes FROM biz.state")}
    more = []
    for name in PARTY_VIEWS:
        if name in built and name not in stale:
            notes = built[name] if isinstance(built[name], dict) else json.loads(built[name] or "{}")
            if notes.get("parties") != now:
                more.append(name)
    return stale + more


def record(meta: Any, views: Iterable[str], at_digest: str | None) -> None:
    """Note on the made views of ``PARTY_VIEWS`` which parties they were built under."""
    names = [n for n in views if n in PARTY_VIEWS]
    if names:
        meta.execute("UPDATE biz.state SET notes = notes::jsonb || jsonb_build_object("
                     "'parties', %s::text) WHERE view = ANY(%s)", (at_digest, names))


def lookup(conn: Any) -> dict[str, tuple[str, str, str | None]]:
    """customer row handle -> (party_id, party name, chain_id) from the built parties; {}
    when 0066 is not applied or nothing is built (the columns stay NULL: said by the caller).
    The party's name is the party row's only when that row has the member's own access
    class, else the member's own name (a party never widens a door)."""
    if conn.execute("SELECT to_regclass('biz.party_members')").fetchone()[0] is None:
        return {}
    return {h: (pid, name, chain) for h, pid, name, chain in conn.execute(
        "SELECT m.handle, m.party_id::text, CASE WHEN p.access_class IS NOT DISTINCT FROM "
        "c.access_class AND p.col_class IS NOT DISTINCT FROM c.col_class THEN p.name "
        "ELSE c.name END, p.chain_id::text FROM biz.party_members m JOIN biz.parties p "
        "USING (party_id) JOIN biz.customers c ON c.handle = m.handle "
        "WHERE p.kind = 'customer'").fetchall()}


def digest(found: Mapping[str, tuple[str, str, str | None]]) -> str:
    """What the views carry of the parties: a hash of ``lookup``'s (handle, party, name,
    chain) in order. A view built under another digest is stale (``business_views.refresh``)."""
    import hashlib
    h = hashlib.sha256()
    for handle, (pid, name, chain) in sorted(found.items()):
        h.update(f"{handle}|{pid}|{name}|{chain}\n".encode("utf-8"))
    return h.hexdigest()[:16]


def carry(row: Any, view: str, found: Mapping[str, tuple[str, str, str | None]]) -> None:
    """Set the party columns of a made row of ``view`` from ``found`` (``lookup``)."""
    key, cols = PARTY_VIEWS[view]
    handle = row.handle if key == "handle" else row.values.get(key)
    hit = found.get(handle) if handle else None
    got = dict(zip(("party_id", "customer_party", "chain_id"), hit or (None, None, None)))
    for c in cols:
        row.values[c] = got[c]


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="parties")
    ap.add_argument("command", choices=("build", "count"))
    ap.add_argument("--dsn")
    args = ap.parse_args(argv)
    from operating.pg import config
    got = build(args.dsn or config.dsn(), write=args.command == "build")
    print(json.dumps({k: v for k, v in got.items() if not k.startswith("_")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
