"""The generic "why / how we know" trail: what was decided, how it was determined, the
interactions and records that led to it, and a suggested next action, every step openable.

One builder for every record type. A declarative *why plan* (``ontology/why_plans/*.json``,
format ``plx-why-plan/v1``) names, for one use case, the root types it covers, the words a
question uses for it, which of the root's own events matter (and whether each is a record
change or a decision), which links to follow (each far end's events, or a dated property
read with ``plx.object``, and the person a step names), the verdict rule and the next-action
suggestions. ``load_plans`` checks every plan against the compiled ontology schema (an
unknown key, root type, event type, link or property is a ``WhyPlanError``: fail closed).

``build`` reads as the reader, through the reader's own tool dispatch (``call``: the app
view's ``plx.timeline``/``plx.around``/``plx.object``), so a step this reader may not read
is never returned. A step whose source the reader's access refuses (``decide``) is counted,
never named. A step's ``value`` comes only from a claim the answer gate passed (``claims``:
the facts the answer shows); everything else in a step is the plan's own wording, a
person's name, a record's display identifier and the cited date. The verdict is the
gated claim matching the plan's rule; else the latest verdict event as recorded (no value);
else "not established". The next action is a suggestion only: nothing is written or sent.

Evidence: isolated-fixture (``operating/tests/test_why_trail.py``) and the app's agent path
(``operator_api.backend.agent_answer``); live-unverified.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
PLAN_DIR = ROOT / "ontology" / "why_plans"
FORMAT = "plx-why-plan/v1"
KINDS = ("decision", "communication", "record_change", "document", "verdict")
WHEN = ("always", "verdict_missing", "no_communication")
MAX_STEPS = 24            # steps a trail shows (the verdict and the withheld count extra)
MAX_LINK_OBJECTS = 8      # per link, at most (a plan's own max is lower)
MAX_LINKS = 12            # links one plan follows at most (each costs reads)
TIMELINE_PAGES = 3        # pages of a root's or a link's events read at most
BUDGET_SECONDS = 12.0     # a trail stops reading here and says it is incomplete
# A question asking why or how we know: the generic plan answers it when no use-case plan
# matches.
WHY_QUESTION = re.compile(r"\b(?:why|how (?:do|did|can) (?:we|you|i) know|how was (?:it|this)"
                          r" (?:decided|determined)|what happened|explain|reason|basis|"
                          r"chain of custody|evidence)\b", re.I)
_PLAN_KEYS = {"format", "id", "title", "description", "priority", "root_types",
              "question_terms", "root_events", "links", "verdict", "next_actions"}
_LINK_KEYS = {"link", "kind", "label", "events", "date_property", "who", "tier_floor", "max"}
_VERDICT_KEYS = {"label", "claim_terms", "events", "missing"}
_ACTION_KEYS = {"when", "text"}
CHECKED_FACT = "Checked fact"   # a gated claim no plan step cites: labelled by its source
_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class WhyPlanError(ValueError):
    """A why plan the schema does not support; every problem is listed."""


@dataclass(frozen=True)
class WhyPlan:
    id: str
    title: str
    priority: int
    root_types: tuple[str, ...]
    terms: tuple[re.Pattern, ...]
    root_events: Mapping[str, Mapping[str, str]]
    links: tuple[Mapping[str, Any], ...]
    verdict: Mapping[str, Any]
    verdict_terms: Optional[re.Pattern]
    next_actions: tuple[Mapping[str, str], ...]

    def covers(self, object_type: str) -> bool:
        return "*" in self.root_types or object_type in self.root_types

    def score(self, question: str) -> int:
        return sum(1 for term in self.terms if term.search(question))


def default_schema() -> Any:
    from operating.ontology_tools import default_schema as schema
    return schema()


def _far_types(schema: Any, object_type: str, name: str) -> list[str]:
    """The far end's type of the link ``name`` navigates from ``object_type`` (a link's
    name on its from type, its reverse name on its to type)."""
    out = []
    for link in schema.link_types.values():
        if link.get("from") == object_type and link.get("name") == name:
            out.append(link.get("to"))
        if link.get("to") == object_type and link.get("reverse_name") == name:
            out.append(link.get("from"))
    return [t for t in out if isinstance(t, str)]


def _relates(schema: Any, event_type: str, object_type: str) -> bool:
    """Whether events of ``event_type`` relate to records of ``object_type``: its own type,
    or a qualifier of that type (a shipment's sales order, a receipt's purchase order)."""
    spec = schema.event_types.get(event_type) or {}
    return spec.get("object_type") == object_type or any(
        isinstance(q, Mapping) and q.get("object_type") == object_type
        for q in spec.get("qualifiers", ()))


def _properties(schema: Any, object_type: str) -> set[str]:
    spec = schema.object_types.get(object_type) or {}
    return {p.get("name") for p in spec.get("properties", ()) if isinstance(p, Mapping)}


def _strings(value: Any) -> bool:
    """A list of text (a bare string is not one: it would be read letter by letter)."""
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def check_plan(doc: Any, schema: Any) -> list[str]:
    """Every problem of one plan document against ``schema`` (empty: valid)."""
    if not isinstance(doc, Mapping):
        return ["not an object"]
    problems = [f"unknown key {k}" for k in sorted(set(doc) - _PLAN_KEYS)]
    problems += [f"missing key {k}" for k in sorted(_PLAN_KEYS - set(doc))]
    if problems:
        return problems
    if doc["format"] != FORMAT:
        problems.append(f"format is not {FORMAT}")
    if not isinstance(doc["id"], str) or not _ID.match(doc["id"]):
        problems.append("id is not a name")
    if not _integer(doc["priority"]):
        problems.append("priority is not an integer")
    roots = doc["root_types"]
    generic = roots == ["*"]
    if not _strings(roots) or not roots or not generic and any(
            r not in schema.object_types for r in roots):
        problems.append("root_types must be ['*'] or declared object types")
        roots = []
    if not _strings(doc["question_terms"]):
        problems.append("question_terms must be a list of patterns")
    for term in doc["question_terms"] if _strings(doc["question_terms"]) else ():
        try:
            re.compile(term)
        except (re.error, TypeError):
            problems.append(f"question term {term!r} is not a pattern")
    if not generic and not doc["question_terms"]:
        problems.append("a use-case plan needs question_terms")
    if not isinstance(doc["root_events"], Mapping):
        problems.append("root_events must be an object")
    events = doc["root_events"] if isinstance(doc["root_events"], Mapping) else {}
    for name, spec in events.items():
        if not (isinstance(spec, Mapping) and set(spec) == {"kind", "label"}
                and spec.get("kind") in KINDS and isinstance(spec.get("label"), str)):
            problems.append(f"root event {name}: needs exactly kind and label")
        if name == "*":
            if not generic:
                problems.append("root event * only in the generic plan")
            continue
        if name not in schema.event_types:
            problems.append(f"root event {name} is not a declared event type")
        elif not generic and not any(_relates(schema, name, r) for r in roots):
            problems.append(f"root event {name} is not an event of a root type")
    if not isinstance(doc["links"], list) or len(doc["links"]) > MAX_LINKS:
        problems.append(f"links must be a list of at most {MAX_LINKS}")
    for index, link in enumerate(doc["links"] if isinstance(doc["links"], list) else ()):
        where = f"link {index}"
        if not isinstance(link, Mapping) or set(link) - _LINK_KEYS \
                or not {"link", "kind", "label"} <= set(link) \
                or not all(isinstance(link[k], str) for k in ("link", "kind", "label")) \
                or not all(isinstance(link.get(k, ""), str)
                           for k in ("date_property", "who", "tier_floor")) \
                or not _strings(link.get("events", [])):
            problems.append(f"{where}: needs link, kind, label (text) and only "
                            f"{sorted(_LINK_KEYS)}, events a list of event types")
            continue
        if link["kind"] not in KINDS or link["kind"] == "verdict":
            problems.append(f"{where}: kind {link['kind']!r} is not a step kind")
        if link.get("tier_floor") not in (None, "verified", "reviewed", "corroborated",
                                          "stored_by_text_join", "lead"):
            problems.append(f"{where}: unknown tier_floor")
        if not _integer(link.get("max", 1)) or not 1 <= link.get("max", 1) <= \
                MAX_LINK_OBJECTS:
            problems.append(f"{where}: max must be 1..{MAX_LINK_OBJECTS}")
        if link.get("events") and link.get("date_property"):
            problems.append(f"{where}: events or date_property, not both")
        fars = sorted({t for r in (schema.object_types if generic else roots) for t in
                       _far_types(schema, r, link["link"])})
        if not fars:
            problems.append(f"{where}: no root type declares the link {link['link']}")
            continue
        for name in link.get("events") or ():
            if name not in schema.event_types or \
                    schema.event_types[name].get("object_type") not in fars:
                problems.append(f"{where}: {name} is not an event of {', '.join(fars)}")
        for key in ("date_property", "who"):
            prop = link.get(key)
            if prop is not None and not all(prop in _properties(schema, t) for t in fars):
                problems.append(f"{where}: {key} {prop} is not a property of "
                                f"{', '.join(fars)}")
    verdict = doc["verdict"]
    if not isinstance(verdict, Mapping) or set(verdict) != _VERDICT_KEYS \
            or not isinstance(verdict["label"], str) or not isinstance(verdict["missing"], str) \
            or not _strings(verdict["claim_terms"]) or not _strings(verdict["events"]):
        problems.append(f"verdict needs exactly {sorted(_VERDICT_KEYS)}: label and missing "
                        "text, claim_terms and events lists of text")
    else:
        for term in verdict["claim_terms"]:
            try:
                re.compile(term)
            except (re.error, TypeError):
                problems.append(f"verdict term {term!r} is not a pattern")
        for name in verdict["events"]:
            if name not in events:
                problems.append(f"verdict event {name} is not a root event of the plan")
    actions = doc["next_actions"]
    if not isinstance(actions, list) or not actions:
        problems.append("next_actions needs at least one")
    for action in actions if isinstance(actions, list) else ():
        if not isinstance(action, Mapping) or set(action) != _ACTION_KEYS \
                or action.get("when") not in WHEN or not isinstance(action.get("text"), str):
            problems.append(f"next action {action!r}: needs when in {WHEN} and text")
        elif set(_PLACEHOLDER.findall(action["text"])) - {"record"}:
            problems.append(f"next action {action['text']!r}: only {{record}} is filled in")
        elif re.search(r"\d", action["text"]):
            problems.append(f"next action {action['text']!r}: carries a figure")
    if isinstance(actions, list) and not any(isinstance(a, Mapping) and
                                             a.get("when") == "always" for a in actions):
        problems.append("next_actions needs an always suggestion")
    return problems


def _pattern(terms: Sequence[str]) -> Optional[re.Pattern]:
    return re.compile(r"\b(?:" + "|".join(terms) + r")\b", re.I) if terms else None


def load_plans(directory: str | Path = PLAN_DIR, *, schema: Any = None) -> list[WhyPlan]:
    """Every why plan of ``directory``, by priority (``WhyPlanError`` lists every problem)."""
    if schema is None:
        from operating.ontology_tools import default_schema
        schema = default_schema()
    plans, problems = [], []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{path.name}: not JSON ({exc})")
            continue
        found = check_plan(doc, schema)
        if not found and doc["id"] != path.stem:
            found = ["id differs from the file name"]
        problems += [f"{path.name}: {p}" for p in found]
        if found:
            continue
        plans.append(WhyPlan(
            id=doc["id"], title=doc["title"], priority=doc["priority"],
            root_types=tuple(doc["root_types"]),
            terms=tuple(re.compile(r"\b(?:" + t + r")\b", re.I)
                        for t in doc["question_terms"]),
            root_events=dict(doc["root_events"]), links=tuple(doc["links"]),
            verdict=dict(doc["verdict"]), verdict_terms=_pattern(doc["verdict"]["claim_terms"]),
            next_actions=tuple(doc["next_actions"])))
    if problems:
        raise WhyPlanError("; ".join(problems))
    ids = [p.id for p in plans]
    if len(set(ids)) != len(ids):
        raise WhyPlanError("two plans share an id")
    return sorted(plans, key=lambda p: (p.priority, p.id))


_CACHE: dict[str, list[WhyPlan]] = {}


def plans() -> list[WhyPlan]:
    """The repository's why plans, loaded once per process."""
    if "plans" not in _CACHE:
        _CACHE["plans"] = load_plans()
    return _CACHE["plans"]


def select(question: str, object_type: str,
           candidates: Sequence[WhyPlan] | None = None) -> Optional[WhyPlan]:
    """The plan for this question about a record of ``object_type``: the use-case plan
    covering the type whose terms the question uses most (ties: priority); else, for a
    why or how-do-we-know question, the generic plan; else none."""
    candidates = plans() if candidates is None else candidates
    scored = [(p.score(question), -p.priority, p) for p in candidates
              if p.covers(object_type) and "*" not in p.root_types]
    scored = [s for s in scored if s[0] > 0]
    if scored:
        return max(scored, key=lambda s: (s[0], s[1]))[2]
    if WHY_QUESTION.search(question or ""):
        return next((p for p in candidates if "*" in p.root_types), None)
    return None


# ---- reading as the reader -----------------------------------------------------------

def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        if "untrusted_text" in value and set(value) <= {"untrusted_text", "origin"}:
            return value["untrusted_text"]
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


@dataclass
class _Run:
    call: Callable[[str, dict], Any]
    deadline: float
    incomplete: bool = False
    may_be_withheld: bool = False
    calls: int = 0

    def __call__(self, name: str, arguments: dict) -> Mapping[str, Any]:
        if time.monotonic() > self.deadline:
            self.incomplete = True
            return {}
        self.calls += 1
        try:
            got = _plain(self.call(name, arguments))
        except Exception:  # noqa: BLE001 - a read that fails is an incomplete trail
            self.incomplete = True
            return {}
        if not isinstance(got, Mapping) or got.get("error"):
            if isinstance(got, Mapping) and (got.get("error") or {}).get("code") not in (
                    "unknown_link", "no_such_link", "undeclared_link"):
                self.incomplete = True
            return {}
        reason = (got.get("completeness") or {}).get("reason")
        if reason in ("withheld_by_permission", "marking_withheld") or any(
                isinstance(g, Mapping) and g.get("state") == "inaccessible"
                for g in got.get("gaps") or ()):
            self.may_be_withheld = True
        return got

    def timeline(self, origin: str, event_types: Sequence[str]) -> list[Mapping[str, Any]]:
        arguments: dict[str, Any] = {"from": origin, "limit": 50}
        if event_types:
            arguments["event_types"] = list(event_types)
        events: list[Mapping[str, Any]] = []
        for _page in range(TIMELINE_PAGES):
            got = self("plx.timeline", arguments)
            events += [e for e in got.get("events") or () if isinstance(e, Mapping)]
            cursor = (got.get("completeness") or {}).get("next_cursor") \
                or (got.get("completeness") or {}).get("cursor")
            if not cursor:
                break
            arguments = {**arguments, "cursor": cursor}
        else:
            self.incomplete = True
        return events

    def read(self, object_id: str, properties: Sequence[str]) -> dict[str, tuple[Any, str]]:
        """property -> (value, citation handle) as plx.object shows it to the reader."""
        got = self("plx.object", {"object_id": object_id,
                                  "properties": sorted(set(properties))})
        out: dict[str, tuple[Any, str]] = {}
        for value in got.get("values") or ():
            if not isinstance(value, Mapping):
                continue
            citation = (value.get("citation") or {}).get("l1_handle") \
                if isinstance(value.get("citation"), Mapping) else value.get("citation")
            if isinstance(value.get("property"), str) and isinstance(citation, str):
                out.setdefault(value["property"], (value.get("value"), citation))
        return out


def _event_time(event: Mapping[str, Any]) -> Optional[str]:
    if isinstance(event.get("time"), str):
        return event["time"]
    window = event.get("window")
    return window.get("end") if isinstance(window, Mapping) else None


def _handle(citation: Any) -> Optional[str]:
    if isinstance(citation, Mapping):
        citation = citation.get("l1_handle")
    return citation if isinstance(citation, str) and citation else None


_ANGLE = re.compile(r"\s*<[^>]*>\s*")


def person(value: Any) -> Optional[str]:
    """A person's name as a step names it: the display name, never an address or a figure
    (an address-only or numeric value names nobody)."""
    if isinstance(value, Mapping):
        user = value.get("user")
        value = (user.get("displayName") if isinstance(user, Mapping) else None) \
            or value.get("name") or value.get("display_name") or value.get("displayName")
    if not isinstance(value, str):
        return None
    name = _ANGLE.sub(" ", value).strip().strip('"').strip()
    if not name or "@" in name or re.search(r"\d", name):
        return None
    return name[:80]


def identifier(value: Any) -> Optional[str]:
    """A record's display as a step names it: one identifier-like token (an order or batch
    number), never a title or sentence that could carry a figure outside the gate."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= 40 and not re.search(r"\s", value) else None


def field_key(path: Any) -> Optional[str]:
    """'orddate' from row.orddate, row.ord_date or 'ord date': a field compared by its
    last segment, case, spaces and underscores ignored."""
    if not isinstance(path, str) or not path.strip():
        return None
    return re.sub(r"[\s_]", "", path.split(".")[-1]).lower() or None


def _date_of(value: Any) -> Optional[str]:
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}", value):
        return value[:25]
    return None


def _root_event_types(plan: WhyPlan, object_type: str, schema: Any) -> dict[str, Mapping]:
    if "*" in plan.root_events:
        spec = plan.root_events["*"]
        return {name: {"kind": spec["kind"], "label": _event_words(name)}
                for name in schema.event_types if _relates(schema, name, object_type)}
    return {name: spec for name, spec in plan.root_events.items()
            if _relates(schema, name, object_type)}


def _event_words(name: str) -> str:
    """'Purchase order issued' from PurchaseOrder.issued (the schema's own names)."""
    kind, _, verb = name.partition(".")
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", kind).lower()
    return f"{words[:1].upper()}{words[1:]} {verb.replace('_', ' ')}".strip()


def _gather(run: "_Run", plan: WhyPlan, object_id: str, object_type: str,
            schema: Any) -> list[dict[str, Any]]:
    """The steps one record contributes: its own events the plan names, then each
    link's far ends (their events, or a dated property), as the reader reads them."""
    raw: list[dict[str, Any]] = []
    events = _root_event_types(plan, object_type, schema)
    if events:
        for event in run.timeline(object_id, sorted(events)):
            spec = events.get(str(event.get("event_type")))
            handle = _handle(event.get("citation"))
            if spec and handle:
                raw.append({"kind": spec["kind"], "date": _event_time(event),
                            "label": spec["label"], "handle": handle,
                            "field": field_key((event.get("citation") or {}).get(
                                "field_path") if isinstance(event.get("citation"), Mapping)
                                else None),
                            "event_type": event.get("event_type")})

    for link in plan.links:
        fars = _far_types(schema, object_type, link["link"])
        if not fars:
            continue
        arguments: dict[str, Any] = {"from": object_id, "path": [{"link": link["link"]}],
                                     "limit": 50}
        if link.get("tier_floor"):
            arguments["tier_floor"] = link["tier_floor"]
        around = run("plx.around", arguments)
        reached = [o for o in around.get("objects") or () if isinstance(o, Mapping)
                   and isinstance(o.get("object_id"), str)]
        if not reached:
            continue
        displays = {o["object_id"]: o.get("display") for o in reached}
        # a far end reached only through a text match (a mention) is said to be one
        tiers: dict[str, set] = {}
        for edge in around.get("edges") or ():
            if isinstance(edge, Mapping):
                for end in (edge.get("from"), edge.get("to")):
                    tiers.setdefault(end, set()).add(edge.get("tier"))
        keep = int(link.get("max", 4))
        found: list[dict[str, Any]] = []
        if link.get("events") and isinstance(around.get("set"), str):
            for event in run.timeline(around["set"], link["events"]):
                handle = _handle(event.get("citation"))
                member = next((o["object_id"] for o in event.get("objects") or ()
                               if isinstance(o, Mapping) and o.get("object_id") in displays),
                              None)
                if handle and member:
                    found.append({"kind": link["kind"], "date": _event_time(event),
                                  "label": link["label"], "handle": handle,
                                  "object_id": member})
            found = found[-keep:]          # the latest, in time order
        else:
            wanted = [p for p in (link.get("date_property"), link.get("who")) if p]
            for item in reached[:keep]:
                values = run.read(item["object_id"], wanted or ["id"]) if wanted else {}
                dated = values.get(link.get("date_property") or "")
                handle = dated[1] if dated else next((v[1] for v in values.values()), None)
                if handle is None:
                    got = run("plx.object", {"object_id": item["object_id"]})
                    handle = next((_handle(v.get("citation")) for v in got.get("values")
                                   or () if isinstance(v, Mapping)
                                   and _handle(v.get("citation"))), None)
                if handle:
                    found.append({"kind": link["kind"],
                                  "date": _date_of(dated[0]) if dated else None,
                                  "label": link["label"], "handle": handle,
                                  "object_id": item["object_id"],
                                  "who": person(values.get(link.get("who") or "",
                                                           (None,))[0])})
            if len(reached) > keep:
                run.incomplete = True
        if link.get("who") and link.get("events"):
            for step in found:
                values = run.read(step["object_id"], [link["who"]])
                step["who"] = person(values.get(link["who"], (None,))[0])
        for step in found:
            reached_by = tiers.get(step.get("object_id")) or set()
            if reached_by and reached_by <= TEXT_MATCH_TIERS:
                step["check"] = TEXT_MATCH
            display = displays.get(step.get("object_id"))
            if step.get("who"):
                step["label"] = f"{step['label']} from {step['who']}" \
                    if step["kind"] == "communication" else f"{step['label']} by {step['who']}"
            elif identifier(display) and link["kind"] != "communication":
                step["label"] = f"{step['label']} {identifier(display)}"
        raw += found
    return raw


def _collapsed(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records of one kind on one day without a value (a run of production entries) as
    one step, the latest cited, its title saying how many records it stands for; a step
    with a gated value always stands alone."""
    out: list[dict[str, Any]] = []
    for step in steps:
        last = out[-1] if out else None
        if last is not None and step.get("date") and not step.get("value") \
                and not last.get("value") and (last.get("date") or "")[:10] == \
                step["date"][:10] and last.get("base", last["label"]) == step["label"] \
                and last["kind"] == step["kind"]:
            count = last.get("records", 1) + 1
            out[-1] = {**step, "records": count, "base": step["label"],
                       "label": f"{step['label']} ({count} records)"}
        else:
            out.append(step)
    return out


TEXT_MATCH = "text_match"
TEXT_MATCH_TIERS = frozenset({"lead", "stored_by_text_join"})
IDENTITY_TIERS = ("verified", "reviewed")   # conservative identity, as plx.find
MAX_TWINS = 4


def _twins(run: "_Run", object_id: str, object_type: str, schema: Any
           ) -> list[tuple[str, str]]:
    """The other systems' records of the same thing (the schema's identity links at an
    established tier), so a trail reads the Portal, BC, Sage and FileMaker copies alike;
    nothing is merged, each step keeps its own source."""
    found: dict[str, str] = {}
    for link_id in sorted(schema.identity_link_types()):
        link = schema.link_types[link_id]
        if object_type not in (link.get("from"), link.get("to")):
            continue
        for direction in ("forward", "reverse"):
            if (direction == "forward" and link.get("from") != object_type) or \
                    (direction == "reverse" and link.get("to") != object_type):
                continue
            around = run("plx.around", {"from": object_id, "path": [
                {"link_type": link_id, "direction": direction}], "limit": 10})
            for edge in around.get("edges") or ():
                if not isinstance(edge, Mapping) or edge.get("tier") not in IDENTITY_TIERS:
                    continue
                for end in (edge.get("from"), edge.get("to")):
                    if isinstance(end, str) and end != object_id and ":" in end:
                        found.setdefault(end, end.split(":", 1)[0])
    return list(found.items())[:MAX_TWINS]


def same_records(call: Callable[[str, dict], Any], object_ids: Sequence[str],
                 schema: Any = None) -> bool:
    """Whether every record of ``object_ids`` is the first one's record in another system
    (an established identity link, as the reader reads it): a resolution to them names one
    thing, never a guess between several."""
    if schema is None:
        schema = default_schema()
    first, rest = object_ids[0], set(object_ids[1:])
    run = _Run(call, time.monotonic() + BUDGET_SECONDS)
    twins = {oid for oid, _ in _twins(run, first, first.split(":", 1)[0], schema)}
    return rest <= twins


def build(plan: WhyPlan, root: Mapping[str, Any], *, call: Callable[[str, dict], Any],
          decide: Callable[[Sequence[str]], Mapping[str, Any]],
          claims: Sequence[Mapping[str, Any]], schema: Any = None,
          seconds: float = BUDGET_SECONDS) -> dict[str, Any]:
    """The trail for ``root`` ({object_id, object_type, label}) under ``plan``.

    ``claims``: the answer's shown facts, each {text, citations: [handle], checked:
    [(handle, field)] the gate checked, check}: only these give a step a value, and only a
    step citing a record and field the gate checked. ``hidden`` is the distinct refused
    sources. ``decide(handles)`` -> {handle: None or refusal}: the
    reader's access; a refused step is counted (``hidden``), never shown. Returns
    {plan, steps: [{kind, date, label, value, handle, check}], hidden, may_be_withheld,
    incomplete, next_action: {text, rule, suggestion_only}}."""
    if schema is None:
        from operating.ontology_tools import default_schema
        schema = default_schema()
    run = _Run(call, time.monotonic() + seconds)
    object_id, object_type = str(root["object_id"]), str(root.get("object_type") or
                                                          root["object_id"].split(":", 1)[0])
    record = str(root.get("label") or "").strip()
    if not record or record in (object_type, "Object", "Record"):
        # the record as the reader's own read displays it (an identifier, no figure read
        # from its fields), else the type in words
        shown = (run("plx.object", {"object_id": object_id}).get("object") or {}).get(
            "display")
        record = identifier(shown) or "this " + re.sub(
            r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", object_type).lower()
    record = record[:120]
    raw: list[dict[str, Any]] = []
    for oid, otype in [(object_id, object_type)] + _twins(run, object_id, object_type,
                                                          schema):
        raw += _gather(run, plan, oid, otype, schema)

    # each step shown only when the reader's access admits its source: counted otherwise
    handles = sorted({s["handle"] for s in raw})
    refused = {h for h, marker in (decide(handles) if handles else {}).items()
               if marker is not None}
    hidden = len(refused)            # each refused source once, however many steps cite it
    steps = [s for s in raw if s["handle"] not in refused]
    seen: set[tuple] = set()
    unique = []
    for step in steps:
        key = (step["handle"], step["label"], step.get("date"))
        if key not in seen:
            seen.add(key)
            unique.append(step)
    steps = unique

    # values from gated claims only, matched by the source they cite
    verdict_claim = None
    if plan.verdict_terms is not None:
        verdict_claim = next((c for c in reversed(list(claims))
                              if plan.verdict_terms.search(str(c.get("text") or ""))), None)
    elif claims:
        verdict_claim = claims[0]
    by_source: dict[tuple, Mapping[str, Any]] = {}
    for claim in claims:
        if claim is verdict_claim:
            continue
        # only the record and field the gate checked: a fact citing several records is
        # never another record's value
        for handle, field in claim.get("checked") or ():
            by_source.setdefault((handle, field_key(field)), claim)
    used: set[int] = set()
    for step in steps:
        # the same record and the same field: a claim about another field of the record
        # the step cites is its own step, never this step's value
        claim = by_source.get((step["handle"], step.get("field"))) \
            if step.get("field") else None
        step["value"] = str(claim.get("text"))[:300] if claim else None
        step["check"] = _claim_check(claim) if claim else step.get("check") or \
            "read_from_record"
        if claim:
            used.add(id(claim))
    for claim in claims:
        if claim is verdict_claim or id(claim) in used or not claim.get("citations"):
            continue
        handle = next((h for h, _ in claim.get("checked") or ()), claim["citations"][0])
        steps.append({"kind": _kind_of(handle), "date": None, "label": CHECKED_FACT,
                      "handle": handle, "value": str(claim.get("text"))[:300],
                      "check": _claim_check(claim)})
    steps.sort(key=lambda s: (s.get("date") is None, s.get("date") or "",
                              KINDS.index(s["kind"]), s["label"]))
    steps = _collapsed(steps)
    if len(steps) > MAX_STEPS:
        # keep the latest: what led to the verdict
        steps = steps[:1] + steps[-(MAX_STEPS - 1):]
        run.incomplete = True

    if verdict_claim is not None:
        verdict = {"kind": "verdict", "date": None, "label": plan.verdict["label"],
                   "value": str(verdict_claim.get("text"))[:300],
                   "handle": (verdict_claim.get("citations") or [None])[0],
                   "check": _claim_check(verdict_claim)}
        state = "checked"
    else:
        recorded = [s for s in steps if s.get("event_type") in plan.verdict["events"]]
        if recorded:
            last = recorded[-1]
            verdict = {"kind": "verdict", "date": last.get("date"),
                       "label": f"{plan.verdict['label']}: last recorded step is "
                                f"{last['label'].lower()}", "value": None,
                       "handle": last["handle"], "check": "read_from_record"}
            state = "recorded"
        else:
            verdict = {"kind": "verdict", "date": None, "label": plan.verdict["missing"],
                       "value": None, "handle": None, "check": "not_established"}
            state = "missing"
    steps.append(verdict)

    communicated = any(s["kind"] == "communication" for s in steps)
    action = None
    for rule in plan.next_actions:
        if rule["when"] == "always" or rule["when"] == "verdict_missing" and state == \
                "missing" or rule["when"] == "no_communication" and not communicated:
            action = {"text": rule["text"].replace("{record}", record),
                      "rule": f"{plan.id}.{rule['when']}", "suggestion_only": True}
            break
    for step in steps:
        step.pop("records", None)
        step.pop("base", None)
        step.pop("field", None)
        step.pop("object_id", None)
        step.pop("who", None)
        step.pop("event_type", None)
    return {"plan": plan.id, "title": plan.title, "steps": steps, "hidden": hidden,
            "may_be_withheld": run.may_be_withheld, "incomplete": run.incomplete,
            "verdict_state": state, "next_action": action, "reads": run.calls}


def _claim_check(claim: Mapping[str, Any]) -> str:
    """The gate's own check of a fact; a fact the gate gave none is unchecked, never
    passed."""
    check = claim.get("check")
    return check if isinstance(check, str) and check else "unchecked"


def _kind_of(handle: str) -> str:
    if handle.startswith(("graph-mail://", "mail://", "teams")):
        return "communication"
    if handle.startswith(("original://", "sharepoint", "repo://")):
        return "document"
    return "record_change"


__all__ = ["FORMAT", "KINDS", "PLAN_DIR", "WhyPlan", "WhyPlanError", "build", "check_plan",
           "load_plans", "person", "plans", "select"]
