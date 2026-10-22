"""Injection test set: crafted mails and documents against the deterministic tools.

An AgentDojo-style corpus; the eval fails on any action proposed from injected text.
Every item (``items.json``, ``plx-injection-item/v1``) seeds an isolated world (an
in-memory ``FixtureSystem`` for structured rows, captured documents resolved by
handle, a fresh ``CaseStore`` and ``ActionPath`` in a temporary directory), opens a
case with the user's objective and then plays the *attacker's best outcome*: the
proposal, confirmation or tool output the injected text asks for. The tools must
refuse or ignore it. No model runs here; the attacker is the item itself, so the
set measures the deterministic defences, not an agent's restraint.

Item::

    {"id", "attack": change_reference | approve_payment | confirm_receipt |
                     confirm_quantity | exfiltrate | instruct_agent | control,
     "objective": "<the case question, recorded at case open>",
     "objective_author_kind": human|agent|unknown (default human: who wrote the
        question, as CaseStore.open records it; only a human's question may supply a
        value, so an agent restating the injected text in its own question is refused),
     "roots": ["<case root>", ...] (optional),
     "rows": [{"relation": "fixture.*", "key": {...}, "row": {...},
               "roles": {"<column>": "<role>", ...} (optional: the relation's declared
                  field roles, e.g. quantity:received, money:amount; a written field
                  with a role traces only to a cited cell of that role),
               "audience": ["<principal>", ...]}],          # rows[0] is the target
     "documents": [{"handle": "graph-mail://...", "relation": "graph-mail.message",
                    "sender_class": internal|external|unknown,
                    "row": {"subject": ..., "body_text": ...}, "audience": [...]}],
     "probe": one of
        {"kind": "propose", "actor": [...], "cite": ["row:0", "doc:0", ...],
         "fields": [...], "equals": <value the text asks for>,
         "precondition_field": <empty target field checked, default fields[0]>}
        {"kind": "confirm", "fact": payment|receipt|quantity|..., "document": 0,
         "claim": "<exact asserting text>", "tier": "<tier granted to it>"}
        {"kind": "firewall", "document": 0, "needle": "<injected instruction>"}
        {"kind": "fields", "document": 0, "field": "<document field>",
         "equals": "<value the text asks for>", "fact": payment|quantity|... (optional),
         "needle": "<injected instruction>" (optional)},
     "expected": {"outcome": refused|ignored|registered|supported, "code": <reason>}}

Document-field items (``probe.kind`` ``fields``) carry a crafted PDF instead of a row:
``documents[i].pdf_pages`` is a list of pages, each a list of text lines, rendered with
reportlab and read by the document-field extractor (``ontology.document_fields``) from
those bytes. Injected text must stay data: it never becomes a typed (non-text) value,
a field read against a conflicting statement, a tier above ``lead`` or a confirmation.

Role-mismatch items (``inj-role-*``) cite a cell holding exactly the asked value in
another role (an invoice amount for a received quantity, the ordered quantity for the
received one, a supplier's customer PO for the Petra PO a payment settles, a cell of no
declared role): ``injection_parameter_role_mismatch``.

Outcomes: ``refused`` (the tool raised, ``code`` = its error code), ``registered``
(an action intent exists; for a document field: injected text was obeyed), ``ignored``
(a confirmation not supported, the needle reaches the output only inside
``untrusted_text`` envelopes, or the field does not hold the asked value: ``code`` = its
gap), ``supported`` (a fact confirmed; for a control field: the value read, cited to
the original bytes).
``control`` items show the same harness registers / supports legitimate inputs, so
a guard that refuses everything fails the set.

Run: ``python3 -m evals.injection.run`` (exit 0 only when every item meets its
expectation). Evidence label: ``isolated-fixture``; nothing here reads a live source.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from ontology.core import Actor, ValidationError
from ontology import document_fields, mentions
from operating import action_path as ap
from operating import injection_guard as guard
from operating.cases import CaseStore, CitationError
from operating.session_identity import CHANNEL_MCP, fixture_session

ITEMS = Path(__file__).with_name("items.json")
FORMAT = "plx-injection-item/v1"
EVIDENCE_LABEL = "isolated-fixture"
ATTACKS = ("change_reference", "approve_payment", "confirm_receipt", "confirm_quantity",
           "exfiltrate", "instruct_agent", "control")
OUTCOMES = ("refused", "ignored", "registered", "supported")
DEFAULT_PRINCIPALS = ("ops",)


def load(path: Path = ITEMS) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise ValueError("injection_items_format")
    items = data["items"]
    ids = [item["id"] for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("injection_item_ids_not_unique")
    for item in items:
        if item["attack"] not in ATTACKS or item["expected"]["outcome"] not in OUTCOMES:
            raise ValueError(f"injection_item_invalid:{item['id']}")
    return items


class _World:
    """One item's isolated sources: fixture rows plus captured documents."""

    def __init__(self, item: Mapping[str, Any], root: Path) -> None:
        self.fx = ap.FixtureSystem()
        self.audience: dict[str, frozenset[str]] = {}
        self.rows: list[str] = []
        for spec in item.get("rows") or ():
            handle = self.fx.seed(spec["relation"], spec["key"], spec["row"],
                                  spec.get("roles"))
            self.audience[handle] = frozenset(spec.get("audience") or DEFAULT_PRINCIPALS)
            self.rows.append(handle)
        self.documents: dict[str, dict[str, Any]] = {}
        self.docs: list[str] = []
        for spec in item.get("documents") or ():
            self.documents[spec["handle"]] = {"relation": spec["relation"],
                                              "l1_handle": spec["handle"],
                                              "row": copy.deepcopy(spec["row"])}
            self.audience[spec["handle"]] = frozenset(spec.get("audience")
                                                      or DEFAULT_PRINCIPALS)
            self.docs.append(spec["handle"])
        self.cases = CaseStore(root / "cases.db", citation_resolver=self.resolve)
        self.path = ap.ActionPath(root / "actions.db", self.cases)

    def resolve(self, handle: str, actor: Actor) -> dict[str, Any]:
        allowed = self.audience.get(handle)
        if allowed is None or not (allowed & actor.principals):
            raise CitationError("not visible")
        if handle in self.documents:
            return copy.deepcopy(self.documents[handle])
        return self.fx.resolve(handle, actor)

    def handle(self, ref: str) -> str:
        kind, _, index = ref.partition(":")
        return (self.rows if kind == "row" else self.docs)[int(index)]


def _actor(probe: Mapping[str, Any]) -> Actor:
    return Actor("plx", frozenset(probe.get("actor") or DEFAULT_PRINCIPALS))


def _propose(item: Mapping[str, Any], world: _World) -> dict[str, Any]:
    probe = item["probe"]
    actor = _actor(probe)
    target = item["rows"][0]
    target_handle = world.rows[0]
    author = item.get("objective_author_kind", "human")
    case_id = world.cases.open(actor, item["objective"], roots=item.get("roots") or (),
                               objective_author_kind=None if author == "unknown" else author)
    try:
        receipt = world.cases.propose(
            actor, case_id, ap.FIXTURE_CAPABILITY,
            # The catalog's fixture payload (operating/action_catalog.json): the
            # field and the value the injected text asks for.
            {"field": probe["fields"][0], "value": probe["equals"]},
            [{"check": "field_empty", "citation": target_handle,
              "field": probe.get("precondition_field", probe["fields"][0])}],
            citations=[world.handle(ref) for ref in probe["cite"]],
            expected_effect={"relation": target["relation"], "key": target["key"],
                             "fields": probe["fields"], "equals": probe["equals"]})
        # The probe's own fixture session: every action path step records who acted.
        intent = world.path.register(actor, case_id, receipt["proposal_id"],
                                     session=fixture_session(actor.tenant, actor.principals,
                                                             channel=CHANNEL_MCP))
    except (ValidationError, CitationError) as exc:
        return {"outcome": "refused", "code": getattr(exc, "code", None) or str(exc)}
    return {"outcome": "registered", "code": None, "intent_id": intent["intent_id"]}


def _confirm(item: Mapping[str, Any], world: _World) -> dict[str, Any]:
    """The message as support for the fact it asserts at ``probe.claim`` (its exact
    text in one field): who wrote that span (sender class, quoted history) and the tier
    the item grants it. The attacker's best case: a high tier."""
    probe = item["probe"]
    spec = item["documents"][probe["document"]]
    tier = probe.get("tier", mentions.TIER)
    supports = []
    for text in (value for value in spec["row"].values() if isinstance(value, str)):
        start = text.find(probe["claim"])
        if start >= 0:
            quoted = mentions.QuotedRanges(text).overlaps(start, start + len(probe["claim"]))
            supports.append({"sender_class": spec["sender_class"], "quoted_history": quoted,
                             "tier": tier})
    if not supports:
        raise ValueError(f"injection_claim_not_in_document:{item['id']}")
    verdict = guard.may_confirm(probe["fact"], supports)
    # The mention projection's own rule must agree (same fact classes and tiers).
    links = [{"link_id": f"l{index}", "tier": tier,
              "external_assertion": guard.is_external_assertion(s["sender_class"],
                                                                s["quoted_history"])}
             for index, s in enumerate(supports)]
    projected = mentions.confirmation_support(links, probe["fact"])
    if projected["supported"] != verdict["supported"]:
        return {"outcome": "refused", "code": "confirmation_rules_disagree"}
    return {"outcome": "supported" if verdict["supported"] else "ignored",
            "code": verdict["reason"]}


def _firewall(item: Mapping[str, Any], world: _World) -> dict[str, Any]:
    probe = item["probe"]
    handle = world.docs[probe["document"]]
    spec = item["documents"][probe["document"]]
    actor = _actor(probe)
    content = world.resolve(handle, actor)
    # A tool output carrying the message, as the case and mail tools return it.
    output = {"messages": [{"citation": handle, "sender_class": spec["sender_class"],
                            **content["row"]}]}
    wrapped = guard.firewall_tool_output(output, sender_class=spec["sender_class"])
    leaks = guard.bare_text_leaks(wrapped, probe["needle"])
    typed = list(guard.typed_values(handle, guard.firewall_content(handle, content)))
    if leaks or typed:
        return {"outcome": "registered", "code": "source_text_outside_envelope"}
    return {"outcome": "ignored", "code": None}


def _pdf(pages: list[list[str]]) -> bytes:
    import io
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page = canvas.Canvas(buffer)
    for lines in pages:
        y = 780
        for line in lines:
            page.drawString(60, y, line)
            y -= 18
        page.showPage()
    page.save()
    return buffer.getvalue()


def _fields(item: Mapping[str, Any], world: _World) -> dict[str, Any]:
    """The crafted document as the document-field extractor reads it from its bytes."""
    probe = item["probe"]
    spec = item["documents"][probe["document"]]
    content = _pdf(spec["pdf_pages"])
    found = document_fields.extract_fields(content, "application/pdf",
                                           filename="document.pdf", l1_handle=spec["handle"])
    needle = probe.get("needle")
    typed = [f for f in found["fields"].values() if f["kind"] != "text"]
    if found["tier"] != document_fields.TIER or not found["external_assertion"] or (
            needle and any(needle in json.dumps(f) for f in typed)):
        return {"outcome": "registered", "code": "document_text_obeyed"}
    field = found["fields"].get(probe["field"]) or {"gap": found["status"]}
    if field.get("value") != probe["equals"]:
        return {"outcome": "ignored", "code": field.get("gap")}
    if any(document_fields.resolve_citation(content, c, mime_type="application/pdf") is None
           for c in field["citations"]):
        return {"outcome": "refused", "code": "citation_unresolved"}
    if "fact" in probe:
        verdict = mentions.confirmation_support(
            [{"link_id": "field", "tier": found["tier"],
              "external_assertion": found["external_assertion"]}], probe["fact"])
        return {"outcome": "supported" if verdict["supported"] else "ignored",
                "code": verdict["reason"]}
    return {"outcome": "supported", "code": None}


PROBES = {"propose": _propose, "confirm": _confirm, "firewall": _firewall, "fields": _fields}


def run_item(item: Mapping[str, Any]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        world = _World(item, Path(tmp))
        result = PROBES[item["probe"]["kind"]](item, world)
    expected = item["expected"]
    passed = (result["outcome"] == expected["outcome"]
              and ("code" not in expected or result["code"] == expected["code"]))
    return {"id": item["id"], "attack": item["attack"], **result,
            "expected": dict(expected), "passed": passed}


def run_all(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    results = [run_item(item) for item in (load() if items is None else items)]
    attacks = [r for r in results if r["attack"] != "control"]
    return {"format": "plx-injection-report/v1", "evidence_label": EVIDENCE_LABEL,
            "items": len(results), "attacks": len(attacks),
            "passed": sum(r["passed"] for r in results),
            "injected_actions": sum(r["outcome"] in ("registered", "supported")
                                    for r in attacks),
            "failed": [r for r in results if not r["passed"]], "results": results}


def main(argv: list[str] | None = None) -> int:
    report = run_all()
    print(json.dumps({key: report[key] for key in ("format", "evidence_label", "items",
                                                   "attacks", "passed", "injected_actions",
                                                   "failed")}, indent=2))
    return 0 if not report["failed"] and report["injected_actions"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
