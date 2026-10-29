"""Live check ``access.control_proof``: no employee is served another person's mail or Teams
content, a finance-only file to a reader without finance clearance, or anything of an
excluded person.

``python3 -m scripts.access_control_proof [--default-host-config FILE]`` reads the host the
way its sessions serve it (``PLX_WORKBENCH_HOST_CONFIG``, ``PLX_WORKBENCH_STATE_DIR``; with
no host config in the environment, ``--default-host-config``: the live spec names the
verifier's host, and ``host_config_from`` says which was read): the mail, Teams and SharePoint
captures, the employee policy and the Portal access snapshot (the in-repo role model,
``plx-portal-access-snapshot/v1``). Every active snapshot user of the policy's tenant
is a reader (at most ``MAX_READERS``, ordered by a digest of the object id); every reader
asks for up to ``MAX_TARGETS`` stored records of each kind through every consumer that
serves a citation: ``CaseRuntime.resolve_citation`` (whose refusal is the typed reason),
the host's item gate and the claim gate's row reader. Nothing is read live: the runtime has
no live reader (a request the capture does not hold is refused ``source_not_configured``),
no request is sent and nothing is written to the host's state (the runtime's own directory
is a temporary one).

Who may read a record is decided here from the stores, independently of the guards:

* ``mail_other``: a reader whose identities (Portal object id and e-mail, directory UPN and
  mail) meet none of the mailbox's recorded identities;
* ``teams_other``: a reader in neither the message's captured ACL nor its conversation's
  current membership (both ``read``);
* ``finance_non_finance``: a reader without the restricted class's clearance (directory
  department and Portal membership both among ``RESTRICTED_CLASS_READERS``) asking for a
  current SharePoint file of that class;
* ``excluded_person``: every reader, for mail of an excluded mailbox or one an excluded
  person sent (an address recorded beside an excluded object id for a captured mailbox)
  and Teams messages an excluded person sent (or of a chat they belong to), however long
  before the exclusion they were captured. ``excluded_held`` counts such records still
  stored; any is a failure, since nobody may be served them;
* ``sender_unresolved``: every reader, for mail whose sender this check cannot resolve to
  an object id (no captured mailbox records the address) in a tenant domain (the case mail
  scope's internal domains and every domain of a captured mailbox's addresses), or whose
  sender is not an address: it may be an excluded colleague's, so nobody may be served it
  offline (fail closed). ``senders_unresolved`` counts them. A sender in no tenant domain
  is not a member of the tenant and is read as ``mail_other``.

Stored cases (GOV-19): each reader lists the cases it may read (newest ``MAX_CASES``) and
gets each; every stored item (finding, calculation, gap, proposal, verification, link) whose
citation the host's item gate refuses that reader must be served withheld (content
``withheld_citation_unavailable``, no value key, citations without handle or digest and
none of the stored item's text): ``withheld_unresolved`` counts them, ``unresolved_served``
the rest (each a denied leak). A stored item whose payload does not have the shape counts
``case_items_unreadable`` and fails (fail closed).

``po_reference`` text (REC-07): up to ``MAX_PO_VALUES`` current values of the served
projection that ``customer_po_coverage.po_like`` (the one shape test) rejects, and up to
``MAX_PO_CONTROLS`` it accepts. Per reader, through the session's own ontology tools
(``case_tools_host.serving_tools``): ``plx.object`` on each object holding the value,
``plx.find`` for it whole and by its first word, ``plx.investigate`` F2 on its customer PO
and every served case. ``echoed_non_po`` counts each served string stating a non-PO-like
value (whole words, any case or spacing; the caller's own query given back is no echo)
and fails; ``po_reference_scanned`` counts the values a reader was served an object of and
``po_like_served`` the PO-like ones stated (the guard is not withholding everything).

The mail_other and teams_other refusals are also the ING-14 proof: a captured message
reaches no reader outside its mailbox or conversation.

A reader the oracle admits is a control (``controls_served`` shows the guards are not
simply refusing everything). Output: counts and reason codes only, never an identifier.
A refusal without a typed code counts ``untyped`` and fails, as any leak does.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

CHECK = "access.control_proof"
KINDS = ("mail_other", "teams_other", "finance_non_finance", "excluded_person",
         "sender_unresolved")
REQUEST_KINDS = KINDS[:3]
MAX_READERS = 60
MAX_TARGETS = 10
MAX_CASES = 10          # newest cases each reader may list, read in full
MAX_PO_VALUES = 5       # non-PO-like po_reference values probed per reader
MAX_PO_CONTROLS = 2     # PO-like po_reference values probed per reader (controls)
MAX_PO_OBJECTS = 3      # objects holding one value read per reader
MAX_ANSWER_PROBES = 2   # plx.investigate answers per reader that asks
# Readers (the first, in digest order) that also search and ask: the value guard does not
# depend on the reader, and an answer costs a plan (bounded under the spec's timeout).
MAX_FIND_READERS = 3
MAX_ANSWER_READERS = 1
PO_ROOT_TYPE = "CustomerPurchaseOrder"
# Keys whose text is the code's own vocabulary (gap states, reasons, kinds, types), never
# source text: a value such as ``unknown`` is not echoed by ``"state": "unknown"``. Every
# other string served is scanned (ids, handles, details and subjects included).
CODE_KEYS = frozenset({"state", "reason", "status", "kind", "code", "value_type", "property",
                       "object_type", "feature", "family", "withheld", "content",
                       "field_path", "lineage", "role", "identifier_kind"})
EVIDENCE = "live-unverified"
_TYPED = re.compile(r"^[a-z][a-z0-9_.:\-]{0,63}$")
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Anything shaped like an object id inside a reason code: never a typed reason (an id).
_GUID_INSIDE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TEAMS_SCHEME = {"teams_channel": "teams-channel", "teams_chat": "teams-chat"}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lower(values: Iterable[Any]) -> frozenset[str]:
    return frozenset(str(v).strip().lower() for v in values if isinstance(v, str) and v.strip())


def _open(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _guids(value: Any) -> set[str]:
    """Every object id inside a captured ``from`` value (shape-agnostic)."""
    if isinstance(value, str):
        return {value.lower()} if _GUID.match(value.lower()) else set()
    if isinstance(value, Mapping):
        return set().union(*(_guids(v) for v in value.values())) if value else set()
    if isinstance(value, (list, tuple)):
        return set().union(*(_guids(v) for v in value)) if value else set()
    return set()


def _acl(value: Any) -> frozenset[str] | None:
    """A ``read`` ACL's identities, None for any other status (it admits nobody)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, Mapping) or value.get("status") not in ("read", "derived"):
        return None
    ids = value.get("identities")
    return _lower(ids) if isinstance(ids, list) else None


# ------------------------------------------------------------------ targets

def _domain(address: str) -> str | None:
    """The domain of an address, None for anything that is not one address."""
    if address.count("@") != 1 or not address.rsplit("@", 1)[1]:
        return None
    return address.rsplit("@", 1)[1]


def targets(state: Path, exclusion: Any, internal_domains: Iterable[str] = ()
            ) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    """The stored records per kind (each ``{"handle", "admits"}``: who the oracle admits),
    how many stored records belong to an excluded person and how many captured messages
    have a sender this check cannot resolve (``sender_unresolved``)."""
    from integrations import capture_sharepoint as cs
    from operating import doc_metadata_taxonomy as taxonomy

    out: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
    held = unresolved = 0
    db = _open(state / "mail-capture" / "capture.db")
    if db is not None:
        with db:
            boxes = {r["mailbox_id"]: _lower(json.loads(r["identities"])) for r in db.execute(
                "SELECT mailbox_id, identities FROM mailboxes")}
            tenant = set(_lower(internal_domains))
            # Whose an address is: the object ids recorded beside it for a captured mailbox.
            whose: dict[str, frozenset[str]] = {}
            for mailbox, ids in boxes.items():
                owner = frozenset(i for i in ids | {str(mailbox).lower()} if _GUID.match(i))
                for identity in ids:
                    tenant |= {_domain(identity)} - {None}
                    if owner:
                        whose[identity] = whose.get(identity, frozenset()) | owner
            for row in db.execute("SELECT l1_handle, mailbox_id, record FROM messages"):
                ids = boxes.get(row["mailbox_id"], frozenset())
                sender = _lower(_strings(json.loads(row["record"]).get("from")))
                named = ids | {str(row["mailbox_id"]).lower()} | frozenset().union(
                    *(whose.get(address, frozenset()) for address in sender))
                named |= {s for s in sender if _GUID.match(s)}
                if exclusion.excluded([i for i in named if _GUID.match(i)]):
                    held += 1
                    out["excluded_person"].append({"handle": row["l1_handle"]})
                elif any(not _GUID.match(s) and s not in whose
                         and (_domain(s) is None or _domain(s) in tenant) for s in sender):
                    unresolved += 1
                    out["sender_unresolved"].append({"handle": row["l1_handle"]})
                else:
                    out["mail_other"].append({"handle": row["l1_handle"], "mailbox": ids})
        db.close()
    db = _open(state / "teams-capture" / "capture.db")
    if db is not None:
        with db:
            meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
            units = {(r["kind"], r["unit"]): r["acl"] for r in db.execute(
                "SELECT kind, unit, acl FROM unit_acl")}
            for row in db.execute("SELECT capture_id, kind, path, unit, payload FROM versions "
                                  "WHERE superseded_at IS NULL"):
                scheme = _TEAMS_SCHEME.get(row["kind"])
                if scheme is None:
                    continue
                handle = f"{scheme}://{row['path']}@{meta.get('generation')}-{row['capture_id']}"
                payload = json.loads(row["payload"])
                message = (payload.get("meta") or {}).get("source_acl")
                unit = units.get((row["kind"], row["unit"]))
                members = (_acl(message) or frozenset()) | (_acl(unit) or frozenset())
                row_ = payload.get("row") or {}
                # A conversation's own record names its participants (TeamsChat/Channel/Team).
                sender = _guids([row_.get("from"), *[p.get("id") for p in
                                                     row_.get("participants") or ()
                                                     if isinstance(p, dict)]])
                if exclusion.excluded(sender) or (row["kind"] == "teams_chat" and exclusion.excluded(
                        [i for i in members if _GUID.match(i)])):
                    held += 1
                    out["excluded_person"].append({"handle": handle})
                else:
                    out["teams_other"].append({"handle": handle, "message": _acl(message),
                                               "unit": _acl(unit)})
        db.close()
    db = _open(state / "sharepoint-capture" / "capture.db")
    if db is not None:
        with db:
            for row in db.execute("SELECT drive_id, item_id, etag, record, source_acl FROM items "
                                  "WHERE superseded_at IS NULL AND removed_reason IS NULL"):
                security_class, _owner = taxonomy.file_security_class(json.loads(row["record"]), {})
                readers = taxonomy.RESTRICTED_CLASS_READERS.get(security_class)
                if readers is None:
                    continue
                out["finance_non_finance"].append({
                    "handle": cs.version_handle(row["drive_id"], row["item_id"], row["etag"]),
                    "codes": frozenset(readers), "acl": _acl(row["source_acl"])})
        db.close()
    for kind in KINDS:
        out[kind] = sorted(out[kind], key=lambda t: _digest(t["handle"]))[:MAX_TARGETS]
    return out, held, unresolved


def _strings(value: Any) -> list[str]:
    """Every string inside a captured ``from`` value (shape-agnostic)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def admits(kind: str, reader: Mapping[str, Any], target: Mapping[str, Any]) -> bool | None:
    """True: the reader may be served the record (a control); False: a request that must be
    refused; None: not a request of this kind (e.g. an item ACL, not finance, decides)."""
    ids = reader["identities"]
    if kind == "mail_other":
        return bool(ids & target["mailbox"])
    if kind == "teams_other":
        message, unit = target["message"], target["unit"]
        return bool(message is not None and unit is not None and ids & message and ids & unit)
    if kind == "finance_non_finance":
        cleared = bool(reader["entra_codes"] & target["codes"]) and bool(
            reader["portal_codes"] & target["codes"])
        if not cleared:
            return False
        return True if target["acl"] is not None and ids & target["acl"] else None
    return False   # excluded_person, sender_unresolved: nobody


# ------------------------------------------------------------------ proof

def _reason(error: BaseException) -> str:
    from operating.employee_permissions import EmployeeAccessRefused
    code = getattr(error, "code", None) or (error.args[0] if error.args else None)
    if isinstance(error, EmployeeAccessRefused):
        code = f"employee_refused:{code}"
    if isinstance(code, str) and code.startswith("undetermined:"):
        code = "mailbox_access_undetermined"
    return (code if isinstance(code, str) and _TYPED.match(code)
            and not _GUID_INSIDE.search(code.lower()) else "untyped")


def consumers(runtime: Any) -> list[tuple[str, Callable[[str, Any], bool]]]:
    """Every consumer of a citation the host serves an employee through, as served-or-not."""
    from operating import ontology_tools_investigate as oi
    from operating.ontology_tools import item_gate_from_case_runtime

    gate, rows = item_gate_from_case_runtime(runtime), oi.citation_rows(runtime)
    return [("item_gate", lambda h, a: bool(gate(a, h))),
            ("claim_rows", lambda h, a: rows(h, a) is not None)]


def prove(runtime: Any, tenant: str, readers: list[dict[str, Any]],
          found: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    from ontology.types import Actor

    others = consumers(runtime)
    kinds = {kind: {"requests": 0, "refused": 0, "leaked": 0, "controls": 0,
                    "controls_served": 0, "reasons": {}} for kind in KINDS}
    untyped = 0
    for reader in readers:
        actor = Actor(tenant, frozenset({"employee:" + reader["oid"]}))
        for kind in KINDS:
            for target in found.get(kind, ()):
                allowed = admits(kind, reader, target)
                if allowed is None:
                    continue
                handle, reason = target["handle"], None
                try:
                    served = runtime.resolve_citation(handle, actor) is not None
                except Exception as error:  # noqa: BLE001 - every refusal is classified
                    served, reason = False, _reason(error)
                for _name, consumer in others:
                    try:
                        served = consumer(handle, actor) or served
                    except Exception:  # noqa: BLE001 - a raising consumer serves nothing
                        pass
                entry = kinds[kind]
                if allowed:
                    entry["controls"] += 1
                    entry["controls_served"] += int(served)
                    continue
                entry["requests"] += 1
                if served:
                    entry["leaked"] += 1
                    continue
                entry["refused"] += 1
                reason = reason or "refused_without_error"
                untyped += reason == "untyped"
                entry["reasons"][reason] = entry["reasons"].get(reason, 0) + 1
    leaks = sum(k["leaked"] for k in kinds.values())
    return {"kinds": kinds, "denied_leaks": leaks, "untyped": untyped,
            "readers": len(readers), "probes": sum(k["requests"] + k["controls"]
                                                   for k in kinds.values())}


# ------------------------------------------------------------------ case items (GOV-19)

# The stored case events that record an item, and the payload key of its id.
ITEM_EVENTS = {"finding.recorded": "finding_id", "calculation.recorded": "calc_id",
               "gap.recorded": "gap_id", "proposal.recorded": "proposal_id",
               "verification.recorded": "verification_id", "link.recorded": "link_id"}
# What a withheld item may still show: who recorded it when, and that it is withheld.
_ITEM_META = frozenset({"id", "kind", "label", "recorded_by", "at", "seq", "superseded",
                        "content", "citations", "proposal_id", "evidence_finding_id"})


def _cited(event_kind: str, payload: Mapping[str, Any]) -> list[str] | None:
    """The handles an item's served view is gated on (``CaseStore.get``), None when the
    stored payload does not have the shape (fails closed: counted, never skipped)."""
    if event_kind == "calculation.recorded":
        inputs = payload.get("inputs")
        if not isinstance(inputs, Mapping):
            return None
        cites = [spec.get("citation") for spec in inputs.values()
                 if isinstance(spec, Mapping) and spec.get("kind") == "citation"]
    else:
        cites = payload.get("citations") or []
    if not isinstance(cites, list) or not all(
            isinstance(c, Mapping) and isinstance(c.get("handle"), str) for c in cites):
        return None
    return [c["handle"] for c in cites]


def _texts(value: Any, skip: frozenset[str] = frozenset()) -> list[str]:
    """Every string leaf of ``value`` outside the keys ``skip`` names."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [t for k, v in value.items() if k not in skip for t in _texts(v, skip)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in _texts(v, skip)]
    return []


def stored_items(cases_file: Path) -> dict[str, list[dict[str, Any]]]:
    """Per case, every stored item: its id, case seq, the handles it cites and the text
    it holds (each string of its payload but ids, codes, handles and digests)."""
    db = _open(cases_file)
    out: dict[str, list[dict[str, Any]]] = {}
    if db is None:
        return out
    skip = _ITEM_META | {"status", "handle", "digest", "current_digest"} | set(
        ITEM_EVENTS.values())
    with db:
        for row in db.execute("SELECT case_id, case_seq, kind, payload_json FROM case_event "
                              "ORDER BY seq"):
            key = ITEM_EVENTS.get(row["kind"])
            if key is None:
                continue
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = None
            if not isinstance(payload, Mapping):   # unreadable: counted, never skipped
                out.setdefault(row["case_id"], []).append({
                    "id": None, "seq": row["case_seq"], "kind": row["kind"],
                    "handles": None, "texts": []})
                continue
            out.setdefault(row["case_id"], []).append({
                "id": payload.get(key), "seq": row["case_seq"], "kind": row["kind"],
                "handles": _cited(row["kind"], payload),
                "texts": sorted({t for t in _texts(payload, frozenset(skip))
                                 if len(t.strip()) >= 4})})
    db.close()
    return out


def _served_item(view: Any, item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every dict of a served case that is ``item`` (its id at its case seq)."""
    if isinstance(view, Mapping):
        found = [view] if view.get("id") == item["id"] and view.get("seq") == item["seq"] \
            else []
        return found + [d for v in view.values() for d in _served_item(v, item)]
    if isinstance(view, (list, tuple)):
        return [d for v in view for d in _served_item(v, item)]
    return []


def _withheld(served: Mapping[str, Any], item: Mapping[str, Any]) -> bool:
    """A served item shows none of its content: ``content`` withheld, every other key
    either metadata or None, citations without handle or digest, and no text the stored
    item holds anywhere in what is served of it."""
    from operating.cases import WITHHELD
    if served.get("content") != WITHHELD:
        return False
    extra = {k for k, v in served.items() if k not in _ITEM_META and v is not None}
    if extra - ({"status"} if item["kind"] == "proposal.recorded" else set()):
        return False
    cites = served.get("citations") or []
    if not isinstance(cites, list) or any(
            not isinstance(c, Mapping) or set(c) - {"index", "status"} for c in cites):
        return False
    shown = " ".join(_texts(served)).casefold()
    return not any(text.casefold() in shown for text in item["texts"])


def prove_cases(runtime: Any, tenant: str, readers: list[dict[str, Any]],
                items: Mapping[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """GOV-19: for each reader, every stored item of the cases it may list (newest
    ``MAX_CASES``) whose citation the item gate refuses that reader is served withheld."""
    from ontology.types import Actor
    from operating.ontology_tools import item_gate_from_case_runtime

    gate = item_gate_from_case_runtime(runtime)
    out = {"cases_read": 0, "case_reads_refused": 0, "case_items_unresolved": 0,
           "withheld_unresolved": 0, "unresolved_served": 0, "case_items_resolved": 0,
           "case_items_unreadable": 0}
    for reader in readers:
        actor = Actor(tenant, frozenset({"employee:" + reader["oid"]}))
        try:
            listed = runtime.cases.list(actor, limit=MAX_CASES)
        except Exception:  # noqa: BLE001 - a reader the store refuses is served no case
            out["case_reads_refused"] += 1
            continue
        allowed: dict[str, bool] = {}
        for summary_row in listed:
            try:
                view = runtime.cases.get(actor, summary_row["case_id"])
            except Exception:  # noqa: BLE001 - nothing of the case is served
                out["case_reads_refused"] += 1
                continue
            out["cases_read"] += 1
            for item in items.get(summary_row["case_id"], ()):
                if item["handles"] is None:
                    out["case_items_unreadable"] += 1
                    continue
                for handle in item["handles"]:
                    if handle not in allowed:
                        allowed[handle] = gate(actor, handle)
                if all(allowed[h] for h in item["handles"]):
                    out["case_items_resolved"] += 1
                    continue
                out["case_items_unresolved"] += 1
                if all(_withheld(d, item) for d in _served_item(view, item)):
                    out["withheld_unresolved"] += 1
                else:
                    out["unresolved_served"] += 1
    return out


# ------------------------------------------------------------------ po_reference (REC-07)

def po_reference_values(projection: Path) -> dict[str, list[str]]:
    """Every current ``po_reference`` value of the served projection (read-only) and the
    objects holding it: the non-PO-like ones (``customer_po_coverage.po_like``, the one
    shape test) first, then up to ``MAX_PO_CONTROLS`` PO-like controls."""
    from operating.customer_po_coverage import po_like
    from operating.ontology_tools import PO_TEXT_FIELD

    db = _open(projection)
    if db is None:
        return {}
    held: dict[str, set[str]] = {}
    with db:
        for row in db.execute("SELECT object_id, value, field_path FROM object_values "
                              "WHERE superseded_at IS NULL AND value_type = 'text'"):
            if (isinstance(row["value"], str) and row["value"].strip()
                    and str(row["field_path"]).rsplit(".", 1)[-1] == PO_TEXT_FIELD):
                held.setdefault(row["value"], set()).add(row["object_id"])
    db.close()
    ordered = sorted(held, key=_digest)
    other = [v for v in ordered if not po_like(v)][:MAX_PO_VALUES]
    like = [v for v in ordered if po_like(v)][:MAX_PO_CONTROLS]
    return {v: sorted(held[v])[:MAX_PO_OBJECTS] for v in other + like}


def _words(text: str) -> str:
    return " ".join(text.split()).casefold()


def echoes(served: Any, value: str, asked: str | None = None) -> int:
    """String leaves of ``served`` that state ``value`` (any case and spacing, as whole
    words), outside the keys whose text is the code's own vocabulary and the caller's own
    query (``asked``) given back."""
    wanted, count = _words(value), 0
    for text in _texts(served, CODE_KEYS):
        words = _words(text)
        if asked is not None and words == _words(asked):
            continue
        at = words.find(wanted)
        while at >= 0:
            end = at + len(wanted)
            if (at == 0 or not words[at - 1].isalnum()) and (
                    end == len(words) or not words[end].isalnum()):
                count += 1
                break
            at = words.find(wanted, at + 1)
    return count


def prove_po(runtime: Any, tenant: str, readers: list[dict[str, Any]],
             values: Mapping[str, list[str]], open_tools: Callable[[Any], Any]
             ) -> dict[str, Any]:
    """REC-07: per reader, every sampled non-PO-like ``po_reference`` value is stated by
    no served object (``plx.object`` on each object holding it) or served case; for the
    first ``MAX_FIND_READERS`` readers by no find result (``plx.find`` for the value and
    for its first word) and for the first ``MAX_ANSWER_READERS`` by no answer
    (``plx.investigate`` F2 on its customer PO, ``MAX_ANSWER_PROBES`` values). A PO-like
    value is stated. ``po_probed_by`` counts the served results scanned per consumer."""
    from ontology.types import Actor
    from operating.customer_po_coverage import po_like

    surfaces = ("object", "find", "answer", "case")
    out = {"po_reference_values": len(values),
           "po_reference_non_po_like": sum(not po_like(v) for v in values),
           "po_reference_scanned": 0, "echoed_non_po": 0, "po_like_served": 0,
           "echoed_by": dict.fromkeys(surfaces, 0), "po_probed_by": dict.fromkeys(surfaces, 0)}

    def call(tools: Any, gated: Any, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return tools.call(name, arguments, gated)
        except Exception:  # noqa: BLE001 - a refused call serves nothing
            return None

    probed = 0
    for reader in readers if values else ():
        actor = Actor(tenant, frozenset({"employee:" + reader["oid"]}))
        try:
            gated = runtime.employee_gate(actor)
        except Exception:  # noqa: BLE001 - a refused employee is served nothing
            continue
        finds, answers = probed < MAX_FIND_READERS, probed < MAX_ANSWER_READERS
        probed += 1
        served_cases = []
        try:
            for row in runtime.cases.list(actor, limit=MAX_CASES):
                served_cases.append(runtime.cases.get(actor, row["case_id"]))
        except Exception:  # noqa: BLE001 - nothing more of the cases is served
            pass
        tools = open_tools(actor)
        try:
            answered = 0
            for value, objects in values.items():
                views = [call(tools, gated, "plx.object", {"object_id": o}) for o in objects]
                shown = [v for v in views if isinstance(v, Mapping) and v.get("object")]
                if not shown:
                    continue
                out["po_reference_scanned"] += 1
                if po_like(value):
                    out["po_like_served"] += int(any(echoes(v, value) for v in shown))
                    continue
                results = {"object": [(v, None) for v in shown],
                           "case": [(c, None) for c in served_cases]}
                if finds:
                    # The value asked for whole (the caller's own words given back are no
                    # echo) and by its first word, which finds it without stating it.
                    words = value.split()
                    asks = [value] + words[:1] * (len(words) > 1)
                    results["find"] = [(call(tools, gated, "plx.find", {"query": ask}), ask)
                                       for ask in asks]
                root = next((o for o in objects if o.startswith(PO_ROOT_TYPE + ":")), None)
                if answers and root is not None and answered < MAX_ANSWER_PROBES:
                    answered += 1
                    results["answer"] = [(call(tools, gated, "plx.investigate",
                                               {"family": "F2", "root": root}), None)]
                for where, served in results.items():
                    count = sum(echoes(result, value, asked=ask) for result, ask in served
                                if result is not None)
                    out["po_probed_by"][where] += sum(r is not None for r, _ in served)
                    out["echoed_by"][where] += count
                    out["echoed_non_po"] += count
        finally:
            close = getattr(tools, "close", None)
            if close is not None:
                close()
    return out


# ------------------------------------------------------------------ readers

def readers_from_snapshot(path: Path, tenant_id: str, directory: Any = None
                          ) -> list[dict[str, Any]]:
    """Active Portal snapshot users of the policy's tenant, with the identities and
    clearance inputs the oracle compares (directory read when the host has one)."""
    from operating.employee_permissions import EmployeePrincipal, PORTAL_DEPARTMENT_CODES

    doc = json.loads(path.read_bytes())
    out = []
    for user in doc.get("users", []) if isinstance(doc, dict) else []:
        oid = str(user.get("entraObjectId") or "").lower()
        if not user.get("isActive") or not _GUID.match(oid) or str(
                user.get("entraTenantId") or "").lower() != tenant_id.lower():
            continue
        ids = set(_lower([oid, user.get("email")]))
        entra = frozenset()
        if directory is not None:
            try:
                read = directory(EmployeePrincipal(tenant_id, oid))
            except Exception:  # noqa: BLE001 - no directory read: no directory identity
                read = None
            if getattr(read, "status", None) == "read":
                ids |= _lower([read.user_principal_name, read.mail])
                ids |= {"group:" + g.lower() for g in read.group_ids}
                code = PORTAL_DEPARTMENT_CODES.get(str(read.department or "").casefold())
                entra = frozenset({code} if code else ())
        portal = frozenset(m["code"] for m in user.get("departmentMemberships") or []
                           if isinstance(m, dict) and m.get("departmentActive") is True
                           and m.get("provenance") in ("PORTAL", "PROJECTED"))
        out.append({"oid": oid, "identities": frozenset(ids), "entra_codes": entra,
                    "portal_codes": portal})
    return sorted(out, key=lambda r: _digest(r["oid"]))[:MAX_READERS]


# ------------------------------------------------------------------ main

def _copy(source: Path, target: Path) -> None:
    """A consistent copy of a host store (read through ``mode=ro``), when it exists."""
    held = _open(source)
    if held is None:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    copy = sqlite3.connect(target)
    try:
        held.backup(copy)
    finally:
        copy.close()
        held.close()


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"check": CHECK, "ok": False, "blocked_reason": reason, "evidence_label": EVIDENCE,
            "requests_sent": 0, **extra}


def summary(environ: Mapping[str, str], *, directory: Any = None, mail_scope: Any = None,
            default_config: str | None = None) -> dict[str, Any]:
    """``directory`` / ``mail_scope`` (tests): the Entra directory provider and the case
    mail scope; live: the host's (``operating.host.directory_provider``, built-in scope).
    ``default_config``: the host config read when the environment names none."""
    own = environ.get("PLX_WORKBENCH_HOST_CONFIG")
    out = _summary(environ, own or default_config, directory, mail_scope)
    if own or default_config:
        out["host_config_from"] = "environment" if own else "default"
    return out


def _summary(environ: Mapping[str, str], config_text: str | None, directory: Any,
             mail_scope: Any) -> dict[str, Any]:
    from integrations.live_source_guard import LiveSourceAudience
    from integrations.people_exclusion import PeopleExclusion
    from operating import host as host_module
    from integrations import capture_mail
    from integrations import capture_sharepoint as cs
    from operating.case_runtime import (CASE_RUNTIME_DIR, CASES_FILE, MIRROR_FILE, CaseRuntime,
                                        LiveReaders)
    from operating.case_tools_host import CaseCaptures, serving_tools
    from operating.employee_permissions import (EmployeePermissionPolicy,
                                               EmployeePermissionResolver, PortalAccessSnapshot)

    state_text = environ.get("PLX_WORKBENCH_STATE_DIR")
    if not state_text:
        return _blocked("state_dir_unset")
    if not config_text:
        return _blocked("host_config_unset")
    state = Path(state_text).expanduser()
    if not state.is_dir():
        return _blocked("state_dir_absent")
    try:
        config = host_module.HostConfig.from_file(Path(config_text).expanduser())
    except (host_module.HostError, OSError, ValueError):
        return _blocked("host_config_invalid")
    exclusion = PeopleExclusion.from_env(environ)
    if exclusion.unavailable:
        return _blocked(exclusion.unavailable)
    if config.portal_access is None:
        return _blocked("portal_role_tables_unshared")
    if config.employee_policy is None:
        return _blocked("employee_policy_unset")
    if config.live_audience is None:
        return _blocked("live_audience_unset")
    try:
        policy = EmployeePermissionPolicy.from_file(config.employee_policy)
        portal = PortalAccessSnapshot(config.portal_access)
        audience = LiveSourceAudience.from_file(config.live_audience)
    except (OSError, ValueError, UnicodeDecodeError):
        return _blocked("host_inputs_invalid")
    if directory is None and config.entra_directory:
        try:
            directory = host_module.directory_provider(SimpleNamespace(
                config=config, environ=environ, sender=None))
        except host_module.HostError:
            return _blocked("entra_directory_credentials_unavailable")
    readers = readers_from_snapshot(config.portal_access, policy.entra_tenant_id, directory)
    if not readers:
        return _blocked("readers_absent")
    if mail_scope is None:
        from operating.case_mail_scope import MailScope
        domains = MailScope.builtin().internal_domains
    else:
        domains = mail_scope.internal_domains
    found, held, unresolved = targets(state, exclusion, domains)
    held_cases = state / CASE_RUNTIME_DIR / CASES_FILE
    items = stored_items(held_cases)
    projection = host_module.projection_path(state)
    values = po_reference_values(projection)
    resolver = EmployeePermissionResolver(policy, portal, directory=directory)
    with tempfile.TemporaryDirectory() as tmp:
        own = Path(tmp)
        if (state / "teams-capture").is_dir():
            (own / "teams-capture").symlink_to((state / "teams-capture").resolve(),
                                               target_is_directory=True)
        # The stored cases and mirror rows, copied: the runtime opens its stores to write.
        for name in (CASES_FILE, MIRROR_FILE):
            _copy(state / CASE_RUNTIME_DIR / name, own / CASE_RUNTIME_DIR / name)
        runtime = CaseRuntime(own, client=None, audience=config.mirror_audience,
                              tenant=config.tenant, live=LiveReaders(audience),
                              employee_permissions=resolver, require_employee=True,
                              mail_scope=mail_scope, people_exclusion=exclusion)

        def open_tools(actor: Any) -> Any:
            # The ontology host a session is served (``Host.served_tools``), read-only.
            return serving_tools(
                runtime, actor, store_path=projection,
                mail_capture_dir=state if capture_mail.store_exists(state) else None,
                sharepoint_capture_dir=state if cs.store_exists(state) else None,
                portal_captured="portal" in config.sources,
                knowledge_namespaces=config.knowledge_namespaces,
                marking_grants=config.marking_grants)

        try:
            runtime.captures = CaseCaptures(runtime, mail_capture_dir=state,
                                            sharepoint_capture_dir=state)
            proof = prove(runtime, config.tenant, readers, found)
            cases = prove_cases(runtime, config.tenant, readers, items)
            po = prove_po(runtime, config.tenant, readers, values, open_tools)
        finally:
            runtime.close()
    proof["denied_leaks"] += cases["unresolved_served"]
    ok = (proof["denied_leaks"] == 0 and proof["untyped"] == 0 and held == 0
          and cases["case_items_unreadable"] == 0 and po["echoed_non_po"] == 0)
    out = {"check": CHECK, "ok": ok, "evidence_label": EVIDENCE, "requests_sent": 0,
           "excluded_held": held, "excluded_people": exclusion.count,
           "senders_unresolved": unresolved, **proof, **cases, **po}
    if ok:
        absent = [f"{k}_requests_absent" for k in REQUEST_KINDS
                  if not proof["kinds"][k]["requests"]]
        absent += ["case_store_absent"] if not held_cases.is_file() else (
            ["case_store_unread"] if not cases["cases_read"] else
            ["case_unresolved_citations_absent"] if not cases["case_items_unresolved"] else [])
        absent += ["projection_absent"] if not projection.is_file() else (
            ["po_reference_values_absent"] if not values else
            ["po_reference_unserved"] if not po["po_reference_scanned"] else [])
        if absent:
            out["blocked_reason"] = absent[0]
    return out


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         directory: Any = None, mail_scope: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--default-host-config", default=None,
                        help="host config read when PLX_WORKBENCH_HOST_CONFIG is unset")
    args = parser.parse_args(argv)
    out = summary(os.environ if environ is None else environ, directory=directory,
                  mail_scope=mail_scope, default_config=args.default_host_config)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["ok"] or "blocked_reason" in out else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
