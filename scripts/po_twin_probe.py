"""Live check of firm Portal-BC purchase-order twins (``ontology.identity_scoring``
``corroborate_po_twins``, the ``twins`` build step): counts and codes only.

    python3 -m scripts.po_twin_probe --store STATE/projection/ontology.sqlite [--samples N]

Offline and with no client: it opens a built projection read-only and reports what the
build's ``twins`` step wrote (``firm_twins``: its ``corroborated`` ``same_record`` rows)
and, judging every current number-text lead again without writing (``replace=False``),
the outcome of each lead by code. Recorded:

* ``open_pos``: purchase orders per binding (``bc`` is BC's open purchase orders, the
  ``purchaseOrders`` entity holds only unposted ones); ``bc_pos_with_lead`` and
  ``bc_pos_with_firm_twin``: BC orders that have a number-text lead / a firm twin;
* ``leads``, ``firm_twins``, ``outcomes`` (per code), ``conflicts`` (``supplier_conflict``
  plus ``conflict``), ``judged_firm`` (the leads the rules would raise now);
* ``lead_shapes``: leads per pair of number series (``bc:<mask>|portal:<mask>``, digits
  ``9``, letters ``A``, a ``PO`` prefix kept) and ``supplier_verdicts`` (agreed,
  conflict, not_agreed, ambiguous, not_judged) the outcomes imply;
* ``samples``: up to ``--samples`` firm twins (else leads) as their outcome, the feature
  names their reason records and their evidence handles counted per scheme.

* ``mail``: how many open Portal purchase orders captured mail could corroborate firmly
  (:func:`mail_section`; measurement only, nothing is promoted).

Pass needs ``bc_pos_with_firm_twin`` >= 1: a firm count of 0 is never a pass. Must be 0:
``links_written`` (changes made through the read-only connection) and
``firm_mismatch`` (written firm twins differing from the leads judged firm now). Never a
value, key, identifier, citation or name.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from ontology import identity_scoring as ids
from ontology import mentions
from ontology.project import _default_normalizer
from ontology.schema import load_schema

CONFLICTS = ("supplier_conflict", "conflict")
# The supplier verdict each outcome code implies (codes judged before the supplier have
# none): the supplier agreed when the judge went on to the date and total.
SUPPLIER_VERDICT = {"supplier_conflict": "conflict", "supplier_not_agreed": "not_agreed",
                    "supplier_ambiguous": "ambiguous", "firm": "agreed",
                    "conflict": "agreed", "no_secondary_agreement": "agreed"}


def _mask(raw: str) -> str:
    """A PO number's series: a ``PO`` prefix kept, digits as ``9``, letters as ``A``."""
    head, rest = ("PO", raw[2:]) if raw[:2].upper() == "PO" else ("", raw)
    return head + re.sub(r"[A-Za-z]", "A", re.sub(r"\d", "9", rest))


def _shape(conn: sqlite3.Connection, object_id: str) -> str:
    """The series of one order's current raw PO numbers, joined (never a value)."""
    raws = {r[0] for r in conn.execute(
        "SELECT raw FROM identifiers WHERE object_id = ? AND identifier_kind = 'petra_po' "
        "AND superseded_at IS NULL", (object_id,)) if isinstance(r[0], str)}
    return "/".join(sorted({_mask(raw) for raw in raws})) or "none"


def _scheme(handle: Any) -> str:
    return handle.split(":", 1)[0] if isinstance(handle, str) and ":" in handle else "unknown"


def _sample(row: sqlite3.Row, outcome: str) -> dict[str, Any]:
    reason = json.loads(row["reason"] or "[]")
    handles = json.loads(row["evidence_handles"] or "[]")
    return {"outcome": outcome, "tier": row["tier"], "binding_kind": row["binding_kind"],
            "features": sorted({str(item.get("feature")) for item in reason
                                if isinstance(item, dict)}),
            "evidence_by_scheme": dict(sorted(Counter(map(_scheme, handles)).items()))}


def probe(store: Path, *, samples: int = 3) -> dict[str, Any]:
    started = time.monotonic()
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        before = conn.total_changes
        open_pos = dict(sorted(Counter(
            str(r[0]).split(".", 1)[0] for r in conn.execute(
                "SELECT binding_id FROM objects WHERE object_type = 'PurchaseOrder'")).items()))
        firm_rows = conn.execute(
            "SELECT * FROM links WHERE substr(link_id, 1, ?) = ? AND superseded_at IS NULL "
            "ORDER BY link_id", (len(ids.TWIN_PREFIX), ids.TWIN_PREFIX)).fetchall()
        leads = conn.execute(
            "SELECT * FROM links WHERE link_type = ? AND binding_kind = 'text_match' AND "
            "tier = 'lead' AND superseded_at IS NULL ORDER BY link_id",
            (ids.TWIN_LINK,)).fetchall()
        proposed: list[dict[str, Any]] = []
        judged = ids.corroborate_po_twins(
            conn, load_schema(), lambda rows: proposed.extend(rows) or len(rows),
            normalizer=_default_normalizer(), replace=False)
        outcomes: dict[str, str] = judged["outcomes"]
        firm_ids = {row["link_id"] for row in firm_rows}
        proposed_ids = {row["link_id"] for row in proposed}

        def bc_end(row: sqlite3.Row) -> set[str]:
            return {end for end in (row["from_object"], row["to_object"])
                    if bc_objects.get(end)}

        ends = {e for row in leads for e in (row["from_object"], row["to_object"])}
        bc_objects = {r[0]: str(r[1]).startswith("bc.") for r in conn.execute(
            "SELECT object_id, binding_id FROM objects WHERE object_type = 'PurchaseOrder'")
            if r[0] in ends}
        by_lead = {row["link_id"]: row for row in leads}
        firm_leads = {lead for lead in by_lead
                      if ids.TWIN_PREFIX + hashlib.sha256(lead.encode()).hexdigest()[:32]
                      in firm_ids}
        shown = [_sample(row, "firm") for row in firm_rows[:samples]]
        if not shown:
            shown = [_sample(by_lead[lead], code) for lead, code in
                     sorted(outcomes.items())[:samples]]
        counts = {code: n for code, n in sorted(judged["counts"].items()) if n}
        bc_side = {row["link_id"]: (row["from_object"] if bc_objects.get(row["from_object"])
                                    else row["to_object"]) for row in leads}
        shapes = Counter(
            f"bc:{_shape(conn, bc_side[lid])}|portal:{_shape(conn, other)}"
            for lid, row in by_lead.items()
            for other in [row["to_object"] if bc_side[lid] == row["from_object"]
                          else row["from_object"]])
        verdicts = Counter(SUPPLIER_VERDICT.get(code, "not_judged")
                           for code in outcomes.values())
        mail = mail_section(conn)
        return {
            "mail": mail,
            "open_pos": open_pos,
            "bc_pos": sum(n for binding, n in open_pos.items() if binding == "bc"),
            "bc_pos_with_lead": len({e for row in leads for e in bc_end(row)}),
            "bc_pos_with_firm_twin": len({e for lead in firm_leads
                                          for e in bc_end(by_lead[lead])}),
            "leads": len(leads), "leads_judged": len(outcomes),
            "firm_twins": len(firm_rows), "judged_firm": len(proposed_ids),
            "outcomes": counts, "lead_shapes": dict(sorted(shapes.items())),
            "supplier_verdicts": dict(sorted(verdicts.items())),
            "conflicts": sum(judged["counts"].get(code, 0) for code in CONFLICTS),
            "firm_mismatch": len(firm_ids ^ proposed_ids),
            "links_written": conn.total_changes - before,
            "samples": shown,
            "evidence_label": "projection-read",
            "seconds": round(time.monotonic() - started, 1)}
    finally:
        conn.close()


# ------------------------------------------------------------------ mail corroboration
#
# How many open Portal purchase orders captured mail could raise to a firm identity: an
# ``Email.mentions_purchase_order`` link whose mention names the order's own number
# (canonical or stored FM) exactly, with no veto, from an email whose supplier agrees with the order's supplier.
# Counts only (never a value, number, name, domain, address or handle).
#
# * A Portal order is one ``portal.app_purchase_order`` record; a mention of its FileMaker
#   twin (``portal.fm_generated_purchase_order``, joined by a verified ``same_record``
#   link) counts for it.
# * Status: the schema declares ``status`` free text, so the probe keeps its own closed
#   vocabulary of labels (upper case, letters only). A label outside it is ``other`` and a
#   missing or several-valued status is ``none`` / ``ambiguous``: none of them is open.
# * Exact: the mention's normalised number equals the order's canonical number (its
#   ``primary`` ``petra_po`` identifiers, its own ``po_number``) or the FM PO number the
#   order itself stores (``fm_po_number``), and its link answered it exactly (``mentions``
#   ``exact_candidate``: the mention's own kind and value, never a number-shape reading).
#   Anything else is ``alias_only``. The grammar keeps a bare number apart from a
#   ``PO``-prefixed one, so ``1234`` never equals ``PO1234``.
# * Vetoes (``ontology.mentions``): ``quoted_history``, ``label_kind_conflict``, and
#   ``several_targets`` when one email version names more than one purchase order and
#   not every two of them are twins (``mentions.twin_folds``, the rule
#   ``mentions.corroborate_links`` applies). A mention of an FM order counts for the
#   Portal order a verified ``same_record`` link joins it to.
# * ``corroborated``: open Portal orders the build's mention step raised (a current
#   ``corroborated`` mention link to the order or its FM twin), beside the probe's rule.
# * Supplier (one verified ``PurchaseOrder.ordered_from`` supplier; several: unknown): the
#   sender's domain at or under the supplier's website domain and distinctive
#   (``mentions.party_domain``) agrees; an external sender's distinctive domain outside it
#   conflicts; else the supplier's normalised name (``identity_scoring.normalize_name``,
#   at least ``NAME_MIN`` letters) in the subject or preview agrees. The projection holds
#   no recipient address, so no recipient domain is compared. An order agrees when one
#   exact mention agrees and none conflicts.
# * Confirmed date: an agreeing order a ``SupplierConfirmation.confirms`` link names whose
#   confirmation holds a ``delivery_date`` (a mention link carries no date).

MAIL_LINK = "Email.mentions_purchase_order"
CONFIRM_LINK = "SupplierConfirmation.confirms"
PORTAL_PO = "portal.app_purchase_order"
CLOSED_STATUSES = frozenset({"COMPLETE", "COMPLETED", "CANCEL", "CANCELLED", "CANCELED",
                             "CLOSED", "RECEIVED", "VOID"})
OPEN_STATUSES = frozenset({"DRAFT", "PENDING", "BORDER", "BACKORDER", "REVISED",
                           "GENERATED", "SUBMITTED", "PARTIAL", "OPEN", "SENT"})
MENTION_VETOES = ("quoted_history", "label_kind_conflict")
FM_FIELD = "fm_po_number"   # the FM PO number a Portal order stores (its own identifier)
NAME_MIN = 4


def status_label(values: set[Any]) -> str:
    """A Portal order's status bucket: a label of the closed vocabulary, else ``other``,
    ``none`` (no status) or ``ambiguous`` (several labels); never the raw value."""
    labels = {re.sub(r"[^A-Z]", "", str(v).upper()) for v in values if str(v).strip()}
    if not labels:
        return "none"
    if len(labels) > 1:
        return "ambiguous"
    label = next(iter(labels))
    return label if label in CLOSED_STATUSES | OPEN_STATUSES else "other"


def _current(conn: sqlite3.Connection, prop: str, object_type: str) -> dict[str, set[Any]]:
    out: dict[str, set[Any]] = {}
    for oid, value in conn.execute(
            "SELECT v.object_id, v.value FROM object_values v JOIN objects o USING (object_id) "
            "WHERE o.object_type = ? AND v.property = ? AND v.superseded_at IS NULL AND "
            "v.value IS NOT NULL", (object_type, prop)):
        out.setdefault(oid, set()).add(value)
    return out


def _email_verdict(sender: Any, sender_class: Any, text: str, supplier: dict[str, Any] | None,
                   held: Any) -> tuple[str, str | None]:
    """(``agreed`` | ``conflict`` | ``unknown``, basis) of one email against one supplier."""
    if supplier is None:
        return "unknown", None
    domain = mentions.mail_domain(sender)
    sites = supplier["sites"]
    if domain and sites:
        found = mentions.party_domain([("website", s) for s in sorted(sites)], [domain],
                                      held, supplier["id"])
        if found is not None and found["status"] == "match":
            return "agreed", "sender_domain"
        if found is None and sender_class != "internal" \
                and not mentions.shared_mail_domain(domain) \
                and not any(mentions.shared_mail_domain(s) for s in sites):
            return "conflict", "sender_domain"
    words = f" {ids.normalize_name(text)} "
    if any(len(name.replace(" ", "")) >= NAME_MIN and f" {name} " in words
           for name in supplier["names"]):
        return "agreed", "supplier_name"
    return "unknown", None


def mail_section(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ``mail`` counts (see the section comment). Reads only."""
    portal = {r[0] for r in conn.execute(
        "SELECT object_id FROM objects WHERE binding_id = ?", (PORTAL_PO,))}
    fold = {oid: oid for oid in portal}
    for a, b in conn.execute(
            "SELECT from_object, to_object FROM links WHERE link_type = ? AND "
            "binding_kind = 'same_record' AND tier = 'verified' AND superseded_at IS NULL",
            (ids.TWIN_LINK,)):
        if a in portal and b not in portal:
            fold.setdefault(b, a)
        elif b in portal and a not in portal:
            fold.setdefault(a, b)
    status = _current(conn, "status", "PurchaseOrder")
    buckets = Counter(status_label(status.get(oid, set())) for oid in portal)
    open_pos = {oid for oid in portal if status_label(status.get(oid, set())) in OPEN_STATUSES}
    canonical: dict[str, set[str]] = {}
    for oid, normalized in conn.execute(
            "SELECT object_id, normalized FROM identifiers WHERE identifier_kind = 'petra_po' "
            "AND role = 'primary' AND superseded_at IS NULL"):
        if oid in portal:
            canonical.setdefault(oid, set()).add(normalized)
    stored_fm: dict[str, set[str]] = {}
    for oid, normalized in conn.execute(
            "SELECT object_id, normalized FROM identifiers WHERE identifier_kind = 'petra_po' "
            "AND property = ? AND superseded_at IS NULL", (FM_FIELD,)):
        if oid in portal:
            stored_fm.setdefault(oid, set()).add(normalized)
    # suppliers: one verified ordered_from supplier per order, its website domains and names
    supplier_of: dict[str, set[str]] = {}
    for a, b in conn.execute(
            "SELECT from_object, to_object FROM links WHERE link_type = ? AND tier = 'verified' "
            "AND superseded_at IS NULL", (ids.TWIN_SUPPLIER_LINK,)):
        if a in portal:
            supplier_of.setdefault(a, set()).add(b)
    sites = _current(conn, "website", "Supplier")
    names = _current(conn, "name", "Supplier")
    suppliers = {sid: {"id": sid,
                       "sites": {d for d in map(mentions.website_domain, sites.get(sid, ())) if d},
                       "names": {n for n in map(ids.normalize_name, names.get(sid, ())) if n}}
                 for sid in sites.keys() | names.keys()}
    held = mentions._suffix_holders(mentions._domain_holders(conn, "website"))
    senders = _current(conn, "sender", "Email")
    texts: dict[str, str] = {}
    for prop in ("subject", "body_preview"):
        for oid, vals in _current(conn, prop, "Email").items():
            texts[oid] = (texts.get(oid, "") + " " + " ".join(sorted(map(str, vals)))).strip()
    # mentions, folded onto Portal orders
    links = conn.execute(
        "SELECT * FROM links WHERE link_type = ? AND tier <> 'contradicted' AND "
        "superseded_at IS NULL ORDER BY link_id", (MAIL_LINK,)).fetchall()
    # (email, version) -> every target it names -> its links (FM twins not yet folded)
    named: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for row in links:
        version = (json.loads(row["evidence_handles"] or "[]") or [None])[0]
        named.setdefault((row["from_object"], str(version)), {}).setdefault(
            row["to_object"], []).append(row)
    mentioned: dict[str, list[dict[str, Any]]] = {}
    unmapped = 0
    corroborated: set[str] = set()
    for row in links:
        target = fold.get(row["to_object"])
        if target is None:
            unmapped += 1
            continue
        reason = json.loads(row["reason"] or "[]")
        why_not = {f.get("feature") for f in json.loads(row["why_not"] or "[]")
                   if isinstance(f, dict)}
        mention = next((f for f in reason if isinstance(f, dict)
                        and f.get("feature") == "identifier_mention"), {})
        version = (json.loads(row["evidence_handles"] or "[]") or [None])[0]
        if row["tier"] == mentions.CORROBORATED:
            corroborated.add(target)
        normalized = mention.get("normalized")
        exact_on = ("canonical" if normalized in canonical.get(target, set()) else
                    "stored_fm" if normalized in stored_fm.get(target, set())
                    and mentions.exact_candidate(reason) else None)
        targets = named[(row["from_object"], str(version))]
        mentioned.setdefault(target, []).append({
            # one record unless every two targets are twins (``mentions.twin_folds``)
            "several": len(targets) > 1 and mentions.twin_folds(
                conn, row, targets, row["recorded_at"], row["superseded_at"]) is None,
            "email": row["from_object"], "version": str(version),
            "exact": exact_on is not None, "exact_on": exact_on,
            "shape": _mask(str(mention.get("normalized") or "")),
            "vetoes": why_not & set(MENTION_VETOES), "sender_class": mention.get("sender_class")})
    vetoes: Counter = Counter()
    verdicts: Counter = Counter()
    any_status: Counter = Counter()
    alias_open: Counter = Counter()
    agreed_by: Counter = Counter()
    exact, unvetoed, agreed_any = set(), set(), set()
    exact_on: dict[str, set[str]] = {"canonical": set(), "stored_fm": set()}
    for target, found in sorted(mentioned.items()):
        hits = [m for m in found if m["exact"]]
        is_exact = bool(hits)
        if is_exact:
            exact.add(target)
            for m in hits:
                exact_on[m["exact_on"]].add(target)
        clean = []
        for m in hits or found:   # alias-only orders are judged too, reported apart
            several = m["several"]
            if is_exact and target in open_pos:
                for veto in sorted(m["vetoes"]) + (["several_targets"] if several else []):
                    vetoes[veto] += 1
            if not m["vetoes"] and not several:
                clean.append(m)
        if not clean:
            if not is_exact and target in open_pos:
                alias_open["vetoed"] += 1
            continue
        sids = supplier_of.get(target, set())
        supplier = suppliers.get(next(iter(sids))) if len(sids) == 1 else None
        judged = []
        for m in clean:
            sent = senders.get(m["email"], set())
            judged.append(_email_verdict(next(iter(sent)) if len(sent) == 1 else None,
                                         m["sender_class"], texts.get(m["email"], ""),
                                         supplier, held))
        codes = {code for code, _basis in judged}
        verdict = "conflict" if "conflict" in codes else \
            "agreed" if "agreed" in codes else "unknown"
        if not is_exact:
            if target in open_pos:
                alias_open[verdict] += 1
            continue
        unvetoed.add(target)
        any_status[verdict] += 1
        if verdict == "agreed":
            agreed_any.add(target)
        if target in open_pos:
            verdicts[verdict] += 1
            if verdict == "agreed":
                agreed_by[next(b for c, b in judged if c == "agreed")] += 1
    agreed = agreed_any & open_pos
    dated = {oid for oid, vals in _current(conn, "delivery_date", "SupplierConfirmation").items()
             if any(str(v).strip() for v in vals)}
    confirmed = set()
    for a, b in conn.execute(
            "SELECT from_object, to_object FROM links WHERE link_type = ? AND "
            "tier <> 'contradicted' AND superseded_at IS NULL", (CONFIRM_LINK,)):
        if a in dated and fold.get(b) is not None:
            confirmed.add(fold[b])
    bc = {r[0] for r in conn.execute(
        "SELECT object_id FROM objects WHERE binding_id LIKE 'bc.%' AND "
        "object_type = 'PurchaseOrder'")}
    with_lead = set()
    for a, b in conn.execute(
            "SELECT from_object, to_object FROM links WHERE link_type = ? AND "
            "binding_kind = 'text_match' AND tier = 'lead' AND superseded_at IS NULL",
            (ids.TWIN_LINK,)):
        for own, other in ((a, b), (b, a)):
            if other in bc and fold.get(own) is not None:
                with_lead.add(fold[own])
    any_mention = set(mentioned)
    return {
        "portal_pos": len(portal),
        "status_buckets": dict(sorted(buckets.items())),
        "open_portal_pos": len(open_pos),
        "mention_links": len(links), "mention_links_not_portal": unmapped,
        "with_mention": len(any_mention), "with_mention_open": len(any_mention & open_pos),
        "exact": len(exact), "exact_open": len(exact & open_pos),
        "exact_canonical": len(exact_on["canonical"]),
        "exact_canonical_open": len(exact_on["canonical"] & open_pos),
        "exact_stored_fm": len(exact_on["stored_fm"]),
        "exact_stored_fm_open": len(exact_on["stored_fm"] & open_pos),
        "alias_only": len(any_mention - exact),
        "alias_only_open": len((any_mention - exact) & open_pos),
        "alias_shapes": dict(sorted(Counter(
            f"mention:{'/'.join(sorted({m['shape'] for m in mentioned[t]}))}|canonical:"
            f"{'/'.join(sorted({_mask(n) for n in canonical.get(t, ())})) or 'none'}"
            for t in any_mention - exact).items())),
        "without_canonical_number": len(portal - canonical.keys()),
        "mention_vetoes_open": dict(sorted(vetoes.items())),
        "alias_only_open_supplier": {code: alias_open.get(code, 0)
                                     for code in ("agreed", "conflict", "unknown", "vetoed")},
        "exact_unvetoed_open": len(unvetoed & open_pos),
        "supplier": {code: verdicts.get(code, 0) for code in ("agreed", "conflict", "unknown")},
        "supplier_any_status": {code: any_status.get(code, 0)
                                for code in ("agreed", "conflict", "unknown")},
        "agreed_by": dict(sorted(agreed_by.items())),
        "confirmed_date": len(agreed & confirmed),
        "confirmed_date_any": len(confirmed),
        "firm": len(agreed),
        "firm_with_bc_lead": len(agreed & with_lead),
        "firm_without_bc_lead": len(agreed - with_lead),
        "corroborated_open": len(corroborated & open_pos),
        "corroborated_open_with_bc_lead": len(corroborated & open_pos & with_lead),
        "corroborated_open_without_bc_lead": len((corroborated & open_pos) - with_lead),
        "corroborated_not_firm_open": len((corroborated & open_pos) - agreed),
        "firm_not_corroborated_open": len(agreed - corroborated),
        "promoted": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--samples", type=int, default=3)
    args = parser.parse_args(argv)
    if not args.store.is_file():
        print(json.dumps({"sources_blocked": ["projection_missing"]}))
        return 0
    print(json.dumps(probe(args.store, samples=args.samples), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
