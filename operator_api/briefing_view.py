"""The briefing's payload: briefing items and suggestion cards as the app shows them,
their questions, ageing, test-record filtering and involvement words.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Iterable, Mapping, Optional

from . import models as m
from .page_core import (
    clean_alert_label,
    clean_enum,
    clean_money_amount,
    clean_person,
    clean_subject,
    clean_text,
)
from .test_records import hidden_words
from .serving import (object_url, open_url, record_words, system_of)
from .answers import (aged_to_today)


HOME_GROUP_PAGE_ROWS = 25  # a group's first page, read by Home's one request and ahead of it
HOME_GROUP_ROWS = 3  # rows of one kind Home lists before "Show more" (app: HOME_ROWS)


HOME_PAGES_AHEAD = 6  # records Home opens first, read ahead per reader (``home_first_records``)


def home_first_records(got: Any, most: int = HOME_PAGES_AHEAD) -> list[tuple[str, str]]:
    """The records a read briefing's Home opens first: the first item sent home of each
    kind, in the briefing's rank order (Home's detail pane opens the first row of the list
    it shows, and each kind's tab its own first row), at most ``most``; (type, object id),
    each once. An item without a record is skipped."""
    out: list[tuple[str, str]] = []
    kinds: set = set()
    for item in list(getattr(got, "items", None) or []):
        kind, oid, otype = item.event_type, item.object_id, item.object_type
        if not item.home or kind in kinds or not oid or not otype:
            continue
        kinds.add(kind)
        if (otype, oid) not in out:
            out.append((otype, oid))
        if len(out) >= most:
            break
    return out


def home_group_pages(got: Any) -> list[str]:
    """The groups of a read briefing whose next page Home can show: the kinds sent home,
    of each only HOME_GROUP_ROWS before "Show more", so a kind whose top items already fill
    those rows needs none (late purchase orders have their own list)."""
    top = list(got.items or [])
    if (got.home_count or 0) <= sum(1 for i in top if i.home):
        return []
    kinds = []
    for group in got.groups or []:
        if getattr(group, "section", None):
            continue   # a gathered section lists its biggest few (their own read), not pages
        shown = sum(1 for i in top if i.event_type == group.event_type
                    and i.home)
        if (group.event_type != "po_late" and group.count > (group.in_top or 0)
                and shown < HOME_GROUP_ROWS):
            kinds.append(group.event_type)
    return kinds


def _answer_kept(answer: Any) -> dict[str, Any]:
    """What feedback on an answer attaches: its words, facts (with citations) and subjects,
    as served to its reader (bounded)."""
    try:
        # guard-ok: a bounded feedback snapshot of the answer itself, which memory keeps
        # whole under the same conversation id; nothing is cut that is not already kept whole
        queries = [
            {"title": c.title, "sql": c.sql, "measure_id": c.measure.id if c.measure else None}
            for c in (getattr(answer, "charts", None) or ()) if getattr(c, "sql", None)
        ] + [
            {"title": ct.about, "sql": ct.query, "measure_id": ct.measure.id if ct.measure else None}
            for ct in (getattr(answer, "cited", None) or ()) if getattr(ct, "query", None)
        ]
        return {"answer": (answer.direct_answer or "")[:4000],
                "conversation_id": answer.conversation_id,
                "as_of": answer.freshness.as_of if answer.freshness else None,
                "facts": [{"text": f.text[:600], "sources": [  # guard-ok: bounded feedback snapshot, kept whole in memory
                    {"system": s.system, "handle": s.handle, "label": s.label}
                    for s in f.sources[:8]]} for f in answer.facts[:30]],  # guard-ok: bounded feedback snapshot, kept whole in memory
                "subjects": [{"type": o.type, "object_id": o.object_id, "label": o.label}
                             for o in answer.subjects[:20]],  # guard-ok: bounded feedback snapshot, kept whole in memory
                "queries": queries}
    except Exception:  # noqa: BLE001 - the feedback keeps the question alone
        return {}


# Briefing: of the top items, the first ones get a trail read as the reader.
BRIEFING_TRAILS = 3
BRIEFING_ITEMS_PAGE = 25   # a group's further items the app asks for at once (api.ts)
BRIEFING_TRAIL_SECONDS = 30.0
# The why-trail question per event type that is not about a purchase order's lateness
# (``operating.why_trail.select`` picks the plan covering the record's type).
BRIEFING_QUESTIONS = {
    "lot_near_expiry": "Why is this lot held or released, and how do we know?",
    "lot_on_hold": "Why is this batch on hold, and how do we know?",
    "quantity_mismatch": "Why don't the received and ordered quantities match?",
    "negative_margin": "Why did this order change, and how do we know?",
    "quote_accepted_no_order": "Why was this decided, and how do we know?",
    "customer_order_at_risk": "Why did this order change, and how do we know?",
    "customer_order_late": "Why is this order still open, and how do we know?",
    "customer_order_slower_than_usual": "Why is this order still open, and how do we know?",
}


def alert_playbook_of(view: Any, alert_id: str, environ: Mapping[str, str]
                      ) -> Optional[dict[str, Any]]:
    """The playbook of the alert ``alert_id`` when this reader's inbox shows it (trimmed
    for them: a hidden alert and a missing one alike give None); the markings it holds
    bound every value the playbook states."""
    from operating import alert_playbooks as ap
    from operating import briefing as bf

    grant = getattr(getattr(view, "access", None), "grant", None)
    if grant is None or not isinstance(alert_id, str):
        return None
    found = view.gap_inbox(kinds=list(bf.EVENT_KINDS), limit=bf.MAX_READ)
    if found.get("status") != "read":
        return None
    gap = next((g for g in found.get("gaps") or () if g.get("id") == alert_id), None)
    if gap is None:
        return None
    _, held = grant()
    with view._request():
        return ap.build(gap, bf.TYPE_OF_KIND.get(str(gap.get("kind")), ""), call=view._call,
                        held=held, environ=environ)


def briefing_aged(found: Mapping[str, Any], today: Optional[date] = None) -> dict[str, Any]:
    """The briefing's late purchase orders with their days late counted to today, as Home
    and the trail count them (``aged_to_today``); every other count stays as built."""
    shifted = aged_to_today({"as_of": found.get("as_of"), "run_at": found.get("run_at"),
                             "gaps": [
        {"days_late": (i.get("facts") or {}).get("days")} if i.get("event_type") == "po_late"
        else {} for i in found.get("items") or ()]}, today)
    items = [{**i, "facts": {**(i.get("facts") or {}), "days": g["days_late"]}}
             if isinstance(g.get("days_late"), int) else i
             for i, g in zip(found.get("items") or (), shifted.get("gaps") or ())] \
        if shifted.get("gaps") else list(found.get("items") or ())
    # the day the counts are made to is the briefing's date (the business day, ET), not
    # the day the engine ran
    as_of = shifted.get("days_as_of") or found.get("as_of")
    return {**found, "items": items, "as_of": as_of}


BRIEFING_TEST_SLACK = 8   # items past the top read too, so a test record's place is filled


def briefing_test(item: Mapping[str, Any], labels: Mapping[str, str],
                  named: frozenset[str], suppliers: Mapping[str, str] | None = None) -> bool:
    """Whether a briefing item is about a test or placeholder record
    (``operator_api.test_records``): its record's label and its supplier's name."""
    from .test_records import is_test
    oid = item.get("object_id")
    return is_test((labels.get(oid),), named, ((suppliers or {}).get(oid),))


def briefing_item(item: Mapping[str, Any], labels: Mapping[str, str],
                  trail: Optional[m.EvidenceTrail], *,
                  counterpart: Optional[str] = None) -> m.BriefingItem:
    """One briefing item (``operating.briefing``) as the API states it. A suspect total
    carries its reason; the app shows the reason, never the figure as fact."""
    oid, kind = item["object_id"], str(item.get("object_type") or "Object")
    facts = item.get("facts") or {}
    amount = facts.get("amount")
    action = item["next_action"]
    next_action = (trail.next_action if trail is not None and trail.next_action
                   else m.NextAction(text=clean_text(action["text"]), rule=action["rule"]))
    kind_words = clean_enum(kind, "object_type") or kind
    fallback_subject = f"{kind_words} record"
    amt_value = clean_money_amount(amount["value"]) if amount and amount.get("value") is not None else None
    lines_sum_val = (
        clean_money_amount(amount["disagree"]["lines_sum"])
        if amount and isinstance(amount.get("disagree"), Mapping)
        and amount["disagree"].get("lines_sum") is not None
        else None
    )
    return m.BriefingItem(
        rank=item["rank"], id=item["id"], event_type=item["event_type"],
        label=clean_alert_label(item["event_type"], item.get("label")),
        evidence=item.get("evidence") or "fixture-only",
        object_id=oid, object_type=kind,
        subject_label=clean_subject(labels.get(oid), fallback=fallback_subject, max_len=200),
        counterpart=clean_person(counterpart) or (clean_text(counterpart) or None),
        summary=clean_text(item.get("summary") or "", strip_email_noise=True),
        carried=carried_words(item.get("carried")),
        why=clean_text(item["why"], strip_email_noise=True) if item.get("why") else None,
        next_action=next_action,
        amount=m.Money(amount=amt_value or str(amount["value"]), currency=amount["currency"])
        if amount else None,
        amount_basis=amount.get("basis") if amount else None,
        amount_suspect=clean_text(amount["suspect"]) if amount and amount.get("suspect")
        else None,
        amount_disagree=bool(amount) and isinstance(amount.get("disagree"), Mapping),
        lines_sum=m.Money(amount=lines_sum_val or str(amount["disagree"]["lines_sum"]),
                          currency=amount["currency"])
        if amount and isinstance(amount.get("disagree"), Mapping)
        and amount["disagree"].get("lines_sum") is not None else None,
        orders_affected=facts.get("orders_affected"), days=facts.get("days"),
        acknowledged=bool(item.get("acknowledged")),
        new_since_visit=bool(item.get("new_since_visit")),
        new_this_generation=bool(item.get("new_this_generation")),
        first_seen=item.get("first_seen"), open_url=object_url(kind, oid),
        sources=[m.Source(system=system_of(h), handle=h, label=record_words(h),
                          open_url=open_url(h))
                 for h in (item.get("citations") or ()) if isinstance(h, str)],
        trail=trail, yours=bool(item.get("yours")),
        why_yours=yours_words(item.get("involvement") or ()))


# Why an item is the reader's (``operating.involvement``), in words.
YOURS_WORDS = {"your_mail": "Mentioned in your mail",
               "you_correspond": "Its {kind} writes to your mailbox"}


def yours_words(reasons: Iterable[Mapping[str, Any]]) -> list[str]:
    """Each distinct reason an item is the reader's, in words; an unknown reason is not
    stated (never guessed into words)."""
    out: list[str] = []
    for reason in reasons:
        if not isinstance(reason, Mapping):
            continue
        code, via = reason.get("reason"), str(reason.get("via") or "")
        if code == "named" and via:
            words = f"You are named as its {via.replace('_', ' ')}"
        elif code == "you_correspond":
            kind = via.split(":", 1)[0] or "counterpart"
            words = YOURS_WORDS[code].format(kind=re.sub(r"(?<!^)(?=[A-Z])", " ",
                                                         kind).lower())
        elif code in YOURS_WORDS:
            words = YOURS_WORDS[code]
        else:
            continue
        if words not in out:
            out.append(words)
    return out


SUGGESTION_TEST_PASSES = 3        # rounds of leaving out test records a card would show
SUGGESTION_TRAIL_SECONDS = 45.0   # every suggestion card's PO trail together
SUGGESTION_DRAFTS_READ = 2000     # late-order gaps read for their supplier drafts
SUGGESTIONS_NOTE = ("Nothing is sent until you review a draft and confirm it yourself; "
                    "it then goes from your own mailbox, or is recorded as a dry run while "
                    "sending is not enabled.")


def suggestion_source(raw: Any) -> Optional[m.Source]:
    """A card's source: as the reader's trail decided it, or (an inbox citation the reader
    sees the finding by) labelled from its handle."""
    if not isinstance(raw, Mapping) or not isinstance(raw.get("handle"), str):
        return None
    handle = raw["handle"]
    system = raw.get("system") if raw.get("system") in m.System.__args__ \
        else system_of(handle)
    return m.Source(system=system, handle=handle,
                    label=str(raw.get("label") or record_words(handle)),
                    open_url=raw.get("open_url") if raw.get("label") else open_url(handle))


def suggestion_model(card: Mapping[str, Any]) -> m.ProactiveSuggestion:
    """One ``operating.proactive_suggestions`` card as the API states it."""
    from operating import card_actions
    subject = card["subject"]
    impact = card.get("impact") or {}
    amount = impact.get("amount") or None
    eta = card.get("eta")
    draft = card.get("draft")
    facts = []
    if draft:
        for fact in draft.get("facts") or ():
            source = suggestion_source({"handle": fact.get("citation")})
            if source is not None:
                facts.append(m.DraftFact(fact=str(fact["fact"]), value=str(fact["value"]),
                                         source=source))
    return m.ProactiveSuggestion(
        id=card["id"], kind=card["kind"], event_types=list(card.get("event_types") or ()),
        rank=int(card.get("rank") or 0),
        headline=clean_text(card["headline"], strip_email_noise=True),
        subject=m.ObjectRef(object_id=subject["object_id"], type=subject["object_type"]
                            or "Object", label=clean_subject(subject["label"], fallback=subject["object_type"] or "Record"),
                            open_url=object_url(subject["object_type"] or "Object",
                                                subject["object_id"])),
        impact=m.SuggestionImpact(
            amount=m.Money(
                amount=clean_money_amount(amount["value"]) or str(amount["value"]),
                currency=str(amount["currency"]),
            )
            if amount and amount.get("currency") else None,
            amount_basis=amount.get("basis") if amount else None,
            amount_suspect=clean_text(amount["suspect"]) if amount and amount.get("suspect")
            else None, days_late=impact.get("days_late"),
            days_at_risk=impact.get("days_at_risk"),
            orders_at_risk=int(impact.get("orders_at_risk") or 0),
            orders=[m.ObjectRef(object_id=o["object_id"], type=o["object_id"].split(":")[0],
                                label=clean_text(o.get("label") or "Customer order"),
                                open_url=object_url(o["object_id"].split(":")[0],
                                                    o["object_id"]))
                    for o in impact.get("orders") or ()]),
        eta=m.SuggestionEta(answer=eta["answer"], when=eta.get("when"),
                            in_mail=bool(eta.get("in_mail")),
                            source=suggestion_source(eta.get("source")),
                            text=clean_text(eta.get("text") or "", strip_email_noise=True)) if eta else None,
        reasoning=[m.SuggestionReason(text=clean_text(r["text"], strip_email_noise=True),
                                      source=suggestion_source(r.get("source")))
                   for r in card.get("reasoning") or () if r.get("text")],
        action=m.NextAction(text=clean_text(card["action"]["text"]) or "Open the record.",
                            rule=str(card["action"].get("rule") or "suggestion")),
        draft=m.SuggestionDraft(
            channel=draft["channel"], audience=draft["audience"],
            to=m.DraftTo(name=clean_person(draft["to"].get("name")), address=draft["to"].get("address"),
                         basis=clean_text(draft["to"].get("basis") or ""),
                         source=suggestion_source(draft["to"].get("source"))),
            subject=clean_subject(draft["subject"], fallback="Follow-up"),
            body=draft["body"], facts=facts,
            review=draft.get("review")) if draft else None,
        confidence=m.SuggestionConfidence(**card["confidence"]),
        missing=[clean_text(x) for x in card.get("missing") or () if clean_text(x)],
        acknowledged=bool(card.get("acknowledged")), new=bool(card.get("new")),
        actions=[m.CardActionOffer(**o) for o in card_actions.offers(card)])


def briefing_model(found: Mapping[str, Any], top: list[m.BriefingItem], *, digest: str,
                   tests_hidden: int = 0) -> m.Briefing:
    """The briefing (``operating.briefing.build``) as the API states it."""
    from operating import briefing as bf

    status = found.get("status")
    routing = found.get("routing") or {}
    return m.Briefing(
        status="read" if status == "read" else "not_configured"
        if status in ("not_configured", "not_scheduled") else "unavailable",
        count=int(found.get("count") or 0),
        counts_exact=bool(found.get("counts_exact", True)),
        new_since_visit=int(found.get("new_since_visit") or 0),
        since_visit=bool(found.get("since_visit")),
        acknowledged=int(found.get("acknowledged") or 0),
        late_total=found.get("late_total"),
        other_teams=int(found.get("other_teams") or 0), hidden=found.get("hidden"),
        hidden_by_type=dict(found.get("hidden_by_type") or {}),
        rank_basis=str(found.get("rank_basis") or bf.RANK_BASIS),
        routing_basis=str(routing.get("basis") or "unknown"),
        departments=list(routing.get("departments") or ()),
        last_visit=found.get("last_visit"), as_of=found.get("as_of"),
        complete=bool(found.get("complete")), more=bool(found.get("more")), items=top,
        refreshing=bool(found.get("refreshing")),
        checked_on=found.get("checked_on") if found.get("refreshing")
        and isinstance(found.get("checked_on"), str) else None,
        groups=[m.BriefingGroup(**{
            **{k: g[k] for k in ("event_type", "count", "in_top", "new_since_visit", "acknowledged")},
            "label": clean_alert_label(g["event_type"], g.get("label")),
        }) for g in found.get("groups") or ()],
        checks=[m.BriefingCheck(
            check=c.get("check"), label=clean_text(c.get("label")),
            status=c.get("status"), words=clean_text(c.get("words")),
        ) for c in found.get("checks") or ()],
        types=[m.BriefingEventType(**{
            **{k: t.get(k) for k in (
                "event_type", "status", "found_live", "first_found",
                "checked", "judged", "judged_share")},
            "label": clean_alert_label(t.get("event_type"), t.get("label")),
            "words": clean_text(t.get("words")),
        }) for t in found.get("types") or ()],
        today=clean_text(found.get("today") or bf.today_line(found)),
        carried=int(found.get("carried") or 0),
        data_notes=[clean_text(n.get("words")) for n in found.get("data_notes") or ()
                    if isinstance(n, Mapping) and clean_text(n.get("words"))],
        data_note_items=[m.BriefingDataNote(check=str(n["check"]), reason=str(n["reason"]),
                                            count=int(n["count"]), words=clean_text(n["words"]),
                                            scope=n.get("scope") is True)
                         for n in found.get("data_notes") or ()
                         if isinstance(n, Mapping) and isinstance(n.get("count"), int)],
        digest_preview=digest, test_records_hidden=hidden_words(tests_hidden),
        yours=int(found.get("yours") or 0),
        involvement=involvement_state(found.get("involvement")))


def carried_words(carried: Any) -> Optional[str]:
    """A carried briefing item's words (``proactive_engine.CARRIED_WORDS``); None when
    the item is current."""
    from operating import briefing as bf
    from operating.proactive_engine_inbox import CARRIED_WORDS

    if not isinstance(carried, Mapping) or carried.get("reason") not in CARRIED_WORDS:
        return None
    return CARRIED_WORDS[carried["reason"]].format(
        checked=bf.at_words(carried.get("last_checked")) or "an earlier run",
        failed=bf.at_words(carried.get("check_failed_at")) or "the last run")


def involvement_state(found: Any) -> str:
    """``Briefing.involvement`` from ``Involvement.out()`` (None: no identity)."""
    if not isinstance(found, Mapping):
        return "no_identity"
    return "checked" if found.get("complete") else "partly_checked"
