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


class Freshness(_Model):
    as_of: Optional[str] = Field(None, description="Oldest capture an answer relies on (ISO).")
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


class EtaCheck(_Model):
    state: Literal["confirmed", "promises_disagree", "lead_only", "no_confirmation",
                   "no_confirmation_found", "not_determined", "withheld", "not_applicable"]
    text: str = Field(description="Is the ETA backed by a vendor confirmation, in words.")
    source: Optional[Source] = Field(None, description="The confirmation (or the lead) "
                                                       "that says so, when the reader can "
                                                       "open it.")


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


ASK_EVENTS: dict[str, type[_Model]] = {
    "understanding": UnderstandingEvent, "status": StatusEvent, "sources": SourcesEvent,
    "token": TokenEvent, "answer": AnswerPayload, "error": ErrorBody}


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
    reason: str
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
    kind: Literal["item_ledger", "credit_memo", "return", "material_po", "quote"]
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
    gaps: list[NotFound] = Field(default_factory=list)


class OrderMargin(_Model):
    order: ObjectRef
    date: Optional[str] = None
    revenue: Optional[Money] = None
    booked_cost: Optional[Money] = None
    margin: Optional[Money] = None
    margin_pct: Optional[str] = None
    gap: Optional[str] = Field(None, description="Why no margin is stated, in words.")


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
    orders: list[OrderMargin] = Field(default_factory=list, description="Worst margin first.")
    orders_more: int = Field(0, ge=0, description="Orders read but not listed (the list "
                                                  "stops at 30).")
    breakdown: Optional[CostBreakdown] = None
    next_action: Optional[NextAction] = Field(None, description="A suggested next step "
                                                                "from the facts only.")


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
    freshness: Freshness
    access_label: Optional[str] = Field(
        None, description="'Viewing with full-read access' when served under the reader's "
                          "declared full-read grant.")


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
