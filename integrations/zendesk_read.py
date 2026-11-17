"""Zendesk Support reader: the order@ queue's tickets, their messages and attachment
metadata. GET only, through the one guarded transport; off until its settings exist.

Customer orders reach Petra through Zendesk (it manages the order mailbox for
distribution). A ticket whose original recipient is one of ``zendesk_scope.json``
``order_recipients`` is an order request: read incrementally with the cursor-based
incremental ticket export (``/api/v2/incremental/tickets/cursor.json``), each ticket's
comments from ``/api/v2/tickets/{id}/comments.json`` (cursor paged). A comment's
attachments are kept as metadata (id, file name, type, size, inline flag); the download
URL carries a token and a redirect to storage, so it is never kept and content is not
fetched until the live check has shown how the tenant serves it.

Settings (names only; values never printed, logged or committed): ``ZENDESK_SUBDOMAIN``
(the one tenant host label), ``ZENDESK_EMAIL`` and ``ZENDESK_API_TOKEN`` (API-token Basic
auth ``<email>/token``). All three are required; none present: the connector is disabled and
sends nothing.

Ontology (``BINDINGS``): a ticket is an order *request*, not a Sage/BC order: it binds to no
``SalesOrder`` row; its subject and message text are a mention document (an order or customer
PO number named in it is a lead link, resolved by the mention pipeline, never here), and its
requester address is the join key to a customer contact. Permissions: every record is in the
audience unit ``zendesk:order-queue``, default deny until Vince names the readers.

Paths and fields are from Zendesk's public API reference and unverified until the
``zendesk.order_queue`` live check has a result: label every output of this module
``isolated-fixture`` or ``injected-transport`` until then.
"""

from __future__ import annotations

import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import quote

from . import live_source_grammar as grammar
from . import capture_vendor as cv
from . import vendor_sweep as vs
from .http import SourceUnavailable
from .live_source_guard import GuardedTransport, Secret, jsonable

SOURCE = "zendesk"
SETTING_SUBDOMAIN, SETTING_EMAIL, SETTING_TOKEN = (
    "ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN")
REQUIRED_SETTINGS = (SETTING_SUBDOMAIN, SETTING_EMAIL, SETTING_TOKEN)
SCOPE_FILE = Path(__file__).with_name("zendesk_scope.json")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,60}[a-z0-9])?")
_ADDRESS = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}")
_PER_PAGE = 100

BINDINGS: dict[str, dict[str, Any]] = {
    "OrderRequestTicket": {
        "ontology_type": None, "properties": {}, "join_key": "requester_email",
        "why_unbound": "a ticket is an order request, not an order: the SalesOrder is made in "
                       "Sage/BC; the ticket's text is a mention document and its order or "
                       "customer PO numbers are lead links"},
    "OrderRequestMessage": {"ontology_type": None, "properties": {}, "join_key": "ticket_id",
                            "why_unbound": "a message belongs to its ticket"},
}


def load_scope(path: Path = SCOPE_FILE) -> dict[str, Any]:
    scope = json.loads(path.read_text(encoding="utf-8"))
    recipients = scope.get("order_recipients")
    if not (isinstance(recipients, list) and recipients
            and all(isinstance(r, str) and _ADDRESS.fullmatch(r) for r in recipients)):
        raise vs.SettingInvalid("order_recipients")
    scope["order_recipients"] = sorted({r.strip().lower() for r in recipients})
    return scope


def settings_from_env(environ: Mapping[str, str] | None = None
                      ) -> tuple[str, dict[str, str], Secret]:
    """``(tenant host, auth headers, the token Secret)``; the subdomain is one DNS label."""
    env = os.environ if environ is None else environ
    missing = vs.missing_settings(REQUIRED_SETTINGS, env)
    if missing:
        raise vs.CredentialsAbsent(missing)
    label = env[SETTING_SUBDOMAIN].strip().lower()
    if not _LABEL.fullmatch(label):
        raise vs.SettingInvalid(SETTING_SUBDOMAIN)
    email = env[SETTING_EMAIL].strip()
    if not _ADDRESS.fullmatch(email):
        raise vs.SettingInvalid(SETTING_EMAIL)
    pair = f"{email}/token:{env[SETTING_TOKEN].strip()}".encode()
    basic = Secret("Basic " + base64.b64encode(pair).decode("ascii"))
    return f"{label}.zendesk.com", {"Authorization": basic.reveal()}, basic


def _instant(value: Any) -> str | None:
    t = vs.text(value, 40)
    return t if t and re.fullmatch(r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9:.]{8,15}Z", t) else None


def _id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    s = str(value).strip()
    return s if re.fullmatch(r"[0-9]{1,20}", s) else None


def map_attachment(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping) or _id(raw.get("id")) is None:
        return None
    size = raw.get("size")
    return {"attachment_id": _id(raw["id"]), "file_name": vs.text(raw.get("file_name"), 255),
            "content_type": vs.text(raw.get("content_type"), 100),
            "size_bytes": size if isinstance(size, int) and not isinstance(size, bool)
            and size >= 0 else None,
            "inline": raw.get("inline") is True}      # never content_url: it carries a token


def map_ticket(raw: Mapping[str, Any], *, unit: str) -> dict[str, Any]:
    ticket_id = _id(raw.get("id"))
    if ticket_id is None:
        raise SourceUnavailable("zendesk ticket has no id")
    via = raw.get("via") if isinstance(raw.get("via"), Mapping) else {}
    src = via.get("source") if isinstance(via.get("source"), Mapping) else {}
    sender = src.get("from") if isinstance(src.get("from"), Mapping) else {}
    address = vs.text(sender.get("address"), 320)
    return {
        "record_type": "OrderRequestTicket", "ontology_type": None, "source": SOURCE,
        "citation": f"zendesk://ticket/{ticket_id}", "audience_unit": unit,
        "join_keys": {"ticket_id": ticket_id,
                      "requester_email": address.lower() if address and _ADDRESS.fullmatch(
                          address) else None,
                      "zendesk_requester_id": _id(raw.get("requester_id")),
                      "zendesk_organization_id": _id(raw.get("organization_id"))},
        "properties": {"subject": vs.text(raw.get("subject"), 300),
                       "status": vs.text(raw.get("status"), 20),
                       "priority": vs.text(raw.get("priority"), 20),
                       "created_at": _instant(raw.get("created_at")),
                       "updated_at": _instant(raw.get("updated_at")),
                       "channel": vs.text(via.get("channel"), 40),
                       "recipient": (vs.text(raw.get("recipient"), 320) or "").lower() or None,
                       "assignee_id": _id(raw.get("assignee_id"))},
        "text": vs.text(raw.get("description"), 20_000),    # a mention document
        "mention_document": True,
    }


def map_comment(ticket_id: str, raw: Mapping[str, Any], *, unit: str) -> dict[str, Any]:
    comment_id = _id(raw.get("id"))
    if comment_id is None:
        raise SourceUnavailable("zendesk comment has no id")
    attachments = [a for a in (map_attachment(x) for x in raw.get("attachments") or []
                               if isinstance(raw.get("attachments"), list)) if a]
    return {"record_type": "OrderRequestMessage", "ontology_type": None, "source": SOURCE,
            "citation": f"zendesk://ticket/{ticket_id}/comment/{comment_id}",
            "audience_unit": unit, "join_keys": {"ticket_id": ticket_id},
            "properties": {"comment_id": comment_id, "author_id": _id(raw.get("author_id")),
                           "public": raw.get("public") is True,
                           "created_at": _instant(raw.get("created_at"))},
            "text": vs.text(raw.get("plain_body") or raw.get("body"), 50_000),
            "mention_document": True, "attachments": attachments}


class ZendeskReader(vs.VendorClient):
    def __init__(self, host: str, auth_headers: Mapping[str, str], transport: GuardedTransport,
                 scope: Mapping[str, Any], **kw: Any) -> None:
        super().__init__(transport, auth_headers, **kw)
        self.host, self.scope = host, scope

    def _url(self, op: str, query: Mapping[str, str] | None = None, **params: str) -> str:
        return grammar.url(op, query, tenant_host=self.host, **params)

    def order_tickets(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """Tickets of the order queue changed since the saved cursor (or since the epoch of
        the first sweep's ``start_time``), with their comments. The cursor advances only to
        what this call returned complete; ``end_of_stream`` marks a complete sweep."""
        wanted = set(self.scope["order_recipients"])
        unit, cap = self.scope["audience_unit"], int(self.scope["max_tickets_per_sweep"])
        cursor = entry.get("cursor")
        start = int(entry.get("start_time") or 0) or 1
        query = {"cursor": cursor} if cursor else {"start_time": str(start)}
        records: list[dict[str, Any]] = []
        tickets = 0
        while True:
            body, _h = self.get_json(self._url("zendesk.incremental_tickets",
                                               {**query, "per_page": str(_PER_PAGE)}))
            rows = body.get("tickets") if isinstance(body, Mapping) else None
            if not isinstance(rows, list) or not isinstance(body.get("end_of_stream"), bool):
                raise SourceUnavailable("zendesk incremental payload invalid")
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                recipient = vs.text(row.get("recipient"), 320)
                if not recipient or recipient.lower() not in wanted:
                    continue
                mapped = map_ticket(row, unit=unit)
                records.append(mapped)
                recipient = mapped["properties"]["recipient"]
                for comment in self.comments(mapped["join_keys"]["ticket_id"], unit):
                    comment["properties"]["recipient"] = recipient   # the capture unit
                    records.append(comment)
                tickets += 1
            after = vs.text(body.get("after_cursor"), 2000)
            if after is None or not grammar.external_query_ok(
                    "zendesk.incremental_tickets", "cursor=" + quote(after, safe="")):
                raise SourceUnavailable("zendesk after_cursor missing or invalid")
            query = {"cursor": after}
            if body["end_of_stream"]:
                return {"records": records, "cursor": after, "complete": True}
            if tickets >= cap:    # bounded sweep: resume from this cursor next time
                return {"records": records, "cursor": after, "complete": False}

    def comments(self, ticket_id: str, unit: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        query: dict[str, str] = {"page[size]": "100"}
        for _page in range(int(self.scope["max_comment_pages_per_ticket"])):
            body, _h = self.get_json(self._url("zendesk.ticket_comments", query,
                                               ticket=ticket_id))
            rows = body.get("comments") if isinstance(body, Mapping) else None
            if not isinstance(rows, list):
                raise SourceUnavailable("zendesk comments payload invalid")
            out.extend(map_comment(ticket_id, r, unit=unit) for r in rows
                       if isinstance(r, Mapping))
            meta = body.get("meta") if isinstance(body.get("meta"), Mapping) else {}
            after = vs.text(meta.get("after_cursor"), 2000)
            if meta.get("has_more") is not True:
                return out
            if after is None:
                raise SourceUnavailable("zendesk comments cursor missing")
            query = {"page[size]": "100", "page[after]": after}
        raise SourceUnavailable("zendesk comments exceed the page bound")


def sweep(environ: Mapping[str, str] | None = None, *, state_path: str | Path,
          transport: GuardedTransport | None = None, now: datetime | None = None,
          scope: Mapping[str, Any] | None = None, store: Any = None) -> dict[str, Any]:
    """One bounded incremental capture; disabled (nothing sent) while settings are absent.
    With ``store`` (a ``CaptureStore``) the records are saved there, by order mailbox,
    before the cursor moves (``integrations.capture_vendor``)."""
    env = os.environ if environ is None else environ
    missing = vs.missing_settings(REQUIRED_SETTINGS, env)
    if missing:
        return {"source": SOURCE, "blocked": "credentials_absent", "missing_settings": missing,
                "requests_sent": 0, "evidence_label": "not-run"}
    try:
        host, headers, secret = settings_from_env(env)
    except vs.SettingInvalid as exc:
        return {"source": SOURCE, "blocked": "setting_invalid",
                "invalid_settings": [exc.setting], "requests_sent": 0,
                "evidence_label": "not-run"}
    transport = transport or GuardedTransport()
    transport.register_secret(secret)
    reader = ZendeskReader(host, headers, transport, scope or load_scope())
    persist = cv.persist_into(store, cv.ZENDESK_KIND,
                              lambda r: (r.get("properties") or {}).get("recipient"),
                              binding=host, source_api="zendesk.incremental_tickets")
    out = vs.run_sweep(SOURCE, vs.SweepState(state_path), reader.order_tickets, transport,
                       now=now, persist=persist)
    out["evidence_label"] = "injected-transport" if transport.sender_injected else "live-read"
    return out


def probe(environ: Mapping[str, str] | None = None, *, state_path: str | Path | None = None,
          transport: GuardedTransport | None = None) -> dict[str, Any]:
    """The live check's read: counts only, never a ticket value (state in a temp file)."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        result = sweep(environ, state_path=state_path or Path(tmp) / "state.json",
                       transport=transport)
    records = result.pop("records", [])
    result["tickets"] = sum(1 for r in records if r["record_type"] == "OrderRequestTicket")
    result["messages"] = sum(1 for r in records if r["record_type"] == "OrderRequestMessage")
    result["attachments"] = sum(len(r.get("attachments", [])) for r in records)
    result["ok"] = "error" not in result and "blocked" not in result
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["probe"]:
        print("usage: python3 -m integrations.zendesk_read probe", file=sys.stderr)
        return 2
    result = probe()
    print(json.dumps(jsonable(result), sort_keys=True))
    return 0 if result.get("ok") or result.get("blocked") else 1


if __name__ == "__main__":
    raise SystemExit(main())
