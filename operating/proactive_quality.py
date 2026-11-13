"""Data-quality alerts: duplicate customers and suppliers, key fields missing on active
records, twins whose systems disagree, and sources not captured lately. Run by
``operating.proactive_engine`` beside its other rules (each in its own window,
``DETECTOR_SECONDS``), set-based over the whole store with index reads only (records by the
type index, links by their end indexes, ``_Store.touching``). Nothing here writes a link or
a value: a duplicate is a proposal a person confirms.

* ``duplicate_parties``: customers and suppliers (``PARTY_TYPES``) across and within systems
  (the system is the first part of the record's binding: ``bc``, ``portal``, ``fm``,
  ``sage``). Candidates share a blocking key (``_keys``: the normalized name, its sorted
  words, a phone number's last ten digits, an email or web domain that is no public mail
  domain, a tax or registration id and an address where the schema declares one); a key
  more than ``BLOCK_MAX`` records share is too common to say anything and is counted, never
  paired. Each pair is scored (``_score``: name agreement from
  ``ontology.identity_scoring.party_names_agree``, plus each other key the two share);
  ``PROPOSE_AT`` or more is raised as ``duplicate_party_candidate`` with a review proposal
  (``proposal``: the ``operating.human_links`` ``prepare_link`` request for the
  ``<Type>.same_record_as`` link, which a person confirms through ``plx.review``). A pair
  already one settled cluster, or a pair a person rejected (a contradicted link), is never
  proposed. ``counts.pairs`` gives the proposals per system pair (``bc/<company>|portal``).
  The same party in two BC companies (two legal entities) is never a duplicate: it is a
  ``same_party_across_companies`` relation proposal (``related``, never an alert). Within one
  system only an exact or close name proposes, and sibling accounts (another currency, a
  code with a suffix, or a name that only shares a prefix) are ``related_accounts``.
* ``key_fields_missing``: an active record (``REQUIREMENTS``: a customer without payment
  terms, an item without a unit, a sales order without a ship date; a purchase order
  without a due date is the engine's ``undated_po``) whose settled twins all leave the
  field empty. Active is the one status definition's verdict for orders, else the record's
  own status words (``OPEN_WORDS``/``DONE_WORDS``, a deleted or blocked flag);
  an unknown status word is never read as active or not (``status_unknown``, counted). A
  requirement whose fields no source declares is named in the rule's reasons
  (``not_served_<Type>__<property>``) and judged nowhere; all of them: ``blocked``.
* ``twin_disagreement``: a settled identity cluster (``COMPARED``) whose members of two
  systems state different values of one property: a status (by its class, open or done; an
  unknown word is not compared), an amount (``Decimal`` in one currency; another currency
  is never compared) or a date. One alert per cluster and property, citing both sides. A
  cluster holding two records of one system (an order split in one system, whole in the
  other) compares no amount, and no property its same-system records state differently
  (``split_record``, counted). A cluster one of whose numbers (``NUMBER_PROPS``) a record
  of another system it holds states outside it, while that system's member in the cluster
  does not, may be two records: not compared (``identity_doubt``, counted).
* ``stale_captures``: a source family whose last capture (the store's ``freshness``) is
  ``STALE_HOURS`` or more before the run, cited by one record of that source so only its
  readers see it; an unknown capture time is counted (``capture_time_unknown``).

Fixture runs are ``isolated-fixture``; no rule names a record, a customer or an item.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import datetime as dt
from datetime import date
from decimal import Decimal
import itertools
import json
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import proactive_business as pb
from .proactive_stock import _clusters, _plural, declares, identity_rows, waiting

DUP_RULE = "duplicate_parties"
FIELDS_RULE = "key_fields_missing"
DISAGREE_RULE = "twin_disagreement"
STALE_RULE = "stale_captures"
RULES = (DUP_RULE, FIELDS_RULE, DISAGREE_RULE, STALE_RULE)
DUPLICATE = "duplicate_party_candidate"
MISSING = "key_field_missing"
DISAGREE = "twins_disagree"
STALE = "source_stale"
KINDS = (DUPLICATE, MISSING, DISAGREE, STALE)

PARTY_TYPES = ("Customer", "Supplier")
NAME_PROPS = ("name",)
EMAIL_PROPS = ("email", "email_2", "email_3", "bc_email", "csr_email", "rep_email",
               "website")
PHONE_PROPS = ("bc_phone_number", "phone_number", "phone")
TAX_PROPS = ("tax_id", "vat_registration_no", "gst_hst_number", "business_number",
             "registration_number")
ADDRESS_PROPS = ("address", "address_line_1", "street")
POSTAL_PROPS = ("postal_code", "post_code", "zip")
# Public mail domains say nothing about whose company a record is.
PUBLIC_DOMAINS = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                            "live.com", "msn.com", "yahoo.com", "yahoo.ca", "icloud.com",
                            "me.com", "aol.com", "protonmail.com", "proton.me", "gmx.com",
                            "mail.com", "shaw.ca", "rogers.com", "sympatico.ca",
                            "bell.net", "telus.net"})
BLOCK_FLAGS = ("deleted_at", "bc_blocked", "blocked", "is_active")   # _flag_active reads
BLOCK_MAX = 25            # records one key may join before it is too common to pair
# A name alone never proposes (the whole store had 19,000 same-name pairs, most of them
# bill-to and ship-to copies): it takes a name and one other shared key, or a tax id.
NAME_POINTS = {"exact": Decimal("0.5"), "close": Decimal("0.4"), "prefix": Decimal("0.3"),
               "near": Decimal("0.1")}
KEY_POINTS = {"tax": Decimal("0.6"), "phone": Decimal("0.3"), "domain": Decimal("0.2"),
              "address": Decimal("0.3")}
PROPOSE_AT = Decimal("0.6")
ACROSS_COMPANIES = "same_party_across_companies"   # a relation, never ``same_record_as``
MAX_RELATED = 2000
HIGH_AT = Decimal("0.8")
MAX_PROPOSALS = 5000      # per run; the rest counted (``proposal_bound``)

OPEN_WORDS = frozenset({"open", "released", "pending", "pending approval", "approved",
                        "confirmed", "in progress", "in_progress", "partially received",
                        "partially shipped", "partial", "submitted", "active", "new",
                        "allocated", "scheduled", "ordered", "sent"})
DONE_WORDS = frozenset({"closed", "complete", "completed", "cancelled", "canceled", "void",
                        "voided", "received", "fully received", "shipped", "fully shipped",
                        "invoiced", "archived", "deleted", "inactive", "rejected",
                        "finished", "done"})
STATUS_PROPS = ("status", "bc_status", "fm_status")
TRUE_WORDS = frozenset({"true", "yes", "1", "all", "ship", "invoice", "payment"})
FALSE_WORDS = frozenset({"false", "no", "0", "", "none", "_blank_", " "})


@dataclass(frozen=True)
class Requirement:
    type: str
    fields: tuple[str, ...]       # any one stated is enough
    label: str                    # the record in words
    field: str                    # the field in words
    by_status: bool               # active by its status words (else by deleted/blocked flags)


REQUIREMENTS = (
    Requirement("Customer", ("payment_terms_code", "payment_terms", "terms"), "Customer",
                "payment terms", False),
    # a purchase order without a due date is the engine's own ``undated_po`` alert (open by
    # the one PO status definition): never raised twice
    Requirement("Product", ("bc_base_uom", "uom", "base_unit_of_measure"), "Item", "unit",
                False),
    Requirement("SalesOrder", ("required_date", "requested_ship_date", "shipment_date",
                               "ship_date"), "Sales order", "ship date", True),
)
LABEL_PROPS = ("code", "po_number", "order_number", "erp_code", "name")
# the number an order is known by in every system that keeps it
NUMBER_PROPS = ("po_number", "order_number")


@dataclass(frozen=True)
class Compared:
    type: str
    statuses: tuple[str, ...]
    amounts: tuple[str, ...]
    dates: tuple[str, ...]
    label: str


COMPARED = (
    Compared("PurchaseOrder", ("status",), ("total_cost",), ("order_date", "expected_date"),
             "Purchase order"),
    Compared("SalesOrder", ("status",), ("total_amount", "subtotal"),
             ("order_date", "required_date"), "Sales order"),
    Compared("Customer", ("status",), ("bc_credit_limit",), (), "Customer"),
    Compared("Supplier", ("status",), (), (), "Supplier"),
)
STALE_HOURS = 48
STALE_HIGH_HOURS = 24 * 7
SOURCE_WORDS = {"portal_api": "Portal", "bc": "Business Central", "graph_mail": "mail",
                "sharepoint": "SharePoint", "mission_control": "Mission Control",
                "teams": "Teams", "meetings": "meetings", "original": "original files"}


# The Portal serves the FileMaker and Sage books beside its own (``portal.fm_customer``,
# ``portal.sage_ar_customer``): each is its own system.
SOURCE_PREFIXES = (("portal.fm_", "fm"), ("portal.sage_", "sage"), ("fm.", "fm"),
                   ("sage.", "sage"))


def _system(record: tuple[str, str] | str | None) -> str:
    """The system a record is kept in: ``bc/<company>`` (BC holds one book per company; the
    company is its audience's first part, ``bc:<company id>/customers``), ``fm``, ``sage``
    or ``portal``. Two BC companies are two legal entities, never one system."""
    binding, audience = record if isinstance(record, tuple) else (record, None)
    binding = binding or "unknown"
    for prefix, name in SOURCE_PREFIXES:
        if binding.startswith(prefix):
            return name
    system = binding.split(".", 1)[0] or "unknown"
    if system == "bc":
        company = str(audience or "").split(":", 1)[-1].split("/", 1)[0]
        return f"bc/{company[:8]}" if company and audience else "bc"
    return system


def _company(system: str) -> str | None:
    return system.split("/", 1)[1] if system.startswith("bc/") else None


def _bindings(st: Any, types: Sequence[str]) -> dict[str, tuple[str, str]]:
    key = "quality:bindings:" + ",".join(types)
    return st.memo(key, lambda: {r[0]: (r[1], r[2]) for r in st.conn.execute(
        "SELECT object_id, binding_id, audience_key FROM objects WHERE object_type IN "
        "(SELECT value FROM json_each(?))", (json.dumps(list(types)),))})


def _sibling(props_a: Mapping[str, list], props_b: Mapping[str, list]) -> bool:
    """Two accounts of one system that are siblings, not copies: each states a different
    currency (a per-currency vendor account), or one's code is the other's with a suffix
    (``C100`` and ``C100-2``: a location or bill-to account)."""
    cur = [{_text(r).casefold() for r in _said(p, ("currency",))} for p in (props_a, props_b)]
    if all(cur) and not cur[0] & cur[1]:
        return True
    codes = [{_text(r).casefold() for r in _said(p, ("code", "erp_code"))}
             for p in (props_a, props_b)]
    return any(x != y and (y.startswith(x) or x.startswith(y))
               for x in codes[0] for y in codes[1])


def _text(row: Mapping[str, Any] | None) -> str:
    return " ".join(str((row or {}).get("value") or "").split())


def _said(props: Mapping[str, list], names: Sequence[str]) -> list[Mapping[str, Any]]:
    return [r for n in names for r in props.get(n) or () if _text(r)]


def _domain(raw: str) -> str | None:
    text = raw.strip().casefold()
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    text = re.sub(r"^[a-z]+://", "", text).split("/", 1)[0].removeprefix("www.")
    text = text.strip(" .>")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", text) or text in PUBLIC_DOMAINS:
        return None
    return text


def _keys(props: Mapping[str, list], declared: Mapping[str, tuple[str, ...]]
          ) -> dict[str, dict[str, Mapping[str, Any]]]:
    """kind -> {key: the row stating it}: the blocking keys one record states."""
    from ontology.identity_scoring import normalize_name

    out: dict[str, dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for row in _said(props, NAME_PROPS):
        name = normalize_name(row["value"])
        if name:
            out["name"].setdefault(name, row)
            out["words"].setdefault(" ".join(sorted(name.split())), row)
    for row in _said(props, declared["phone"]):
        digits = re.sub(r"\D", "", str(row["value"]))
        if len(digits) >= 7:
            out["phone"].setdefault(digits[-10:], row)
    for row in _said(props, declared["email"]):
        for part in re.split(r"[;,\s]+", str(row["value"])):
            found = _domain(part) if part else None
            if found:
                out["domain"].setdefault(found, row)
    for row in _said(props, declared["tax"]):
        tax = re.sub(r"[^0-9A-Za-z]", "", str(row["value"])).upper()
        if len(tax) >= 6:
            out["tax"].setdefault(tax, row)
    postal = {re.sub(r"\s", "", str(r["value"])).upper() for r in _said(props, declared["postal"])}
    for row in _said(props, declared["address"]):
        street = re.sub(r"[^0-9a-z ]", "", str(row["value"]).casefold())
        street = " ".join(street.split())
        if re.search(r"\d", street) and len(street) >= 6:
            out["address"].setdefault(street + "|" + "|".join(sorted(postal)), row)
    return out


def _score(level: str, shared: Iterable[str]) -> Decimal:
    total = NAME_POINTS.get(level, Decimal(0)) + sum(
        (KEY_POINTS[k] for k in set(shared) if k in KEY_POINTS), Decimal(0))
    return min(total, Decimal(1))


def duplicate_parties(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable,
                      types: Sequence[str] = PARTY_TYPES) -> dict[str, Any]:
    from ontology.identity_scoring import party_names_agree

    types = [t for t in types if declares(t, "name", f"{t}.same_record_as")]
    if not types:
        return result([], status="blocked",
                      reasons=[waiting(f"{t}.name") for t in PARTY_TYPES])
    skipped = skips()
    counts: Counter = Counter()
    pairs_by_system: Counter = Counter()
    related: list[dict[str, Any]] = []
    gaps = []
    for object_type in types:
        declared = {k: tuple(p for p in props if declares(object_type, p))
                    for k, props in (("phone", PHONE_PROPS), ("email", EMAIL_PROPS),
                                     ("tax", TAX_PROPS), ("address", ADDRESS_PROPS),
                                     ("postal", POSTAL_PROPS))}
        wanted = [*NAME_PROPS, *(p for v in declared.values() for p in v),
                  *(p for p in ("code", "erp_code", "currency", *BLOCK_FLAGS)
                    if declares(object_type, p))]
        found = st.values([object_type], wanted)
        binding = _bindings(st, types)
        link = f"{object_type}.same_record_as"
        root, _leads = _clusters(st, found, link, settled)
        # a pair a person rejected: its link contradicted (the same rows ``_clusters`` read)
        rejected = {tuple(sorted((r[3], r[4]))) for r in identity_rows(st, found, link)
                    if r[5] == "contradicted"}
        keys: dict[str, dict[str, dict[str, Mapping[str, Any]]]] = {}
        blocks: dict[tuple[str, str], list[str]] = defaultdict(list)
        for oid, entry in sorted(found.items()):
            pb._tick(st)
            counts["records"] += 1
            keys[oid] = _keys(entry["props"], declared)
            if not keys[oid].get("name"):
                skipped.note("name_unknown", [oid], [(oid, "[]")])
                continue
            for kind, values in keys[oid].items():
                for value in values:
                    blocks[(kind, value)].append(oid)
        candidates: dict[tuple[str, str], set[str]] = defaultdict(set)
        for (kind, _value), members in blocks.items():
            if len(members) < 2:
                continue
            if len(members) > BLOCK_MAX:
                counts["keys_too_common"] += 1
                continue
            for a, b in itertools.combinations(sorted(set(members)), 2):
                candidates[(a, b)].add(kind)
        for (a, b), kinds in sorted(candidates.items()):
            pb._tick(st)
            if root.get(a, a) == root.get(b, b):
                counts["already_one"] += 1
                continue
            if (a, b) in rejected:
                counts["rejected"] += 1
                continue
            name_a = next(iter(keys[a]["name"].values()))
            name_b = next(iter(keys[b]["name"].values()))
            level = party_names_agree(name_a["value"], name_b["value"])
            shared = [k for k in ("tax", "phone", "domain", "address")
                      if set(keys[a].get(k, ())) & set(keys[b].get(k, ()))]
            score = _score(level, shared)
            systems = tuple(sorted((_system(binding.get(a)), _system(binding.get(b)))))
            companies = {_company(x) for x in systems}
            if None not in companies and len(companies) == 2:
                # the same party in two BC companies: two legal entities, never one record;
                # a person may relate them (``ACROSS_COMPANIES``), no data-quality alert
                if level in ("exact", "close") or "tax" in shared:
                    counts["across_companies"] += 1
                    if len(related) < MAX_RELATED:
                        related.append({"mode": "propose_relation", "relation": ACROSS_COMPANIES,
                                        "record": a, "other": b, "systems": list(systems),
                                        "basis": [f"names {level}", *shared]})
                continue
            if systems[0] == systems[1] and (level not in ("exact", "close")
                                             or _sibling(found[a]["props"], found[b]["props"])):
                counts["related_accounts"] += 1   # sibling accounts of one book: no alert
                continue
            if score < PROPOSE_AT:
                counts["below_threshold"] += 1
                continue
            if counts["proposed"] >= MAX_PROPOSALS:
                counts["proposal_bound"] += 1
                continue
            pair = "|".join(systems)
            evidence = [keys[a][k][v] for k in shared for v in keys[a][k] if v in keys[b][k]]
            evidence += [keys[b][k][v] for k in shared for v in keys[b][k] if v in keys[a][k]]
            said = {"tax": "tax or registration id", "phone": "phone number",
                    "domain": "email or web domain", "address": "street address"}
            basis = [f"names {level}" if level in NAME_POINTS else "names differ",
                     *(f"same {said[k]}" for k in shared)]
            where = ("in " + systems[0]) if systems[0] == systems[1] else \
                f"in {systems[0]} and {systems[1]}"
            noun = object_type.lower()
            retired = any(_flag_active(found[x]["props"])[0] == "done" for x in (a, b))
            counts["blocked_copy"] += retired
            made = gap(DUPLICATE, DUP_RULE, a, object_type, b,
                       summary=f"Two {noun} records {where} look like one {noun}: "
                               f"{_text(name_a)} and {_text(name_b)} "
                               f"(score {score}: {', '.join(basis)})"
                               + ("; one of them is blocked or deleted, so it is the retired "
                                  "copy if they match" if retired else ""),
                       missing=f"Are these the same {noun}? Confirm or reject the link in "
                               "the record's workspace; nothing is linked until a person "
                               "confirms.",
                       why="Their names and the keys they share score them as one "
                           f"{noun}; no settled link says so yet.",
                       # a pair one of whose records is blocked or deleted is most often
                       # the retired copy of the other: low priority (owner, 2026-11-08)
                       priority="low" if retired else
                       "high" if score >= HIGH_AT else "medium",
                       about=(object_type, "same_record_as"),
                       groups=[cites([name_a]), cites([name_b])],
                       extra=cites(evidence),
                       event={"orders_affected": 2})
            if made is None:
                continue
            made["follow_up"]["proposal"] = {"mode": "prepare_link", "record": a, "document": b,
                                "link_type": link, "score": str(score),
                                "systems": list(systems), "basis": basis}
            counts["proposed"] += 1
            pairs_by_system[pair] += 1
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {**{k: counts.get(k, 0) for k in (
        "records", "proposed", "already_one", "rejected", "below_threshold",
        "keys_too_common", "proposal_bound", "across_companies", "related_accounts")},
        "pairs": dict(sorted(pairs_by_system.items()))}
    out["related"] = related   # review proposals of the cross-company relation, never alerts
    return out


# --------------------------------------------------------------------------- missing fields

def _fold(raw: Any) -> str:
    return " ".join(str(raw or "").replace("_", " ").split()).casefold()


def _status_class(props: Mapping[str, list], names: Sequence[str] = STATUS_PROPS
                  ) -> tuple[str | None, Mapping[str, Any] | None]:
    """("open"/"done", the row) of the first status the record states one value of;
    ("unknown", row) for a word neither set knows; (None, None) when none is stated."""
    for name in names:
        row = pb._one(_said(props, (name,)))
        if row is None:
            continue
        word = _fold(row["value"])
        if word in OPEN_WORDS:
            return "open", row
        if word in DONE_WORDS:
            return "done", row
        return "unknown", row
    return None, None


def _flag_active(props: Mapping[str, list]) -> tuple[str, Mapping[str, Any] | None]:
    """A record active unless deleted or blocked; an unknown flag word is unknown."""
    for name in ("deleted_at",):
        if _said(props, (name,)):
            return "done", _said(props, (name,))[0]
    for name in ("bc_blocked", "blocked"):
        row = pb._one(_said(props, (name,)))
        if row is None:
            continue
        word = _fold(row["value"])
        if word in TRUE_WORDS:
            return "done", row
        if word not in FALSE_WORDS:
            return "unknown", row
    for name in ("is_active",):
        row = pb._one(_said(props, (name,)))
        if row is not None:
            word = _fold(row["value"])
            if word in FALSE_WORDS - {""}:
                return "done", row
            if word not in TRUE_WORDS:
                return "unknown", row
    return "open", None


def key_fields_missing(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                       result: Callable, settled: Sequence[str], skips: Callable,
                       requirements: Sequence[Requirement] = REQUIREMENTS,
                       absence: Callable[[Sequence[str]], list[str]] | None = None,
                       status_of: Mapping[str, Callable[[], Mapping[str, str]]] | None = None
                       ) -> dict[str, Any]:
    """``absence``: the audiences a verdict resting on what records of a type do not
    hold needs a grant on (``REQUIRES_GRANT``); without it a gap is shown to no one.
    ``status_of``: per type, the one status definition's verdict per record (``open``,
    ``closed``, ``draft`` or ``unknown``: ``po_status`` for purchase orders, the one
    sales-order definition for customer orders); a type without one reads its own status
    words (``OPEN_WORDS``/``DONE_WORDS``)."""
    served = [(r, tuple(f for f in r.fields if declares(r.type, f))) for r in requirements]
    unserved = [f"{r.type}.{r.fields[0]}" for r, fields in served if not fields]
    served = [(r, fields) for r, fields in served if fields]
    if not served:
        return result([], status="blocked", reasons=[waiting(f) for f in unserved])
    skipped = skips()
    counts: Counter = Counter()
    gaps = []
    for req, fields in served:
        found = st.values([req.type], [*fields, *STATUS_PROPS, *LABEL_PROPS, "deleted_at",
                                       "bc_blocked", "blocked", "is_active"])
        root, _leads = _clusters(st, found, f"{req.type}.same_record_as", settled)
        clusters: dict[str, list[str]] = defaultdict(list)
        for oid in found:
            clusters[root.get(oid, oid)].append(oid)
        for top, members in sorted(clusters.items()):
            pb._tick(st)
            members.sort()
            if any(_said(found[m]["props"], fields) for m in members):
                counts["stated"] += 1
                continue
            states = [(_status_class(found[m]["props"]) if req.by_status
                       else _flag_active(found[m]["props"])) for m in members]
            classes = {c for c, _row in states}
            verdicts = (status_of or {}).get(req.type)
            if verdicts is not None:
                said = {verdicts().get(m, "unknown") for m in members}
                classes = ({"open"} if said == {"open"} else
                           {"done"} if said & {"closed", "draft"} else {"unknown"})
            if "done" in classes:
                counts["inactive"] += 1
                continue
            if "unknown" in classes or (req.by_status and None in classes):
                skipped.note("status_unknown", members, [(m, "[]") for m in members])
                continue
            counts["missing"] += 1
            label_rows = [r for m in members
                          for r in [pb._one(_said(found[m]["props"], (p,)))
                                    for p in LABEL_PROPS] if r][:1]
            anchor = [row for _c, row in states if row is not None] or label_rows
            if not anchor:
                skipped.note("record_uncited", members, [(m, "[]") for m in members])
                continue
            named = _text(label_rows[0]) if label_rows else ""
            what = f"{req.label} {named}".strip()
            made = gap(MISSING, FIELDS_RULE, top, req.type, f"{req.type}|{fields[0]}",
                       summary=f"{what} is active and states no {req.field}",
                       missing=f"What is its {req.field}? Enter it in the system the record "
                               "is kept in.",
                       why=f"No {req.field} is stated on the record or any of its copies in "
                           "other systems, and its status says it is still active.",
                       priority="medium", about=(req.type, fields[0]),
                       groups=[cites(anchor)], extra=cites(label_rows))
            if made is not None:
                made["requires"] = absence([req.type]) if absence is not None else []
            gaps.append(made)
    out = result(gaps, not_evaluated=skipped,
                 reasons=[waiting(f) for f in dict.fromkeys(unserved)])
    out["counts"] = {k: counts.get(k, 0) for k in ("stated", "inactive", "missing")}
    return out


# --------------------------------------------------------------------------- disagreement

def _amount(st: Any, row: Mapping[str, Any], codes: Mapping[tuple, str | None]
            ) -> tuple[Decimal, str] | None:
    number = pb._dec(row.get("value"))
    currency = pb._currency(codes, row)
    return (number, currency) if number is not None and currency else None


def twin_disagreement(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                      result: Callable, settled: Sequence[str], skips: Callable,
                      compared: Sequence[Compared] = COMPARED) -> dict[str, Any]:
    compared = [c for c in compared if declares(c.type, None, f"{c.type}.same_record_as")]
    if not compared:
        return result([], status="blocked",
                      reasons=[waiting(f"{c.type}.same_record_as") for c in COMPARED])
    skipped = skips()
    counts: Counter = Counter()
    gaps = []
    # copies the identity step corroborated (BC code and name agreeing) are one record too
    tiers = (*settled, "corroborated")
    for spec in compared:
        props = tuple(p for p in (*spec.statuses, *spec.amounts, *spec.dates)
                      if declares(spec.type, p))
        link = f"{spec.type}.same_record_as"
        twinned = {e for l in st.links([link], tiers)
                   for e in (l["from_object"], l["to_object"])}
        if not twinned or not props:
            continue
        found = st.values([spec.type], [*props, *LABEL_PROPS], objects=sorted(twinned))
        binding = _bindings(st, [spec.type])
        root, _leads = _clusters(st, found, link, tiers)
        clusters: dict[str, list[str]] = defaultdict(list)
        for oid in found:
            clusters[root.get(oid, oid)].append(oid)
        codes = pb._currencies(st, [r for e in found.values()
                                    for p in spec.amounts for r in e["props"].get(p) or ()])
        numbers = _numbers(st, spec.type)
        for top, members in sorted(clusters.items()):
            pb._tick(st)
            members.sort()
            if len({_system(binding.get(m)) for m in members}) < 2:
                continue
            counts["clusters"] += 1
            if _numbered_elsewhere(members, numbers, binding):
                # a member's own number is held, outside the cluster, by a record of a
                # system the cluster holds another number for: the copies may be two
                # records (live 11-12: 3 of 15 were a BC order linked to a Portal backorder
                # or a later order while the Portal's order of the BC number stood apart)
                counts["identity_doubt"] += 1
                continue
            for prop in props:
                sides: dict[str, tuple[Any, Mapping[str, Any], str]] = {}
                stating: Counter = Counter()
                unreadable = split = False
                for m in members:
                    rows = _said(found[m]["props"], (prop,))
                    if not rows:
                        continue
                    row = pb._one(rows)
                    if row is None:
                        unreadable = True
                        break
                    if prop in spec.statuses:
                        cls, _r = _status_class(found[m]["props"], (prop,))
                        value = cls if cls in ("open", "done") else None
                    elif prop in spec.amounts:
                        value = _amount(st, row, codes)
                        if value is not None:
                            # compared to the cent: a float-written copy (250.70000000000002)
                            # states the same amount as its decimal twin (250.700)
                            value = (value[0].quantize(Decimal("0.01")), value[1])
                        if value is not None and value[0] == 0:
                            # a zero total is no statement of the amount: a source zeroes
                            # an invoiced order's header (Sage) or leaves an unpriced copy
                            # at 0 (live 11-10: 6 of 15 disagreements were a 0 side)
                            counts["zero_amount"] += 1
                            continue
                    else:
                        value = pb._day(row["value"])
                    if value is None:
                        unreadable = True
                        break
                    system = _system(binding.get(m))
                    stating[system] += 1
                    held = sides.setdefault(system, (value, row, m))
                    if held[0] != value:
                        split = True
                if unreadable:
                    skipped.note("value_unreadable", members, [(m, "[]") for m in members])
                    continue
                if split or (prop in spec.amounts and max(stating.values(), default=0) > 1):
                    # two records of one system in one cluster: one order split into two
                    # (or two merged into one) across systems; a part's amount is no
                    # statement of the whole, and two differing copies of one side have no
                    # one value to compare (live 11-12: 2 of 15 were a Sage order holding
                    # two Portal orders, its total their sum)
                    counts["split_record"] += 1
                    continue
                if len(sides) < 2:
                    continue
                if prop in spec.amounts and len({v[1] for v, _r, _m in sides.values()}) > 1:
                    counts["currency_differs"] += 1
                    continue
                values = {v for v, _r, _m in sides.values()}
                if len(values) < 2:
                    counts["agree"] += 1
                    continue
                counts["disagree"] += 1
                said = []
                for system, (value, row, _m) in sorted(sides.items()):
                    if isinstance(value, tuple):
                        shown = f"{format(value[0], 'f')} {value[1]}"
                    elif isinstance(value, date):
                        shown = value.isoformat()
                    else:
                        shown = f"{_text(row)} ({value})"
                    said.append(f"{system.split('/', 1)[0]} says {shown}")
                label = next((_text(r) for m in members
                              for r in [pb._one(_said(found[m]["props"], (p,)))
                                        for p in LABEL_PROPS] if r), "")
                field = prop.replace("_", " ")
                rows = [row for _v, row, _m in sides.values()]
                finance = pb.FINANCE if prop in spec.amounts else None
                made = gap(DISAGREE, DISAGREE_RULE, top, spec.type, prop,
                           summary=f"{spec.label} {label}".strip() + f": the systems disagree "
                                   f"on its {field}: " + "; ".join(said),
                           missing=f"Which {field} is right? Correct the system that is "
                                   "wrong.",
                           why="The copies of one record in different systems state "
                               f"different {field} values.",
                           priority="medium", about=(spec.type, prop),
                           groups=[cites([r], finance) for r in rows])
                gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("clusters", "agree", "disagree",
                                                    "currency_differs", "zero_amount",
                                                    "split_record", "identity_doubt")}
    return out


def _number(raw: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(raw or "")).casefold()


def _numbers(st: Any, object_type: str) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(number -> every record of the type stating it, record -> its numbers), from
    ``NUMBER_PROPS`` in every system."""
    props = [p for p in NUMBER_PROPS if declares(object_type, p)]
    holders: dict[str, set[str]] = defaultdict(set)
    numbered: dict[str, set[str]] = defaultdict(set)
    if not props:
        return holders, numbered
    for oid, entry in st.values([object_type], props).items():
        for prop in props:
            for row in entry["props"].get(prop) or ():
                number = _number(row.get("value"))
                if number and number not in ("none", "null"):
                    holders[number].add(oid)
                    numbered[oid].add(number)
    return holders, numbered


def _numbered_elsewhere(members: Sequence[str],
                        numbers: tuple[Mapping[str, set[str]], Mapping[str, set[str]]],
                        binding: Mapping[str, tuple[str, str]]) -> bool:
    """Whether a member's number is held outside the cluster by a record of another system
    the cluster holds, none of whose members in the cluster states that number."""
    holders, numbered = numbers
    inside = set(members)
    systems = {_system(binding.get(m)) for m in members}
    for number in {n for m in members for n in numbered.get(m, ())}:
        mine = {_system(binding.get(m)) for m in holders[number] & inside}
        for other in holders[number] - inside:
            system = _system(binding.get(other))
            if system in systems and system not in mine:
                return True
    return False


# --------------------------------------------------------------------------- stale

def _instant(raw: Any) -> dt.datetime | None:
    try:
        moment = dt.datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=dt.timezone.utc)


def stale_captures(st: Any, as_of: date, *, gap: Callable, cites: Callable,
                   result: Callable, settled: Sequence[str], skips: Callable,
                   now: dt.datetime | None = None, hours: int = STALE_HOURS
                   ) -> dict[str, Any]:
    from .ontology_tools import _AUDIENCE_SOURCE

    if now is None or now.tzinfo is None:
        return result([], status="failed", reasons=["run_moment_unknown"])
    try:
        raw = dict(st.conn.execute("SELECT key, value FROM store_meta").fetchall())
        freshness = json.loads(raw.get("freshness") or "[]")
        audiences = json.loads(raw.get("audience_keys") or "[]")
    except Exception:   # noqa: BLE001 - an unreadable meta is no clean "all fresh"
        return result([], status="failed", reasons=["freshness_unreadable"])
    if not isinstance(freshness, list) or not isinstance(audiences, list):
        return result([], status="failed", reasons=["freshness_unreadable"])
    skipped = skips()
    counts: Counter = Counter()

    def one_per_audience() -> dict[str, str]:
        # one record per audience, in one pass over the record table (a probe per audience
        # scanned it once per mailbox: 157 s on the whole store)
        firsts = getattr(st, "audience_firsts", None)
        if firsts is not None:
            return firsts()   # the run's one ranged scan, shared with its audience keys
        return st.memo("quality:one_per_audience", lambda: {
            key: oid for key, oid in st.conn.execute(
                "SELECT audience_key, MIN(object_id) FROM objects GROUP BY audience_key")})
    by_source: dict[str, list[str]] = defaultdict(list)
    for key in sorted(a for a in audiences if isinstance(a, str)):
        by_source[_AUDIENCE_SOURCE.get(key.split(":", 1)[0], key.split(":", 1)[0])].append(key)
    gaps = []
    for entry in freshness:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("source"), str):
            skipped.note("entry_unreadable")
            continue
        source = entry["source"]
        counts["sources"] += 1
        captured = _instant(entry.get("captured_at"))
        if captured is None:
            skipped.note("capture_time_unknown")
            continue
        age = (now - captured).total_seconds() / 3600
        if age < hours:
            counts["fresh"] += 1
            continue
        row = None
        for key in by_source.get(source, ()):
            oid = one_per_audience().get(key)
            row = None if oid is None else st.conn.execute(
                "SELECT object_id, l1_handle, markings FROM object_versions WHERE "
                "object_id = ? LIMIT 1", (oid,)).fetchone()
            if row is not None:
                break
        if row is None:
            skipped.note("source_uncited")
            continue
        counts["stale"] += 1
        whole = int(age)
        words = SOURCE_WORDS.get(source, source.replace("_", " "))
        made = gap(STALE, STALE_RULE, row[0], row[0].split(":", 1)[0], source,
                   summary=f"{words} was last captured {captured.isoformat()[:16]} UTC, "
                           f"{_plural(whole, 'hour')} ago: newer changes are not in it",
                   missing=f"Is the {words} capture running? Check its last run.",
                   why=f"The newest capture of this source is {hours} hours or more old.",
                   priority="high" if whole >= STALE_HIGH_HOURS else "medium",
                   about=("source", source),
                   groups=[cites([{"l1_handle": row[1], "object_id": row[0],
                                   "markings": row[2]}])],
                   event={"days": whole // 24})
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts.get(k, 0) for k in ("sources", "fresh", "stale")}
    return out
