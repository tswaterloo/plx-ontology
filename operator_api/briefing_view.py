"""The briefing's payload: briefing items and suggestion cards as the app shows them,
their questions, ageing, test-record filtering and involvement words.

Moved from ``operator_api.backend`` by concern (no behaviour change); every name is
re-exported there."""

from __future__ import annotations

from datetime import date
import re
from typing import Any, Iterable, Mapping, Optional

from . import models as m
from .test_records import hidden_words
from .serving import (object_url, open_url, record_words, system_of)
from .answers import (aged_to_today)


def _answer_kept(answer: Any) -> dict[str, Any]:
    """What feedback on an answer attaches: its words, facts (with citations) and subjects,
    as served to its reader (bounded)."""
    try:
        return {"answer": (answer.direct_answer or "")[:4000],
                "conversation_id": answer.conversation_id,
                "as_of": answer.freshness.as_of if answer.freshness else None,
                "facts": [{"text": f.text[:600], "sources": [
                    {"system": s.system, "handle": s.handle, "label": s.label}
                    for s in f.sources[:8]]} for f in answer.facts[:30]],
                "subjects": [{"type": o.type, "object_id": o.object_id, "label": o.label}
                             for o in answer.subjects[:10]]}
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
                   else m.NextAction(text=action["text"][:400], rule=action["rule"]))
    return m.BriefingItem(
        rank=item["rank"], id=item["id"], event_type=item["event_type"],
        label=item["label"], evidence=item.get("evidence") or "fixture-only",
        object_id=oid, object_type=kind,
        subject_label=labels.get(oid) or f"{kind} record",
        counterpart=counterpart,
        summary=str(item.get("summary") or "")[:300],
        carried=carried_words(item.get("carried")),
        why=str(item["why"])[:600] if item.get("why") else None,
        next_action=next_action,
        amount=m.Money(amount=amount["value"], currency=amount["currency"])
        if amount else None,
        amount_basis=amount.get("basis") if amount else None,
        amount_suspect=str(amount["suspect"])[:200] if amount and amount.get("suspect")
        else None,
        amount_disagree=bool(amount) and isinstance(amount.get("disagree"), Mapping),
        lines_sum=m.Money(amount=str(amount["disagree"]["lines_sum"]),
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
                 for h in (item.get("citations") or ())[:3] if isinstance(h, str)],
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
                    label=str(raw.get("label") or record_words(handle))[:200],
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
                facts.append(m.DraftFact(fact=str(fact["fact"]), value=str(fact["value"])[:200],
                                         source=source))
    return m.ProactiveSuggestion(
        id=card["id"], kind=card["kind"], event_types=list(card.get("event_types") or ()),
        rank=int(card.get("rank") or 0), headline=str(card["headline"])[:300],
        subject=m.ObjectRef(object_id=subject["object_id"], type=subject["object_type"]
                            or "Object", label=str(subject["label"])[:200],
                            open_url=object_url(subject["object_type"] or "Object",
                                                subject["object_id"])),
        impact=m.SuggestionImpact(
            amount=m.Money(amount=amount["value"], currency=str(amount["currency"]))
            if amount and amount.get("currency") else None,
            amount_basis=amount.get("basis") if amount else None,
            amount_suspect=str(amount["suspect"])[:200] if amount and amount.get("suspect")
            else None, days_late=impact.get("days_late"),
            days_at_risk=impact.get("days_at_risk"),
            orders_at_risk=int(impact.get("orders_at_risk") or 0),
            orders=[m.ObjectRef(object_id=o["object_id"], type=o["object_id"].split(":")[0],
                                label=str(o.get("label") or "Customer order")[:200],
                                open_url=object_url(o["object_id"].split(":")[0],
                                                    o["object_id"]))
                    for o in impact.get("orders") or ()]),
        eta=m.SuggestionEta(answer=eta["answer"], when=eta.get("when"),
                            in_mail=bool(eta.get("in_mail")),
                            source=suggestion_source(eta.get("source")),
                            text=str(eta.get("text") or "")[:300]) if eta else None,
        reasoning=[m.SuggestionReason(text=str(r["text"])[:600],
                                      source=suggestion_source(r.get("source")))
                   for r in card.get("reasoning") or () if r.get("text")],
        action=m.NextAction(text=str(card["action"]["text"])[:400] or "Open the record.",
                            rule=str(card["action"].get("rule") or "suggestion")),
        draft=m.SuggestionDraft(
            channel=draft["channel"], audience=draft["audience"],
            to=m.DraftTo(name=draft["to"].get("name"), address=draft["to"].get("address"),
                         basis=str(draft["to"].get("basis") or ""),
                         source=suggestion_source(draft["to"].get("source"))),
            subject=draft["subject"], body=draft["body"], facts=facts,
            review=draft.get("review")) if draft else None,
        confidence=m.SuggestionConfidence(**card["confidence"]),
        missing=[str(x)[:300] for x in card.get("missing") or ()],
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
        groups=[m.BriefingGroup(**{k: g[k] for k in ("event_type", "label", "count", "in_top",
                                                     "new_since_visit", "acknowledged")})
                for g in found.get("groups") or ()],
        checks=[m.BriefingCheck(**{k: c.get(k) for k in ("check", "label", "status",
                                                         "words")})
                for c in found.get("checks") or ()],
        types=[m.BriefingEventType(**{k: t.get(k) for k in (
            "event_type", "label", "status", "words", "found_live", "first_found")})
               for t in found.get("types") or ()],
        today=str(found.get("today") or bf.today_line(found)),
        carried=int(found.get("carried") or 0),
        data_notes=[str(n.get("words"))[:300] for n in found.get("data_notes") or ()
                    if isinstance(n, Mapping)][:20],
        digest_preview=digest, test_records_hidden=hidden_words(tests_hidden),
        yours=int(found.get("yours") or 0),
        involvement=involvement_state(found.get("involvement")))


def carried_words(carried: Any) -> Optional[str]:
    """A carried briefing item's words (``proactive_engine.CARRIED_WORDS``); None when
    the item is current."""
    from operating import briefing as bf
    from operating.proactive_engine import CARRIED_WORDS

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
