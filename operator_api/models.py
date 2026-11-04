"""Typed payloads of the Workbench app API (``operator_api``), the interface the web app builds on.

Every response body is one of these models; ``operator_api/openapi.json`` is generated from
them (``python3 -m operator_api openapi --write``) and a test keeps the committed file equal
to the generated one.

Rules the shapes carry:
* a fact is text plus its sources; every figure in it came through the answer gate (the
  backend maps only gate-kept claims to facts), a vector never supplies a value;
* hidden items are a count by owner or team only: no title, snippet, sender or handle;
* money is a decimal string with its currency, never a float;
* errors are ``{"error": {"code", "message"}}`` with a plain-language message.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

API_VERSION = "1.0.0"

System = Literal["BC", "Portal", "Sage", "FileMaker", "Email", "Teams", "File", "MC"]
AuthMode = Literal["entra", "dev", "mock"]
Intent = Literal["status", "why", "who", "how_much", "compare", "find_document", "policy",
                 "timeline", "other"]
Outcome = Literal["answered", "partial", "not_found", "abstained", "refused"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _optional(schema: dict) -> None:
    """An added field older clients never send or read: no default in the schema, so the
    generated client types it optional (absent or null), never required."""
    schema.pop("default", None)


# ---- errors ---------------------------------------------------------------------------

class ErrorBody(_Model):
    code: str = Field(description="Stable machine code, e.g. sign_in_required.")
    message: str = Field(description="Plain-language sentence safe to show the person.")


class ErrorResponse(_Model):
    error: ErrorBody


# ---- identity -------------------------------------------------------------------------

class EntraConfig(_Model):
    tenant_id: str
    client_id: str
    authority: str = Field(description="https://login.microsoftonline.com/<tenant>/v2.0")
    scopes: list[str] = Field(description="Delegated scopes to request for this API.")
    redirect_uri: Optional[str] = Field(
        None, description="Registered SPA redirect URI; null until the app registration "
                          "has one (sign-in is then unavailable).")


class AuthConfig(_Model):
    """Public: what the front end needs to start sign-in. Identifiers only, no secret."""
    mode: AuthMode
    entra: Optional[EntraConfig] = None
    dev_login: bool = Field(description="POST /api/auth/dev-login is served (dev/mock only).")
    sign_in_available: bool
    message: Optional[str] = Field(None, description="Why sign-in is unavailable, in words.")


class Me(_Model):
    display_name: str
    email: Optional[str] = None
    principal: str = Field(description="employee:<oid>, or the fixture principals joined.")
    auth: AuthMode
    proof: str = Field(description="How the identity was proven (entra_access_token, "
                                   "configured_employee, fixture).")
    tenant: str
    dev_mode: bool
    data_label: str = Field(description="Evidence label of the data served, e.g. "
                                        "'source-mirror captures' or 'FIXTURE'.")
    expires_at: Optional[int] = None
    access_label: Optional[str] = Field(
        None, description="Set when this reader views with a declared full-read grant.")


class DevLoginRequest(_Model):
    ticket: Optional[str] = Field(
        None, max_length=4096, description="A console session ticket minted by the host "
        "owner (live state only); omitted for a dev or fixture identity.")


class DevLoginResponse(_Model):
    ok: bool
    me: Me


class LogoutResponse(_Model):
    ok: bool


# ---- sources, facts, answers -----------------------------------------------------------

class Source(_Model):
    system: System
    handle: str = Field(description="The L1 citation handle (graph-mail://, api://, bc://, ...).")
    label: str = Field(description="Short human label, e.g. 'BC purchase order 90412'.")
    quote: Optional[str] = Field(None, description="The exact quoted passage, when text.")
    fields: Optional[dict[str, str]] = Field(None, description="Record fields, when a row.")
    open_url: Optional[str] = Field(None, description="Opens the source for this reader; "
                                                      "null when it cannot be opened.")
    as_of: Optional[str] = Field(None, description="When the source was captured (ISO).")


class Fact(_Model):
    text: str
    sources: list[Source]
    check: Optional[str] = Field(None, description="The answer gate's check of this fact.")


class NotFound(_Model):
    what: str
    where_it_would_live: str


class HiddenOwner(_Model):
    owner_or_team: str = Field(description="A shared unit's human label, or the one unnamed "
                                           "bucket for other people's mail, chats and files.")
    count: int = Field(ge=1)
    named: bool = Field(True, description="False for the unnamed private bucket.")


class Hidden(_Model):
    count: Optional[int] = Field(ge=0, description="Null when no count may be given (the "
                                                   "people exclusion list is not loaded).")
    by_owner: list[HiddenOwner] = Field(default_factory=list)
    message: Optional[str] = Field(None, description="e.g. '4 more items match, but you "
                                                     "don't have access. Ask Purchasing.'")


class ConflictValue(_Model):
    value: str
    source: Source
    trusted: bool
    why: str


class Conflict(_Model):
    field: str
    subject: Optional[str] = None
    values: list[ConflictValue] = Field(min_length=2)


class FreshnessSource(_Model):
    system: System
    as_of: Optional[str] = None
    age_seconds: Optional[int] = None
    stale: bool = False
    capture: Optional[Literal["complete", "partial", "failed", "never_captured"]] = Field(
        None, json_schema_extra=_optional,
        description="How the last capture of this source ended: failed and "
                    "never_captured are stale whatever their age; partial (a budgeted "
                    "capture continuing next cycle) is judged by age.")


class Freshness(_Model):
    as_of: Optional[str] = Field(None, description="The data cut-off (ISO): the newest "
                                 "capture the served data holds, the same on every screen. "
                                 "A source read over a day before it is named in the label "
                                 "and keeps its own time in sources.")
    label: Optional[str] = Field(None, description="e.g. 'Data as of 10:42 today'.")
    sources: list[FreshnessSource] = Field(default_factory=list)


class ObjectRef(_Model):
    object_id: str
    type: str
    label: str
    open_url: str


# ---- evidence trail ("how we know") ------------------------------------------------------

StepKind = Literal["due_date", "po_issued", "confirmation", "revision", "lead", "follow_up",
                   "shipped", "receipt", "verdict", "claim",
                   # the why trail's (operating/why_trail.py)
                   "decision", "communication", "record_change", "document"]


class TrailStep(_Model):
    kind: StepKind
    at: Optional[str] = Field(None, description="When it happened (ISO date or time), as "
                                                "the cited record states it; null when the "
                                                "record carries no date.")
    title: str = Field(description="What happened, e.g. 'Vendor confirmed delivery by "
                                   "2026-07-02'; every date and figure in it is a cited value.")
    detail: Optional[str] = None
    source: Optional[Source] = Field(None, description="The record or message the step "
                                                       "cites; open_url opens it for this "
                                                       "reader. Null only for a verdict "
                                                       "that states no date (undetermined).")
    check: str = Field(description="How the step was checked: read_from_record (a value "
                                   "read from the cited record as this reader), lead (a "
                                   "machine-read reference, e.g. a message naming the PO), "
                                   "computed_from_cited_records (the verdict), text_match "
                                   "(a message or file naming the record by text), "
                                   "not_established (a why verdict no record states), "
                                   "unchecked, or the answer gate's check of a claim.")
    sender: Optional[str] = Field(None, description="A message's sender, only when the "
                                                    "people exclusion decides it may be shown.")


PromiseState = Literal["late", "on_time", "not_due", "received", "undetermined",
                       "not_judged"]


class PromiseLine(_Model):
    """One order line the supplier promised its own date, matched on firm evidence (the
    line's item code or description), with its verdict against that date."""
    line: ObjectRef
    promised_date: Optional[str] = None
    state: PromiseState = Field(description="Against the supplier's date for this line: "
                                            "late (open past it), not_due, on_time, "
                                            "received (its receipt date unread), "
                                            "undetermined, or not_judged (a lead).")
    days_late: Optional[int] = Field(None, ge=1, description="Days past the supplier's "
                                                            "promised date (late only).")
    matched_by: Optional[str] = Field(None, description="How the confirmation's item was "
                                                        "matched to the line: item_code or "
                                                        "description.")
    source: Optional[Source] = Field(None, description="The confirmation that promised "
                                                       "the line its date.")


class VendorPromise(_Model):
    """The order against the date the supplier promised, beside our requested-by verdict
    and never in its place. ``link`` confirmed: a firm link joins the confirmation to the
    order; unconfirmed_link: a lead whose dates are stated and never judged."""
    link: Literal["confirmed", "unconfirmed_link"]
    state: PromiseState
    promised_date: Optional[str] = None
    days_late: Optional[int] = Field(None, ge=1, description="Days past the supplier's "
                                                            "promised date (late only).")
    measured_against: Optional[Literal["today", "received"]] = Field(
        None, description="today: the order is open; received: its last receipt.")
    complete: bool = Field(True, description="False: a later promise this reader cannot "
                                             "see may exist.")
    source: Optional[Source] = Field(None, description="The confirmation that states the "
                                                       "promised date.")
    per_line: bool = Field(False, description="The supplier promised each item its own "
                                              "date; lines lists the matched lines.")
    lines: list[PromiseLine] = Field(default_factory=list)
    lines_hidden: int = Field(0, ge=0, description="Promised lines whose confirmation this "
                                                   "reader can't open: counted, never named.")
    lines_unmatched: int = Field(0, ge=0, description="Open lines no promise matched.")


class EtaCheck(_Model):
    state: Literal["confirmed", "promises_disagree", "lead_only", "no_confirmation",
                   "no_confirmation_found", "not_determined", "withheld", "not_applicable"]
    text: str = Field(description="Is the ETA backed by a vendor confirmation, in words.")
    source: Optional[Source] = Field(None, description="The confirmation (or the lead) "
                                                       "that says so, when the reader can "
                                                       "open it.")
    promise: Optional[VendorPromise] = Field(
        None, json_schema_extra=_optional,
        description="The supplier's promise and the order (per line when the supplier "
                    "promised each item its own date) against it; omitted when none is "
                    "read or the reader can't open the confirmation.")


class NextAction(_Model):
    text: str = Field(description="A suggested next step derived only from the trail's "
                                  "facts. A suggestion: nothing is sent or written.")
    rule: str = Field(description="The rule that suggested it, e.g. no_confirmation, "
                                  "promise_moved, promise_passed.")
    contact: Optional[str] = Field(None, description="Who to ask: the vendor-side sender "
                                                     "of a message this reader can open; "
                                                     "null otherwise.")
    contact_source: Optional[Source] = None
    suggestion_only: Literal[True] = True


class PoTrail(_Model):
    subject: ObjectRef
    state: Optional[str] = Field(None, description="plx.process lateness state: late, "
                                                   "on_time, not_due, undetermined, "
                                                   "not_applicable.")
    days_late: Optional[int] = None
    verdict: Optional[str] = Field(None, description="The verdict and its rule in one "
                                                     "sentence.")
    eta: Optional[EtaCheck] = None
    steps: list[TrailStep] = Field(default_factory=list)
    hidden_steps: int = Field(0, ge=0, description="Steps in places this reader can't "
                                                   "open: counted, never named.")
    may_hide_more: bool = Field(False, description="A source this reader can't fully "
                                                   "read may hold more steps (not counted).")
    next_action: Optional[NextAction] = None
    not_found: list[NotFound] = Field(default_factory=list)


class MaterialRow(_Model):
    order: Optional[ObjectRef] = None
    assembly: ObjectRef
    item: ObjectRef
    po: ObjectRef
    state: Optional[str] = None
    days_late: Optional[int] = None
    verdict: Optional[str] = None


class TrailGap(_Model):
    reason: str = Field(description="The typed gap, e.g. assembly_order_not_linked_to_"
                                    "order_line, line_item_not_recorded, link_lead_only.")
    state: str = Field(description="absent_in_source (the reader's walk was complete), "
                                   "unknown, or note (a count stated for completeness, e.g. "
                                   "POs already received or closed).")
    what: str
    where_it_would_live: str


class EvidenceTrail(_Model):
    """How we know: the steps, records and messages that led to the answer, each opening
    its source; a purchase order's lateness trail, a customer order's materials, or the
    answer's dated, checked claims."""
    kind: Literal["purchase_order", "materials", "claims", "why"]
    title: str
    plan: Optional[str] = Field(None, description="kind why: the why plan that built it "
                                                  "(operating/why_trail.py).",
                                json_schema_extra=_optional)
    trails: list[PoTrail] = Field(default_factory=list)
    materials: list[MaterialRow] = Field(default_factory=list)
    steps: list[TrailStep] = Field(default_factory=list, description="kind claims: the "
                                                                     "answer's dated claims.")
    gaps: list[TrailGap] = Field(default_factory=list)
    hidden_steps: int = Field(0, ge=0)
    hidden_message: Optional[str] = Field(None, description="e.g. '2 more steps in places "
                                                            "you can't open.'")
    may_hide_more: Optional[bool] = Field(None, description="kind why: a source this reader "
                                                            "can't fully read may hold more "
                                                            "steps (not counted).",
                                          json_schema_extra=_optional)
    complete: Optional[bool] = Field(None, description="kind why: false when only the "
                                                       "latest steps are shown or a read "
                                                       "did not finish.",
                                     json_schema_extra=_optional)
    next_action: Optional[NextAction] = Field(None, description="kind why: a suggested next "
                                                                "step; nothing is sent.",
                                              json_schema_extra=_optional)


class AnswerPayload(_Model):
    answer_id: str
    question: str
    outcome: Outcome
    direct_answer: str
    facts: list[Fact] = Field(default_factory=list)
    not_found: list[NotFound] = Field(default_factory=list)
    hidden: Hidden
    conflicts: list[Conflict] = Field(default_factory=list)
    freshness: Freshness
    subjects: list[ObjectRef] = Field(default_factory=list,
                                      description="Records the answer is about.")
    did_you_mean: list[ObjectRef] = Field(default_factory=list,
                                          description="Choices when a name or number matched "
                                                      "several records; never merged.")
    trail: Optional[EvidenceTrail] = Field(None, description="How we know (optional: "
                                                             "older clients ignore it).",
                                           json_schema_extra=_optional)
    conversation_id: str
    latency_ms: int
    access_label: Optional[str] = Field(
        None, description="'Viewing with full-read access' when the answer was served under "
                          "the reader's declared full-read grant.")
    profitability: Optional[Profitability] = Field(
        None, json_schema_extra=_optional,
        description="A customer's profitability and why an order lost money, when the "
                    "question asks it (optional: older clients ignore it).")
    list_total: Optional[int] = Field(
        None, json_schema_extra=_optional, ge=0,
        description="When the answer lists only the first of a longer list (the late "
                    "purchase orders): the whole list's size; the app offers 'show all' "
                    "(GET /api/late).")
    assistant_unavailable: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Why the assistant could not work the question (its usage limit, its "
                    "sign-in, its time limit or an error), in words; the answer then shows "
                    "what the records read without it show. Null when it ran.")
    follow_ups: list[str] = Field(
        default_factory=list, max_length=3, json_schema_extra=_optional,
        description="Up to three questions to ask next, built only from this answer's own "
                    "records and what it could not confirm (optional: older clients ignore "
                    "it).")
    proposals: list[Proposal] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="Follow-up drafts and proposed updates built from the answer's gated "
                    "trail; never sent or applied.")


# ---- ask (SSE) -------------------------------------------------------------------------

class HistoryTurn(_Model):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class AskRequest(_Model):
    question: str = Field(min_length=1, max_length=1000)
    conversation_id: Optional[str] = Field(None, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    history: list[HistoryTurn] = Field(default_factory=list, max_length=12)
    object_id: Optional[str] = Field(None, max_length=700,
                                     description="Ask about this record (object page).")


class Candidate(_Model):
    object_id: str
    type: str
    label: str
    score: Optional[float] = None


class UnderstoodEntity(_Model):
    text: str
    resolved: Optional[ObjectRef] = None
    ambiguous: bool = False
    candidates: list[Candidate] = Field(default_factory=list)


class UnderstandingEvent(_Model):
    intent: Intent
    entities: list[UnderstoodEntity] = Field(default_factory=list)
    time_frame: Optional[str] = None
    rewrites: list[str] = Field(default_factory=list)


class StatusEvent(_Model):
    step: Literal["understanding", "searching", "reading", "checking", "answering"]
    message: str
    elapsed_ms: int


class SourcesEvent(_Model):
    sources: list[Source]


class TokenEvent(_Model):
    text: str


class VerdictEvent(_Model):
    """The answer's first line, sent the moment the answer gate accepts it (before the
    full answer): only what that decision passed, every citation decided for the reader."""
    text: str = Field(max_length=400)


ASK_EVENTS: dict[str, type[_Model]] = {
    "understanding": UnderstandingEvent, "status": StatusEvent, "sources": SourcesEvent,
    "verdict": VerdictEvent, "token": TokenEvent, "answer": AnswerPayload,
    "error": ErrorBody}


# ---- suggest ---------------------------------------------------------------------------

class Suggestion(_Model):
    object_id: str
    type: str
    label: str
    sublabel: Optional[str] = None
    match: Literal["exact", "prefix", "fuzzy"]
    score: float
    open_url: str


class SuggestResponse(_Model):
    query: str
    suggestions: list[Suggestion]
    took_ms: int
    source: Literal["search_entities", "projection", "fixture"]


# ---- home ------------------------------------------------------------------------------

class LatePo(_Model):
    object_id: str
    label: str
    supplier: Optional[str] = None
    due_date: Optional[str] = None
    days_late: Optional[int] = None
    open_lines: Optional[int] = None
    amount: Optional[Money] = Field(None, description="The order's total in its own "
                                    "currency, when this reader may open it.")
    amount_suspect: Optional[str] = Field(None, description="Why the total is suspect "
                                          "(shown as a check, never as fact).")
    reason: str
    confirmation: Optional[Literal["none_found"]] = Field(
        None, json_schema_extra=_optional,
        description="none_found: no supplier confirmation is linked to the order. Null: "
                    "the list's entry does not say (the order's page decides it).")
    open_url: str
    sources: list[Source] = Field(default_factory=list)


class AtRisk(_Model):
    object_id: str
    label: str
    kind: str
    reason: str
    open_url: str
    sources: list[Source] = Field(default_factory=list)


class NeedsAttention(_Model):
    status: Literal["read", "not_configured", "unavailable"]
    late_pos: list[LatePo] = Field(default_factory=list)
    at_risk: list[AtRisk] = Field(default_factory=list)
    hidden: Optional[Hidden] = Field(None, description="Omitted when the source of the list "
                                                       "does not count what it hides.")
    as_of: Optional[str] = None
    late_total: Optional[int] = Field(
        None, json_schema_extra=_optional,
        description="Every late purchase order this reader may see; late_pos lists the "
                    "longest late of them.")
    refreshing: bool = Field(False, description="True while some checks' findings are of "
                                                "older data than the records now show: those "
                                                "are left out until the checks re-run on it.")
    checked_on: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="While refreshing: when the checks left out last ran (ISO time).")
    test_records_hidden: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="How many test or placeholder records were left out, in words "
                    "('2 test records hidden'); null for none.")


class LateList(_Model):
    """Every late purchase order this reader may see, a page at a time, longest late first
    (Home and the late-list answer show the first ones; "show all" pages the rest)."""
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    items: list[LatePo] = Field(default_factory=list)
    more: bool = False
    next_offset: int = Field(0, ge=0, description="Where the next page starts: past every "
                             "order this page read, shown or left out (stable paging).")
    as_of: Optional[str] = None


class RecentQuestion(_Model):
    question: str
    asked_at: str
    answer_id: Optional[str] = None
    conversation_id: Optional[str] = None


class Home(_Model):
    greeting_name: str
    examples: list[str]
    needs_attention: NeedsAttention
    recent_questions: list[RecentQuestion]
    freshness: Freshness


# ---- object page -----------------------------------------------------------------------

class Money(_Model):
    amount: str = Field(pattern=r"^-?\d+(\.\d+)?$", description="Decimal string.")
    currency: str


class CostLine(_Model):
    label: str = Field(description="The item, e.g. 'item FG-9 (Tube 50 ml)'.")
    quantity: Optional[str] = None
    unit: Optional[str] = None
    revenue: Optional[Money] = None
    booked_cost: Optional[Money] = None
    margin: Optional[Money] = None
    unit_price: Optional[str] = Field(None, description="Revenue per unit, decimal string.")
    unit_cost: Optional[str] = Field(None, description="Booked cost per unit, decimal string.")
    share_of_cost_pct: Optional[str] = None
    sources: list[Source] = Field(default_factory=list)


class CostSource(_Model):
    kind: Literal["item_ledger", "sage_cost_of_sales", "credit_memo", "return",
                  "material_po", "quote"]
    label: str
    detail: Optional[str] = None
    amount: Optional[Money] = None
    per_unit: Optional[str] = Field(None, description="e.g. '$16.00 CAD/PCS goods cost'.")
    sources: list[Source] = Field(default_factory=list)


class CostBreakdown(_Model):
    order: ObjectRef
    date: Optional[str] = None
    revenue: Optional[Money] = None
    booked_cost: Optional[Money] = None
    margin: Optional[Money] = None
    margin_pct: Optional[str] = None
    lines: list[CostLine] = Field(default_factory=list)
    cost_sources: list[CostSource] = Field(default_factory=list)
    drivers: list[str] = Field(default_factory=list)
    why: list[str] = Field(default_factory=list, json_schema_extra=_optional,
                           description="The margin's why in one or two plain sentences, "
                                       "e.g. 'Item cost rose 36.4% (1.59 → 2.17 CAD/PCS) "
                                       "while the price was unchanged: −69.00 CAD against "
                                       "<order> (<date>).'; each a driver's own figures.")
    gaps: list[NotFound] = Field(default_factory=list)


class OrderMargin(_Model):
    order: ObjectRef
    date: Optional[str] = None
    revenue: Optional[Money] = None
    booked_cost: Optional[Money] = None
    margin: Optional[Money] = None
    margin_pct: Optional[str] = None
    gap: Optional[str] = Field(None, description="Why no margin is stated, in words.")
    gap_code: Optional[str] = Field(None, json_schema_extra=_optional,
                                    description="The first reason's code, e.g. "
                                                "COST_NOT_VALUED (BC holds no valid cost).")


class ProductMargin(_Model):
    """One item's margin over the customer's checked order lines of the period."""
    item: str = Field(description="The item, e.g. 'item FG-9 (Tube 50 ml)'.")
    quantity: str
    unit: Optional[str] = None
    revenue: Optional[Money] = None
    booked_cost: Optional[Money] = None
    margin: Optional[Money] = None
    margin_pct: Optional[str] = None
    orders: int = Field(ge=0, description="Checked orders carrying the item.")
    unit_cost_range: Optional[str] = Field(None, description="Lowest to highest booked unit "
                                                             "cost over those orders.")
    complete: bool = Field(description="False: an invoiced order without a checked margin "
                                       "carries the item, or not every order was read; the "
                                       "figures cover the checked orders only.")
    sources: list[Source] = Field(default_factory=list)


class Profitability(_Model):
    customer: ObjectRef
    period_start: str
    period_end: str
    period: str = Field(description="The period in words, e.g. 'the last 12 months'.")
    revenue: Optional[Money] = None
    booked_cost: Optional[Money] = None
    margin: Optional[Money] = None
    margin_pct: Optional[str] = None
    orders_in_period: int = Field(ge=0)
    orders_counted: int = Field(ge=0, description="Orders whose margin is complete; the "
                                                  "totals cover these only.")
    withheld_orders: int = Field(0, ge=0, description="Orders whose figures you may not "
                                                      "read: a count only, never named.")
    orders_read: int = Field(0, ge=0, description="Orders read; fewer than orders_in_period "
                                                  "means no customer total is stated.")
    orders_in_period_exact: bool = Field(True, description="False: orders_in_period is a "
                                                           "lower bound.")
    period_basis: Optional[str] = Field(None, description="How an order counts in the "
                                                          "period.")
    ranking: Optional[str] = Field(None, description="What the ranking covers, e.g. "
                                                     "'partial: worst of the 60 most recent "
                                                     "orders; 214 more in the period not "
                                                     "read'.")
    margin_basis: str = Field("on item cost only", description="What every margin here "
                              "covers: item ledger cost only (BC books no labour or "
                              "overhead).")
    margin_note: Optional[str] = Field(None, description="The plain line saying what the "
                                       "margin basis leaves out.")
    verdict: Optional[Literal["profitable", "not_profitable"]] = Field(
        None, description="Stated only when every invoiced order of the period has a "
                          "checked margin; null otherwise.")
    orders_excluded: int = Field(0, ge=0, description="Orders of the period not yet "
                                                      "invoiced: excluded, never blocking.")
    orders: list[OrderMargin] = Field(default_factory=list, description="Worst margin first.")
    orders_more: int = Field(0, ge=0, description="Orders read but not listed (the list "
                                                  "stops at 30).")
    products: list[ProductMargin] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="Margin per item over the checked orders, worst first.")
    breakdown: Optional[CostBreakdown] = None
    next_action: Optional[NextAction] = Field(None, description="A suggested next step "
                                                                "from the facts only.")
    test_records_hidden: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="How many test or placeholder records were left out, in words "
                    "('2 test records hidden'); null for none.")


class ObjectStatus(_Model):
    state: str
    text: str
    sources: list[Source] = Field(default_factory=list)


class TimelineEvent(_Model):
    at: Optional[str] = None
    system: System
    title: str
    summary: Optional[str] = None
    source: Optional[Source] = None


class LinkedItem(_Model):
    kind: Literal["email", "teams", "document", "record", "task"]
    label: str
    at: Optional[str] = None
    source: Source


class Person(_Model):
    name: str
    role: str = Field(description="What they did on the records: owner, approver, sender...")
    organisation: Optional[str] = None
    sources: list[Source] = Field(default_factory=list)


class LandedCostLine(_Model):
    label: str
    amount: Money
    sources: list[Source] = Field(default_factory=list)


class LandedCost(_Model):
    status: Literal["computed", "partial", "not_established"]
    total: Optional[Money] = None
    per_unit: Optional[Money] = None
    lines: list[LandedCostLine] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class LateLine(_Model):
    line: str
    item: Optional[str] = None
    quantity: Optional[str] = None
    due_date: Optional[str] = None
    days_late: Optional[int] = None
    sources: list[Source] = Field(default_factory=list)


class CustomerAmount(_Model):
    """A customer's total over a period, in one currency, from its own cited records."""
    label: str = Field(description="What is summed, e.g. 'Invoiced (excluding tax)'.")
    period: str = Field(description="e.g. 'last 12 months (since 2025-11-01)'.")
    money: Money
    count: int = Field(ge=0, description="The records summed.")
    system: System
    sources: list[Source] = Field(default_factory=list)


class OrderRow(_Model):
    object_id: str
    label: str
    open_url: str
    date: Optional[str] = None
    status: Optional[str] = None
    amount: Optional[Money] = None
    sources: list[Source] = Field(default_factory=list)


class CustomerSummary(_Model):
    """What a customer's own record links to, read as the reader: revenue per period and
    currency, open and recent orders, contacts where captured, and its copies in other
    systems; each figure with its sources. What could not be established is named."""
    revenue: list[CustomerAmount] = Field(default_factory=list)
    orders_total: Optional[int] = Field(None, description="Every order linked to the "
                                        "record, counted over the whole set.")
    orders_at_least: Optional[int] = Field(
        None, description="When the total could not be counted: the orders read, at least "
                          "this many (shown as 'n+'); never a capped count stated as exact.")
    open_orders: Optional[int] = None
    open_list: list[OrderRow] = Field(default_factory=list)
    recent_orders: list[OrderRow] = Field(default_factory=list)
    contacts: list[Person] = Field(default_factory=list)
    also_in: list[ObjectRef] = Field(default_factory=list,
                                     description="The same customer's record in another "
                                                 "system, as the projection links them.")
    same_name: list[ObjectRef] = Field(
        default_factory=list, description="Other customers of the same name this reader "
                                          "may see (copies of this one left out).")
    not_established: list[str] = Field(default_factory=list)


# ---- proposals (drafts and proposed updates; nothing is ever sent or written) -----------

ProposalKind = Literal["vendor_chase", "customer_heads_up", "record_update"]


class DraftFigure(_Model):
    """One figure a proposal states (a PO number, a date, a line's quantity, a contact) and
    the record or message it was read from; every one passed this reader's gate."""
    label: str
    value: str
    source: Optional[Source] = Field(None, description="Null only for the record's own "
                                                       "label, which the page states.")


class ProposedChange(_Model):
    """A record field the evidence shows is out of date: before, after and why. The person
    applies it in the system named; the Workbench writes nothing."""
    system: System
    record: str = Field(description="The record to change, e.g. 'PO 90412'.")
    field: str = Field(description="The field, in words, e.g. 'Expected receipt date'.")
    before: str
    after: str
    before_source: Source
    evidence: Source = Field(description="The message or document that states the new "
                                         "value.")
    why: str


class Proposal(_Model):
    """A follow-up draft (email text the person edits, copies or opens in their own mail
    client) or a proposed record update. Never sent and never applied by the Workbench."""
    id: str
    kind: ProposalKind
    title: str
    subject: ObjectRef
    to: Optional[str] = Field(None, description="The recipient's address, only when a "
                                                "cited source this reader may open states it "
                                                "and the people directory clears it; null "
                                                "when unknown.")
    to_name: Optional[str] = None
    contact_note: Optional[str] = Field(None, description="Where the address was found, or "
                                                          "that no contact was found.")
    email_subject: Optional[str] = None
    body: Optional[str] = Field(None, description="The draft text; every date and figure in "
                                                  "it is one of ``figures``.")
    change: Optional[ProposedChange] = None
    figures: list[DraftFigure] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="What the draft could not "
                                                                 "state from the records, in "
                                                                 "words.")
    sent: Literal[False] = Field(False, description="Always false: the Workbench never "
                                                    "sends or writes; the person does.")


class ObjectPage(_Model):
    object_id: str
    type: str
    label: str
    summary: str
    status: ObjectStatus
    facts: list[Fact] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    linked: list[LinkedItem] = Field(default_factory=list)
    people: Optional[list[Person]] = Field(None, description="Omitted when no people plan "
                                                             "answered for this record.")
    landed_cost: Optional[LandedCost] = None
    late_lines: list[LateLine] = Field(default_factory=list)
    related: list[ObjectRef] = Field(default_factory=list)
    not_found: list[NotFound] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    hidden: Optional[Hidden] = Field(None, description="Omitted when nothing counts what is "
                                                       "hidden about this record.")
    trail: Optional[EvidenceTrail] = Field(None, description="A purchase order's lateness "
                                                             "trail (how we know).",
                                           json_schema_extra=_optional)
    customer: Optional[CustomerSummary] = Field(
        None, json_schema_extra=_optional,
        description="A customer's revenue, orders and contacts (customers only).")
    profitability: Optional[Profitability] = Field(
        None, json_schema_extra=_optional,
        description="A BC customer's or sales order's margin and why (the margin section).")
    complete: bool = Field(
        True, description="False on a quick first read (?quick=1): the record, its "
                          "timeline and links without the checks (lateness, landed cost, "
                          "people, how we know), which the full read adds.")
    freshness: Freshness
    access_label: Optional[str] = Field(
        None, description="'Viewing with full-read access' when served under the reader's "
                          "declared full-read grant.")
    proposals: list[Proposal] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="Follow-up drafts and proposed updates built from this page's gated "
                    "facts and trail; never sent or applied.")


PAGE_SECTIONS = ("late", "people", "landed_cost", "trail", "margin")


class ObjectSection(_Model):
    """One slow part of a record's page, read after the quick first read (``?quick=1``)
    and merged into it by the app: ``late`` (the late-PO check: ``status`` when it
    establishes one, and ``late_lines``), ``people`` (people on the record),
    ``landed_cost``, ``trail`` (how we know, with the page's ``not_found`` said against
    it), ``margin`` (a BC customer's or sales order's margin and why). The full page (no ``quick``) is the quick read with every section merged."""
    object_id: str
    section: Literal["late", "people", "landed_cost", "trail", "margin"]
    applies: bool = Field(True, description="False: this kind of record has no such "
                                            "section (nothing to show, nothing withheld).")
    status: Optional[ObjectStatus] = Field(
        None, json_schema_extra=_optional,
        description="late: the record's status as the late-PO check established it "
                    "(replaces the quick read's); omitted when it established none.")
    late_lines: list[LateLine] = Field(default_factory=list)
    people: Optional[list[Person]] = Field(None, json_schema_extra=_optional)
    landed_cost: Optional[LandedCost] = Field(None, json_schema_extra=_optional)
    trail: Optional[EvidenceTrail] = Field(None, json_schema_extra=_optional)
    profitability: Optional[Profitability] = Field(
        None, json_schema_extra=_optional,
        description="margin: a BC customer's margin and why, or a BC sales order's (its "
                    "customer's, led by the order).")
    not_found: Optional[list[NotFound]] = Field(
        None, json_schema_extra=_optional,
        description="trail: the page's not-found items said against the trail (replace "
                    "the quick read's); omitted for the other sections.")
    proposals: Optional[list[Proposal]] = Field(
        None, json_schema_extra=_optional,
        description="trail: the page's follow-up drafts and proposed updates, built from "
                    "the record, its late lines and this trail (replace the quick read's); "
                    "never sent or applied. Omitted for the other sections.")
    access_label: Optional[str] = None


# ---- source view -----------------------------------------------------------------------

class SourceView(_Model):
    """One cited source opened for this reader (``open_url`` of a Source is the app page
    ``/source?handle=<handle>``, which reads this). Not found reads the same as no access."""
    handle: str
    system: System
    label: str
    kind: Literal["record", "text"]
    title: Optional[str] = None
    text: Optional[str] = None
    fields: Optional[dict[str, str]] = None
    as_of: Optional[str] = None
    text_offset: int = 0
    next_offset: Optional[int] = None
    changed_since: bool = False
    access_label: Optional[str] = Field(
        None, description="'Viewing with full-read access' when served under the reader's "
                          "declared full-read grant.")


# ---- feedback --------------------------------------------------------------------------

class FeedbackRequest(_Model):
    answer_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    rating: Literal[1, -1]
    note: Optional[str] = Field(None, max_length=2000,
                                description="'What was wrong', in the person's words.")


class FeedbackResponse(_Model):
    ok: bool
    feedback_id: str
    stored_in: list[Literal["search.feedback", "feedback_log"]]


AnyEvent = Union[UnderstandingEvent, StatusEvent, SourcesEvent, TokenEvent, AnswerPayload,
                 ErrorBody]


# ---- briefing --------------------------------------------------------------------------

EventType = Literal["po_late", "po_due", "eta_slipped", "vendor_no_reply",
                    "vendor_terms_unapplied", "negative_margin", "lot_near_expiry",
                    "lot_on_hold", "quote_accepted_no_order", "quantity_mismatch",
                    "customer_order_at_risk", "po_undated"]


class BriefingItem(_Model):
    """One event routed to this reader, as its own grant shows it."""
    rank: int = Field(ge=1)
    id: str
    event_type: EventType
    label: str = Field(description="The event type in words, e.g. 'Late purchase order'.")
    evidence: Literal["live", "fixture-only"] = Field(
        "fixture-only", description="'live': a recorded run of this event's check over this "
                                    "data has found one; else 'fixture-only'.")
    object_id: str
    object_type: str
    subject_label: str = Field(description="The record's display label for this reader.")
    counterpart: Optional[str] = Field(None, description="A purchase order's supplier as this "
                                                         "reader reads it (one supplier only); "
                                                         "null when not readable or not a PO.")
    summary: str = Field(description="What happened; every date and figure is cited.")
    why: Optional[str] = None
    next_action: NextAction
    amount: Optional[Money] = Field(None, description="Money involved: a cited figure this "
                                                      "reader may open; null otherwise. When "
                                                      "amount_suspect is set it is NOT a fact: "
                                                      "show the reason, never the figure.")
    amount_basis: Optional[str] = Field(None, description="What the amount is: total_cost, "
                                                          "total_amount, loss, "
                                                          "margin_at_stake; or, never "
                                                          "ranked: unit_price_sum (a Portal "
                                                          "total adding unit prices), "
                                                          "portal_lines_unpriced, "
                                                          "not_checked_against_lines.")
    amount_suspect: Optional[str] = Field(None, description="Why the source's total looks "
                                                            "wrong (suspect source data); "
                                                            "never ranked by.")
    amount_disagree: bool = Field(False, description="The total and its own lines disagree: "
                                                     "both shown, neither ranked by.")
    lines_sum: Optional[Money] = Field(None, description="What its lines sum to, when they "
                                                         "disagree and this reader may open "
                                                         "every line.")
    orders_affected: Optional[int] = Field(None, ge=0)
    days: Optional[int] = Field(None, description="Days late, slipped, waiting or to expiry, "
                                                  "between cited dates.")
    acknowledged: bool = False
    new_since_visit: bool
    new_this_generation: bool
    first_seen: Optional[str] = None
    open_url: str
    sources: list[Source] = Field(default_factory=list)
    trail: Optional[EvidenceTrail] = Field(None, description="How we know, read as this "
                                                             "reader (top items only).")


class BriefingGroup(_Model):
    """Every item of one event type in the briefing: its count, and how many of those are
    shown among the top items; the rest open as a list (``/api/briefing/items``)."""
    event_type: EventType
    label: str
    count: int = Field(ge=0)
    in_top: int = Field(ge=0)
    new_since_visit: int = Field(ge=0)
    acknowledged: int = Field(ge=0)


class BriefingCheck(_Model):
    """One check behind the event types, as this run left it: never a silent gap."""
    check: str
    label: str
    status: Literal["complete", "partial", "not_checked", "failed"]
    words: str = Field(description="e.g. 'vendor replies: not checked this run'.")


class Briefing(_Model):
    """Your briefing: the events routed to you that you may see, ranked; the top items one
    by one and every other one in its type's group; each check's state; a preview of the
    daily digest. Suggestions only; nothing is sent or written."""
    status: Literal["read", "not_configured", "unavailable"]
    count: int = Field(ge=0)
    counts_exact: bool = Field(True, description="False: the read stopped at its cap and the "
                                                 "counts are 'at least'.")
    new_since_visit: int = Field(ge=0)
    since_visit: bool = Field(False, description="Only what is new since your last visit.")
    acknowledged: int = Field(0, ge=0)
    late_total: Optional[int] = Field(None, ge=0, description="Late purchase orders: Home's "
                                                              "count and rule.")
    other_teams: int = Field(ge=0, description="Events you may see routed to other teams.")
    hidden: Optional[int] = Field(None, ge=0, description="Events for your teams in places "
                                                          "you can't open: counted, never "
                                                          "named.")
    hidden_by_type: dict[str, int] = Field(default_factory=dict, description="The hidden "
                                           "count per event type (types whose policy gives "
                                           "no count are absent).")
    rank_basis: str
    routing_basis: str
    departments: list[str] = Field(default_factory=list)
    last_visit: Optional[str] = None
    as_of: Optional[str] = None
    complete: bool
    refreshing: bool = Field(False, description="True while some checks' findings are of "
                                                "older data than the records now show: those "
                                                "are left out until the checks re-run on it.")
    checked_on: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="While refreshing: when the checks left out last ran (ISO time).")
    more: bool = False
    items: list[BriefingItem] = Field(default_factory=list, description="The top items.")
    groups: list[BriefingGroup] = Field(default_factory=list)
    checks: list[BriefingCheck] = Field(default_factory=list)
    types: list[BriefingEventType] = Field(default_factory=list)
    today: str = Field("", description="Today in one line: what the latest check (or your "
                                       "last visit) found new, worst first, else what is "
                                       "still open.")
    digest_preview: str = Field(description="The daily digest text this reader would get. "
                                            "Preview only: never sent.")
    digest_sent: Literal[False] = False
    test_records_hidden: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="How many test or placeholder records were left out, in words "
                    "('2 test records hidden'); null for none.")


class BriefingEventType(_Model):
    """One event type: its checks' state this run and whether a recorded run over this data
    has found one of this type."""
    event_type: EventType
    label: str
    status: Literal["complete", "partial", "not_checked", "failed"]
    words: str
    found_live: Optional[bool] = Field(None, description="Null where that is withheld.")
    first_found: Optional[str] = None


class BriefingItems(_Model):
    """One event type's items of your briefing past the top ones, a page at a time (the
    group's "+N more": never the top items again)."""
    event_type: EventType
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    items: list[BriefingItem] = Field(default_factory=list)
    more: bool = False


class BriefingSeen(_Model):
    ok: bool
    at: Optional[str] = None


# ---- proactive suggestions ("needs your attention: what to do") -------------------------

class DraftTo(_Model):
    name: Optional[str] = None
    address: Optional[str] = Field(None, description="Only an address the trail cleared "
                                                     "for this reader (a vendor-side sender "
                                                     "of a message it may open); else null.")
    basis: str = Field(description="Where the recipient comes from, or what is missing.")
    source: Optional[Source] = None


class DraftFact(_Model):
    fact: str
    value: str
    source: Source


class SuggestionDraft(_Model):
    """A message a person may copy, edit and send themselves: worded only from cited values
    this reader may open, never from a message's text. Never sent by the Workbench."""
    status: Literal["draft_not_sent"] = "draft_not_sent"
    sent: Literal[False] = False
    channel: Literal["email", "internal_note"]
    audience: Literal["vendor", "customer", "internal"]
    to: DraftTo
    subject: str
    body: str
    facts: list[DraftFact] = Field(default_factory=list, description="Each stated fact "
                                                                     "and its source.")
    review: Optional[str] = None


class SuggestionEta(_Model):
    answer: Literal["yes", "no", "possibly", "conflicting", "hidden", "unknown"] = Field(
        description="ETA confirmed by the vendor? Unknown states are 'unknown', never yes.")
    when: Optional[str] = Field(None, description="The vendor's confirmed date (yes only, "
                                                  "and only beside a source you can open).")
    in_mail: bool = Field(False, description="The confirmation is an email.")
    source: Optional[Source] = None
    text: str = ""


class SuggestionReason(_Model):
    text: str
    source: Optional[Source] = None


class SuggestionImpact(_Model):
    amount: Optional[Money] = None
    amount_basis: Optional[str] = None
    amount_suspect: Optional[str] = Field(None, description="Why the total looks wrong in "
                                                            "the source: never ranked by.")
    days_late: Optional[int] = None
    days_at_risk: Optional[int] = Field(None, description="The worst customer order's days "
                                        "at risk (a lower bound).")
    orders_at_risk: int = Field(0, ge=0)
    orders: list[ObjectRef] = Field(default_factory=list)


class SuggestionConfidence(_Model):
    level: Literal["high", "medium", "low"]
    why: str


class ProactiveSuggestion(_Model):
    """One record that needs attention: what we noticed, how we know, what to do and a
    draft message (never sent)."""
    id: str
    kind: Literal["vendor_chase", "customer_update", "margin_review"]
    event_types: list[str] = Field(default_factory=list)
    rank: int = Field(ge=0)
    headline: str
    subject: ObjectRef
    impact: SuggestionImpact
    eta: Optional[SuggestionEta] = None
    reasoning: list[SuggestionReason] = Field(default_factory=list)
    action: NextAction
    draft: Optional[SuggestionDraft] = None
    confidence: SuggestionConfidence
    missing: list[str] = Field(default_factory=list)
    acknowledged: bool = False
    new: bool = False


class Suggestions(_Model):
    """What needs your attention and what to do about it, ranked by impact (customer
    orders held up, money within its currency, days late); every other open finding is
    counted by type. Suggestions only: nothing is sent or written."""
    status: Literal["read", "not_configured", "unavailable"]
    total: int = Field(ge=0, description="Records with a suggestion (all, not only shown).")
    more: int = Field(0, ge=0)
    by_kind: dict[str, int] = Field(default_factory=dict)
    collapsed: dict[str, int] = Field(default_factory=dict, description="Open findings of "
                                      "other types (no drafted action), counted per type.")
    items: list[ProactiveSuggestion] = Field(default_factory=list)
    rank_basis: str = ""
    note: str
    as_of: Optional[str] = None
    complete: bool = False
    sent: Literal[False] = False
