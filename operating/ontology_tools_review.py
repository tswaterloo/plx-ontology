"""``plx.review``: a person's review decisions on the ontology (directive dd40ff1bf83f P3).

The confirm / reject controls of the employee workspace call this one tool. Mode
``record`` appends one decision to the Workbench's own review store
(``ontology.reviews``); mode ``status`` lists the reviews of a record as this reader may
see them. Nothing is written to any business system (``business_system_writes`` is 0
by contract).

Who: the author is the host's ``SessionIdentity`` bound at construction, never an
argument (the input contract has no author field). Only a person records: the session
must be a current ``console`` session (author ``human``), employee-proven, or a fixture
console session (a host's explicit demo flag; its author is a ``fixture:`` name, never
an employee principal, ``_fixture_author``); an MCP
session is an agent acting for the employee and is refused (``review_needs_a_person``),
as models propose and never confirm by self-consent. ``call`` takes the host's session
actor, which must be the session's own; the reader is that actor through the host's
employee gate (``gate``) and the read policy decided now (``policy_for``). An identity
decision (``accept_link``, ``reject_link``, ``alias``, ``reject_alias`` (an alias lead
turned down: the alias joins nothing while it holds), or the retraction of one: what
the projection and ``plx.find`` read as a person's identity decision; and an
``allocate`` that would overrule a ``reject_link`` on its pair, which links the two
records again, or the retraction of one, which puts the rejection back) is recorded only when that reader, the employee's entitlements resolved
now, holds the policy principal ``review:identity`` (else ``identity_review_required``,
nothing written). ``identity_gate`` is that one check: every writer of an identity
decision calls it on the reader its employee gate resolved (this tool; the legacy
ledger import retired with ``operating/po_mail_links.py``), and no writer checks it
another way; the overruling allocation and its retraction are checked in the review
store's own write (``ReviewStore.record``'s ``identity_gate``). The gate is write-time: a decision an
entitled employee recorded stays in force after the grant is later revoked (who may
decide is checked when the decision is written, never again when it is read).

What is checked before anything is written, for the reader, now, in the projection
store the tools read: the subject (and a link's target) is readable; every cited
evidence version is the current version of a record the reader may read, with the
digest the person reviewed (else ``evidence_changed``: the person saw an older
version); a cited field is a value of that version the reader may read; a link type is
declared between the two records' types; an alias is a kind of the one identifier
grammar that reads the value, and the grammar reads the same identifier in the cited
field (else ``alias_not_in_cited_field``), so the alias always cites where it is
written; a value review names a property of which the subject has a
value the reader may read (else ``value_not_found``) of a type a person reviews
(numbers, quantities, money and dates: else ``value_not_reviewable``): a confirmation
cites that value's own citation (else ``value_not_cited``), a correction states a value
of the property's type (else ``value_invalid``), a quantity's unit (``unit_required``), money's currency
(``currency_required``), nothing of either where the type has none
(``unit_not_applicable``), and cites the page or the span it was read at (else
``citation_required``); a cited page is one ``plx.read`` resolves for this reader: the
host's one citation reader reads it (``pages``, ``CitationReader.read`` with ``page``),
the same read and gates ``plx.read`` reads the page with, and only ``resolved`` is
taken (``changed_since``: ``evidence_changed``; a read that failed, or a refusal whose
gap is a typed unknown such as a page past the page cap, a page whose text was never
read or a page not captured: ``page_text_unknown``; anything else, a refusal included,
and a host that reads no pages: ``page_not_found``, so a page is never taken
unchecked); a span lies inside the cited field's text, else the cited page's (else
``evidence_span_invalid``; a span on a page with no text read: ``page_text_unknown``); a
handle several records share is read as the record the decision is about (an end of a
link or allocation, the subject of a value review or alias), else refused
(``evidence_ambiguous``); an allocation (supply = subject, demand = target) names a link
type the schema declares many-to-many between their types in either order (else
``allocation_needs_many_to_many``) and a quantity property the supply's type declares
(else ``allocation_property_invalid``), and its share fits the one value of that property
``plx.aggregate`` splits (its ``_pick`` over the values the reader may read): in its unit
after Portal's spelling fold and at most the value (``ontology/units.py``; else
``allocation_conflict``, ``value_not_found`` when no one value stands),
so one share alone never gives out more than the supply holds (``plx.aggregate`` still
checks the shares together). A refusal never tells a record the reader may
not read from one that does not exist. ``document_kind`` is refused
(``document_kind_via_document_fields``): ``plx.document_fields`` is the one path that
records what a document is (decision 2026-10-08-doc-fields-confirmations).

Effect (``projection`` in the answer): an alias applies to the next ``plx.find`` (read
from the review store per call, ``ontology.resolve.ReviewedAliases``); a link decision
and an allocation enter the projection at its next build (``ontology.reviews.link_step``;
``plx.aggregate`` splits a measure only by reviewed allocations; on one pair the later of
an allocation and a rejection holds and ``status`` shows the earlier ``superseded``, so a
pair never has both in force); a value review applies
to the next ``plx.object`` (``show_value_reviews``): beside each value of its property the
reader may read, never in its place (``plx.compare`` shows it beside each side's value
too). The next ``plx.investigate`` keeps the source value and its citation on its entry
with the correction beside it, and its computations (``plx.aggregate``, an engine that
takes reviewed inputs) use the corrected value, citing the correction. Document kinds
recorded before are confirmations of the one kind store
(``operating.kind_confirmation``: the review store's, imported under their review ids, and
the ones this tool recorded there): ``status`` lists them with their state in that store
(``superseded`` once a later confirmation of the same bytes holds, the one the next build
projects) and ``retract`` withdraws one there (``KindConfirmations.withdraw``) from the
next build. A review applies only while the versions it
cites are current and held by their source: ``status`` shows ``withdrawn`` /
``evidence_changed`` once one moved, ``withdrawn`` / ``source_removed`` once its source
removed one (a deleted mail), and a review whose evidence the reader may not read is
counted in ``withheld``, never dropped silently. A ``source_removed`` review whose cited
record a full capture found replaced by a fresh id (``ontology.store_ddl``
``object_removals.replaced_by``) reads through to it in ``replaced``: the one
successor, or every candidate of an ambiguous pairing, as far as the reader may read
them. The review itself is never moved to the successor: its evidence is the old
record's.

Hosts: ``register`` serves the tool through ``ontology_host.CaseHostTools.call`` (a host
built with ``reviews=``); it is not on the MCP listing (case-host 22-tool budget).
Evidence label: isolated-fixture (module tests over a built projection store).
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Callable, Mapping, Sequence

from ontology.json_contract import validate
from ontology.reviews import ReviewError, ReviewStore, effective, state_at, version_identity
from ontology.store import instant, removed_at_sql
from ontology.units import unit_token

from . import kind_confirmation
from .claim_checks import to_decimal
from .ontology_tools_aggregate import _pick
from .ontology_tools import OntologyTools, ToolError, _completeness, _error

TOOL_NAME = "plx.review"
TOOL_NAMES = (TOOL_NAME,)
MAX_REVIEWS = 50
MAX_CANDIDATES = 500   # replacement candidates named per cited record
_PROJECTION = {"alias": "plx_find_now", "reject_alias": "plx_find_now",
               "accept_link": "next_projection_build", "reject_link": "next_projection_build", "allocate": "next_projection_build",
               "confirm_value": "plx_object_now", "correct_value": "plx_object_now"}
# Value types a person may correct, with the grammar of the value text.
_NUMBER = r"-?[0-9]{1,18}(?:\.[0-9]{1,12})?"
_CORRECTABLE = {"decimal": _NUMBER, "money": _NUMBER, "quantity": _NUMBER,
                "integer": r"-?[0-9]{1,18}", "date": r"[0-9]{4}-[0-9]{2}-[0-9]{2}"}
# A stored value a person may confirm (the projection's decimal text, integer or date).
_STORED = dict(_CORRECTABLE, decimal=r"-?[0-9]+(?:\.[0-9]+)?", money=r"-?[0-9]+(?:\.[0-9]+)?",
               quantity=r"-?[0-9]+(?:\.[0-9]+)?")
_MACHINE_READ = frozenset({"parser", "ocr"})
# Who may record an identity decision: the employee-policy principal the host maps to
# the Portal's owner and admin roles (SUPER_ADMIN, ADMIN; owner decision ab1b0f5d9fa8,
# the policy's ``review:identity`` entry with ``portal_roles``).
IDENTITY_REVIEW = "review:identity"
IDENTITY_DECISIONS = frozenset({"accept_link", "reject_link", "alias", "reject_alias"})


def _bare(digest: str) -> str:
    return digest[7:] if digest.startswith("sha256:") else digest


class ReviewTools:
    """``plx.review`` over one host's ontology tools and review store.

    ``base``: the host's ``OntologyTools`` (store, policy, item gate, schema, grammar,
    contracts, clock). ``session``: the host's ``SessionIdentity`` (the author).
    ``gate(session_actor) -> reader``: the host's employee gate (default: the session
    actor reads as itself). ``policy_for(reader) -> ReadPolicy``: the policy decided
    for this call (default: ``base.policy`` as it is). ``kinds``: the kind store
    (``operating.kind_confirmation``; default: the one of the review store's state
    directory, opened once it exists or the review store holds a document kind).
    ``pages(handle, page, session_actor) -> read``: that page of the cited original read
    for this reader by the host's ``plx.read`` reader (``original_pages``: its
    ``CitationReader.read`` result); without it no page is cited."""

    def __init__(self, base: OntologyTools, reviews: ReviewStore, *, session: Any,
                 gate: Callable[[Any], Any] | None = None,
                 policy_for: Callable[[Any], Any] | None = None,
                 kinds: kind_confirmation.KindConfirmations | None = None,
                 pages: Callable[[str, int, Any], Mapping[str, Any]] | None = None
                 ) -> None:
        self.base, self.reviews, self.session = base, reviews, session
        self.gate = gate or (lambda actor: actor)
        self.policy_for = policy_for
        self.contracts = base.contracts
        self._kinds = kinds
        self.pages = pages

    def kinds(self, tenant: str) -> kind_confirmation.KindConfirmations | None:
        """The kind store after importing the review store's earlier document kinds of
        ``tenant`` (``import_reviews``), or None when there is neither."""
        legacy = bool(self.reviews.kind_history(tenant))
        if self._kinds is None:
            path = kind_confirmation.store_path(self.reviews.path.parent)
            if not legacy and not path.is_file():
                return None
            self._kinds = kind_confirmation.KindConfirmations(path)
        if legacy:
            self._kinds.import_reviews(self.base.store.conn, self.reviews, tenant)
        return self._kinds

    def call(self, name: str, arguments: Any, actor: Any) -> dict[str, Any]:
        """Run ``plx.review`` for the session actor ``actor``; the result always
        satisfies the contract."""
        if name not in TOOL_NAMES:
            raise KeyError(name)
        if not isinstance(arguments, dict):
            return self.base._checked(name, _error("invalid_input", "arguments must be an object"))
        if arguments.get("decision") == "document_kind":
            return self.base._checked(name, _error(
                "document_kind_via_document_fields", "plx.review does not record what a "
                "document is; plx.document_fields does",
                "confirm the document's kind with plx.document_fields mode request"))
        problems = validate(arguments, self.contracts["tools"][name]["input"],
                            root=self.contracts, limit=3)
        if problems:
            return self.base._checked(name, _error("invalid_input", "; ".join(problems)))
        try:
            # The author is decided before anything is read or written.
            author = self._author(actor) if arguments["mode"] == "record" else None
            reader = self._reader(actor)
            if author is not None and arguments["decision"] in IDENTITY_DECISIONS:
                identity_gate(reader)
            with self.base.store.lock:
                if author is not None:
                    result = self._record(arguments, reader, author, actor)
                else:
                    result = self._status(arguments, reader)
        except ToolError as exc:
            result = _error(exc.code, exc.message, exc.hint)
        except ReviewError as exc:
            result = _error(exc.code, f"the review was not recorded ({exc.code})")
        except kind_confirmation.ConfirmationRefused as exc:
            result = _error(exc.code, f"the review was not recorded ({exc.code})")
        return self.base._checked(name, result)

    # ------------------------------------------------------------------ identity

    def _reader(self, actor: Any) -> Any:
        reader = self.gate(actor)
        if self.policy_for is not None:
            self.base.policy = self.policy_for(reader)
        return reader

    def _author(self, actor: Any) -> dict[str, str]:
        from .session_identity import AUTHOR_HUMAN, SessionIdentity

        session = self.session
        if type(session) is not SessionIdentity:
            raise ToolError("review_needs_session", "this host binds no session identity, so "
                            "no review can be attributed", "record reviews from the workspace")
        if session.expired() or actor != session.actor:
            raise ToolError("session_actor_mismatch", "the caller is not this host's current "
                            "session", "sign in again")
        if session.author_kind != AUTHOR_HUMAN or (session.employee is None
                                                   and not session.fixture):
            raise ToolError("review_needs_a_person", "a review is recorded only by a person "
                            "signed in as an employee; an agent session proposes, it never "
                            "confirms", "ask the employee to confirm it in the workspace")
        return {"principal": session.employee.principal if session.employee is not None
                else _fixture_author(session), "channel": session.channel,
                "session": session.session_ref}

    # ------------------------------------------------------------------ record

    def _visible(self, scope: Any, object_id: str) -> Any:
        version = self.base._visible_versions(scope, [object_id]).get(object_id)
        if version is None:
            raise ToolError("object_not_found", f"no record {object_id} this reader can read",
                            "find it with plx.find")
        return version

    def _evidence(self, scope: Any, items: list[Mapping[str, Any]], about: Sequence[str],
                  caller: Any) -> list[dict[str, Any]]:
        """Each cited version, checked for the reader now: (object id, handle, digest,
        field path, page and span, value text of the field). A handle several records
        share is read as the first record of ``about`` (what the decision is about: a
        link's or allocation's two ends, else its subject) among them; with none of them
        among several it is refused, never read as an arbitrary one. A page is checked
        against the original for ``caller`` (``pages``)."""
        conn = self.base.store.conn
        out: list[dict[str, Any]] = []
        for item in items:
            handle = item["l1_handle"]
            owners = [row[0] for row in conn.execute(
                "SELECT DISTINCT object_id FROM object_versions WHERE l1_handle = ?", (handle,))]
            visible = self.base._visible_versions(scope, owners)
            current = [v for v in visible.values() if v["l1_handle"] == handle]
            if not current:
                if visible:
                    raise ToolError("evidence_changed", "a cited record has a newer version "
                                    "than the one reviewed", "reload the record and review again")
                raise ToolError("evidence_not_found", "a cited record version is not one this "
                                "reader can read", "cite a record from plx.object")
            if len(current) > 1:
                current = [v for oid in about for v in current if v["object_id"] == oid][:1]
                if not current:
                    raise ToolError("evidence_ambiguous", "the cited version is held by several "
                                    "records and none is what the decision is about",
                                    "cite the subject's (or the target's) own version")
            version = current[0]
            if _bare(version["digest"]) != _bare(item["digest"]):
                raise ToolError("evidence_changed", "a cited record's content differs from the "
                                "content reviewed", "reload the record and review again")
            entry = {"object_id": version["object_id"], "l1_handle": handle,
                     "digest": version["digest"]}
            text = None
            if "field_path" in item:
                rows = [r for r in self.base.store.values_by_citation([(handle,
                                                                        item["field_path"])])
                        if r["object_id"] == version["object_id"]
                        and self.base._row_visible(scope, r)]
                if not rows or rows[0]["value"] is None:
                    raise ToolError("evidence_field_not_found", "the cited field is not a value "
                                    "of the cited record this reader can read",
                                    "cite a field from plx.object")
                entry["field_path"] = item["field_path"]
                text = rows[0]["value"]
            for key in ("page", "start", "end"):
                if key in item:
                    entry[key] = item[key]
            if "page" in item:
                page = self._page(handle, item["page"], caller)
                if text is None and "start" in item and not page:
                    raise _page_text_unknown("no text of the cited page was read (a scan "
                                             "without text), so the span cannot be checked")
                text = page if text is None else text
            if "start" in item and (item["start"] >= item["end"] or (
                    text is not None and item["end"] > len(text))):
                raise ToolError("evidence_span_invalid", "the cited span is empty or runs past "
                                "the cited field's text", "cite the characters the value is read in")
            if any(e["object_id"] == entry["object_id"] for e in out):
                raise ToolError("invalid_input", "a record is cited twice")
            out.append(dict(entry, _text=text if "field_path" in entry else None))
        return out

    def _page(self, handle: str, number: int, caller: Any) -> str:
        """The text of the cited page as ``plx.read`` reads it for ``caller`` (empty when
        none was read); refused unless that read is ``resolved``."""
        if self.pages is None:
            raise _page_not_found()
        try:
            read = self.pages(handle, number, caller)
        except Exception:  # noqa: BLE001 - a read that failed tells nothing of the page
            raise _page_text_unknown("the host could not read the cited page now") from None
        status = read.get("status") if isinstance(read, Mapping) else None
        if status == "resolved":
            content = read.get("content")
            return content if isinstance(content, str) else ""
        if status == "changed_since":
            raise ToolError("evidence_changed", "the cited original has a newer version than "
                            "the one cited", "cite the version plx.read shows")
        gaps = read.get("gaps") if status == "unresolvable" else None
        if isinstance(gaps, list) and any(isinstance(gap, Mapping) and gap.get("state") in
                                          ("unknown", "not_ingested") for gap in gaps):
            reasons = sorted({str(gap.get("reason"))[:64] for gap in gaps
                              if isinstance(gap, Mapping)})[:4]
            raise _page_text_unknown("the host holds no text of the cited page for this "
                                     f"reader ({', '.join(reasons)})")
        raise _page_not_found()

    def _alias(self, alias: Mapping[str, str], evidence: list[dict[str, Any]]
               ) -> dict[str, str]:
        grammar = self.base.grammar
        kind = alias["identifier_kind"]
        if grammar is None or kind not in grammar.kinds():
            raise ToolError("unknown_identifier_kind", f"{kind} is not an identifier kind",
                            "call plx.schema for the identifier kinds")
        normalized = grammar.normalize(kind, alias["value"])
        if not normalized:
            raise ToolError("identifier_invalid", f"the value is not a {kind} identifier")
        kinds = {kind, *grammar.kind_spec(kind).get("alias_kinds", ())}
        for entry in evidence:
            text = entry["_text"]
            if text is None:
                continue
            if grammar.normalize(kind, text) == normalized:
                return {"identifier_kind": kind, "normalized": normalized, "raw": text}
            for mention in grammar.find_identifiers(text):
                if mention.kind in kinds and grammar.normalize(kind, mention.text) == normalized:
                    return {"identifier_kind": kind, "normalized": normalized,
                            "raw": mention.text}
        raise ToolError("alias_not_in_cited_field", "the identifier grammar does not read this "
                        "identifier in a cited field", "cite the field that states it")

    def _allocation(self, scope: Any, subject: str, arguments: Mapping[str, Any]
                    ) -> dict[str, Any]:
        """An allocation's fields once its link type and property are checked against the
        schema and its share against the supply's own values this reader may read (the
        target's visibility is checked by the caller first)."""
        link_type = arguments["link_type"]
        schema = self.base.schema
        declared = (schema.link_types if schema is not None else {}).get(link_type) or {}
        types = {subject.split(":", 1)[0], arguments["target"].split(":", 1)[0]}
        if {declared.get("from"), declared.get("to")} != types or len(types) != 2 \
                or declared.get("cardinality") != "N:M":
            raise ToolError("allocation_needs_many_to_many", f"{link_type} is not declared "
                            "many-to-many between these two records' types",
                            "call plx.schema for the link types")
        prop = arguments["allocation"]["property"]
        spec = next((p for p in (schema.object_types.get(subject.split(":", 1)[0]) or {})
                     .get("properties", ()) if p["name"] == prop), None)
        if spec is None or spec.get("value_type") != "quantity":
            raise ToolError("allocation_property_invalid", f"{prop} is not a quantity "
                            "property of the supply's type", "call plx.schema with object_type")
        # The one value plx.aggregate splits: its pick over the rows this reader can read,
        # in authority order (StoreReader.values orders them as plx.aggregate reads them).
        row = _pick([r for r in self.base.store.values([subject])
                     if r["property"] == prop and self.base._row_visible(scope, r)], False).row
        if row is None:
            raise ToolError("value_not_found", f"no one value of {prop} of this record stands "
                            "for this reader (none, or its sources disagree with no precedence)",
                            "allocate a value plx.object shows")
        share, value = to_decimal(arguments["allocation"]["quantity"]), to_decimal(row["value"])
        unit = unit_token(arguments["allocation"]["unit"])
        if value is None or unit is None or unit_token(row["unit"]) != unit or share > value:
            raise ToolError("allocation_conflict", f"the share does not fit the supply's own "
                            f"{prop}: more than its value or in another unit",
                            "allocate at most the value, in its unit (plx.object shows it)")
        return {"target": arguments["target"], "link_type": link_type,
                "allocation": dict(arguments["allocation"])}

    def _value(self, scope: Any, subject: str, decision: str, spec: Mapping[str, Any],
               evidence: list[dict[str, Any]], version: Mapping[str, Any]) -> dict[str, str]:
        """A value review's property and value (module docstring): a confirmation records
        the cited value as read, a correction the typed value the person read. A
        correction citing only other records also cites the subject's current version
        (``version``, at the corrected value's field), so a new version of the subject
        withdraws it as it withdraws a confirmation."""
        prop = spec["property"]
        rows = [r for r in self.base.store.scoped_currency(
                    scope, self.base.store.values([subject]))
                if r["property"] == prop and self.base._row_visible(scope, r)]
        if not rows:
            raise ToolError("value_not_found", f"no value of {prop} of this record this reader "
                            "can read", "review a value plx.object shows")
        value_type = rows[0]["value_type"]
        grammar = _CORRECTABLE.get(value_type)
        if grammar is None:   # source text is never echoed outside its envelope
            raise ToolError("value_not_reviewable", f"a {value_type} value is not reviewed "
                            "here", "numbers, quantities, money and dates are")
        if decision == "confirm_value":
            cited = {(e["l1_handle"], e.get("field_path")) for e in evidence
                     if e["object_id"] == subject}
            row = next((r for r in rows if (r["l1_handle"], r["field_path"]) in cited
                        and re.fullmatch(_STORED[value_type], r["value"] or "")), None)
            if row is None:
                raise ToolError("value_not_cited", "a confirmation cites the value it confirms",
                                "cite the value's own citation from plx.object")
            return {key: row[key] for key in ("property", "value", "unit", "currency")
                    if row[key] is not None}
        if not any("page" in e or "start" in e for e in evidence):
            raise ToolError("citation_required", "a correction cites the page or the span of "
                            "the original it was read in", "cite the page or span you read")
        text = spec.get("value", "")
        if not re.fullmatch(grammar, text) or (value_type == "date" and _date(text) is None):
            raise ToolError("value_invalid", f"the value is not a {value_type} value")
        unit, currency = spec.get("unit"), spec.get("currency")
        if value_type == "quantity" and unit is None:
            raise ToolError("unit_required", "a quantity is corrected with its unit")
        if value_type == "money" and currency is None:
            raise ToolError("currency_required", "an amount is corrected with its currency")
        if (currency is not None and value_type != "money") or (
                unit is not None and value_type not in ("quantity", "money", "decimal")):
            raise ToolError("unit_not_applicable", f"a {value_type} value has no "
                            + ("currency" if currency is not None else "unit"))
        if all(e["object_id"] != subject for e in evidence):
            corrected = {"object_id": subject, "l1_handle": version["l1_handle"],
                         "digest": version["digest"], "_text": None}
            field = next((r["field_path"] for r in rows
                          if r["l1_handle"] == version["l1_handle"]), None)
            evidence.append(corrected if field is None else dict(corrected, field_path=field))
        out = {"property": prop, "value": text}
        out.update({k: v for k, v in (("unit", unit), ("currency", currency)) if v})
        return out

    def _record(self, arguments: Mapping[str, Any], reader: Any,
                author: Mapping[str, str], caller: Any) -> dict[str, Any]:
        base = self.base
        at = base.clock().astimezone(dt.timezone.utc)
        scope = base._scope(reader, at)
        decision = arguments["decision"]
        tenant = reader.tenant
        if decision == "retract":
            kinds = self.kinds(tenant)
            kind = kinds.get(tenant, arguments["retracts"]) if kinds is not None else None
            if kind is not None and kind.get("channel") == kind_confirmation.REVIEW_CHANNEL:
                # An earlier document type: withdrawn in the one kind store.
                about = _kind_about(kind)
                if not self._readable(scope, about):
                    raise ToolError("review_not_found", "no review this reader can read has "
                                    "that id", "list them with plx.review mode status")
                withdrawal = kinds.withdraw(tenant, kind["id"], author, instant(at))
                return {"mode": "record", "created": True,
                        "review": _kind_view(withdrawal, about["subject"], "effective", None),
                        "projection": "next_projection_build", "business_system_writes": 0,
                        "meta": base._meta(base._store_meta(), at, ())}
            named = self.reviews.get(tenant, arguments["retracts"])
            if named is None or not self._readable(scope, named):
                raise ToolError("review_not_found", "no review this reader can read has that id",
                                "list them with plx.review mode status")
            if named["decision"] in IDENTITY_DECISIONS:
                identity_gate(reader)
            record, created = self.reviews.record(
                tenant=tenant, author=author, decision="retract", subject=named["subject"],
                evidence=[], retracts=named["review_id"],
                identity_gate=lambda: identity_gate(reader))
            projection = _PROJECTION.get(named["decision"], "next_projection_build")
        else:
            subject = arguments["subject"]
            version = self._visible(scope, subject)
            about = [subject] + ([arguments["target"]] if decision in (
                "accept_link", "reject_link", "allocate") else [])
            evidence = self._evidence(scope, list(arguments["evidence"]), about, caller)
            fields: dict[str, Any] = {}
            if decision in ("accept_link", "reject_link", "allocate"):
                self._visible(scope, arguments["target"])
            if decision in ("accept_link", "reject_link"):
                declared = (base.schema.link_types if base.schema is not None else {}).get(
                    arguments["link_type"]) or {}
                if (declared.get("from"), declared.get("to")) != (
                        subject.split(":", 1)[0], arguments["target"].split(":", 1)[0]):
                    raise ToolError("undeclared_link", f"{arguments['link_type']} is not "
                                    "declared between these two records' types",
                                    "call plx.schema for the link types")
                fields = {"target": arguments["target"], "link_type": arguments["link_type"]}
            elif decision == "allocate":
                fields = self._allocation(scope, subject, arguments)
            elif decision in ("alias", "reject_alias"):
                fields = {"alias": self._alias(arguments["alias"], evidence)}
            elif decision in ("confirm_value", "correct_value"):
                fields = {"value": self._value(scope, subject, decision, arguments["value"],
                                               evidence, version)}
            record, created = self.reviews.record(
                tenant=tenant, author=author, decision=decision, subject=subject,
                evidence=[{k: v for k, v in e.items() if k != "_text"} for e in evidence],
                identity_gate=lambda: identity_gate(reader), **fields)
            projection = _PROJECTION[decision]
        state, reason = _states(base, scope, self.reviews.history(tenant, record["subject"])
                                ).get(record["review_id"], ("effective", None))
        return {"mode": "record", "review": _view(record, state, reason), "created": created,
                "projection": projection, "business_system_writes": 0,
                "meta": base._meta(base._store_meta(), at, ())}

    # ------------------------------------------------------------------ status

    def _readable(self, scope: Any, record: Mapping[str, Any]) -> bool:
        return _readable(self.base, scope, record)

    def _replaced(self, scope: Any, record: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The replacements, at the read, of the records a review cites (and its target):
        each with its successor or candidates the reader may read (``withheld``: how
        many it may not, or past the cap)."""
        ids = sorted({item["object_id"] for item in record["evidence"]} |
                     ({record["target"]} if record.get("target") else set()))
        at = scope.at.isoformat()
        out = []
        for object_id, basis, replaced_by in self.base.store.conn.execute(
                "SELECT object_id, replaced_basis, replaced_by FROM object_removals rm "
                f"WHERE object_id IN ({','.join('?' for _ in ids)}) AND "
                "replaced_basis IS NOT NULL AND julianday(since) <= julianday(?) AND "
                "(until IS NULL OR julianday(until) > julianday(?)) ORDER BY object_id, since",
                (*ids, at, at)):
            named = json.loads(replaced_by)
            visible = self.base._visible_versions(scope, named)
            shown = [oid for oid in named if oid in visible][:MAX_CANDIDATES]
            entry: dict[str, Any] = {"object_id": object_id, "basis": basis}
            if basis == "source_replacement":
                if shown:
                    entry["successor"] = shown[0]
            else:
                entry["candidates"] = shown
            if len(shown) < len(named):
                entry["withheld"] = len(named) - len(shown)
            out.append(entry)
        return out

    def _status(self, arguments: Mapping[str, Any], reader: Any) -> dict[str, Any]:
        base = self.base
        at = base.clock().astimezone(dt.timezone.utc)
        scope = base._scope(reader, at)
        subject = arguments["subject"]
        self._visible(scope, subject)
        history = self.reviews.history(reader.tenant, subject)
        states = _states(base, scope, history)
        by_id = {r["review_id"]: r for r in history}
        # Document kinds are read from the one kind store (imported first), with their
        # state there; the review store's copy of an imported one is not listed twice.
        kinds = self.kinds(reader.tenant)
        held = kinds.reviewed(reader.tenant, subject) if kinds is not None else []
        kind_by_id = {record["id"]: record for record, _, _ in held}
        listed: list[tuple[str, Mapping[str, Any] | None, dict[str, Any]]] = []
        for record in history:
            if record["review_id"] in kind_by_id:
                continue
            # A retraction is as readable as the review it names.
            about = by_id.get(record["retracts"]) if record["decision"] == "retract" else record
            listed.append((record["recorded_at"], about,
                           _view(record, *states[record["review_id"]])))
        for record, state, reason in held:
            about = _kind_about(kind_by_id.get(record.get("retracts"), record))
            if state is None:   # in force in the kind store: its cited versions decide
                state, reason = _states(base, scope, [dict(about, review_key=about["review_id"],
                                                           seq=0)])[about["review_id"]]
            listed.append((record["confirmed_at"], about,
                           _kind_view(record, subject, state, reason)))
        shown, withheld = [], 0
        for _, about, view in sorted(listed, key=lambda entry: entry[0]):
            if about is None or not self._readable(scope, about):
                withheld += 1
                continue
            if view.get("state_reason") == "source_removed":
                replaced = self._replaced(scope, view)
                if replaced:
                    view["replaced"] = replaced
            shown.append(view)
        page = shown[-MAX_REVIEWS:]
        truncated = len(page) < len(shown)
        return {"mode": "status", "subject": subject, "reviews": page,
                "withheld": {"reviews": withheld, "reason": "evidence_not_visible"},
                "completeness": _completeness(len(page), len(shown), truncated,
                                              "page_cap" if truncated else None, None),
                "business_system_writes": 0, "meta": base._meta(base._store_meta(), at, ())}


def _fixture_author(session: Any) -> str:
    """A fixture console person as a review author: ``fixture:`` and the host-configured
    fixture email, else the fixture principals, else the session digest; never an
    ``employee:`` principal, so a fixture decision is never read as an employee's."""
    from .ontology_tools_traverse import _REVIEWER

    names = [f"fixture:{name}" for name in sorted(session.identities) if "@" in name]
    names.append("fixture:" + "+".join(sorted(session.principals)))
    return next((name for name in names if _REVIEWER.fullmatch(name)),
                f"fixture:{session.session_ref[:32]}")


def identity_gate(reader: Any) -> Any:
    """The one gate of an identity decision (``IDENTITY_DECISIONS``, their retractions
    and an allocation overruling a rejection, or its retraction), for every writer: ``reader`` (the actor
    the writer's employee gate resolved now) is returned when it holds
    ``IDENTITY_REVIEW``, else ``ToolError`` ``identity_review_required`` before anything
    is written."""
    if IDENTITY_REVIEW not in getattr(reader, "principals", ()):
        raise ToolError("identity_review_required", "an identity decision is recorded only "
                        "by an employee entitled to identity review (review:identity)",
                        "ask an identity reviewer to decide it")
    return reader


def _date(text: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _readable(base: OntologyTools, scope: Any, record: Mapping[str, Any]) -> bool:
    """The reader may read the review: both ends and every cited record now, each cited
    version through its item ACL, and a value review's cited fields under their
    markings."""
    ids = [record["subject"]] + ([record["target"]] if record.get("target") else []) + [
        item["object_id"] for item in record["evidence"]]
    visible = base._visible_versions(scope, ids)
    if not (all(oid in visible for oid in ids) and all(
            base._item_allowed(scope.actor, item["l1_handle"]) for item in record["evidence"])):
        return False
    fields = [(item["l1_handle"], item["field_path"]) for item in record["evidence"]
              if "field_path" in item]
    if "value" not in record or not fields:
        return True
    readable = {(r["l1_handle"], r["field_path"]) for r in base.store.values_by_citation(fields)
                if r["object_id"] in visible and base._row_visible(scope, r)}
    return all(field in readable for field in fields)


def _states(base: OntologyTools, scope: Any, history: list[dict[str, Any]]
            ) -> dict[str, tuple[str, str | None]]:
    """``ontology.reviews.state_at`` of ``history`` in the store the reader reads, at
    the read's time (a link decision pins a cited purchase order's identity there)."""
    conn = base.store.conn
    at = scope.at

    def current_digest(object_id: str) -> str | None:
        row = conn.execute(
            "SELECT digest FROM object_versions WHERE object_id = ? AND "
            "julianday(recorded_at) <= julianday(?) AND (superseded_at IS NULL OR "
            "julianday(superseded_at) > julianday(?))",
            (object_id, at.isoformat(), at.isoformat())).fetchone()
        return None if row is None else row[0]

    def removed(object_id: str) -> bool:
        return conn.execute("SELECT " + removed_at_sql("?", "?"),
                            (object_id, at.isoformat(), at.isoformat())).fetchone()[0] == 1
    return state_at(history, current_digest, removed, version_identity(conn))


def value_reviews(base: OntologyTools, reviews: ReviewStore, scope: Any, object_id: str,
                  properties: Any) -> dict[str, dict[str, Any]]:
    """property -> the value review of ``object_id`` in force at the read (recorded by
    then, neither superseded nor retracted, every cited version current and held by its
    source) that this reader may read (``_readable``), for ``properties``, as
    ``$defs/value_review`` (``value``: the text a person read, as stored). ``scope``:
    an ``OntologyTools._scope``. A review the reader may not read is not returned, so
    neither the correction nor its citation reaches that reader."""
    at = instant(scope.at)
    history = [r for r in reviews.value_history(scope.actor.tenant, object_id)
               if r["recorded_at"] <= at]
    if not history:
        return {}
    states = _states(base, scope, history)
    out: dict[str, dict[str, Any]] = {}
    for record in effective(history).values():
        prop = record["value"]["property"]
        if prop not in properties or states[record["review_id"]][0] != "effective" \
                or not _readable(base, scope, record):
            continue
        shown: dict[str, Any] = {
            "review_id": record["review_id"], "decision": record["decision"],
            "tier": "reviewed", "reviewed_by": record["author"]["principal"],
            "reviewed_at": record["recorded_at"],
            "evidence": [{k: v for k, v in item.items() if k != "object_id"}
                         for item in record["evidence"]]}
        if record["decision"] == "correct_value":
            shown.update({k: record["value"][k] for k in ("value", "unit", "currency")
                          if k in record["value"]})
        out[prop] = shown
    return out


def show_value_reviews(base: OntologyTools, reviews: ReviewStore, scope: Any,
                       object_id: str, values: list[dict[str, Any]]) -> None:
    """Show the value reviews of ``object_id`` in force at the read (``value_reviews``)
    on ``values`` (``plx.object`` values, as this reader may read them), never in their
    place. A confirmation marks the value it cites ``read_by`` ``human_reviewed``, tier
    ``reviewed``; a correction goes beside each value of its property (a machine-read
    document value stays a ``lead``). Each carries ``review``: the decision, tier
    ``reviewed``, who, when, its citations and a correction's value."""
    if not values:
        return
    types = {v["property"]: v["value_type"] for v in values}
    for prop, shown in value_reviews(base, reviews, scope, object_id, types).items():
        if "value" in shown and types[prop] == "integer":
            shown["value"] = int(shown["value"])
        cited = {(e["l1_handle"], e.get("field_path")) for e in shown["evidence"]}
        for value in values:
            if value["property"] != prop:
                continue
            if shown["decision"] == "confirm_value":
                citation = value["citation"]
                if (citation["l1_handle"], citation["field_path"]) not in cited:
                    continue
                value.update(read_by="human_reviewed", tier="reviewed")
            elif value.get("read_by") in _MACHINE_READ:
                value["tier"] = "lead"
            value["review"] = dict(shown)


def correction_inputs(base: OntologyTools, review: Mapping[str, Any]
                      ) -> tuple[list[dict[str, Any]], set[str]]:
    """(the value citations, ``$defs/value_citation``, of a review's cited fields; the
    markings of every version it cites), for an answer that computes from a person's
    corrected value: it cites the correction beside the value it corrects and carries
    the markings of what the correction cites (``plx.aggregate``)."""
    citations: list[dict[str, Any]] = []
    marks: set[str] = set()
    for item in review["evidence"]:
        row = base.store.conn.execute("SELECT lineage, markings FROM object_versions WHERE "
                                      "l1_handle = ? AND digest = ?",
                                      (item["l1_handle"], item["digest"])).fetchall()
        for _, markings in row:
            marks.update(json.loads(markings))
        if row and "field_path" in item:
            citations.append({"l1_handle": item["l1_handle"], "field_path": item["field_path"],
                              "digest": item["digest"], "lineage": row[0][0]})
    return citations, marks


def original_pages(host: Any) -> Callable[[str, int, Any], Mapping[str, Any]]:
    """``ReviewTools.pages`` over a case host: page ``n`` of a cited original read for the
    session actor by the host's one ``plx.read`` reader (``host.evidence.reader``: its
    ``CitationReader.read`` with ``page``), so the review takes a page exactly when
    ``plx.read`` resolves it, through every gate ``plx.read`` reads it with."""
    return lambda handle, number, actor: host.evidence.reader.read(handle, actor,
                                                                   page=number)


def _page_not_found() -> ToolError:
    return ToolError("page_not_found", "the cited original has no such page this reader "
                     "can read (or this host cannot read its pages)",
                     "cite a page plx.read shows, or the span of a field")


def _page_text_unknown(why: str) -> ToolError:
    return ToolError("page_text_unknown", f"what the cited page says is unknown: {why}",
                     "cite the span of a field that states the value, or ask again later")


def _kind_about(record: Mapping[str, Any]) -> dict[str, Any]:
    """A kind-store document type of ``plx.review`` shaped as a review for ``_readable``
    and ``state_at`` (a withdrawal is as readable as the one it withdraws)."""
    return {"review_id": record["id"], "decision": "document_kind",
            "subject": record["original_id"], "evidence": record["evidence"],
            "recorded_at": record["confirmed_at"]}


def _kind_view(record: Mapping[str, Any], subject: str, state: str,
               reason: str | None) -> dict[str, Any]:
    """A kind-store record of ``plx.review`` as a review (``_view``)."""
    out: dict[str, Any] = {
        "review_id": record["id"],
        "decision": "retract" if record.get("retracts") else "document_kind",
        "subject": subject, "evidence": [dict(item) for item in record["evidence"]],
        "author": dict(record["author"]), "recorded_at": record["confirmed_at"],
        "state": state}
    if record.get("retracts"):
        out["retracts"] = record["retracts"]
    else:
        out["document_kind"] = record["portal_document_type"]
    if reason is not None:
        out["state_reason"] = reason
    return out


def _view(record: Mapping[str, Any], state: str, reason: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "review_id": record["review_id"], "decision": record["decision"],
        "subject": record["subject"],
        "evidence": [dict(item) for item in record["evidence"]],
        "author": dict(record["author"]), "recorded_at": record["recorded_at"],
        "state": state}
    for key in ("target", "link_type", "document_kind", "retracts", "allocation", "value"):
        if key in record:
            out[key] = record[key]
    if "alias" in record:
        out["alias"] = {"identifier_kind": record["alias"]["identifier_kind"],
                        "normalized": record["alias"]["normalized"]}
    if reason is not None:
        out["state_reason"] = reason
    return out


def register(host_tools: Any, session: Any) -> None:
    """Serve ``plx.review`` through ``ontology_host.CaseHostTools.call`` for the host's
    session: the host passes its session actor; the reader is that actor through the
    runtime's employee gate, under the policy the host decides for this call. The host
    must hold a review store (``CaseHostTools(reviews=...)``), the same one its
    ``plx.find`` reads aliases from."""
    if host_tools.reviews is None:
        raise ValueError("review_store_not_configured")
    # Built per call: the host rebuilds its tools after close().
    host_tools.register_tool(TOOL_NAME, lambda arguments, actor: ReviewTools(
        host_tools.tools, host_tools.reviews, session=session,
        gate=host_tools.runtime.employee_gate, policy_for=host_tools.policy_now,
        pages=original_pages(host_tools)).call(TOOL_NAME, arguments, actor))


__all__ = ["IDENTITY_REVIEW", "MAX_REVIEWS", "ReviewTools", "TOOL_NAME", "TOOL_NAMES",
           "correction_inputs", "identity_gate", "original_pages", "register",
           "show_value_reviews", "value_reviews"]
