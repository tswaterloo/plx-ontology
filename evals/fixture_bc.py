"""The fixture world's Business Central, served as BC API v2.0 to the GET-only BC client.

Test-only transport for the fixture dev baseline (``scripts.eval_dev_baseline fixture``):
``sender(world)`` answers the token request and the GETs ``integrations.bc_read`` sends
(companies, entity-set pages under ``Prefer: odata.maxpagesize`` with ``$skiptoken``,
the modified-since and posted-only (``status ne 'Draft'``) ``$filter``, ``$select``, a record by id, a document's lines nested or
filtered by ``documentId``) from ``world.bc`` / ``world.bc_lines``
(``evals/oracle/fixture_world.py``), so the fixture product captures the BC records the
oracle reads. Every other method is refused. Evidence label: injected-transport, never
live; the credentials in ``ENV`` are fixture values, never a secret.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from evals.oracle.fixture_world import COMPANIES
from integrations.live_source_grammar import BC_DOCUMENT_LINES, BC_LINE_PARENT_FIELD

TENANT_ID = "f1f1f1f1-0000-4000-8000-00000000f1f1"
ENV = {"BC_TENANT_ID": TENANT_ID, "BC_CLIENT_ID": "f2f2f2f2-0000-4000-8000-00000000f2f2",
       "BC_CLIENT_SECRET": "fixture-not-a-secret", "BC_ENVIRONMENT": "Production"}
BOOKS = {"production": {COMPANIES[0][0]: "fx_one", COMPANIES[1][0]: "fx_two"}}
HOST = "https://api.businesscentral.dynamics.com"
BASE = f"/v2.0/{TENANT_ID}/{ENV['BC_ENVIRONMENT']}/api/v2.0"
_PATH = re.compile(re.escape(BASE) + r"/companies\(([^)]+)\)/(\w+)(?:\(([^)]+)\)(?:/(\w+))?)?")
_NOT_FOUND = (404, {}, b'{"error":{"code":"BadRequest_NotFound"}}')


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _row(record: Mapping[str, Any]) -> dict[str, Any]:
    """A world record as BC returns it: its fields, the etag its citation pins, its time."""
    etag = unquote(record["citation"].rsplit("@", 1)[1])
    return {"@odata.etag": etag, "lastModifiedDateTime": record["last_modified"],
            **record["fields"]}


def sender(world: Any) -> Any:
    """The transport ``integrations.bc_read`` sends through (``capture_bc(..., sender=)``)."""
    headers_of: dict[tuple[str, str], list[dict[str, Any]]] = {}
    lines_of: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for entity_set, records in world.bc.items():
        for record in records:
            headers_of.setdefault((record["company_id"], entity_set), []).append(_row(record))
            line_set = BC_DOCUMENT_LINES.get(entity_set)
            if line_set:
                parent = BC_LINE_PARENT_FIELD.get(line_set, "documentId")
                lines_of[(record["company_id"], entity_set, record["id"])] = [
                    dict(_row(line), **{parent: record["id"]})
                    for line in world.bc_lines.get(record["id"], [])]

    def send(method: str, url: str, headers: Mapping[str, str], body: Any, timeout: Any,
             max_bytes: Any) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(url)
        if parts.hostname == "login.microsoftonline.com" and method == "POST":
            return 200, {}, json.dumps({"access_token": "fixture-token",
                                        "expires_in": 3599}).encode()
        if method != "GET" or parts.hostname != urlsplit(HOST).hostname:
            return 405, {}, b'{"error":{"code":"MethodNotAllowed"}}'
        path, query = unquote(parts.path), parse_qs(parts.query)
        if path == f"{BASE}/companies":
            return 200, {}, json.dumps({"value": [
                {"id": company, "name": name, "displayName": name, "@odata.etag": 'W/"c"'}
                for company, name in COMPANIES]}).encode()
        found = _PATH.fullmatch(path)
        if found is None:
            return _NOT_FOUND
        company, entity_set, document, line_set = found.groups()
        filters = query.get("$filter", [""])[0]
        if document is not None and line_set is None:  # a record by id
            rows = [r for r in headers_of.get((company, entity_set), []) if r["id"] == document]
            return (200, {}, json.dumps(rows[0]).encode()) if rows else _NOT_FOUND
        if document is not None:  # a document's lines, nested
            rows = lines_of.get((company, entity_set, document), [])
        elif entity_set in BC_DOCUMENT_LINES.values():  # lines filtered by their document
            field, _, value = filters.split(" ")
            rows = next((v for (c, h, d), v in lines_of.items() if c == company
                         and d == value and BC_DOCUMENT_LINES[h] == entity_set), [])
            filters = ""
        else:
            rows = headers_of.get((company, entity_set), [])
        for term in filters.split(" and ") if filters else ():
            if term == "status ne 'Draft'":  # the posted-only form (BC_POSTED_FILTER)
                rows = [r for r in rows if r.get("status") != "Draft"]
                continue
            field, op, instant = term.split(" ")  # the modified-since form the grammar builds
            if (field, op) != ("lastModifiedDateTime", "ge"):
                return 400, {}, b'{"error":{"code":"BadRequest"}}'
            rows = [r for r in rows if _instant(r["lastModifiedDateTime"]) >= _instant(instant)]
        if "$select" in query:
            names = query["$select"][0].split(",") + ["@odata.etag"]
            rows = [{k: r[k] for k in names if k in r} for r in rows]
        size = int(re.search(r"maxpagesize=(\d+)", headers.get("Prefer", "maxpagesize=100"))[1])
        offset = int(query.get("$skiptoken", ["0"])[0])
        page: dict[str, Any] = {"value": rows[offset:offset + size]}
        if offset + size < len(rows):
            kept = "".join(f"&{k}={quote(v[0], safe=',:')}" for k, v in sorted(query.items())
                           if k in ("$filter", "$select"))
            page["@odata.nextLink"] = f"{HOST}{parts.path}?{kept[1:]}&$skiptoken={offset + size}"
        return 200, {}, json.dumps(page).encode()
    return send
