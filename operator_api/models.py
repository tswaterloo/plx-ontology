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

from pydantic import BaseModel, ConfigDict, Field, model_validator

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

def _stale_since() -> Any:
    return Field(None, json_schema_extra=_optional, description=(
        "Set when the server serves the last page it read for this same reader instead of "
        "reading it now: when that page was read (ISO). It is read again in the background."))


def _reading() -> Any:
    return Field(False, json_schema_extra=_optional, description=(
        "True when this screen's first read for this reader is still going on: nothing of "
        "it is shown yet (its lists are empty and its counts zero, never a finding); ask "
        "again shortly for the page."))


def _read_at() -> Any:
    return Field(None, json_schema_extra=_optional, description=(
        "Set when this page is the one read for this same reader a moment ago, served as "
        "kept: when it was read (ISO). Other people's decisions and actions it shows are "
        "as of then."))



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
    can_view_as: bool = Field(False, json_schema_extra=_optional,
                              description="The owner-open reader on a server that can view "
                                          "as someone (/api/view-as/people).")
    owner: bool = Field(False, json_schema_extra=_optional,
                        description="The owner-open reader: may read everyone's work log "
                                    "and time saved.")
    ops_reader: bool = Field(False, json_schema_extra=_optional,
                             description="The owner or a named Team reader "
                                         "(PLX_TEAM_VIEW_READERS): sees the reliability "
                                         "alarms and /ops.")


class ViewAsPerson(_Model):
    key: str = Field(description="An opaque handle for X-PLX-View-As; never an id.")
    name: str


class ViewAsMark(_Model):
    key: str = Field(max_length=40, description="The person's opaque view-as key.")


class ViewAsMarked(_Model):
    recorded: bool
    event: Literal["start", "stop"]


class ViewAsPeople(_Model):
    """Whom the owner may view as. ``available`` False: this server cannot (no employee
    resolver or people exclusion), so nobody is listed."""
    available: bool
    people: list[ViewAsPerson] = Field(default_factory=list)


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
    """The evidence trail: the steps, records and messages that led to the answer, each opening
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


AskDraftKind = Literal["email", "payment", "record_change", "approval"]


class DraftDetail(_Model):
    label: str
    value: str


class RecipientQuestion(_Model):
    """A recipient the reader's own mail did not settle, put to them as one tap: ``pick``
    one of the candidates (who the line means) or ``any`` (who else goes on it); every
    candidate cleared by the people exclusion, best first."""
    field: Literal["to", "cc"]
    pick: Literal["one", "any"]
    label: str
    candidates: list[str] = Field(default_factory=list)


class RecordQuestion(_Model):
    """A record the line names that the draft could not state, put to the reader: its
    note, and the sentence that asks the recipient instead (one tap adds it to the
    message)."""
    record: str
    outcome: Literal["not_found", "unreadable", "not_looked_up"]
    note: str
    ask_line: str


class AskDraft(_Model):
    """A draft the Ask wrote because the question asked for one: an email the person edits
    and sends from its preview with one explicit confirm (``/api/actions/preview`` and
    ``/api/actions/confirm``, ``card_id`` = ``id``, the approve-to-send desk), or an
    action (a payment, a record change, an approval) shown in full, which the person
    files as a follow-up or assigns to a colleague for approval through the same desk.
    Nothing is sent, paid or written by the Ask itself."""
    id: str = Field(description="The card id the approve-to-send desk takes.")
    kind: AskDraftKind
    title: str
    email_subject: Optional[str] = None
    body: str
    details: list[DraftDetail] = Field(default_factory=list, description="An action's "
                                       "details, each shown before any confirm.")
    actions: list["CardActionOffer"] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list, description="What the draft lacks "
                               "before it can be done, in words.")
    in_reply_to: Optional[str] = Field(None, json_schema_extra=_optional, description=(
        "The message in your own mailbox a reply goes to (its citation)."))
    to: Optional[str] = Field(None, json_schema_extra=_optional,
                              description="The address a reply is drafted to.")
    also_to: list[str] = Field(default_factory=list, description="Others the reader's own "
                               "mail settles on To (a new message).")
    cc: list[str] = Field(default_factory=list, description="Who the reader's own mail "
                          "settles on Cc (a new message).")
    recipient_questions: list[RecipientQuestion] = Field(default_factory=list)
    record_questions: list[RecordQuestion] = Field(default_factory=list)
    sent: Literal[False] = False


class ClarifyOption(_Model):
    """One quick reply: its words, and the record it picks (a record the reader may
    read, offered from their own records) or none (a reading such as a period)."""
    label: str
    detail: Optional[str] = Field(None, description="The record's own values that tell "
                                  "it apart (date, amount, status).")
    object: Optional[ObjectRef] = None


class Clarify(_Model):
    """The one question asked back instead of guessing: answer it with an option
    (``AskRequest.clarified.option``) or in your own words (``clarified.text``); the
    answer continues the same conversation."""
    question: str
    options: list[ClarifyOption] = Field(default_factory=list, max_length=4)
    free_text: bool = True


class Assumption(_Model):
    """What the answer took the question to mean, in one line, and the other readings:
    picking one asks it again under that reading (``AskRequest.clarified.option``)."""
    text: str
    options: list[ClarifyOption] = Field(default_factory=list, max_length=3)


class ClarifyReply(_Model):
    """The person's answer to an earlier answer's ``clarify`` question or ``assumption``:
    the answer it replies to, and an option's index or their own words."""
    answer_id: str = Field(min_length=1, max_length=80)
    option: Optional[int] = Field(None, ge=0, le=3)
    text: Optional[str] = Field(None, min_length=1, max_length=400)

    @model_validator(mode="after")
    def _one(self) -> "ClarifyReply":
        if (self.option is None) == (self.text is None):
            raise ValueError("Pick one option or write an answer.")
        return self


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
    trail: Optional[EvidenceTrail] = Field(None, description="The evidence trail (optional: "
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
    attachments: list[AskAttachmentNote] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="The question's attachments: what was read and what was not, and why.")
    drafts: list[AskDraft] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="Emails and actions the question asked to have drafted; each is done "
                    "only from its preview with your explicit confirm.")
    instruction: Optional["InstructPart"] = Field(
        None, json_schema_extra=_optional,
        description="When the box was told to do something: its question, exact writes, "
                    "documents or proposed workflow (the drafts are in ``drafts``).")
    clarify: Optional[Clarify] = Field(
        None, json_schema_extra=_optional,
        description="One question asked back because the answer turns on a choice the "
                    "question leaves open; nothing else is answered until it is picked.")
    assumption: Optional[Assumption] = Field(
        None, json_schema_extra=_optional,
        description="What the answer took an open question to mean, with the other "
                    "readings to change it.")


# ---- ask (SSE) -------------------------------------------------------------------------

class HistoryTurn(_Model):
    role: Literal["user", "assistant"]
    text: str = Field(max_length=4000)


class AskAttachment(_Model):
    """A file or image attached to one question (``operating.ask_attachments``): read in
    memory for that answer only, never stored."""
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(min_length=1, max_length=11_200_000,
                      description="The file's bytes, base64 (8 MB a file, 10 MB together).")


class AskAttachmentNote(_Model):
    """What the answer and the saved Ask say of an attachment: never its content."""
    name: str
    kind: Optional[Literal["pdf", "spreadsheet", "text", "image"]] = None
    size: int
    digest: str
    read: bool
    note: Optional[str] = Field(None, description="Why it was not read, or read in part.")


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
    attachments: list[AskAttachment] = Field(
        default_factory=list, max_length=5,
        description="Files and images the person attached (PDF, .xlsx, .csv, text, PNG, "
                    "JPEG, GIF, WebP): up to five, 10 MB together.")
    context: Optional["InstructContext"] = Field(
        None, json_schema_extra=_optional,
        description="The page the box was opened on (a meeting, an autopilot item, a page); "
                    "an email or file goes in ``source``, a record in ``object_id``.")
    mode: Optional[Literal["do", "ask"]] = Field(
        None, json_schema_extra=_optional,
        description="The person's pick when the box asked whether to do it or answer it.")
    instruction: Optional["InstructFollowUp"] = Field(
        None, json_schema_extra=_optional,
        description="An answer to the question an instruction asked, or the meetings to "
                    "use: continues that instruction.")
    clarified: Optional[ClarifyReply] = Field(
        None, json_schema_extra=_optional,
        description="An answer to the question an earlier answer asked back (or another "
                    "reading of its assumption): continues that question.")

    @model_validator(mode="after")
    def _attachments_fit(self) -> "AskRequest":
        if sum(len(a.data) for a in self.attachments) > ATTACHMENTS_BASE64_MAX:
            raise ValueError("Attachments together can be up to 10 MB.")
        if self.clarified is not None and (self.instruction is not None
                                           or self.mode is not None):
            raise ValueError("An answer to a question asked back continues only that "
                             "question.")
        return self


# the base64 length of 10 MB (``operating.ask_attachments.MAX_TOTAL``), and the largest
# /api/ask body the app reads at all (``operator_api.app``): that and the question,
# history and names with room for JSON escapes
ATTACHMENTS_BASE64_MAX = -(-10 * 1024 * 1024 // 3) * 4
ASK_BODY_MAX = ATTACHMENTS_BASE64_MAX + 600_000


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
    same_as: list[str] = Field(default_factory=list, description="The other records this "
                               "reader's projection reads as this same record in another "
                               "system (a shared code or a verified same_record_as link). "
                               "A picker merges rows only on this or an identical id.")


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
    reading: bool = _reading()


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
    reading: bool = _reading()


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
    type_label: Optional[str] = Field(None, json_schema_extra=_optional, description=(
        "That type's business name where the schema declares one (a raw Portal, FileMaker "
        "or Sage table, e.g. 'AR invoice entry'); absent for curated types."))
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
    type_label: Optional[str] = Field(None, json_schema_extra=_optional, description=(
        "The type's business name where the schema declares one (a raw Portal, FileMaker "
        "or Sage table, e.g. 'AR invoice entry'); absent for curated types."))
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
    profitability_choices: list[ObjectRef] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="The margin section's same-named customers that no identity link "
                    "joins: each a choice, never added together.")
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
    reading: bool = _reading()


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
    profitability_choices: list[ObjectRef] = Field(
        default_factory=list, json_schema_extra=_optional,
        description="margin: the Business Central customers of this page's exact name that "
                    "no identity link makes one record: each its own choice (its own page "
                    "shows its margin), never added together; profitability is null then.")
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


class SourceRecord(_Model):
    """One record citing a source: one projected from it (``from``) or linked to such a
    record (``linked``, by ``link``: the link's kind in words)."""
    record: ObjectRef
    how: Literal["from", "linked"]
    link: Optional[str] = None


class SourceRecords(_Model):
    """The records citing one source, as this reader may read them now (``/api/source/
    records``). A source the reader may not open reads as not found; a record the reader
    may not read is left out. ``served`` false: this server holds no records to look in."""
    handle: str
    records: list[SourceRecord] = Field(default_factory=list)
    truncated: bool = False
    served: bool = True
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
                    "return_unanswered",
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
                    "approval_waiting", "chase_escalating", "party_chase_unanswered",
                    "internal_order_chase",
                    "slow_moving_stock", "expiring_lot_stock",
                    "assembly_blocked_by_component", "certificate_expiring",
                    "freight_cost_anomaly", "stale_quote", "priority_order_unfilled",
                    "duplicate_party_candidate",
                    "key_field_missing", "twins_disagree", "source_stale",
                    "sync_missing", "sync_mismatch", "reorder_soon",
                    "inventory_adjustment", "blocked_account_activity", "gl_posting_errors",
                    "recurring_task_missed", "label_change", "label_revision_superseded",
                    "rd_project",
                    "unapplied_remittance", "short_payment", "statement_difference"]


class CopyFinding(_Model):
    rule: str
    start: int
    end: int
    detail: str
    part: Optional[str] = None


class CopyCheck(_Model):
    """The slop gate's verdict on a drafted text (``operating.copy_gate``)."""
    ok: bool
    findings: list[CopyFinding] = Field(default_factory=list)


class AlertPrecision(_Model):
    value: float = Field(ge=0, le=1)
    checked: int = Field(ge=0, description="Live items hand-checked.")
    correct: Optional[int] = Field(None, ge=0, description="How many of them were right.")
    on: str = Field(description="Day of the check.")
    source: str
    commit: Optional[str] = None
    note: Optional[str] = None


class AlertKind(_Model):
    kind: str
    label: str
    surface: Literal["alert", "data_health", "weekly"]


class AlertType(_Model):
    type: str
    kind: str
    label: str
    precision: Optional[AlertPrecision] = None
    actionable: bool
    default: Literal["on", "off"]
    home: bool
    roles: list[str] = Field(default_factory=list)


class AlertKinds(_Model):
    """The alert registry (operating/alert_kinds.json)."""
    rule: str
    kinds: list[AlertKind]
    types: list[AlertType]


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
    alert_kind: Optional[str] = Field(None, description="The registry kind it merges into "
                                      "(operating/alert_kinds.json).")
    default_on: bool = Field(False, json_schema_extra=_optional,
                             description="On by default (measured >= 90% on 15+).")
    home: bool = Field(False, json_schema_extra=_optional, description="Shown on My work.")
    copy_check: Optional[CopyCheck] = None
    first_seen: Optional[str] = None
    open_url: str
    sources: list[Source] = Field(default_factory=list)
    trail: Optional[EvidenceTrail] = Field(None, description="The evidence trail, read as this "
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


class BriefingDataNote(_Model):
    """One check's records it could not judge for one reason, as a count."""
    check: str
    reason: str
    count: int = Field(ge=0)
    words: str


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
    home_count: int = Field(0, ge=0, json_schema_extra=_optional,
                            description="Items whose kind reaches My work.")
    watching_count: int = Field(0, ge=0, json_schema_extra=_optional,
                                description="Items left to Watching.")
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
    data_note_items: list[BriefingDataNote] = Field(default_factory=list, description=(
        "The same notes with their check and reason, so a reader can list the records "
        "(GET /api/briefing/unjudged)."))
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
    reading: bool = _reading()


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


class UnjudgedRecord(_Model):
    id: str
    type: str = Field(description="The record's object type (its id's prefix).")
    label: str


class BriefingUnjudged(_Model):
    """The records one check could not judge that you may read (the count on your
    briefing), a page at a time; a record you may not read is never listed or counted."""
    check: str
    reason: Optional[str] = None
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0, description="The offset of the next page (a record your "
                                               "whole read does not show is left out of "
                                               "this one, so it can hold fewer).")
    complete: bool = Field(description="False when the check also skipped records it could "
                                       "not name, so the list is not all it skipped.")
    records: list[UnjudgedRecord] = Field(default_factory=list)
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
    copy_check: Optional[CopyCheck] = None
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
    copy_check: Optional[CopyCheck] = None


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
    copy_check: Optional[CopyCheck] = None
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
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()


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
    copy_check: Optional[CopyCheck] = None
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
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()
    reading: bool = _reading()


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
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()
    reading: bool = _reading()


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
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()


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
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()
    reading: bool = _reading()


class TeamPerson(_Model):
    """One person's row of the Team view. ``key`` is an opaque handle for the drill-down
    (never an object id, a name or an address)."""
    key: str
    name: str = Field(description="Their Teams display name, else a calendar's name for them, "
                      "else their address's local part; never an id.")
    team: Optional[str] = None
    roles: list[str] = Field(default_factory=list)
    self: bool = False
    d7: dict[str, int] = Field(default_factory=dict, description="Counts per channel, last 7 "
                               "days (mail_sent, mail_received, teams, meetings, mc_approvals, "
                               "mc_questions, mc_tasks, planner, sharepoint, bc, portal, sage).")
    d28: dict[str, int] = Field(default_factory=dict, description="The same, last 28 days.")
    awaiting_reply: int = 0
    tasks: int = 0
    alerts: int = 0
    alerts_closed: int = Field(0, description="Their alerts closed in the last 28 days.")
    reply_minutes_7: Optional[int] = Field(None, description="Median minutes to reply to a "
                                           "message addressed to them, last 7 days.")
    reply_minutes_28: Optional[int] = None
    replies_28: int = 0
    trend: list[int] = Field(default_factory=list, description="Everything they did per day, "
                             "the last 28 days, oldest first.")
    hours: list[int] = Field(default_factory=list, description="Messages sent per hour of the "
                             "day (US Eastern), last 28 days.")


class SystemCoverage(_Model):
    system: str
    share: float = Field(description="Share of the system's actions by people that are tied "
                         "to a person (0..1); the rest name no user we can match.")
    actions: int


class TeamView(_Model):
    """The Team view: per person, activity in every captured system over 7 and 28 days,
    open work, reply time, a daily trend and busiest hours. The owner and the named Team
    readers see everyone; every other reader only their own row. The owner and the people
    the exclusion does not clear are in no row and no count, for every reader."""
    scope: Literal["everyone", "self"]
    as_of: Optional[str] = Field(None, description="When the activity was counted (ISO).")
    window_to: Optional[str] = None
    state: Literal["ready", "not_built", "blocked"]
    people: list[TeamPerson] = Field(default_factory=list)
    coverage: list[SystemCoverage] = Field(default_factory=list)
    note: str
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()


class TeamItem(_Model):
    id: str
    bucket: Literal["awaiting_reply", "tasks", "alerts"]
    kind: str
    title: str
    counterpart: Optional[str] = None
    age_days: Optional[int] = None
    channel: Optional[str] = None
    waiting_on: str = Field(description="The person's name for a thread awaiting their reply; "
                            "else the departments that answer it.")
    snippet: Optional[str] = None
    record_id: Optional[str] = None
    record_type: Optional[str] = None


class TeamDay(_Model):
    day: str
    counts: dict[str, int] = Field(default_factory=dict)


class TeamPartner(_Model):
    key: Optional[str] = Field(None, description="Their drill-down handle, when you may open "
                               "their row.")
    name: str
    n: int


class TeamPersonDetail(_Model):
    """One person's drill-down: their row, every day of the last 28 across systems, who they
    work with most, and their open items (what you may open through the normal path; marked
    items stay counted in ``withheld``, never shown)."""
    person: TeamPerson
    timeline: list[TeamDay] = Field(default_factory=list)
    partners: list[TeamPartner] = Field(default_factory=list)
    items: list[TeamItem] = Field(default_factory=list)
    withheld: int = 0
    truncated: bool = False


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


class LabelRevision(_Model):
    id: str
    revision: Optional[str] = None
    rank: int = 0
    name: Optional[str] = None
    first_seen: Optional[str] = None
    channels: list[str] = Field(default_factory=list)
    approved_on: Optional[str] = None
    approved_by: Optional[str] = Field(None, description="The approving message's sender.")
    current: bool
    superseded_on: Optional[str] = None
    citations: list[str] = Field(default_factory=list)


class LabelRevisionFamily(_Model):
    family: str
    current: Optional[str] = None
    current_approved: bool
    revisions: list[LabelRevision] = Field(default_factory=list)


class LabelRevisionOnOrder(_Model):
    id: str
    name: Optional[str] = None
    revision: Optional[str] = None
    current: Optional[str] = None
    superseded_on: Optional[str] = None
    summary: Optional[str] = None
    citations: list[str] = Field(default_factory=list)


class LabelRevisions(_Model):
    """A record's label and artwork revisions on your alert list, newest first per file
    (current: the highest you can see), who approved each and when; on an order, the
    superseded revisions it points at."""
    object_id: str
    revisions: int
    unapproved_current: int
    families: list[LabelRevisionFamily] = Field(default_factory=list)
    superseded_on_orders: list[LabelRevisionOnOrder] = Field(default_factory=list)


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
    reading: bool = _reading()


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
    state: Literal["shown", "empty", "partial", "finance_marked", "quality_marked", "unread",
                   "blocked", "not_available"] = Field(
        description="finance_marked / quality_marked: shown only to a reader holding finance "
                    "/ quality_controlled (the same whatever the data holds); partial: a read "
                    "limit was reached or a part was not readable; unread: its source (the "
                    "alert list, or your item access) could not be read, so the section may still have "
                    "items; blocked: the people exclusion is not loaded, so no mail, "
                    "Teams, meeting or Planner line is shown; not_available: the data it needs "
                    "is not captured yet (the note says what).")
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
    """A meeting prep brief, a weekly review or a QC pack (daily, audit), generated from
    the records you may read (no model call). Every row and figure cites its sources;
    nothing hidden is counted."""
    kind: Literal["meeting", "weekly", "qc_daily", "qc_audit"]
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
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()


class MeetingInvitee(_Model):
    """One person on a calendar invitation, as the invitation lists them (directory facts
    every employee sees; the people exclusion decided before it is shown)."""
    name: str
    address: Optional[str] = None
    role: Literal["organizer", "required", "optional", "resource"]
    response: Optional[Literal["accepted", "declined", "tentativelyaccepted", "none",
                               "notresponded", "organizer"]] = None
    outside: bool = Field(False, description="Their address is outside the mailbox's domain.")
    sources: list[Source] = Field(default_factory=list)


class MeetingOpenItem(_Model):
    """An open action item from an earlier meeting of the same series (the meeting
    autopilot's tracked items the reader may see)."""
    label: str
    detail: Optional[str] = None
    date: Optional[str] = None
    sources: list[Source] = Field(default_factory=list)


class UpcomingMeeting(_Model):
    object: ObjectRef
    start: Optional[str] = None
    end: Optional[str] = None
    all_day: bool = False
    location: Optional[str] = None
    online: bool = False
    join_url: Optional[str] = Field(None, description="The Teams join link (teams.microsoft.com "
                                                      "only).")
    agenda: Optional[str] = Field(None, description="The invitation's body preview, clipped.")
    organizer: Optional[MeetingInvitee] = None
    attendees: list[MeetingInvitee] = Field(default_factory=list)
    is_organizer: bool = False
    private: bool = Field(False, description="A private or confidential event: its time only.")
    people_withheld: bool = Field(False, description="The people exclusion is not loaded: no "
                                                     "organizer, attendee or agenda shown.")
    parties: list[ObjectRef] = Field(default_factory=list,
                                     description="Customers and suppliers it is linked to.")
    open_items: list[MeetingOpenItem] = Field(default_factory=list)
    brief_url: str
    sources: list[Source] = Field(default_factory=list)


class UpcomingMeetings(_Model):
    meetings: list[UpcomingMeeting]
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()
    reading: bool = _reading()


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


class MyWorkItem(_Model):
    """One piece of work an autopilot prepared: what happened, the prepared work (answer,
    drafted reply, impacted records, recommendation, a dry-run entry), its evidence."""
    item_id: str
    autopilot: str
    label: str
    title: str
    mine: bool = Field(description="Yours (approve here); false: a report's item you see "
                       "through the owner's team grant (view only).")
    happened: dict[str, Any] = Field(default_factory=dict)
    prepared: dict[str, Any] = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)
    due_at: str
    created_at: str
    card_id: Optional[str] = Field(None, description="The drafted message's card for "
                                   "``/api/actions/*`` (preview, then your confirm).")
    offers: list[CardActionOffer] = Field(default_factory=list)
    write_ready: bool = Field(False, description="A prepared entry: POST "
                              "``/api/my-work/{item_id}/write`` makes it a dry-run write "
                              "intent for ``/api/writes*``.")
    order_import: bool = Field(False, description="A prepared order with its lines: GET "
                               "``/api/my-work/{item_id}/order-import`` gives it as an import "
                               "file (CSV) for the Portal or Business Central.")


class MyWorkNudge(_Model):
    item_id: str
    title: str
    due_at: str
    text: str


class MyWorkBriefEntry(_Model):
    item_id: str
    title: str


class MyWorkBrief(_Model):
    """The morning brief: decisions waiting, drafts ready, orders to enter, supplier
    follow-ups and ETA slips, as counts and the oldest few items of each."""
    at: str
    counts: dict[str, int] = Field(default_factory=dict)
    top: dict[str, list[MyWorkBriefEntry]] = Field(default_factory=dict)


class MyWorkDigest(_Model):
    open: int
    by_autopilot: dict[str, int] = Field(default_factory=dict)
    due_today: int
    brief: MyWorkBrief | None = None
    mc_approvals: Optional[dict[str, Any]] = Field(None, description=(
        "Your pending Mission Control approval gates per decider, grouped by class "
        "(riskier first), each with the history-based recommendation, the low-risk mark "
        "and its MC task link (``operating.mc_digest``). Read-only: decide in MC."))
    nudges: list[MyWorkNudge] = Field(default_factory=list)


class MyWork(_Model):
    """The work the autopilots prepared for you (and, per the owner's grants, for the people
    of the teams you manage), with your daily digest. Nothing is sent or written."""
    status: Literal["read", "off", "not_scheduled"]
    items: list[MyWorkItem] = Field(default_factory=list)
    digest: MyWorkDigest
    sent: Literal[0]
    writes: Literal[0]
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()


AutopilotState = Literal["off", "dry_run", "passed", "live_held", "live"]
AutopilotDecisionKind = Literal["accept", "edit", "reject", "snooze"]
AutopilotItemState = Literal["waiting", "snoozed", "edited", "accepted", "rejected"]


class AutopilotScore(_Model):
    judged: int
    matched: int
    percent: Optional[float] = Field(None, description="matched / judged, one decimal; "
                                     "absent when nothing was judged.")


class AutopilotGatePart(_Model):
    """One scored window the write gate counts for a workflow: fresh days (from the rules'
    ``fresh_from``) or a historical window never used for tuning (the rules'
    ``clean_windows``)."""
    window: list[str] = Field(default_factory=list)
    kind: Literal["fresh", "historical"]
    judged: int
    matched: int


class AutopilotCountdown(_Model):
    """The workflow's way to its write gate: the counts the gate admitted
    (``autopilot_write.gate_counts``), items judged on held-out days (fresh days, or past
    days never tuned on) against the
    ``need_judged`` the gate asks for, and the precision so far against ``need_percent``.
    Recomputed from the counts at every read."""
    judged: int
    matched: int
    need_judged: int
    need_percent: float
    percent: Optional[float] = Field(None, description="matched / judged, one decimal; "
                                     "absent when nothing was judged.")
    passes: bool
    window: list[str] = Field(default_factory=list)
    data_end: Optional[str] = None
    run: Optional[str] = Field(None, description="The scorer run these counts came from.")
    refused: Optional[str] = Field(None, description="Why the role's newest scorer run was "
                                   "not admitted (status:partial, not_held_out, counts...): the "
                                   "counts are then an earlier run's. Absent when it was.")
    refused_run: Optional[str] = None
    parts: list[AutopilotGatePart] = Field(default_factory=list, description="The scored "
                                           "windows summed into these counts, oldest first.")


class AutopilotWorkflowRow(_Model):
    """One workflow of one role (a ``person:`` role is one person's whole day): its replay
    scores, its write state and how many of its people switched it on."""
    role: str
    person: Optional[str] = Field(None, description="The person id of a person role.")
    workflow: str
    dev: Optional[AutopilotScore] = Field(None, description="Replay on tuning days.")
    fresh: Optional[AutopilotScore] = Field(None, description="Replay on held-out days "
                                            "(the write gate's counts).")
    n: int = Field(description="Items judged: fresh when scored, else dev.")
    state: AutopilotState = Field(description="off: switched off; dry_run: gate not "
                                  "passed; passed: passed, nobody it acts for switched it "
                                  "on; live_held: someone switched it on, held by the host's "
                                  "master switch; live: acts for whoever switched it on.")
    on: bool = Field(description="False when the owner switched this workflow off.")
    switched_on: int = Field(0, json_schema_extra=_optional, description="People of this row who switched it on for "
                             "themselves and have not paused (only yourself unless you are "
                             "the owner or an admin).")
    missing: list[str] = Field(default_factory=list)
    last_run: Optional[str] = None
    scored_at: Optional[str] = None
    open_items: int = 0
    draft: Optional[bool] = Field(None, json_schema_extra=_optional,
                                  description="Draft mode: prepared for this person before "
                        "the gate passed (operating/autopilot_draft.json). Each item waits "
                        "for their accept; sends and writes keep their own gates.")
    countdown: Optional[AutopilotCountdown] = Field(
        None, json_schema_extra=_optional,
        description="Judged fresh items toward the write gate; absent until the scorer "
                    "published a result for this role.")


class AutopilotActionResult(_Model):
    """One action of an accepted item, in order: what it was and how it ended."""
    kind: str = Field(description="The action kind: bc_write, send_mail, mc_task...")
    system: str = Field(description="The system it writes to: bc, mail, mc... or other.")
    status: str = Field(description="done (went through), unknown or pending (could not be "
                        "confirmed: check before retrying), failed, or why it never ran: "
                        "unavailable, refused, killed, rate_limited, dry_run...")
    reasons: list[str] = Field(default_factory=list)


class AutopilotDecision(_Model):
    decision: AutopilotDecisionKind
    at: str
    by_you: bool
    until: Optional[str] = None
    text: Optional[str] = Field(None, description="The edited draft.")
    note: Optional[str] = None
    outcome: Optional[str] = Field(None, description="The write path's outcome of an "
                                   "accept over all its actions: done (all), partly_done "
                                   "(some, then one was not), unknown (one could not be "
                                   "confirmed: check before retrying), failed, dry_run "
                                   "(recorded only), killed, refused...")
    reasons: list[str] = Field(default_factory=list)
    steps: list[AutopilotActionResult] = Field(
        default_factory=list,
        description="Each action of an accept in order (empty before steps were kept).")
    sent: bool = Field(False, description="The item's email went out: its own send "
                       "action's result is done (an entry before it may have succeeded "
                       "while the send did not).")


class AutopilotItem(_Model):
    """A prepared item with its evidence, its drafted action and your decision."""
    item_id: str
    autopilot: str
    label: str
    title: str
    workflow: str
    system: str = Field(description="mail, teams, mc, bc, sage, portal, filemaker, "
                        "sharepoint, meetings, planner or other.")
    roles: list[str] = Field(default_factory=list)
    owner: str = Field(description="The person id the item was prepared for.")
    mine: bool
    happened: dict[str, Any] = Field(default_factory=dict)
    prepared: dict[str, Any] = Field(default_factory=dict)
    citations: list[str] = Field(default_factory=list)
    due_at: str
    created_at: str
    today: bool = Field(description="Prepared or due today (Toronto day), or past due.")
    state: AutopilotItemState
    decision: Optional[AutopilotDecision] = None
    live: Optional[bool] = Field(None, description="An accept performs this item live now (the "
                       "owner opened its kind to approved writes, or its workflow passed "
                       "its gate and is switched live): it is accepted alone, after a "
                       "confirm, never in a bulk accept.")
    live_kinds: list[str] = Field(default_factory=list, description="The write kinds an "
                                  "accept would perform live.")
    confirm_token: Optional[str] = Field(None, description="Set only in the answer to an "
                                         "accept of a live item without a confirm: send it "
                                         "back with the same accept (within 10 minutes) to "
                                         "perform the item.")


class AutopilotPerson(_Model):
    id: str
    name: str = Field(description="From the person's replay role; empty when no name is on "
                      "record (the role roster holds ids only).")
    you: bool = False
    roles: list[str] = Field(default_factory=list)


class AutopilotSwitches(_Model):
    global_off: bool = Field(description="The write path's global kill switch.")
    master_switch: bool = Field(description="The host's master switch for live writes "
                                "(host configuration, not set from the app).")
    autopilot_off: bool = Field(description="The autopilot itself is switched off: "
                                "nothing new is prepared.")
    workflows_off: list[str] = Field(default_factory=list)


class AutopilotScoreBar(_Model):
    """A workflow's held-out score for one person against the gate's bar."""
    judged: int
    matched: int
    percent: Optional[float] = None
    need_judged: int
    need_percent: float


class AutopilotSuspension(_Model):
    """Why a person's opt-in was switched off by itself: the workflow's score for them
    no longer meets the bar (``below_bar``), too few items are judged (``too_few``), no
    score is on record (``no_score``) or its gate was lost (``gate_lost``)."""
    at: Optional[str] = None
    reason: str
    score: Optional[AutopilotScoreBar] = None


class AutopilotOptIn(_Model):
    """One of your workflows you may switch on for yourself: it passed its gate for you,
    or you switched it on before and it was suspended. Off until you switch it on."""
    workflow: str
    eligible: bool = Field(description="It passes its gate for you now.")
    on: bool = Field(description="You switched it on (and it was not suspended since).")
    since: Optional[str] = Field(None, description="When you switched it on.")
    score: Optional[AutopilotScoreBar] = None
    suspended: Optional[AutopilotSuspension] = None


class AutopilotOptInPerson(_Model):
    """Someone who switched a workflow on for themselves (shown to the owner and admins,
    who can see it but cannot switch it for them)."""
    person: str
    workflow: str
    since: Optional[str] = None
    paused: bool = False


class AutopilotGateNotice(_Model):
    """A workflow that passed its fresh gate for a role, which the owner has not yet been
    told of (``operating.autopilot_write.notices``). Information only: it switches nothing
    on; each person switches the workflow on for themselves."""
    role: str
    workflow: str
    passed_at: str


class AutopilotNoticeAck(_Model):
    role: str = Field(min_length=1, max_length=200)
    workflow: str = Field(min_length=1, max_length=200)


class AutopilotArea(_Model):
    """The autopilot area: today's prepared items you may see, per workflow its scores and
    state, the switches (``owner``: you may change them), and your own opt-ins."""
    owner: bool
    gate_notices: Optional[list[AutopilotGateNotice]] = Field(
        None, json_schema_extra=_optional, description="Owner only: each workflow's first "
        "pass of its fresh gate the owner has not acknowledged yet, told once. Absent for "
        "everyone else.")
    switches: AutopilotSwitches
    my_opt_ins: list[AutopilotOptIn] = Field(default_factory=list, description="Your "
                                              "workflows you may switch on for yourself.")
    paused: bool = Field(False, json_schema_extra=_optional,
                         description="You paused everything you switched on.")
    switched_on: Optional[list[AutopilotOptInPerson]] = Field(
        None, json_schema_extra=_optional, description="Owner and admins only: who switched "
        "what on. Absent for everyone else.")
    people: list[AutopilotPerson] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    items: list[AutopilotItem] = Field(default_factory=list)
    workflows: list[AutopilotWorkflowRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    known_workflows: list[str] = Field(default_factory=list, description=(
        "every workflow the autopilot knows (``operating.workflows``), scored or not: the "
        "workflow filter offers them before any is scored or prepared"))
    stale_since: Optional[str] = _stale_since()
    read_at: Optional[str] = _read_at()


class AutopilotDecisionRequest(_Model):
    decision: AutopilotDecisionKind
    text: Optional[str] = Field(None, max_length=8000, description="edit, or accept with "
                                "your edit: the draft's new text.")
    until: Optional[str] = Field(None, max_length=40, description="snooze: until when "
                                 "(ISO instant, at most 30 days ahead).")
    note: Optional[str] = Field(None, max_length=500)
    confirm_token: Optional[str] = Field(None, max_length=80, description="accept of a "
                                         "live item: the token the first accept answered "
                                         "(the person confirmed it goes live).")
    bulk: Optional[bool] = Field(None, description="Part of a bulk decision: a live item is "
                       "refused (409), never performed or offered a confirm.")


class AutopilotSwitchRequest(_Model):
    global_off: bool


class AutopilotWorkflowSwitch(_Model):
    on: bool = Field(description="false: the workflow is switched off for everyone "
                     "(nothing is performed, not even a dry run); true: back on. Switching "
                     "it on never makes it act for anyone: each person does that.")


class AutopilotOptInRequest(_Model):
    workflow: str = Field(max_length=120)
    on: bool = Field(strict=True, description="true: on for you; false: off.")


class AutopilotPauseRequest(_Model):
    paused: bool = Field(strict=True)


class AutopilotAuditRow(_Model):
    at: str
    event: str
    status: Optional[str] = None
    mode: Optional[str] = None
    workflow: Optional[str] = None
    role: Optional[str] = None
    system: Optional[str] = None
    kind: Optional[str] = None
    operation: Optional[str] = None
    target: Optional[str] = None
    item_id: Optional[str] = None
    title: Optional[str] = None
    why: Optional[str] = None
    reasons: list[str] = Field(default_factory=list)
    key: Optional[str] = None
    by: Optional[str] = None
    undoable: bool = False
    undone: bool = False
    # an undo that was tried but is not done: pending, failed or unknown (None otherwise)
    undo_status: Optional[str] = None
    undo_note: Optional[str] = None


class AutopilotAudit(_Model):
    """Every write-path row (newest first): decisions, live attempts and outcomes, gate
    changes and the owner's switches. Owner only."""
    rows: list[AutopilotAuditRow] = Field(default_factory=list)
    live: int = Field(description="Live actions done.")


# --- the autopilot studio (workflow pages, modes, builder, replay, schedules) ------------

AutopilotMode = Literal["off", "suggest", "draft", "auto", "watch"]


class AutopilotStep(_Model):
    """A workflow's trigger, one condition or one action: its registry key and its words."""
    kind: str = Field(max_length=80, description="The registry's key: an event "
                      "('message.received'), a condition ('side_in') or a handler "
                      "('order_entry').")
    words: str = Field(max_length=300, description="The step in plain words.")
    params: dict[str, Any] = Field(default_factory=dict)


class AutopilotGate(_Model):
    """The write gate's reading of one workflow: held-out (fresh) replay when scored, else
    the tuning replay; ``passed`` only from the fresh gate."""
    percent: Optional[float] = None
    judged: int = 0
    matched: int = 0
    fresh: bool = Field(False, description="The counts are the held-out replay's.")
    passed: bool = False
    rule: str = Field(description="What passing takes, in words.")


class AutopilotWorkflowDetail(_Model):
    """One workflow as the studio shows it: what starts it, what it checks and does, its
    gate, its mode, per role its row, what it caught and missed, and its history."""
    workflow: str
    label: str
    trigger: Optional[AutopilotStep] = Field(None, description="Null when no registered "
                                             "autopilot feeds it (a workflow seen in replay "
                                             "only).")
    conditions: list[AutopilotStep] = Field(default_factory=list)
    actions: list[AutopilotStep] = Field(default_factory=list)
    autopilots: list[str] = Field(default_factory=list)
    mode: AutopilotMode
    modes: list[AutopilotMode] = Field(description="The modes this server can set for it "
                                       "now (watch only after its gate passed).")
    gate: AutopilotGate
    rows: list[AutopilotWorkflowRow] = Field(default_factory=list)
    caught: Optional[int] = Field(None, description="Replay items it got right.")
    missed: Optional[int] = Field(None, description="Replay items it got wrong or missed.")
    false_alarms: Optional[int] = Field(None, description="Null: not counted by this gate.")
    hours_saved: Optional[float] = Field(None, description="Null: not measured.")
    hours_basis: Optional[str] = None
    open_items: int = 0
    history: list[AutopilotAuditRow] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class AutopilotCatalog(_Model):
    """What the builder offers: the triggers, conditions and actions the registry knows."""
    triggers: list[AutopilotStep]
    conditions: list[AutopilotStep]
    actions: list[AutopilotStep]
    builder: bool = Field(description="False: this server shows the catalog but does not "
                          "keep or replay a drafted workflow yet.")


class AutopilotWorkflowDraft(_Model):
    label: str = Field(min_length=1, max_length=120)
    trigger: AutopilotStep
    conditions: list[AutopilotStep] = Field(default_factory=list, max_length=12)
    actions: list[AutopilotStep] = Field(min_length=1, max_length=8)
    person: Optional[str] = Field(None, max_length=200)
    role: Optional[str] = Field(None, max_length=120)


class AutopilotDraftKept(_Model):
    workflow: str = Field(description="The draft's id; it starts in Draft mode, never live.")
    mode: AutopilotMode = "draft"
    problems: list[str] = Field(default_factory=list)


class AutopilotReplayRequest(_Model):
    days: int = Field(ge=1, le=60)


class AutopilotReplayItem(_Model):
    at: str
    title: str
    outcome: Literal["caught", "missed", "false_alarm"]
    why: Optional[str] = None


class AutopilotReplay(_Model):
    """A workflow tried on past days: what it would have prepared against what people did.
    Nothing is sent or written by a replay."""
    workflow: str
    days: int
    judged: int
    caught: int
    missed: int
    false_alarms: int
    items: list[AutopilotReplayItem] = Field(default_factory=list)


class AutopilotModeRequest(_Model):
    mode: AutopilotMode
    role: Optional[str] = Field(None, max_length=120, description="draft/auto: the role "
                                "(or person: role) it applies to.")


class AutopilotSchedule(_Model):
    id: str
    label: str
    workflow: str
    cadence: str = Field(description="When it runs, in words.")
    checks: str = Field(description="What each run looks at, in words.")
    on: bool
    last_run: Optional[str] = None
    next_run: Optional[str] = None


class AutopilotSchedules(_Model):
    schedules: list[AutopilotSchedule] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


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
    live: bool = Field(False, description="The page showed this run as a live write (the "
                       "preview's live_missing was empty). A run that would write live is "
                       "refused (409) unless this is true.")


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


# ---- the work log (``/api/work-log*``, ``operator_api.work_log``) ---------------------------

WorkLogKind = Literal["ask", "alert", "autopilot", "action", "write", "watch", "note", "save"]


class WorkLogRecord(_Model):
    object_id: str
    type: Optional[str] = None
    label: Optional[str] = None


class WorkLogLoop(_Model):
    """A sent message's follow-through: waiting, answered (a reply arrived), overdue (no
    reply inside the window: ``nudge`` and a follow-up to ask for) or closed by you."""
    state: Literal["waiting", "answered", "overdue", "closed"]
    due: Optional[str] = None
    answered_at: Optional[str] = None
    possible_reply_at: Optional[str] = Field(
        None, description="Mail from a recipient after the send, outside its thread: "
        "maybe the reply, never counted as one.")
    nudge: Optional[str] = None
    follow_up: Optional[str] = Field(None, description="A question for Ask that drafts the "
                                     "follow-up (nothing is sent).")


class WorkLogEvent(_Model):
    seq: int
    at: str
    mine: bool
    reader_name: Optional[str] = Field(None, description="Who did it (the owner's view).")
    kind: WorkLogKind
    verb: str
    title: str
    thread: str = Field(description="What \"continue\" reopens (GET /api/work-log/threads/"
                                    "{thread}).")
    source: str = Field(description="The store the item lives in (actions_journal, "
                                    "autopilot_queue, write_intents, watchlists, feedback, "
                                    "ai_watcher, ask, saved).")
    source_id: Optional[str] = None
    records: list[WorkLogRecord] = Field(default_factory=list)
    people: list[str] = Field(default_factory=list)
    thread_name: Optional[str] = None
    pinned: bool = False
    case_id: Optional[str] = None
    loop: Optional[WorkLogLoop] = None


class WorkLogPage(_Model):
    events: list[WorkLogEvent]
    total: Optional[int] = Field(description="Null when the log cannot be shown (the people "
                                 "exclusion list is not loaded: fail closed).")
    offset: int = 0
    all_readers: bool = False


class MemoryItem(_Model):
    """One thing the Workbench remembers about you (``operator_api.memory``): a standing
    instruction, a preference, an open commitment or a fact, with where it came from."""
    id: int
    kind: Literal["fact", "preference", "instruction", "commitment", "summary"]
    source: str = Field(description="What it was learned from: ask, clarify, autopilot, "
                                    "learn, ...")
    at: str
    text: str
    records: list[str] = Field(default_factory=list)
    importance: float
    confidence: float


class MemoryItems(_Model):
    items: list[MemoryItem]
    available: bool = Field(description="False when memory is off (no store) or cannot be "
                                        "read now (the people exclusion is not loaded).")


class MemoryForgotten(_Model):
    forgotten: bool


class TrustFreshness(_Model):
    """One source system: the newest capture taken from it, the newest capture the served
    data holds, and the lag between them."""
    system: System
    captured_at: Optional[str] = Field(None, description="The newest capture taken (the "
                                       "capture ledger).")
    served_at: Optional[str] = Field(None, description="The newest capture applied to the "
                                     "data the app serves.")
    lag_seconds: Optional[int] = Field(None, description="captured_at - served_at; absent "
                                       "when either is unknown.")
    stale: bool = False
    capture: Optional[str] = None


class TrustAnswerScore(_Model):
    at: str
    runs: int
    items: int
    correct: int
    answered: int
    accuracy: Optional[float] = Field(None, description="Percent of answered runs correct.")
    coverage: Optional[float] = Field(None, description="Percent of scored runs answered.")
    evidence_label: str


class TrustAnswerSet(_Model):
    name: str
    dev: Optional[TrustAnswerScore] = Field(None, description="Absent: not measured yet.")
    held_out: Optional[TrustAnswerScore] = Field(None, description="Absent: not measured "
                                                 "yet.")


class TrustAlert(_Model):
    type: str
    kind: str
    label: str
    on: bool = Field(description="On by default (shown on My work when home).")
    home: bool
    judged: Optional[int] = Field(None, description="Items hand-checked; absent when the "
                                  "type was never measured.")
    correct: Optional[int] = Field(None, description="Items right; absent when the "
                                   "registry's rounded rate gives no whole count.")
    percent: Optional[float] = None
    measured_on: Optional[str] = None
    note: Optional[str] = None


class TrustLine(_Model):
    state: Literal["live", "gated", "pending"]
    words: str


class TrustSwitch(_Model):
    system: str
    state: Literal["on", "off", "none"] = Field(description="none: no write path exists.")
    words: str


class TrustGate(_Model):
    """The autopilot's write gate as the write rules set it (``rules.gate``)."""
    need_judged: int = Field(description="Judged held-out items (fresh days or past days "
                             "never tuned on) a workflow needs.")
    need_percent: float = Field(description="Precision it needs on them, in percent.")


class TrustPage(_Model):
    """What a reader needs to judge the Workbench: data freshness per system, answer
    scores per question set, alert precision per type, the autopilot's workflows and
    what is live, gated and pending. Counts and times only."""
    checked_at: str
    freshness: list[TrustFreshness] = Field(default_factory=list)
    answers: list[TrustAnswerSet] = Field(default_factory=list)
    alert_rule: str = ""
    alerts: list[TrustAlert] = Field(default_factory=list)
    autopilot: list[AutopilotWorkflowRow] = Field(default_factory=list)
    autopilot_note: Optional[str] = None
    autopilot_gate: Optional[TrustGate] = Field(None, description="The write gate's numbers; "
                                                "absent when the write rules are unreadable "
                                                "(then nothing acts).")
    lines: list[TrustLine] = Field(default_factory=list)
    switches: list[TrustSwitch] = Field(default_factory=list)


class AutopilotScoreDay(_Model):
    day: str = Field(description="The day (US Eastern), ISO date.")
    prepared: int = Field(description="Items prepared for you, counted the day you first "
                          "saw them.")
    accepted: int
    edited: int
    rejected: int


class AutopilotScorecard(_Model):
    """Your daily autopilot scorecard from your work log, newest day first. ``available``
    False (nothing shown) while the people exclusion is not loaded."""
    available: bool
    days: list[AutopilotScoreDay] = Field(default_factory=list)


class TimeSavedWeek(_Model):
    start: str = Field(description="The week's Monday (US Eastern), ISO date.")
    minutes: int
    items: dict[str, int] = Field(default_factory=dict, description="How many of each basis "
                                  "(mail_sent, teams_sent, record) the minutes count.")


class TimeSavedPerson(_Model):
    name: Optional[str] = None
    mine: bool = False
    weeks: list[TimeSavedWeek]


class TimeSaved(_Model):
    """Per person and week, the minutes the Workbench saved them, from their work log: an
    estimate whose ``method`` says how it is counted. A reader sees their own; the owner
    may see everyone's (``all_readers``). ``available`` False (nothing shown) while the
    people exclusion is not loaded."""
    available: bool
    all_readers: bool = False
    weeks: list[str] = Field(default_factory=list)
    people: list[TimeSavedPerson] = Field(default_factory=list)
    minutes: dict[str, int] = Field(default_factory=dict, description="Minutes per item of "
                                    "each basis.")
    basis: dict[str, str] = Field(default_factory=dict)
    method: str = ""


class WorkLogTurn(_Model):
    question: str
    answer: Optional[dict[str, Any]] = Field(description="The answer as you saw it; null "
                                             "(``withheld`` says why) when a source it cites "
                                             "is no longer open to you.")
    withheld: Optional[str] = None
    at: str


class WorkLogResume(_Model):
    href: str
    turns: list[WorkLogTurn] = Field(default_factory=list, description="An Ask session's "
                                     "turns, oldest first, as you saw them.")


class WorkLogThread(_Model):
    thread: str
    name: Optional[str] = None
    pinned: bool = False
    case_id: Optional[str] = None
    closed_at: Optional[str] = None
    events: list[WorkLogEvent]
    related: list[WorkLogEvent] = Field(default_factory=list, description="Your other work "
                                        "on the same records.")
    resume: WorkLogResume


class WorkLogOrganise(_Model):
    name: Optional[str] = Field(None, max_length=120)
    pinned: Optional[bool] = None
    case_id: Optional[str] = Field(None, max_length=120, pattern=r"^[A-Za-z0-9_.:-]*$")
    closed: Optional[bool] = Field(None, description="Close a waiting loop yourself.")


class WorkLogSave(_Model):
    """The record page's or Ask's Save (kept in the browser too)."""
    saved: bool
    kind: Literal["object", "question"]
    label: str = Field(min_length=1, max_length=300)
    object_type: Optional[str] = Field(None, max_length=60)
    object_id: Optional[str] = Field(None, max_length=400)


# ---- instruct: tell it to do anything, anywhere ----------------------------------------

class InstructContext(_Model):
    """The page the instruction was typed on, attached by the app: an email (its citation),
    a record, a meeting, an autopilot item, or just the page."""
    kind: Literal["email", "source", "record", "meeting", "autopilot", "page"]
    handle: Optional[str] = Field(None, max_length=4096)
    object_type: Optional[str] = Field(None, max_length=60)
    object_id: Optional[str] = Field(None, max_length=700)
    meeting_id: Optional[str] = Field(None, max_length=200)
    item_id: Optional[str] = Field(None, max_length=200)
    page: Optional[str] = Field(None, max_length=300)


class InstructFollowUp(_Model):
    """An answer to the question an instruction asked, or the meetings to use."""
    instruction_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    answer: Optional[str] = Field(None, max_length=600)
    use: list[str] = Field(default_factory=list, max_length=3)


class InstructRead(_Model):
    """Something the instruction read for you, as you may open it."""
    kind: Literal["email", "record", "meeting", "work_log", "attachment"]
    label: str
    open_url: Optional[str] = None


class InstructMeeting(_Model):
    meeting_id: str
    title: str
    day: str


class InstructStep(_Model):
    kind: str
    words: str
    params: dict[str, Any] = Field(default_factory=dict)


class InstructWorkflow(_Model):
    """A recurring instruction as a proposed autopilot workflow: Draft mode, never live
    until the owner keeps it and switches it on."""
    label: str
    trigger: InstructStep
    conditions: list[InstructStep] = Field(default_factory=list)
    actions: list[InstructStep]
    mode: Literal["draft"] = "draft"


class InstructWrite(_Model):
    """An exact change prepared on the write desk (``/api/writes/{intent_id}``): each field
    as it is now and as it would be. A grant holder other than you approves it there."""
    intent_id: Optional[str] = None
    label: str
    changes: list[WriteChange] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    status: Literal["prepared", "refused"]


class InstructDocument(_Model):
    """A document made as a file, and its upload to SharePoint as a draft you approve
    (``/api/instruct/uploads/{upload_id}``)."""
    name: str
    kind: Literal["docx", "xlsx", "pdf"]
    size: int
    digest: str
    download_url: str
    upload_id: str
    folder: Optional[str] = None


class FillCitation(_Model):
    """Where a filled value was read: the record, its property and the source it came
    from (``open_url`` opens that source as you may read it)."""
    record: str
    field: str
    source: str
    open_url: Optional[str] = None


class FillField(_Model):
    """One blank of the form: what it held, what the fill put there and why."""
    label: str
    before: str
    after: str
    status: Literal["filled", "kept", "person_only", "unknown_field", "no_fact",
                    "ambiguous", "suggested"] = Field(description=(
                        "filled: from one value the records agree on (``citation``); kept: it "
                        "already held a value; person_only: a signature, signer or signing "
                        "date; unknown_field: no rule for the label; no_fact: no record read "
                        "holds it; ambiguous: the records disagree; suggested: a value from "
                        "a source that cannot be certain (``after``, ``citation``) for the "
                        "person to check, not written. Only filled changes."))
    reason: str = ""
    citation: Optional[FillCitation] = None


class FillReply(_Model):
    """The filled copy as a reply to the email the form came in: prepared, never sent from
    here (``missing`` says what a send with a file needs)."""
    to: list[str]
    subject: str
    attachment: str
    status: Literal["not_available"]
    missing: list[str]


class InstructFill(_Model):
    """A form filled from the records as a new file (the original is never changed),
    every blank listed with its source, for review before anything is saved. Saving it
    to SharePoint is the upload you approve (``/api/instruct/uploads/{upload_id}``)."""
    name: str = Field(description="The filled copy's file name.")
    original: str
    kind: Literal["docx", "xlsx", "pdf"]
    record: ObjectRef
    counts: dict[str, int]
    fields: list[FillField]
    notes: list[str] = Field(default_factory=list)
    digest_before: str
    digest_after: Optional[str] = None
    size: int = Field(0, description="The filled copy's bytes (0: nothing was filled).")
    download_url: Optional[str] = Field(None, description="The filled copy, for you only; "
                                                          "none when nothing was filled.")
    upload_id: Optional[str] = None
    folder: Optional[str] = None
    reply: Optional[FillReply] = None


class DocFillRequest(_Model):
    """Fill this form for this record: the file (base64), the record (an identifier such
    as "PO 1234" or a record id), and optionally the SharePoint file it came from (the
    filled copy goes beside it) or the email it came in (a reply is prepared)."""
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(min_length=1, max_length=11_200_000)
    record: str = Field(min_length=1, max_length=700)
    source: Optional[str] = Field(None, max_length=4096)
    reply_to: Optional[str] = Field(None, max_length=4096)


class InstructPart(_Model):
    """What an instruction came back with beside its drafts: one question (or, when it
    was unclear whether to do it or answer it, ``route``), exact writes, documents or a
    proposed workflow; and what continuing it needs (its words, page, your answers, the
    meetings used and offered). Nothing is sent, written or uploaded until a person
    approves it."""
    status: Literal["question", "drafted", "workflow", "route"]
    text: str
    question: Optional[str] = None
    options: list[str] = Field(default_factory=list, max_length=4, description="Choices "
                               "for the question, each from your materials; answering in "
                               "your own words works too.")
    proposed: list[InstructMeeting] = Field(default_factory=list, description="Meetings "
                                            "whose notes the question proposes to use.")
    meetings: list[InstructMeeting] = Field(default_factory=list, description="Meetings you "
                                            "attended that may hold context.")
    writes: list[InstructWrite] = Field(default_factory=list)
    documents: list[InstructDocument] = Field(default_factory=list)
    fills: list[InstructFill] = Field(default_factory=list, description="Forms filled from "
                                      "the records, each as a new file to review.")
    workflow: Optional[InstructWorkflow] = None
    read: list[InstructRead] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    answers: list[str] = Field(default_factory=list)
    meetings_used: list[str] = Field(default_factory=list)
    files: list[AskAttachmentNote] = Field(default_factory=list, description="The files "
                                           "this instruction read (never their content): "
                                           "continuing it needs them attached again.")


class UploadPreview(_Model):
    upload_id: str
    name: str
    kind: str
    size: int
    digest: str
    folder: Optional[str] = None
    before: str
    after: str
    problems: list[str]
    missing: list[str]
    preview_hash: str
    acknowledge: list[str] = Field(default_factory=list, description=(
        "Why this upload is not a routine write, in words: the reader's confirm accepts "
        "each (the preview_hash covers them)."))
    status: Literal["prepared", "would_upload", "uploaded", "failed", "unknown"] = Field(
        description="would_upload: approved and recorded, not made (``missing`` says why); "
                    "uploaded: made through the gated write path; unknown: check SharePoint.")


class UploadApprove(_Model):
    confirm: bool = Field(description="True only from the person's second (Confirm) click.")
    preview_hash: str = Field(max_length=64)


# AskDraft names CardActionOffer before it is defined
AskDraft.model_rebuild()
AnswerPayload.model_rebuild()
AskRequest.model_rebuild()
