"""The Business Central twin: BC's API v2.0 (OData v4) for one environment, per company,
with etags, built from gen-bc's facts.

Generated, not written per entity set: the pinned ``$metadata``
(``ontology/schema/generated/meta/bc_metadata.json``, ``ontology.generator.bc``) gives the
entity sets the company navigates to (``companies`` contained collections), each set's key
and properties (Edm type through the generator's ``VALUE_TYPES``, nullability; a stream is
never a value) and the line collections a document contains (its line set's parent field as
``integrations.live_source_grammar`` names it). A twin cell has the role a live BC cell has
(``injection_guard.cited_role`` over the compiled schema's BC bindings and the identifier
carriers): an ``externalDocumentNumber`` that two kinds carry is ambiguous here too.

Records live as typed relations ``fixture.bc_<entity set>`` of the twin's own
``IsolatedOwnerSystem`` (``owner``; reused, not a second engine): key ``{company_id, <key
property>}``, owner-managed ``version``, which is the record's etag. So the catalog's
isolated-owner types (``fixture.record.set_field``, ``set_cited_value``) target a BC twin
record through the action path unchanged (``FixtureDispatcher`` over ``owner``; a
``field_equals`` precondition on ``version`` becomes the owner's compare-and-set, the
If-Match), and what they write is what the OData side answers. The owner's own resolver
reads nothing (``readers`` empty): the read view is ``resolve``.

OData, as ``TwinTransport`` sends it (``OwnerRequest``; paths below the API root
``/v2.0/{tenant}/{environment}/api/v2.0``): ``GET /companies``, ``/companies(<id>)``,
``/companies(<id>)/<set>``, ``/companies(<id>)/<set>(<id>)``,
``/companies(<id>)/<set>(<id>)/<lines>``, ``/companies(<id>)/<set>(<id>)/<lines>(<id>)``
``PATCH`` of a record or of one line at those two keyed paths, and ``DELETE`` of a line
(204; upstream plx-customer-portal@5cb95e1 outbox-drain.ts:828 deletes surplus quote lines with ``If-Match: *``).

* Company scope: a record exists only under its own company, a line only under its own
  document. Another company's id under this company, or another document's line under this
  document, is a 404 (``BadRequest_NotFound``), on a read and on a PATCH (nothing
  applied); a listing holds only this company's records.
* Etags: ``If-Match`` compares weak ``W/`` tags by exact string, as BC does (not RFC 7232
  weak comparison). Every record carries ``@odata.etag`` (``W/"..."``, new on every write by anyone).
  ``If-Match: *`` matches any version (OData) and is what the Portal's BC outbox sends on
  every PATCH, of a header and of a line (``ANY_ETAG``, cited there), so the twin applies
  it as BC does: last writer wins, another user's save is overwritten without notice.
  If-Match is a comma-separated list of entity tags (RFC 7232): any listed tag or ``*``
  matches. The other answers serve the Workbench owner's own stricter path only (``ODataDoor``
  sends the etag of the version its case cited): another etag is 412
  (``Request_EntityChanged``), an absent If-Match 428, nothing applied. Both codes are the
  twin's reading of BC (live-unverified), and nothing here proves real BC write safety:
  no upstream writer relies on them.
* A PATCH body names properties of the entity type only (400 otherwise), never a key
  property, a line's document field nor a computed one: the pin's ``Core.Computed``
  (``ontology.generator.bc``; the live document declares none), the grammar's calculated
  fields (``BC_CALCULATED_FIELDS``) and ``lastModifiedDateTime``; each value of its Edm type
  (a date a calendar day); the
  answer is the updated entity and ``lastModifiedDateTime`` moves.
* Fails closed: a query option (none is emulated), another method (405), an unknown set or
  navigation (404).

The action path writes a twin record through this OData too: the isolated owner's
``door`` is ``ODataDoor`` (``TwinWorld`` sets it), so the dispatch of an isolated-owner
type is a GET of the record and one PATCH with ``If-Match`` the etag of the version the
case cited; a 412 is the owner's ``etag_mismatch``, nothing written (Workbench owner only;
the Portal's outbox would have overwritten).

Limits (stated, not emulated): a property BC computes is refused only as far as those
declare it (BC's live $metadata marks nothing read-only); no BC business rule (a
released document's status) is checked; on the owner contract a Guid, Decimal or instant
is typed ``string`` (the isolated owner's ``FIELD_TYPES``), checked as Edm only on OData
and at seeding. The status codes and
error codes are the twin's reading of BC (live-unverified). The OData side serves one
caller at a time (an in-process test twin): a PATCH compares the etag, then writes.

Read view: ``resolve`` for the twin's ``fixture://`` handles, checked against a per-company
audience (``LiveSourceAudience``, resources ``bc:twin:<company>:<set>`` as
``integrations.bc_read`` names them; a line set also needs its header set's). A company's
own record is ``bc:twin:<company>:companies``, granted to that company's readers only, as
the live capture keeps it (``integrations/bc_read.py:1043``, ``BcCapture._capture_company``):
a reader of one company never resolves another's. Evidence label: ``isolated-fixture``;
never live.
"""

from __future__ import annotations

import base64
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import unquote

from integrations.live_source_grammar import (BC_CALCULATED_FIELDS, BC_LINE_PARENT_FIELD,
                                              BC_MODIFIED_FIELD)
from integrations.live_source_guard import LiveSourceAudience
from ontology.core import Actor
from ontology.generator.bc import NOT_A_VALUE, PIN_PATH, VALUE_TYPES
from ontology.generator.build import snake
from operating import injection_guard
from operating.action_adapters import OwnerRequest
from operating.action_catalog import role_declared, value_fits_type
from operating.case_verification import field_status
from operating.cases import CitationError
from operating.goal2_exec_owner import (EVIDENCE_LABEL, IsolatedOwnerSystem, NothingSent,
                                        OwnerRejected, parse_handle)
from operating.goal2_exec_owner import handle as owner_handle

from ..portal import TwinError

ENVIRONMENT = "twin"
RELATION_PREFIX = "fixture.bc_"
COMPANY_SET = "companies"
COMPANY_KEY = "company_id"
VERSION_FIELD = "version"
CAPTURES = Path(__file__).with_name("fixtures") / "bc_captures.json"
CAPTURES_FORMAT = "plx-twin-bc-captures/v1"
# The generator's value type -> the isolated owner's field type (``FIELD_TYPES``): a
# decimal and an instant are text there, as ``integrations.bc_read`` stores them.
OWNER_TYPES = {"text": "string", "integer": "integer", "decimal": "string",
               "boolean": "boolean", "date": "date", "timestamp": "string"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}$")
# The If-Match every upstream BC PATCH sends (plx-customer-portal@549f6f0e
# portal/src/lib/business-central/outbound.ts:184-204, the drain's plan for a record;
# outbox-drain.ts:807 a sales quote line, 892 a sales order header, 918 a sales order
# line, 1054 a purchase order line, 1485 the plan's): it skips BC's concurrency check.
ANY_ETAG = "*"
_SEGMENT = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9]{0,63})(?:\((?P<key>[^()/]{1,64})\))?$")


@dataclass(frozen=True)
class EntitySet:
    name: str
    relation: str
    key: str
    properties: Mapping[str, tuple[str, str, bool]]   # name -> (value type, Edm, nullable)
    contains: Mapping[str, str]                       # contained navigation -> set
    parent: str | None                                # the header set of a line set
    parent_field: str | None
    computed: frozenset[str]                          # never written by a client


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def _value_type(edm: str) -> str:
    if edm in VALUE_TYPES:
        return VALUE_TYPES[edm]
    if edm.startswith("enum:"):   # enum members stay text (their JSON spelling is not pinned)
        return "text"
    raise TwinError("edm_type_not_emulated:" + edm)


def entity_sets(pin_path: Path = PIN_PATH) -> dict[str, EntitySet]:
    """The served sets of the pin: ``companies`` and every set it contains, with the line
    sets their documents contain (a ``lines``-scoped set is served only there)."""
    sets = _load(pin_path)["entity_sets"]
    by_type = {spec["entity_type"]: name for name, spec in sets.items()}

    def contained(name: str) -> dict[str, str]:
        return {nav["name"]: by_type[nav["target"]] for nav in sets[name]["navigation"]
                if nav["contained"] and nav["collection"] and nav["target"] in by_type}

    served = {COMPANY_SET} | {name for name in contained(COMPANY_SET).values()
                              if sets[name]["scope"] == "company"}
    for name in list(served):
        served |= set(contained(name).values()) - {COMPANY_SET}
    out = {}
    for name in sorted(served):
        spec = sets[name]
        if len(spec["key"]) != 1:
            raise TwinError("entity_set_key_not_single:" + name)
        properties = {prop["name"]: (_value_type(prop["type"]), prop["type"], prop["nullable"])
                      for prop in spec["properties"] if prop["type"] not in NOT_A_VALUE}
        if {VERSION_FIELD, COMPANY_KEY} & set(properties):
            raise TwinError("property_shadows_twin_field:" + name)
        parents = [parent for parent in spec["parents"]
                   if name in contained(parent).values()]
        parent = parents[0] if len(parents) == 1 else None
        parent_field = BC_LINE_PARENT_FIELD.get(name, "documentId") if parent else None
        if parent_field is not None and parent_field not in properties:
            raise TwinError("line_set_without_parent_field:" + name)
        # BC's live $metadata declares no Core.Computed (pin 09a82605): the grammar's
        # calculated fields and lastModifiedDateTime are read-only whatever the pin says.
        computed = frozenset(prop["name"] for prop in spec["properties"]
                             if prop.get("computed") is True and prop["name"] in properties
                             ) | (BC_CALCULATED_FIELDS.get(name, frozenset())
                                  | {BC_MODIFIED_FIELD}) & set(properties)
        out[name] = EntitySet(name, RELATION_PREFIX + snake(name), spec["key"][0], properties,
                              contained(name), parent, parent_field, computed)
    return out


def etag(entity_set: str, key: Mapping[str, Any], version: int) -> str:
    """An opaque weak etag of one record version (BC's shape: ``W/"<base64>"``)."""
    digest = hashlib.sha256(json.dumps([entity_set, dict(key)], sort_keys=True)
                            .encode("utf-8")).hexdigest()[:16]
    return 'W/"' + base64.b64encode(f"'{digest};{version};'".encode()).decode() + '"'


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    return {"status": status, "body": {"error": {"code": code, "message": message}}}


NOT_FOUND = ("BadRequest_NotFound", "The resource was not found in this company")


def _fit(value_type: str, edm: str, value: Any) -> tuple[bool, Any]:
    """``(fits, stored value)`` of one value of an Edm type (None: nullable checked by the
    caller). A decimal is stored as its decimal text, a Guid lower-cased."""
    if value is None:
        return True, None
    if value_type == "text":
        if not isinstance(value, str) or (edm == "Edm.Guid" and not _GUID.fullmatch(value)):
            return False, None
        return True, value.lower() if edm == "Edm.Guid" else value
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool), value
    if value_type == "boolean":
        return isinstance(value, bool), value
    if value_type == "decimal":
        if isinstance(value, bool) or not isinstance(value, (int, Decimal, str)):
            return False, None
        try:
            number = Decimal(value) if not isinstance(value, Decimal) else value
        except InvalidOperation:
            return False, None
        return number.is_finite(), str(number)
    if value_type == "date":
        return value_fits_type("date", value), value
    if value_type == "timestamp" and isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False, None
        return moment.tzinfo is not None, value
    return False, None


class BcTwin:
    """``readers``: company id -> the Workbench principals that may read its records (the
    audience the twin's citations are checked against); ``tenant``: the readers' tenant."""

    SYSTEM = "bc"

    def __init__(self, path: str | Path, *, readers: Mapping[str, Iterable[str]], tenant: str,
                 clock: Callable[[], datetime] | None = None,
                 pin_path: Path = PIN_PATH) -> None:
        self.sets = entity_sets(pin_path)
        self.owner = IsolatedOwnerSystem(path, readers=frozenset(), field_roles={})
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        companies = {_company_id(company): list(principals)
                     for company, principals in readers.items()}
        self.audience = LiveSourceAudience(
            {f"bc:{ENVIRONMENT}:{company}:*": principals
             for company, principals in companies.items()},
            tenant=tenant, default_classification="internal")
        self.writes = 0
        for spec in self.sets.values():
            self.owner.define(spec.relation, {name: OWNER_TYPES[kind] for name, (kind, _e, _n)
                                              in spec.properties.items()},
                              version_field=VERSION_FIELD)
        # (entity set, company or None) -> record keys in seeding order
        self._keys: dict[tuple[str, str | None], list[str]] = {}

    @classmethod
    def from_captures(cls, path: str | Path, document: Mapping[str, Any], *, tenant: str,
                      clock: Callable[[], datetime] | None = None) -> "BcTwin":
        """A twin whose audience and records are a captures document's
        (``load_captures``)."""
        twin = cls(path, readers={company["row"]["id"]: company["workbench_readers"]
                                  for company in document["companies"]},
                   tenant=tenant, clock=clock)
        twin.seed_captures(document)
        return twin

    # ---- seeding (fixture captures) -------------------------------------

    def seed_captures(self, document: Mapping[str, Any]) -> list[str]:
        handles = [self.seed(None, COMPANY_SET, company["row"])
                   for company in document["companies"]]
        for record in document["records"]:
            handles.append(self.seed(record["company_id"], record["entity_set"], record["row"]))
        return handles

    def seed(self, company_id: str | None, entity_set: str, row: Mapping[str, Any]) -> str:
        """One record as BC holds it (``row``: its properties); its ``fixture://`` handle."""
        spec = self.sets.get(entity_set)
        if spec is None:
            raise TwinError("entity_set_not_served:" + str(entity_set))
        if (company_id is None) != (entity_set == COMPANY_SET):
            raise TwinError("company_scope_mismatch:" + entity_set)
        if company_id is not None:
            company_id = _company_id(company_id)
            if self._record(COMPANY_SET, None, company_id) is None:
                raise TwinError("company_not_seeded")
        unknown = sorted(set(row) - set(spec.properties))
        if unknown:
            raise TwinError("property_not_in_metadata:" + ",".join(unknown))
        stored = {}
        for name, (kind, edm, nullable) in spec.properties.items():
            fits, value = _fit(kind, edm, row.get(name))
            if not fits or (value is None and not nullable):
                raise TwinError("property_value_invalid:" + name)
            stored[name] = value
        if spec.parent is not None and self._record(
                spec.parent, company_id, stored[spec.parent_field]) is None:
            raise TwinError("line_without_its_document:" + entity_set)
        key = self._key(spec, company_id, stored[spec.key])
        self._keys.setdefault((entity_set, company_id), []).append(stored[spec.key])
        return self.owner.seed(spec.relation, key, stored)

    def _key(self, spec: EntitySet, company_id: str | None, record_id: str) -> dict[str, str]:
        return ({spec.key: record_id} if company_id is None
                else {COMPANY_KEY: company_id, spec.key: record_id})

    def _record(self, entity_set: str, company_id: str | None,
                record_id: Any) -> dict[str, Any] | None:
        spec = self.sets[entity_set]
        if not isinstance(record_id, str) or not _GUID.fullmatch(record_id):
            return None
        return self.owner.record(spec.relation, self._key(spec, company_id, record_id.lower()))

    def companies(self) -> list[str]:
        return list(self._keys.get((COMPANY_SET, None), ()))

    def handles(self, company_id: str, entity_set: str) -> list[str]:
        """The ``fixture://`` handles of one company's records of a set, in seeding order."""
        spec = self.sets[entity_set]
        return [owner_handle(spec.relation, self._key(spec, company_id, record_id))
                for record_id in self._keys.get((entity_set, _company_id(company_id)), ())]

    # ---- OData (TwinTransport) ------------------------------------------

    def handle(self, request: Any) -> dict[str, Any]:
        """Answer one ``OwnerRequest`` as BC's API v2.0 would (module docstring)."""
        raw = request.path
        if not isinstance(raw, str) or not raw.startswith("/") or "?" in raw or "#" in raw:
            return _error(400, "BadRequest", "Query options are not emulated by the twin")
        segments = [_SEGMENT.fullmatch(part) for part in unquote(raw)[1:].split("/")]
        if any(found is None for found in segments) or segments[0]["name"] != COMPANY_SET:
            return _error(404, *NOT_FOUND)
        if len(segments) == 1:
            if request.method != "GET":
                return _error(405, "BadRequest_MethodNotAllowed", "Not emulated by the twin")
            if segments[0]["key"] is None:
                return {"status": 200, "body": {"value": self._list(COMPANY_SET, None)}}
            return self._get(COMPANY_SET, None, segments[0]["key"])
        company = segments[0]["key"]
        if (company is None or len(segments) > 3
                or self._record(COMPANY_SET, None, company) is None):
            return _error(404, *NOT_FOUND)
        company = company.lower()
        header = segments[1]
        spec = self.sets.get(header["name"])
        if spec is None or spec.name == COMPANY_SET or (
                spec.name not in self.sets[COMPANY_SET].contains.values()):
            return _error(404, *NOT_FOUND)
        if len(segments) == 3:
            line_set, line_key = spec.contains.get(segments[2]["name"]), segments[2]["key"]
            if (header["key"] is None or line_set is None
                    or self._record(spec.name, company, header["key"]) is None):
                return _error(404, *NOT_FOUND)
            parent_field = self.sets[line_set].parent_field
            if line_key is None:
                if request.method != "GET":
                    return _error(405, "BadRequest_MethodNotAllowed", "Not emulated by the twin")
                return {"status": 200, "body": {"value": [
                    row for row in self._list(line_set, company)
                    if row.get(parent_field) == header["key"].lower()]}}
            line = self._record(line_set, company, line_key)
            if line is None or line[parent_field] != header["key"].lower():
                return _error(404, *NOT_FOUND)
            if request.method == "GET":
                return self._get(line_set, company, line_key)
            if request.method == "PATCH":
                return self._patch(self.sets[line_set], company, line_key, request)
            if request.method == "DELETE":
                return self._delete(self.sets[line_set], company, line_key, request)
            return _error(405, "BadRequest_MethodNotAllowed", "Not emulated by the twin")
        if request.method == "GET":
            if header["key"] is None:
                return {"status": 200, "body": {"value": self._list(spec.name, company)}}
            return self._get(spec.name, company, header["key"])
        if request.method == "PATCH" and header["key"] is not None:
            return self._patch(spec, company, header["key"], request)
        return _error(405, "BadRequest_MethodNotAllowed", "Not emulated by the twin")

    def _render(self, spec: EntitySet, company_id: str | None,
                row: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {"@odata.etag": etag(spec.name, self._key(
            spec, company_id, row[spec.key]), row[VERSION_FIELD])}
        for name, (kind, _edm, _nullable) in spec.properties.items():
            value = row.get(name)
            out[name] = Decimal(value) if kind == "decimal" and value is not None else value
        return out

    def _list(self, entity_set: str, company_id: str | None) -> list[dict[str, Any]]:
        spec = self.sets[entity_set]
        return [self._render(spec, company_id, row) for row in (
            self._record(entity_set, company_id, record_id)
            for record_id in self._keys.get((entity_set, company_id), ())) if row is not None]

    def _get(self, entity_set: str, company_id: str | None, record_id: str) -> dict[str, Any]:
        row = self._record(entity_set, company_id, record_id)
        if row is None:
            return _error(404, *NOT_FOUND)
        return {"status": 200, "body": self._render(self.sets[entity_set], company_id, row)}

    def _precondition(self, spec: EntitySet, company_id: str, row: Mapping[str, Any],
                      request: Any) -> dict[str, Any] | None:
        """The error answer when If-Match (a list of entity tags, or ``*``) does not hold."""
        headers = {str(name).lower(): value for name, value in (request.headers or {}).items()}
        if_match = headers.get("if-match")
        if not isinstance(if_match, str) or not if_match.strip():
            return _error(428, "BadRequest_PreconditionRequired",
                          "An If-Match header is required to change a record")
        current = etag(spec.name, self._key(spec, company_id, row[spec.key]), row[VERSION_FIELD])
        tags = [tag.strip() for tag in if_match.split(",")]
        if ANY_ETAG not in tags and current not in tags:
            return _error(412, "Request_EntityChanged",
                          "The record was changed since it was read; read it again")
        return None

    def _delete(self, spec: EntitySet, company_id: str, record_id: str,
                request: Any) -> dict[str, Any]:
        row = self._record(spec.name, company_id, record_id)
        if row is None:
            return _error(404, *NOT_FOUND)
        refused = self._precondition(spec, company_id, row, request)
        if refused:
            return refused
        self.owner.delete_by_other_writer(spec.relation, self._key(spec, company_id, row[spec.key]))
        self._keys[(spec.name, company_id)].remove(row[spec.key])
        self.writes += 1
        return {"status": 204, "body": None}

    def _patch(self, spec: EntitySet, company_id: str, record_id: str,
               request: Any) -> dict[str, Any]:
        row = self._record(spec.name, company_id, record_id)
        if row is None:
            return _error(404, *NOT_FOUND)
        refused = self._precondition(spec, company_id, row, request)
        if refused:
            return refused
        key = self._key(spec, company_id, row[spec.key])
        body = request.body
        if not isinstance(body, Mapping) or not body:
            return _error(400, "BadRequest", "The request body names no property")
        changes = {}
        for name, value in body.items():
            if (name not in spec.properties or name in (spec.key, spec.parent_field)
                    or name in spec.computed):
                return _error(400, "BadRequest_PropertyNotWritable",
                              "A property of the body is not writable on this entity")
            kind, edm, nullable = spec.properties[name]
            fits, stored = _fit(kind, edm, value)
            if not fits or (stored is None and not nullable):
                return _error(400, "BadRequest_InvalidValue",
                              "A property value does not fit its type")
            changes[name] = stored
        if "lastModifiedDateTime" in spec.properties:
            changes["lastModifiedDateTime"] = (self.clock().astimezone(timezone.utc)
                                               .isoformat(timespec="seconds")
                                               .replace("+00:00", "Z"))
        self.owner.update_by_other_writer(spec.relation, key, changes)
        self.writes += 1
        return {"status": 200, "body": self._render(
            spec, company_id, self.owner.record(spec.relation, key))}

    # ---- the read view --------------------------------------------------

    def holds(self, handle: Any) -> bool:
        """Whether ``handle`` names one of the twin's relations."""
        try:
            parsed = parse_handle(handle)
        except CitationError:
            return False
        return parsed is not None and parsed[0].startswith(RELATION_PREFIX)

    def _set_of(self, relation: str) -> EntitySet | None:
        return next((spec for spec in self.sets.values() if spec.relation == relation), None)

    def resolve(self, handle: str, actor: Actor) -> dict[str, Any]:
        """The case store's citation resolver for the twin's ``fixture://`` handles: the
        record if the actor's audience grants its company and set (default deny)."""
        parsed = parse_handle(handle) if isinstance(handle, str) else None
        spec = self._set_of(parsed[0]) if parsed is not None else None
        if spec is None:
            raise CitationError("not_a_bc_twin_handle")
        key = parsed[1]
        # A company's own record is its company's (resource bc:twin:<company>:companies).
        company = key.get(spec.key if spec.name == COMPANY_SET else COMPANY_KEY)
        if not isinstance(company, str) or not _GUID.fullmatch(company):
            raise CitationError("not visible")
        resources = [f"bc:{ENVIRONMENT}:{company}:{name}"
                     for name in (spec.name, spec.parent) if name is not None]
        if not all(self.audience.allows(actor, resource) for resource in resources):
            raise CitationError("not visible")
        row = self.owner.record(spec.relation, key)
        if row is None:
            raise CitationError("twin_row_absent")
        cite = f"bc://{ENVIRONMENT}/{company}/{spec.name}/{row[spec.key]}@twin"
        roles = {}
        for name in spec.properties:
            role = injection_guard.cited_role(cite, {"relation": spec.name, "row": row},
                                              "row." + name)
            if role is not None and role_declared(role):
                roles[name] = role
        return {"relation": spec.relation, "key": copy.deepcopy(key), "row": row,
                "evidence": EVIDENCE_LABEL, "field_roles": roles,
                "field_types": {**{name: OWNER_TYPES[kind] for name, (kind, _e, _n)
                                   in spec.properties.items()}, VERSION_FIELD: "integer"},
                "read_only_fields": [VERSION_FIELD, spec.key, *sorted(spec.computed)]}


class ODataDoor:
    """The ``door`` of the BC twin's isolated owner (``IsolatedOwnerSystem.door``): the
    action path's apply as BC's own OData through ``transport`` (``TwinTransport``). It
    needs the payload's ``field_equals`` on the version the case cited (that version's
    etag is the ``If-Match``; nothing else is ever sent), GETs the record, checks the
    payload's other conditions when the record is still at that version, then PATCHes
    the fields. This door is the Workbench owner's own path, stricter than the Portal's
    BC outbox (``ANY_ETAG``). A refusal is ``OwnerRejected`` (412: ``etag_mismatch``),
    nothing written; a failure before the PATCH (the GET answered otherwise or not at
    all) is ``NothingSent``: no write request left, so the owner settles the key not
    applied; another answer to the PATCH raises (never heard). ``answers``: (method,
    status, If-Match) sent."""

    def __init__(self, twin: BcTwin, transport: Any) -> None:
        self.twin, self.transport = twin, transport
        self.answers: list[tuple[str, int, str | None]] = []

    def _send(self, method: str, path: str, if_match: str | None = None,
              body: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        answer = self.transport.send(OwnerRequest(
            BcTwin.SYSTEM, method, path, {"If-Match": if_match} if if_match else {}, body))
        self.answers.append((method, answer["status"], if_match))
        return answer

    def __call__(self, payload: Mapping[str, Any]) -> None:
        spec = self.twin._set_of(payload.get("relation"))
        key = payload.get("key")
        if (spec is None or spec.name == COMPANY_SET or not isinstance(key, Mapping)
                or set(key) != {COMPANY_KEY, spec.key}):
            raise OwnerRejected("odata_target_not_a_company_record")
        expect = list(payload.get("expect") or ())
        cited = [item for item in expect if item.get("field") == VERSION_FIELD]
        if (len(cited) != 1 or cited[0].get("check") != "field_equals"
                or type(cited[0].get("value")) is not int):
            raise OwnerRejected("odata_if_match_needs_the_cited_version")
        if_match = etag(spec.name, key, cited[0]["value"])
        path = f"/companies({key[COMPANY_KEY]})/{spec.name}({key[spec.key]})"
        try:   # a GET only: whatever fails here, no write request was sent
            read = self._send("GET", path)
            if read["status"] == 404:
                raise OwnerRejected("target_absent")
            if read["status"] != 200:
                raise NothingSent(f"odata_answer_{read['status']}")
            if read["body"].get("@odata.etag") == if_match:   # else the PATCH answers 412
                for item in expect:
                    wanted = "empty" if item.get("check") == "field_empty" else "equals_expected"
                    if (item is not cited[0] and field_status(
                            read["body"], item["field"], item.get("value", ""))["status"]
                            != wanted):
                        raise OwnerRejected("conditional_write_conflict")
        except (OwnerRejected, NothingSent):
            raise
        except Exception as exc:  # noqa: BLE001 - the read failed before any PATCH
            raise NothingSent(f"odata_read_failed:{type(exc).__name__}") from exc
        written = self._send("PATCH", path, if_match, dict(payload.get("set") or {}))
        if written["status"] == 200:
            return
        if written["status"] == 412:
            raise OwnerRejected("etag_mismatch")
        if 400 <= written["status"] < 500:
            raise OwnerRejected(f"odata_{written['status']}:{written['body']['error']['code']}")
        raise RuntimeError(f"odata_answer_{written['status']}")


def _company_id(value: Any) -> str:
    if not isinstance(value, str) or not _GUID.fullmatch(value):
        raise TwinError("company_id_not_a_guid")
    return value.lower()


def load_captures(path: Path = CAPTURES) -> dict[str, Any]:
    document = _load(path)
    if document.get("format") != CAPTURES_FORMAT:
        raise TwinError("captures_format")
    return document


__all__ = ["ANY_ETAG", "BcTwin", "CAPTURES", "COMPANY_KEY", "ENVIRONMENT", "EntitySet",
           "ODataDoor", "RELATION_PREFIX", "VERSION_FIELD", "entity_sets", "etag",
           "load_captures"]
