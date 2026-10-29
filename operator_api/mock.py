"""Mock backend (``--mock``): realistic, wholly synthetic payloads in the contract's shapes,
so the front end can be built and demoed before the real backend answers.

Evidence label: isolated-fixture. Every company, person, number and handle below is
invented; nothing was read from a PLX system. The payloads go through the same pydantic
models as the real backend's, so a shape the contract does not allow fails here first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import secrets
import threading
import time
from typing import Any, Iterator
from urllib.parse import quote

from . import models as m
from .auth import Reader

LABEL = "FIXTURE: synthetic demo data (isolated-fixture), not real PLX records"
NOW = datetime(2026, 10, 29, 14, 5, tzinfo=timezone.utc)


def _iso(delta_hours: float = 0) -> str:
    return (NOW - timedelta(hours=delta_hours)).isoformat().replace("+00:00", "Z")


def obj_url(kind: str, object_id: str) -> str:
    return f"/object/{kind}/{quote(object_id, safe='')}"


def src_url(handle: str) -> str:
    return f"/source?handle={quote(handle, safe='')}"


def src(system: str, handle: str, label: str, *, quote_text: str | None = None,
        fields: dict[str, str] | None = None, hours: float = 1) -> m.Source:
    return m.Source(system=system, handle=handle, label=label, quote=quote_text,
                    fields=fields, open_url=src_url(handle), as_of=_iso(hours))


# ---- the synthetic world ------------------------------------------------------------

OBJECTS: list[dict[str, Any]] = [
    {"object_id": "PurchaseOrder:demo:90412", "type": "PurchaseOrder", "label": "PO 90412",
     "sublabel": "Maple Leaf Soapworks · due 17 Oct · 12 days late", "aliases": ["90412"]},
    {"object_id": "PurchaseOrder:demo:90388", "type": "PurchaseOrder", "label": "PO 90388",
     "sublabel": "Northwind Packaging · due 24 Oct · 5 days late", "aliases": ["90388"]},
    {"object_id": "PurchaseOrder:demo:90455", "type": "PurchaseOrder", "label": "PO 90455",
     "sublabel": "Ridgeway Labels · due 3 Nov · at risk", "aliases": ["90455"]},
    {"object_id": "SalesOrder:demo:77120", "type": "SalesOrder", "label": "SO 77120",
     "sublabel": "Glow Botanicals · ships 6 Nov", "aliases": ["77120"]},
    {"object_id": "Supplier:demo:maple-leaf", "type": "Supplier",
     "label": "Maple Leaf Soapworks", "sublabel": "Supplier · Ontario, Canada",
     "aliases": ["maple leaf", "soapworks", "canadian soap"]},
    {"object_id": "Supplier:demo:northwind", "type": "Supplier",
     "label": "Northwind Packaging Ltd", "sublabel": "Supplier · cartons and shippers",
     "aliases": ["northwind"]},
    {"object_id": "Supplier:demo:ridgeway", "type": "Supplier", "label": "Ridgeway Labels",
     "sublabel": "Supplier · printed labels", "aliases": ["ridgeway"]},
    {"object_id": "Customer:demo:glow", "type": "Customer", "label": "Glow Botanicals",
     "sublabel": "Customer · retail skincare", "aliases": ["glow"]},
    {"object_id": "Customer:demo:harbor", "type": "Customer", "label": "Harbor Skin Co",
     "sublabel": "Customer · spa and hotel", "aliases": ["harbor", "harbour"]},
    {"object_id": "Item:demo:BAR-LAV-100", "type": "Item",
     "label": "BAR-LAV-100 Lavender soap bar 100 g", "sublabel": "Item · finished good",
     "aliases": ["lavender", "soap bar"]},
    {"object_id": "Lot:demo:L-24-0917", "type": "Lot", "label": "Lot L-24-0917",
     "sublabel": "BAR-LAV-100 · received 2 Oct", "aliases": ["l-24-0917"]},
]

H_PO = "bc://demo/purchaseOrders/90412"
H_PO_LINE = "bc://demo/purchaseOrderLines/90412-10000"
H_MAIL = "graph-mail://demo/messages/msg-5531"
H_MAIL2 = "graph-mail://demo/messages/msg-5560"
H_PORTAL = "api://demo/app_purchase_order/90412"
H_TEAMS = "teams://demo/channels/purchasing/messages/88121"
H_FILE = "sharepoint://demo/drives/purchasing/items/quote-maple-leaf-2026.pdf"

PO_ROW = src("BC", H_PO, "BC purchase order 90412", fields={
    "No.": "90412", "Buy-from Vendor": "Maple Leaf Soapworks",
    "Expected Receipt Date": "2026-10-17", "Status": "Released"}, hours=0.5)
PO_LINE = src("BC", H_PO_LINE, "BC PO 90412 line 10000", fields={
    "Item": "BAR-LAV-100", "Quantity": "12000", "Outstanding Quantity": "12000",
    "Unit of Measure": "EA", "Direct Unit Cost": "0.84", "Currency": "CAD"}, hours=0.5)
MAIL = src("Email", H_MAIL, "Email from Maple Leaf Soapworks, 21 Oct",
           quote_text="Our saponification line was down for maintenance; the 12,000 "
                      "lavender bars will now ship on 4 November.", hours=2)
MAIL2 = src("Email", H_MAIL2, "Email to Maple Leaf Soapworks, 27 Oct",
            quote_text="Can you confirm the 4 November ship date still holds? Glow "
                       "Botanicals needs the bars for SO 77120.", hours=2)
PORTAL = src("Portal", H_PORTAL, "Portal PO 90412", fields={
    "PO": "90412", "Promised date": "2026-10-24", "Buyer": "Purchasing team"}, hours=0.2)
TEAMS = src("Teams", H_TEAMS, "Teams #purchasing, 22 Oct",
            quote_text="Maple Leaf pushed 90412 to Nov 4, flagging for the Glow order.",
            hours=3)
FILE = src("File", H_FILE, "Quote Maple Leaf Soapworks 2026.pdf",
           quote_text="Unit price CAD 0.84 per bar, FOB Mississauga, lead time 3 weeks.",
           hours=30)

FRESH = m.Freshness(as_of=_iso(3), label="Data as of 11:05 today", sources=[
    m.FreshnessSource(system="BC", as_of=_iso(0.5), age_seconds=1800),
    m.FreshnessSource(system="Portal", as_of=_iso(0.2), age_seconds=720),
    m.FreshnessSource(system="Email", as_of=_iso(2), age_seconds=7200),
    m.FreshnessSource(system="Teams", as_of=_iso(3), age_seconds=10800)])

HIDDEN_FINANCE = m.Hidden(count=3, by_owner=[m.HiddenOwner(owner_or_team="Finance",
                                                           count=3)],
                          message="3 more items match, but you don't have access. "
                                  "Ask Finance.")
NO_HIDDEN = m.Hidden(count=0, by_owner=[], message=None)

PO_REF = m.ObjectRef(object_id="PurchaseOrder:demo:90412", type="PurchaseOrder",
                     label="PO 90412", open_url=obj_url("PurchaseOrder",
                                                        "PurchaseOrder:demo:90412"))

CONFLICT_DATE = m.Conflict(field="Expected receipt date", subject="PO 90412", values=[
    m.ConflictValue(value="2026-10-17", source=PO_ROW, trusted=True,
                    why="Business Central is the system of record for receipts and "
                        "expected receipt dates."),
    m.ConflictValue(value="2026-10-24", source=PORTAL, trusted=False,
                    why="The Portal's promised date was entered later by hand and "
                        "Business Central was not updated.")])


def _late_answer(question: str) -> dict[str, Any]:
    return {
        "outcome": "answered",
        "direct_answer": "PO 90412 from Maple Leaf Soapworks is 12 days late: it was due "
                         "17 Oct and the supplier now says it ships 4 November because "
                         "their soap line was down.",
        "facts": [
            m.Fact(text="PO 90412 for 12,000 BAR-LAV-100 bars was expected on "
                        "17 Oct 2026; nothing has been received.",
                   sources=[PO_ROW, PO_LINE], check="value_matches_record"),
            m.Fact(text="On 21 Oct Maple Leaf Soapworks wrote that the bars will now ship "
                        "on 4 November after maintenance on their line.",
                   sources=[MAIL], check="quote_matches_text"),
            m.Fact(text="The order feeds SO 77120 for Glow Botanicals, which ships 6 Nov.",
                   sources=[MAIL2, TEAMS], check="quote_matches_text"),
        ],
        "not_found": [m.NotFound(what="A confirmed new ship date in writing after 27 Oct",
                                 where_it_would_live="An email reply from Maple Leaf "
                                 "Soapworks to Purchasing, or the BC PO's expected "
                                 "receipt date")],
        "hidden": HIDDEN_FINANCE, "conflicts": [CONFLICT_DATE], "subjects": [PO_REF],
        "did_you_mean": []}


def _landed_answer(question: str) -> dict[str, Any]:
    return {
        "outcome": "partial",
        "direct_answer": "Landed cost for PO 90412 is not complete yet: goods are "
                         "CAD 10,080.00; freight and duty are not on record.",
        "facts": [m.Fact(text="Goods: 12,000 bars at CAD 0.84 = CAD 10,080.00.",
                         sources=[PO_LINE, FILE], check="expression_matches_inputs")],
        "not_found": [m.NotFound(what="Freight invoice", where_it_would_live="A carrier "
                                 "invoice posted in BC, or the forwarder's email"),
                      m.NotFound(what="Duty and brokerage", where_it_would_live="The "
                                 "customs broker's entry summary (Finance)")],
        "hidden": HIDDEN_FINANCE, "conflicts": [], "subjects": [PO_REF], "did_you_mean": []}


def _ambiguous_answer(question: str) -> dict[str, Any]:
    refs = [m.ObjectRef(object_id=o["object_id"], type=o["type"], label=o["label"],
                        open_url=obj_url(o["type"], o["object_id"]))
            for o in OBJECTS if o["type"] in ("Customer",)]
    return {"outcome": "partial",
            "direct_answer": "I found more than one customer that could match. Which one "
                             "did you mean?",
            "facts": [], "not_found": [], "hidden": NO_HIDDEN, "conflicts": [],
            "subjects": [], "did_you_mean": refs}


def _unknown_answer(question: str) -> dict[str, Any]:
    return {"outcome": "not_found",
            "direct_answer": "I couldn't find records that answer this.",
            "facts": [], "not_found": [m.NotFound(
                what="Records matching your question",
                where_it_would_live="Try naming a PO, order, customer, supplier or lot; "
                                    "or check the shared drive for the document")],
            "hidden": NO_HIDDEN, "conflicts": [], "subjects": [], "did_you_mean": []}


def _pick(question: str) -> tuple[str, Any]:
    q = question.lower()
    if re.search(r"land|cost|freight|duty", q):
        return "how_much", _landed_answer
    if re.search(r"customer|client", q) and not re.search(r"\d{4,}", q):
        return "who", _ambiguous_answer
    if re.search(r"late|delay|where|status|when|soap|904|maple|canad", q):
        return "why", _late_answer
    return "other", _unknown_answer


class MockBackend:
    data_label = LABEL

    def __init__(self, *, step_delay: float = 0.0) -> None:
        self.step_delay = step_delay
        self._answers: dict[str, str] = {}      # answer_id -> reader key
        self._recent: dict[str, list[m.RecentQuestion]] = {}
        self._feedback: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def me(self, reader: Reader) -> m.Me:
        s = reader.session
        return m.Me(display_name=reader.display_name, email=reader.email,
                    principal=",".join(sorted(s.principals)), auth=reader.mode,
                    proof=s.proof, tenant=s.tenant, dev_mode=reader.mode != "entra",
                    data_label=self.data_label, expires_at=s.expires_at)

    def _pause(self) -> None:
        if self.step_delay:
            time.sleep(self.step_delay)

    def ask(self, reader: Reader, request: m.AskRequest) -> Iterator[tuple[str, Any]]:
        started = time.perf_counter()

        def ms() -> int:
            return int((time.perf_counter() - started) * 1000)

        conversation = request.conversation_id or secrets.token_hex(8)
        intent, build = _pick(request.question)
        yield "status", m.StatusEvent(step="understanding", message="Reading your question",
                                      elapsed_ms=ms())
        self._pause()
        entities = []
        if build is _late_answer or build is _landed_answer:
            entities.append(m.UnderstoodEntity(text="the soap order", resolved=PO_REF))
        yield "understanding", m.UnderstandingEvent(
            intent=intent, entities=entities, time_frame=None,
            rewrites=["purchase order lavender soap bars Maple Leaf Soapworks late",
                      "PO expected receipt date delay supplier email"])
        yield "status", m.StatusEvent(step="searching",
                                      message="Searching BC, the Portal, email and Teams",
                                      elapsed_ms=ms())
        self._pause()
        parts = build(request.question)
        cited = {s.handle: s for f in parts["facts"] for s in f.sources}
        if cited:
            yield "sources", m.SourcesEvent(sources=list(cited.values()))
        yield "status", m.StatusEvent(step="checking",
                                      message="Checking every figure against its source",
                                      elapsed_ms=ms())
        self._pause()
        for word in re.findall(r"\S+\s*", parts["direct_answer"]):
            yield "token", m.TokenEvent(text=word)
        answer_id = secrets.token_hex(12)
        with self._lock:
            self._answers[answer_id] = reader.key
            recent = self._recent.setdefault(reader.key, [])
            recent.insert(0, m.RecentQuestion(question=request.question, asked_at=_iso(),
                                              answer_id=answer_id,
                                              conversation_id=conversation))
            del recent[10:]
        yield "answer", m.AnswerPayload(answer_id=answer_id, question=request.question,
                                        freshness=FRESH, conversation_id=conversation,
                                        latency_ms=ms(), **parts)

    def suggest(self, reader: Reader, q: str, limit: int) -> m.SuggestResponse:
        from difflib import SequenceMatcher

        started = time.perf_counter()
        needle = q.strip().lower()
        scored = []
        for obj in OBJECTS:
            names = [obj["label"].lower(), *obj["aliases"]]
            if any(n == needle for n in names):
                match, score = "exact", 1.0
            elif any(n.startswith(needle) or f" {needle}" in n for n in names):
                match, score = "prefix", 0.9
            else:
                best = max(SequenceMatcher(None, needle, n[:len(needle) + 2]).ratio()
                           for n in names)
                if best < 0.6:
                    continue
                match, score = "fuzzy", round(best * 0.8, 3)
            scored.append((score, obj, match))
        scored.sort(key=lambda item: (-item[0], item[1]["label"]))
        return m.SuggestResponse(query=q, source="fixture",
                                 took_ms=int((time.perf_counter() - started) * 1000),
                                 suggestions=[m.Suggestion(
                                     object_id=o["object_id"], type=o["type"],
                                     label=o["label"], sublabel=o["sublabel"], match=match,
                                     score=score, open_url=obj_url(o["type"], o["object_id"]))
                                     for score, o, match in scored[:limit]])

    def home(self, reader: Reader) -> m.Home:
        late = [
            m.LatePo(object_id="PurchaseOrder:demo:90412", label="PO 90412",
                     supplier="Maple Leaf Soapworks", due_date="2026-10-17", days_late=12,
                     open_lines=1, reason="12,000 lavender bars not received; supplier "
                     "says ships 4 Nov", open_url=obj_url("PurchaseOrder",
                                                          "PurchaseOrder:demo:90412"),
                     sources=[PO_ROW, MAIL]),
            m.LatePo(object_id="PurchaseOrder:demo:90388", label="PO 90388",
                     supplier="Northwind Packaging Ltd", due_date="2026-10-24", days_late=5,
                     open_lines=2, reason="Shipper cartons not received; no reply since "
                     "23 Oct", open_url=obj_url("PurchaseOrder", "PurchaseOrder:demo:90388"),
                     sources=[src("BC", "bc://demo/purchaseOrders/90388",
                                  "BC purchase order 90388", fields={
                                      "Expected Receipt Date": "2026-10-24"})])]
        risk = [m.AtRisk(object_id="SalesOrder:demo:77120", label="SO 77120", kind="order",
                         reason="Needs PO 90412's bars, which now arrive 4 Nov; ships 6 Nov",
                         open_url=obj_url("SalesOrder", "SalesOrder:demo:77120"),
                         sources=[MAIL2]),
                m.AtRisk(object_id="PurchaseOrder:demo:90455", label="PO 90455",
                         kind="purchase_order", reason="Not confirmed by the supplier; due "
                         "3 Nov", open_url=obj_url("PurchaseOrder", "PurchaseOrder:demo:90455"))]
        with self._lock:
            recent = list(self._recent.get(reader.key, []))
        return m.Home(greeting_name=reader.display_name.split(" ")[0],
                      examples=["why is the soap order from the canadian supplier late",
                                "landed cost of PO 90412",
                                "what did we last tell Glow Botanicals",
                                "which POs are late this week"],
                      needs_attention=m.NeedsAttention(status="read", late_pos=late,
                                                       at_risk=risk, hidden=HIDDEN_FINANCE,
                                                       as_of=_iso(0.5)),
                      recent_questions=recent, freshness=FRESH)

    def object_page(self, reader: Reader, object_type: str, object_id: str) -> m.ObjectPage:
        obj = next((o for o in OBJECTS if o["object_id"] == object_id
                    or (o["type"].lower() == object_type.lower()
                        and object_id.lower() in o["aliases"])), None)
        if obj is None:
            raise KeyError("object_not_found")
        po = obj["object_id"] == "PurchaseOrder:demo:90412"
        status = (m.ObjectStatus(state="late", text="12 days late: due 17 Oct, supplier "
                                 "now ships 4 Nov", sources=[PO_ROW, MAIL]) if po else
                  m.ObjectStatus(state="open", text="Open", sources=[]))
        return m.ObjectPage(
            object_id=obj["object_id"], type=obj["type"], label=obj["label"],
            summary=obj["sublabel"], status=status,
            facts=[m.Fact(text="Supplier: Maple Leaf Soapworks", sources=[PO_ROW]),
                   m.Fact(text="12,000 × BAR-LAV-100 at CAD 0.84", sources=[PO_LINE])]
            if po else [],
            timeline=[
                m.TimelineEvent(at="2026-09-26T15:00:00Z", system="BC", title="PO released",
                                source=PO_ROW),
                m.TimelineEvent(at="2026-10-21T13:12:00Z", system="Email",
                                title="Supplier delays shipment to 4 Nov",
                                summary="Soap line down for maintenance", source=MAIL),
                m.TimelineEvent(at="2026-10-22T09:40:00Z", system="Teams",
                                title="Purchasing flags impact on SO 77120", source=TEAMS),
                m.TimelineEvent(at="2026-10-27T16:20:00Z", system="Email",
                                title="We asked to confirm 4 Nov", source=MAIL2)] if po else [],
            linked=[m.LinkedItem(kind="email", label="Delay notice from Maple Leaf",
                                 at="2026-10-21T13:12:00Z", source=MAIL),
                    m.LinkedItem(kind="teams", label="#purchasing thread",
                                 at="2026-10-22T09:40:00Z", source=TEAMS),
                    m.LinkedItem(kind="document", label="Quote 2026 (PDF)", source=FILE)]
            if po else [],
            people=[m.Person(name="Dana Ortiz", role="Buyer", organisation="PLX",
                             sources=[PORTAL]),
                    m.Person(name="Jordan Lee", role="Supplier contact",
                             organisation="Maple Leaf Soapworks", sources=[MAIL])] if po
            else [],
            landed_cost=m.LandedCost(status="partial",
                                     total=None,
                                     lines=[m.LandedCostLine(label="Goods", amount=m.Money(
                                         amount="10080.00", currency="CAD"),
                                         sources=[PO_LINE])],
                                     missing=["Freight invoice", "Duty and brokerage"])
            if po else None,
            late_lines=[m.LateLine(line="10000", item="BAR-LAV-100", quantity="12000 EA",
                                   due_date="2026-10-17", days_late=12, sources=[PO_LINE])]
            if po else [],
            related=[m.ObjectRef(object_id="SalesOrder:demo:77120", type="SalesOrder",
                                 label="SO 77120", open_url=obj_url(
                                     "SalesOrder", "SalesOrder:demo:77120")),
                     m.ObjectRef(object_id="Supplier:demo:maple-leaf", type="Supplier",
                                 label="Maple Leaf Soapworks", open_url=obj_url(
                                     "Supplier", "Supplier:demo:maple-leaf"))] if po else [],
            not_found=[m.NotFound(what="Goods receipt", where_it_would_live="A posted BC "
                                  "purchase receipt for PO 90412")] if po else [],
            conflicts=[CONFLICT_DATE] if po else [],
            hidden=HIDDEN_FINANCE if po else NO_HIDDEN, freshness=FRESH)

    def source(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        known = {s.handle: s for s in (PO_ROW, PO_LINE, MAIL, MAIL2, PORTAL, TEAMS, FILE)}
        s = known.get(handle)
        if s is None:
            raise KeyError("source_not_found")
        return m.SourceView(handle=s.handle, system=s.system, label=s.label,
                            kind="record" if s.fields else "text", title=s.label,
                            text=s.quote, fields=s.fields, as_of=s.as_of,
                            text_offset=text_offset)

    def feedback(self, reader: Reader, request: m.FeedbackRequest) -> m.FeedbackResponse:
        with self._lock:
            if self._answers.get(request.answer_id) != reader.key:
                raise KeyError("answer_not_found")
            self._feedback.append({"answer_id": request.answer_id, "rating": request.rating,
                                   "note": request.note})
            number = len(self._feedback)
        return m.FeedbackResponse(ok=True, feedback_id=f"fb-{number}",
                                  stored_in=["feedback_log"])
