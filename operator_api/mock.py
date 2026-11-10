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
from typing import Any, Iterator, Optional, Sequence
from urllib.parse import quote

from operating.answer_gate import _sentences

from . import ask_progress, models as m, presentation, proposals
from .actions import ActionsMixin
from .autopilot_api import AutopilotMixin
from .autopilot_area import AutopilotAreaMixin
from .assist_api import AssistMixin
from .write_intents_api import WriteIntentsMixin
from .watch import MockWatchMixin
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
    {"object_id": "SalesOrder:demo:77131", "type": "SalesOrder", "label": "SO 77131",
     "sublabel": "Glow Botanicals · invoiced 20 Oct", "aliases": ["77131"]},
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

def _line(line_id: str, label: str) -> m.ObjectRef:
    oid = f"PurchaseOrderLine:demo:{line_id}"
    return m.ObjectRef(object_id=oid, type="PurchaseOrderLine", label=label,
                       open_url=obj_url("PurchaseOrderLine", oid))


# The supplier promised each item its own date (a firm link: the confirmation names the
# PO and comes from the supplier's domain); one line is past its promise, one is not.
PROMISE_90412 = m.VendorPromise(
    link="confirmed", state="late", promised_date="2026-10-24", days_late=5,
    measured_against="today", source=MAIL, per_line=True,
    lines=[m.PromiseLine(line=_line("90412-10000", "Line 10000 · BAR-LAV-100 Lavender soap "
                                    "bar 100 g"), promised_date="2026-10-24", state="late",
                         days_late=5, matched_by="item_code", source=MAIL),
           m.PromiseLine(line=_line("90412-20000", "Line 20000 · BOX-GIFT-6 Gift box of 6"),
                         promised_date="2026-11-04", state="not_due",
                         matched_by="description", source=MAIL)])

CONTACT = "Jordan Lee <jordan.lee@mapleleaf.example>"
PO_TRAIL = m.EvidenceTrail(kind="purchase_order", title="Why PO 90412 is late", trails=[
    m.PoTrail(
        subject=m.ObjectRef(object_id="PurchaseOrder:demo:90412", type="PurchaseOrder",
                            label="PO 90412",
                            open_url=obj_url("PurchaseOrder", "PurchaseOrder:demo:90412")),
        state="late", days_late=12,
        verdict="12 days late: due 2026-10-17, nothing received.",
        eta=m.EtaCheck(state="confirmed", text="Yes: the vendor confirmed 2026-11-04 "
                       "(revised 1 time).", source=MAIL, promise=PROMISE_90412),
        steps=[m.TrailStep(kind="due_date", title="Requested by 2026-10-17", source=PO_ROW,
                           detail="the governing requested-by date",
                           check="read_from_record"),
               m.TrailStep(kind="due_date", title="Expected on a line by 2026-10-17",
                           source=PO_LINE, check="read_from_record"),
               m.TrailStep(kind="po_issued", at="2026-09-26", title="PO issued",
                           source=PO_ROW, check="read_from_record"),
               m.TrailStep(kind="revision", at="2026-10-21",
                           title="Promise revised from 2026-10-17 to 2026-11-04",
                           source=MAIL, check="read_from_record"),
               m.TrailStep(kind="follow_up", at="2026-10-21",
                           title="Email from the vendor side: Delay on lavender bars",
                           source=MAIL, check="lead", sender=CONTACT),
               m.TrailStep(kind="follow_up", at="2026-10-27",
                           title="Email we sent: Can you confirm 4 November?",
                           source=MAIL2, check="lead"),
               m.TrailStep(kind="verdict", at="2026-10-29",
                           title="12 days late: due 2026-10-17, nothing received.",
                           source=PO_ROW, check="computed_from_cited_records")],
        next_action=m.NextAction(text="The promise moved once, to 2026-11-04: confirm it "
                                      "still holds with the vendor.", rule="promise_moved",
                                 contact=CONTACT, contact_source=MAIL))])

H_PO_88 = "bc://demo/purchaseOrders/90388"
H_MAIL_55 = "graph-mail://demo/messages/msg-5602"
PO_88 = src("BC", H_PO_88, "BC purchase order 90388", fields={
    "Expected Receipt Date": "2026-10-24"})
LEAD_MAIL = src("Email", H_MAIL_55, "Email from ridgeway-print.example, 28 Oct",
                quote_text="Re: labels for order 90455 - we expect to dispatch around "
                           "6 November.", hours=4)


def _po_trail(oid: str, label: str, *, state: str, days: int | None, verdict: str,
              eta: m.EtaCheck, steps: list[m.TrailStep]) -> m.EvidenceTrail:
    ref = m.ObjectRef(object_id=oid, type="PurchaseOrder", label=label,
                      open_url=obj_url("PurchaseOrder", oid))
    return m.EvidenceTrail(kind="purchase_order", title=f"How we know: {label}", trails=[
        m.PoTrail(subject=ref, state=state, days_late=days, verdict=verdict, eta=eta,
                  steps=steps)])


TRAILS = {
    "PurchaseOrder:demo:90388": _po_trail(
        "PurchaseOrder:demo:90388", "PO 90388", state="late", days=5,
        verdict="5 days late: due 2026-10-24, nothing received.",
        eta=m.EtaCheck(state="no_confirmation_found", text="No vendor confirmation found "
                       "in the mail, files and records searched."),
        steps=[m.TrailStep(kind="due_date", title="Requested by 2026-10-24", source=PO_88,
                           check="read_from_record")]),
    "PurchaseOrder:demo:90455": _po_trail(
        "PurchaseOrder:demo:90455", "PO 90455", state="not_due", days=None,
        verdict="Not late yet: open on 2026-10-29, due by 2026-11-03.",
        eta=m.EtaCheck(state="lead_only", text="Lead only: a document that looks like a "
                       "confirmation names this PO, but its link to the PO is not "
                       "confirmed, so its date does not count.", source=LEAD_MAIL,
                       promise=m.VendorPromise(link="unconfirmed_link", state="not_judged",
                                               promised_date="2026-11-06",
                                               source=LEAD_MAIL)),
        steps=[m.TrailStep(kind="lead", at="2026-10-28", title="A message names this PO "
                           "and a dispatch date", source=LEAD_MAIL, check="lead")]),
}

# A customer order's materials: the assembly made for SO 77120 consumes PO 90412's bars.
_ref = lambda kind, oid, label: m.ObjectRef(object_id=oid, type=kind, label=label,  # noqa: E731
                                            open_url=obj_url(kind, oid))
TRAILS["SalesOrder:demo:77120"] = m.EvidenceTrail(
    kind="materials", title="Materials for SO 77120, late first", materials=[
        m.MaterialRow(order=_ref("SalesOrder", "SalesOrder:demo:77120", "SO 77120"),
                      assembly=_ref("AssemblyOrder", "AssemblyOrder:demo:AO-7712",
                                    "AO-7712"),
                      item=_ref("Item", "Item:demo:BAR-LAV-100",
                                "BAR-LAV-100 Lavender soap bar 100 g"),
                      po=PO_REF, state="late", days_late=12,
                      verdict="12 days late: due 2026-10-17, nothing received.")])
AT_RISK_77120 = m.ObjectStatus(state="at_risk", text="At risk: needs PO 90412's bars, which "
                               "now arrive 4 Nov; ships 6 Nov", sources=[MAIL2])

# ---- margin (the order's and the customer's, on item cost only) ----------------------
H_INV = "bc://demo/salesInvoiceLines/SI-55120-10000"
H_ILE = "bc://demo/itemLedgerEntries/884120"
INV = src("BC", H_INV, "BC sales invoice SI-55120 line 10000", fields={
    "Item": "BAR-LAV-100", "Quantity": "120", "Unit Price": "2.20"})
ILE = src("BC", H_ILE, "BC item ledger entry 884120", fields={
    "Item": "BAR-LAV-100", "Cost Amount (Actual)": "-260.40"})
SO_REF = m.ObjectRef(object_id="SalesOrder:demo:77120", type="SalesOrder", label="SO 77120",
                     open_url=obj_url("SalesOrder", "SalesOrder:demo:77120"))
SO_NOCOST = m.ObjectRef(object_id="SalesOrder:demo:77131", type="SalesOrder",
                        label="SO 77131", open_url=obj_url("SalesOrder",
                                                           "SalesOrder:demo:77131"))
BREAKDOWN_77120 = m.CostBreakdown(
    order=SO_REF, date="2026-10-02", revenue=m.Money(amount="264.00", currency="CAD"),
    booked_cost=m.Money(amount="260.40", currency="CAD"),
    margin=m.Money(amount="3.60", currency="CAD"), margin_pct="1.36",
    lines=[m.CostLine(label="item BAR-LAV-100 (Lavender soap bar 100 g)", quantity="120",
                      unit="PCS", revenue=m.Money(amount="264.00", currency="CAD"),
                      booked_cost=m.Money(amount="260.40", currency="CAD"),
                      margin=m.Money(amount="3.60", currency="CAD"), unit_price="2.20",
                      unit_cost="2.17", sources=[INV, ILE])],
    cost_sources=[m.CostSource(kind="item_ledger", label="item BAR-LAV-100",
                               detail="1 entry", amount=m.Money(amount="260.40",
                                                                currency="CAD"),
                               sources=[ILE])],
    drivers=["Item BAR-LAV-100 (Lavender soap bar 100 g) on SO 77120 against SO 76980 "
             "(2026-06-11, the previous order with it): item cost 2.17 CAD/PCS against "
             "1.59 CAD/PCS, up 36.5%, which took 69.60 CAD off the margin at 120 PCS; the "
             "price was unchanged at 2.20 CAD/PCS."],
    why=["Item cost rose 36.5% (1.59 → 2.17 CAD/PCS) while the price was unchanged: "
         "−69.60 CAD against SO 76980 (2026-06-11)."])


def _profit(customer: m.ObjectRef, *, focus: m.CostBreakdown | None) -> m.Profitability:
    return m.Profitability(
        customer=customer, period_start="2025-10-29", period_end="2026-10-29",
        period="the last 12 months", revenue=m.Money(amount="1840.00", currency="CAD"),
        booked_cost=m.Money(amount="1512.40", currency="CAD"),
        margin=m.Money(amount="327.60", currency="CAD"), margin_pct="17.80",
        orders_in_period=3, orders_counted=2, orders_read=3,
        ranking="partial: the 2 checked orders only; 1 invoiced order has no cost value in "
                "BC (SO 77131)",
        margin_note="Labour and overhead aren't booked in Business Central, so this "
                    "overstates true margin.",
        orders=[m.OrderMargin(order=SO_REF, date="2026-10-02",
                              revenue=m.Money(amount="264.00", currency="CAD"),
                              booked_cost=m.Money(amount="260.40", currency="CAD"),
                              margin=m.Money(amount="3.60", currency="CAD"), margin_pct="1.36"),
                m.OrderMargin(order=SO_NOCOST, date="2026-10-20", gap="cost not valued",
                              gap_code="COST_NOT_VALUED")],
        breakdown=focus)


GLOW = m.ObjectRef(object_id="Customer:demo:glow", type="Customer", label="Glow Botanicals",
                   open_url=obj_url("Customer", "Customer:demo:glow"))
H_SAGE = "sage://demo/salesInvoices/harbor-2026"
SAGE_INV = src("Sage", H_SAGE, "Sage invoices, Harbor Skin Co", hours=26)
HARBOR_SUMMARY = m.CustomerSummary(revenue=[m.CustomerAmount(
    label="Invoiced (excluding tax)", period="last 12 months (since 2025-10-29)",
    money=m.Money(amount="12480.00", currency="CAD"), count=9, system="Sage",
    sources=[SAGE_INV])])

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


def _profit_answer(question: str) -> dict[str, Any]:
    return {
        "outcome": "partial",
        "direct_answer": "Glow Botanicals made CAD 327.60 on CAD 1,840.00 over the last 12 "
                         "months (17.8%), on 2 of its 3 invoiced orders. SO 77120 earned "
                         "only 1.4% because the soap bar's cost rose 36.5% while its price "
                         "stayed at 2.20 CAD/PCS.",
        "facts": [m.Fact(text="SO 77120: CAD 264.00 invoiced against CAD 260.40 booked item "
                              "cost, a margin of CAD 3.60.", sources=[INV, ILE],
                         check="expression_matches_inputs")],
        "not_found": [m.NotFound(what="A booked item cost for SO 77131",
                                 where_it_would_live="BC item ledger entries for its "
                                 "shipment (Finance)")],
        "hidden": NO_HIDDEN, "conflicts": [], "subjects": [GLOW], "did_you_mean": [],
        "profitability": _profit(GLOW, focus=BREAKDOWN_77120)}


def _changed_answer(question: str) -> dict[str, Any]:
    return {
        "outcome": "answered",
        "direct_answer": "Since your last visit: Maple Leaf Soapworks moved PO 90412 to "
                         "4 November, and we asked them to confirm that date; Purchasing "
                         "flagged the delay against SO 77120.",
        "facts": [m.Fact(text="On 21 Oct Maple Leaf Soapworks wrote that the bars will now "
                              "ship on 4 November.", sources=[MAIL],
                         check="quote_matches_text"),
                  m.Fact(text="On 27 Oct we asked them to confirm the 4 November date.",
                         sources=[MAIL2], check="quote_matches_text"),
                  m.Fact(text="On 22 Oct Purchasing flagged the delay for the Glow "
                              "Botanicals order.", sources=[TEAMS],
                         check="quote_matches_text")],
        "not_found": [m.NotFound(what="A reply confirming 4 November",
                                 where_it_would_live="An email from Maple Leaf Soapworks "
                                 "to Purchasing")],
        "hidden": NO_HIDDEN, "conflicts": [], "subjects": [PO_REF], "did_you_mean": []}


def _pick(question: str) -> tuple[str, Any]:
    q = question.lower()
    if re.search(r"profit|margin", q):
        return "how_much", _profit_answer
    if re.search(r"changed|since (my|our|the) last|what'?s new", q):
        return "timeline", _changed_answer
    if re.search(r"land|cost|freight|duty", q):
        return "how_much", _landed_answer
    if re.search(r"customer|client", q) and not re.search(r"\d{4,}", q):
        return "who", _ambiguous_answer
    if re.search(r"late|delay|where|status|when|soap|904|maple|canad", q):
        return "why", _late_answer
    return "other", _unknown_answer


class MockBackend(ActionsMixin, WriteIntentsMixin, AutopilotMixin, AutopilotAreaMixin, AssistMixin,
                  MockWatchMixin):
    data_label = LABEL

    def __init__(self, *, step_delay: float = 0.0) -> None:
        self.step_delay = step_delay
        self._answers: dict[str, str] = {}      # answer_id -> reader key
        self._recent: dict[str, list[m.RecentQuestion]] = {}
        self._feedback: list[dict[str, Any]] = []
        self._kept: dict[str, dict[str, Any]] = {}   # answer_id -> what feedback attaches
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
        # The agent path's plain-words steps (``ask_progress``), as the fixture's records
        # would give them: the record found, the sources read, the check.
        if entities:
            steps = [("searching", f"Finding {PO_REF.label}"),
                     ("reading", "Reading Business Central receipts"),
                     ("reading", "Checking supplier confirmations in mail")]
        else:
            steps = [("searching", "Looking up records")]
        for step, message in steps:
            yield "status", m.StatusEvent(step=step, message=message, elapsed_ms=ms())
            self._pause()
        parts = build(request.question)
        cited = {s.handle: s for f in parts["facts"] for s in f.sources}
        if cited:
            yield "sources", m.SourcesEvent(sources=list(cited.values()))
        yield "status", m.StatusEvent(step="checking", message=ask_progress.GATE_STEP,
                                      elapsed_ms=ms())
        self._pause()
        first = next((s for s, _gap in _sentences(parts["direct_answer"]) if s.strip()), "")
        if parts["facts"] and first:
            # the gate accepted the answer: its first line before the rest is written
            yield "verdict", m.VerdictEvent(text=first.strip()[:400])
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
        answer = m.AnswerPayload(answer_id=answer_id, question=request.question,
                                 freshness=FRESH, conversation_id=conversation,
                                 latency_ms=ms(), **parts)
        answer.follow_ups = presentation.follow_ups(request.question, answer)
        from .backend import _answer_kept
        with self._lock:
            self._kept[answer_id] = {"question": request.question, **_answer_kept(answer)}
            while len(self._kept) > 200:
                self._kept.pop(next(iter(self._kept)))
        yield "answer", answer

    def suggest(self, reader: Reader, q: str, limit: int) -> m.SuggestResponse:
        started = time.perf_counter()
        # as the live lookup does: each trailing phrase of what was typed, longest first
        words = q.strip().lower().split()
        got: list = []
        for start in range(len(words)):
            got = self._suggest_phrase(" ".join(words[start:]))
            if got:
                break
        return m.SuggestResponse(query=q, source="fixture",
                                 took_ms=int((time.perf_counter() - started) * 1000),
                                 suggestions=[m.Suggestion(
                                     object_id=o["object_id"], type=o["type"],
                                     label=o["label"], sublabel=o["sublabel"], match=match,
                                     score=score, open_url=obj_url(o["type"], o["object_id"]))
                                     for score, o, match in got[:limit]])

    @staticmethod
    def _suggest_phrase(needle: str) -> list:
        from difflib import SequenceMatcher

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
        return scored

    def forget_reader(self, reader: Reader) -> None:
        return None

    def late_list(self, reader: Reader, *, offset: int = 0, limit: int = 25) -> m.LateList:
        late = self._late()
        page = late[offset:offset + limit]
        return m.LateList(total=len(late), offset=offset, items=page,
                          more=offset + len(page) < len(late),
                          next_offset=offset + len(page), as_of=_iso(0.5))

    @staticmethod
    def _late() -> list[m.LatePo]:
        return [
            m.LatePo(object_id="PurchaseOrder:demo:90412", label="PO 90412",
                     supplier="Maple Leaf Soapworks", due_date="2026-10-17", days_late=12,
                     open_lines=1, amount=m.Money(amount="18400.00", currency="CAD"), reason="12,000 lavender bars not received; supplier "
                     "says ships 4 Nov", open_url=obj_url("PurchaseOrder",
                                                          "PurchaseOrder:demo:90412"),
                     sources=[PO_ROW, MAIL]),
            m.LatePo(object_id="PurchaseOrder:demo:90388", label="PO 90388",
                     supplier="Northwind Packaging Ltd", due_date="2026-10-24", days_late=5,
                     open_lines=2, amount=m.Money(amount="2310.50", currency="USD"), reason="Shipper cartons not received; no reply since "
                     "23 Oct", confirmation="none_found",
                     open_url=obj_url("PurchaseOrder", "PurchaseOrder:demo:90388"),
                     sources=[src("BC", "bc://demo/purchaseOrders/90388",
                                  "BC purchase order 90388", fields={
                                      "Expected Receipt Date": "2026-10-24"})])]

    def home(self, reader: Reader) -> m.Home:
        late = self._late()
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
                                                       as_of=_iso(0.5), late_total=len(late)),
                      recent_questions=recent, freshness=FRESH)

    def _briefing(self, since_visit: bool, reader: str | None = None
                  ) -> tuple[dict, dict[str, str]]:
        """A demo briefing built by the real rules (``operating.briefing.build``) over demo
        events: top items, groups with counts, a suspect total, a PO due today, an
        acknowledged one, a partial check and a hidden count."""
        from operating import briefing as bf

        def gap(n: int, kind: str, oid: str, summary: str, **extra: Any) -> dict:
            return {"id": f"pe-demo{n:011d}", "kind": kind, "object_id": oid,
                    "object_type": oid.split(":", 1)[0], "summary": summary,
                    "state": extra.pop("state", "open"),
                    "first_seen": extra.pop("first_seen", _iso(30)),
                    "citations": extra.pop("citations", ["bc://demo/purchaseOrders/90412"]),
                    "follow_up": {"why": extra.pop("why", None)}, **extra}

        labels = {"PurchaseOrder:demo:90412": "PO 90412", "PurchaseOrder:demo:90388": "PO 90388",
                  "PurchaseOrder:demo:90455": "PO 90455", "PurchaseOrder:demo:90501": "PO 90501"}
        gaps = [
            gap(1, "po_impact", "PurchaseOrder:demo:90412", "affects 1 sales order, worst 2 "
                "days at risk", why="An item of this purchase order is consumed by an "
                "assembly order made for SO 77120.", first_seen=_iso(0.5),
                event={"orders_affected": 1, "days": 12, "amount": {
                    "value": "18400.00", "currency": "CAD", "basis": "total_cost"}}),
            gap(2, "unconfirmed_po", "PurchaseOrder:demo:90388", "An open purchase order "
                "needed by 2026-10-24 has no supplier confirmation", days_late=5,
                event={"amount": {"value": "2400.00", "currency": "CAD",
                                  "basis": "total_cost"}}),
            gap(3, "unconfirmed_po", "PurchaseOrder:demo:90501", "An open purchase order "
                "needed by 2026-07-14 has no supplier confirmation", days_late=109,
                event={"amount": {"value": "4410000.21", "currency": "CAD",
                                  "basis": "total_cost", "suspect": "the total is over "
                                  "1,000 times this vendor's usual order in CAD"}}),
            gap(4, "unconfirmed_po", "PurchaseOrder:demo:90455", "An open purchase order "
                "needed by 2026-10-31 has no supplier confirmation"),       # due today
            gap(5, "vendor_no_reply", "PurchaseOrder:demo:90388", "A request to the vendor "
                "has had no reply for 8 days", event={"days": 8}),
        ]
        gaps += [gap(10 + n, "lot_near_expiry", f"Lot:demo:{n}", f"Expires in {n + 3} days",
                     event={"days": n + 3}, first_seen=_iso(0.5 if n < 3 else 30))
                 for n in range(9)]
        gaps += [gap(30 + n, "unconfirmed_po", f"PurchaseOrder:demo:91{n:03d}",
                     "An open purchase order past its need date has no supplier "
                     "confirmation", days_late=2 + n,
                     state="acknowledged" if n == 0 else "open") for n in range(6)]
        # as the proactive engine reads its inbox: an item marked by feedback is suppressed
        from operating import feedback_signals
        hide = feedback_signals.suppressor(None)   # PLX_FEEDBACK_DIR only, never a state
        gaps = [g for g in gaps if hide(g["id"], reader) is None]
        history = {d: {"runs": 3, "first_found": "2026-10-30T06:00:00Z"} for d in (
            "late_or_unconfirmed_po", "orders_at_risk", "lot_attention", "vendor_no_reply")}
        detectors = {d: "complete" for d in bf.CHECK_WORDS}
        detectors["vendor_terms_unapplied"] = "truncated"
        inbox = {"status": "read", "gaps": gaps, "complete": False, "as_of": _iso(0.5)[:10],
                 "run_at": _iso(0.5), "detectors": detectors, "history": history,
                 "detector_reasons": {"vendor_terms_unapplied": ["time_budget"]}}
        found = bf.build(inbox, routing=bf.Routing(frozenset({"PROC", "QUALITY"})),
                         hidden={"lot_on_hold": 3, "vendor_no_reply": 1},
                         last_visit=_iso(24), since_visit=since_visit)
        for item in found["items"]:
            labels.setdefault(item["object_id"], item["object_id"].split(":")[-1])
        return found, labels

    def briefing(self, reader: Reader, *, since_visit: bool = False) -> m.Briefing:
        from operating import briefing as bf
        from .backend import briefing_item, briefing_model

        found, labels = self._briefing(since_visit, reader.key)
        top = [briefing_item(i, labels, None) for i in found["items"][:bf.TOP_ITEMS]]
        return briefing_model(found, top, digest=bf.digest_text(
            found, first_name=reader.display_name.split(" ")[0], labels=labels))

    def briefing_items(self, reader: Reader, event_type: str, *, offset: int = 0,
                       limit: int = 25, since_visit: bool = False) -> m.BriefingItems:
        from operating import briefing as bf
        from .backend import briefing_item

        found, labels = self._briefing(since_visit, reader.key)
        members = [i for i in found["items"] if i["event_type"] == event_type
                   and i["rank"] > bf.TOP_ITEMS]
        page = members[offset:offset + limit]
        return m.BriefingItems(event_type=event_type, total=len(members), offset=offset,
                               items=[briefing_item(i, labels, None) for i in page],
                               more=offset + len(page) < len(members))

    def briefing_seen(self, reader: Reader) -> m.BriefingSeen:
        return m.BriefingSeen(ok=True, at=_iso(0))

    def watcher_item_state(self, reader: Reader, item_id: str,
                           request: m.WatcherItemStateRequest) -> m.WatcherItemState:
        """The demo briefing lists no watcher item: every id is not found."""
        from .errors import UserFacing
        raise UserFacing("item_not_found", "That item isn't in your briefing.", 404)

    def labour(self, reader: Reader) -> m.LabourPage:
        """The demo world holds no time sheets and grants no labour reader."""
        return m.LabourPage(state="not_granted", note="Labour figures need the hr or "
                            "ops-management grant.", weeks=[], trend=[], alerts=[])

    def upcoming_meetings(self, reader: Reader) -> m.UpcomingMeetings:
        """The demo world holds no calendar: nothing to prepare."""
        return m.UpcomingMeetings(meetings=[])

    def approvals_queue(self, reader: Reader) -> m.ApprovalsQueue:
        return m.ApprovalsQueue(as_of="2026-01-05", total=0, sent=0, writes=0)

    def reply_triage(self, reader: Reader) -> m.ReplyTriage:
        """The demo world holds no mailbox: nothing waits on a reply."""
        return m.ReplyTriage(as_of="2026-01-05", total=0, sent=0, writes=0)

    def rd_board(self, reader: Reader) -> m.RdBoard:
        """The demo world holds no R&D or sample threads."""
        from operating.rd_projects import idle_days
        return m.RdBoard(as_of="2026-01-05", idle_after_days=idle_days(), total=0, idle=0, sent=0,
                         writes=0)

    def record_pack(self, reader: Reader, object_type: str, object_id: str) -> m.RecordPack:
        from .errors import UserFacing
        raise UserFacing("not_found", "That record is not one you can open.", 404)

    # meeting follow-ups: the real views over the follow-ups kept under ``state_dir`` (none:
    # not found), read with no record reads and no markings
    state_dir: Any = None
    meeting_people: Any = None   # None: the host's people exclusion

    def meeting_follow_up(self, reader: Reader, meeting_id: str) -> m.MeetingFollowUp:
        from operating import meeting_followup as mf
        from operating import card_actions as ca
        from .briefs import _follow_ups, _viewer
        from .errors import UserFacing
        store = mf.FollowUps(self.state_dir, self.meeting_people) if self.state_dir else None
        record = store.get(meeting_id) if store else None
        page = mf.page(record, _viewer(reader, frozenset()), None, ()) if record else None
        if page is None:
            raise UserFacing("not_found", "That meeting is not one you attended.", 404)
        for action in page["actions"]:
            card = action["prepared"].get("card")
            if card:
                action["prepared"]["offers"] = ca.offers(card)
        return m.MeetingFollowUp(follow_up=page)

    def my_action_items(self, reader: Reader, person: Any) -> m.MyActionItems:
        from operating import meeting_followup as mf
        from .briefs import _viewer
        from .errors import UserFacing
        if not self.state_dir:
            raise UserFacing("not_found", "No meeting follow-ups are kept here.", 404)
        return m.MyActionItems(person=person, items=mf.my_items(
            mf.FollowUps(self.state_dir, self.meeting_people), _viewer(reader, frozenset()), person))

    def meeting_brief(self, reader: Reader, object_type: str, object_id: str) -> m.Brief:
        from .errors import UserFacing
        raise UserFacing("not_found", "That record is not one you can open.", 404)

    def weekly_review(self, reader: Reader, week: Any) -> m.Brief:
        """The real builder over no records: every figure reads as not readable."""
        from operating import briefs as br
        from .briefs import brief_model

        def call(tool: str, args: dict) -> dict:
            return {"error": {"code": "not_served", "message": "the demo serves no records"}}
        return brief_model(br.weekly_review(call, frozenset(), {"status": "not_configured"},
                                            week=week))

    def qc_pack_dict(self, reader: Reader, kind: str) -> dict[str, Any]:
        """The real builder over no records and no markings: every section is marked."""
        from operating import briefs as br

        def call(tool: str, args: dict) -> dict:
            return {"error": {"code": "not_served", "message": "the demo serves no records"}}
        if kind == "daily":
            return br.qc_daily_pack(call, frozenset(), {"status": "not_configured"})
        return br.qc_audit_pack(call, frozenset())

    def qc_pack(self, reader: Reader, kind: str) -> m.Brief:
        from .briefs import brief_model
        return brief_model(self.qc_pack_dict(reader, kind))

    def qc_pack_text(self, reader: Reader, kind: str) -> str:
        from operating import briefs as br
        return br.pack_text(self.qc_pack_dict(reader, kind))

    def alert_playbook_dict(self, reader: Reader, alert_id: str) -> Optional[dict[str, Any]]:
        """A demo alert's playbook by the real builder over the demo briefing; no record
        reads (no party address, no owner), so its email names no recipient."""
        from operating import alert_playbooks as ap

        found, labels = self._briefing(False)
        item = next((i for i in found.get("items") or () if i.get("id") == alert_id), None)
        if item is None:
            return None
        gap = {**item, "event": {**(item.get("facts") or {}), **(item.get("links") or {})}}
        cited = (item.get("citations") or [None])[0]

        def call(tool: str, args: dict) -> dict:
            label = labels.get(args.get("object_id", ""))
            if tool != "plx.object" or not label or not cited:
                return {}
            number = label.rsplit(" ", 1)[-1]
            return {"object": {"display": {"untrusted_text": number,
                                           "origin": {"citation": cited}}}, "values": []}

        return ap.build(gap, str(item.get("event_type") or ""), call=call, held=(),
                        environ={}, owner_links={})

    def suggestions(self, reader: Reader) -> m.Suggestions:
        """Demo suggestions built by the real composer (``operating.proactive_suggestions``)
        over the demo briefing, one demo PO trail (no confirmation, a cleared vendor
        contact) and one late-order supplier draft."""
        from operating import briefing as bf
        from operating import proactive_suggestions as ps
        from .backend import SUGGESTIONS_NOTE, suggestion_model

        found, labels = self._briefing(False)
        po, h = "PurchaseOrder:demo:90388", "bc://demo/purchaseOrders/90388"
        mail = "graph-mail://purchasing/messages/demo-90388"
        trail = {"kind": "purchase_order", "title": "PO 90388", "trails": [{
            "subject": {"object_id": po}, "state": "late", "days_late": 5,
            "verdict": "Late: needed by 2026-10-24, not received.",
            "eta": {"state": "no_confirmation", "text": "No: there is no vendor "
                    "confirmation on record for this PO.", "source": None},
            "steps": [{"kind": "due_date", "at": "2026-10-24", "title": "Needed by 2026-10-24",
                       "source": {"handle": h, "label": "BC purchase order 90388",
                                  "system": "BC", "open_url": None}},
                      {"kind": "follow_up", "at": "2026-10-20T14:02:00Z",
                       "title": "Message about PO 90388", "sender": "orders@supplier.example",
                       "source": {"handle": mail, "label": "Email", "system": "Email",
                                  "open_url": None}}],
            "next_action": {"text": "No vendor confirmation on record: ask the vendor "
                            "(orders@supplier.example) to confirm the delivery date.",
                            "rule": "no_confirmation", "contact": "orders@supplier.example",
                            "contact_source": {"handle": mail, "label": "Email",
                                               "system": "Email", "open_url": None}},
            "hidden_steps": 0, "may_hide_more": False, "not_found": []}]}
        line = "PurchaseOrderLine:demo:90388-1"
        draft = {"facts": [
            {"fact": "supplier", "value": "Demo Aromatics", "citation": h},
            {"fact": "order_number", "value": "PO 90388", "citation": h},
            {"fact": "line_item", "value": "Lavender oil 40/42", "line_id": line,
             "citation": h + "/lines/1"},
            {"fact": "expected_receipt", "value": "2026-10-24", "line_id": line,
             "citation": h + "/lines/1"},
            {"fact": "ordered_quantity", "value": "200", "unit": "kg", "line_id": line,
             "citation": h + "/lines/1"},
            {"fact": "received_quantity", "value": "0", "unit": "kg", "line_id": line,
             "citation": h + "/lines/1"}]}
        built = ps.build(found["items"], labels=labels, trails={po: trail},
                         late_drafts={po: draft})
        return m.Suggestions(status="read", total=built["total"], more=built["more"],
                             by_kind=built["by_kind"], collapsed=built["collapsed"],
                             items=[suggestion_model(c) for c in built["suggestions"]],
                             rank_basis=bf.RANK_BASIS, note=SUGGESTIONS_NOTE,
                             as_of=_iso(0.5), complete=False)

    def object_page(self, reader: Reader, object_type: str, object_id: str, *,
                    quick: bool = False) -> m.ObjectPage:
        obj = next((o for o in OBJECTS if o["object_id"] == object_id
                    or (o["type"].lower() == object_type.lower()
                        and object_id.lower() in o["aliases"])), None)
        if obj is None:
            raise KeyError("object_not_found")
        po = obj["object_id"] == "PurchaseOrder:demo:90412"
        status = (m.ObjectStatus(state="late", text="12 days late: due 17 Oct, supplier "
                                 "now ships 4 Nov", sources=[PO_ROW, MAIL]) if po else
                  AT_RISK_77120 if obj["object_id"] == "SalesOrder:demo:77120" else
                  m.ObjectStatus(state="open", text="Open", sources=[]))
        page = m.ObjectPage(
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
            trail=PO_TRAIL if po else TRAILS.get(obj["object_id"]),
            customer=HARBOR_SUMMARY if obj["object_id"] == "Customer:demo:harbor" else None,
            profitability=_profit(GLOW, focus=BREAKDOWN_77120)
            if obj["object_id"] in ("Customer:demo:glow", "SalesOrder:demo:77120",
                                    "SalesOrder:demo:77131") else None,
            hidden=HIDDEN_FINANCE if po else NO_HIDDEN, freshness=FRESH)
        page.proposals = proposals.for_page(page)   # drafts only: never sent
        return page

    def explore(self, reader: Reader, object_type: str, object_id: str) -> m.Explore:
        """The fixture's neighbours: the page's related records (firm), its linked
        messages and files are not records and are not walked."""
        page = self.object_page(reader, object_type, object_id)
        nodes = [m.ExploreNode(object_id=r.object_id, type=r.type, label=r.label,
                               open_url=r.open_url, tier="firm") for r in page.related]
        groups = []
        for kind in dict.fromkeys(n.type for n in nodes):
            groups.append(m.ExploreGroup(key=f"{kind}|related", label=f"{kind}: related",
                                         nodes=[n for n in nodes if n.type == kind]))
        return m.Explore(object_id=page.object_id, type=page.type, label=page.label,
                         groups=groups, message=None if groups else
                         "No linked records are visible to you for this one.")

    def record_history(self, reader: Reader, object_type: str, object_id: str,
                       cursor: Optional[str] = None) -> m.RecordHistory:
        """The mock keeps no versions: its history is unknown, said so, never empty."""
        if cursor:
            raise ValueError("invalid_cursor")
        page = self.object_page(reader, object_type, object_id)
        return m.RecordHistory(object_id=page.object_id, type=page.type, label=page.label,
                               message="The mock workspace keeps no change history.")

    def record_as_of(self, reader: Reader, object_type: str, object_id: str,
                     at: str) -> m.RecordAsOf:
        """The mock keeps no versions: what it held then is unknown, said so."""
        page = self.object_page(reader, object_type, object_id)
        return m.RecordAsOf(object_id=page.object_id, type=page.type, label=page.label,
                            at=at, message="The mock workspace keeps no earlier versions.")

    def object_impact(self, reader: Reader, object_type: str, object_id: str, mode: str,
                      change: Any = None) -> m.ImpactPanel:
        """The Impact panel: the mock has no links to walk, so nothing is reached and
        that is said as unknown, never as none."""
        page = self.object_page(reader, object_type, object_id)
        return m.ImpactPanel(object_id=page.object_id, mode=mode,
                             change=None if mode == "why" else "delayed 7 days",
                             unknown=["the mock workspace records no links to walk"])

    def object_section(self, reader: Reader, object_type: str, object_id: str,
                       section: str) -> m.ObjectSection:
        """The full page's part for ``section``, as the real backend splits it."""
        page = self.object_page(reader, object_type, object_id)
        po = page.type == "PurchaseOrder"
        margin = page.type in ("Customer", "SalesOrder")
        return m.ObjectSection(
            object_id=page.object_id, section=section,
            applies=po or section in ("people", "landed_cost")
            or (section == "margin" and margin),
            profitability=page.profitability if section == "margin" else None,
            status=page.status if section == "late" and po else None,
            late_lines=page.late_lines if section == "late" else [],
            people=page.people if section == "people" else None,
            landed_cost=page.landed_cost if section == "landed_cost" else None,
            trail=page.trail if section == "trail" else None,
            not_found=page.not_found if section == "trail" and po else None,
            proposals=page.proposals if section == "trail" and po else None)

    def source(self, reader: Reader, handle: str, text_offset: int) -> m.SourceView:
        known = {s.handle: s for s in (PO_ROW, PO_LINE, MAIL, MAIL2, PORTAL, TEAMS, FILE,
                                         INV, ILE)}
        s = known.get(handle)
        if s is None:
            raise KeyError("source_not_found")
        return m.SourceView(handle=s.handle, system=s.system, label=s.label,
                            kind="record" if s.fields else "text", title=s.label,
                            text=s.quote, fields=s.fields, as_of=s.as_of,
                            text_offset=text_offset)

    def answer_context(self, reader: Reader, answer_id: str) -> dict[str, Any]:
        with self._lock:
            if self._answers.get(answer_id) != reader.key:
                raise KeyError("answer_not_found")
            return dict(self._kept.get(answer_id) or {})

    # the fixture's grants: reader key -> markings held (set by a test or a demo)
    held_markings: dict[str, frozenset[str]] = {}

    def feedback_grant(self, reader: Reader, handles: Sequence[str]
                       ) -> tuple[frozenset[str], dict[str, Any]]:
        """(the markings the fixture reader holds, every handle readable)."""
        return frozenset(self.held_markings.get(reader.key, ())), {h: None for h in handles}

    def served_version(self) -> str:
        return "fixture"

    def feedback(self, reader: Reader, request: m.FeedbackRequest) -> m.FeedbackResponse:
        with self._lock:
            if self._answers.get(request.answer_id) != reader.key:
                raise KeyError("answer_not_found")
            self._feedback.append({"answer_id": request.answer_id, "rating": request.rating,
                                   "note": request.note})
            number = len(self._feedback)
        return m.FeedbackResponse(ok=True, feedback_id=f"fb-{number}",
                                  stored_in=["feedback_log"])
