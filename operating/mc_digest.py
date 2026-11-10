"""The Mission Control approvals digest: each decider's pending MC approval gates, grouped by
class, ranked, with the history-based recommendation and the low-risk classes marked; each
item links to its task in Mission Control. Read-only: the Workbench decides no gate.

Where the items come from: the ``approval_request`` autopilot (``operating.autopilot``)
prepares one item per pending gate, routed to the people who decided approvals in the
gate's MC project in the 30 days before it (``autopilot.deciders``; none: the approver
roster). ``digest`` reads only the items ``autopilot.visible`` already let the reader see
(their own and, per the owner's team grants, their reports'), so it shows nothing the
reader's My work does not; it reads no store, calls no client and writes nothing.

The class (``classify``) comes from the gate's own reason, by the first class whose words
it states, the riskier classes first (a reason naming money and a merge is money); a reason
naming none is ``other``, never low risk. ``history`` counts, per class, the gates of the
same MC audience (operator scope and project) decided in the ``HISTORY_DAYS`` before the
request: a replay of a past day reads only what was decided by then. A class is marked low
risk only when it is declared a low-risk candidate (``CLASSES``) and its history shows at
least ``LOW_RISK_MIN`` decisions of which at least ``LOW_RISK_SHARE`` were approvals; the mark
is advice for the decider. Whether any class may ever be approved without a person is the
owner's open decision (A11): an approval is an MC write, which the Workbench never makes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any, Mapping, Sequence

# The MC host the hub guard admits (``integrations.plx_hub_guard.DEFAULT_MC_BASE_URL``); a
# link only, never fetched. The gate is decided on its task's page.
MC_TASKS_URL = "https://mc.plxcustomer.io/tasks/"
TASK_ID = re.compile(r"^TASK-\d{1,7}$")
HISTORY_DAYS = 60
LOW_RISK_MIN = 15
LOW_RISK_SHARE = (95, 100)          # approved * 100 >= 95 * decided (integers, no float)
APPROVED = ("approved", "granted")
REJECTED = ("rejected", "denied", "declined")
RECOMMENDATIONS = ("approve", "review", "superseded")

# (id, label, risk, words): the first match wins, riskier first. risk "low" = a candidate
# that history may mark low risk; "high" and "normal" are never marked.
CLASSES: tuple[tuple[str, str, str, re.Pattern[str]], ...] = (
    ("money", "Money, price or ledger change", "high", re.compile(
        r"\b(money|price|prices|pricing|invoice|invoices|payment|payments|payroll|cost|"
        r"costs|ledger|gl|tax|refund|credit)\b", re.I)),
    ("permission", "Permission, access or secret change", "high", re.compile(
        r"\b(permission|permissions|auth|acl|role|roles|grant|grants|access|secret|secrets|"
        r"credential|credentials|key|keys|token|tokens|password)\b", re.I)),
    ("migration", "Database or schema migration", "high", re.compile(
        r"\b(migration|migrations|migrate|schema|ddl|database|db|backfill)\b", re.I)),
    ("production", "Go-live, deploy or production change", "high", re.compile(
        r"\b(go[- ]?live|deploy|deploys|deployment|production|prod|release|cutover)\b",
        re.I)),
    ("workflow", "CI or workflow change", "high", re.compile(
        r"\b(workflow|workflows|ci|github actions)\b", re.I)),
    ("merge", "Code merge", "normal", re.compile(
        r"\b(merge|merges|merged|pr|pull request)\b", re.I)),
    ("launch", "Agent launch", "low", re.compile(r"\b(launch|launches|lease|steward)\b",
                                                  re.I)),
)
OTHER = ("other", "Decision or other ask", "normal")
RISK_ORDER = {"high": 0, "normal": 1, "low": 2}
CLASS_ORDER = {c[0]: n for n, c in enumerate(CLASSES)} | {OTHER[0]: len(CLASSES)}


def classify(reason: Any) -> str:
    """The class a gate's reason states (module doc); none or no reason: ``other``."""
    text = str(reason or "")
    for cid, _label, _risk, words in CLASSES:
        if words.search(text):
            return cid
    return OTHER[0]


def _meta(cid: str) -> tuple[str, str, str]:
    for c in CLASSES:
        if c[0] == cid:
            return c[0], c[1], c[2]
    return OTHER


def _instant(raw: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(raw or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _decisions(st: Any) -> list[tuple[str, datetime, str, str]]:
    """(audience, decided at, class, approved|rejected) of every decided gate the store
    holds, read once per store connection."""
    cache = st.__dict__.get("_mc_digest_decisions")
    if cache is not None:
        return cache
    from .autopilot import _read, _v
    rows = st.conn.execute(
        "SELECT object_id, audience_key FROM objects WHERE object_type = 'MCApproval'"
    ).fetchall()
    units = {r[0]: str(r[1] or "") for r in rows}
    cache = []
    for oid, r in _read(st, list(units), ("status", "decided_at", "reason")).items():
        status, at = _v(r, "status").lower(), _instant(_v(r, "decided_at"))
        word = "approved" if status in APPROVED else "rejected" if status in REJECTED else ""
        if at is not None and word:
            cache.append((units.get(oid, ""), at, classify(_v(r, "reason")), word))
    st.__dict__["_mc_digest_decisions"] = cache
    return cache


def history(st: Any, audience: str, before: Any, *, days: int = HISTORY_DAYS
            ) -> dict[str, Any]:
    """Per class, the gates of ``audience`` decided in the ``days`` before ``before`` (the
    request's time): ``{"days", "by_class": {class: {"approved", "decided"}}}``; an unknown
    audience or time: no history (``by_class`` empty, ``unknown`` names why)."""
    at = _instant(before)
    if not audience.startswith("mc:") or at is None:
        return {"days": days, "by_class": {},
                "unknown": "request_time_unknown" if at is None else "audience_unknown"}
    since = at - timedelta(days=days)
    by: dict[str, dict[str, int]] = {}
    for unit, decided, cid, word in _decisions(st):
        if unit == audience and since <= decided < at:
            row = by.setdefault(cid, {"approved": 0, "decided": 0})
            row["decided"] += 1
            row["approved"] += word == "approved"
    return {"days": days, "by_class": dict(sorted(by.items()))}


def task_link(task_id: Any) -> str | None:
    """The gate's task page in Mission Control; a task id of another shape: none."""
    tid = str(task_id or "").strip()
    return MC_TASKS_URL + tid if TASK_ID.match(tid) else None


def judged(cid: str, past: Mapping[str, Any] | None) -> dict[str, Any]:
    """The class's history line and its low-risk mark (module doc)."""
    _c, _label, risk = _meta(cid)
    row = ((past or {}).get("by_class") or {}).get(cid) or {}
    try:
        approved, decided = int(row.get("approved") or 0), int(row.get("decided") or 0)
    except (TypeError, ValueError):
        approved, decided = 0, 0
    if not 0 <= approved <= decided:
        approved, decided = 0, 0
    days = (past or {}).get("days") or HISTORY_DAYS
    out: dict[str, Any] = {"approved": approved, "decided": decided, "days": days}
    if past is None or "by_class" not in past:
        out.update(low_risk=False, why="no history recorded with this item")
    elif risk != "low":
        out.update(low_risk=False, why=f"{risk}-risk class: always decided by a person")
    elif decided < LOW_RISK_MIN:
        out.update(low_risk=False, why=f"{decided} decided in {days} days "
                                       f"(fewer than {LOW_RISK_MIN})")
    elif approved * LOW_RISK_SHARE[1] < LOW_RISK_SHARE[0] * decided:
        out.update(low_risk=False, why=f"{approved} of {decided} approved "
                                       f"(below {LOW_RISK_SHARE[0]}%)")
    else:
        out.update(low_risk=True, why=f"{approved} of {decided} approved in {days} days")
    return out


def item_facts(reason: Any, task_id: Any, past: Mapping[str, Any]) -> dict[str, Any]:
    """What the approval handler records on its item: the class, the project's decision
    history per class at the request, and the task's MC link."""
    return {"class": classify(reason), "history": dict(past), "link": task_link(task_id)}


def _entry(item: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    happened = (item.get("body") or {}).get("happened") or {}
    work = (item.get("body") or {}).get("prepared") or {}
    fields = happened.get("fields") or {}
    mc = work.get("mc") if isinstance(work.get("mc"), Mapping) else {}
    cid = classify(fields.get("reason"))
    past = mc.get("history") if isinstance(mc.get("history"), Mapping) else None
    rec = work.get("recommendation")
    asked = _instant(happened.get("at"))
    return {
        "item_id": item["item_id"], "title": item.get("title") or "",
        "task_id": str(fields.get("task_id") or ""),
        "gate_id": str((work.get("action") or {}).get("gate") or ""),
        "requested_at": happened.get("at") or "",
        "waiting_hours": max(0, int((now - asked).total_seconds() // 3600)) if asked else None,
        "recommendation": rec if rec in RECOMMENDATIONS else "review",
        "reasons": [str(r) for r in work.get("reasons") or ()][:3],
        "class": cid, "history": judged(cid, past),
        "link": task_link(fields.get("task_id")),
        "mine": bool(item.get("mine")),
    }


def digest(items: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Per decider (the item's owner), their pending MC gates grouped by class: riskier
    classes first, low-risk classes last; in a class the oldest request first, superseded
    gates after the rest. ``items``: what ``autopilot.visible`` showed the reader."""
    by_owner: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        happened = (item.get("body") or {}).get("happened") or {}
        if item.get("autopilot") != "approval_request" or \
                happened.get("object_type") != "MCApproval":
            continue
        by_owner.setdefault(str(item.get("owner") or ""), []).append(_entry(item, now))
    deciders = []
    for owner, entries in sorted(by_owner.items()):
        groups: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            groups.setdefault(e["class"], []).append(e)
        out = []
        for cid, rows in groups.items():
            _c, label, risk = _meta(cid)
            rows.sort(key=lambda e: (e["recommendation"] == "superseded",
                                     e["requested_at"] or "~", e["item_id"]))
            out.append({"class": cid, "label": label, "risk": risk,
                        "low_risk": all(e["history"]["low_risk"] for e in rows),
                        "pending": len(rows),
                        "recommended": {r: sum(e["recommendation"] == r for e in rows)
                                        for r in RECOMMENDATIONS},
                        "items": rows})
        out.sort(key=lambda g: (g["low_risk"], RISK_ORDER[g["risk"]], CLASS_ORDER[g["class"]]))
        deciders.append({"decider": owner, "mine": any(e["mine"] for e in entries),
                         "pending": len(entries),
                         "low_risk": sum(g["pending"] for g in out if g["low_risk"]),
                         "groups": out})
    deciders.sort(key=lambda d: (not d["mine"], d["decider"]))
    return {"at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pending": sum(d["pending"] for d in deciders), "deciders": deciders,
            "approves": 0,
            "note": "Decide each gate in Mission Control; the Workbench approves nothing."}
