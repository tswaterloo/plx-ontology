"""The flagship path, run end to end on one state directory's captures, per order, and
scored against the captured source rows (never against the product's own output).

Orders come from the lateness model, read by a raw-capture oracle
(``evals.oracle.po_lateness``: open purchase-order lines past their governing date, from
the captured Portal rows and Business Central records the reader may read, never through
the projection, its links or its tools). Each order runs the steps a person takes at the
console, each through the console's own path
(``operator_ui.case_console.object_workspace_for``), and records ``pass``, ``fail`` or
``blocked`` per step with the exact reason:

1. ``home``: scored as recall. The run reports ``home.recall``: the late orders the home
   lists (``ObjectWorkspace.gap_inbox``: a gap on one of the order's records) out of the
   late orders the oracle finds. Per order: ``late_order_listed`` or a fail.
2. ``ask``: Ask answers "why is this order late / what is missing" through its one path
   (``ObjectWorkspace.ask_run``), naming the question's family (``QUESTION_FAMILY``, the
   late-order plan; ``--family`` for another ``--question``) and asking for its plan answer
   (``plan_answer``), so the plan's result is the answer when code states it whole (no
   planner call; else the planner goes on from it). It passes only when the answer is correct against the
   oracle: it cites every late line of the order in one system, names each line's
   governing date in a claim that cites the record the date is read from, cites a
   Business Central record of the order and a second source: a captured mail, SharePoint
   document or Teams message whose own text names one of the order's PO numbers
   (``names_number``: one order whatever its written form, ``PO#4643``, ``PO 4643``,
   ``PO4643``, ``PO-4643``; a whole token with its prefix, never a bare number). A claim
   citing another order's record, a document that does not name the number, or a claim the
   answer itself marks a lead (reached through a lead-tier link) must say it is a lead
   (``LEAD_WORDS``); a worded lead still counts as a second source when the document names
   the number. A Business Central-Portal number twin is never a second source (decision
   ``2026-10-28-flagship-second-source-rule``).
   The order's numbers are its group's: a BC order joins its Portal order's group by a
   firm ``Your_Reference`` link only (the product's rule, ``evals.oracle.po_reference.
   decide``, a copy a parity test ties to it; a lead never joins), so a document naming the
   FileMaker PO number of a firmly linked order is a second source for it.
3. ``timing``: the Ask took ``ASK_SECONDS`` or less; slower fails.
4. ``landed_cost``: passes only with a number (``plx.investigate`` family ``landed_cost``;
   every landed cost is an analytic estimate, labelled ``estimate``). Naming the missing
   inputs alone fails.
5. ``draft``: a follow-up is drafted on a Workbench case, never sent (``draft_step``).
6. ``margin``: the order margin of the customer order this purchase order feeds, over firm
   links only. The schema declares no direct purchase-order to sales-order link, so the
   order is the open one the impact chain (``impact_chain`` plan) ranks first among those
   the PO feeds: PO line -> item, the Portal entity map's verified BC item <-> Portal
   product identity, FileMaker assembly orders, their order lines, the sales order. No
   order reached fails with where the walk stopped. The margin is finance-gated: a reader
   without the finance marking gets ``margin_withheld`` (blocked), never a figure. Once per
   run, ``margin`` also reports one sample sales order's margin (``scripts.flagship_margin``,
   ``unrelated_sample``, never a pass).

Nothing is written to a source system and no message is sent: the only writes are the
Workbench's own case and action stores under ``--work-dir``.

Output (stdout, JSON): counts and enums per order and step, with each step's seconds;
with ``--identify`` also each order's number and object id, the answer's claims and the
drafts' text (for the walkthrough; never a live-check record).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.oracle import po_lateness  # noqa: E402
from evals.oracle.keys import FM_PO, PO, PO_LINE, RECEIPT  # noqa: E402

SCHEMA = "plx-flagship-path/v2"
QUESTION = "Why is this order late, and what is missing?"
QUESTION_FAMILY = "late_po"   # the plan (ontology/plans) that answers QUESTION
STEPS = ("home", "ask", "timing", "landed_cost", "draft", "margin")
PURCHASE_ORDER = "PurchaseOrder"
ASK_SECONDS = 20  # an Ask slower than this fails the timing step
MAX_ORDERS = 10
PICK_LIMIT = 20  # candidates one plx.find lists when mapping an order to its object
HOME_SCAN = 500  # home gaps on purchase orders mapped to their records, at most
RAW_PAGE, RAW_ROWS = 500, 50_000  # mirror rows per page; per relation at most
BC_PAGE = 1000
BC_CAPTURE = Path("bc-capture") / "capture.sqlite"
BC_SETS = (po_lateness.BC_ORDERS, po_lateness.BC_LINES, po_lateness.BC_RECEIPTS,
           po_lateness.BC_REFERENCES)
PORTAL_RELATIONS = (PO, PO_LINE, RECEIPT, FM_PO, po_lateness.ENTITY_MAP)
# The documents that can be the Ask's second source: captured mail, SharePoint and Teams.
DOCUMENT_SYSTEMS = ("graph-mail", "sharepoint", "teams-chat", "teams-channel")
TEAMS_SYSTEMS = ("teams-chat", "teams-channel")
# A PO number as written: its series prefix (PO, DPO) and an optional '#', '-' or space
# before the digits, a whole token. One order, whatever the form (PO#4643 = PO 4643); a
# suffixed number (PO#4643-FM12, an FM clash-series order) is another order.
PO_FORM = re.compile(r"(?<![A-Za-z0-9#-])(D?PO)[ \t]*[#-]?[ \t]*0*(\d{1,9})"
                     r"(?![A-Za-z0-9]|-[A-Za-z0-9])", re.IGNORECASE)
FIRM_FLOOR = "corroborated"
# completeness reasons that cut no part of the walk: the answer fitted to one response or paged
FITTED = ("result_too_large", "answer_paged")
IMPACT_FAMILY = "impact_chain"   # the plan that walks a late PO to the orders it feeds
LEAD_WORDS = re.compile(r"\b(?:lead|leads|unconfirmed|not confirmed)\b", re.IGNORECASE)
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")
ORDER_SIDE = frozenset({"PurchaseOrder", "PurchaseOrderLine"})
SALES_SIDE = frozenset({"SalesOrder", "SalesOrderLine"})


def step(status: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"status": status, "reason": reason, **detail}


def timed(run_step: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """``run_step``'s result with the wall seconds it took (``seconds``)."""
    started = time.monotonic()
    result = run_step(*args, **kwargs)
    return {**result, "seconds": round(time.monotonic() - started, 1)}


def handle_base(handle: str) -> str:
    """A citation handle without its version (``...@<version>``): the record it names."""
    return handle.rsplit("@", 1)[0] if isinstance(handle, str) else ""


def system_of(handle: str) -> str:
    """The source system a citation handle names (its scheme, e.g. ``api``, ``bc``,
    ``graph-mail``, ``sharepoint``); ``unknown`` when it carries none."""
    scheme = handle.split(":", 1)[0] if isinstance(handle, str) and ":" in handle else ""
    return scheme or "unknown"


def _card_handle(item: Mapping[str, Any]) -> str | None:
    origin = (item.get("display") or {}).get("origin") or {}
    return origin.get("citation") if isinstance(origin.get("citation"), str) else None


def own_record(workspace: Any, object_id: str) -> str | None:
    """The record holding the object's own card (``handle_base`` of its display's handle,
    else of its values')."""
    card = workspace._call("plx.object", {"object_id": object_id})
    handle = _card_handle(card.get("object") or {}) or next(
        (value["citation"]["l1_handle"] for value in card.get("values") or ()
         if isinstance((value.get("citation") or {}).get("l1_handle"), str)), None)
    return handle_base(handle) if handle else None


def date_texts(day: date) -> list[str]:
    """The ways an answer can write ``day``: ISO, "September 1, 2026", "Sep 1, 2026",
    "1 September 2026", "9/1/2026" and "09/01/2026"."""
    month = MONTHS[day.month - 1]
    return [day.isoformat(), f"{month} {day.day}, {day.year}",
            f"{month[:3]} {day.day}, {day.year}", f"{day.day} {month} {day.year}",
            f"{day.month}/{day.day}/{day.year}", f"{day.month:02d}/{day.day:02d}/{day.year}"]


def names_date(text: str, day: date) -> bool:
    return any(re.search(r"(?<![0-9A-Za-z])" + re.escape(form) + r"(?![0-9])", text,
                         re.IGNORECASE) for form in date_texts(day))


def po_key(number: str) -> tuple[str, int] | None:
    """(series, number) of a PO number written whole (``PO#4643`` -> ("PO", 4643)), else
    None: a bare number or any other text is no PO number."""
    found = PO_FORM.fullmatch(number.strip()) if isinstance(number, str) else None
    return (found.group(1).upper(), int(found.group(2))) if found else None


def names_number(text: str, numbers: Sequence[str]) -> bool:
    """Whether ``text`` names one of ``numbers``, PO numbers each (``po_key``), in any of
    their written forms as a whole token. A bare number names nothing, nor does a number
    of the order that carries no PO prefix: it matches any quantity, line or reference."""
    wanted = {key for key in map(po_key, numbers) if key is not None}
    return bool(wanted) and any((m.group(1).upper(), int(m.group(2))) in wanted
                                for m in PO_FORM.finditer(text or ""))


# ---- the raw captures the oracle reads -------------------------------------------


class RawCaptures:
    """The captured source rows of one reader, read directly (module doc): the Portal
    mirror relations through ``runtime.mirror.rows`` and the Business Central capture
    through ``CaptureStore`` with the employee-gated reader. A relation or unit the reader
    may not read is skipped and named in ``unread`` (never read as absence of lateness).
    ``confirms`` reads a cited mail or SharePoint document's own captured text."""

    def __init__(self, runtime: Any, actor: Any) -> None:
        self.runtime, self.actor = runtime, actor
        self.unread: list[str] = []

    def portal(self) -> dict[str, list[dict[str, Any]]]:
        from integrations.source_mirror import MirrorError
        out: dict[str, list[dict[str, Any]]] = {}
        for relation in PORTAL_RELATIONS:
            rows: list[dict[str, Any]] = []
            try:
                while len(rows) < RAW_ROWS:
                    page = self.runtime.mirror.rows(self.actor, relation, limit=RAW_PAGE,
                                                    offset=len(rows))
                    rows += [{"row": item["row"], "citation": item["citation"]}
                             for item in page.get("rows") or ()]
                    if not page.get("truncated") or not page.get("rows"):
                        break
                else:
                    self.unread.append(f"{relation}:row_cap")
            except MirrorError:
                self.unread.append(relation)
                continue
            out[relation] = rows
        return out

    def bc(self) -> list[dict[str, Any]]:
        from integrations.capture_store import CaptureError, CaptureStore
        path = Path(self.runtime.state_dir) / BC_CAPTURE
        live = getattr(self.runtime, "live", None)
        if live is None or not path.is_file():
            self.unread.append("bc_capture")
            return []
        try:
            gated = self.runtime.employee_gate(self.actor)
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
                units = [u for (u,) in conn.execute(
                    "SELECT DISTINCT unit FROM versions WHERE kind = 'bc' ORDER BY unit")]
            store = CaptureStore.open_read_only(path, live.audience)
        except (PermissionError, CaptureError, sqlite3.Error, OSError):
            self.unread.append("bc_capture")
            return []
        out: list[dict[str, Any]] = []
        try:
            for unit in units:
                company, _, entity_set = unit.partition(":")[2].partition("/")
                if entity_set not in BC_SETS:
                    continue
                after = None
                try:
                    while True:
                        page = store.records(gated, "bc", unit, after_path=after,
                                             limit=BC_PAGE)
                        out += [{"company_id": company, "entity_set": entity_set,
                                 "fields": dict(item["row"]), "citation": item["citation"]}
                                for item in page["items"]]
                        after = page.get("next_after_path")
                        if not after:
                            break
                except (CaptureError, PermissionError):
                    self.unread.append(f"bc_unit:{entity_set}")
        finally:
            store.close()
        return out

    def confirms(self, handle: str, numbers: Sequence[str]) -> bool:
        """Whether the captured mail or SharePoint document ``handle`` names one of
        ``numbers`` exactly in its own text; False when it is not captured or not readable
        for this reader (a live-read document cannot be confirmed: fails closed)."""
        system = system_of(handle)
        try:
            gated = self.runtime.employee_gate(self.actor)
            audience = self.runtime.live.audience
            if system == "graph-mail":
                from operating.ontology_tools_evidence import RuntimeCitations
                if not audience.may_read_any(gated, "mail:"):
                    return False
                found = RuntimeCitations(
                    self.runtime, mail_capture_dir=self.runtime.mail_capture_dir
                ).mail_capture(handle, gated, audience, text=True)
                row = (found[0] or {}).get("row") if found else None
            elif system in TEAMS_SYSTEMS:
                if not audience.may_read_any(gated, "teams:"):
                    return False
                # The runtime's Teams read: the store's per-record membership check and
                # the people exclusion (fail-closed), as plx.read serves the message.
                found = self.runtime.claim_row(handle, gated)
                row = (found or {}).get("row") if not (found or {}).get("changed_since") \
                    else None
            elif system == "sharepoint":
                from integrations.capture_sharepoint import SharePointCaptureStore
                store = SharePointCaptureStore.open_read_only(self.runtime.state_dir)
                try:
                    found = store.resolve(handle, gated, audience, text=True)
                finally:
                    getattr(store, "close", lambda: None)()
                row = (found or {}).get("row")
            else:
                return False
        except Exception:  # noqa: BLE001 - unreadable is unconfirmed, never confirmed
            return False
        if not isinstance(row, Mapping):
            return False
        text = " ".join(str(row.get(field) or "")
                        for field in ("subject", "body_text", "content_text", "name"))
        return names_number(text, numbers)


def lateness(raw: Any, as_of: date) -> dict[str, Any]:
    """The oracle's answer over ``raw``'s captured rows, with the parts it could not read."""
    result = po_lateness.late_orders(raw.portal(), raw.bc(), as_of)
    result["counts"]["unread"] = sorted(set(raw.unread))
    return result


# ---- the steps --------------------------------------------------------------------


def home_inbox(workspace: Any, as_of: date) -> Mapping[str, Any]:
    """The gaps the home shows: the scheduled engine's inbox (``gap_inbox``). Where the
    store has no inbox yet (the fixture world is projected offline, no host cycle), the
    engine runs once over the store into a temporary inbox and the same read follows."""
    home = workspace.gap_inbox()
    path = getattr(getattr(workspace, "store_status", None), "store_path", None)
    if home.get("status") != "not_scheduled" or path is None:
        return home
    import tempfile
    from operating import proactive_engine
    with tempfile.TemporaryDirectory() as tmp:
        inbox = Path(tmp) / "inbox.sqlite"
        proactive_engine.run(path, inbox, as_of)
        return workspace.gap_inbox(inbox=inbox)


def home_records(workspace: Any, home: Mapping[str, Any]) -> dict[str, str]:
    """The records the home's purchase-order gaps sit on (``own_record``), each to its
    object id; ``HOME_SCAN`` objects at most."""
    objects: list[str] = []
    for gap in home.get("gaps") or ():
        if gap.get("object_type") == PURCHASE_ORDER and gap.get("object_id") not in objects:
            objects.append(gap["object_id"])
    out: dict[str, str] = {}
    for object_id in objects[:HOME_SCAN]:
        record = own_record(workspace, object_id)
        if record:
            out[record] = object_id
    return out


def listed(group: Mapping[str, Any], records: Mapping[str, str]) -> bool:
    return any(handle_base(h["citation"]) in records for h in group["headers"])


def home_recall(late: Sequence[Mapping[str, Any]], records: Mapping[str, str]
                ) -> dict[str, Any]:
    """The late orders the home lists out of the late orders the oracle finds (``recall``
    None when the oracle finds none)."""
    hits = sum(1 for group in late if listed(group, records))
    return {"listed": hits, "late": len(late),
            "recall": round(hits / len(late), 3) if late else None}


def home_step(group: Mapping[str, Any] | None, records: Mapping[str, str],
              home: Mapping[str, Any]) -> dict[str, Any]:
    if group is None:
        return step("fail", "order_not_late_by_oracle")
    if listed(group, records):
        return step("pass", "late_order_listed")
    if home.get("truncated"):
        return step("fail", "order_not_reached_by_home_scan", scanned=home.get("scanned"),
                    unscanned=home.get("unscanned"))
    if home.get("more"):   # the list is capped and late orders come first: report the cap
        return step("fail", "late_order_beyond_home_cap", limit=home.get("limit"))
    return step("fail", "late_order_not_on_home", scanned=home.get("scanned"))


def ask_step(workspace: Any, object_id: str, question: str, *,
             late: Mapping[str, Any] | None, others: set[str],
             confirm: Callable[[str, Sequence[str]], bool], identify: bool,
             family: str | None = None) -> dict[str, Any]:
    """The Ask's verdict against the oracle (module doc): ``late`` is the order's late
    group, ``others`` the records of every other order the oracle holds, ``confirm``
    whether a cited mail or SharePoint document names the order's number. ``family``: the
    question family whose plan answer the Ask asks for (None: the planner chooses)."""
    started = time.monotonic()
    request = {"object_id": object_id, "question": question}
    if family is not None:
        request.update(family=family, plan_answer=True)
    try:
        asked = workspace.ask_run(request)
    except LookupError as error:
        return step("blocked", str(error.args[0]) if error.args else "ask_not_served",
                    missing_setting="CLAUDE_CODE_OAUTH_TOKEN or PLX_CLAUDE_CODE_OAUTH_TOKEN "
                                    "(the Ask planner's credential, operating.agent_environment)")
    except Exception as error:  # noqa: BLE001 - the planner or loop failed: report, go on
        return step("fail", f"ask_error:{type(error).__name__}", detail=str(error)[:200])
    ask_seconds = round(time.monotonic() - started, 1)   # the Ask alone, not the scoring
    claims, loop = asked["claims"], asked["loop"]
    systems = Counter(system_of(handle) for claim in claims for handle in claim["citations"])
    numbers = list((late or {}).get("numbers") or ())
    confirmed: dict[str, bool] = {}

    def is_confirmed(handle: str) -> bool:
        if handle not in confirmed:
            confirmed[handle] = bool(numbers) and confirm(handle, numbers)
        return confirmed[handle]

    def is_lead(handle: str) -> bool:
        if system_of(handle) in DOCUMENT_SYSTEMS:
            return not is_confirmed(handle)
        return handle_base(handle) in others

    # A claim the answer marks a lead (it rests on a lead-tier link) must say so too.
    def marked_lead(claim: Mapping[str, Any]) -> bool:
        return claim.get("lead") is True or any(
            isinstance(v, Mapping) and v.get("lead") is True
            for v in claim.get("value_claims") or ())

    lead_claims = [claim for claim in claims if marked_lead(claim)
                   or any(map(is_lead, claim["citations"]))]
    unworded = [claim for claim in lead_claims if not LEAD_WORDS.search(claim["text"])]
    cited = {handle_base(h) for claim in claims for h in claim["citations"]}
    lines = list((late or {}).get("late_lines") or ())
    by_system: dict[str, list[Mapping[str, Any]]] = {}
    for line in lines:
        by_system.setdefault(line["system"], []).append(line)
    covered = sorted(s for s, held in by_system.items()
                     if all(handle_base(line["citation"]) in cited for line in held))

    def dated(line: Mapping[str, Any]) -> bool:
        due, source = date.fromisoformat(line["due"]), handle_base(line["date_source"])
        return any(names_date(claim["text"], due) for claim in claims
                   if source in {handle_base(h) for h in claim["citations"]})

    right = [s for s in covered if all(dated(line) for line in by_system[s])]
    bc_records = {handle_base(r) for r in (late or {}).get("records") or ()
                  if system_of(r) == "bc"}
    second = sorted({h for claim in claims for h in claim["citations"]
                     if system_of(h) in DOCUMENT_SYSTEMS and is_confirmed(h)})
    detail: dict[str, Any] = {"ask_seconds": ask_seconds,
        "outcome": asked["outcome"], "loop_reason": asked["reason"],
        "claims_shown": len(claims), "claims_withheld": asked["withheld"],
        "unsupported": len(loop.get("unsupported_claims") or ()),
        "unknowns": len(loop.get("unknowns") or ()),
        "tool_calls": len(loop.get("tool_calls") or ()),
        "systems_cited": dict(sorted(systems.items())),
        "late_lines": len(lines), "late_systems_fully_cited": covered,
        "governing_dates_named": bool(right),
        "bc_record_cited": bool(bc_records & cited), "second_sources": len(second),
        "lead_cited": len(lead_claims), "lead_unworded": len(unworded)}
    if identify:
        detail["claims"] = [{"text": claim["text"], "citations": list(claim["citations"]),
                             **({"lead": True} if claim in lead_claims else {})}
                            for claim in claims]
        detail["unknown_texts"] = [str(item)[:300] for item in loop.get("unknowns") or ()]
        detail["oracle"] = {"governing_dates": list((late or {}).get("governing_dates") or ()),
                            "late_lines": [line["citation"] for line in lines]}
    if asked["outcome"] != "answered":
        return step("fail", f"ask_{asked['outcome']}:{asked['reason']}", **detail)
    if late is None:
        return step("fail", "order_not_late_by_oracle", **detail)
    if unworded:
        return step("fail", "lead_not_worded_as_lead", **detail)
    if not covered:
        return step("fail", "late_lines_not_cited", **detail)
    if not right:
        return step("fail", "governing_date_not_named", **detail)
    if not detail["bc_record_cited"]:
        return step("fail", "no_bc_record_cited", **detail)
    if not second:
        return step("fail", "no_second_source_cited", **detail)
    return step("pass", "names_late_lines_and_date_cross_system", **detail)


def timing_step(ask: Mapping[str, Any]) -> dict[str, Any]:
    """The Ask's own time (``ask_seconds``: ``ask_run`` alone, without the scorer's
    document reads; else the step's): ``ASK_SECONDS`` or less passes, slower fails."""
    seconds = ask.get("ask_seconds", ask.get("seconds"))
    if ask.get("status") == "blocked" or seconds is None:
        return step("blocked", "ask_not_run")
    if seconds > ASK_SECONDS:
        return step("fail", f"ask_over_{ASK_SECONDS}_seconds", seconds=seconds)
    return step("pass", f"ask_within_{ASK_SECONDS}_seconds", seconds=seconds)


def _entries(result: Mapping[str, Any], kind: str) -> dict[str, Mapping[str, Any]]:
    return {entry["name"]: entry for entry in result.get("answer") or ()
            if entry.get("kind") == kind}


def shown_amount(entry: Mapping[str, Any] | None) -> str | None:
    """A computed amount as a person reads it: cents (``landed_cost.money_text``) with its
    unit; the entry's own value keeps its full precision."""
    from decimal import Decimal, InvalidOperation
    from operating.landed_cost import money_text
    value = (entry or {}).get("value")
    unit = (entry or {}).get("unit") or (entry or {}).get("currency")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return f"{money_text(number, unit)} {unit}" if isinstance(unit, str) and unit else str(value)


def landed_step(workspace: Any, object_id: str, *, identify: bool) -> dict[str, Any]:
    """Passes only with a landed-cost number: the actual ``landed_cost``, else the open
    order's ``landed_cost_estimate``; ``estimate`` is read from the entry's own label (a
    step starting ``ESTIMATE``), and an estimate number without that label fails."""
    from evals.oracle.source import decimal_of
    try:
        investigated = workspace.investigate("landed_cost", object_id)
    except (LookupError, KeyError, ValueError) as error:
        return step("fail", f"investigate_refused:{error.args[0] if error.args else error}")
    result = investigated.get("investigation") or {}
    if result.get("error"):
        return step("fail", f"investigate_error:{result['error'].get('code')}")
    computed = _entries(result, "computed")
    gaps = result.get("gaps") or []
    codes = sorted({str(gap.get("reason") or gap.get("code")) for gap in gaps})
    detail: dict[str, Any] = {
        "computed": len(computed), "gaps": len(gaps), "missing_inputs": codes,
        "answerability": (result.get("answerability") or {}).get("state")}
    if identify:
        detail["landed_cost"] = (computed.get("landed_cost") or {}).get("value")
        detail["landed_cost_estimate"] = (computed.get("landed_cost_estimate") or {}
                                          ).get("value")
        detail["gap_subjects"] = [str(gap.get("subject"))[:300] for gap in gaps]
        detail["landed_cost_shown"] = shown_amount(computed.get("landed_cost")
                                                   or computed.get("landed_cost_estimate"))
    for name in ("landed_cost", "landed_cost_estimate"):
        entry = computed.get(name) or {}
        value = entry.get("value")
        if value is None:
            continue
        if isinstance(value, bool) or decimal_of(value) is None:
            return step("fail", "landed_cost_not_a_number", source=name, **detail)
        labelled = any(str(text).startswith("ESTIMATE") for text in entry.get("steps") or ())
        if name == "landed_cost_estimate" and not labelled:
            return step("fail", "landed_cost_estimate_not_labelled", source=name, **detail)
        return step("pass", "landed_cost_shown", source=name, estimate=labelled, **detail)
    if codes:
        return step("fail", "landed_cost_names_only_missing_inputs", **detail)
    return step("fail", "no_landed_cost_and_no_missing_input_named", **detail)


def draft_step(workspace: Any, object_id: str, gaps: Sequence[Mapping[str, Any]], *,
               identify: bool) -> dict[str, Any]:
    if getattr(workspace, "follow_ups", None) is None:
        return step("blocked", "follow_up_not_served")
    detail: dict[str, Any] = {}
    drafts: list[Mapping[str, Any]] = []
    follow_ups: list[Mapping[str, Any]] = []
    try:
        recorded = workspace.record_on_case({"family": "landed_cost", "root": object_id})
        listing = recorded["case"]
        # This order's follow-ups and drafts, never another order's on the same case: the
        # ones this call recorded, and on a rerun (outcome ``exists``, no record id) the
        # follow-ups whose missing text names this order.
        ids = {item.get("record_id") for item in recorded.get("recorded") or ()} - {None}
        drafts += [item for item in listing.get("drafts") or ()
                   if item.get("record_id") in ids]
        follow_ups += [item for item in listing.get("follow_ups") or ()
                       if item.get("record_id") in ids
                       or f" of {object_id}:" in str(item.get("missing") or "")]
        detail["recorded"] = dict(sorted(Counter(
            item.get("outcome") for item in recorded.get("recorded") or ()).items()))
    except (LookupError, KeyError, ValueError, PermissionError) as error:
        detail["record_refused"] = str(error.args[0] if error.args else error)[:200]
    proposals = []
    for gap in gaps:
        try:
            proposals.append(workspace.follow_up(dict(gap["follow_up"]))["proposal"])
        except (LookupError, KeyError, ValueError, PermissionError) as error:
            detail.setdefault("proposal_refused", []).append(
                str(error.args[0] if error.args else error)[:200])
    executed = [p for p in proposals if p.get("executed") or p.get("approved")
                or (p.get("draft") and p["draft"].get("sent") is not False)]
    # The home gap's supplier draft as the server worded and recorded it (the console never
    # keeps the client's text): specific to the order, each fact cited, never sent, and
    # counted only when the case store reads it back as that workbench.draft.record.
    worded = [p for p in proposals if p.get("draft")
              and p["draft"].get("status") == "draft_not_sent"
              and p["draft"].get("sent") is False and p["draft"].get("citations")]
    messages, unread = [], Counter()
    for proposal in worded:
        draft = proposal["draft"]
        try:
            stored = (workspace.case_records(proposal.get("case_id"))["case"]
                      .get("drafts") or ())
        except (LookupError, KeyError, ValueError, PermissionError):
            unread["case_not_read"] += 1
            continue
        found = [item for item in stored if draft.get("record_id")
                 and item.get("record_id") == draft["record_id"]]
        if not found:
            unread["draft_not_on_case"] += 1
        elif found[0].get("statement") != draft.get("text"):
            unread["draft_text_differs"] += 1
        else:
            messages.append(draft)
    detail.update(drafts=len(drafts), supplier_drafts=len(messages),
                  supplier_drafts_not_read_back=dict(sorted(unread.items())),
                  supplier_draft_facts=sum(len(m.get("facts") or ()) for m in messages),
                  supplier_draft_unresolved=sorted({item for m in messages
                                                    for item in m.get("unresolved") or ()}), follow_ups=len(follow_ups), proposals=len(proposals),
                  proposals_cited=sum(1 for p in proposals if p.get("cited")),
                  not_prepared=sorted({p["not_prepared"] for p in proposals
                                       if p.get("not_prepared")}),
                  answerers=sorted({who for item in [*drafts, *follow_ups]
                                    for who in item.get("who_can_answer") or ()}
                                   | {who for gap in gaps
                                      for who in gap["follow_up"]["who_can_answer"]}),
                  executed=len(executed))
    if identify:
        detail["draft_texts"] = [item.get("statement") or item.get("text")
                                 for item in drafts]
        detail["supplier_draft_texts"] = [m.get("text") for m in messages]
    if executed:
        return step("fail", "follow_up_executed", **detail)
    cited = [item for item in [*drafts, *follow_ups]
             if any(cite.get("status") == "resolved" for cite in item.get("citations") or ())]
    if messages:
        return step("pass", "supplier_draft_recorded_not_sent", **detail)
    if drafts and cited:
        return step("pass", "draft_recorded_not_sent", **detail)
    if cited or detail["proposals_cited"]:
        return step("pass", "follow_up_recorded_not_sent", **detail)
    return step("fail", "no_cited_draft_or_follow_up", **detail)


def feeds_links(schema: Any = None) -> list[str]:
    """The schema's link types between a purchase order (or its line) and a customer's
    sales order (or its line): the only way a purchase order feeds a customer order."""
    if schema is None:
        from ontology.schema import load_schema
        schema = load_schema()
    return sorted(link_id for link_id, link in schema.link_types.items()
                  if {link.get("from"), link.get("to")} & ORDER_SIDE
                  and {link.get("from"), link.get("to")} & SALES_SIDE)


def fed_order(workspace: Any, object_id: str) -> tuple[str | None, dict[str, Any]]:
    """The open customer order the impact chain (``IMPACT_FAMILY``, firm links only: the
    entity map's verified BC item and PO identity among them) ranks first among those this
    purchase order feeds, and what the walk said: (None, detail) when it reaches none."""
    from operating.impact_chain import ranked_orders
    try:
        investigated = workspace.investigate(IMPACT_FAMILY, object_id)
    except (LookupError, KeyError, ValueError) as error:
        return None, {"impact_refused": str(error.args[0] if error.args else error)[:200]}
    result = (investigated or {}).get("investigation") or {}
    ranked = [order for _po, order in ranked_orders(result.get("answer") or ())]
    detail: dict[str, Any] = {
        "orders_fed": len(ranked),
        "impact_stops": sorted({str(gap.get("reason")) for gap in result.get("gaps") or ()
                                if gap.get("reason")})[:10]}
    completeness = result.get("completeness") or {}
    if completeness.get("truncated") and completeness.get("reason") not in FITTED:
        # the walk itself was cut (a step budget): no order fed is then no fact of the data
        detail["impact_cut"] = str(completeness.get("reason") or "truncated")
    return (ranked[0] if ranked else None), detail


def margin_step(workspace: Any, object_id: str, *, feeds: Sequence[str],
                identify: bool) -> dict[str, Any]:
    """The margin of the customer order this purchase order feeds, firm links only: a
    direct link type that says so (``feeds``), else the open order the impact chain ranks
    first among those the purchase order feeds (``fed_order``). The margin is the
    ``order_margin`` answer for this reader, finance-gated (a reader without the marking
    gets ``margin_withheld``, never a figure). No order fed: a fail naming where the walk
    stopped; never an unrelated sample."""
    orders: set[str] = set()
    for link_type in feeds:
        for direction in ("forward", "reverse"):
            around = workspace._call("plx.around", {
                "from": object_id, "path": [{"link_type": link_type, "direction": direction}],
                "tier_floor": FIRM_FLOOR, "limit": PICK_LIMIT})
            orders |= {item["object_id"] for item in around.get("objects") or ()
                       if item.get("object_type") == "SalesOrder"}
    if len(orders) > 1:
        return step("fail", "sales_order_fed_ambiguous", sales_orders=len(orders))
    via: dict[str, Any] = {"fed_by": "link"}
    if not orders:
        order_id, chain = fed_order(workspace, object_id)
        via = {"fed_by": "impact_chain", **chain}
        if order_id is None:
            return step("fail", "no_sales_order_fed", **via)
        orders = {order_id}
    from scripts.flagship_margin import FAMILY, answer_step
    (order,) = orders
    try:
        investigated = workspace.investigate(FAMILY, order)
    except (LookupError, KeyError, ValueError) as error:
        return step("fail", f"investigate_refused:{error.args[0] if error.args else error}")
    out = answer_step(investigated, identify=identify)
    out.pop("schema", None)
    out.update(via)
    if identify:
        out["object_id"] = order
    return out


# ---- orders --------------------------------------------------------------------


def resolve_order(workspace: Any, order: str) -> tuple[str | None, str | None]:
    """(object id, number) of one ``--order`` (a plx object id or a number plx.find finds as
    a purchase order); (None, reason) when it is not found or ambiguous."""
    if order.startswith(f"{PURCHASE_ORDER}:"):
        return order, None
    found = workspace.find(order, object_type=PURCHASE_ORDER)
    candidates = [c for c in (found.get("find") or {}).get("candidates") or ()
                  if (c.get("object") or {}).get("object_type") == PURCHASE_ORDER]
    exact = [c for c in candidates if order.lower() in json.dumps(
        (c.get("object") or {}).get("display")).lower()]
    chosen = exact or candidates
    if not chosen:
        return None, "order_not_found"
    return chosen[0]["object"]["object_id"], None


def number_of(workspace: Any, object_id: str) -> str | None:
    card = workspace._call("plx.object", {"object_id": object_id})
    display = (card.get("object") or {}).get("display")
    if isinstance(display, Mapping):
        return str(display.get("untrusted_text") or "") or None
    return str(display) if display else None


def object_of(workspace: Any, group: Mapping[str, Any], records: Mapping[str, str]
              ) -> str | None:
    """The purchase-order object holding one of the late group's own header records: the
    one the home lists first, then the Business Central one, then any; None when the
    product holds none of them (found by number, matched by record, never by text)."""
    headers = {handle_base(h["citation"]): h["system"] for h in group["headers"]}
    for record, object_id in records.items():
        if record in headers:
            return object_id
    found: dict[str, str] = {}
    for number in group["numbers"]:
        listing = workspace.find(number, object_type=PURCHASE_ORDER)
        for candidate in ((listing.get("find") or {}).get("candidates") or ())[:PICK_LIMIT]:
            item = candidate.get("object") or {}
            if item.get("object_type") != PURCHASE_ORDER or item["object_id"] in found.values():
                continue
            record = own_record(workspace, item["object_id"])
            if record in headers:
                found[headers[record]] = item["object_id"]
    return found.get("bc") or found.get("portal") or next(iter(found.values()), None)


def pick_late(late: Sequence[Mapping[str, Any]], count: int) -> list[Mapping[str, Any]]:
    """``count`` late orders, deterministically: those Business Central holds first, then
    by the hash of their numbers (no order is chosen by value)."""
    return sorted(late, key=lambda g: (not g["bc"], hashlib.sha256(
        "/".join(g["numbers"]).encode()).hexdigest()))[:count]


def group_of(workspace: Any, object_id: str, late: Sequence[Mapping[str, Any]]
             ) -> Mapping[str, Any] | None:
    record = own_record(workspace, object_id)
    return next((g for g in late if record in {handle_base(h["citation"])
                                                for h in g["headers"]}), None)


# ---- the run -------------------------------------------------------------------


def run(workspace: Any, orders: Sequence[str], *, count: int, as_of: date, question: str,
        identify: bool, ask: bool = True, raw: Any = None,
        feeds: Sequence[str] | None = None, margin: bool = True,
        family: str | None = None) -> dict[str, Any]:
    """Every step for each order over ``workspace`` (an ``ObjectWorkspace``), scored
    against ``raw`` (default: the workspace reader's ``RawCaptures``)."""
    if raw is None:
        raw = RawCaptures(workspace.access.runtime, workspace.actor)
    feeds = feeds_links() if feeds is None else list(feeds)
    started = time.monotonic()
    oracle = lateness(raw, as_of)
    oracle_seconds = round(time.monotonic() - started, 1)
    started = time.monotonic()
    home = home_inbox(workspace, as_of)
    records = home_records(workspace, home)
    late = oracle["late"]
    summary: dict[str, Any] = {
        "oracle": {**oracle["counts"], "seconds": oracle_seconds},
        "home": {"seconds": round(time.monotonic() - started, 1),
                 "scanned": home.get("scanned"), "truncated": home.get("truncated"),
                 "gaps": len(home.get("gaps") or ()),
                 "gaps_on_purchase_orders": sum(
                     1 for gap in home.get("gaps") or ()
                     if gap.get("object_type") == PURCHASE_ORDER),
                 "recall": home_recall(late, records)}}
    chosen: list[tuple[str | None, str, str | None, Mapping[str, Any] | None]] = []
    if orders:
        for order in orders:
            object_id, refused = resolve_order(workspace, order)
            group = group_of(workspace, object_id, late) if object_id else None
            chosen.append((object_id, order, refused, group))
        summary["selected_by"] = "argument"
    else:
        for group in pick_late(late, count):
            object_id = object_of(workspace, group, records)
            chosen.append((object_id, "", None if object_id else "order_not_held", group))
        summary["selected_by"] = "lateness_model"
    rows = []
    for index, (object_id, given, refused, group) in enumerate(chosen, 1):
        row: dict[str, Any] = {"order": index}
        if identify:
            row["given"] = given or None
            row["numbers"] = list((group or {}).get("numbers") or ())
        if object_id is None:
            row["steps"] = {name: step("fail", refused or "order_not_found")
                            for name in STEPS}
            rows.append(row)
            continue
        if identify:
            row["object_id"], row["number"] = object_id, number_of(workspace, object_id)
        others = {handle_base(r) for g in oracle["groups"]
                  if group is None or g["headers"] != group["headers"]
                  for r in g["records"]}
        gaps = [gap for gap in home.get("gaps") or () if gap.get("object_id") == object_id]
        asked = (timed(ask_step, workspace, object_id, question, late=group, others=others,
                       confirm=raw.confirms, identify=identify, family=family) if ask
                 else step("blocked", "ask_not_requested"))
        row["steps"] = {
            "home": home_step(group, records, home),
            "ask": asked,
            "timing": timing_step(asked),
            "landed_cost": timed(landed_step, workspace, object_id, identify=identify),
            "draft": timed(draft_step, workspace, object_id, gaps, identify=identify),
            "margin": timed(margin_step, workspace, object_id, feeds=feeds,
                            identify=identify)}
        rows.append(row)
    summary["orders"] = rows
    summary["passed"] = {name: sum(1 for row in rows if row["steps"][name]["status"] == "pass")
                         for name in STEPS}
    summary["orders_passing_steps_1_3"] = sum(
        1 for row in rows if all(row["steps"][name]["status"] == "pass"
                                 for name in STEPS[:3]))
    summary["orders_all_steps"] = sum(
        1 for row in rows if all(row["steps"][name]["status"] == "pass" for name in STEPS))
    summary["blocked"] = sorted({row["steps"][name]["reason"] for row in rows
                                 for name in STEPS
                                 if row["steps"][name]["status"] == "blocked"})
    if margin:
        from scripts.flagship_margin import margin_sample
        summary["margin"] = timed(margin_sample, workspace, identify=identify)
    return summary


def workspace_for(state_dir: Path, mirror_audience: Path, tenant: str,
                  principals: Sequence[str], clearance: str, *, work_dir: Path,
                  planner: Any, live: Any = None,
                  markings: Sequence[str] = ()
                  ) -> tuple[Any, Callable[[], None], dict[str, Any]]:
    """The console's object workspace for one fixture reader (its ``markings``) over
    ``state_dir``'s projection, with its follow-up controls over a Workbench action store
    in ``work_dir``; its closer; and which projection it reads (``run_projection``'s
    summary: the host's served generation, read-only and pinned, when the directory has
    one; else a private offline build with a disk check)."""
    from operating import case_workspace_live as live_mod
    from operating.action_path import ActionPath
    from operating.session_identity import CHANNEL_CONSOLE, fixture_session
    from operator_ui import case_console

    runtime = live_mod.offline_runtime(state_dir, mirror_audience, tenant, live=live)
    chosen = None
    try:
        chosen = live_mod.run_projection(state_dir, tenant)
        projection = chosen.path
        session = fixture_session(tenant, principals, channel=CHANNEL_CONSOLE,
                                  clearance=clearance)
        work_dir.mkdir(parents=True, exist_ok=True)
        path = ActionPath(work_dir / case_console.ACTIONS_FILE, runtime.cases)
        held = {f"{kind}_capture_dir": state_dir for kind in ("mail", "sharepoint")
                if _store_exists(kind, state_dir)}
        workspace = case_console.object_workspace_for(
            runtime, projection, session.actor, state_dir=state_dir, action_path=path,
            session=session, planner=planner, evidence=dict(live_mod.MIRROR_EVIDENCE),
            markings=tuple(markings), **held)
    except BaseException:
        if chosen is not None:
            chosen.close()
        runtime.close()
        raise

    def close() -> None:
        try:
            runtime.close()
        finally:
            chosen.close()
    return workspace, close, chosen.summary()


def granted_markings(host_config: Path, principals: Sequence[str]) -> set[str]:
    """The markings a host config's ``marking_grants`` give a reader holding ``principals``
    (``operating.ontology_host.check_marking_grants``: an invalid grant raises ValueError);
    none when it grants none. Implied markings (a finance holder's purchasing_prices) are
    the read policy's, never added here."""
    from operating.ontology_host import check_marking_grants
    doc = json.loads(Path(host_config).read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping):
        raise ValueError("host_config_not_an_object")
    grants = check_marking_grants(doc.get("marking_grants"))
    return {marking for marking, holders in grants.items() if holders & set(principals)}


def _store_exists(kind: str, state_dir: Path) -> bool:
    from integrations import capture_mail, capture_sharepoint
    module = capture_mail if kind == "mail" else capture_sharepoint
    return bool(module.store_exists(state_dir))


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         planner: Any = None, host_options: Mapping[str, Any] | None = None) -> int:
    from operating import case_workspace_live as live

    parser = argparse.ArgumentParser(prog="python3 scripts/eval_flagship_path.py",
                                     description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--mirror-audience", required=True, type=Path)
    parser.add_argument("--tenant", default="plx")
    parser.add_argument("--principal", action="append", required=True,
                        help="the reader's principals (repeat)")
    parser.add_argument("--clearance", default="internal")
    parser.add_argument("--marking", action="append", default=[],
                        help="a marking the reader holds (repeat; e.g. finance)")
    parser.add_argument("--live-source-audience", type=Path, default=None,
                        help="the BC/mail/SharePoint grants (plx-live-source-audience/v1); "
                             "without it only the Portal mirror is read")
    parser.add_argument("--host-policy", type=Path, default=None,
                        help="a host config (plx-host-config/v1) whose marking_grants decide "
                             "the markings the reader's principals hold, as that host grants "
                             "them (no capture cycle; --host-config's are used without it)")
    parser.add_argument("--family", default=None,
                        help="the question family the Ask names (default: QUESTION_FAMILY "
                             "for the default question, none for another)")
    parser.add_argument("--host-config", type=Path, default=None,
                        help="run one host capture cycle into --state-dir first")
    parser.add_argument("--order", action="append", default=[],
                        help="a purchase order number or plx object id (repeat)")
    parser.add_argument("--orders", type=int, default=3,
                        help="how many late orders to pick without --order (default 3)")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--no-ask", action="store_true",
                        help="skip the Ask step (it reads blocked: ask_not_requested)")
    parser.add_argument("--work-dir", type=Path, default=None,
                        help="the Workbench case/action store directory (default "
                             "<state-dir>/flagship)")
    parser.add_argument("--identify", action="store_true",
                        help="also print order numbers, object ids, claims and drafts")
    parser.add_argument("--model", default=None, help="the Ask planner's model")
    args = parser.parse_args(argv)
    if not 1 <= args.orders <= MAX_ORDERS or len(args.order) > MAX_ORDERS:
        parser.error(f"at most {MAX_ORDERS} orders")
    env = os.environ if environ is None else environ
    as_of = args.as_of or po_lateness.business_today()   # the oracle's ET day
    policy = args.host_policy or args.host_config
    try:
        granted = granted_markings(policy, args.principal) if policy is not None else set()
    except (OSError, ValueError) as error:
        parser.error(f"host policy is invalid: {type(error).__name__}: {error}"[:200])
    markings = sorted(set(args.marking) | granted)
    family = args.family or (QUESTION_FAMILY if args.question == QUESTION else None)
    summary: dict[str, Any] = {"schema": SCHEMA, "evidence": live.MIRROR_EVIDENCE["label"],
                               "as_of": as_of.isoformat(), "question": args.question,
                               "capture": None, "sent": 0, "markings": markings,
                               "family": family}
    if args.host_config is not None:
        code, summary["capture"] = live._host_cycle(args, env, host_options or {})
        if code != 0:
            print(json.dumps(summary, indent=1, sort_keys=True))
            return 1
    summary["blocked_upstream"] = live.upstream_parts(live.portal_upstream(args.state_dir))
    if planner is None and not args.no_ask:
        if env.get("CLAUDE_CODE_OAUTH_TOKEN") or env.get("PLX_CLAUDE_CODE_OAUTH_TOKEN"):
            from operating.agent_loop import ClaudeCodePlanner
            planner = (ClaudeCodePlanner(model=args.model) if args.model
                       else ClaudeCodePlanner())
    readers_live = None
    if args.live_source_audience is not None:
        from operating.case_runtime import build_live_readers
        from operating.ingestion import IngestError, load_live_audience
        try:
            readers_live = build_live_readers(
                load_live_audience(args.live_source_audience, env), env,
                bc_sender=(host_options or {}).get("sender"))
        except IngestError as error:
            parser.error(f"live source audience is invalid: {error.code}")
    try:
        workspace, close, summary["projection"] = workspace_for(
            args.state_dir.resolve(), args.mirror_audience, args.tenant, args.principal,
            args.clearance, work_dir=(args.work_dir or args.state_dir / "flagship").resolve(),
            planner=planner, live=readers_live, markings=markings)
    except live.ProjectionDiskLow as error:
        summary["projection"] = {"source": "private", "reason": error.code,
                                 "free_bytes": error.free, "needed_bytes": error.needed}
        print(json.dumps(summary, indent=1, sort_keys=True))
        return 1
    try:
        summary.update(run(workspace, args.order, count=args.orders, as_of=as_of,
                           question=args.question, identify=args.identify,
                           ask=not args.no_ask, family=family))
    finally:
        close()
    print(json.dumps(summary, indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
