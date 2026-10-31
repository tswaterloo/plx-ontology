"""The answer's evidence trail ("How we know") as the app contract's ``EvidenceTrail``.

``operating.evidence_trail`` builds the steps from what the generic tools returned as the
reader; this module is the app boundary: every step's cited record is decided again for
this reader (``view.access.decide``, as the answer's facts are), a step the reader may not
open is dropped and counted (never named), a message's sender and the next action's
contact are shown only when the people directory decides the address may be shown
(``operating.people_graph.PeopleDirectory``: an external address, or a tenant person the
exclusion clears; anything undecided is not shown), and nothing is sent or written.

Which trail an answer gets (``for_answer``): a purchase order the question is about (a
found root, or a late PO the question names) gets its lateness trail; a customer order,
assembly or manufacturing order, or customer asked about with a materials or PO question
gets the reverse impact walk; otherwise the answer's dated, gate-passed claims, when there
are two or more.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from operating import evidence_trail as et

from . import models as m

MAX_PO_SUBJECTS = 2
PO_SECONDS = 20.0          # one PO trail's budget inside an answer
MATERIALS_SECONDS = 35.0
# A question about the materials or purchase orders behind an order.
MATERIALS_QUESTION = re.compile(
    r"\b(?:materials?|components?|parts?|POs?|purchase orders?|supplies|supplier|vendor|"
    r"what(?:'s| is) (?:late|holding)|late)\b", re.I)
SHOWN_PERSON = ("external", "person")


def _ref(oid: str, label: Optional[str], url: Callable[[str, str], str]) -> m.ObjectRef:
    kind = oid.split(":", 1)[0] or "Object"
    return m.ObjectRef(object_id=oid, type=kind, label=(label or _words(kind))[:200],
                       open_url=url(kind, oid))


def _words(kind: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).lower().capitalize()


class People:
    """The people directory of the reader's runtime, asked once per answer; without one
    no name is shown (fail closed)."""

    def __init__(self, view: Any) -> None:
        self.view = view
        self._dir: Any = None
        self._read = False

    def shown(self, text: Optional[str]) -> bool:
        address = et.address_of(text)
        if address is None:
            return False
        if not self._read:
            self._read = True
            try:
                from operating.people_graph import PeopleDirectory
                runtime = getattr(self.view, "sources", None)
                self._dir = PeopleDirectory.from_runtime(runtime) if runtime is not None \
                    else None
            except Exception:  # noqa: BLE001 - no directory: no names
                self._dir = None
        if self._dir is None:
            return False
        try:
            return self._dir.address(address).state in SHOWN_PERSON
        except Exception:  # noqa: BLE001 - undecided: not shown
            return False


class Mapper:
    """Steps to the contract, each decided for this reader (one ``decide`` per answer)."""

    def __init__(self, backend: Any, view: Any, *, object_url: Callable[[str, str], str],
                 host_schemes: Sequence[str] = (), open_url: Callable[[str], str]) -> None:
        self.backend, self.view = backend, view
        self.url, self.open_url, self.host_schemes = object_url, open_url, tuple(host_schemes)
        self.people = People(view)
        self.hidden = 0

    def decide(self, handles: Iterable[str]) -> dict[str, Any]:
        handles = [h for h in dict.fromkeys(handles) if isinstance(h, str)]
        if not handles:
            return {}
        return self.view.access.decide(handles)

    def source(self, handle: str, decided: Mapping[str, Any]) -> Optional[m.Source]:
        """The step's source when this reader may open it, else None (counted)."""
        marker = decided.get(handle, {"reason": "undecided"})
        if marker is not None and not (handle.startswith(self.host_schemes)
                                       and marker.get("reason") == "item_refused"):
            return None
        source = self.backend._source(self.view, handle,
                                      openable=None if marker is None else False)
        if source.open_url is None and handle.startswith(self.host_schemes):
            # opened through the reader's host, which decides it again on open
            source = source.model_copy(update={"open_url": self.open_url(handle)})
        return source

    def steps(self, raw: Sequence[Mapping[str, Any]], decided: Mapping[str, Any]
              ) -> tuple[list[m.TrailStep], int]:
        out, hidden = [], 0
        refused: set[str] = set()   # promise dates whose record this reader can't open
        for item in raw:
            handle = item.get("handle")
            if handle is None and item.get("kind") == "verdict":
                source = None   # an undetermined verdict cites no date
            else:
                source = self.source(handle, decided) if isinstance(handle, str) else None
                if source is None:
                    hidden += 1     # a step this reader can't open: counted, never named
                    if item.get("kind") in ("confirmation", "revision") and item.get("date"):
                        refused.add(str(item["date"]))
                    continue
            title = str(item["title"])
            if item.get("kind") == "revision" and item.get("date"):
                earlier = re.match(r"Promise revised from (\S+) to ", title)
                if earlier and earlier.group(1) in refused:
                    title = f"Promise revised to {item['date']}"
            sender = item.get("sender")
            out.append(m.TrailStep(kind=item["kind"], at=item.get("at"),
                                   title=title[:300],
                                   detail=item.get("detail"), source=source,
                                   check=str(item.get("check") or "read_from_record"),
                                   sender=sender if sender and self.people.shown(sender)
                                   else None))
        return out, hidden

    def po(self, raw: Mapping[str, Any]) -> m.PoTrail:
        handles = [s.get("handle") for s in raw.get("steps") or ()]
        eta = raw.get("eta") or None
        action = raw.get("next_action") or None
        handles += [(eta or {}).get("handle"), (action or {}).get("contact_handle")]
        decided = self.decide(h for h in handles if isinstance(h, str))
        steps, hidden = self.steps(raw.get("steps") or (), decided)
        hidden += int(raw.get("hidden") or 0)
        eta_model = None
        if eta:
            handle = eta.get("handle")
            source = self.source(handle, decided) if isinstance(handle, str) else None
            if isinstance(handle, str) and source is None:
                # the promise's record is refused to this reader: neither it nor its date
                eta_model = m.EtaCheck(state="withheld", text="A vendor confirmation "
                                       "exists that you can't open, so its date is not "
                                       "shown.")
            else:
                eta_model = m.EtaCheck(state=eta["state"], text=str(eta["text"])[:300],
                                       source=source)
        action_model = None
        if action:
            handle = action.get("contact_handle")
            contact_source = self.source(handle, decided) if isinstance(handle, str) else None
            contact = action.get("contact")
            refused = {str(x.get("date")) for x in raw.get("steps") or ()
                       if x.get("kind") in ("confirmation", "revision") and x.get("date")
                       and isinstance(x.get("handle"), str)
                       and self.source(x["handle"], decided) is None}
            if eta and eta.get("date") and isinstance(eta.get("handle"), str) \
                    and self.source(eta["handle"], decided) is None:
                refused.add(str(eta["date"]))
            shown = bool(contact) and contact_source is not None and self.people.shown(contact)
            text = str(action["text"])
            for day in refused:   # a promise this reader can't open: its date is not said
                text = text.replace(f" ({day})", "").replace(f", to {day}", "") \
                    .replace(day, "the vendor's date")
            if shown:
                text = text.replace("the vendor", f"the vendor ({contact})", 1)
            action_model = m.NextAction(text=text[:400], rule=str(action["rule"]),
                                        contact=contact if shown else None,
                                        contact_source=contact_source if shown else None)
        self.hidden += hidden
        return m.PoTrail(
            subject=_ref(raw["subject"], raw.get("label"), self.url), state=raw.get("state"),
            days_late=raw.get("days_late"), verdict=raw.get("verdict"), eta=eta_model,
            steps=steps, hidden_steps=hidden, may_hide_more=bool(raw.get("may_hide")),
            next_action=action_model,
            not_found=[m.NotFound(what=str(g.get("what"))[:300],
                                  where_it_would_live=str(g.get("where"))[:300])
                       for g in raw.get("gaps") or ()][:5])


def hidden_words(count: int, maybe: bool) -> Optional[str]:
    if count:
        more = " There may be more." if maybe else ""
        return (f"{count} more step{'s' if count != 1 else ''} in places you can't "
                f"open.{more}")
    if maybe:
        return "Some steps may be in places you can't open."
    return None


def late_list_step(mapper: Mapper, po: Any, run_at: Optional[str]) -> Optional[m.TrailStep]:
    """The reader's late list's own verdict for this PO (the list Home shows, as of its
    run), cited by its evidence: shown beside a live verdict that differs (say the
    captures are stale now), never in its place."""
    handles = [s.handle for s in getattr(po, "sources", None) or ()]
    decided = mapper.decide(handles)
    source = next((got for got in (mapper.source(h, decided) for h in handles) if got), None)
    if source is None:
        return None
    when = f" as of {run_at[:10]}" if run_at else ""
    return m.TrailStep(kind="verdict", at=run_at[:10] if run_at else None,
                       title=f"On your late list{when}: {po.days_late} days late. "
                             f"{str(po.reason).rstrip('.')}."[:300],
                       detail="The late list's own check when it last ran; the verdict "
                              "below is read now.", source=source,
                       check="computed_from_cited_records")


def po_trails(mapper: Mapper, view: Any, oids: Sequence[str],
              labels: Mapping[str, str] | None = None, seconds: float = PO_SECONDS,
              late: Mapping[str, tuple[Any, Optional[str]]] | None = None
              ) -> Optional[m.EvidenceTrail]:
    call = lambda name, args: view._call(name, dict(args))  # noqa: E731
    trails = []
    for oid in list(dict.fromkeys(oids))[:MAX_PO_SUBJECTS]:
        raw = et.po_trail(call, oid, seconds=seconds, label=(labels or {}).get(oid))
        if raw.get("state") is None and not raw.get("steps"):
            continue
        trail = mapper.po(raw)
        listed = (late or {}).get(oid)
        if listed is not None and trail.state != "late":
            extra = late_list_step(mapper, *listed)
            if extra is not None:
                at = next((i for i, s in enumerate(trail.steps) if s.kind == "verdict"),
                          len(trail.steps))
                trail.steps.insert(at, extra)
        trails.append(trail)
    if not trails:
        return None
    maybe = any(t.may_hide_more for t in trails)
    title = "How we know" if len(trails) > 1 else (
        f"How we know: {trails[0].subject.label}")
    return m.EvidenceTrail(kind="purchase_order", title=title[:200], trails=trails,
                           hidden_steps=mapper.hidden,
                           hidden_message=hidden_words(mapper.hidden, maybe))


MAX_STARTS = 2


def materials_title(rows: Sequence[m.MaterialRow], shown: int) -> str:
    """The table's heading: neutral, counting every row the walk found (purchase orders
    and late ones over all of them), and how many of them the table shows."""
    if not rows:
        return "From the recorded links: no open purchase order found"
    pos = {r.po.object_id for r in rows}
    late = {r.po.object_id for r in rows if r.state == "late"}
    text = (f"From the recorded links: {len(pos)} purchase order{'s' if len(pos) != 1 else ''}"
            f" for this order's materials, {len(late)} late")
    more = len(rows) - shown
    return text + (f" ({shown} rows shown, {more} more not shown)" if more > 0 else "")


def materials_trail(mapper: Mapper, view: Any, starts: Sequence[tuple[str, ...]],
                    seconds: float = MATERIALS_SECONDS) -> Optional[m.EvidenceTrail]:
    """The reverse walk from each start (at most ``MAX_STARTS``: an identifier naming the
    order in two systems is walked from each record, never merged), its rows, typed gaps
    and the late POs' trails."""
    call = lambda name, args: view._call(name, dict(args))  # noqa: E731
    deadline = time.monotonic() + seconds
    rows: list[m.MaterialRow] = []
    trails: list[m.PoTrail] = []
    gaps: list[m.TrailGap] = []
    late_pos: set[str] = set()
    checked: set[str] = set()
    hidden, maybe = 0, False
    for start in list(dict.fromkeys(starts))[:MAX_STARTS]:
        root, root_type = start[0], start[1]
        left = deadline - time.monotonic()
        if left <= 0:
            break
        raw = et.materials(call, root, root_type, seconds=left)
        labels = dict(raw.get("labels") or {})
        if len(start) > 2 and start[2]:
            labels.setdefault(root, str(start[2]))   # the start as the question names it
        rows += [m.MaterialRow(order=_ref(r["order"], labels.get(r["order"]), mapper.url)
                               if r.get("order") else None,
                               assembly=_ref(r["assembly"], labels.get(r["assembly"]),
                                             mapper.url),
                               item=_ref(r["item"], labels.get(r["item"]), mapper.url),
                               po=_ref(r["po"], labels.get(r["po"]), mapper.url),
                               state=r.get("state"), days_late=r.get("days_late"),
                               verdict=r.get("verdict"))
                 for r in raw.get("rows") or ()]
        trails += [mapper.po(t) for t in raw.get("trails") or ()]
        gaps += [m.TrailGap(reason=str(g["reason"]), state=str(g["state"]),
                            what=str(g["what"])[:300],
                            where_it_would_live=str(g["where"])[:300])
                 for g in raw.get("gaps") or ()]
        late_pos.update(raw.get("late_pos") or ())
        checked.update(raw.get("checked_pos") or ())
        hidden += int(raw.get("hidden") or 0)
        maybe = maybe or bool(raw.get("may_hide"))
    seen: set[tuple[str, str]] = set()
    rows = [r for r in rows if (r.item.object_id, r.po.object_id) not in seen
            and not seen.add((r.item.object_id, r.po.object_id))]
    said: set[tuple[str, str]] = set()
    gaps = [g for g in gaps if (g.reason, g.what) not in said
            and not said.add((g.reason, g.what))]
    seen_po: set[str] = set()
    trails = [t for t in trails if t.subject.object_id not in seen_po
              and not seen_po.add(t.subject.object_id)]
    late, checked_n = len(late_pos), len(checked)
    if not rows and not gaps:
        return None
    shown = rows[:et.MAX_ROWS]
    title = materials_title(rows, len(shown))
    maybe = maybe or any(t.may_hide_more for t in trails)
    hidden += mapper.hidden
    return m.EvidenceTrail(kind="materials", title=title, trails=trails[:et.MAX_PO_TRAILS],
                           materials=shown, gaps=gaps[:12], hidden_steps=hidden,
                           hidden_message=hidden_words(hidden, maybe))


def claims_trail(mapper: Mapper, claims: Sequence[Mapping[str, Any]]
                 ) -> Optional[m.EvidenceTrail]:
    raw = et.claims_trail(claims)
    if len(raw) < 2:
        return None
    decided = mapper.decide(s["handle"] for s in raw)
    steps, hidden = mapper.steps(raw, decided)
    if len(steps) < 2:
        return None
    return m.EvidenceTrail(kind="claims", title="How we know", steps=steps,
                           hidden_steps=hidden, hidden_message=hidden_words(hidden, False))


def for_answer(backend: Any, view: Any, question: str, roots: Sequence[Mapping[str, Any]],
               late_pos: Sequence[tuple[Any, Optional[str]]],
               claims: Sequence[Mapping[str, Any]], *,
               ambiguous: Sequence[Mapping[str, Any]] = (),
               object_url: Callable[[str, str], str], open_url: Callable[[str], str],
               host_schemes: Sequence[str]) -> Optional[m.EvidenceTrail]:
    """The answer's trail (module doc), or None. Never raises: a trail that fails is left
    out and the answer stands."""
    mapper = Mapper(backend, view, object_url=object_url, open_url=open_url,
                    host_schemes=host_schemes)
    started = time.monotonic()
    try:
        mention = {str(r.get("object_id")): str(r.get("mention") or "")[:120] for r in roots
                   if isinstance(r, Mapping)}
        typed = [(str(r.get("object_id")), str(r.get("object_type") or
                                                 str(r.get("object_id")).split(":", 1)[0]))
                 for r in roots if isinstance(r, Mapping) and isinstance(r.get("object_id"),
                                                                         str)]
        late = {po.object_id: (po, run_at) for po, run_at in late_pos}
        pos = [oid for oid, kind in typed if kind == "PurchaseOrder"] + list(late)
        starts = [(oid, kind, mention.get(oid, "")) for oid, kind in typed
                  if kind in et.START_TYPES]
        if not starts and not pos:
            # one identifier naming the order in several records (say its FileMaker and
            # Sage copies): each record of a start type is walked, none chosen
            starts = [(str(c["object_id"]), str(c.get("object_type")),
                       str(item.get("mention") or "")[:120])
                      for item in ambiguous for c in item.get("candidates") or ()
                      if isinstance(c, Mapping) and c.get("object_type") in et.START_TYPES
                      and isinstance(c.get("object_id"), str)]
        if starts and MATERIALS_QUESTION.search(question or "") and not pos:
            got = materials_trail(mapper, view, starts)
            if got is not None:
                return got
        if pos:
            return po_trails(mapper, view, pos, late=late)
        return claims_trail(mapper, claims)
    except Exception as error:  # noqa: BLE001 - the trail is extra; the answer stands
        logging.getLogger("operator_api.trail").warning(
            "trail not built: %s after %.1fs", type(error).__name__,
            time.monotonic() - started)
        return None


def for_page(backend: Any, view: Any, oid: str, label: str, *,
             object_url: Callable[[str, str], str], open_url: Callable[[str], str],
             host_schemes: Sequence[str],
             late: Sequence[tuple[Any, Optional[str]]] = ()) -> Optional[m.EvidenceTrail]:
    """A purchase order page's trail (the same as the answer's), or None."""
    mapper = Mapper(backend, view, object_url=object_url, open_url=open_url,
                    host_schemes=host_schemes)
    try:
        return po_trails(mapper, view, [oid], {oid: label},
                         late={po.object_id: (po, run_at) for po, run_at in late})
    except Exception as error:  # noqa: BLE001 - the page stands without it
        logging.getLogger("operator_api.trail").warning("page trail not built: %s",
                                                        type(error).__name__)
        return None
