"""R&D and sample projects per customer, as alerts and a board, run by
``operating.proactive_engine`` beside its other rules (``rd_projects``).

A thread is R&D work when a human message of it (quoted history removed; the subject only on a
message that is no reply or forward) is about a sample request, a formulation change, a
stability test or a certificate of analysis (``classify``; automated mail and mailings are
skipped and counted). Threads are grouped into projects per customer, conservatively: a project
is one customer, one kind and one product item (the settled ``mentions_product`` link) or, with
no single item, one conversation. The customer is the thread's single settled customer link
(``Email.sent_by_customer``, ``mentions_customer``) or, with none, its one outside business
domain (a customer or a supplier: the mail carries no link yet); a thread with no customer link
whose mail is settled-linked ``Email.sent_by_supplier`` is purchasing, no project (counted,
``supplier_thread``); two customers, or only
personal-mail senders, make no project (counted, not guessed). A thread needs a human message
of ours and one of theirs, and the first message about the work must be theirs (a sample or
quote we asked a supplier for is no customer project).

Kinds:

* ``rd_project_line`` (low; the board only, never the briefing): one per thread, with its
  kind, state (``waiting_on_us``: the customer wrote last and asked; ``waiting_on_customer``:
  we wrote last; ``update_received``: the customer wrote last, asking nothing; ``closed``: the
  customer closed it), last activity, owner (the person of ours who wrote most) and the
  customer's last address (for the drafted status update only);
* ``rd_project_idle`` (medium; OFF unless ``PLX_RD_IDLE_ALERT=1``: live precision is below
  the alert gate): a project not closed whose last activity, over all its
  threads, is ``IDLE_BUSINESS_DAYS`` business days old or more (``PLX_RD_IDLE_DAYS``, 1 to 60).

Access, per item and failing closed: a line requires a grant on every audience of its thread
and the board shows it only if the reader may also open the customer and item records it names
(``board``); the board's project, status, last activity, owner and waiting-on are computed from
the lines the reader sees and from no other, and no count of withheld lines is given, so a
hidden thread never moves a figure. The idle alert rests on the absence of activity in every
thread, so it requires every audience of every thread of the project.

People first, fail closed (``people``): with the guard unavailable or no tenant domain known
the rule is ``blocked``; a copy in a mailbox the exclusion does not clear is dropped unread; a
thread with a person of ours not cleared is not judged. An alert stores message ids, enum words
and a subject cut to ``SUBJECT_CHARS`` characters, never a body. The status update is a draft
worded from the project's kind, state and dates only (never a message's text, a price, a
quantity or a name): sent only through the card's preview and the reader's confirm.
Rule-based, no model call; fixture runs are ``isolated-fixture``. Mail only (Teams chats are
not read).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import hashlib
import os
import re
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import inbox_triage as it
from . import label_changes as lc
from . import proactive_threads as pt
from .proactive_business import _tick
from .proactive_chasers import _FORWARD, PUBLIC_MAIL

RULE = "rd_projects"
LINE = "rd_project_line"
IDLE = "rd_project_idle"
KINDS = (LINE, IDLE)
WINDOW_DAYS = 90
IDLE_BUSINESS_DAYS = 5
IDLE_ENV = "PLX_RD_IDLE_DAYS"
ALERT_ENV = "PLX_RD_IDLE_ALERT"   # the idle alert is OFF unless this is "1" (precision gate)
SUBJECT_CHARS = 60
MAX_TO = 8
MAX_LINES = 8
WORD_GROUPS = ("sample OR samples", "formulation OR formulations OR reformulation OR formula "
               "OR formulas OR prototype", "stability OR coa OR coas OR certificate")
KIND_ORDER = ("stability_test", "formulation_change", "sample_request", "coa")
KIND_WORD = {"stability_test": "stability test", "formulation_change": "formulation change",
             "sample_request": "sample request", "coa": "certificate of analysis"}
STATES = ("waiting_on_us", "waiting_on_customer", "update_received", "closed")

_PATTERNS = {
    "stability_test": re.compile(r"\bstability\s+(?:test(?:s|ing)?|stud(?:y|ies)|data|"
                                 r"results?|trial)\b|\bshelf[\s-]+life\b|\baccelerated\s+"
                                 r"(?:aging|ageing|stability)\b", re.I),
    "formulation_change": re.compile(r"\b(?:re-?)?formulat(?:ion|ions|e|ed|ing)\b|\bformulas?\b"
                                     r"|\bprototypes?\b|\bbench\s+(?:trial|batch)\b", re.I),
    "sample_request": re.compile(
        r"\b(?:send|ship|shipped|sent|request(?:ed|ing|s)?|need|needs|want|get|provide|submit|"
        r"approve|approved|approval|test|testing|receive[d]?|courier|mail(?:ed)?|prepare[d]?|"
        r"make|made)\b\W+(?:\w+\W+){0,5}?samples?\b|\bsamples?\b\W+(?:\w+\W+){0,5}?\b(?:"
        r"request(?:s|ed)?|approval|approved|ready|shipped|sent|arrive[ds]?|test(?:ing)?|"
        r"results?|feedback|batch|eta)\b|\btrial\s+(?:batch|run|size)\b", re.I),
    "coa": re.compile(r"\bcoas?\b|\bcertificates?\s+of\s+analysis\b", re.I),
}
# Sampling that is no sample sent to a customer (a quality plan, a statistic) and offers.
NOT_RD = re.compile(r"\bsampl(?:e|ing)\s+(?:plan|size|point|rate|frequency|procedure|"
                    r"method|room)\b|\bfree\s+samples?\b|\bsample\s+sale\b|\bformula\s+(?:one|1)"
                    r"\b|\bsample\s+(?:rate|text)\b", re.I)
CLOSING = re.compile(r"\b(?:thank(?:s|\s+you)|received|approved|perfect|all\s+set|no\s+further|"
                     r"that\s+works|got\s+it|looks\s+good|confirmed)\b", re.I)


def classify(text: Any) -> str | None:
    """The R&D kind a message's text is about (module doc), the first of ``KIND_ORDER``."""
    own = NOT_RD.sub(" ", text if isinstance(text, str) else "")
    for kind in KIND_ORDER:
        if _PATTERNS[kind].search(own):
            return kind
    return None


def idle_alert_on(environ: Mapping[str, str] | None = None) -> bool:
    return str((os.environ if environ is None else environ).get(ALERT_ENV) or "").strip() == "1"


def idle_days(environ: Mapping[str, str] | None = None) -> int:
    raw = (os.environ if environ is None else environ).get(IDLE_ENV)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return IDLE_BUSINESS_DAYS
    return value if 1 <= value <= 60 else IDLE_BUSINESS_DAYS


def project_key(customer: str, kind: str, anchor: str) -> str:
    return hashlib.sha256(f"{customer}|{kind}|{anchor}".encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- the read

def population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    key = f"rd_projects:{as_of.isoformat()}"
    if callable(getattr(st, "memo", None)):
        return st.memo(key, lambda: _population(st, as_of, people))
    return _population(st, as_of, people)


def _population(st: Any, as_of: date, people: Any) -> dict[str, Any]:
    return lc.mail_population(st, as_of, people, WORD_GROUPS, WINDOW_DAYS)


def rd_projects(st: Any, as_of: date, *, gap: Callable, cites: Callable, result: Callable,
                skips: Callable, people: Any, **_: Any) -> dict[str, Any]:
    blocked = pt._blocked(people, result)
    if blocked is not None:
        return blocked
    pop = population(st, as_of, people)
    skipped = skips()
    if pop["dropped_unread"]:
        skipped["not_cleared_unread"] += pop["dropped_unread"]
    counts: Counter = Counter()
    lines = project_lines(st, pop, as_of, people, counts, skipped)
    n_idle = idle_days()
    gaps = []
    projects: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        projects[line["key"]].append(line)
    for key, group in sorted(projects.items()):
        group.sort(key=lambda r: (r["last_at"], r["last"]))
        for line in group:
            _tick(st)
            made = _line_gap(gap, cites, pop, line, key, as_of)
            if made is None:
                skipped["message_uncited"] += 1
                continue
            counts["lines"] += 1
            gaps.append(made)
        latest = group[-1]
        waited = pt._business_days(latest["last_at"].date(), as_of)
        counts["projects"] += 1
        if latest["state"] == "closed" or waited < n_idle or not idle_alert_on():
            continue
        groups = [pt._cites_of(cites, pop["rows"], [r["last"]], ("sent_at", "subject", "sender"))
                  for r in group[-MAX_LINES:]]
        who = {"waiting_on_us": "waiting on us", "waiting_on_customer":
               "waiting on the customer"}.get(latest["state"], "last note received")
        subject_id = latest["customer"] or latest["last"]
        subject_type = subject_id.split(":", 1)[0]
        made = gap(
            IDLE, RULE, subject_id, subject_type, f"rd|{key}",
            summary=(f"A {KIND_WORD[latest['kind']]} project has had no activity for {waited} "
                     f"business day(s) (last {latest['last_at'].date().isoformat()}; {who})"),
            missing="Send the customer a status update, or close the project (nothing is "
                    "sent until you send it)",
            why="The project's threads were read for their latest message; none is newer than "
                "the days shown (the messages cited).",
            priority="medium", about=(subject_type, "rd_project"),
            groups=groups,
            event={"project": key, "kind": latest["kind"], "state": latest["state"],
                   "days": waited, "last_on": latest["last_at"].date().isoformat(),
                   "threads": len(group), "customer": latest["customer"],
                   "party": latest["party"]})
        if made is None:
            skipped["message_uncited"] += 1
            continue
        made["requires"] = sorted({a for r in group for a in r["t"].audiences})
        counts[IDLE] += 1
        gaps.append(made)
    out = result(gaps, not_evaluated=skipped)
    out["counts"] = {k: counts[k] for k in ("threads", "projects", "lines", IDLE, "automated",
                                            "mailing", "no_single_customer", "one_sided",
                                            "asked_by_us", "supplier_thread")}
    return out


def project_lines(st: Any, pop: Mapping[str, Any], as_of: date, people: Any, counts: Counter,
                  skipped: Counter, *, before: datetime | None = None) -> list[dict[str, Any]]:
    """The R&D threads of the population as project lines (module doc: kind, state, owner,
    contact, customer or party). ``before``: only the messages sent before this instant
    count (a replay's day never reads a later message: the thread is judged as it stood)."""
    domains = pt._domains(people)
    directory = getattr(people, "directory", None) or {}
    since = as_of - timedelta(days=WINDOW_DAYS)
    threads: list[dict[str, Any]] = []
    for conversation in pop["members"]:
        _tick(st)
        t = pt._Thread(pop, conversation, people, domains)
        if not t.messages:
            continue
        kinds: set[str] = set()
        humans = []
        first_by_us: bool | None = None
        for oid in t.messages:
            if before is not None and t.sent(oid) >= before:
                break
            sender, subject, text = t.sender(oid), t.subject(oid), t.text(oid)
            if pt.automated(sender, text, subject) or it.bulk(sender, text, subject):
                counts["automated"] += 1
                continue
            to = pt._to(pop["rows"][oid])
            if isinstance(to, list) and len(to) > MAX_TO:
                counts["mailing"] += 1
                continue
            humans.append(oid)
            if _FORWARD.match(subject):
                text = pt._own((pop["rows"][oid].get("body_preview") or {}).get("value") or "")
            kind = classify(text)
            if kind:
                kinds.add(kind)
                if first_by_us is None:
                    first_by_us = pt._internal(sender, domains)
        if not kinds or not humans:
            continue
        ours = [pt._internal(t.sender(o), domains) for o in humans]
        if not (any(ours) and not all(ours)):
            counts["one_sided"] += 1   # a notice or a pitch: nobody of ours wrote, or only we did
            continue
        if first_by_us:
            counts["asked_by_us"] += 1   # we asked: a supplier's sample or quote, no customer
            continue
        if t.sent(humans[-1]).date() < since:
            continue
        counts["threads"] += 1
        if not t.ok:
            skipped[t.why] += 1
            continue
        threads.append({"t": t, "conversation": conversation, "humans": humans,
                        "last": humans[-1],
                        "kind": next(k for k in KIND_ORDER if k in kinds)})
    links = lc._links(st, sorted({o for x in threads for o in x["humans"]}))
    lines: list[dict[str, Any]] = []
    for x in threads:
        t: pt._Thread = x["t"]
        customers = sorted({c for o in x["humans"] for c in links[o]["customers"]})
        items = sorted({i for o in x["humans"] for i in links[o]["items"]})
        domains_out = sorted({a.rsplit("@", 1)[1] for o in x["humans"]
                              for a in [t.sender(o)] if "@" in a
                              and not pt._internal(a, domains)})
        party = None
        if len(customers) > 1:   # two customers in one thread: not guessed
            counts["no_single_customer"] += 1
            continue
        if customers:
            customer = customers[0]
        elif any(links[o]["suppliers"] for o in x["humans"]):
            # the outside party is a supplier by a settled link: its samples, quotes and
            # certificates are purchasing, never a customer's project
            counts["supplier_thread"] += 1
            continue
        elif len(domains_out) == 1 and domains_out[0] not in PUBLIC_MAIL:
            # no settled customer link on the thread's mail: the one outside domain is the
            # party (a customer, a supplier: it is not known which), never a person
            customer, party = None, "@" + domains_out[0]
        else:
            counts["no_single_customer"] += 1
            continue
        anchor = items[0] if len(items) == 1 else x["conversation"]
        last = x["last"]
        text = t.text(last)
        if pt._internal(t.sender(last), domains):
            state = "waiting_on_customer"
        elif CLOSING.search(text) and not it.reply_ask(text):
            state = "closed"
        elif it.reply_ask(text):
            state = "waiting_on_us"
        else:
            state = "update_received"
        writers = Counter(directory[t.sender(o)] for o in x["humans"]
                          if pt._internal(t.sender(o), domains) and directory.get(t.sender(o)))
        owner = max(writers, key=lambda k: (writers[k], k)) if writers else None
        contact = next((t.sender(o) for o in reversed(x["humans"])
                        if not pt._internal(t.sender(o), domains)), None)
        lines.append({"t": t, "key": project_key(customer or party, x["kind"], anchor), "party": party,
                      "conversation": x["conversation"], "kind": x["kind"], "state": state,
                      "customer": customer, "items": items[:3], "last": last,
                      "last_at": t.sent(last), "owner": owner, "contact": contact,
                      "humans": x["humans"], "first_at": t.sent(x["humans"][0])})
    return lines


def _line_gap(gap: Callable, cites: Callable, pop: Mapping[str, Any], line: Mapping[str, Any],
              key: str, as_of: date) -> dict[str, Any] | None:
    t: pt._Thread = line["t"]
    waited = pt._business_days(line["last_at"].date(), as_of)
    msgs = [{"id": o, "channel": "mail", "sent": t.sent(o).date().isoformat(),
             "subject": pt._clip(t.subject(o), SUBJECT_CHARS)} for o in line["humans"][-3:]]
    subject_id = line["customer"] or line["last"]
    made = gap(
        LINE, RULE, subject_id, subject_id.split(":", 1)[0],
        f"rdline|{line['conversation']}",
        summary=(f"{KIND_WORD[line['kind']].capitalize()} thread, last activity "
                 f"{line['last_at'].date().isoformat()}, {line['state'].replace('_', ' ')}"),
        missing="Nothing yet: tracked on the R&D board",
        why="A thread about this kind of work was read for its latest human message (cited).",
        priority="low", about=(subject_id.split(":", 1)[0], "rd_project"),
        groups=[pt._cites_of(cites, pop["rows"], [line["last"]],
                             ("sent_at", "subject", "sender"))],
        event={"project": key, "kind": line["kind"], "state": line["state"], "days": waited,
               "last_on": line["last_at"].date().isoformat(),
               "first_on": line["first_at"].date().isoformat(),
               "messages_n": len(line["humans"]), "customer": line["customer"],
               "party": line["party"],
               "items": line["items"], "owner": line["owner"],
               "contact": line["contact"], "messages": msgs})
    if made is None:
        return None
    made["requires"] = list(t.audiences)
    return made


# --------------------------------------------------------------------------- the board

def status_draft(project: Mapping[str, Any], today: date) -> dict[str, Any]:
    """A status update to the customer, worded from the project's kind, state and dates only
    (never a message's text, price, quantity, name or the thread's subject); a draft: nothing
    is sent (the card's preview and confirm are the only way out)."""
    word = KIND_WORD.get(project.get("kind"), "request")
    last = project.get("last_on") or "recently"
    if project.get("status") == "waiting_on_us":
        line = ("We are working on it and will come back to you by "
                f"{it._next_business_day(today).isoformat()}.")
    elif project.get("status") == "waiting_on_customer":
        line = ("We are waiting for your reply on our last message and will go on as soon "
                "as we have it.")
    else:
        line = "Please tell us if there is anything you need from us."
    to = project.get("contact")
    return {"channel": "email",
            "to": [{"address": to}] if isinstance(to, str) and "@" in to else [],
            "subject": f"Status of your {word}",
            "body": (f"Hello,\n\nA quick update on your {word}: our last activity on it was "
                     f"{last}. {line}\n\nBest regards"),
            "status": "draft_not_sent", "sent": False}


def board(gaps: Iterable[Mapping[str, Any]], today: date, *,
          decide: Callable[[Sequence[str]], Mapping[str, Any]], me: Iterable[str] = (),
          idle_after: int | None = None) -> dict[str, Any]:
    """The reader's R&D board from their own trimmed alert list (``read_inbox`` as the reader:
    a line needs a grant on every audience of its thread) and, per item, the customer and
    item records it names, which ``decide`` must clear (a record it cannot decide or refuses
    drops the line, fail closed). A project is built from the lines left and from no other."""
    n_idle = idle_after if idle_after is not None else idle_days()
    mine = {str(x).lower() for x in me}
    raw = []
    for item in gaps:
        if item.get("kind") != LINE or item.get("state") == "resolved":
            continue
        event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
        if not isinstance(event.get("project"), str) or event.get("kind") not in KIND_WORD \
                or event.get("state") not in STATES or not (
                    isinstance(event.get("customer"), str)
                    or (event.get("customer") is None and isinstance(event.get("party"), str))):
            continue   # a line whose shape cannot be trusted is not shown
        raw.append((item, event))
    handles = sorted({h for _i, e in raw for h in [e.get("customer"), *(e.get("items") or ())]
                      if isinstance(h, str)})
    try:
        verdict = decide(handles) if handles else {}
    except Exception:  # noqa: BLE001 - an undecidable reader sees nothing
        verdict = {}
    open_ = {h for h in handles if h in verdict and verdict[h] is None}
    projects: dict[str, list[tuple[Mapping, Mapping]]] = defaultdict(list)
    for item, event in raw:
        named = [h for h in [event.get("customer"), *(event.get("items") or ())] if h]
        if all(isinstance(h, str) and h in open_ for h in named):
            projects[event["project"]].append((item, event))
    out = []
    for key, group in projects.items():
        group.sort(key=lambda p: (p[1].get("last_on") or "", str(p[0].get("id"))), reverse=True)
        item, latest = group[0]
        status = latest["state"]
        try:
            waited = int(latest.get("days") or 0)
        except (TypeError, ValueError):
            waited = 0
        owners = Counter(e.get("owner") for _i, e in group if isinstance(e.get("owner"), str))
        owner = max(owners, key=lambda k: (owners[k], k)) if owners else None
        out.append({
            "id": key, "customer": latest["customer"], "party": latest.get("party"), "kind": latest["kind"],
            "status": status, "last_on": latest.get("last_on"), "days": waited,
            "idle": status != "closed" and waited >= n_idle,
            "owner": owner, "owner_is_you": bool(owner and owner.lower() in mine),
            "waiting_on": {"waiting_on_us": "us", "waiting_on_customer": "customer"}.get(status),
            "items": sorted({i for _x, e in group for i in e.get("items") or ()}),
            "contact": latest.get("contact"),
            "lines": [{"id": i.get("id"), "state": e["state"], "last_on": e.get("last_on"),
                       "messages_n": int(e.get("messages_n") or 0),
                       "subject": ((e.get("messages") or [{}])[-1] or {}).get("subject"),
                       "citations": list(i.get("citations") or ())}
                      for i, e in group[:MAX_LINES]],
            "citations": list(item.get("citations") or ())})
    out.sort(key=lambda p: (not p["idle"], p["status"] == "closed", -p["days"], p["id"]))
    return {"schema": "plx-rd-board/v1", "as_of": today.isoformat(),
            "idle_after_days": n_idle, "total": len(out),
            "idle": sum(p["idle"] for p in out), "projects": out, "sent": 0, "writes": 0}
