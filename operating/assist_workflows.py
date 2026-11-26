"""Assist workflows (PLAN-1124 "Assist = always-on agent" (b)): a person's own repeatable jobs.

A person creates workflows in three ways:
1. Plain language ("every Monday 8 AM send me late POs by supplier", "when mail from croda.com arrives,
   draft a reply", "when a PO goes late, chase the supplier", "when an MC question arrives, prepare the answer").
2. From their mined routines (``assist.routines`` on the Routines tab).
3. From starter templates (``TEMPLATES``).

Triggers:
* ``schedule``: hourly, daily, weekdays, weekly (named weekday) or monthly (first working day or day 1-28),
  evaluated in US Eastern (``America/New_York``).
* ``event``: ``mail_from`` (new mail in the person's own mailbox), ``teams_from`` (new readable Teams message
  addressed to the person), ``po_late`` (open non-private, non-excluded ``biz.alerts`` row with
  ``rule = 'po_late'``), ``mc_question`` (new open Mission Control question matter for this person),
  ``qc_hold`` (open non-private, non-excluded ``biz.alerts`` row with ``rule = 'lot_on_hold'``).
  Mission Control approvals stay in Mission Control itself and are not workflow triggers.
* ``manual``: on demand ("Run now"). Scheduled and event workflows can also be run on demand at any time.

Each fire appends a ``requested`` job to ``assist.job_log`` (migration 0082) so it runs through the exact
same ``operating.assist_job`` plan -> parallel pieces -> screens -> prepared-writes desk pipeline. Nothing is
sent or written without the person's two-step confirmation.

No caps on how many workflows, runs or pieces a person can have (business rule). To prevent a failing workflow
from spinning in a loop, consecutive failed runs back off exponentially (5 min up to 24 h) and auto-pause
after ``AUTO_PAUSE_FAILURES`` consecutive failures; resuming, editing or running on demand clears the streak.

Evidence: isolated-fixture (``operating/tests/test_assist_workflows.py``); live-unverified."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import uuid
from typing import Any, Mapping, Optional, Sequence

from operating import assist_job as aj
from operating.card_text import mask_served
from operating.recurring_instructions import EASTERN, WEEKDAYS

LOG = logging.getLogger(__name__)
TABLE = "assist.workflow_log"
WHATS = ("created", "updated", "paused", "resumed", "stopped", "run_now",
         "fired", "run_succeeded", "run_failed", "auto_paused")
PERSON_WHATS = ("created", "updated", "paused", "resumed", "stopped", "run_now")
TRIGGER_KINDS = ("schedule", "event", "manual")
CADENCES = ("hourly", "daily", "weekdays", "weekly", "monthly")
EVENT_KINDS = ("mail_from", "teams_from", "po_late", "mc_question", "qc_hold")
SOURCE_KINDS = ("plain_language", "routine", "template")
LIVE_JOB_STATES = frozenset({"queued", "running", "stopping"})
BACKOFF_BASE_SECONDS = 300          # 5 minutes after 1st consecutive failure
BACKOFF_MAX_SECONDS = 86400         # capped at 24 hours between retries
AUTO_PAUSE_FAILURES = 5             # auto-pause after 5 consecutive failures so a loop never spins

_DAY_ALIASES = {
    "mon": "monday", "monday": "monday", "mondays": "monday",
    "tue": "tuesday", "tues": "tuesday", "tuesday": "tuesday", "tuesdays": "tuesday",
    "wed": "wednesday", "weds": "wednesday", "wednesday": "wednesday", "wednesdays": "wednesday",
    "thu": "thursday", "thur": "thursday", "thurs": "thursday", "thursday": "thursday", "thursdays": "thursday",
    "fri": "friday", "friday": "friday", "fridays": "friday",
    "sat": "saturday", "saturday": "saturday", "saturdays": "saturday",
    "sun": "sunday", "sunday": "sunday", "sundays": "sunday",
}
_SHORT_TO_WEEKDAY = {"Mon": "monday", "Tue": "tuesday", "Wed": "wednesday", "Thu": "thursday",
                     "Fri": "friday", "Sat": "saturday", "Sun": "sunday"}

TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "template_id": "late-pos-weekly",
        "name": "Weekly late POs by supplier",
        "description": "Every Monday at 8:00 AM, group overdue purchase orders by supplier and draft follow-up emails.",
        "trigger_kind": "schedule",
        "trigger": {"cadence": "weekly", "weekday": "monday", "hour": 8, "minute": 0},
        "instruction": "List every open late purchase order grouped by supplier and draft a supplier chase email for each supplier with overdue POs.",
    },
    {
        "template_id": "daily-morning-brief",
        "name": "Weekday morning open-work brief",
        "description": "Every weekday at 8:00 AM, pull together what is waiting on you across mail, Teams and open tasks.",
        "trigger_kind": "schedule",
        "trigger": {"cadence": "weekdays", "hour": 8, "minute": 0},
        "instruction": "Summarize everything waiting on me today across mail, Teams, Mission Control questions and overdue orders, and prepare replies or follow-up actions.",
    },
    {
        "template_id": "mail-from-sender",
        "name": "Prepare a reply when mail arrives from a sender",
        "description": "Whenever a new email arrives from a sender or domain you name, read related records and draft a reply.",
        "trigger_kind": "event",
        "trigger": {"event": "mail_from", "sender": "", "subject": ""},
        "instruction": "Read the new email, look up any orders, invoices or threads it names, and prepare a reply and follow-up actions for my review.",
    },
    {
        "template_id": "teams-from-sender",
        "name": "Prepare a reply when a Teams message arrives from someone",
        "description": "Whenever a new Teams message addressed to you arrives from a person you name, read related records and draft a reply.",
        "trigger_kind": "event",
        "trigger": {"event": "teams_from", "sender": "", "subject": ""},
        "instruction": "Read the new Teams message and its thread, look up any records it names, and prepare a reply for my review.",
    },
    {
        "template_id": "po-goes-late",
        "name": "Draft a supplier chase when a PO goes late",
        "description": "Whenever an open purchase order goes past its requested date, check recent supplier mail and draft a chase.",
        "trigger_kind": "event",
        "trigger": {"event": "po_late", "supplier": ""},
        "instruction": "Check the late purchase order, its lines and any recent mail from the supplier, and draft a follow-up email asking for a confirmed ship date.",
    },
    {
        "template_id": "mc-question-prep",
        "name": "Prepare an answer when a Mission Control question arrives",
        "description": "Whenever a Mission Control question is addressed to you, read the task and records and draft the answer.",
        "trigger_kind": "event",
        "trigger": {"event": "mc_question"},
        "instruction": "Read the Mission Control question, its task and related records, and prepare a clear answer with the facts behind it for my review.",
    },
    {
        "template_id": "qc-hold-impact",
        "name": "Check order impact when a batch goes on QC hold",
        "description": "Whenever a batch goes on quality hold, list affected open sales orders and draft customer or team follow-ups.",
        "trigger_kind": "event",
        "trigger": {"event": "qc_hold", "product": ""},
        "instruction": "Check the batch on quality hold, find every affected open order and shipment, and prepare follow-up messages and actions for my review.",
    },
    {
        "template_id": "reconcile-supplier-invoices",
        "name": "Reconcile supplier invoices against POs",
        "description": "Run whenever you want to compare open supplier invoices against purchase orders and receipts.",
        "trigger_kind": "manual",
        "trigger": {},
        "instruction": "Reconcile open supplier invoices against their purchase orders and receipts, list every quantity or price mismatch, and prepare follow-up messages.",
    },
    {
        "template_id": "uninvoiced-shipments-check",
        "name": "Check shipped orders not yet invoiced",
        "description": "Every weekday at 8:30 AM, find shipped orders with no invoice posted yet and draft billing follow-ups.",
        "trigger_kind": "schedule",
        "trigger": {"cadence": "weekdays", "hour": 8, "minute": 30},
        "instruction": "Find every shipped customer order that does not have a matching posted invoice yet, list the order number, customer, ship date and dollar value, and prepare follow-up messages or billing steps.",
    },
    {
        "template_id": "qa-coa-followup",
        "name": "Follow up on missing QA Certificates of Analysis",
        "description": "Every weekday at 9:00 AM, check open batches and shipments waiting on a COA and draft follow-up emails.",
        "trigger_kind": "schedule",
        "trigger": {"cadence": "weekdays", "hour": 9, "minute": 0},
        "instruction": "Check open production batches, incoming material lots and upcoming shipments waiting on a Certificate of Analysis or lab sign-off, and draft follow-up emails to the supplier or QA contact.",
    },
    {
        "template_id": "customer-order-status-digest",
        "name": "Customer order status and backorder digest",
        "description": "Every weekday at 8:30 AM, summarize open customer orders, backorders and upcoming ship dates with update drafts.",
        "trigger_kind": "schedule",
        "trigger": {"cadence": "weekdays", "hour": 8, "minute": 30},
        "instruction": "Summarize open customer orders, backorders and orders shipping in the next 5 business days with order numbers, promised dates and open balances, and draft customer status updates where dates moved.",
    },
    {
        "template_id": "ap-ar-overdue-review",
        "name": "Weekly AP and AR overdue review",
        "description": "Every Monday at 9:00 AM, review past-due receivables and overdue payables and prepare follow-up messages.",
        "trigger_kind": "schedule",
        "trigger": {"cadence": "weekly", "weekday": "monday", "hour": 9, "minute": 0},
        "instruction": "List overdue customer invoices and past-due supplier bills grouped by account with invoice numbers, due dates and balances, and prepare payment follow-up drafts for my review.",
    },
)


def new_workflow_id() -> str:
    return uuid.uuid4().hex[:24]


def is_missing_table(exc: BaseException) -> bool:
    return getattr(exc, "sqlstate", None) == "42P01" or "does not exist" in str(exc).lower()


def template_by_id(template_id: str) -> Optional[dict[str, Any]]:
    for t in TEMPLATES:
        if t["template_id"] == template_id:
            return dict(t)
    return None


def backoff_seconds(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 0
    return int(min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** (consecutive_failures - 1))))


# ----------------------------------------------------------------------------- plain-language parser

def _parse_clock(text: str) -> tuple[int, int, str]:
    """Extract an optional leading clock time ('at 8 AM', '8:30 pm', '14:00') from ``text``."""
    m = re.match(r"^\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b\s*[,;:-]?\s*", text, re.I)
    if not m:
        return 8, 0, text
    raw_h, raw_m, mer = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if not mer and not m.group(2) and not re.match(r"^\s*at\b", text, re.I):
        return 8, 0, text        # bare number without 'at' or am/pm is probably part of the instruction
    if mer == "pm" and raw_h < 12:
        raw_h += 12
    elif mer == "am" and raw_h == 12:
        raw_h = 0
    if not (0 <= raw_h <= 23 and 0 <= raw_m <= 59):
        return 8, 0, text
    return raw_h, raw_m, text[m.end():]


def _clean_instruction(rest: str, fallback: str) -> str:
    cleaned = rest.lstrip(" ,;:-").strip()
    if not cleaned:
        return fallback.strip()
    return cleaned[0].upper() + cleaned[1:]


def _stitch_instruction(before: str, after: str, fallback: str) -> str:
    """Combine text before and after an extracted trigger phrase into a clean instruction."""
    left = re.sub(r"[\s,;:-]+$", "", before or "").strip()
    right = re.sub(r"^[\s,;:-]+", "", after or "").strip()
    if left and right:
        combined = f"{left} {right}"
    else:
        combined = left or right
    return _clean_instruction(combined, fallback)


def parse_plain_language(text: str) -> dict[str, Any]:
    """Parse a user's plain-language workflow description into a structured trigger, instruction and name.
    Matches schedule phrases and event triggers anywhere in the prompt (leading, mid-sentence, or
    trailing), so the form shows the parsed trigger for review before saving."""
    raw = " ".join(str(text or "").split())
    if not raw:
        raise ValueError("Say what the workflow should do.")

    # 1. Event: new Teams message from X [about Y], or "when X messages on Teams"
    # guard-ok: explicit user-typed workflow trigger syntax, confirmed in UI form
    m_teams = re.search(
        r"\b(?:when|whenever|if)\s+(?:a\s+)?(?:new\s+)?teams\s+(?:message\s+|chat\s+)?(?:arrives\s+)?from\s+"
        r"([^\s,;]+(?:\s+(?!about\b|with\s+subject\b|arrives\b|comes\s+in\b)[^\s,;]+)*)"
        r"(?:\s+(?:about|with\s+subject)\s+([^,;]+?))?"
        r"(?:\s+(?:arrives|comes\s+in))?(?=\s*[,;:-]|\s*$)",
        raw, re.I,
    )
    if not m_teams:
        m_teams = re.search(
            r"\b(?:when|whenever|if)\s+([^\s,;]+(?:\s+(?!messages?\b|pings?\b|writes?\b|chats?\b)[^\s,;]+){0,2})\s+"
            r"(?:messages?|pings?|writes?|chats?)\s+(?:me\s+)?(?:on|in)\s+teams"
            r"(?:\s+(?:about|with\s+subject)\s+([^,;]+?))?(?=\s*[,;:-]|\s*$)",
            raw, re.I,
        )
    if m_teams:
        sender = m_teams.group(1).strip().rstrip(".,;:")
        subject = (m_teams.group(2) or "").strip()
        instr = _stitch_instruction(raw[:m_teams.start()], raw[m_teams.end():], raw)
        trig: dict[str, Any] = {"event": "teams_from", "sender": sender}
        if subject:
            trig["subject"] = subject
        return {"name": f"When a Teams message arrives from {sender}", "trigger_kind": "event", "trigger": trig,
                "instruction": instr, "trigger_summary": describe_trigger("event", trig)}

    # 1b. Event: new mail from X [about Y], or "when Croda emails [about Y]"
    # guard-ok: explicit user-typed workflow trigger syntax, confirmed in UI form
    m_mail = re.search(
        r"\b(?:when|whenever|if)\s+(?:a\s+)?(?:new\s+)?(?:mail|email|message)\s+(?:arrives\s+)?from\s+"
        r"([^\s,;]+(?:\s+(?!about\b|with\s+subject\b|arrives\b|comes\s+in\b)[^\s,;]+)*)"
        r"(?:\s+(?:about|with\s+subject)\s+([^,;]+?))?"
        r"(?:\s+(?:arrives|comes\s+in))?(?=\s*[,;:-]|\s*$)",
        raw, re.I,
    )
    if not m_mail:
        m_mail = re.search(
            r"\b(?:when|whenever|if)\s+([^\s,;]+(?:\s+(?!e-?mails?\b|sends?\b)[^\s,;]+){0,2})\s+"
            r"(?:e-?mails?(?:\s+me|\s+us)?|sends?\s+(?:an?\s+)?e-?mail)"
            r"(?:\s+(?:about|with\s+subject)\s+([^,;]+?))?(?=\s*[,;:-]|\s*$)",
            raw, re.I,
        )
    if m_mail:
        sender = m_mail.group(1).strip().rstrip(".,;:")
        subject = (m_mail.group(2) or "").strip()
        instr = _stitch_instruction(raw[:m_mail.start()], raw[m_mail.end():], raw)
        trig = {"event": "mail_from", "sender": sender}
        if subject:
            trig["subject"] = subject
        return {"name": f"When mail arrives from {sender}", "trigger_kind": "event", "trigger": trig,
                "instruction": instr, "trigger_summary": describe_trigger("event", trig)}

    # 2. Event: PO goes late (anywhere in the prompt)
    # guard-ok: explicit user-typed workflow trigger syntax, confirmed in UI form
    m_po = re.search(
        r"\b(?:when|whenever|if)\s+(?:a|any)\s+(?:po|purchase\s+order)\s+(?:from\s+([^,;]+?)\s+)?"
        r"(?:goes|is|becomes)\s+(?:late|overdue|past\s+due)(?=\s*[,;:-]|\s*$)",
        raw, re.I,
    )
    if m_po:
        supplier = (m_po.group(1) or "").strip()
        instr = _stitch_instruction(raw[:m_po.start()], raw[m_po.end():], raw)
        trig = {"event": "po_late"}
        if supplier:
            trig["supplier"] = supplier
        label = f"When a PO from {supplier} goes late" if supplier else "When a PO goes late"
        return {"name": label, "trigger_kind": "event", "trigger": trig,
                "instruction": instr, "trigger_summary": describe_trigger("event", trig)}

    # 3. Event: MC question arrives (anywhere in the prompt)
    # guard-ok: explicit user-typed workflow trigger syntax, confirmed in UI form
    m_mc = re.search(
        r"\b(?:when|whenever|if)\s+(?:an?\s+)?(?:mc|mission\s+control)\s+question\s+"
        r"(?:arrives|is\s+asked|comes\s+in)(?=\s*[,;:-]|\s*$)",
        raw, re.I,
    )
    if m_mc:
        instr = _stitch_instruction(raw[:m_mc.start()], raw[m_mc.end():], raw)
        trig = {"event": "mc_question"}
        return {"name": "When a Mission Control question arrives", "trigger_kind": "event", "trigger": trig,
                "instruction": instr, "trigger_summary": describe_trigger("event", trig)}

    # 3b. Event: QC hold (anywhere in the prompt, including "watch for any QC hold")
    # guard-ok: explicit user-typed workflow trigger syntax, confirmed in UI form
    m_qc = re.search(
        r"\b(?:when|whenever|if|watch\s+for|monitor\s+for|alert(?:\s+me)?\s+(?:on|for))\s+(?:a|any|new)?\s*"
        r"(?:(?:qc|quality)\s+holds?(?:\s+(?:on|for)\s+([^,;]+?))?(?:\s+(?:arrives?|happens?|occurs?|is\s+placed|is\s+opened))?|"
        r"(?:batch|lot)(?:\s+of\s+([^,;]+?))?\s+(?:goes|is\s+put)\s+on\s+(?:qc|quality)\s+hold)"
        r"(?=\s*(?:[,;:-]|\band\b|\bthen\b)|\s*$)",
        raw, re.I,
    )
    if m_qc:
        product = (m_qc.group(1) or m_qc.group(2) or "").strip()
        instr = _stitch_instruction(raw[:m_qc.start()], raw[m_qc.end():], raw)
        trig = {"event": "qc_hold"}
        if product:
            trig["product"] = product
        label = f"When a QC hold occurs on {product}" if product else "When a batch goes on QC hold"
        return {"name": label, "trigger_kind": "event", "trigger": trig,
                "instruction": instr, "trigger_summary": describe_trigger("event", trig)}

    # 4. Schedule: hourly / every N hours (anywhere in the prompt)
    m_hr = re.search(r"\b(?:hourly|every\s+(?:(\d+)\s+)?hours?)\b", raw, re.I)
    if m_hr:
        every_h = max(1, int(m_hr.group(1) or 1))
        instr = _stitch_instruction(raw[:m_hr.start()], raw[m_hr.end():], raw)
        trig = {"cadence": "hourly", "every_hours": every_h}
        return {"name": f"{describe_trigger('schedule', trig)}: {instr}", "trigger_kind": "schedule",
                "trigger": trig, "instruction": instr, "trigger_summary": describe_trigger("schedule", trig)}

    # 5. Schedule: prefer explicit multi-word schedule phrases first ("weekly on friday",
    #    "every monday", "every weekday", "every morning", ...) before bare cadence words
    #    ("daily", "weekdays", "weekly", "monthly") so e.g. "Send weekly scorecard weekly on
    #    friday at 4pm" binds to "weekly on friday at 4pm" instead of the adjective "weekly".
    # guard-ok: explicit user-typed schedule syntax, confirmed in UI form
    _DAY_PAT = (
        r"mon(?:day)?s?|tue(?:s|sday)?s?|wed(?:s|nesday)?s?|"
        r"thu(?:r|rs|rsday)?s?|fri(?:day)?s?|sat(?:urday)?s?|sun(?:day)?s?"
    )
    m_sched = re.search(
        r"\b(?:"
        r"weekly\s+on\s+(" + _DAY_PAT + r")|"
        r"every\s+(working\s+day|weekdays?|morning|day|week|month|" + _DAY_PAT + r")"
        r")\b",
        raw, re.I,
    )
    if not m_sched:
        m_sched = re.search(r"\b(daily|weekdays|weekly|monthly)\b", raw, re.I)
    if m_sched:
        token = next((g for g in m_sched.groups() if g), "").lower().strip()
        before = raw[:m_sched.start()]
        after = raw[m_sched.end():]
        day = 1
        if token in ("month", "monthly"):
            m_dom = re.match(r"^\s*(?:on\s+(?:the\s+|day\s+)?)?(\d{1,2})(?:st|nd|rd|th)?\b(.*)$", after, re.I)
            if m_dom and 1 <= int(m_dom.group(1)) <= 28:
                day = int(m_dom.group(1))
                after = m_dom.group(2) or ""
        hour, minute, rest_after = _parse_clock(after)
        # Also check for a clock time right before the schedule token (e.g. "at 8am every weekday")
        if rest_after == after and before:
            m_pre_clock = re.search(
                r"\s*\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$",
                before, re.I,
            )
            if m_pre_clock:
                ph, pm, _ = _parse_clock(m_pre_clock.group(0))
                hour, minute = ph, pm
                before = before[:m_pre_clock.start()]
        instr = _stitch_instruction(before, rest_after, raw)
        if token in _DAY_ALIASES:
            trig = {"cadence": "weekly", "weekday": _DAY_ALIASES[token], "hour": hour, "minute": minute}
        elif token in ("weekday", "weekdays", "working day"):
            trig = {"cadence": "weekdays", "hour": hour, "minute": minute}
        elif token in ("day", "daily", "morning"):
            trig = {"cadence": "daily", "hour": hour, "minute": minute}
        elif token in ("week", "weekly"):
            trig = {"cadence": "weekly", "weekday": "monday", "hour": hour, "minute": minute}
        else:
            trig = {"cadence": "monthly", "day": day, "hour": hour, "minute": minute}
        summary = describe_trigger("schedule", trig)
        return {"name": f"{summary}: {instr}", "trigger_kind": "schedule", "trigger": trig,
                "instruction": instr, "trigger_summary": summary}

    # 6. On demand fallback (no schedule or event phrase recognized)
    return {"name": raw, "trigger_kind": "manual", "trigger": {}, "instruction": raw,
            "trigger_summary": describe_trigger("manual", {})}


def describe_trigger(trigger_kind: str, trigger: Mapping[str, Any]) -> str:
    if trigger_kind == "manual":
        return "On demand"
    if trigger_kind == "schedule":
        cadence = str(trigger.get("cadence") or "daily")
        if cadence == "hourly":
            n = int(trigger.get("every_hours") or 1)
            return "Every hour" if n == 1 else f"Every {n} hours"
        hour = int(trigger.get("hour", 8))
        minute = int(trigger.get("minute", 0))
        h12 = hour % 12 or 12
        mer = "AM" if hour < 12 else "PM"
        clock = f"{h12}:{minute:02d} {mer} ET"
        if cadence == "weekly":
            wd = str(trigger.get("weekday") or "monday").capitalize()
            return f"Every {wd} at {clock}"
        if cadence == "weekdays":
            return f"Every weekday at {clock}"
        if cadence == "monthly":
            day = int(trigger.get("day") or 1)
            return f"Monthly (day {day}) at {clock}"
        return f"Every day at {clock}"
    if trigger_kind == "event":
        ev = str(trigger.get("event") or "")
        if ev == "mail_from":
            sender = str(trigger.get("sender") or "").strip()
            subj = str(trigger.get("subject") or "").strip()
            parts = []
            if sender:
                parts.append(f"from {sender}")
            if subj:
                parts.append(f"about \"{subj}\"")
            return "When mail arrives " + " ".join(parts) if parts else "When matching mail arrives"
        if ev == "teams_from":
            sender = str(trigger.get("sender") or "").strip()
            subj = str(trigger.get("subject") or "").strip()
            parts = []
            if sender:
                parts.append(f"from {sender}")
            if subj:
                parts.append(f"about \"{subj}\"")
            return "When a Teams message arrives " + " ".join(parts) if parts else "When a matching Teams message arrives"
        if ev == "po_late":
            sup = str(trigger.get("supplier") or "").strip()
            return f"When a PO from {sup} goes late" if sup else "When any PO goes late"
        if ev == "mc_question":
            return "When a Mission Control question arrives"
        if ev == "qc_hold":
            prod = str(trigger.get("product") or "").strip()
            return f"When a batch of {prod} goes on QC hold" if prod else "When any batch goes on QC hold"
    return "Custom trigger"


def validate_trigger(trigger_kind: str, trigger: Mapping[str, Any]) -> dict[str, Any]:
    if trigger_kind not in TRIGGER_KINDS:
        raise ValueError(f"Trigger kind must be one of {', '.join(TRIGGER_KINDS)}.")
    if trigger_kind == "manual":
        return {}
    if trigger_kind == "schedule":
        cadence = str(trigger.get("cadence") or "").strip()
        if cadence not in CADENCES:
            raise ValueError(f"Schedule cadence must be one of {', '.join(CADENCES)}.")
        if cadence == "hourly":
            every_h = int(trigger.get("every_hours") or 1)
            if every_h < 1 or every_h > 168:
                raise ValueError("Hourly interval must be between 1 and 168 hours.")
            return {"cadence": "hourly", "every_hours": every_h}
        hour = int(trigger.get("hour", 8))
        minute = int(trigger.get("minute", 0))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("Schedule hour must be 0-23 and minute 0-59.")
        out: dict[str, Any] = {"cadence": cadence, "hour": hour, "minute": minute}
        if cadence == "weekly":
            wd = str(trigger.get("weekday") or "").lower()
            if wd not in WEEKDAYS:
                raise ValueError("Weekly schedule needs a weekday (monday through sunday).")
            out["weekday"] = wd
        elif cadence == "monthly":
            day = int(trigger.get("day") or 1)
            if not (1 <= day <= 28):
                raise ValueError("Monthly day must be between 1 and 28.")
            out["day"] = day
        return out
    # event
    ev = str(trigger.get("event") or "").strip()
    if ev not in EVENT_KINDS:
        raise ValueError(f"Event trigger must be one of {', '.join(EVENT_KINDS)}.")
    if ev in ("mail_from", "teams_from"):
        sender = str(trigger.get("sender") or "").strip()
        subject = str(trigger.get("subject") or "").strip()
        if not sender and not subject:
            kind_name = "mail" if ev == "mail_from" else "Teams"
            raise ValueError(f"A {kind_name} event workflow must name a sender or subject filter.")
        out = {"event": ev, "sender": sender}
        if subject:
            out["subject"] = subject
        return out
    if ev == "po_late":
        supplier = str(trigger.get("supplier") or "").strip()
        out = {"event": "po_late"}
        if supplier:
            out["supplier"] = supplier
        return out
    if ev == "qc_hold":
        product = str(trigger.get("product") or "").strip()
        out = {"event": "qc_hold"}
        if product:
            out["product"] = product
        return out
    return {"event": ev}


def from_routine(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build workflow fields from a mined ``assist.routines`` row."""
    kind = str(row.get("kind") or "routine").replace("_", " ")
    trig_raw = row.get("trigger") or {}
    desc = str(row.get("description") or "").strip()
    relation = str(trig_raw.get("relation") or "")
    if relation == "clock" and trig_raw.get("weekday") in _SHORT_TO_WEEKDAY:
        wd = _SHORT_TO_WEEKDAY[str(trig_raw["weekday"])]
        trigger_kind = "schedule"
        trigger: dict[str, Any] = {"cadence": "weekly", "weekday": wd, "hour": 8, "minute": 0}
        name = f"Weekly {kind} ({wd.capitalize()})"
        instruction = desc or f"Prepare my regular {kind} work for this {wd.capitalize()}, reading open records and drafting any needed messages or entries."
    else:
        prior = str(trig_raw.get("event") or "").split(":", 1)[-1].replace("_", " ")
        trigger_kind = "manual"
        trigger = {}
        name = f"Routine: {kind}" + (f" after {prior}" if prior else "")
        instruction = desc or (f"Prepare my usual {kind} step after {prior}, checking the relevant records and drafting changes for my approval."
                               if prior else f"Prepare my usual {kind} work for my approval.")
    return {"name": name, "instruction": instruction, "trigger_kind": trigger_kind, "trigger": trigger,
            "source_kind": "routine", "source_ref": str(row.get("routine_id") or "")}


# ----------------------------------------------------------------------------- schedule math

def _as_utc(at: dt.datetime) -> dt.datetime:
    return at.astimezone(dt.timezone.utc) if at.tzinfo else at.replace(tzinfo=dt.timezone.utc)


def _matches_day(trigger: Mapping[str, Any], day: dt.date) -> bool:
    cadence = trigger.get("cadence")
    if cadence == "daily":
        return True
    if cadence == "weekdays":
        return day.weekday() < 5
    if cadence == "weekly":
        return WEEKDAYS[day.weekday()] == trigger.get("weekday", "monday")
    if cadence == "monthly":
        target = int(trigger.get("day") or 1)
        if target == 1:
            first = day.replace(day=1)
            while first.weekday() >= 5:
                first += dt.timedelta(days=1)
            return day == first
        return day.day == target
    return False


def next_due(trigger_kind: str, trigger: Mapping[str, Any], after: dt.datetime) -> Optional[dt.datetime]:
    """First scheduled run strictly after ``after`` (UTC); None for non-schedule workflows.
    Every cadence (including ``hourly`` / ``every_hours``) aligns to US Eastern (``America/New_York``)."""
    if trigger_kind != "schedule":
        return None
    after_utc = _as_utc(after)
    cadence = trigger.get("cadence")
    if cadence == "hourly":
        every = max(1, int(trigger.get("every_hours") or 1))
        # Step hour-by-hour in UTC (monotonic across DST) and check local US Eastern hour
        cur = after_utc.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        for _ in range(max(48, every * 2 + 24)):
            local_h = cur.astimezone(EASTERN).hour
            if local_h % every == 0:
                return cur
            cur += dt.timedelta(hours=1)
        return None
    hour, minute = int(trigger.get("hour", 8)), int(trigger.get("minute", 0))
    local = after_utc.astimezone(EASTERN)
    day = local.date()
    for _ in range(400):
        candidate = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)
        if candidate > local and _matches_day(trigger, day):
            return candidate.astimezone(dt.timezone.utc)
        day += dt.timedelta(days=1)
    return None


def due_slot(trigger: Mapping[str, Any], active_since: dt.datetime, now: dt.datetime) -> Optional[dt.datetime]:
    """Latest scheduled slot in ``(active_since, now]`` (UTC), aligned to US Eastern, or None."""
    since_utc, now_utc = _as_utc(active_since), _as_utc(now)
    if now_utc <= since_utc:
        return None
    cadence = trigger.get("cadence")
    if cadence == "hourly":
        every = max(1, int(trigger.get("every_hours") or 1))
        cur = now_utc.replace(minute=0, second=0, microsecond=0)
        while cur > since_utc:
            if cur <= now_utc and cur.astimezone(EASTERN).hour % every == 0:
                return cur
            cur -= dt.timedelta(hours=1)
        return None
    hour, minute = int(trigger.get("hour", 8)), int(trigger.get("minute", 0))
    local_now = now_utc.astimezone(EASTERN)
    local_since = since_utc.astimezone(EASTERN)
    day = local_now.date()
    earliest = local_since.date() - dt.timedelta(days=1)
    while day >= earliest:
        if _matches_day(trigger, day):
            slot = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=EASTERN)
            if local_since < slot <= local_now:
                return slot.astimezone(dt.timezone.utc)
        day -= dt.timedelta(days=1)
    return None


# ----------------------------------------------------------------------------- append-only log & fold

def append(pg: Any, workflow_id: str, person: str, what: str, detail: Optional[Mapping[str, Any]] = None) -> None:
    if what not in WHATS:
        raise ValueError(f"unknown workflow log row {what!r}")
    pg.execute(
        f"INSERT INTO {TABLE} (workflow_id, person, what, detail) VALUES (%s, %s, %s, %s)",
        (workflow_id, person, what, json.dumps(dict(detail or {}), default=str)),
    )
    if not getattr(pg, "autocommit", True):
        pg.commit()


def create(
    pg: Any,
    person: str,
    *,
    name: str,
    instruction: str,
    trigger_kind: str,
    trigger: Mapping[str, Any],
    source_kind: str = "plain_language",
    source_ref: str = "",
) -> str:
    """Create a workflow (append one ``created`` row). No per-person count cap."""
    clean_name = mask_served(" ".join(str(name or "").split()))
    clean_instr = mask_served(" ".join(str(instruction or "").split()))
    if not clean_instr:
        raise ValueError("A workflow needs an instruction saying what Assist should prepare.")
    if len(clean_instr) > aj.TEXT_CHARS:
        raise ValueError(f"A workflow instruction is at most {aj.TEXT_CHARS} characters.")
    if not clean_name:
        clean_name = clean_instr
    if source_kind not in SOURCE_KINDS:
        raise ValueError(f"source_kind must be one of {', '.join(SOURCE_KINDS)}.")
    clean_trigger = validate_trigger(trigger_kind, trigger)
    wid = new_workflow_id()
    append(pg, wid, person, "created", {
        "name": clean_name, "instruction": clean_instr,
        "trigger_kind": trigger_kind, "trigger": clean_trigger,
        "source_kind": source_kind, "source_ref": str(source_ref or ""),
    })
    return wid


def rows_of(pg: Any, workflow_id: str) -> list[dict[str, Any]]:
    cur = pg.execute(
        f"SELECT seq, workflow_id, person, at, what, detail FROM {TABLE} WHERE workflow_id = %s ORDER BY seq",
        (workflow_id,),
    )
    return [dict(zip(("seq", "workflow_id", "person", "at", "what", "detail"), r)) for r in cur.fetchall()]


def workflows_of(pg: Any, person_ids: Sequence[str], *, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Every workflow of the given person(s), newest first. No cap. Excludes deleted workflows by default."""
    ids = [r[0] for r in pg.execute(
        f"SELECT workflow_id FROM {TABLE} WHERE what = 'created' AND person = ANY(%s) ORDER BY seq DESC",
        (list(person_ids),),
    ).fetchall()]
    if not ids:
        return []
    cur = pg.execute(
        f"SELECT seq, workflow_id, person, at, what, detail FROM {TABLE} WHERE workflow_id = ANY(%s) ORDER BY seq",
        (ids,),
    )
    by: dict[str, list[dict[str, Any]]] = {i: [] for i in ids}
    for r in cur.fetchall():
        by[r[1]].append(dict(zip(("seq", "workflow_id", "person", "at", "what", "detail"), r)))
    folded = [fold(by[i]) for i in ids if by[i]]
    return folded if include_deleted else [w for w in folded if not w.get("deleted")]


def all_workflows(pg: Any) -> list[dict[str, Any]]:
    cur = pg.execute(f"SELECT seq, workflow_id, person, at, what, detail FROM {TABLE} ORDER BY seq")
    by: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in cur.fetchall():
        row = dict(zip(("seq", "workflow_id", "person", "at", "what", "detail"), r))
        wid = row["workflow_id"]
        if wid not in by:
            by[wid] = []
            order.append(wid)
        by[wid].append(row)
    return [fold(by[i]) for i in order if any(x["what"] == "created" for x in by[i])]


def _parse_ts(v: Any) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return _as_utc(v)
    if isinstance(v, str) and v:
        try:
            return _as_utc(dt.datetime.fromisoformat(v))
        except ValueError:
            return None
    return None


def fold(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold append-only ``assist.workflow_log`` rows into one workflow's current state."""
    first = next((r for r in rows if r["what"] == "created"), None)
    if first is None:
        raise ValueError("a workflow has no created row")
    d0 = first["detail"] or {}
    created_at = _parse_ts(first["at"]) or dt.datetime.now(dt.timezone.utc)
    out: dict[str, Any] = {
        "workflow_id": first["workflow_id"],
        "person": first["person"],
        "name": str(d0.get("name") or ""),
        "instruction": str(d0.get("instruction") or ""),
        "trigger_kind": str(d0.get("trigger_kind") or "manual"),
        "trigger": dict(d0.get("trigger") or {}),
        "source_kind": str(d0.get("source_kind") or "plain_language"),
        "source_ref": str(d0.get("source_ref") or ""),
        "status": "active",
        "deleted": False,
        "pause_reason": "",
        "created_at": created_at,
        "updated_at": created_at,
        "active_since": created_at,
        "runs": [],
        "fired_keys": set(),
        "reconciled_jobs": set(),
        "consecutive_failures": 0,
        "backoff_until": None,
        "last_error": "",
        "last_fired_at": None,
    }
    for r in rows:
        w = r["what"]
        d = r["detail"] or {}
        at = _parse_ts(r["at"]) or created_at
        if w == "updated":
            for k in ("name", "instruction", "trigger_kind"):
                if k in d and d[k] is not None:
                    out[k] = str(d[k])
            if "trigger" in d and isinstance(d["trigger"], Mapping):
                out["trigger"] = dict(d["trigger"])
            out["updated_at"] = at
            out["active_since"] = at
            out["consecutive_failures"] = 0
            out["backoff_until"] = None
            out["last_error"] = ""
            if out["status"] == "paused" and out["pause_reason"] == "paused_after_failures":
                out["pause_reason"] = ""
        elif w == "paused":
            if out["status"] != "stopped":
                out["status"] = "paused"
                out["pause_reason"] = str(d.get("reason") or "paused_by_person")
                out["updated_at"] = at
        elif w == "auto_paused":
            if out["status"] == "active":
                out["status"] = "paused"
                out["pause_reason"] = str(d.get("reason") or "paused_after_failures")
                out["last_error"] = str(d.get("message") or out["last_error"])
                out["updated_at"] = at
        elif w == "resumed":
            if out["status"] != "stopped":
                out["status"] = "active"
                out["pause_reason"] = ""
                out["active_since"] = at
                out["updated_at"] = at
                out["consecutive_failures"] = 0
                out["backoff_until"] = None
                out["last_error"] = ""
        elif w == "stopped":
            out["status"] = "stopped"
            if d.get("deleted") is True:
                out["deleted"] = True
                out["pause_reason"] = "deleted_by_person"
            elif not out["deleted"]:
                out["pause_reason"] = "stopped_by_person"
            out["updated_at"] = at
        elif w in ("fired", "run_now"):
            jid = str(d.get("job_id") or "")
            dk = str(d.get("dedupe_key") or "")
            if dk:
                out["fired_keys"].add(dk)
            out["last_fired_at"] = at
            if w == "run_now":
                out["consecutive_failures"] = 0
                out["backoff_until"] = None
            if jid:
                out["runs"].append({"job_id": jid, "at": at, "kind": w,
                                    "trigger_note": str(d.get("trigger_note") or ""), "dedupe_key": dk})
        elif w == "run_succeeded":
            jid = str(d.get("job_id") or "")
            if jid:
                out["reconciled_jobs"].add(jid)
            out["consecutive_failures"] = 0
            out["backoff_until"] = None
            out["last_error"] = ""
        elif w == "run_failed":
            jid = str(d.get("job_id") or "")
            if jid:
                out["reconciled_jobs"].add(jid)
            out["consecutive_failures"] = int(d.get("consecutive_failures") or (out["consecutive_failures"] + 1))
            out["backoff_until"] = _parse_ts(d.get("backoff_until"))
            out["last_error"] = str(d.get("message") or d.get("reason") or "The last run failed.")
    out["runs"].reverse()          # newest first
    out["trigger_summary"] = describe_trigger(out["trigger_kind"], out["trigger"])
    return out


def routines_of(pg: Any, person_ids: Sequence[str]) -> list[dict[str, Any]]:
    """The person's mined routines from ``assist.routines`` (ordered by mined acts desc, no cap)."""
    try:
        cur = pg.execute(
            "SELECT routine_id, scope, kind, trigger, description, mined, validated, status "
            "FROM assist.routines WHERE scope = ANY(%s) ORDER BY coalesce((mined->>'acts')::int, 0) DESC, routine_id",
            (list(person_ids),),
        )
    except Exception as exc:  # noqa: BLE001 - missing table or no grant: surface cleanly
        if is_missing_table(exc) or getattr(exc, "sqlstate", None) == "42501":
            LOG.info("assist workflows: assist.routines unavailable (%s)", exc)
            return []
        raise
    cols = ("routine_id", "scope", "kind", "trigger", "description", "mined", "validated", "status")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _tx(pg: Any) -> Any:
    """Wrap multi-statement workflow operations in one atomic transaction on real psycopg connections."""
    tx = getattr(pg, "transaction", None)
    return tx() if callable(tx) else nullcontext()


WF_LOCK_NS = 41051   # two-arg pg_advisory_xact_lock(ns, key) namespace (objsubid=2, disjoint from 1-arg locks)


def wf_lock_key(workflow_id: str) -> tuple[int, int]:
    """Two-arg advisory lock key ``(ns, key)`` disjoint from 1-arg ``assist_job.lock_key``."""
    return (WF_LOCK_NS, int(workflow_id, 16) & 0x7FFFFFFF)


def _lock_workflow(pg: Any, workflow_id: str) -> None:
    if callable(getattr(pg, "transaction", None)):
        pg.execute("SELECT pg_advisory_xact_lock(%s, %s)", wf_lock_key(workflow_id))


def _like_contains(value: str) -> str:
    """Escape LIKE wildcards (``\\``, ``%``, ``_``) before wrapping in ``%...%`` for ``ESCAPE '\\'``."""
    escaped = value.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _insert_wf(pg: Any, workflow_id: str, person: str, what: str, detail: Optional[Mapping[str, Any]] = None) -> None:
    if what not in WHATS:
        raise ValueError(f"unknown workflow log row {what!r}")
    pg.execute(
        f"INSERT INTO {TABLE} (workflow_id, person, what, detail) VALUES (%s, %s, %s, %s)",
        (workflow_id, person, what, json.dumps(dict(detail or {}), default=str)),
    )


def _insert_job(pg: Any, job_id: str, person: str, what: str, detail: Optional[Mapping[str, Any]] = None) -> None:
    if what not in aj.WHATS:
        raise ValueError(f"unknown job log row {what!r}")
    pg.execute(
        f"INSERT INTO {aj.TABLE} (job_id, person, what, detail) VALUES (%s, %s, %s, %s)",
        (job_id, person, what, json.dumps(dict(detail or {}), default=str)),
    )


def update_workflow(
    pg: Any,
    wf: Mapping[str, Any],
    *,
    name: Optional[str] = None,
    instruction: Optional[str] = None,
    trigger_kind: Optional[str] = None,
    trigger: Optional[Mapping[str, Any]] = None,
) -> None:
    """Atomically edit a non-stopped, non-deleted workflow under its advisory lock."""
    wid, person = str(wf["workflow_id"]), str(wf["person"])
    with _tx(pg):
        _lock_workflow(pg, wid)
        fresh = fold(rows_of(pg, wid))
        if fresh.get("deleted") or fresh["status"] == "stopped":
            raise ValueError("A stopped workflow cannot be edited.")
        clean_name = mask_served(" ".join((name if name is not None else fresh["name"]).split()))
        clean_instr = mask_served(" ".join((instruction if instruction is not None else fresh["instruction"]).split()))
        if not clean_instr:
            raise ValueError("A workflow needs an instruction.")
        if len(clean_instr) > aj.TEXT_CHARS:
            raise ValueError(f"A workflow instruction is at most {aj.TEXT_CHARS} characters.")
        t_kind = trigger_kind or fresh["trigger_kind"]
        t_val = trigger if trigger is not None else fresh["trigger"]
        clean_trig = validate_trigger(t_kind, t_val)
        _insert_wf(pg, wid, person, "updated", {
            "name": clean_name or clean_instr,
            "instruction": clean_instr,
            "trigger_kind": t_kind,
            "trigger": clean_trig,
        })
    if not callable(getattr(pg, "transaction", None)) and not getattr(pg, "autocommit", True):
        pg.commit()


def stop_workflow(pg: Any, wf: Mapping[str, Any], *, deleted: bool = False) -> list[str]:
    """Stop the workflow permanently (and optionally mark it deleted so it leaves the list) and append
    ``stop`` to any live job belonging to it, serialized against concurrent ``tick`` fires in one transaction.
    Returns the live job ids asked to stop."""
    wid, person = str(wf["workflow_id"]), str(wf["person"])
    stopped_jobs: list[str] = []
    with _tx(pg):
        _lock_workflow(pg, wid)
        fresh = fold(rows_of(pg, wid))
        if deleted:
            if not fresh.get("deleted"):
                _insert_wf(pg, wid, person, "stopped", {"by": "person", "deleted": True})
        elif fresh["status"] != "stopped":
            _insert_wf(pg, wid, person, "stopped", {"by": "person"})
        for run in fresh.get("runs") or ():
            jid = run["job_id"]
            jrows = aj.rows_of(pg, jid)
            if not jrows:
                continue
            state = aj.fold(jrows)
            if state["status"] in ("queued", "running") and not state["stop_asked"]:
                _insert_job(pg, jid, person, "stop", {"by": "workflow_stop", "workflow_id": wid})
                stopped_jobs.append(jid)
    if not callable(getattr(pg, "transaction", None)) and not getattr(pg, "autocommit", True):
        pg.commit()
    return stopped_jobs


def delete_workflow(pg: Any, wf: Mapping[str, Any]) -> list[str]:
    """Delete a workflow cleanly (append-only ``stopped`` with ``deleted: true``) and stop any live jobs."""
    return stop_workflow(pg, wf, deleted=True)


def trigger_run_now(pg: Any, wf: Mapping[str, Any]) -> str:
    """Run any non-stopped workflow immediately on demand: atomically creates one ``assist.job_log`` job
    and logs ``run_now``."""
    wid, person = str(wf["workflow_id"]), str(wf["person"])
    jid = aj.new_job_id()
    text = mask_served(str(wf["instruction"]))
    with _tx(pg):
        _lock_workflow(pg, wid)
        fresh = fold(rows_of(pg, wid))
        if fresh["status"] == "stopped":
            raise ValueError("This workflow was stopped. Create a new workflow to run it again.")
        _insert_job(pg, jid, person, "requested", {
            "text": text, "workflow_id": wid, "workflow_name": fresh["name"],
            "trigger_note": "Run on demand",
        })
        _insert_wf(pg, wid, person, "run_now", {"job_id": jid, "trigger_note": "Run on demand"})
    if not callable(getattr(pg, "transaction", None)) and not getattr(pg, "autocommit", True):
        pg.commit()
    return jid


# ----------------------------------------------------------------------------- worker tick (triggers & backoff)

def _job_states(pg: Any, job_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    if not job_ids:
        return {}
    cur = pg.execute(
        f"SELECT seq, job_id, person, at, what, detail FROM {aj.TABLE} WHERE job_id = ANY(%s) ORDER BY seq",
        (list(job_ids),),
    )
    by: dict[str, list[dict[str, Any]]] = {}
    for r in cur.fetchall():
        row = dict(zip(("seq", "job_id", "person", "at", "what", "detail"), r))
        by.setdefault(row["job_id"], []).append(row)
    return {jid: aj.fold(rows) for jid, rows in by.items() if any(x["what"] == "requested" for x in rows)}


def _reconcile_one(pg: Any, wf: dict[str, Any], states: Mapping[str, dict[str, Any]], now: dt.datetime) -> dict[str, Any]:
    """Reconcile finished/failed jobs back onto the workflow log so failure streaks back off and never spin."""
    wid, person = wf["workflow_id"], wf["person"]
    failures = int(wf["consecutive_failures"])
    changed = False
    for run in reversed(wf["runs"]):            # oldest to newest
        jid = run["job_id"]
        if jid in wf["reconciled_jobs"]:
            continue
        st = states.get(jid)
        if st is None or st["status"] in LIVE_JOB_STATES:
            continue
        if st["status"] in ("finished", "stopped"):
            append(pg, wid, person, "run_succeeded", {"job_id": jid, "job_status": st["status"]})
            failures = 0
            changed = True
        elif st["status"] == "failed":
            failures += 1
            delay = backoff_seconds(failures)
            until = now + dt.timedelta(seconds=delay)
            msg = st.get("message") or st.get("reason") or "The job failed."
            append(pg, wid, person, "run_failed", {
                "job_id": jid, "reason": st.get("reason") or "failed", "message": msg,
                "consecutive_failures": failures, "backoff_until": until.isoformat(),
            })
            changed = True
            if failures >= AUTO_PAUSE_FAILURES and wf["status"] == "active":
                append(pg, wid, person, "auto_paused", {
                    "reason": "paused_after_failures", "consecutive_failures": failures, "last_job_id": jid,
                    "message": f"Paused automatically after {failures} failed runs in a row. Resume or edit to try again.",
                })
    return fold(rows_of(pg, wid)) if changed else wf


def _person_mailboxes(pg: Any, person: str) -> list[str]:
    """The person's own mailbox addresses/keys for scoping ``mail_from`` events."""
    found = {person.lower()}
    try:
        rows = pg.execute("SELECT mailbox, addrs FROM biz.mailbox_owners WHERE lower(mailbox) = lower(%s)",
                          (person,)).fetchall()
        for mb, addrs in rows:
            if mb:
                found.add(str(mb).lower())
            for a in (addrs or ()):
                if a:
                    found.add(str(a).lower())
    except Exception as exc:  # noqa: BLE001 - mailbox_owners absent in minimal fixture
        if not is_missing_table(exc):
            LOG.warning("assist workflows: mailbox_owners read failed (%s)", exc)
    return sorted(found)


_TEAMS_AUTHOR_SQL = (
    "coalesce(nullif(btrim(t.author), ''), "
    "CASE WHEN pg_input_is_valid(t.sender, 'json') AND left(btrim(t.sender), 1) = '{' "
    "THEN nullif(btrim((t.sender)::json->>'display_name'), '') END)"
)


def _load_teams_chat_acls(state_dir: Optional[str | Path]) -> Optional[dict[str, dict[str, Any]]]:
    """Read Teams chat ACLs (including ``history_start``) from the local capture store when available.
    Returns None when no Teams capture store exists at ``state_dir``; returns ``{}`` (fail closed) if a
    store exists on disk but cannot be read."""
    raw = state_dir or os.environ.get("PLX_STATE_DIR")
    if not raw:
        return None
    from integrations.capture_teams import CHAT_KIND, DB_FILE, STORE_DIR
    db_path = Path(raw) / STORE_DIR / DB_FILE
    if not db_path.is_file():
        return None
    out: dict[str, dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT unit, acl FROM unit_acl WHERE kind = ?", (CHAT_KIND.name,)
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - unreadable capture store fails closed for Teams chats
        LOG.warning("assist workflows: Teams chat ACL store unreadable (%s)", exc)
        return {}
    for unit, raw_acl in rows:
        if not isinstance(unit, str):
            continue
        try:
            parsed = json.loads(raw_acl)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, Mapping):
            out[unit] = dict(parsed)
    return out


def _teams_history_allows(
    chat_acls: Mapping[str, Mapping[str, Any]],
    audience_key: str,
    created_at: Any,
    identities: set[str],
) -> bool:
    """True only when the chat's captured ACL admits at least one of ``identities`` at ``created_at``."""
    acl = chat_acls.get(audience_key)
    if acl is None:
        return False
    from integrations.capture_teams import _history_acl
    ts = _as_utc(created_at).strftime("%Y-%m-%dT%H:%M:%S.%fZ") if isinstance(created_at, dt.datetime) else created_at
    shown = _history_acl(acl, {"created_at": ts})
    allowed = {str(i).strip().lower() for i in (shown.get("identities") or ()) if isinstance(i, str)}
    return bool(identities & allowed)


def _match_alert_events(
    pg: Any,
    *,
    rule: str,
    since: dt.datetime,
    filter_text: str,
    dk_prefix: str,
    note_prefix: str,
    instr: str,
) -> list[tuple[str, str, str]]:
    """Query open, non-private, non-excluded business alerts matching ``rule`` (mirrors ``operating/pg/alerts.py``)."""
    sql = (
        "SELECT a.handle, a.why, a.first_seen FROM biz.alerts a "
        "WHERE a.rule = %s AND a.state = 'open' "
        "AND a.private IS FALSE AND a.mailbox IS NULL AND coalesce(cardinality(a.units), 0) = 0 "
        "AND a.objects IS NOT NULL "
        "AND NOT EXISTS ("
        "SELECT 1 FROM unnest(a.objects) o(id) "
        "JOIN ontology.people_excluded_objects p ON p.object_id = o.id"
        ") AND a.first_seen >= %s"
    )
    params: list[Any] = [rule, since]
    if filter_text:
        sql += " AND lower(a.why) LIKE %s ESCAPE '\\'"
        params.append(_like_contains(filter_text))
    sql += " ORDER BY a.first_seen ASC"
    out: list[tuple[str, str, str]] = []
    for handle, why, first_seen in pg.execute(sql, params).fetchall():
        day = _as_utc(first_seen).date().isoformat() if isinstance(first_seen, dt.datetime) else str(first_seen)
        dk = f"{dk_prefix}:{handle}:{day}"
        note = f"{note_prefix}: {why}"
        text = f"{note} [record {handle}]. {instr}"
        out.append((dk, note, text))
    return out


def _table_columns(pg: Any, schema: str, table: str) -> set[str]:
    try:
        rows = pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = %s AND table_name = %s",
            (schema, table),
        ).fetchall()
        return {str(r[0]) for r in rows}
    except Exception:  # noqa: BLE001 - non-pg fixture falls back to base columns
        return set()


def _match_events(
    pg: Any,
    wf: Mapping[str, Any],
    *,
    chat_acls: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[tuple[str, str, str]]:
    """Return ``[(dedupe_key, trigger_note, job_text), ...]`` for new events matching ``wf``."""
    trig = wf["trigger"]
    ev = str(trig.get("event") or "")
    since = wf["active_since"]
    instr = str(wf["instruction"])
    out: list[tuple[str, str, str]] = []
    try:
        if ev == "mail_from":
            sender = str(trig.get("sender") or "").strip()
            subject = str(trig.get("subject") or "").strip()
            mailboxes = _person_mailboxes(pg, str(wf["person"]))
            cols = _table_columns(pg, "biz", "emails")
            sql = (
                "SELECT e.handle, e.sender, coalesce(e.subject, ''), e.received_at FROM biz.emails e "
                "WHERE lower(e.mailbox) = ANY(%s) AND e.received_at >= %s AND NOT coalesce(e.is_bulk, false)"
            )
            if "ai_ok" in cols:
                sql += " AND e.ai_ok IS TRUE"
            if "excluded" in cols:
                sql += " AND e.excluded IS FALSE"
            if "rec_marks" in cols:
                sql += (
                    " AND e.rec_marks IS NOT NULL "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM unnest(e.rec_marks) rm WHERE rm IS NULL OR btrim(rm) NOT IN ('', '[]')"
                    ")"
                )
            if "object_ids" in cols:
                sql += (
                    " AND e.object_ids IS NOT NULL AND cardinality(e.object_ids) > 0 "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM unnest(e.object_ids) o(id) "
                    "JOIN ontology.people_excluded_objects p ON p.object_id = o.id"
                    ")"
                )
            params: list[Any] = [mailboxes, since]
            if sender:
                sql += " AND lower(coalesce(e.sender, '')) LIKE %s ESCAPE '\\'"
                params.append(_like_contains(sender))
            if subject:
                sql += " AND lower(coalesce(e.subject, '')) LIKE %s ESCAPE '\\'"
                params.append(_like_contains(subject))
            sql += " ORDER BY e.received_at ASC"
            for handle, frm, subj, _at in pg.execute(sql, params).fetchall():
                dk = f"mail:{handle}"
                note = f"New email from {frm}" + (f" ({subj})" if subj else "")
                text = f"{note} [record {handle}]. {instr}"
                out.append((dk, note, text))
        elif ev == "teams_from":
            sender = str(trig.get("sender") or "").strip()
            subject = str(trig.get("subject") or "").strip()
            sql = (
                f"SELECT t.handle, coalesce({_TEAMS_AUTHOR_SQL}, ''), coalesce(t.subject, ''), "
                "r.said_at, t.created_at, t.audience_keys[1] "
                "FROM assist.readings r JOIN biz.teams_messages t ON t.handle = r.handles[1] "
                "WHERE r.source = 'teams_messages' AND r.status = 'ok' "
                "AND r.actor_person = %s AND r.said_at >= %s "
                "AND t.ai_ok IS TRUE AND t.excluded IS FALSE "
                "AND t.rec_marks IS NOT NULL "
                "AND NOT EXISTS ("
                "SELECT 1 FROM unnest(t.rec_marks) rm WHERE rm IS NULL OR btrim(rm) NOT IN ('', '[]')"
                ") "
                "AND t.object_ids IS NOT NULL AND cardinality(t.object_ids) > 0 "
                "AND NOT EXISTS ("
                "SELECT 1 FROM unnest(t.object_ids) o(id) "
                "JOIN ontology.people_excluded_objects p ON p.object_id = o.id"
                ") "
                "AND t.audience_keys IS NOT NULL AND cardinality(t.audience_keys) = 1 "
                "AND EXISTS ("
                "SELECT 1 FROM search.audience_units u "
                "WHERE u.audience_key = t.audience_keys[1] "
                "AND ((u.sharing = 'shared' AND u.audience_key LIKE 'teams:channel:%%') "
                "OR EXISTS (SELECT 1 FROM unnest(u.people) mp WHERE lower(mp) = lower(%s)))"
                ")"
            )
            params = [wf["person"], since, wf["person"]]
            if sender:
                sql += f" AND lower(coalesce({_TEAMS_AUTHOR_SQL}, '')) LIKE %s ESCAPE '\\'"
                params.append(_like_contains(sender))
            if subject:
                sql += " AND lower(coalesce(t.subject, '') || ' ' || coalesce(t.said, '')) LIKE %s ESCAPE '\\'"
                params.append(_like_contains(subject))
            sql += " ORDER BY r.said_at ASC"
            rows = pg.execute(sql, params).fetchall()
            identities = set(_person_mailboxes(pg, str(wf["person"]))) if chat_acls is not None and rows else set()
            for handle, frm, subj, _at, created_at, aud_key in rows:
                if chat_acls is not None and str(aud_key).startswith("teams:chat:"):
                    if not _teams_history_allows(chat_acls, str(aud_key), created_at, identities):
                        continue
                dk = f"teams:{handle}"
                note = (f"New Teams message from {frm}" if frm else "New Teams message") + (f" ({subj})" if subj else "")
                text = f"{note} [record {handle}]. {instr}"
                out.append((dk, note, text))
        elif ev == "po_late":
            supplier = str(trig.get("supplier") or "").strip()
            return _match_alert_events(
                pg, rule="po_late", since=since, filter_text=supplier,
                dk_prefix="po_late", note_prefix="PO went late", instr=instr,
            )
        elif ev == "mc_question":
            rows = pg.execute(
                "SELECT subject_key, title, basis_at FROM assist.matters "
                "WHERE owner_person = %s AND kind = 'task' AND subject_key LIKE 'mc-question:%%' "
                "AND state = 'open' AND basis_at >= %s ORDER BY basis_at ASC",
                (wf["person"], since),
            ).fetchall()
            for skey, title, _at in rows:
                dk = f"mc_q:{skey}"
                note = f"Mission Control question arrived: {title}"
                text = f"{note} [{skey}]. {instr}"
                out.append((dk, note, text))
        elif ev == "qc_hold":
            product = str(trig.get("product") or "").strip()
            return _match_alert_events(
                pg, rule="lot_on_hold", since=since, filter_text=product,
                dk_prefix="qc_hold", note_prefix="Batch on QC hold", instr=instr,
            )
    except Exception as exc:  # noqa: BLE001 - missing event source table is logged, never silent
        if is_missing_table(exc):
            LOG.warning("assist workflows: event source for %s unavailable (%s)", ev, exc)
            return []
        raise
    return out


def _fire_once(pg: Any, wf: Mapping[str, Any], dedupe_key: str, trigger_note: str, job_text: str) -> Optional[str]:
    """Atomically record a unique fire on ``assist.workflow_log`` and enqueue one ``assist.job_log`` job
    in ONE transaction under the workflow's advisory lock, re-checking that the workflow is still active
    so a concurrent Stop can never leave a newly fired job running."""
    jid = aj.new_job_id()
    wid, person = str(wf["workflow_id"]), str(wf["person"])
    detail = {"job_id": jid, "dedupe_key": dedupe_key, "trigger_note": trigger_note}
    with _tx(pg):
        _lock_workflow(pg, wid)
        fresh = fold(rows_of(pg, wid))
        if fresh["status"] != "active":
            return None
        row = pg.execute(
            f"INSERT INTO {TABLE} (workflow_id, person, what, detail) VALUES (%s, %s, 'fired', %s) "
            f"ON CONFLICT DO NOTHING RETURNING seq",
            (wid, person, json.dumps(detail, default=str)),
        ).fetchone()
        if row is None:
            return None
        _insert_job(pg, jid, person, "requested", {
            "text": mask_served(job_text), "workflow_id": wid, "workflow_name": fresh["name"],
            "trigger_note": trigger_note, "dedupe_key": dedupe_key,
        })
    if not callable(getattr(pg, "transaction", None)) and not getattr(pg, "autocommit", True):
        pg.commit()
    return jid


def tick(
    pg: Any,
    now: Optional[dt.datetime] = None,
    *,
    state_dir: Optional[str | Path] = None,
) -> dict[str, int]:
    """Evaluate every active workflow once: reconcile completed runs (with backoff/auto-pause on failure),
    then enqueue one job per due schedule slot or new matching event. Safe to run repeatedly."""
    now_utc = _as_utc(now or dt.datetime.now(dt.timezone.utc))
    counts = {"checked": 0, "fired": 0, "skipped_backoff": 0, "skipped_running": 0, "auto_paused": 0}
    workflows = all_workflows(pg)
    all_jids = [r["job_id"] for wf in workflows for r in wf["runs"]]
    states = _job_states(pg, all_jids)
    has_teams = any(
        wf["status"] == "active" and wf["trigger_kind"] == "event" and wf["trigger"].get("event") == "teams_from"
        for wf in workflows
    )
    chat_acls = _load_teams_chat_acls(state_dir) if has_teams else None
    for wf in workflows:
        counts["checked"] += 1
        was_active = wf["status"] == "active"
        wf = _reconcile_one(pg, wf, states, now_utc)
        if was_active and wf["status"] == "paused" and wf["pause_reason"] == "paused_after_failures":
            counts["auto_paused"] += 1
        if wf["status"] != "active":
            continue
        if wf["backoff_until"] is not None and now_utc < wf["backoff_until"]:
            counts["skipped_backoff"] += 1
            continue
        if any((states.get(r["job_id"]) or {}).get("status") in LIVE_JOB_STATES for r in wf["runs"]):
            counts["skipped_running"] += 1
            continue
        if wf["trigger_kind"] == "schedule":
            slot = due_slot(wf["trigger"], wf["active_since"], now_utc)
            if slot is None:
                continue
            dk = f"sched:{slot.isoformat()}"
            if dk in wf["fired_keys"]:
                continue
            note = f"Scheduled ({wf['trigger_summary']})"
            if _fire_once(pg, wf, dk, note, wf["instruction"]):
                counts["fired"] += 1
        elif wf["trigger_kind"] == "event":
            for dk, note, job_text in _match_events(pg, wf, chat_acls=chat_acls):
                if dk in wf["fired_keys"]:
                    continue
                if _fire_once(pg, wf, dk, note, job_text):
                    counts["fired"] += 1
                    break     # one active run at a time per workflow; next tick picks up the next event
    return counts


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="assist-workflows")
    p.add_argument("command", choices=("tick",))
    p.add_argument("--state-dir", default=None)
    args = p.parse_args(argv)
    import psycopg
    from operating.pg import config
    logging.basicConfig(level=logging.INFO)
    with psycopg.connect(config.dsn(), autocommit=True) as pg:
        print(json.dumps(tick(pg, state_dir=args.state_dir), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
