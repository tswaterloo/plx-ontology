"""Live check ``access.control_proof``: no employee is served another person's mail or Teams
content, a finance-only file to a reader without finance clearance, or anything of an
excluded person (``denied_leaks`` 0 with real employee identities).

``python3 -m scripts.access_control_proof`` reads the host the way its sessions serve it
(``PLX_WORKBENCH_HOST_CONFIG``, ``PLX_WORKBENCH_STATE_DIR``): the mail, Teams and SharePoint
captures, the employee policy and the Portal access snapshot (the in-repo role model,
``plx-portal-access-snapshot/v1``, since the reporting API does not serve the Portal role
tables: docs/reference/permissions.md blocker 3). Every active snapshot user of the policy's tenant
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

A reader the oracle admits is a control (``controls_served`` shows the guards are not
simply refusing everything). Output: counts and reason codes only, never an identifier.
A refusal without a typed code counts ``untyped`` and fails, as any leak does.
"""

from __future__ import annotations

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
                sender = _guids((payload.get("row") or {}).get("from"))
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

def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return {"check": CHECK, "ok": False, "blocked_reason": reason, "evidence_label": EVIDENCE,
            "requests_sent": 0, **extra}


def summary(environ: Mapping[str, str], *, directory: Any = None, mail_scope: Any = None
            ) -> dict[str, Any]:
    """``directory`` / ``mail_scope`` (tests): the Entra directory provider and the case
    mail scope; live: the host's (``operating.host.directory_provider``, built-in scope)."""
    from integrations.live_source_guard import LiveSourceAudience
    from integrations.people_exclusion import PeopleExclusion
    from operating import host as host_module
    from operating.case_runtime import CaseRuntime, LiveReaders
    from operating.case_tools_host import CaseCaptures
    from operating.employee_permissions import (EmployeePermissionPolicy,
                                               EmployeePermissionResolver, PortalAccessSnapshot)

    state_text, config_text = (environ.get("PLX_WORKBENCH_STATE_DIR"),
                               environ.get("PLX_WORKBENCH_HOST_CONFIG"))
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
    resolver = EmployeePermissionResolver(policy, portal, directory=directory)
    with tempfile.TemporaryDirectory() as tmp:
        own = Path(tmp)
        if (state / "teams-capture").is_dir():
            (own / "teams-capture").symlink_to((state / "teams-capture").resolve(),
                                               target_is_directory=True)
        runtime = CaseRuntime(own, client=None, audience=config.mirror_audience,
                              tenant=config.tenant, live=LiveReaders(audience),
                              employee_permissions=resolver, require_employee=True,
                              mail_scope=mail_scope, people_exclusion=exclusion)
        try:
            runtime.captures = CaseCaptures(runtime, mail_capture_dir=state,
                                            sharepoint_capture_dir=state)
            proof = prove(runtime, config.tenant, readers, found)
        finally:
            runtime.close()
    ok = proof["denied_leaks"] == 0 and proof["untyped"] == 0 and held == 0
    out = {"check": CHECK, "ok": ok, "evidence_label": EVIDENCE, "requests_sent": 0,
           "excluded_held": held, "excluded_people": exclusion.count,
           "senders_unresolved": unresolved, **proof}
    if ok:
        absent = [k for k in REQUEST_KINDS if not proof["kinds"][k]["requests"]]
        if absent:
            out["blocked_reason"] = f"{absent[0]}_requests_absent"
    return out


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         directory: Any = None, mail_scope: Any = None) -> int:
    out = summary(os.environ if environ is None else environ, directory=directory,
                  mail_scope=mail_scope)
    print(json.dumps(out, sort_keys=True))
    return 0 if out["ok"] or "blocked_reason" in out else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
