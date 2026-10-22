"""Decision evidence: the recorded decisions about one object, in time order (G-11).

The engine of the ``plx.investigate`` plan ``ontology/plans/decision_evidence.json`` (family
F11, evidence kind ``compute``). The plan reaches the records that hold a decision about its
root (Mission Control approval gates and task events) and records around it (mail naming the
root) and reads them with the generic primitives; this engine only puts what the plan cited
in time order. It reads no source by itself and decides no reason: a decision is a record
the plan reached and read, with its own cited time, actor, outcome and recorded basis.

``engine_args``:

* ``records``: one entry per kind of decision record, ``{"label", "time", "actor",
  "outcome", "basis"}``; each role lists ``property`` evidence items of one object type
  listed before the compute item (``basis`` is optional: a record kind the ontology binds no
  basis for). Per reached object the first listed item it carries is used. Optional, for a
  kind of record that can be blank or pending (a printed approval form):
  ``"decided_by_any": [roles]`` (of ``actor``/``outcome``): a record states a decision only
  when one of these roles carries a value; ``"open_outcomes": [words]``: an outcome whose
  text holds one of these words (``pending``, ``awaiting``) is no decision outcome. A record
  that states no decision is the gap ``decision_not_made_on_record`` (``absent_in_source``,
  the record answers that nothing was decided there), never a decision dated by a printed
  date. A basis is what
  the decider recorded (Mission Control: the gate's ``note``, PLX_MC@2f653cf
  ``approvals.ts``), never the requester's ``reason`` for asking.
* ``context`` (optional): kinds of record that are cited context, never decisions,
  ``{"label", "time", "cites"}`` with the same item rules. A mail naming the root says who
  wrote what when; its sender decided nothing and its subject is no outcome, so it is never
  counted as a decision.

Review decisions (``ontology/reviews.py``) reach a plan as the links they project: a link
the walk crossed whose reason is ``person_reviewed`` (tier ``reviewed`` or ``contradicted``)
or ``person_allocated`` (an allocation). ``plx.around`` puts on such an edge the review
behind it (``review``: ``operating.ontology_tools_traverse.link_review``) when the reader
may read that review; the engine re-reads it from the review store for the investigating
actor (``reviews``, the host's ``TraverseTools.review``) and lists it as a decision (time:
when it was recorded; actor: the reviewer; outcome: the decision; basis: the evidence it
cites) only when the re-read is the same review at the same instant and it is in force.
Otherwise it is a typed gap naming the link (its id carries the review id), never a
decision placed in time by guess:

* ``review_decision_withheld`` (``inaccessible``): the review cites a record this reader
  may not read; its reviewer and time are not shown and it is not absence;
* ``review_decision_not_served`` (``unknown``): the host serves no review for the link (no
  review store, or one that does not hold it as the projection recorded it);
* ``decision_time_unverified``: no re-read, or one that differs from the edge;
* ``review_retracted``: a later review withdrew it, so it is no decision in force;
* ``review_superseded``: a later decision on the pair is in force; the gap names it (or says
  it is withheld from this reader);
* ``review_not_in_force``: its cited evidence changed, left its source or is not in the
  store (``ontology.reviews.state_at``).

Rules:

* A decision is listed only with a time re-read from its citation for the investigating
  actor (``resolver``) that states the same instant; else it is a typed gap
  (``decision_time_unverified``), never listed on an unchecked time. A record without a
  decision time (an approval gate still pending) is a ``decision_time_not_recorded`` gap.
* Order: by instant. A bare date is its whole day in a time zone the record does not
  state, so a decision on a date and one at an instant some UTC offset (-12:00 to +14:00)
  puts on that day (``2026-10-01T23:30:00-04:00`` is on the 1st there, the 2nd in UTC)
  have no known order, nor have two decisions at one instant (however each offset writes
  it), nor two bare dates of one day: ``decision_order_unknown`` names their entries and
  records, never ordered by guess (two different days are ordered by date).
* A decision without an actor, or without a recorded basis, keeps its place with a typed gap
  (``decision_actor_not_recorded``, ``decision_basis_not_recorded``): ``absent_in_source``
  only when the cited field was read and is empty (none or blank text), else ``unknown``.
  A reason is never inferred from another record (a mail that names the root is not the
  basis of an approval, nor the request's reason the basis of its decision).
* Several ticked outcome boxes on one record (approved and rejected) are the gap
  ``decision_outcome_conflict`` naming each; the decision keeps its place citing them all,
  never one picked for it.
* Records are never merged: a decision Mission Control records twice (the gate and its task's
  ``approval.decided`` event) is two cited records.
* One review projects one link per stretch its evidence was held: it is one decision.
* Nothing found (no listed decision, no reviewed link) is the typed gap
  ``no_recorded_decision`` (``unknown``: what this reader may not read stays withheld in the
  primitives' own gaps, so it never reads as absence).

Entries: ``<name>_<n>`` per decision in time order (value: its cited time; ``inputs``: the
answer entries it rests on; ``basis``: the record), then ``<name>_context_<n>`` per context
record (value: its re-read time, else none, after the timed ones), then ``<name>`` (how many
decisions were listed; none when no decision was found).
Steps name entries and objects only, never source text.
Evidence label of fixture runs: ``isolated-fixture``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Sequence

from ontology.document_fields import CHECKED
from operating.cases import _field

ENGINE = "decision_evidence"
ROLES = ("time", "actor", "outcome", "basis")
REQUIRED_ROLES = ("time", "actor", "outcome")
CONTEXT_ROLES = ("time", "cites")
RECORD_OPTIONS = ("decided_by_any", "open_outcomes")
REVIEW_FEATURES = ("person_reviewed", "person_allocated")
MAX_STEP_CHARS = 400
_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

Resolver = Callable[[str, Any], Any]


def _names(value: Any) -> list[str] | None:
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return value
    return None


def plan_check(args: Mapping[str, Any], doc: Mapping[str, Any], index: int,
               schema: Any) -> list[str]:
    """Problems with ``engine_args`` (module doc): every role lists property evidence items
    listed before the compute item, all of one object type per record kind."""
    if not isinstance(args, Mapping):
        return ["engine_args must be an object"]
    out: list[str] = [f"unknown engine argument {k}"
                      for k in set(args) - {"records", "context"}]
    records = args.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= 8:
        return out + ["records must list 1-8 record kinds"]
    context = args.get("context", [])
    if not isinstance(context, list) or len(context) > 8:
        return out + ["context must list 0-8 record kinds"]
    items = {e["name"]: e for e in doc["evidence"][:index]}
    labels: set[str] = set()
    kinds = [(r, ROLES, REQUIRED_ROLES) for r in records] + [
        (r, CONTEXT_ROLES, CONTEXT_ROLES) for r in context]
    for record, roles, required in kinds:
        if not isinstance(record, Mapping) or not _LABEL.match(str(record.get("label"))):
            out.append("every record needs a label (lower_snake)")
            continue
        label = record["label"]
        if label in labels:
            out.append(f"{label}: duplicate label")
        labels.add(label)
        options = set(RECORD_OPTIONS) if roles is ROLES else set()
        out += [f"{label}: unknown role {r}"
                for r in set(record) - {"label", *roles, *options}]
        if "decided_by_any" in record and (
                not isinstance(record["decided_by_any"], list) or not record["decided_by_any"]
                or not set(record["decided_by_any"]) <= {"actor", "outcome"}):
            out.append(f"{label}: decided_by_any must list actor and/or outcome")
        if "open_outcomes" in record and (
                not isinstance(record["open_outcomes"], list) or not record["open_outcomes"]
                or not all(isinstance(w, str) and w.strip() for w in record["open_outcomes"])):
            out.append(f"{label}: open_outcomes must list words")
        types: set[str] = set()
        for role in roles:
            if role not in record and role not in required:
                continue
            names = _names(record.get(role))
            if names is None:
                out.append(f"{label}: role {role} must list evidence names")
                continue
            for name in names:
                item = items.get(name)
                if item is None or item["kind"] != "property":
                    out.append(f"{label}: {role} {name} is not a property evidence item "
                               "listed before it")
                else:
                    types.add(item["object_type"])
        if len(types) > 1:
            out.append(f"{label}: roles name several object types {sorted(types)}")
    return out


def _blank(value: Any) -> bool:
    """Whether a cited value records nothing: none, or text (plain or untrusted) that is
    blank. An empty note is no basis."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        value = value.get("untrusted_text")
    return value is None or (isinstance(value, str) and not value.strip())


def _open(value: Any, record: Mapping[str, Any]) -> bool:
    """Whether an outcome's text says the decision is still open (``open_outcomes``): the
    text opens with one of the words, so "Pending approval" is open and "Approved with open
    points" is a decided outcome."""
    if isinstance(value, Mapping) and "untrusted_text" in value:
        value = value.get("untrusted_text")
    words = [w.lower() for w in record.get("open_outcomes", ())]
    return isinstance(value, str) and any(
        re.match(rf"\W*{re.escape(w)}\b", value.lower()) for w in words)


# The widest UTC offsets in use: a bare date's day starts at +14:00 and ends at -12:00.
_EARLIEST, _LATEST = dt.timedelta(hours=14), dt.timedelta(hours=12)


def _unordered(one: tuple[dt.datetime, dt.datetime], other: tuple[dt.datetime, dt.datetime]
               ) -> bool:
    """Whether two decision times (``_instant`` spans) have no known order: two bare dates
    of one day, a bare date and an instant some UTC offset puts on that day, or two
    timestamps of one instant."""
    bare = [span for span in (one, other) if span[0] != span[1]]
    if len(bare) == 2:
        return one[0] == other[0]
    if not bare:
        return one[0] == other[0]
    (start, end), moment = bare[0], (other if bare[0] is one else one)[0]
    return start - _EARLIEST <= moment < end + _LATEST


def _joined(names: Sequence[str], limit: int) -> str:
    """``names`` joined: as many whole names as fit in ``limit``, then how many more."""
    text = ", ".join(names)
    if len(text) <= limit:
        return text
    shown: list[str] = []
    for item in names:
        if len(", ".join([*shown, item])) + 16 > limit:
            break
        shown.append(item)
    return f"{', '.join(shown)} and {len(names) - len(shown)} more"


def _instant(text: Any) -> tuple[dt.datetime, dt.datetime] | None:
    """The interval a cited time stands for: a timestamp with an offset is a point, a bare
    date its whole day; anything else (no offset, unparsable) is unreadable."""
    if not isinstance(text, str):
        return None
    try:
        if _DATE.match(text):
            day = dt.datetime.fromisoformat(text).replace(tzinfo=dt.timezone.utc)
            return day, day + dt.timedelta(days=1)
        moment = dt.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment, moment


class _Run:
    def __init__(self, evidence: Mapping[str, Sequence[Mapping[str, Any]]],
                 resolver: Resolver | None, actor: Any,
                 reviews: Callable[[str, Any], Any] | None = None) -> None:
        self.evidence, self.resolver, self.actor = evidence, resolver, actor
        self.reviews = reviews
        self.gaps: list[dict[str, Any]] = []
        self.reasons: list[str] = []

    def gap(self, code: str, state: str, subject: str, detail: str, *,
            incomplete: bool = True) -> None:
        """A typed gap; ``incomplete``: the answer lacks something it was asked for."""
        self.gaps.append({"state": state, "reason": code, "subject": subject[:700],
                          "detail": f"{code}: {detail}"[:600]})
        if incomplete and code not in self.reasons:
            self.reasons.append(code)

    def pick(self, names: Sequence[str], oid: str) -> Mapping[str, Any] | None:
        """The entry of ``oid`` from the first listed item that carries a value, else the
        first that carries it empty (cited, so the gap can say the source field is empty)."""
        empty = None
        for name in names:
            for entry in self.evidence.get(name, ()):
                if entry.get("object_id") == oid:
                    if not _blank(entry.get("value")):
                        return entry
                    empty = empty or entry
        return empty

    def reread(self, entry: Mapping[str, Any]) -> tuple[dt.datetime, dt.datetime] | None:
        """The entry's time, only when its citation re-read for the actor states it."""
        span = _instant(entry.get("value"))
        citation = entry.get("citation") or {}
        if span is None or self.resolver is None:
            return None
        try:
            view = self.resolver(str(citation.get("l1_handle")), self.actor)
            raw = _field(view, str(citation.get("field_path"))) if isinstance(
                view, Mapping) else None
        except Exception:  # noqa: BLE001 - unreadable or not carried: fails closed
            return None
        return span if _instant(raw) == span else None


    def review(self, served: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """The review ``plx.around`` served, re-read from the review store for the actor:
        only when the store states the same review, decision, reviewer and instant."""
        if self.reviews is None:
            return None
        try:
            view = self.reviews(str(served.get("review_id")), self.actor)
        except Exception:  # noqa: BLE001 - unreadable: fails closed
            return None
        if not isinstance(view, Mapping) or any(
                view.get(k) != served.get(k) for k in ("review_id", "decision", "reviewed_by")):
            return None
        span = _instant(view.get("reviewed_at"))
        return view if span is not None and span == _instant(served.get("reviewed_at")) \
            else None


def _reviews(run: _Run, edges: Sequence[Mapping[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """(how many reviewed links the walk crossed, the review decisions in force among them
    as ``plan_run``'s found decisions); the rest are typed gaps (module doc)."""
    crossed, found, seen = 0, [], set()
    for edge in edges:
        if not any(isinstance(f, Mapping) and f.get("feature") in REVIEW_FEATURES
                   for f in edge.get("reason") or ()):
            continue
        crossed += 1
        link = f"{edge['link_id']} ({edge['link_type']} {edge['from']} -> {edge['to']})"
        served = edge.get("review")
        if isinstance(served, Mapping) and served.get("withheld") == "not_readable":
            run.gap("review_decision_withheld", "inaccessible", link,
                    "a person's review decision on this link cites a record this reader may "
                    "not read: its reviewer and time are withheld, which is not absence")
            continue
        if not isinstance(served, Mapping) or not isinstance(served.get("review_id"), str):
            run.gap("review_decision_not_served", "unknown", link,
                    "a person reviewed this link (ontology/reviews.py), but the host served no "
                    "review for it, so the decision is not placed in time")
            continue
        rid = served["review_id"]
        if rid in seen:   # another stretch of the same review's link
            continue
        seen.add(rid)
        view = run.review(served)
        if view is None:
            run.gap("decision_time_unverified", "unknown", link,
                    f"review {rid}: the review store does not re-read the same review at the "
                    "same instant for this reader, so the decision is not ordered")
            continue
        state = view.get("state")
        if state == "retracted":
            run.gap("review_retracted", "unknown", link,
                    f"review {rid} was retracted by a later review: no decision in force, "
                    "not listed", incomplete=False)
            continue
        if state == "superseded":
            current = view.get("superseded_by", "")
            run.gap("review_superseded", "unknown", link,
                    f"review {rid} is superseded: "
                    + (f"review {current} on this pair is in force" if current else
                       "the review in force on this pair is withheld from this reader"
                       if current is None else "no review on this pair is in force")
                    + "; it is not listed as a decision in force", incomplete=False)
            continue
        if state != "effective":
            run.gap("review_not_in_force", "unknown", link,
                    f"review {rid} is {state} ({view.get('state_reason') or 'no reason'}): "
                    "not listed as a decision in force", incomplete=False)
            continue
        found.append({
            "span": _instant(view["reviewed_at"]), "key": ("person_review", edge["link_id"]),
            "value": view["reviewed_at"], "inputs": [],
            "steps": [f"person_review {rid} on link {edge['link_id']}: time reviewed_at, "
                      "re-read from the review store",
                      f"actor: reviewer {view['reviewed_by']}",
                      f"outcome: {view['decision']}",
                      "basis: the evidence the review cites (the link's evidence)"],
            "basis": [edge["from"], edge["to"]]})
    return crossed, found


def _digest(entry: dict[str, Any]) -> dict[str, Any]:
    entry["digest"] = hashlib.sha256(json.dumps(
        {k: entry[k] for k in ("name", "value", "inputs", "steps", "basis")},
        sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return entry


def _context(run: _Run, kinds: Sequence[Mapping[str, Any]], name: str) -> list[dict[str, Any]]:
    """One entry per context record (a mail naming the root): its cited entries, ordered by
    a time its citation re-reads for this reader, the others after them. Never a decision:
    no outcome, no actor, no gap for what a context record does not state."""
    found: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for kind in kinds:
        label = kind["label"]
        objects = sorted({e["object_id"] for role in CONTEXT_ROLES for n in kind[role]
                          for e in run.evidence.get(n, ()) if e.get("object_id")})
        for oid in objects:
            time = run.pick(kind["time"], oid)
            span = run.reread(time) if time is not None and not _blank(time.get("value")) \
                else None
            used = [time["name"]] if span is not None else []
            steps = [f"{label} {oid}: cited context, not a decision; time "
                     + (f"entry {time['name']}" if span is not None else
                        "not a re-read instant, so not ordered")]
            for cited in kind["cites"]:
                entry = run.pick([cited], oid)
                if entry is not None and not _blank(entry.get("value")):
                    used.append(entry["name"])
                    steps.append(f"cites entry {entry['name']}")
            order = (0, span[0], span[1], label, oid) if span is not None else (
                1, label, oid)
            found.append((order, {"kind": "computed",
                                  "value": time["value"] if span is not None else None,
                                  "inputs": used, "steps": steps, "basis": [oid],
                                  "check": "not_applicable"}))
    found.sort(key=lambda pair: pair[0])   # timed (0, ...) before untimed (1, ...)
    return [_digest({"name": f"{name}_context_{n}"[:64], **entry,
                     "steps": [s[:MAX_STEP_CHARS] for s in entry["steps"]][:40]})
            for n, (_, entry) in enumerate(found, start=1)]


def _missing_state(entry: Mapping[str, Any] | None) -> str:
    return "absent_in_source" if entry is not None and entry.get("citation") else "unknown"


def plan_run(args: Mapping[str, Any], evidence: Mapping[str, Sequence[Mapping[str, Any]]],
             edges: Sequence[Mapping[str, Any]], *, items: Mapping[str, Mapping[str, Any]],
             name: str, as_of: str, resolver: Resolver | None, actor: Any, complete: bool,
             covers: Callable[[str], bool],
             reviews: Callable[[str, Any], Any] | None = None) -> dict[str, Any]:
    """The decisions the plan reached, in time order, with their typed gaps (module doc)."""
    run = _Run(evidence, resolver, actor, reviews)
    found: list[dict[str, Any]] = []
    for record in args["records"]:
        label = record["label"]
        objects = sorted({e["object_id"] for role in ROLES for n in record.get(role, ())
                          for e in evidence.get(n, ()) if e.get("object_id")})
        for oid in objects:
            subject = f"{label} {oid}"
            picked = {role: run.pick(record[role], oid) for role in ROLES if role in record}
            outcome = picked.get("outcome")
            if outcome is not None and _open(outcome.get("value"), record):
                picked["outcome"] = None
            if "decided_by_any" in record and all(
                    picked.get(role) is None or _blank(picked[role].get("value"))
                    for role in record["decided_by_any"]):
                run.gap("decision_not_made_on_record", "absent_in_source", subject,
                        "the record states no " + " and no ".join(record["decided_by_any"])
                        + (" (its outcome is open: entry " + outcome["name"] + ")"
                           if outcome is not None and picked["outcome"] is None else "")
                        + ", so it records no decision and none is listed for it",
                        incomplete=False)
                continue
            ticked = [e for n in record.get("outcome", ()) for e in run.evidence.get(n, ())
                      if e.get("object_id") == oid and e.get("value") == CHECKED]
            if len(ticked) > 1:
                run.gap("decision_outcome_conflict", "unknown", subject,
                        "several outcomes are ticked on this record (entries "
                        + _joined([e["name"] for e in ticked], 300)
                        + "); no one of them is taken as the decision")
            time = picked["time"]
            if time is None or _blank(time.get("value")):
                run.gap("decision_time_not_recorded", _missing_state(time), subject,
                        "no decision time is recorded on this record, so no decision is "
                        "listed for it" + (f" (outcome: entry {picked['outcome']['name']})"
                                           if picked.get("outcome") else ""), incomplete=False)
                continue
            span = run.reread(time)
            if span is None:
                run.gap("decision_time_unverified", "unknown", subject,
                        f"entry {time['name']}: the time is not a readable instant its "
                        "citation states for this reader, so the decision is not ordered")
                continue
            used = [time["name"]]
            steps = [f"{label} {oid}: time entry {time['name']}"]
            for role in ("actor", "outcome", "basis"):
                entry = picked.get(role)
                if role == "outcome" and len(ticked) > 1:
                    used += [e["name"] for e in ticked if e["name"] not in used]
                    steps.append("outcome: conflicting entries "
                                 + _joined([e["name"] for e in ticked], 300))
                    continue
                if entry is not None and not _blank(entry.get("value")):
                    used.append(entry["name"])
                    steps.append(f"{role}: entry {entry['name']}")
                    continue
                steps.append(f"{role}: not recorded (typed gap)")
                run.gap(f"decision_{role}_not_recorded", _missing_state(entry), subject,
                        f"the record states no {role} of this decision"
                        + ("" if role in record else
                           "; the ontology binds none for this kind of record")
                        + "; nothing is inferred in its place")
            found.append({"span": span, "key": (label, oid), "value": time["value"],
                          "inputs": used, "steps": steps, "basis": [oid]})
    reviewed, decided = _reviews(run, edges)
    found += decided
    found.sort(key=lambda d: (d["span"][0], d["span"][1], d["key"]))
    entries: list[dict[str, Any]] = []
    for index, decision in enumerate(found, start=1):
        entry: dict[str, Any] = {
            "name": f"{name}_{index}"[:64], "kind": "computed", "value": decision["value"],
            "inputs": decision["inputs"],
            "steps": [s[:MAX_STEP_CHARS] for s in decision["steps"]][:40],
            "basis": decision["basis"], "check": "not_applicable"}
        entries.append(_digest(entry))
    for i, earlier in enumerate(found):
        group = [i, *(j for j in range(i + 1, len(found))
                      if _unordered(earlier["span"], found[j]["span"]))]
        if len(group) > 1:
            records = [f"{entries[k]['name']} ({' '.join(found[k]['key'])})" for k in group]
            prefix = ("one instant, or a bare date and a time some time zone puts on that "
                      "day, so the listed order is not known: ")
            run.gap("decision_order_unknown", "unknown",
                    _joined([entries[k]["name"] for k in group], 700),
                    prefix + _joined(records, 570 - len(prefix)))
    context = _context(run, args.get("context", ()), name)
    noted = (f"; {len(context)} context record(s) cited, not counted as decisions"
             if context else "")
    if entries:
        entries.append({"name": name, "kind": "computed", "value": len(entries),
                        "inputs": [e["name"] for e in entries][:50],
                        "steps": [f"{len(entries)} recorded decision(s) listed in time order"
                                  + noted + ("" if complete else "; the run was cut, so "
                                             "others may exist")], "check": "not_applicable"})
    elif context:
        entries.append({"name": name, "kind": "computed", "value": None, "inputs": [],
                        "steps": ["no recorded decision listed" + noted],
                        "check": "not_applicable"})
    if not found and not reviewed:
        run.gaps.append({
            "state": "unknown", "reason": "no_recorded_decision", "subject": name,
            "detail": "no_recorded_decision: no decision about this object was found in what "
                      "this reader may read; a withheld source is reported in its own gap and "
                      "this is not proof that none was made",
            "searched": {"sources": [r["label"] for r in args["records"]][:8]}})
    return {"entries": entries[:-1] + context + entries[-1:] if context else entries,
            "gaps": run.gaps, "reasons": run.reasons}


PLAN_ENGINES = {ENGINE: {"check": plan_check, "run": plan_run}}

__all__ = ["ENGINE", "PLAN_ENGINES", "plan_check", "plan_run"]
