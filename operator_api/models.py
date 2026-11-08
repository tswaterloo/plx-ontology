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

from typing import Any, Literal, Optional, Union

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
    feedback_admin: bool = Field(False, json_schema_extra=_optional,
                                 description="The owner or a feedback admin: triages every "
                                             "reader's feedback.")


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
    searchable_to: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="With hybrid search on: the newest text of this source search can "
                    "find (ISO). Captured records after it are not searchable yet.")


class TruthFlag(_Model):
    """The latest cross-system truth check (``operating.truth_check``): how many of the
    open claims the engine makes the other systems' evidence contradicts."""
    checked_at: str = Field(description="When the truth check ran (ISO).")
    late_claims: int = Field(0, description="Purchase orders claimed late (past their need "
                             "date), one per order; unconfirmed orders not yet due are not "
                             "counted.")
    late_contradicted: int = Field(0, description="Of those, the orders contradicted by evidence in "
                                   "another system (received, cancelled, a test record, a "
                                   "draft twin, or a need date that is a default).")
    claims: int = Field(0, description="Every open claim checked.")
    contradicted: int = Field(0, description="Every open claim contradicted by evidence.")
    detectors_incomplete: int = Field(0, description="Detectors whose latest run did not "
                                      "complete (their claims are partly checked).")
    stale: bool = Field(False, description="The check is older than TRUTH_MAX_AGE (2 hours): "
                        "its counts are not stated in the label.")


class BuildWait(_Model):
    """The next projection build waits for disk space (``operating.host.build_wait``):
    the last one was skipped because its estimate did not fit, and the host builds again
    once this much is free; captures go on meanwhile."""
    needed_bytes: int = Field(description="Free bytes the next build waits for: its "
                              "estimate, margin and input copies.")
    free_bytes: Optional[int] = Field(None, description="Free bytes now (the latest check's "
                                      "when the disk cannot be measured here).")
    checked_at: Optional[str] = Field(None, description="When the host last checked (ISO).")
    mode: Optional[Literal["cold", "incremental"]] = Field(
        None, description="The build the estimate is for.")


class DocumentsPending(_Model):
    """Documents the latest delta cycle has not read yet (its reading budget ran out or a
    file timed out): the records holding them are current, their document fields pending;
    the served generation's document objects stay served until a later cycle reads them.
    Counts only, without the excluded people's mail or OneDrives."""
    files: Optional[int] = Field(None, description="Files not read yet; omitted when some "
                                 "are held by an excluded person's mail or OneDrive (the "
                                 "count could not leave theirs out).")
    timed_out: Optional[int] = Field(None, description="Of those, files whose read timed "
                                     "out; omitted with files.")
    records: int = Field(0, description="Records whose document fields are pending, not "
                         "counting the excluded people's mail or OneDrives.")
    this_record: Optional[bool] = Field(
        None, json_schema_extra=_optional,
        description="On a record's page: whether this record's document fields are "
                    "pending.")


class DerivedCode(_Model):
    """Derived data on screen made by older code than the code deployed
    (``operating.derived_status``): being refreshed, or flagged when model-made."""
    stale: list[str] = Field(default_factory=list, description="Artifacts made by older "
                             "code, being refreshed (their findings are held back or "
                             "labelled meanwhile).")
    flagged: list[str] = Field(default_factory=list, description="Model-made artifacts of "
                               "older code: labelled, refreshed by their own schedule.")
    titles: dict[str, str] = Field(default_factory=dict, description="Each named artifact's "
                                   "title.")
    checked_at: Optional[str] = Field(None, description="When it was checked (ISO).")


class CaptureAlarm(_Model):
    """Capture sources that have not succeeded for twice their normal interval, or a
    capture service that keeps failing to start (``operating.ops_health``)."""
    stalled: list[str] = Field(default_factory=list, description="Capture sources past "
                               "twice their normal interval.")
    start_loop: bool = Field(False, description="The capture service keeps failing to start.")
    line: str = Field(description="The health line Home shows, e.g. 'captures not "
                      "refreshed: mail, teams (oldest 23 h)'.")


class Freshness(_Model):
    as_of: Optional[str] = Field(None, description="The data cut-off (ISO): the newest "
                                 "capture the served data holds, the same on every screen. "
                                 "A source read over a day before it is named in the label "
                                 "and keeps its own time in sources.")
    label: Optional[str] = Field(None, description="e.g. 'Data as of 10:42 today'.")
    sources: list[FreshnessSource] = Field(default_factory=list)
    truth: Optional[TruthFlag] = Field(None, json_schema_extra=_optional,
                                       description="The latest truth check's counts; the "
                                       "label names a late list the evidence disputes.")
    build_wait: Optional[BuildWait] = Field(
        None, json_schema_extra=_optional,
        description="The next projection build waits for disk space; the label says how "
                    "much ('next build waits for 374 GB free').")
    documents: Optional[DocumentsPending] = Field(
        None, json_schema_extra=_optional,
        description="Documents the latest delta cycle has not read yet; the label names "
                    "them, and on a record's page whether its document fields are pending.")
    derived_code: Optional[DerivedCode] = Field(
        None, json_schema_extra=_optional,
        description="Derived data made by older code than the code deployed: the label "
                    "says it is being refreshed; never presented as current.")
    capture_alarm: Optional[CaptureAlarm] = Field(
        None, json_schema_extra=_optional,
        description="Set when captures are stalled: Home shows its line.")


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


class DueDate(_Model):
    """The order's due date as the one PO reader decides it, with where it comes from."""
    date: str = Field(description="The due date (ISO).")
    basis: Literal["requested", "vendor_eta", "lead_time"] = Field(
        description="requested: a requested-by date a record states; vendor_eta: the "
                    "supplier's confirmed date (no record states one); lead_time: the order "
                    "date plus the order's lead time. A default date is never a due date.")
    source: Source = Field(description="The cited value it rests on, opened for this "
                                       "reader.")


class CitedText(_Model):
    value: str
    source: Source


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
    due: Optional[DueDate] = Field(None, json_schema_extra=_optional,
                                   description="The due date and its basis; omitted when "
                                               "none is decided or its value is not this "
                                               "reader's to open.")
    vendor_number: Optional[CitedText] = Field(
        None, json_schema_extra=_optional,
        description="The order number the supplier knows (a FileMaker-era order's "
                    "FileMaker PO number), when every copy holds one; omitted otherwise.")


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
    source: Optional[str] = Field(None, max_length=4096,
                                  description="Ask about this document: the citation of a "
                                              "mail, attachment or file the person has open "
                                              "or picked; read for them as they may read it.")


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
    confirmation: Optional[Literal["none_found", "found_unverified", "needs_check",
                                   "confirmed"]] = Field(
        None, json_schema_extra=_optional,
        description="none_found: no supplier confirmation is linked to any copy of the "
                    "order. found_unverified: one is linked, only as an unverified lead. "
                    "needs_check: a lead names a number another order also holds, so it "
                    "cannot be tied to this order. confirmed: a settled confirmation is "
                    "linked. Null: the list's entry does not say (the order's page decides "
                    "it).")
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
    stale_since: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when the server could not read this page in time and serves the "
                    "last page it read for this same reader: when that page was read "
                    "(ISO). It is refreshed in the background.")


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
    stale_since: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when the server could not read this page in time and serves the "
                    "last page it read for this same reader instead: when that page was "
                    "read (ISO). It is refreshed in the background.")


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


class ProfitDriver(_Model):
    """One reason a customer's margin is what it is, over its checked orders only."""
    kind: Literal["losing_orders", "low_margin_item", "credit", "discount", "freight"]
    text: str = Field(description="The reason in one plain sentence; every figure in it is "
                                  "``amount`` or a checked order's own.")
    established: bool = Field(description="False: not established from the records (the "
                                          "text says so and ``where_it_would_live`` where "
                                          "it would be read); no figure is stated.")
    amount: Optional[Money] = Field(None, description="Its effect on the margin, signed.")
    where_it_would_live: Optional[str] = None
    sources: list[Source] = Field(default_factory=list, description="The cited records "
                                                                    "this reader may open.")


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
    drivers: list[ProfitDriver] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="Why the customer makes or loses money (losing orders, low-margin items, "
                    "credits; discounts and freight said as not established), over the "
                    "checked orders only, one currency each.")
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


class LinkRef(ObjectRef):
    tier: Optional[str] = Field(None, description="How firmly the link holds: verified, "
                                                  "lead (a machine-read reference, e.g. a "
                                                  "mail naming the order), ...")
    evidence: list[Source] = Field(default_factory=list, description=(
        "The records and messages the link was read from that this reader may open, mail "
        "and documents first; one the reader may not open is left out."))
    possible: bool = Field(False, json_schema_extra=_optional, description="Reached only through a lead or disputed "
                                              "link (possibly related): a suggested link, "
                                              "never in the group's count.")


class LinkGroup(_Model):
    link_type: str = Field(description="The schema's link type, e.g. SalesInvoice.bills.")
    direction: Literal["forward", "reverse"]
    name: str = Field(description="The link's navigation name on this record's type "
                                  "(the schema's name forward, reverse_name in reverse).")
    words: str = Field(description="The navigation name in plain words, e.g. 'Invoices'.")
    type: str = Field(description="The type of the records at the link's other end.")
    role: Literal["customer", "fulfilment", "same_record", "related"] = Field(
        description="Where the page shows the group: the order's customer, its shipments "
                    "and invoices, the same record in another system, or other links.")
    count: Optional[int] = Field(None, description="How many records solid links reach as "
                                                   "this reader reads it (possible ones "
                                                   "never counted); null when not counted.")
    possible: int = Field(0, json_schema_extra=_optional, description="How many more records only lead or disputed "
                                         "links reach (possibly related), never in "
                                         "``count``; the first of them are in ``records`` "
                                         "marked ``possible``.")
    records: list[LinkRef] = Field(default_factory=list)


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


class KnownRecord(_Model):
    """A customer, supplier or item a person corresponds about: how many of their messages
    the reader may read name it."""
    object_id: str
    label: str
    url: str
    messages: int


class PersonKnows(_Model):
    """Who knows what (``plx.aggregate`` mode ``who_knows``): what one person of ours
    corresponds about most, counted over the messages the reader may read, and the open
    threads waiting on their reply. Counts only: never a message's text."""
    messages: int
    customers: list[KnownRecord] = Field(default_factory=list)
    suppliers: list[KnownRecord] = Field(default_factory=list)
    items: list[KnownRecord] = Field(default_factory=list)
    waiting_on_them: Optional[int] = Field(
        None, json_schema_extra=_optional,
        description="Open threads waiting on this person's reply; omitted when unknown.")
    complete: bool = Field(False, description="False: some people or threads could not be "
                                              "decided, so the counts may be low.")


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
    links: list[LinkGroup] = Field(default_factory=list, description=(
        "The records one hop away, one group per link kind the schema declares for this "
        "record's type, each record with the evidence the link was read from."))
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
    knows: Optional[PersonKnows] = Field(
        None, json_schema_extra=_optional,
        description="A person's: what they correspond about most (the knows section).")
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
    stale_since: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when the server could not read this page in time and serves the "
                    "last page it read for this same reader: when that page was read "
                    "(ISO). It is refreshed in the background.")


PAGE_SECTIONS = ("late", "people", "landed_cost", "trail", "margin", "knows")


class ExploreNode(_Model):
    object_id: str
    type: str
    label: str
    open_url: str
    tier: Literal["firm", "suggested"] = Field(
        description="firm: a verified, reviewed or corroborated link; suggested: a "
                    "text-matched or lead link that has not been confirmed.")


class ExploreGroup(_Model):
    key: str = Field(description="The link type and direction, stable across reads.")
    label: str = Field(description="The kind of link in words, e.g. 'Sales orders: ordered by'.")
    nodes: list[ExploreNode] = Field(default_factory=list)
    more: bool = Field(False, description="True when this reader's walk of this kind was "
                                          "cut at the cap; never a count.")


class HistoryChange(_Model):
    property: str
    label: str = Field(description="The property in words, e.g. 'PO number'.")
    before: list[str] = Field(default_factory=list, description="Its values just before.")
    after: list[str] = Field(default_factory=list, description="Its values from then on.")
    more: bool = Field(False, description="True when more values were recorded than shown.")


class HistoryEntry(_Model):
    at: str = Field(description="When the capture first held it (ISO, UTC).")
    kind: Literal["first_recorded", "changed", "link_added", "link_removed",
                  "removed_from_source", "restored"]
    title: str = Field(description="What happened, in words.")
    source: Optional[str] = Field(None, description="Who or which system wrote it, e.g. "
                                                    "'BC purchase order'; a link's basis.")
    source_handle: Optional[str] = Field(None, description="The captured version it was "
                                                           "read from (an L1 handle).")
    changes: list[HistoryChange] = Field(default_factory=list)
    more_changes: bool = False
    link: Optional[ExploreNode] = Field(None, description="The record linked or unlinked.")
    link_label: Optional[str] = Field(None, description="The kind of link in words.")


class RecordHistory(_Model):
    """A record's change log as this reader may read it, newest first, a page at a time.
    A change the reader may not read (a value marked beyond them then or since, a record
    they cannot open) is left out and ``partial`` says so, never how many."""
    object_id: str
    type: str
    label: str
    entries: list[HistoryEntry] = Field(default_factory=list)
    next_cursor: Optional[str] = Field(None, description="Pass as cursor for older changes.")
    partial: bool = False
    message: Optional[str] = None
    access_label: Optional[str] = Field(
        None, description="Set when read under a full-read grant (journalled).")


class AsOfValue(_Model):
    label: str
    values: list[str] = Field(default_factory=list)
    source: Optional[str] = None


class AsOfLinks(_Model):
    label: str
    count: int


class RecordAsOf(_Model):
    """The record as the Workbench had recorded it at a time: its values and its links
    counted per kind, as this reader may read them (markings judged then and since)."""
    object_id: str
    type: str
    label: str
    at: str = Field(description="The time read (the end of the day for a bare date).")
    values: list[AsOfValue] = Field(default_factory=list)
    links: list[AsOfLinks] = Field(default_factory=list)
    partial: bool = False
    message: Optional[str] = None
    access_label: Optional[str] = Field(
        None, description="Set when read under a full-read grant (journalled).")


class Explore(_Model):
    """A record's neighbours one hop out, grouped by kind of link, for the link explorer.
    Walking on is the same read from a neighbour (lazy expansion), so two hops are two
    reads, each as this reader sees it. No counts of what the reader may not see."""
    object_id: str
    type: str
    label: str
    groups: list[ExploreGroup] = Field(default_factory=list)
    partial: bool = Field(False, description="True when some linked records are not visible "
                                             "to this reader (their number is never given).")
    capped: bool = Field(False, description="True when kinds of link or records were left "
                                            "out to keep the read small.")
    message: Optional[str] = None


class ImpactOrder(_Model):
    """One customer order a what-if change reaches (an order and its twins, once)."""
    order: ObjectRef
    tier: Literal["firm", "lead"] = Field(description="firm: every link on the chain is "
                                          "verified, reviewed or corroborated; lead: possible.")
    required_date: Optional[str] = None
    new_material_date: Optional[str] = None
    late_by_days: Optional[int] = None
    slips: Optional[bool] = None
    revenue: Optional[Money] = Field(None, description="Finance: only for a reader shown it.")


class ImpactMoney(_Model):
    measure: Literal["revenue_at_risk", "revenue_lost", "revenue_change", "cost_change"]
    money: Money
    orders: int
    tier: Literal["firm", "lead"]


class ImpactCause(_Model):
    """One cause of a late or unprofitable order, citing the records it rests on."""
    cause: Literal["late_supply", "supplier_no_reply", "short_receipt", "qc_hold", "cost_jump",
                   "freight"]
    record: ObjectRef
    tier: Literal["firm", "lead"]
    detail: str
    sources: list[str] = Field(default_factory=list, description="Cited L1 handles.")


class ImpactPanel(_Model):
    """A record page's Impact panel (``plx.around`` mode what_if or why, as this reader
    reads it). ``unknown``: what could not be established, never said to be absent.
    ``partial``: a bound (depth, fan-out, time, list size) cut the walk, or some linked
    records are not visible to this reader (``partial_reason`` withheld_by_permission)."""
    object_id: str
    mode: Literal["what_if", "why"]
    change: Optional[str] = Field(None, description="what_if: the change in words.")
    orders: list[ImpactOrder] = Field(default_factory=list)
    orders_total: int = 0
    customers: list[ObjectRef] = Field(default_factory=list)
    money: list[ImpactMoney] = Field(default_factory=list)
    causes: list[ImpactCause] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    partial: bool = False
    partial_reason: Optional[str] = None
    applies: bool = Field(True, description="False: this kind of record has no impact walk.")


class ObjectSection(_Model):
    """One slow part of a record's page, read after the quick first read (``?quick=1``)
    and merged into it by the app: ``late`` (the late-PO check: ``status`` when it
    establishes one, and ``late_lines``), ``people`` (people on the record),
    ``landed_cost``, ``trail`` (how we know, with the page's ``not_found`` said against
    it), ``margin`` (a BC customer's or sales order's margin and why). The full page (no ``quick``) is the quick read with every section merged."""
    object_id: str
    section: Literal["late", "people", "landed_cost", "trail", "margin", "knows"]
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
    knows: Optional[PersonKnows] = Field(
        None, json_schema_extra=_optional,
        description="knows: on a person's page, the customers, suppliers and items they "
                    "correspond about most and the threads waiting on them.")
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
    stale_since: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when the server could not read this section in time and serves "
                    "the last one it read for this same reader: when that was read (ISO). "
                    "It is refreshed in the background.")


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

_APP_VERSION = Field(None, max_length=40, pattern=r"^[A-Za-z0-9._+-]+$",
                    description="The app's version, as the browser reports it.")
_PAGE = Field(None, max_length=300, description="The app path the feedback was given on.")


class FeedbackRequest(_Model):
    answer_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    rating: Literal[1, -1]
    note: Optional[str] = Field(None, max_length=2000,
                                description="'What was wrong', in the person's words.")
    expected: Optional[str] = Field(None, max_length=2000, json_schema_extra=_optional,
                                    description="'The right answer is', in the person's "
                                                "words (a thumbs-down only).")
    claims: Optional[list[int]] = Field(None, max_length=50, json_schema_extra=_optional,
                                        description="The answer's facts (their index) the "
                                                    "person marked wrong.")
    page: Optional[str] = _PAGE
    app_version: Optional[str] = _APP_VERSION


class FeedbackResponse(_Model):
    ok: bool
    feedback_id: str
    stored_in: list[Literal["search.feedback", "feedback_log", "feedback_store"]]


FeedbackKind = Literal["answer", "alert", "page", "record", "list", "ui", "general"]
FeedbackStatus = Literal["new", "accepted", "fixed", "wont_fix"]
ItemVerdict = Literal["useful", "not_useful", "wrong"]


class ItemFeedbackRequest(_Model):
    """A briefing or alert item: useful, not useful or wrong."""
    item_id: str = Field(min_length=1, max_length=200)
    verdict: ItemVerdict
    item_kind: Literal["briefing", "watcher", "suggestion", "today"] = "briefing"
    event_type: Optional[str] = Field(None, max_length=80)
    object_type: Optional[str] = Field(None, max_length=40)
    object_id: Optional[str] = Field(None, max_length=200)
    label: Optional[str] = Field(None, max_length=300)
    note: Optional[str] = Field(None, max_length=2000)
    page: Optional[str] = _PAGE
    app_version: Optional[str] = _APP_VERSION


class NoteFeedbackRequest(_Model):
    """A free note on a page, a record, a list, the app itself or anything else."""
    kind: Literal["page", "record", "list", "ui", "general"] = "general"
    note: str = Field(min_length=1, max_length=2000)
    page: Optional[str] = _PAGE
    object_type: Optional[str] = Field(None, max_length=40)
    object_id: Optional[str] = Field(None, max_length=200)
    label: Optional[str] = Field(None, max_length=300)
    app_version: Optional[str] = _APP_VERSION


class FeedbackClaim(_Model):
    i: int
    text: Optional[str] = None
    citations: list[dict[str, Optional[str]]] = Field(default_factory=list)


class FeedbackEntry(_Model):
    id: str
    at: str
    kind: FeedbackKind
    status: FeedbackStatus
    mine: bool
    reader_name: Optional[str] = None
    rating: Optional[int] = None
    verdict: Optional[ItemVerdict] = None
    note: Optional[str] = None
    expected: Optional[str] = None
    page: Optional[str] = None
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    label: Optional[str] = None
    item_id: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    claims: list[FeedbackClaim] = Field(default_factory=list)
    flagged: list[int] = Field(default_factory=list)
    served_version: Optional[str] = None
    app_version: Optional[str] = None
    # what is not shown to this reader: the markings (and ``source``) it lacks
    withheld: list[str] = Field(default_factory=list)


class FeedbackList(_Model):
    """Your feedback; every reader's for the owner and the admins (``all_readers``)."""
    all_readers: bool
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    counts: dict[str, int] = Field(description="Entries per status under the other filters.")
    entries: list[FeedbackEntry]


class FeedbackStatusRequest(_Model):
    status: FeedbackStatus


AnyEvent = Union[UnderstandingEvent, StatusEvent, SourcesEvent, TokenEvent, AnswerPayload,
                 ErrorBody]


# ---- briefing --------------------------------------------------------------------------

EventType = Literal["po_late", "po_due", "eta_slipped", "vendor_no_reply",
                    "vendor_terms_unapplied", "negative_margin", "lot_near_expiry",
                    "lot_on_hold", "quote_accepted_no_order", "quantity_mismatch",
                    "customer_order_at_risk", "po_undated", "customer_order_late",
                    "watcher_attention", "stock_out_risk", "overdue_receivable",
                    "unit_cost_jump", "customer_demand_drop", "promise_date_slip",
                    "margin_erosion", "supplier_reliability",
                    "shipped_not_invoiced", "duplicate_customer_order",
                    "recurring_component_damage", "customer_chasing_order",
                    "tell_before_they_ask", "quality_hold_aging", "reject_trend",
                    "expedite_unanswered", "supplier_shortage_open_orders",
                    "supplier_chasing_payment",
                    "customer_paying_slower", "early_pay_discount_missed",
                    "duplicate_supplier_invoice", "duplicate_supplier_payment",
                    "customer_credits_rising",
                    "price_below_typical", "price_below_cost", "customer_price_drop",
                    "mc_task_overdue", "mc_task_stuck", "mc_approval_pending",
                    "planner_task_overdue", "aged_receivable_worsening",
                    "aged_payable_worsening", "production_late",
                    "assembly_damage_spike", "teams_unanswered_ask",
                    "commitment_due", "commitment_missed", "needs_my_reply",
                    "approval_waiting", "chase_escalating",
                    "slow_moving_stock", "expiring_lot_stock",
                    "assembly_blocked_by_component", "certificate_expiring",
                    "freight_cost_anomaly", "stale_quote", "priority_order_unfilled",
                    "duplicate_party_candidate",
                    "key_field_missing", "twins_disagree", "source_stale",
                    "reorder_soon",
                    "inventory_adjustment", "blocked_account_activity", "gl_posting_errors",
                    "recurring_task_missed", "label_change", "rd_project",
                    "unapplied_remittance", "short_payment", "statement_difference"]


class BriefingItem(_Model):
    """One event routed to this reader, as its own grant shows it."""
    rank: int = Field(ge=1)
    id: str
    event_type: EventType
    label: str = Field(description="The event type in words, e.g. 'Late purchase order'.")
    evidence: Literal["live", "fixture-only"] = Field(
        "fixture-only", description="'live': a recorded run of this event's check over this "
                                    "data has found one; else 'fixture-only'.")
    carried: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when this item is from an earlier or failed check, never current: "
                    "e.g. 'check failed at 2026-11-07 2:00 PM ET; last checked 2026-11-06 "
                    "2:00 PM ET'. Null: found by the latest check.")
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
    yours: bool = Field(False, description="About a record you are involved in (named on "
                                           "it, mentioned in your mail, or its supplier or "
                                           "customer writes to you), read as you.")
    why_yours: list[str] = Field(default_factory=list, description="Why it is yours, in "
                                                                   "words.")


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
    words: str = Field(description="e.g. 'vendor replies: checked (last run 2026-11-08 "
                                   "9:30 AM ET)'.")
    last_run: Optional[str] = Field(None, description="When the check last ran (ISO time).")


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
    carried: int = Field(0, ge=0, description="How many items are from an earlier or failed "
                                            "check (each marked by its carried words).")
    data_notes: list[str] = Field(default_factory=list, description=(
        "What the checks could not judge, per check and reason, as counts only: e.g. "
        "'late customer orders: cannot check 12 records that lack order lines'."))
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
    stale_since: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when the server could not read this page in time and serves the "
                    "last page it read for this same reader: when that page was read "
                    "(ISO). It is refreshed in the background.")
    yours: int = Field(0, ge=0, description="Items about records you are involved in; "
                                             "they are ranked first.")
    involvement: Literal["checked", "partly_checked", "no_identity"] = Field(
        "no_identity", description="Whether your involvement was checked for every item "
                                   "('partly_checked': some were not, they rank as not "
                                   "yours), or you have no identity on record.")


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


class WatcherItemStateRequest(_Model):
    """Dismiss or snooze one item the AI watcher flagged in your briefing; ``open`` undoes
    either. Only your own briefing changes; nothing is sent."""
    state: Literal["dismissed", "snoozed", "open"]
    until: Optional[str] = Field(None, max_length=10, description="A snooze's last day "
                                 "(YYYY-MM-DD, after today); absent: a week from today.")
    reason: Optional[str] = Field(None, max_length=500)


class WatcherItemState(_Model):
    item_id: str
    state: Literal["dismissed", "snoozed", "open"]
    until: Optional[str] = None
    at: str


# ---- watchlists and alert rules (``/api/watch*``) ---------------------------------------

class WatchRef(_Model):
    object_id: str
    type: Optional[str] = None
    label: Optional[str] = Field(None, description="A label from the record's unmarked "
                                 "name or number; null when it has none.")


class WatchFollowRequest(_Model):
    object_id: str = Field(min_length=1, max_length=400, description="The record's id, as "
                           "its page states it.")


class WatchFollow(_Model):
    object_id: str
    object_type: Optional[str] = None
    label: Optional[str] = None
    at: Optional[str] = None
    following: bool
    readable: bool = Field(description="False when your grant no longer admits the record: "
                           "it stays followed and tells you nothing until it does.")
    watching_since: Optional[str] = Field(None, description="When the incremental pass "
                                          "first read it; null: not yet.")


class WatchChange(_Model):
    property: str
    before: Optional[str] = None
    after: Optional[str] = None


class WatchAlert(_Model):
    alert_id: str
    at: str
    origin: Literal["follow", "rule"]
    kind: Literal["changed", "linked", "unlinked", "linked_changed", "removed", "rule"]
    sentence: str
    about: WatchRef = Field(description="The followed record, or a rule's subject or the "
                            "record its count is grouped by.")
    record: WatchRef = Field(description="The record that changed.")
    link_type: Optional[str] = None
    changes: list[WatchChange] = Field(description="Only values whose markings you hold.")
    rule_id: Optional[str] = None
    rule_text: Optional[str] = None
    counted: Optional[int] = Field(None, description="A windowed rule: the changes counted, "
                                   "every one on a record you may open.")
    read: bool


class WatchRule(_Model):
    rule_id: str
    text: str
    spec: dict[str, Any] = Field(description="The compiled rule (plx-alert-rule/v1).")
    description: str
    at: str
    state: Literal["active", "paused", "refused"]
    note: Optional[str] = None
    watching_since: Optional[str] = None


class Watchlist(_Model):
    """Your follows, your alert rules and your alerts, newest first, as your grant shows
    them now."""
    configured: bool
    follows: list[WatchFollow]
    rules: list[WatchRule]
    alerts: list[WatchAlert]
    unread: int


class WatchRuleCompileRequest(_Model):
    text: str = Field(min_length=1, max_length=300, description="The rule in plain words.")


class WatchRuleDraft(_Model):
    """The compiled rule shown back for confirmation; nothing is kept."""
    text: str
    spec: dict[str, Any]
    description: str = Field(description="What the rule will do, in plain words.")
    markings: list[str] = Field(description="The markings the figures it reads carry.")
    compiled_by: Literal["grammar", "model"]
    digest: str = Field(description="Send it back with the spec to confirm this very rule.")


class WatchRuleRequest(_Model):
    text: str = Field(min_length=1, max_length=300)
    spec: dict[str, Any]
    digest: str = Field(min_length=1, max_length=64)


class WatchRuleDeleted(_Model):
    rule_id: str
    deleted: bool


class WatchReadRequest(_Model):
    alert_ids: list[str] = Field(max_length=500)


class WatchReadResult(_Model):
    marked: int


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


ActionKind = Literal["send_email", "reply_in_thread", "forward", "teams_message",
                     "create_follow_up", "mark_done", "snooze", "dismiss", "assign"]
ActionStatus = Literal["pending", "sent", "failed", "unknown", "dry_run", "follow_up",
                       "done", "snoozed", "dismissed", "assigned"]


class CardActionOffer(_Model):
    """An action a card offers (``operating.card_actions``). Outbound ones send only after
    a preview and an explicit confirm by the signed-in person."""
    kind: ActionKind
    label: str
    outbound: bool


class ActionPayload(_Model):
    """An action's fields. Outbound: to, cc, subject (a new email only), body, and the
    cited message a reply or forward answers (``in_reply_to``, not editable) or the Teams
    chat. Local: a follow-up's due date and note, a snooze's end, a dismissal's reason."""
    to: Optional[list[str]] = Field(None, max_length=20)
    cc: Optional[list[str]] = Field(None, max_length=20)
    subject: Optional[str] = Field(None, max_length=1000)
    body: Optional[str] = Field(None, max_length=20000)
    in_reply_to: Optional[str] = Field(None, max_length=4096)
    thread_recipients: Optional[list[str]] = Field(
        None, max_length=50, description="Who a reply reaches as Graph addresses it (the "
        "message's Reply-To, else its sender), read from the message; never edited.")
    chat_id: Optional[str] = Field(None, max_length=300)
    due: Optional[str] = Field(None, max_length=10)
    until: Optional[str] = Field(None, max_length=10)
    reason: Optional[str] = Field(None, max_length=2000)
    note: Optional[str] = Field(None, max_length=2000)
    assignee: Optional[str] = Field(None, max_length=320, description="Assign to: a "
                                    "colleague's address (kept in the Workbench only).")


class ActionRequest(_Model):
    card_id: str = Field(min_length=1, max_length=200)
    kind: ActionKind
    payload: Optional[ActionPayload] = Field(None, description="The person's edits; "
                                             "absent: the drafted payload.")


class ActionConfirm(ActionRequest):
    confirm: bool = Field(description="True only from the person's own Send/Confirm click.")
    idempotency_key: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$",
                                 description="One per dialog: a second click returns the "
                                             "first result and never sends twice.")
    payload_hash: Optional[str] = Field(None, max_length=64, description="The preview's "
                                        "hash (outbound): what the person saw is what goes.")
    confirm_unknown_domains: bool = False
    checked_sent_items: bool = Field(False, description="The person checked Sent Items "
                                     "after an unknown outcome and the message is not there.")


class ActionCitation(_Model):
    fact: str
    value: str
    handle: str
    present: bool = Field(True, description="The final text still states this value.")


class GraphRequest(_Model):
    method: Literal["POST"]
    url: str
    body: dict[str, Any]


class TeamsChat(_Model):
    name: str
    chat_id: str


class ActionPreview(_Model):
    """The exact final payload and every check of it; nothing is done by a preview."""
    card_id: str
    kind: ActionKind
    label: str
    outbound: bool
    sender: Optional[str] = Field(None, description="Your own mailbox, the only sender.")
    payload: ActionPayload
    payload_hash: str
    citations: list[ActionCitation] = Field(default_factory=list)
    uncited: list[str] = Field(default_factory=list, description="Dates or quantities no "
                               "citation backs: blocking.")
    unknown_domains: list[str] = Field(default_factory=list, description="Recipient domains "
                                       "outside the known ones: need one more confirm.")
    problems: list[str] = Field(default_factory=list, description="Why it cannot be done.")
    missing: list[str] = Field(default_factory=list, description="What a live send lacks; "
                               "with any, confirming records a dry run and sends nothing.")
    request: Optional[GraphRequest] = None
    teams_chats: list[TeamsChat] = Field(default_factory=list)


class ActionResult(_Model):
    card_id: str
    kind: ActionKind
    status: ActionStatus
    text: str
    at: str
    seq: int
    missing: list[str] = Field(default_factory=list)
    replayed: bool = Field(False, description="The same confirm again: the first result, "
                                              "nothing done twice.")
    request: Optional[GraphRequest] = None


class PlaybookAddress(_Model):
    address: str
    source: str = Field(description="The citation the address is read from.")


class PlaybookEmail(_Model):
    """A drafted message to the record's outside party (never sent here: it goes only
    through ``/api/actions/preview`` and your confirm, ``card_id`` = the playbook's id)."""
    channel: Literal["email"]
    party: Optional[str] = None
    to: Optional[PlaybookAddress] = None
    subject: str
    body: str
    status: Literal["draft_not_sent"]


class PlaybookOwner(_Model):
    name: str
    address: Optional[str] = None
    link: str


class PlaybookTeams(_Model):
    """A drafted Teams note to the record's internal owner, when the ontology knows them."""
    channel: Literal["teams"]
    owner: Optional[PlaybookOwner] = None
    owner_known: bool
    why_no_owner: Optional[str] = None
    body: str
    status: Literal["draft_not_sent"]


class PlaybookChange(_Model):
    """The change a person makes in the source system themselves: the Workbench never
    writes BC, the Portal or MC (``writes`` is 0)."""
    system: str
    do_this: str
    field: str
    value: Optional[str] = None
    value_basis: str
    writes: Literal[0]
    by: str


class PlaybookLink(_Model):
    system: str
    url: Optional[str] = None
    status: Literal["ready", "not_configured"]
    reason: Optional[str] = None


class PlaybookFact(_Model):
    fact: str
    value: str
    citation: str


class AlertPlaybook(_Model):
    """One alert's ready-to-use next steps (``operating.alert_playbooks``), worded from its
    own cited sources as you read them. Nothing is sent or written by reading it."""
    id: str = Field(description="The card id the actions (``/api/actions/*``) take.")
    alert_id: str
    event_type: str
    kind: Optional[str] = None
    object_id: str
    object_type: str
    summary: Optional[str] = None
    email: Optional[PlaybookEmail] = None
    teams: Optional[PlaybookTeams] = None
    change: Optional[PlaybookChange] = None
    links: list[PlaybookLink] = Field(default_factory=list)
    local: list[ActionKind] = Field(default_factory=list)
    offers: list[CardActionOffer] = Field(default_factory=list)
    facts: list[PlaybookFact] = Field(default_factory=list)
    rests_on: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    sent: Literal[False]
    writes: Literal[0]


class RecordPackDocument(_Model):
    family: str = Field(description="What the document is, from its title words (invoice, "
                        "packing_slip, bol, pod, coa, order_confirmation, tracking, ...).")
    label: str
    title: Optional[str] = None
    kind: str = Field(description="attachment, sharepoint_file, original, email or record.")
    date: Optional[str] = None
    via: str = Field(description="The record it was found around (the pack's or a linked one).")
    url: Optional[str] = None
    citation: str


class RecordPackDraft(_Model):
    """The reply to the record's party listing the documents (never sent here: it goes only
    through ``/api/actions/preview`` and your confirm, ``card_id`` = ``card_id``)."""
    channel: Literal["email"]
    party: Optional[str] = None
    to: Optional[PlaybookAddress] = None
    subject: str
    body: str
    status: Literal["draft_not_sent"]
    attach: list[str] = Field(default_factory=list, description="Citations of the documents "
                              "to attach when you send it.")


class MeetingFollowUp(_Model):
    """A meeting's follow-up page (``operating.meeting_followup.page``): summary, cited
    decisions, action items with their prepared work, open questions, risks and status.
    Nothing in it is sent or written."""
    follow_up: dict[str, Any]


class MyActionItems(_Model):
    """One person's meeting action items (only that person and each meeting's organiser)."""
    person: Optional[str] = None
    items: list[dict[str, Any]]


class RecordPack(_Model):
    """The documents a customer or supplier usually asks for about one record, found around
    it and its linked records as you may read them, with a drafted reply."""
    object_id: str
    object_type: str
    label: str
    documents: list[RecordPackDocument] = Field(default_factory=list)
    expected: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    missing_labels: list[str] = Field(default_factory=list)
    records_read: int
    links: list[PlaybookLink] = Field(default_factory=list)
    card_id: Optional[str] = Field(None, description="The card the actions take; null when the "
                                   "record's id is too long for a card.")
    draft: RecordPackDraft
    draft_missing: list[str] = Field(default_factory=list)
    offers: list[CardActionOffer] = Field(default_factory=list)
    gaps: list[dict[str, str]] = Field(default_factory=list)
    basis: str
    sent: Literal[False]
    writes: Literal[0]


class ApprovalNudge(_Model):
    channel: Literal["email", "teams"]
    to: list[dict[str, str]] = Field(default_factory=list)
    subject: str
    body: str
    status: Literal["draft_not_sent"]
    sent: Literal[False]


class ApprovalItem(_Model):
    id: str
    kind: str
    channel: str
    summary: Optional[str] = None
    family: Optional[str] = None
    asked_by: Optional[str] = None
    waiting_on: list[str] = Field(default_factory=list)
    days: int
    aging: str
    object_id: Optional[str] = None
    citations: list[str] = Field(default_factory=list)
    nudge: ApprovalNudge
    card_id: Optional[str] = Field(None, description="The nudge's card for ``/api/actions/*``.")
    offers: list[CardActionOffer] = Field(default_factory=list)


class ApprovalGroup(_Model):
    waiting_on: str
    count: int
    oldest_days: int
    items: list[ApprovalItem]


class ApprovalsQueue(_Model):
    """Every pending approval on your own alert list (mail asks no one answered, Mission
    Control gates), grouped by who it waits on, oldest first, each with a nudge drafted.
    Nothing is sent or written."""
    as_of: str
    total: int
    by_aging: dict[str, int] = Field(default_factory=dict)
    groups: list[ApprovalGroup] = Field(default_factory=list)
    sent: Literal[0]
    writes: Literal[0]


class ReplyLine(_Model):
    id: str
    kind: str
    summary: Optional[str] = None
    channel: str
    sent: Optional[str] = None
    subject: Optional[str] = None
    days: int
    records_named: dict[str, int] = Field(default_factory=dict)
    customer_tier: Optional[str] = None
    chases: int = 0
    rank: list[int] = Field(default_factory=list)
    new_inquiry: bool = Field(False, description="A first question from a domain we never "
                              "mailed (a prospect): low priority, listed last.")
    object_id: Optional[str] = None
    citations: list[str] = Field(default_factory=list)
    draft: ApprovalNudge
    card_id: Optional[str] = Field(None, description="The reply's card for ``/api/actions/*``.")
    offers: list[CardActionOffer] = Field(default_factory=list)


class ReplyTriage(_Model):
    """What needs your reply today: your own mail and one-to-one chats where someone asked
    you something you have not answered, heaviest first (records named, the customer's
    tier, repeat chases, days waited), each with a holding reply drafted. Only your own
    mailbox; nothing is sent or written."""
    as_of: str
    total: int
    lines: list[ReplyLine] = Field(default_factory=list)
    sent: Literal[0]
    writes: Literal[0]


class WorkloadTeam(_Model):
    team: str
    awaiting_reply: Optional[int] = Field(None, description="Null when fewer than three "
                                          "people make up the team (it would name one).")
    tasks: int
    alerts: int
    suppressed: bool = False


class WorkloadPerson(_Model):
    person: str
    team: str
    self: bool
    awaiting_reply: int
    tasks: int
    alerts: int


class Workload(_Model):
    """Who is overloaded: open threads awaiting reply, open tasks and other alerts, counted
    per team; per person only your own counts and those of the teams the owner granted you.
    Counts only, never message content."""
    teams: list[WorkloadTeam] = Field(default_factory=list)
    people: list[WorkloadPerson] = Field(default_factory=list)
    granted_teams: list[str] = Field(default_factory=list)


class WorkloadGrants(_Model):
    """The owner's assignment: each person's team, and the teams each manager sees per
    person (Entra object ids, team codes in capitals)."""
    teams: dict[str, str] = Field(default_factory=dict)
    managers: dict[str, list[str]] = Field(default_factory=dict)


class RdLine(_Model):
    id: str
    state: Literal["waiting_on_us", "waiting_on_customer", "update_received", "closed"]
    last_on: Optional[str] = None
    messages_n: int = 0
    subject: Optional[str] = None
    citations: list[str] = Field(default_factory=list)


class RdProject(_Model):
    id: str
    customer: Optional[str] = Field(None, description="The customer's record handle (open it "
                                    "to read the name); null when the threads' mail is linked "
                                    "to no single customer.")
    party: Optional[str] = Field(None, description="Then the one outside domain of the "
                                 "threads (a customer or a supplier: not known which).")
    kind: Literal["stability_test", "formulation_change", "sample_request", "coa"]
    status: Literal["waiting_on_us", "waiting_on_customer", "update_received", "closed"]
    last_on: Optional[str] = None
    days: int
    idle: bool
    owner: Optional[str] = Field(None, description="The person of ours who wrote most (an "
                                 "Entra object id), never a name or an address.")
    owner_is_you: bool = False
    waiting_on: Optional[Literal["us", "customer"]] = None
    items: list[str] = Field(default_factory=list)
    lines: list[RdLine] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    draft: Optional[ApprovalNudge] = Field(None, description="A drafted status update to the "
                                           "customer, worded from kind, state and dates only.")
    card_id: Optional[str] = Field(None, description="The update's card for ``/api/actions/*``.")
    offers: list[CardActionOffer] = Field(default_factory=list)


class RdBoard(_Model):
    """R&D and sample projects per customer (sample requests, formulation changes, stability
    tests, certificates of analysis), built only from the threads and records you may open.
    Each project: status, last activity, owner and who it waits on, with a drafted status
    update (sent only through the card's preview and your confirm)."""
    as_of: str
    idle_after_days: int
    total: int
    idle: int
    projects: list[RdProject] = Field(default_factory=list)
    sent: Literal[0]
    writes: Literal[0]


class OwnerWorkloadPerson(_Model):
    person: str = Field(description="An Entra object id, never a name or an address.")
    team: str
    awaiting_reply: int
    tasks: int
    alerts: int
    alerts_closed: int = Field(description="Alerts owned by the person that closed in the "
                               "window (a count: nothing says who closed them).")


class OwnerWorkload(_Model):
    """Per person, for the owner only: open threads awaiting reply, open tasks, alerts and
    closed alerts. Counts only, never message content; people the exclusion does not clear
    never appear. Covers mail, Teams and tasks only: floor and lab work is not seen, so
    these figures undercount it."""
    window_days: int
    people: list[OwnerWorkloadPerson] = Field(default_factory=list)
    note: str


class LabelChange(_Model):
    id: str
    kind: str
    state: Optional[str] = None
    requested_on: Optional[str] = None
    approved_on: Optional[str] = None
    done_on: Optional[str] = None
    done_by: Optional[str] = None
    channel: Optional[str] = None
    days: Optional[int] = None
    stale: bool
    no_approval: bool
    summary: Optional[str] = None
    citations: list[str] = Field(default_factory=list)


class LabelChanges(_Model):
    """An item's label and artwork changes on your alert list: requested, approved, done,
    stale, changed with no approval."""
    object_id: str
    total: int
    by_state: dict[str, int] = Field(default_factory=dict)
    stale: int
    no_approval: int
    changes: list[LabelChange] = Field(default_factory=list)


class ActionStates(_Model):
    cards: dict[str, ActionResult] = Field(default_factory=dict, description="Each card's "
                                           "latest action by you.")


class ActionLogRow(ActionResult):
    card_label: str
    actor: str = Field(description="Who confirmed it (their name).")
    actor_email: Optional[str] = Field(None, description="Their own mailbox, the sender.")
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: Optional[str] = None
    payload_hash: str
    citations: list[ActionCitation] = Field(default_factory=list)
    http_status: Optional[int] = None


class ActionsLog(_Model):
    """Your confirmed actions, newest first, from the append-only actions journal."""
    rows: list[ActionLogRow] = Field(default_factory=list)


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
    actions: list[CardActionOffer] = Field(default_factory=list,
                                           json_schema_extra=_optional)


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
    stale_since: Optional[str] = Field(
        None, json_schema_extra=_optional,
        description="Set when the server could not read this page in time and serves the "
                    "last page it read for this same reader: when that page was read "
                    "(ISO). It is refreshed in the background.")


# ---- briefs (operating/briefs.py): meeting prep and the weekly review ---------------------

class BriefRow(_Model):
    label: str
    detail: Optional[str] = None
    who: Optional[str] = Field(None, description="Who sent a mail or Teams thread, as you may "
                                                 "read their person record; only once the "
                                                 "people exclusion is loaded.")
    date: Optional[str] = Field(None, description="ISO date the row is about, when cited.")
    amount: Optional[Money] = Field(None, description="Finance readers only; per currency.")
    object: Optional[ObjectRef] = None
    sources: list[Source] = Field(default_factory=list)


class BriefFigure(_Model):
    label: str
    value: Optional[str] = Field(None, description="Decimal string; null when not readable.")
    unit: Literal["records", "money", "percent"]
    currency: Optional[str] = None
    previous: Optional[str] = Field(None, description="The same figure the week before.")
    delta: Optional[str] = Field(None, description="value - previous (signed decimal).")
    note: Optional[str] = None
    sources: list[Source] = Field(default_factory=list)


class BriefSection(_Model):
    key: str
    title: str
    state: Literal["shown", "empty", "partial", "finance_marked", "unread", "blocked"] = Field(
        description="finance_marked: shown only to a reader holding finance (the same "
                    "whatever the data holds); partial: a read limit was reached or a part "
                    "was not readable; unread: its source (the alert list, or your item "
                    "access) could not be read, which is not the same as none; blocked: the "
                    "people exclusion is not loaded, so no mail, Teams or meeting line is "
                    "shown.")
    note: Optional[str] = None
    rows: list[BriefRow] = Field(default_factory=list)
    more: int = Field(0, description="Rows you may read beyond those listed.")
    figures: list[BriefFigure] = Field(default_factory=list)


class BriefGap(_Model):
    code: str
    state: str
    subject: str
    detail: str


class Brief(_Model):
    """A meeting prep brief or a weekly review, generated from the records you may read
    (no model call). Every row and figure cites its sources; nothing hidden is counted."""
    kind: Literal["meeting", "weekly"]
    title: str
    subject: Optional[ObjectRef] = None
    subject_sources: list[Source] = Field(default_factory=list)
    when: Optional[str] = None
    as_of: str
    parties: list[ObjectRef] = Field(default_factory=list)
    possible_parties: list[ObjectRef] = Field(
        default_factory=list, description="Lead-tier matches (a domain only): never read.")
    sections: list[BriefSection]
    gaps: list[BriefGap] = Field(default_factory=list)
    basis: str
    summary_question: Optional[str] = Field(
        None, description="A question for /api/ask (the answer path with its claim checks) "
                          "that summarises this brief; optional.")
    stored_at: Optional[str] = Field(
        None, description="When a finished week's review was generated and kept; served "
                          "again only while your grant is the same and every source it shows "
                          "still opens for you.")


class UpcomingMeeting(_Model):
    object: ObjectRef
    start: Optional[str] = None
    brief_url: str
    sources: list[Source] = Field(default_factory=list)


class UpcomingMeetings(_Model):
    meetings: list[UpcomingMeeting]


# ---- approve-to-write (operating.write_intents) -------------------------------------------

WriteOperation = Literal["purchase_order_create", "sales_order_create",
                         "purchase_order_line_update", "vendor_note", "customer_note"]
WriteStatus = Literal["prepared", "approved", "would_write", "written", "failed", "pending",
                      "unknown"]


class WritePrepare(_Model):
    """An exact entry for a business system, as an autopilot or a person prepared it."""
    system: Literal["bc", "portal"]
    operation: WriteOperation
    environment: Optional[str] = Field(None, max_length=50,
                                       description="BC environment; absent: Production.")
    company_id: str = Field(min_length=36, max_length=36)
    record_id: Optional[str] = Field(None, max_length=36, description="The record a change "
                                     "or note is for; absent for a new order.")
    payload: dict[str, Any]
    precondition: dict[str, str] = Field(description="etag: the record's @odata.etag as "
                                         "read; party_id: the vendor or customer read.")
    reason: str = Field(min_length=1, max_length=1000)
    citations: list[str] = Field(min_length=1, max_length=20)


class WriteHistoryRow(_Model):
    status: WriteStatus
    at: str
    actor: str


class WriteIntentView(_Model):
    intent: dict[str, Any]
    status: WriteStatus
    text: str
    at: str
    approved_by: Optional[dict[str, Any]] = None
    request: Optional[dict[str, Any]] = Field(None, description="The exact BC API v2.0 "
                                              "request recorded (would write).")
    missing: list[str] = Field(default_factory=list, description="What a live write lacks.")
    history: list[WriteHistoryRow] = Field(default_factory=list)
    replayed: bool = False


class WriteIntentList(_Model):
    items: list[WriteIntentView]


class WriteChange(_Model):
    field: str
    current: Any = None
    proposed: Any = None
    changed: bool


class WriteLine(_Model):
    n: int
    fields: dict[str, Any]
    amount: Optional[str] = None


class WriteWarning(_Model):
    code: str
    text: str


class WritePreview(_Model):
    """Field by field against the record as read now; nothing is done."""
    intent_id: str
    intent: dict[str, Any]
    label: str
    current_etag: Optional[str] = None
    changes: list[WriteChange]
    lines: list[WriteLine]
    warnings: list[WriteWarning]
    problems: list[str]
    request: dict[str, Any]
    undo: str
    live_missing: list[str]
    preview_hash: str


class WriteApprove(_Model):
    confirm: bool = Field(description="True only from the person's second (Confirm) click.")
    preview_hash: str = Field(max_length=64)
    acknowledge_warnings: bool = False


class WriteExecute(_Model):
    idempotency_key: str = Field(min_length=16, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    checked_bc: bool = Field(False, description="The person checked BC after an unknown "
                                                "outcome and the entry is not there.")


class LabourSource(_Model):
    handle: str = Field(description="The captured time-sheet attachment or message it is "
                        "read from (no file name: a time sheet's name may be a person's).")
    sent: str = Field(description="The message's sent date.")


class LabourWeek(_Model):
    department: str
    week: str = Field(description="The Monday the week starts.")
    run: Optional[str] = Field(None, description="The production run or order the sheet "
                               "names; null: the department's whole week.")
    hours: str
    overtime_hours: Optional[str] = Field(None, description="Null when a sheet of the week "
                                          "has no overtime column.")
    overtime_basis: Optional[Literal["stated", "over_44_hours", "mixed", "fewer_than_3"]] = \
        Field(None, description="stated: the sheets' overtime columns; over_44_hours: a "
              "sheet with none, each person's hours past 44 in the week; mixed: both; "
              "fewer_than_3: fewer than 3 people worked overtime, so none is shown.")
    headcount_days: Optional[int] = Field(None, description="Person-days with hours; null "
                                          "when a sheet has no day columns.")
    week_basis: Literal["stated", "sent"] = Field(description="stated: the sheet's own week "
                                                 "or dates; sent: the message's sent date.")
    markings: list[str]
    sources: list[LabourSource]


class LabourAlert(_Model):
    kind: Literal["labour_overtime_spike"]
    department: str
    week: str
    overtime_hours: str
    baseline_median: str
    baseline_weeks: int
    rise_percent: str
    markings: list[str]
    sources: list[LabourSource]


class LabourPoint(_Model):
    week: str
    hours: str
    overtime_hours: Optional[str] = None


class LabourTrend(_Model):
    department: str
    points: list[LabourPoint]


class LabourPage(_Model):
    state: Literal["ready", "not_granted", "not_built"]
    note: str
    built_at: Optional[str] = None
    weeks: list[LabourWeek]
    trend: list[LabourTrend]
    alerts: list[LabourAlert]
