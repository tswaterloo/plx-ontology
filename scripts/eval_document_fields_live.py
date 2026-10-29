"""Live check ``ontology.document_fields_sharepoint``: the document-class measure plus the
exposed extraction items scored against the same capture.

Runs ``operating.sharepoint_host_check --document-class`` (one GET-only SharePoint
capture into the check's state, its projection and the class counts) and adds, over that
capture store and report (``evaluate``; eval tooling calls the product, never the reverse):

* **Denominators** (DOC-03, QA-02, QA-03): per field of the class (``field_outcomes``) and
  per quantity role (``role_outcomes``), how many projected documents the reader left
  ``extracted``, ``blank`` (``value_missing``, ``unchecked``), ``unreadable``, ``ambiguous``
  (two readings or no decided one), ``label_absent`` (the document never names the field:
  its silence), ``label_seen_unread`` (it names the field, by a label or a table row, where
  the reader read no value, or a table row did not line up with its header: a reader
  limit, never the document's silence) or ``other_gap`` (a gap reason this mapping does
  not know: counted, never as extracted). Each row's buckets sum to its ``documents``;
  ``ambiguous_by`` names the gap reasons of its ``ambiguous`` ones and ``unread_by``
  why its seen labels were left unread (``document_fields.UNREAD_REASONS``: the header
  line not read as cells, a row not lined up, a single-spaced label and value, no key
  read, a value not beside its label). ``role_documents`` counts, per role, each
  document once (the host check's ``document_outcomes``) in the first bucket of
  ``ROLE_PRECEDENCE`` its fields hold: a role read on a document, else a reader limit
  there, else what the paper shows (ambiguous, unreadable, blank), else its silence.
  ``roles_unread`` counts the roles with a document in a reader-limit bucket
  (``label_seen_unread``, ``other_gap``: an unknown reason is never a pass): blank,
  ambiguous and unreadable are facts on the paper, reported per role, never unread; a
  role one document read is never read on another for it; the live check wants 0, and
  a report of several documents that does not tell them apart has both null (unknown,
  never a pass). A label left unread where only handwriting OCR could not key is written
  (``document_fields.HANDWRITTEN_UNKEYED``: most of its box's text read below the
  handwriting bound) is its own bucket, ``handwritten_unkeyed``, after the reader limits:
  ``roles_unread_handwritten`` counts the roles with a document there (reported, a typed
  reader limit until an operator keys those boxes, never a pass and never in
  ``roles_unread``) and ``handwritten_packets`` lists, per such packet, each role's
  citation positions (page and span, never text); a printed box no key was read from
  stays ``label_seen_unread`` and in ``roles_unread``. A field's
  role is the schema's ``quantity_role`` of the property it projects into, else of the
  record property its ``attests`` rule compares it with:
  planned, produced, inspected, accepted (released), damaged ... are separate rows, never
  merged. A keyed field (damage per component) also counts its ``entries``.
  ``field_shares`` is each field's ``extracted`` share of the documents that state its
  label (rounded down), which a live check's ``json_min`` bounds per field: a document
  that never names the field (``label_absent``) is outside it, never a miss, and the
  field is never inferred from another label (a retest date is no expiry date); a
  conflict (``ambiguous``), a blank or a reader limit stays in it as a miss. Each
  ``field_outcomes`` row adds ``stated`` (that denominator), ``raw_share`` (the share
  of every document) and ``share_status`` (``measured``; ``insufficient_stated``, a
  null share, when fewer than ``MIN_STATED`` documents state the label: one is no
  share; ``no_documents``). ``--floor NAME=MIN`` (repeatable, a field of the class and
  its minimum share, the same as the live check's ``json_min``) adds ``floors`` (per
  field ``met``, ``below`` or ``insufficient_stated``) and makes the run ``blocked``
  with ``insufficient_stated`` when a floored field has no share and none measured is
  below its minimum (a measured miss is a fail, never blocked).
* **Accuracy** only against operator-interpreted items (``label.status`` ``human_audited``,
  ``evals/item.schema.json``) whose pinned original this capture holds, field by field
  against the runtime reader (``evals.document_fields``). Until such an item exists it is
  ``{"status": "no_operator_items"}``: never measured against a curator's or the
  oracle's reading. ``exposed_items`` / ``exposed_fields`` are the agreement with the
  curated (oracle-only) P2/P8 bundle (correct / wrong / abstain / blocked, recorded;
  the live check wants wrong 0), not accuracy.

* **Funnel** (``funnel``): where every current captured document stopped, from the host
  check's ``stages`` (``ontology.document_fields.stages``): ``files``, ``captured``
  (bytes held), ``members`` (container members read as documents of their own),
  ``text_extracted``, ``ocr_attempted`` / ``ocr_unavailable`` / ``ocr_failed``
  (``ocr_failed_by``: those failed OCR runs by typed cause, ``deadline``, the tool that
  exited (``pdf_unreadable``, ``pdfinfo_exit``, ``text_layer_exit``, ``render_exit``,
  ``recognize_exit``, ``tool_signal``; ``tool_exit``: the macOS helper), ``output_bound``,
  ``render_mismatch``, ``image_page_bound``, ``page_bound``,
  ``page_count``, ``ocr_output_invalid``; ``untyped`` stored before causes were typed,
  ``unknown`` any other; counts only, summing to ``ocr_failed``; a PDF poppler refuses
  and pypdf cannot read is typed by its bytes, ``not_a_pdf``, ``pdf_truncated`` or
  ``pdf_stub`` (whole, no stream: a placeholder), else
  ``pdf_unreadable``), ``ocr_failed_by_portal`` (the same causes crossed with what the
  Portal states on the file: ``portal_stated`` / ``portal_empty`` / ``portal_unread``;
  ``ocr_failed_by_portal_sums``: its cells sum to ``ocr_failed_by``),
  ``classified`` per document class (every class, the measured one included),
  ``excluded`` by reason (the stage reasons, ``other_class``: classified as another
  class, ``not_projected``: of this class but not projected), ``evaluated`` (the
  projected documents of this class) and ``signals`` (per class, documents whose text
  holds its title phrase but not as their class: why), ``ambiguous_class`` (the
  ``ambiguous_class`` documents by cause, ``prose_title`` or ``title_lines``, of
  ``class_read`` documents whose class was decided, and ``prose_title_rate``, their
  share; null when no class was read). ``sums``: every document is
  counted once (``files + members == evaluated + excluded``). ``dropped_at`` names the
  first stage (``capture``, ``text``, ``classification``, ``class``, ``projection``) that
  left no document; null once one is evaluated. ``no_candidate`` (null once one is
  evaluated) types why none was, from these counts alone: ``stage`` (``dropped_at``),
  ``reason`` (``no_files`` / ``no_bytes_captured`` at capture, ``no_text_read``,
  ``not_projected``, and at classification or class what the read text held of the
  class's title: ``title_phrase_not_title`` when a document holds its phrase but not as
  its title (``signals``), else ``no_title_phrase_unread_documents`` when some documents
  were left unread before their class was read (``unread_kinds``: a customer PO can only
  be among them), else ``no_title_phrase``), ``unread_documents``, ``title_signals`` and
  ``portal_metadata_documents`` (the versions the Portal states columns on). The class is
  read from title text only: the Portal's DocumentCategory column cannot stand in for it,
  since the mirrored taxonomy writes one shared value (``OTHER``) for many categories
  (``operating.doc_metadata_taxonomy.DOCUMENT_TYPES``, CUSTOMER_PO included).
  ``truncated``: the host check listed fewer current files than were captured (its
  store's read-back bound); the counts are a lower bound and the run is never ``ok``.
* **Unevaluated**: documents left unread because this host has no OCR engine
  (``excluded.ocr_unavailable``) are a counted gap (``unevaluated.ocr_unavailable``); the
  documents that were evaluated are still scored. Only when none was evaluated and some
  were left unread so is the run ``blocked`` with ``ocr_unavailable``: never a pass and
  never "no documents" (``blocked_when``); the counts are still printed. When every
  document was read and none is of the class (``no_class_document``: no title signal,
  each one read as another class, no class, the own party's, or a stub: a ``pdf_stub``
  OCR failure, a file under ``TINY_FILE_BYTES`` with no OCR text, a tiny unsupported
  image), the run is ``blocked`` with ``no_class_document``, a source gap: the folder
  holds no document of the class to read. Only over a whole capture
  (``capture_complete``); a cut capture, or any file that may hide one, keeps the fail.

Links (``--links``, live check ``ontology.document_links``, QA-04): whether a captured
document links to the right Portal/BC record. The same GET-only SharePoint capture, plus
one GET-only BC capture of the entity sets the host captures (``--bc-capture`` of
``scripts/ontology_projection_probe.py``: ``capture_bc``) and, when the Portal is served
(``PLX_WORKBENCH_API_URL`` and ``PLX_WORKBENCH_API_KEY`` set), a Portal mirror of the
relations bound to the record types the captured documents can use (``link_relations``
bound to the kinds each document type's projected documents reference: the relations
those kinds are looked up in and those its comparisons reach; ``portal_mirror.relations``
of ``relations_declared``; the lookups' first, ``--portal-workers`` at a time), all laid
out as a host state directory and projected together. Each
document-field object's references (``evidence.references``: a profile's ``refers_to``
field, keyed by the field, else the identifier grammar's reading, keyed by its kind) are
read through the served host (``operating.host.Host.served_tools``: ``plx.around`` over the
class's declared mention link types) as ``--principal``, and each gets one outcome
(``OUTCOMES``): ``linked_one`` by the served link's tier (one candidate record in the
projection, its link served, and every carrier relation below captured whole),
``ambiguous`` (two or more candidate entities: never one picked; candidate records a
verified or reviewed identity link, ``same_record_as``, joins are one entity, a Portal record
and its twin in another system, and a lead or contradicted one never joins them;
``ambiguous_candidates``: the ambiguous references by their number of distinct entities,
``identity_joined``: the references whose candidates such a link joined), ``withheld`` (candidates exist, the reader is served none of their links),
``no_record`` (no candidate, and every served carrier relation of the reference's kind
bound to a record type the class may link to was captured whole) or
``source_not_captured`` (no candidate or one, and some such relation was not captured
whole: a Portal not served, a BC capture that failed or did not finish). A reference no declared
link type can reach (no carrier relation bound to a linkable type) is ``not_linkable``,
outside the outcomes. For a ``linked_one`` over the class's ``attests`` link type, every
declared comparison with that record is ``agree`` / ``conflict`` / ``not_comparable``
(``operating.attested_conflicts.agreement``, as the reader may compare it); each
``not_comparable`` is counted under the one cause that comparison decided
(``agreement_with_causes``; one outside ``NOT_COMPARABLE_CAUSES`` is ``unclassified``) in
``agreement_not_comparable`` and per profile and field in ``not_comparable_by_field``.
``cross_scope_corroborated`` counts references whose candidates span two scopes (two BC
companies, or two entities) yet came out ``linked_one`` or hold an inferred link above
``lead``: must be 0. ``leaks`` counts what the served host gives ``--withheld-principal``
(granted the documents, no record source) over those link types: edges, objects and
references it is served as linked or ambiguous: must be 0. A run with neither BC records
nor a Portal relation captured is ``blocked`` with ``no_record_source``; one whose OCR-less
host left documents unread and projected none is ``blocked`` with ``ocr_unavailable``.
``links_measured`` (``linked_one`` + ``ambiguous`` + ``no_record``: references a captured
record source answered either way) must be at least 1: a run where every reference is
``source_not_captured``, ``withheld`` or ``not_linkable`` measured no link and is never
``ok``. ``requests_sent`` counts the SharePoint capture's, the BC capture's and the
Portal mirror's requests (``portal_mirror.requests_sent``: its client's GETs).
``--budget-seconds`` bounds the whole run: each read phase (SharePoint, then the Portal,
then BC) gets at most what that budget leaves after a reserve sized from this run's own
cost and the estimate that follows the read (none left: the phase is not run). The
reserve is measured after every read (``reserve``; ``estimate_seconds``: the last
estimate's own) in two projections, each served and up to ``MEASURE_SAMPLE`` of its
documents measured as the run measures every one (both readers), into a scratch store
served in place of the state's projection, which is set aside and put back after (and at
the start of the next run when one was killed mid-estimate): the document sources alone,
whole (once: the documents the run measures, ``documents``, and the identifiers they
reference), and those with every other relation's rows a 1-in-``SAMPLE_STRIDE`` stride
keeps plus every row that names a referenced identifier (a source samples its own
queries and never decodes the rows it drops). The documents' seconds, plus the sample's
own reading of every row once (the projection reads each too; its filter not at all),
plus what the kept rows added beyond that, scaled by the rows stepped over to the rows
kept, are ``projection_estimate_seconds``; the mean seconds of the documents measured over the
second projection, where each holds the records it links, are
``measure_seconds_per_document`` (measured, never scaled), and times ``documents``
``measure_estimate_seconds``; the three are held with ``RESERVE_MARGIN`` (the
estimates' error); a sample that raised is ``failed`` (its error code) and no further
phase is read. BC, read last, reads in slices (``bc_slices``; a slice resumes the units
the one before it paused and reuses the ones it completed), with the reserve
measured again after each: a slice reads the ``t`` seconds after which the reserve and
``t`` times the projection seconds a read second adds (BC's own once a slice measured
it, else this run's reads so far; never less than 1) still fit, until BC finishes or a
slice would be no longer than an estimate; the reserve recorded is measured after the last
slice. A BC session start (``FRESH_SINCE_ENV``, else ``FRESH_SINCE_FILE`` in the state: a
run that left BC unfinished keeps it for the next run on that state, so a paused unit it
resumes is not stale) lets a slice reuse the units this session completed. Measurement
always measures the first document and reads no further one once the budget is spent. ``phase_seconds`` records each phase's
seconds (``reserve``: the estimates'). A run that left documents unread (a
SharePoint root left ``time_budget``, by either budget), a record source unread or
documents unmeasured is ``incomplete`` (``reason`` ``budget``, the phases ``cut``, the
documents measured and unmeasured; ``reason`` ``sample_projection_failed`` and ``reserve``
cut when a sample raised): never ``ok`` and never ``blocked``.

Prints counts, booleans and enums only. Live-unverified until the verifier records a
result; tests: ``ontology/tests/test_quality_packets.py`` and
``tests/test_eval_document_fields_live.py`` (injected transport).

    python3 -m scripts.eval_document_fields_live --state-dir ... --document-class ao_packet
    python3 -m scripts.eval_document_fields_live --links --state-dir ... --scope ... \
        --live-audience ...
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Iterable, Mapping

from evals import document_fields as scoring
from integrations.captured_text import (OCR_FAILED, OCR_NO_TEXT, OCR_UNAVAILABLE, TEXT_OCR,
                                        TEXT_READ, text_status_kind)
from evals.runner import VISIBLE_ITEM_DIRS, load_items
from ontology import document_fields
from operating import sharepoint_host_check as check
# The tiers at which an identity link establishes one record held in two systems: the
# ontology tools' one rule.
from operating.ontology_tools import _IDENTITY_TIERS as IDENTITY_TIERS

BUCKETS = ("extracted", "blank", "unreadable", "ambiguous", "label_absent",
           "label_seen_unread", "handwritten_unkeyed", "other_gap")
# Gap reasons of ontology/document_fields.py by what they say about the paper.
REASONS = {"value_missing": "blank", "unchecked": "blank", "not_stated": "label_absent",
           document_fields.LABEL_SEEN_UNREAD: "label_seen_unread",
           document_fields.HANDWRITTEN_UNKEYED: "handwritten_unkeyed",
           "unaligned": "label_seen_unread",
           "unreadable": "unreadable", "number_unreadable": "unreadable",
           "unit_unknown": "unreadable", "currency_unknown": "unreadable",
           "too_long": "unreadable", "date_ambiguous": "ambiguous",
           "decimal_separator_ambiguous": "ambiguous", "several_values": "ambiguous",
           document_fields.SPLIT_AT_GAP: "ambiguous",
           "conflicting_values": "ambiguous", "currency_conflict": "ambiguous",
           "key_missing": "ambiguous"}
# A role's document outcome, the first its fields hold: a read value, then what the paper
# shows (two readings, an unreadable mark, a blank box) only where no reader limit (a
# label seen unread, a gap reason this mapping does not know: unknown, never a pass) is.
ROLE_PRECEDENCE = ("extracted", "label_seen_unread", "other_gap", "handwritten_unkeyed",
                   "ambiguous", "unreadable", "blank", "label_absent")
READER_LIMITS = ("label_seen_unread", "other_gap")
OPERATOR_LABEL = "human_audited"
OCR_RAN = (TEXT_OCR, OCR_FAILED, OCR_NO_TEXT)   # text statuses after an OCR run
_FIELD_PATH = re.compile(r"fields\.([a-z0-9_]+)\.(?:value|entries)")


def _buckets(tally: Mapping[str, Any]) -> dict[str, int]:
    out = dict.fromkeys(BUCKETS, 0)
    for key, count in tally.items():
        if key == "entries" or not isinstance(count, int):
            continue
        out["extracted" if key == "stated" else REASONS.get(key, "other_gap")] += count
    return out


def field_roles(document_class: str) -> dict[str, str]:
    """``field -> quantity role`` of a class's fields (module docstring)."""
    from ontology.schema import load_schema

    schema = load_schema()
    profile = next(p for p in document_fields.profiles()["profiles"]
                   if p["id"] == document_class)
    roles: dict[str, str] = {}
    for prop in schema.object_types.get(profile["object_type"], {}).get("properties", ()):
        for source in prop["sources"]:
            m = _FIELD_PATH.fullmatch(source["path"])
            if m and prop.get("quantity_role"):
                roles[m[1]] = prop["quantity_role"]
    for rule in document_fields.comparisons():
        if rule["document_type"] != profile["object_type"] or rule["field"] in roles \
                or "property" not in rule:
            continue
        at = schema.link_types[rule["link_type"]]["to"]
        for hop in rule["path"]:
            link = schema.link_types[hop["link_type"]]
            at = link["from"] if hop["direction"] == "reverse" else link["to"]
        role = next((p.get("quantity_role") for p in schema.object_types[at]["properties"]
                     if p["name"] == rule["property"]), None)
        if role:
            roles[rule["field"]] = role
    return roles


# The fewest documents stating a field's label its share is measured over: one stated
# document is no share (a floor of 2 of 3 cannot be met or missed on it).
MIN_STATED = 2
SHARE_STATUSES = ("measured", "insufficient_stated", "no_documents")
INSUFFICIENT_STATED = "insufficient_stated"


def _share(part: int, whole: int) -> float:
    return math.floor(part * 10000 / whole) / 10000   # rounded down: 2 of 3 is 0.6666


def stated_share(row: Mapping[str, int]) -> dict[str, Any]:
    """A field row's ``stated`` (its documents but those that never name the field:
    ``label_absent`` is the document's silence, never a miss; a conflict, a blank, an
    unread or unknown gap stays a miss in it), ``share`` (``extracted`` over ``stated``,
    rounded down; None below ``MIN_STATED``), ``raw_share`` (``extracted`` over every
    document) and ``share_status`` (``SHARE_STATUSES``)."""
    documents, stated = row["documents"], row["documents"] - row["label_absent"]
    status = ("no_documents" if not documents else INSUFFICIENT_STATED
              if stated < MIN_STATED else "measured")
    return {"stated": stated, "raw_share": _share(row["extracted"], documents)
            if documents else None,
            "share": _share(row["extracted"], stated) if status == "measured" else None,
            "share_status": status}


def field_shares(fields: Mapping[str, Mapping[str, Any]]) -> dict[str, float | None]:
    """Per field, the share of the documents stating it the reader left it ``extracted``
    (:func:`stated_share`: never overstated, 2 of 3 is 0.6666), so a live check states its
    own minimum share per field (``json_min``); None over fewer than ``MIN_STATED`` stating
    documents (unknown, which no minimum passes)."""
    return {name: stated_share(row)["share"] for name, row in sorted(fields.items())}


def floor_outcome(fields: Mapping[str, Mapping[str, Any]],
                  floors: Mapping[str, Decimal]) -> dict[str, Any] | None:
    """Per floored field (``--floor NAME=MIN``) ``met``, ``below`` or ``insufficient_stated``
    (no share: stated on fewer than ``MIN_STATED`` documents), and ``blocked``
    (``insufficient_stated`` when some floored field has no share and none measured falls
    below its minimum: a run that missed a measured floor is a fail, never blocked behind
    a field the source left too thin); None with no floor."""
    if not floors:
        return None
    by_field = {}
    for name, minimum in sorted(floors.items()):
        share = stated_share(fields[name])["share"]
        by_field[name] = (INSUFFICIENT_STATED if share is None else
                          "met" if Decimal(str(share)) >= minimum else "below")
    thin = INSUFFICIENT_STATED in by_field.values() and "below" not in by_field.values()
    return {"fields": by_field, "blocked": INSUFFICIENT_STATED if thin else None}


def denominators(report: Mapping[str, Any]) -> dict[str, Any]:
    """``field_outcomes``, ``role_outcomes``, ``role_documents`` and ``roles_unread`` of a
    ``--document-class`` report."""
    documents = report["documents"]
    profile = next(p for p in document_fields.profiles()["profiles"]
                   if p["id"] == report["document_class"])
    tallies = report.get("fields") or {}
    fields: dict[str, dict[str, Any]] = {}
    for spec in profile["fields"]:
        tally = tallies.get(spec["name"], {})
        row: dict[str, Any] = {"documents": documents, **_buckets(tally)}
        row.update((k, v) for k, v in stated_share(row).items() if k != "share")
        row["ambiguous_by"] = {reason: count for reason, count in sorted(tally.items())
                               if REASONS.get(reason) == "ambiguous" and count}
        row["unread_by"] = dict(sorted((tally.get("unread_by") or {}).items()))
        if "keyed" in spec:
            row["entries"] = _buckets(tally.get("entries", {}))
        fields[spec["name"]] = row
    roles: dict[str, dict[str, int]] = {}
    of_role: dict[str, list[str]] = {}
    for name, role in sorted(field_roles(report["document_class"]).items()):
        into = roles.setdefault(role, {"documents": 0, **dict.fromkeys(BUCKETS, 0),
                                       "unread_by": {}})
        for key in ("documents", *BUCKETS):
            into[key] += fields[name][key]
        for why, count in fields[name]["unread_by"].items():
            into["unread_by"][why] = into["unread_by"].get(why, 0) + count
        of_role.setdefault(role, []).append(name)
    outcomes = _document_outcomes(report)
    by_role = None if outcomes is None else {
        role: _role_documents(names, outcomes) for role, names in sorted(of_role.items())}
    unread = None if by_role is None else sum(
        1 for row in by_role.values() if any(row[bucket] for bucket in READER_LIMITS))
    handwritten = None if by_role is None else sum(
        1 for row in by_role.values() if row["handwritten_unkeyed"])
    packets = [row for packet in report.get("handwritten_unkeyed") or ()
               if (row := {role: [c for name in names for c in packet["fields"].get(name, ())]
                           for role, names in sorted(of_role.items())
                           if any(name in packet["fields"] for name in names)})]
    return {"field_outcomes": fields, "field_shares": field_shares(fields),
            "role_outcomes": roles, "role_documents": by_role,
            "roles_unread": unread, "roles_unread_handwritten": handwritten,
            "handwritten_packets": packets}


def _role_documents(names: list[str], outcomes: list[tuple[Mapping[str, str], int]]
                    ) -> dict[str, int]:
    """How many documents a role's fields left in each bucket, each document counted once,
    in the first bucket of ``ROLE_PRECEDENCE`` its fields hold (module docstring)."""
    row = dict.fromkeys(ROLE_PRECEDENCE, 0)
    for outcome, documents in outcomes:
        found = {_bucket(outcome.get(name, "not_stated")) for name in names}
        row[next(bucket for bucket in ROLE_PRECEDENCE if bucket in found)] += documents
    return row


def _bucket(outcome: str) -> str:
    return "extracted" if outcome == "stated" else REASONS.get(outcome, "other_gap")


def _document_outcomes(report: Mapping[str, Any]
                       ) -> list[tuple[Mapping[str, str], int]] | None:
    """Each document's outcome per field and how many documents had it
    (``document_outcomes``: one entry per distinct combination); a one-document report's
    field tallies are its document's; None when a report of several documents does not
    tell them apart."""
    if "document_outcomes" in report:
        return [(row["fields"], row["documents"]) for row in report["document_outcomes"]]
    if report["documents"] != 1:
        return None
    return [({name: key for name, tally in (report.get("fields") or {}).items()
             for key, count in tally.items() if isinstance(count, int) and count}, 1)]


def accuracy(store: Any, items: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Field outcomes against the operator-interpreted ones of ``items`` (default: the
    visible eval items) only (module docstring)."""
    if items is None:
        items = [item for directory in VISIBLE_ITEM_DIRS
                 for item in load_items(directory, split="dev")]
    scored = scoring.score_items(
        [item for item in items if (item.get("label") or {}).get("status") == OPERATOR_LABEL],
        scoring.sharepoint_originals(store))
    if not scored["items"]:
        return {"status": "no_operator_items", "items": 0}
    held = sum(item["status"] != "blocked" for item in scored["items"])
    return {"status": "measured" if held else "originals_not_captured", "items": held,
            "fields": scored["counts"]}


def score_exposed(store: Any) -> dict[str, Any]:
    """Field outcome counts of the exposed items over ``store`` (a capture store)."""
    scored = scoring.score_items(load_items(VISIBLE_ITEM_DIRS[1], split="dev"),
                                 scoring.sharepoint_originals(store))
    return {"exposed_items": sum(item["status"] != "blocked" for item in scored["items"]),
            "exposed_fields": scored["counts"]}


def funnel(report: Mapping[str, Any]) -> dict[str, Any]:
    """The stage funnel of a ``--document-class`` report (module docstring)."""
    stages, target = report["stages"], report["document_class"]
    text, classes = stages["text"], stages["classes"]
    excluded = dict(stages["excluded"])
    other = sum(n for profile, n in classes.items() if profile != target)
    missing = classes.get(target, 0) - report["documents"]
    excluded.update({"other_class": other, "not_projected": max(missing, 0)})
    out = {
        "files": stages["files"], "members": stages["members"],
        "captured": stages["files"] - sum(n for reason, n in stages["excluded"].items()
                                          if reason.startswith("content_")),
        "text_extracted": sum(text.get(status, 0) for status in TEXT_READ),
        "ocr_attempted": sum(text.get(status, 0) for status in OCR_RAN),
        "ocr_unavailable": text.get(OCR_UNAVAILABLE, 0),
        "ocr_failed": text.get(OCR_FAILED, 0),
        "ocr_failed_by": dict(sorted(stages["ocr_failed_by"].items())),
        "classified": {p["id"]: classes.get(p["id"], 0)
                       for p in document_fields.profiles()["profiles"]},
        "excluded": dict(sorted((k, v) for k, v in excluded.items() if v)),
        "evaluated": report["documents"], "signals": stages["signals"],
        "unread_kinds": stages["unread_kinds"], "truncated": stages.get("truncated") is True}
    read = sum(classes.values()) + sum(stages["excluded"].get(reason, 0) for reason in (
        "unclassified", "ambiguous_class", document_fields.OWN_ISSUED))
    prose = stages["ambiguous"]["prose_title"]
    out["ambiguous_class"] = {**stages["ambiguous"], "class_read": read,
                              "prose_title_rate": round(prose / read, 4) if read else None}
    out["sums"] = missing >= 0 and \
        out["files"] + out["members"] == out["evaluated"] + sum(excluded.values())
    reached = (("capture", out["captured"]), ("text", out["text_extracted"]),
               ("classification", sum(classes.values())), ("class", classes.get(target, 0)),
               ("projection", out["evaluated"]))
    out["dropped_at"] = next((stage for stage, n in reached if not n), None)
    out["no_candidate"] = no_candidate(report, out)
    return out


def no_candidate(report: Mapping[str, Any], out: Mapping[str, Any]) -> dict[str, Any] | None:
    """Why no document of the class was evaluated, from the funnel's counts alone (module
    docstring, Funnel); None once one is."""
    stage = out["dropped_at"]
    if stage is None:
        return None
    unread = sum(out["unread_kinds"].values())
    signals = sum((out["signals"].get(report["document_class"]) or {}).values())
    if stage == "capture":
        reason = "no_bytes_captured" if out["files"] else "no_files"
    elif stage == "text":
        reason = "no_text_read"
    elif stage == "projection":
        reason = "not_projected"
    else:   # classification, class: what the read text held of the class's title
        reason = "title_phrase_not_title" if signals else \
            "no_title_phrase_unread_documents" if unread else "no_title_phrase"
    portal = report.get("portal_metadata")
    return {"stage": stage, "reason": reason, "unread_documents": unread,
            "title_signals": signals, "portal_metadata_documents":
            portal.get("documents_with_portal_metadata") if isinstance(portal, Mapping)
            else None}


PORTAL_STATES = ("portal_stated", "portal_empty", "portal_unread")


def ocr_failed_by_portal(store: Any) -> dict[str, dict[str, int]]:
    """The failed OCR runs of the capture's current files by typed cause and by what the
    Portal states on the file: ``portal_stated`` (a Portal-taxonomy column is filled: the
    Portal uploaded or tagged it), ``portal_empty`` (its columns were read, none stated),
    ``portal_unread`` (no column snapshot bound to the version). Counts only."""
    from integrations.captured_text import ocr_failure_cause
    from operating import doc_metadata_taxonomy as taxonomy
    out: dict[str, dict[str, int]] = {}
    rows = store.db.execute("SELECT text_status, record FROM items WHERE "
                            "superseded_at IS NULL AND text_status LIKE 'ocr_failed%'")
    for row in rows.fetchall():
        cause = ocr_failure_cause(str(row["text_status"]))
        if cause is None:
            continue
        stated = taxonomy.stated_metadata(json.loads(row["record"]))
        state = ("portal_unread" if stated is None else
                 "portal_stated" if stated["columns"] else "portal_empty")
        cell = out.setdefault(cause, {})
        cell[state] = cell.get(state, 0) + 1
    return {cause: dict(sorted(cell.items())) for cause, cell in sorted(out.items())}


NO_CLASS_DOCUMENT = "no_class_document"
# What a document left out of the class may have been read as for the source to hold none:
# its class read (another, none, the own party's), or a stub (below): OCR failed on a
# ``pdf_stub``, OCR read no text of a tiny file, an unsupported file is a tiny image.
# Anything else (unreadable, ambiguous_class, content_*, reread_*, not_projected, another
# OCR cause, a larger file) may hide one of the class.
READ_EMPTY = ("other_class", "unclassified", document_fields.OWN_ISSUED, OCR_NO_TEXT,
              OCR_FAILED, "unsupported")
TINY_FILE_BYTES = 1024   # under this no file holds a legible page: a scan's image is larger


def stub_files(store: Any) -> dict[str, tuple[int, int]]:
    """The capture's current files stored ``ocr_no_text`` or ``unsupported``, per status:
    (all, tiny), tiny being under ``TINY_FILE_BYTES`` (an unsupported one an image too)."""
    out = {OCR_NO_TEXT: (0, 0), "unsupported": (0, 0)}
    rows = store.db.execute(
        "SELECT text_status, record FROM items WHERE superseded_at IS NULL AND "
        "(text_status LIKE 'unsupported%' OR text_status LIKE 'ocr_no_text%')")
    for row in rows.fetchall():
        status = text_status_kind(str(row["text_status"]))
        if status not in out:
            continue
        record = json.loads(row["record"])
        size = record.get("size")
        tiny = isinstance(size, int) and not isinstance(size, bool) and \
            0 <= size < TINY_FILE_BYTES and (status == OCR_NO_TEXT or str(
                record.get("mime_type") or "").startswith("image/"))
        every, small = out[status]
        out[status] = (every + 1, small + tiny)
    return out


def no_class_document(out: Mapping[str, Any], stubs: Mapping[str, tuple[int, int]]) -> bool:
    """The funnel ``out`` read every document through and none is of the class: none was
    evaluated, the class's title is on no text (``title_signals`` 0, a classification or
    class drop), and each excluded document was read as empty of it (``READ_EMPTY``):
    every failed OCR run on a ``pdf_stub``, and every ``ocr_no_text`` or unsupported one
    a tiny file (``stubs``: :func:`stub_files`). A source gap, never a fail (module
    docstring, Unevaluated)."""
    excluded, why = out["excluded"], out["no_candidate"]
    return not out["evaluated"] and not out["truncated"] and why is not None and \
        why["stage"] in ("classification", "class") and why["title_signals"] == 0 and \
        not out["ocr_unavailable"] and set(excluded) <= set(READ_EMPTY) and \
        set(out["ocr_failed_by"]) <= {"pdf_stub"} and \
        all(excluded.get(status, 0) == every == tiny
            for status, (every, tiny) in stubs.items())


def evaluate(store: Any, report: Mapping[str, Any],
             floors: Mapping[str, Decimal] | None = None) -> dict[str, Any]:
    """What this live entry adds to the host check's ``--document-class`` report;
    ``floors`` (``--floor``): :func:`floor_outcome`."""
    out = {**score_exposed(store), **denominators(report), "accuracy": accuracy(store),
           "funnel": funnel(report)}
    crosstab = ocr_failed_by_portal(store)
    out["funnel"]["ocr_failed_by_portal"] = crosstab
    out["funnel"]["ocr_failed_by_portal_sums"] = (
        {cause: sum(cell.values()) for cause, cell in crosstab.items()}
        == out["funnel"]["ocr_failed_by"])
    unread = out["funnel"]["excluded"].get(OCR_UNAVAILABLE, 0)
    out["unevaluated"] = {OCR_UNAVAILABLE: unread}
    if unread and not out["funnel"]["evaluated"]:
        out["blocked"] = OCR_UNAVAILABLE
    elif report.get("capture_complete") is True and \
            no_class_document(out["funnel"], stub_files(store)):
        # only over a whole capture: a root the budget cut may hold one never read
        out["blocked"] = NO_CLASS_DOCUMENT
    elif out["funnel"]["evaluated"] and \
            (floor := floor_outcome(out["field_outcomes"], floors or {})) is not None:
        out["floors"] = floor["fields"]
        if floor["blocked"]:
            out["blocked"] = floor["blocked"]
    return out


def _floors(argv: list[str]) -> tuple[list[str], dict[str, Decimal]]:
    """``--floor NAME=MIN`` (repeatable) taken out of a ``--document-class`` argv: a field
    of that class and a minimum share in (0, 1]; anything else is a usage error (exit 2),
    never a floor silently dropped."""
    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_document_fields_live",
                                     allow_abbrev=False, add_help=False)
    parser.add_argument("--floor", action="append", default=[])
    parser.add_argument("--document-class")
    args, rest = parser.parse_known_args(argv)
    if not args.floor:
        return argv, {}
    profile = next((p for p in document_fields.profiles()["profiles"]
                    if p["id"] == args.document_class), None)
    if profile is None:
        parser.error("--floor needs --document-class naming a document class")
    names = {spec["name"] for spec in profile["fields"]}
    floors: dict[str, Decimal] = {}
    for given in args.floor:
        name, _, value = given.partition("=")
        try:
            minimum = Decimal(value)
        except ArithmeticError:
            minimum = Decimal("NaN")
        if name not in names or name in floors or not (minimum.is_finite()
                                                       and 0 < minimum <= 1):
            parser.error(f"--floor wants NAME=MIN, a field of {profile['id']} once and "
                         f"0 < MIN <= 1: {given!r}")
        floors[name] = minimum
    return [*rest, "--document-class", args.document_class], floors


LINKS_CHECK = "ontology.document_links"
OUTCOMES = ("linked_one", "ambiguous", "no_record", "source_not_captured", "withheld")
# Tiers an inferred link may hold above a lead; ``reviewed`` is a person's decision.
INFERRED_ABOVE_LEAD = ("verified", "corroborated")
WITHHELD_PRINCIPAL = "staff:document-links-withheld"
HOST_CONFIG = "links-host.json"
NO_RECORD_SOURCE = "no_record_source"
BUDGET = "budget"
# The reserve's sample: one row in SAMPLE_STRIDE of every captured record relation, and
# every row that names an identifier a document references, are read and projected with
# the documents whole, to time the projection (a model: what the documents cost alone is
# not scaled, what the kept rows add is scaled up to every row) and measure documents
# over the records they link. A source samples its own queries
# (``ontology.project.RowSample``), so the rows it drops are never decoded.
SAMPLE_STRIDE = 16
# The share of the estimates (their own seconds, the projection's and the measurement's)
# held on top of them, for the sample's error and BC sets of uneven density: on the
# injected world the stride's estimate ran up to a fifth short of the projection it sized.
RESERVE_MARGIN = 0.5
# The documents an estimate measures (the first ones, both readers each): their mean is
# ``reserve.measure_seconds_per_document``. The live runs of 2026-10-13 measured about
# 0.17 s a document with no record linked and about 22 s once BC and Portal records were,
# so one cheap first document (or a sample that linked none) under-reserved the rest.
MEASURE_SAMPLE = 4
# The scratch of an estimate in the state directory: its sample store, the state's
# projection set aside while the sample is served in its place (RESERVE_KEPT), and the
# mark that the projection path may hold the sample (RESERVE_SERVED).
RESERVE_DIR, RESERVE_KEPT, RESERVE_SERVED = "links-reserve", "kept.sqlite", "served"
# A sample projection that raised (``reserve.failed``): the reads stop, never a pass.
RESERVE_FAILED = "sample_projection_failed"
# The BC session start (``bc_read.FRESH_SINCE_ENV``) a run left unfinished, kept in its
# state directory: the next run on it within FRESH_SINCE_HOURS resumes its paused units
# as that session's, not stale; a run that finished BC starts a new one.
FRESH_SINCE_FILE = "links-fresh-since"
FRESH_SINCE_HOURS = 24


def link_relations(schema: Any, documents: Mapping[str, Iterable[str]] | None = None,
                   compared: bool = True) -> list[str]:
    """The Portal relations bound to the record types a document class may link to (its
    declared mention link types) or compare with (its ``attests`` paths). ``documents``
    (document object type -> the identifier kinds its projected documents reference)
    binds them to what those documents can use: of each type, the Portal relations a
    reference of one of its kinds is looked up in (``_linkable``) and, when it references
    one, the ones its comparisons reach (not ``compared``: the lookups' alone); a type the
    run holds no document of adds none. What ``--links`` mirrors when the Portal is
    served."""
    from ontology import mentions

    if documents is None:
        held = {p["object_type"] for p in document_fields.profiles()["profiles"]}
        types = {target for (source, target) in mentions.mention_link_types(
            schema.link_types) if source in held}
        out: set[str] = set()
    else:
        held = {t for t, kinds in documents.items() if kinds}
        types = set()
        out = {relation for t, kinds in documents.items() for kind in kinds
               for (source, relation) in _linkable(schema, t, kind) if source == "portal_api"}
    for rule in document_fields.comparisons():
        if rule["document_type"] not in held or not compared:
            continue
        at = [schema.link_types[rule["link_type"]]["to"]]
        for hop in [*rule["path"], *rule.get("key_path", ())]:
            link = schema.link_types[hop["link_type"]]
            at.append(link["from"] if hop["direction"] == "reverse" else link["to"])
        types.update(at)
    return sorted(out | {b["relation"] for t in types
                         for b in schema.object_types.get(t, {}).get("bindings", ())
                         if b["source"] == "portal_api"})


def _linkable(schema: Any, document_type: str, kind: str) -> list[tuple[str, str]]:
    """(source, relation) of the bindings of the record types ``document_type`` may link
    to that a served carrier of ``kind`` (or of an alias kind the candidate lookup also
    searches) names: where a record this reference names would be projected from."""
    from ontology import identifiers, mentions

    if kind not in identifiers.kinds():
        return []   # a kind the grammar does not declare names no record: never no_record
    targets = {target for (source, target) in mentions.mention_link_types(schema.link_types)
               if source == document_type}
    carried = {c["relation"] for k in (kind, *identifiers.alias_kinds(kind))
               if k in identifiers.kinds() for c in identifiers.carriers(k)
               if c.get("served", True)}
    return sorted({(b["source"], b["relation"]) for t in targets
                   for b in schema.object_types.get(t, {}).get("bindings", ())
                   if b["relation"] in carried})


def _candidates(conn: sqlite3.Connection, document: str) -> list[sqlite3.Row]:
    """The document's current document-field links (``store_document_links``)."""
    rows = conn.execute(
        "SELECT link_id, link_type, to_object, tier, reason FROM links WHERE from_object = ? "
        "AND binding_kind = 'mention' AND superseded_at IS NULL ORDER BY link_id",
        (document,)).fetchall()
    return [row for row in rows if json.loads(row["reason"])[0].get("feature")
            == "document_reference"]


def _named(link: Mapping[str, Any]) -> tuple[Any, Any]:
    """(identifier kind, normalised value) of the reference a document-field link answers."""
    reason = json.loads(link["reason"])[0]
    return reason.get("identifier_kind"), reason.get("normalized")


def _entities(conn: sqlite3.Connection, identity_types: list[str], objects: Iterable[str]
              ) -> list[list[str]]:
    """The candidate records ``objects`` as distinct entities: records a current identity
    link (``identity_types``) at an ``IDENTITY_TIERS`` tier joins, directly or through other
    candidates, are one entity; any other record is its own (never joined on a lead)."""
    objects = sorted(set(objects))
    parent = {o: o for o in objects}

    def root(o: str) -> str:
        while parent[o] != o:
            o = parent[o]
        return o

    if len(objects) > 1 and identity_types:
        marks, types = ",".join("?" * len(objects)), ",".join("?" * len(identity_types))
        for a, b in conn.execute(
                f"SELECT from_object, to_object FROM links WHERE link_type IN ({types}) AND "
                f"tier IN ({','.join('?' * len(IDENTITY_TIERS))}) AND superseded_at IS NULL "
                f"AND from_object IN ({marks}) AND to_object IN ({marks})",
                [*identity_types, *IDENTITY_TIERS, *objects, *objects]):
            parent[max(root(a), root(b))] = min(root(a), root(b))
    groups: dict[str, list[str]] = {}
    for o in objects:
        groups.setdefault(root(o), []).append(o)
    return list(groups.values())


def _scopes(conn: sqlite3.Connection, entities: Iterable[list[str]]) -> set[str]:
    """The scopes the entities span: a BC record's company (its capture handle), any other
    entity itself (two entities are two scopes until a person says otherwise)."""
    from integrations.live_source_guard import parse_handle

    out: set[str] = set()
    for members in entities:
        companies = set()
        for object_id in members:
            for (handle,) in conn.execute("SELECT l1_handle FROM object_versions WHERE "
                                          "object_id = ? AND superseded_at IS NULL",
                                          (object_id,)):
                try:
                    companies.add("bc:" + parse_handle(handle, "bc", 4)[0][1].lower())
                except ValueError:
                    pass
        out.update(companies or {"record:" + members[0]})
    return out


def _served_links(served: Any, reader: Any, document: str, link_types: Iterable[str]
                  ) -> tuple[dict[str, str] | None, int]:
    """(link id -> tier of every link ``plx.around`` serves ``reader`` from ``document``
    over ``link_types``, or None when the document is not served; and the edges, objects
    and reached counts it was served)."""
    from operating.ontology_tools_traverse import TIER_ORDER

    edges: dict[str, str] = {}
    served_count = 0
    for link_type in sorted(set(link_types)):
        cursor = None
        while True:
            arguments = {"from": document, "limit": 50, "tier_floor": TIER_ORDER[-1],
                         "path": [{"link_type": link_type, "direction": "forward"}],
                         **({"cursor": cursor} if cursor else {})}
            out = served.call("plx.around", arguments, reader)
            if "error" in out:
                return None, served_count
            edges.update({edge["link_id"]: edge["tier"] for edge in out["edges"]})
            served_count += len(out["edges"]) + len(out["objects"]) + (
                (out["hops"][0]["count"] or 0) if cursor is None else 0)
            cursor = out["completeness"].get("next_cursor")
            if not cursor:
                break
    return edges, served_count


def _tally() -> dict[str, Any]:
    return {"references": 0, "linked_one": {}, **dict.fromkeys(OUTCOMES[1:], 0)}


def _count(tally: dict[str, Any], outcome: str, tier: str | None) -> None:
    tally["references"] += 1
    if outcome == "linked_one":
        tally["linked_one"][tier] = tally["linked_one"].get(tier, 0) + 1
    else:
        tally[outcome] += 1


def link_outcomes(store: Path, host: Any, principal: str, withheld: str,
                  captured: set[tuple[str, str]], stop: Callable[[], bool] | None = None
                  ) -> dict[str, Any]:
    """The reference outcomes, agreement, cross-scope and leak counts of every projected
    document-field object (module docstring) as the served ``host`` reads them for
    ``principal`` and for ``withheld``; ``captured``: the (source, relation) pairs the run
    captured whole. Once ``stop()`` is true no further document is read: the rest are
    ``documents_unmeasured``; the first document is always read (``stop`` is still
    asked before it, so a caller can time it), so a run whose projection ran past its
    budget still measures a link."""
    from ontology import mentions
    from ontology.schema import load_schema
    from ontology.types import Actor
    from operating import attested_conflicts
    from operating.attested_conflicts import AGREEMENTS
    from operating.ontology_tools import identity_link_type

    schema = load_schema()
    identity_types = sorted(lid for lid, link in schema.link_types.items()
                            if identity_link_type(link))
    profiles = {p["object_type"]: p for p in document_fields.profiles()["profiles"]}
    declared = mentions.mention_link_types(schema.link_types)
    reader = Actor(host.config.tenant, frozenset({principal}), "restricted")
    other = Actor(host.config.tenant, frozenset({withheld}), "restricted")
    total = _tally()
    out: dict[str, Any] = {"documents": 0, "documents_withheld": 0,
                           "documents_unmeasured": 0, "not_linkable": 0, "by_field": {},
                           "agreement": dict.fromkeys(AGREEMENTS, 0), "agreement_by_field": {},
                           "agreement_not_comparable": {}, "not_comparable_by_field": {},
                           "cross_scope_references": 0, "cross_scope_corroborated": 0,
                           "ambiguous_candidates": {}, "identity_joined": 0}
    hidden = {"documents": 0, **_tally(), "served": 0}
    conn = sqlite3.connect(f"file:{store}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" * len(profiles))
    try:
        documents = conn.execute(
            "SELECT o.object_id, o.object_type, v.value FROM objects o JOIN object_values v "
            "ON v.object_id = o.object_id AND v.property = 'evidence' AND v.superseded_at IS "
            f"NULL WHERE o.object_type IN ({marks}) ORDER BY o.object_id",
            sorted(profiles)).fetchall()
        with host.served_tools(reader) as served:
            for n, document in enumerate(documents):
                if stop is not None and stop() and n:
                    out["documents_unmeasured"] = len(documents) - n
                    break
                doc_type = document["object_type"]
                profile = profiles[doc_type]
                link_types = [lt for (source, _), lt in declared.items() if source == doc_type]
                visible, _ = _served_links(served, reader, document["object_id"], link_types)
                shown, count = _served_links(served, other, document["object_id"], link_types)
                hidden["served"] += count
                if visible is None:
                    out["documents_withheld"] += 1
                    continue
                out["documents"] += 1
                hidden["documents"] += shown is not None
                links = _candidates(conn, document["object_id"])
                references: dict[tuple[str, str], str] = {}
                for ref in json.loads(document["value"]).get("references") or ():
                    key = (ref["identifier_kind"], ref["normalized"])
                    if ref.get("field") or key not in references:
                        references[key] = ref.get("field") or ref["identifier_kind"]
                fields = out["by_field"].setdefault(profile["id"], {})
                attests = (profile.get("attests") or {}).get("link_type")
                for (kind, normalized), name in sorted(references.items()):
                    if not _linkable(schema, doc_type, kind):
                        out["not_linkable"] += 1
                        continue
                    mine = [row for row in links if _named(row) == (kind, normalized)]
                    records = {row["to_object"] for row in mine}
                    entities = _entities(conn, identity_types, records)
                    out["identity_joined"] += len(entities) < len(records)
                    cross = len(_scopes(conn, entities)) > 1
                    out["cross_scope_references"] += cross
                    outcome, tier = _outcome(schema, doc_type, kind, mine, len(entities),
                                             visible, captured)
                    for tally in (total, fields.setdefault(name, _tally())):
                        _count(tally, outcome, tier)
                    if outcome == "ambiguous":
                        many = out["ambiguous_candidates"]
                        many[str(len(entities))] = many.get(str(len(entities)), 0) + 1
                    _count(hidden, *_outcome(schema, doc_type, kind, mine, len(entities),
                                             shown or {}, captured))
                    if cross and (outcome == "linked_one" or any(
                            row["tier"] in INFERRED_ABOVE_LEAD for row in mine)):
                        out["cross_scope_corroborated"] += 1
                    if outcome != "linked_one" or not any(
                            row["link_type"] == attests for row in mine):
                        continue
                    tools = served.tools   # the reader's policy now, as the host's trim
                    tools.policy = served.policy_now(reader)
                    scope = tools._scope(reader, tools._as_known_at({}))
                    by_field = out["agreement_by_field"].setdefault(profile["id"], {})
                    # One decision: the outcome and the cause the product's comparison
                    # gave (read at call time, never re-derived here).
                    decided = attested_conflicts.agreement_with_causes(
                        tools, scope, document["object_id"], mine[0]["to_object"])
                    causes = out["not_comparable_by_field"].setdefault(profile["id"], {})
                    for field, (agreed, cause) in decided.items():
                        out["agreement"][agreed] += 1
                        row = by_field.setdefault(field, dict.fromkeys(AGREEMENTS, 0))
                        row[agreed] += 1
                        if agreed != "not_comparable":
                            continue
                        cause = cause if cause in NOT_COMPARABLE_CAUSES else "unclassified"
                        for tally in (out["agreement_not_comparable"],
                                      causes.setdefault(field, {})):
                            tally[cause] = tally.get(cause, 0) + 1
    finally:
        conn.close()
    revealed = sum(hidden["linked_one"].values()) + hidden["ambiguous"]
    out["references"] = total.pop("references")
    out["outcomes"] = total
    out["links_measured"] = sum(total["linked_one"].values()) + total["ambiguous"] + \
        total["no_record"]
    out["withheld_reader"] = {"documents": hidden["documents"],
                              "references": hidden["references"],
                              "withheld": hidden["withheld"], "revealed": revealed}
    out["leaks"] = hidden["served"] + revealed
    return out


NOT_COMPARABLE_CAUSES = (
    # why a field ``agreement`` called not_comparable was not compared, as the product's
    # comparison decided it (``attested_conflicts.agreement_with_causes``): the reason of
    # a gap ``_compare`` reported (a reason not listed here is unclassified), a
    # missing_in_record lead, or one of ``attested_conflicts.NOT_COMPARED``:
    "property_not_declared", "currency_unknown", "currency_differs", "tax_basis_unknown",
    "tax_basis_differs", "unreadable", "several_values", "missing_in_record",
    "document_not_stated",           # the reader may read no value of the document field
    "record_event_absent",           # the record holds no readable event of the rule's type
    "no_record_reached",             # the rule's path reaches no record declaring it
    "record_property_not_served",    # every record reached declares it not served
    "record_source_lacks_property",  # no record reached is from a source that holds it
    "document_blank",                # a blank page field facing no record value
    "record_value_absent",           # no readable record value (empty, withheld)
    "no_entry_compared",             # keyed: no document entry met a record entry's key
    "document_value_unread",         # the document field holds no value it was read as
    "different_measure",             # values met, none of one measure (unit, a code word)
    "unclassified")                  # none of these: reported, never dropped


def _outcome(schema: Any, document_type: str, kind: str, links: list[sqlite3.Row],
             entities: int, visible: Mapping[str, str], captured: set[tuple[str, str]]
             ) -> tuple[str, str | None]:
    """One reference's outcome for a reader served ``visible`` (module docstring);
    ``entities``: the distinct entities its candidate records are (``_entities``)."""
    whole = all(pair in captured for pair in _linkable(schema, document_type, kind))
    if not links:
        return ("no_record" if whole else "source_not_captured"), None
    seen = [row for row in links if row["link_id"] in visible]
    if not seen:
        return "withheld", None
    if entities > 1:
        return "ambiguous", None
    if not whole:   # the one candidate captured may not be the only record: never linked
        return "source_not_captured", None
    return "linked_one", visible[seen[0]["link_id"]]


def _portal_mirror(client: Any, state: Path, audience: Path, relations: list[str],
                   actor: Any, budget_seconds: float | None, order: list[str] | None = None,
                   workers: int = 1) -> tuple[set[str], dict[str, Any] | None]:
    """Mirror ``relations`` (GET only) into the host's mirror file, ``order`` first,
    ``workers`` at a time: (the relations captured whole, a blocked part or None)."""
    from integrations.source_mirror import MirrorError, SourceMirror
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    from scripts.ontology_projection_probe import PORTAL, _part

    (state / CASE_RUNTIME_DIR).mkdir(parents=True, exist_ok=True)
    with SourceMirror(state / CASE_RUNTIME_DIR / MIRROR_FILE, client, audience,
                      tenant=actor.tenant) as mirror:
        try:
            synced = mirror.sync(actor, relations=relations, budget_seconds=budget_seconds,
                                 order=order, workers=workers)
        except MirrorError as exc:
            if exc.code != "source_unavailable":
                raise
            return set(), _part("portal_mirror", PORTAL, exc.status,
                                exc.upstream_code or exc.code)
    complete = {row["relation"] for row in synced["coverage"].get("relations", ())
                if row.get("complete") and row["relation"] in relations}
    return complete, (None if complete else _part("portal_mirror", PORTAL,
                                                  error_code="no_relation_complete"))


def _allowance(own: float | None, deadline: float | None, monotonic: Callable[[], float],
               reserve: Mapping[str, Any]) -> float | None:
    """A read phase's budget: its own, cut to what ``deadline`` leaves after the estimate
    that follows it (the last one's seconds), the projection and measurement estimates,
    all with ``RESERVE_MARGIN`` (never below 0; none once the sample failed); its own alone
    without a total budget."""
    if deadline is None:
        return own
    if reserve.get("failed"):
        return 0.0
    left = max(0.0, deadline - monotonic() -
               (1 + RESERVE_MARGIN) * (reserve["estimate_seconds"] +
                                       reserve["projection_estimate_seconds"] +
                                       reserve["measure_estimate_seconds"]))
    return left if own is None else min(own, left)


class _Kept:
    """One relation's records, re-iterable, as a ``RowSample`` keeps them."""

    def __init__(self, records: Iterable[Any], sample: Any) -> None:
        self._records, self._sample = records, sample

    def __iter__(self) -> Any:
        return self._sample(self._records)


class _Sample:
    """A capture source without a ``sample`` of its own: every record read, the
    ``RowSample`` keeping 1 in ``SAMPLE_STRIDE`` (its reads scaled too: over, never
    under)."""

    def __init__(self, inner: Any, sample: Any) -> None:
        self._inner, self._sample, self.source = inner, sample, inner.source

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def records(self, relations: Any) -> dict[str, _Kept]:
        held = self._inner.records(relations)
        return {relation: _Kept(held[relation], self._sample) for relation in held}


def _captures(state: Path, stack: Any, portal: set[str]) -> dict[str, Any]:
    """The capture sources the run projects: the state's, and the Portal mirror when a
    relation of it was captured whole."""
    from ontology.project import MirrorCaptures
    from operating.case_runtime import CASE_RUNTIME_DIR, MIRROR_FILE
    from operating.host import state_captures

    sources = state_captures(state, stack)
    if portal:
        sources["portal_api"] = stack.enter_context(
            MirrorCaptures(state / CASE_RUNTIME_DIR / MIRROR_FILE))
    return sources


class _Referenced:
    """A ``RowSample`` (its ``rows`` and ``kept``) that keeps 1 row in ``stride`` and every
    row naming one of ``keys`` (identifiers' letters and digits, upper case), so the
    records a document links are projected with it: a word of the row's text, up to three
    adjacent ones joined (``AO-26005B``, ``fm-cust-12``), or a word's digits after its
    letters (``PO12172025`` names ``12172025``) equal to a key; a row naming one by chance
    is kept too (over, never under). Set intersections over the row's words, joined ones
    only when a word starts a key: the cost does not grow with the keys. ``seconds``: its own time on ``clock`` (reading every row, kept or not, and
    the filter), which the projection's other work does not scale with: the estimate
    counts reading once, as the projection reads every row too, and the filter not at all
    (``filter_seconds``; the 2026-10-15 daily scaled both by the stride: 1210 s estimated
    for a 460 s projection)."""

    def __init__(self, stride: int, keys: Iterable[str],
                 clock: Callable[[], float] | None = None) -> None:
        import time

        self.stride, self.rows, self.kept, self._keys = stride, 0, 0, frozenset(keys)
        self._heads = frozenset(key[:n] for key in self._keys for n in range(1, len(key)))
        self._clock = clock or time.monotonic
        self.seconds = self.filter_seconds = 0.0

    def __call__(self, rows: Iterable[Any]) -> Any:
        clock = self._clock
        began = clock()
        for row in rows:
            self.rows += 1
            keep = (self.rows - 1) % self.stride == 0
            if not keep and self._keys:
                before = clock()
                keep = self._named(row)
                self.filter_seconds += clock() - before
            if keep:
                self.kept += 1
                self.seconds += clock() - began
                yield row
                began = clock()
        self.seconds += clock() - began

    def _named(self, row: Any) -> bool:
        text, keys = _row_text(row).upper(), self._keys
        words = _WORD.findall(text)
        if not keys.isdisjoint(words) or not keys.isdisjoint(_DIGITS_AFTER.findall(text)):
            return True
        if self._heads.isdisjoint(words):   # no word starts a key: no joined one is one
            return False
        twos = [a + b for a, b in zip(words, words[1:])]
        return not (keys.isdisjoint(twos) and
                    keys.isdisjoint([a + b for a, b in zip(twos, words[2:])]))


_WORD = re.compile(r"[0-9A-Z]+")
_DIGITS_AFTER = re.compile(r"(?<![0-9A-Z])[A-Z]+([0-9][0-9A-Z]*)")   # a word's, after its letters


def _row_text(row: Any) -> str:
    """The text of a raw capture row: a database row's values, else a record's fields."""
    if isinstance(row, (sqlite3.Row, tuple)):
        return "\n".join(str(v) for v in row if isinstance(v, (str, int, float)))
    return json.dumps(getattr(row, "row", None), default=str)


def _documents(store: Path) -> tuple[int, set[str], dict[str, list[str]]]:
    """(the document-field objects ``store`` holds, as ``link_outcomes`` reads them; the
    letters and digits, upper case, of every identifier they reference; per object type
    held, the identifier kinds its documents reference: ``link_relations``'s binding)."""
    import contextlib

    profiles = sorted({p["object_type"] for p in document_fields.profiles()["profiles"]})
    with contextlib.closing(sqlite3.connect(f"file:{store}?mode=ro", uri=True)) as conn:
        rows = conn.execute(
            "SELECT o.object_type, v.value FROM objects o JOIN object_values v ON "
            "v.object_id = o.object_id AND v.property = 'evidence' AND v.superseded_at IS "
            f"NULL WHERE o.object_type IN ({','.join('?' * len(profiles))})",
            profiles).fetchall()
    keys: set[str] = set()
    kinds: dict[str, set[str]] = {}
    for object_type, value in rows:
        held = kinds.setdefault(object_type, set())
        for ref in json.loads(value).get("references") or ():
            keys.add("".join(c for c in str(ref.get("normalized") or "").upper()
                             if "A" <= c <= "Z" or "0" <= c <= "9"))
            held.add(str(ref.get("identifier_kind")))
    return len(rows), keys - {""}, {t: sorted(k) for t, k in sorted(kinds.items())}


def _restore(state: Path) -> None:
    """Put back the projection an estimate set aside (``_estimate``), also the one a run
    killed mid-estimate left: the sample the projection path may hold goes first, then
    its mark, then the state's own projection returns and the scratch goes."""
    import shutil

    from operating.host import projection_path

    scratch, store = state / RESERVE_DIR, projection_path(state)
    if (scratch / RESERVE_SERVED).exists():
        store.unlink(missing_ok=True)
        (scratch / RESERVE_SERVED).unlink()
    if (scratch / RESERVE_KEPT).exists():
        os.replace(scratch / RESERVE_KEPT, store)
    shutil.rmtree(scratch, ignore_errors=True)


def _estimate(state: Path, schema: Any, portal: set[str], tenant: str, principal: str,
              withheld: str, monotonic: Callable[[], float], alone: dict[str, Any],
              whole: bool = True) -> tuple[float, float, int]:
    """(seconds the projection of what ``state`` holds now takes, the mean seconds of one
    document's measurement over it, the documents the run measures), measured in two
    projections into a scratch store, each served at the state's projection path (the
    state's own set aside and put back, ``_restore``) with up to ``MEASURE_SAMPLE`` of its
    documents read by ``link_outcomes`` as the run reads every one: one of the document
    sources alone, whole (once, kept in ``alone``: a record read adds no document; it
    gives the documents and the identifiers they reference), and one with them whole and
    the rows of every other relation a ``_Referenced`` sample keeps (only those decoded),
    whose seconds over the first (what the records add, less the sample's own: reading
    every row is counted once, its filter never) are scaled by the rows stepped over to
    those kept; its documents' mean is measured over the records they link, never scaled.
    Not ``whole``: the documents alone only (``alone`` filled, nothing else projected)."""
    from contextlib import ExitStack

    from ontology import project
    from operating import build_steps
    from operating.host import Host, HostConfig, projection_path

    carriers = {b["source"] for p in document_fields.profiles()["profiles"]
                for b in schema.object_types[p["object_type"]].get("bindings", ())}
    scratch, store = state / RESERVE_DIR, projection_path(state)

    def timed(sample: _Referenced | None) -> tuple[float, float | None, Any] | None:
        """(the projection's seconds; its documents' mean measurement seconds, None when
        it projected none; the documents' ``_documents`` with no ``sample``); None when it
        would project nothing, or no record source with them."""
        started = monotonic()
        with ExitStack() as stack:
            sources = _captures(state, stack, portal)
            if sample is None:
                sources = {name: s for name, s in sources.items() if name in carriers}
            if not sources or sample is not None and set(sources) <= carriers:
                return None
            for name, source in sources.items():
                if name in carriers:
                    continue
                if hasattr(type(source), "sample"):   # samples its own queries
                    source.sample = sample
                else:
                    sources[name] = _Sample(source, sample)
            project.build_store(scratch / "store.sqlite", schema, sources,
                                post_project=build_steps.post_project(state, tenant))
        built = monotonic() - started
        documents = _documents(scratch / "store.sqlite") if sample is None else None
        os.replace(scratch / "store.sqlite", store)
        asked: list[float] = []
        link_outcomes(store, Host(state, HostConfig.from_file(state / HOST_CONFIG),
                                  environ={}),
                      principal, withheld, set(),   # the first MEASURE_SAMPLE only
                      stop=lambda: asked.append(monotonic()) is None and
                      len(asked) > MEASURE_SAMPLE)
        ended = asked[MEASURE_SAMPLE] if len(asked) > MEASURE_SAMPLE else monotonic()
        return built, (ended - asked[0]) / min(len(asked), MEASURE_SAMPLE) if asked \
            else None, documents

    _restore(state)   # a killed estimate's, before this one sets the projection aside
    scratch.mkdir(parents=True)
    store.parent.mkdir(parents=True, exist_ok=True)
    if store.is_file():
        os.replace(store, scratch / RESERVE_KEPT)
    (scratch / RESERVE_SERVED).touch()   # from here the projection path may hold a sample
    try:
        if not alone:
            built, measured, documents = timed(None) or (0.0, None, (0, set(), {}))
            alone.update(built=built, measured=measured, documents=documents[0],
                         keys=documents[1], kinds=documents[2])
        sample = _Referenced(SAMPLE_STRIDE, alone["keys"], monotonic)
        both = timed(sample) if whole else None
    finally:
        _restore(state)
    if both is None:
        return alone["built"], alone["measured"] or 0.0, alone["documents"]
    # What the kept rows added, scaled to every row; reading the rows once (the sample
    # read each one), the filter never.
    read = max(0.0, sample.seconds - sample.filter_seconds)
    projection = alone["built"] + read + \
        max(0.0, both[0] - alone["built"] - sample.seconds) * sample.rows / max(1, sample.kept)
    measured = both[1] if both[1] is not None else alone["measured"]
    return projection, measured or 0.0, alone["documents"]


def _fresh_since(path: Path, resumed: bool) -> str:
    """The BC session start of this run (``FRESH_SINCE_FILE``): the one ``path`` kept when
    BC was left unfinished on this state (``resumed``) under ``FRESH_SINCE_HOURS`` ago,
    else now, kept there."""
    now = datetime.now(timezone.utc)
    try:
        kept = datetime.strptime(path.read_text("utf-8").strip(),
                                 "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        kept = None
    if resumed and kept is not None and \
            timedelta(0) <= now - kept < timedelta(hours=FRESH_SINCE_HOURS):
        return kept.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(now.strftime("%Y-%m-%dT%H:%M:%SZ"), "utf-8")
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def links(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
          sender: Any = None, bc_sender: Any = None, bc_books: Any = None,
          portal_client: Any = None, sleep: Callable[[float], None] | None = None,
          monotonic: Callable[[], float] | None = None) -> dict[str, Any]:
    """The ``--links`` run (module docstring): capture, project, measure, within
    ``args.budget_seconds`` when set (``incomplete``: what that budget cut)."""
    import time
    from contextlib import ExitStack

    from integrations import capture_sharepoint as cs
    from integrations.bc_read import FRESH_SINCE_ENV
    from integrations.live_source_guard import LiveSourceAudience
    from integrations.source_mirror import AUDIENCE_SCHEMA
    from ontology.project import ProjectionError, build_store
    from ontology.project_bc import BcCaptures
    from ontology.schema import load_schema
    from ontology.types import Actor
    from operating import build_steps
    from operating.host import (BC_DIR, BC_FILE, CONFIG_SCHEMA, Host, HostConfig,
                                projection_path)
    from scripts.ontology_projection_probe import BC, PORTAL, _failure_part, _part, capture_bc

    clock = monotonic or time.monotonic
    started = clock()
    deadline = None if args.budget_seconds is None else started + args.budget_seconds
    phases: dict[str, float] = {}
    skipped: list[str] = []
    mark = started
    reserve: dict[str, Any] = {"projection_estimate_seconds": 0.0,
                               "measure_estimate_seconds": 0.0,
                               "measure_seconds_per_document": 0.0, "documents": 0,
                               "estimates": 0,
                               "estimate_seconds": 0.0,
                               "bc_slices": 0, "failed": None}
    alone: dict[str, Any] = {}   # ``_estimate``'s documents alone

    def phase(name: str) -> None:
        nonlocal mark
        now = clock()
        phases[name] = round(phases.get(name, 0.0) + now - mark, 1)
        mark = now

    def measure_reserve(portal: set[str]) -> None:
        """The reserve after a read (module docstring), with a total budget."""
        if deadline is None or reserve["failed"]:
            return
        began = clock()
        try:
            estimate, per_document, documents = _estimate(
                state, schema, portal, audience.tenant, args.principal,
                args.withheld_principal, clock, alone)
        except Exception as exc:  # noqa: BLE001 - a typed incomplete, never a crash
            reserve["failed"] = exc.code if isinstance(exc, ProjectionError) else \
                type(exc).__name__
            phase("reserve")
            return
        reserve["estimate_seconds"] = round(clock() - began, 1)
        reserve["projection_estimate_seconds"] = round(estimate, 1)
        reserve["measure_seconds_per_document"] = round(per_document, 1)
        reserve["documents"] = documents
        reserve["measure_estimate_seconds"] = round(documents * per_document, 1)
        reserve["estimates"] += 1
        phase("reserve")

    state = Path(args.state_dir).expanduser().resolve()
    _restore(state)   # a run killed mid-estimate left the state's projection set aside
    capture = cs.run_cli(argparse.Namespace(
        state_dir=str(state), scope=args.scope, live_audience=args.live_audience,
        principal=args.principal, withheld_principal="staff:document-links-capture-withheld",
        folder_only_principal="staff:document-links-folder-only", runs=1,
        max_store_bytes=None,
        budget_seconds=_allowance(args.capture_budget_seconds, deadline, clock, reserve)),
        environ=environ, sender=sender, sleep=sleep, monotonic=clock)
    phase("capture")
    audience = LiveSourceAudience.from_file(args.live_audience)
    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
    blocked: list[dict[str, Any]] = []
    schema = load_schema()
    declared = link_relations(schema)
    mirror_audience = state / "links-mirror-audience.json"
    mirror_audience.write_text(json.dumps({
        "schema": AUDIENCE_SCHEMA, "tenant": audience.tenant, "audiences": {
            f"{family}.*": [args.principal]
            for family in sorted({r.split(".", 1)[0] for r in declared})}}), "utf-8")
    (state / HOST_CONFIG).write_text(json.dumps({
        "schema": CONFIG_SCHEMA, "tenant": audience.tenant,
        "capture_principal": args.principal, "mirror_audience": str(mirror_audience),
        "live_audience": str(Path(args.live_audience).resolve())}), "utf-8")
    measure_reserve(set())
    if portal_client is not None and deadline is None:
        try:   # the documents alone, which the binding reads (no reserve without a budget)
            _estimate(state, schema, set(), audience.tenant, args.principal,
                      args.withheld_principal, clock, alone, whole=False)
        except Exception:  # noqa: BLE001 - unbound: every declared relation, never fewer
            alone.clear()
        phase("reserve")
    # Only the relations the captured documents' kinds are looked up in or compare with
    # (2026-10-15: all 25 declared took 486 s and left BC none); unbound when the
    # documents alone were not projected.
    relations = declared if "kinds" not in alone else link_relations(schema, alone["kinds"])
    # The relations a reference is looked up in first: a budget cut leaves comparisons out
    # before links.
    first = None if "kinds" not in alone else \
        link_relations(schema, alone["kinds"], compared=False)
    # The Portal before BC: its relations are read whole or not at all and gate every
    # Portal-carried kind, while a BC capture the budget cuts keeps its finished units.
    portal: set[str] = set()
    portal_sent = 0
    portal_skipped = None   # why a configured Portal mirror read nothing
    portal_budget = _allowance(args.portal_budget_seconds, deadline, clock, reserve)
    if portal_client is None:
        blocked.append(_part("portal_mirror", PORTAL, error_code="not_configured"))
    elif not relations:
        # no captured document references a kind a Portal relation answers
        portal_skipped = "no_relation_needed"
    elif portal_budget == 0:
        skipped.append("portal_mirror")
        portal_skipped = "budget"
    else:
        before = portal_client.requests_sent
        try:
            portal, down = _portal_mirror(portal_client, state, mirror_audience, relations,
                                          Actor(audience.tenant, frozenset({args.principal})),
                                          portal_budget, first,
                                          getattr(args, "portal_workers", 1))
        finally:
            portal_sent = portal_client.requests_sent - before
        if down is not None:
            blocked.append(down)
    phase("portal_mirror")
    if portal:
        measure_reserve(portal)
    bc_db = state / BC_DIR / BC_FILE
    bc_result = None
    bc_env = dict(os.environ if environ is None else environ)
    # A slice reuses the units an earlier slice of this run (or of its verifier session)
    # completed, and resumes the ones it paused (``bc_read`` ``fresh_since``): the
    # session's start, else the one a run that left BC unfinished on this state kept.
    fresh_file = state / FRESH_SINCE_FILE
    if not bc_env.get(FRESH_SINCE_ENV):
        bc_env[FRESH_SINCE_ENV] = _fresh_since(fresh_file, bc_db.is_file())
    bc_sent, before_bc, bc_estimated = 0, reserve["projection_estimate_seconds"], True
    while True:
        bc_read = phases.get("bc_capture", 0.0)
        bc_own = None if args.bc_budget_seconds is None else \
            max(0.0, args.bc_budget_seconds - bc_read)
        bc_budget = _allowance(bc_own, deadline, clock, reserve)
        if deadline is not None and bc_budget:
            # The projection seconds a read second adds: BC's own once a slice measured
            # it, else this run's reads so far; at least 1, as BC's sets differ in density
            # (its first slice is unmeasured, a later set may be denser than the ones read).
            read = bc_read or sum(phases.get(name, 0.0) for name in ("capture",
                                                                     "portal_mirror"))
            grown = reserve["projection_estimate_seconds"] - (before_bc if bc_read else 0.0)
            per_read = max(1.0, grown / read if read else 0.0)
            bc_budget = max(0.0, bc_budget / (1 + (1 + RESERVE_MARGIN) * per_read))
            if bc_result is not None and bc_budget <= reserve["estimate_seconds"]:
                break   # a slice shorter than the estimate after it buys nothing
        if bc_budget == 0:
            if bc_result is None:
                skipped.append("bc_capture")
            break
        try:
            sliced = capture_bc({"environ": bc_env, "sender": bc_sender, "books": bc_books},
                                str(bc_db), bc_budget, audience=audience, actor=actor,
                                monotonic=clock)
        except Exception as exc:  # noqa: BLE001 - a source read: a part, never a message
            blocked.append(_failure_part("bc_capture", BC, exc))
            break
        finally:
            phase("bc_capture")
        bc_sent += sliced["requests_sent"] or 0
        bc_result = {**sliced, "requests_sent": bc_sent}
        reserve["bc_slices"] += 1
        bc_estimated = False
        if deadline is None or sliced["stopped_reason"] != "budget":
            break
        measure_reserve(portal)
        bc_estimated = True
    phase("bc_capture")
    if not bc_estimated:   # the recorded reserve holds what the last slice read
        measure_reserve(portal)
    if bc_result is not None and bc_result["all_complete"] and fresh_file.is_file():
        fresh_file.unlink()
    if bc_result is not None and not bc_result["records_current"]:
        blocked.append(_part("bc_capture", BC,
                             error_code=bc_result["reason"] or "no_records_captured"))
    captured = {("portal_api", relation) for relation in portal}
    if bc_db.is_file():
        with BcCaptures(bc_db) as bc:
            captured |= {("bc", b["relation"]) for spec in schema.object_types.values()
                         for b in spec.get("bindings", ()) if b["source"] == "bc"
                         and bc.capture_gap(b["relation"]) is None}
    store = projection_path(state)
    store.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        sources = _captures(state, stack, portal)
        built = build_store(store, schema, sources,
                            post_project=build_steps.post_project(state, audience.tenant))
    phase("project")
    host = Host(state, HostConfig.from_file(state / HOST_CONFIG), environ={})
    measured = link_outcomes(store, host, args.principal, args.withheld_principal, captured,
                             stop=None if deadline is None else lambda: clock() >= deadline)
    phase("measure")
    labels = {capture["evidence_label"], *([bc_result["evidence_label"]] if bc_result else [])}
    roots = [r for run in capture["runs"] for t in run["targets"] for r in t["roots"]]
    roots_cut = sum(cs.TIME_BUDGET in (r["reason"] or "").split(",") for r in roots)
    cut = (["reserve"] if reserve["failed"] else []) + \
        (["capture"] if roots_cut else []) + skipped + \
        (["measure"] if measured["documents_unmeasured"] else [])
    report = {"check": LINKS_CHECK,
              "evidence_label": labels.pop() if len(labels) == 1 else "mixed",
              "read_only_guard": capture["read_only_guard"],
              "sources": sorted(sources), "blocked_upstream": blocked,
              "bc_capture": bc_result,
              "portal_mirror": None if portal_client is None else {
                  "relations": len(relations), "relations_declared": len(declared),
                  "workers": getattr(args, "portal_workers", 1),
                  "relations_complete": len(portal),
                  "requests_sent": portal_sent, "skipped": portal_skipped},
              "capture": {key: capture["summary"][key] for key in (
                  "files_current", "contents_captured", "texts_extracted", "texts_ocr",
                  "texts_ocr_unavailable")},
              "capture_complete": capture["summary"]["roots"] > 0 and
              capture["summary"]["roots_complete"] == capture["summary"]["roots"],
              "requests_sent": capture["requests_sent"] + ((bc_result or {}).get(
                  "requests_sent") or 0) + portal_sent,
              "budget_seconds": args.budget_seconds, "phase_seconds": phases,
              "reserve": None if deadline is None else reserve,
              # A read or measurement a budget cut: documents left unread or unmeasured,
              # never a pass (and never ``blocked``: the budget is this run's, not a source's).
              "incomplete": {"reason": RESERVE_FAILED if reserve["failed"] else BUDGET,
                             "cut": cut, "roots": len(roots),
                             "roots_cut": roots_cut,
                             "documents_measured": measured["documents"] +
                             measured["documents_withheld"],
                             "documents_unmeasured": measured["documents_unmeasured"]}
              if cut else None,
              "link_steps": (built.get("post_project") or {}).get("document_fields", {}).get(
                  "counts"), **measured}
    if not cut and not (bc_result or {}).get("records_current") and not portal:
        report["blocked"] = NO_RECORD_SOURCE
    elif not cut and report["capture"]["texts_ocr_unavailable"] and not measured["documents"]:
        report["blocked"] = OCR_UNAVAILABLE
    report["ok"] = not cut and not report.get("blocked") and \
        measured["documents"] >= args.min_documents and not measured["leaks"] and \
        not measured["cross_scope_corroborated"] and measured["links_measured"] >= 1
    return report


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         sender: Any = None, bc_sender: Any = None, bc_books: Any = None,
         portal_client: Any = None, monotonic: Callable[[], float] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--lot-trace" in argv:
        return lot_trace_main(argv, environ=environ, sender=sender,
                              portal_client=portal_client, monotonic=monotonic)
    if "--links" not in argv:
        argv, floors = _floors(argv)
        return check.main(argv, environ=environ, evaluate=lambda store, report: evaluate(
            store, report, floors))
    from integrations import capture_sharepoint as cs

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_document_fields_live",
                                     description="document-to-record links (--links)")
    parser.add_argument("--links", action="store_true", required=True)
    parser.add_argument("--state-dir", required=True,
                        help="throwaway Workbench state directory (outside every work tree)")
    parser.add_argument("--scope", required=True, help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting --principal the "
                             "SharePoint scope and BC, --withheld-principal the files only")
    parser.add_argument("--principal", default=cs.CAPTURE_PRINCIPAL)
    parser.add_argument("--withheld-principal", default=WITHHELD_PRINCIPAL)
    parser.add_argument("--min-documents", type=int, default=1)
    parser.add_argument("--capture-budget-seconds", type=float, default=None)
    parser.add_argument("--bc-budget-seconds", type=float, default=None)
    parser.add_argument("--portal-budget-seconds", type=float, default=None)
    parser.add_argument("--portal-workers", type=int, default=1,
                        help="Portal relations the mirror reads at the same time (1..4)")
    parser.add_argument("--budget-seconds", type=float, default=None,
                        help="total seconds for the run: each read phase gets at most what "
                             "is left after a reserve for the projection measured in this "
                             "run, and measurement stops reading documents at it after the "
                             "first (incomplete budget, never ok)")
    args = parser.parse_args(argv)
    from integrations.source_mirror import MAX_SYNC_WORKERS
    if not 1 <= args.portal_workers <= MAX_SYNC_WORKERS:
        parser.error(f"--portal-workers must be 1..{MAX_SYNC_WORKERS}")
    for name in ("budget_seconds", "capture_budget_seconds", "bc_budget_seconds",
                 "portal_budget_seconds"):
        value = getattr(args, name)
        if value is not None and not 0 < value < float("inf"):   # NaN fails too
            parser.error(f"--{name.replace('_', '-')} must be a positive number of seconds")
    env = os.environ if environ is None else environ
    if portal_client is None and env.get("PLX_WORKBENCH_API_URL") and \
            env.get("PLX_WORKBENCH_API_KEY"):
        from integrations.workbench_read_api import WorkbenchReadApiClient
        portal_client = WorkbenchReadApiClient(env["PLX_WORKBENCH_API_URL"],
                                               env["PLX_WORKBENCH_API_KEY"])
    report = links(args, environ=environ, sender=sender, bc_sender=bc_sender,
                   bc_books=bc_books, portal_client=portal_client, monotonic=monotonic)
    print(json.dumps(report, indent=1, sort_keys=True))
    return check.exit_code(report)


def lot_trace_main(argv: list[str], *, environ: Mapping[str, str] | None = None,
                   sender: Any = None, portal_client: Any = None,
                   monotonic: Callable[[], float] | None = None) -> int:
    """``--lot-trace``: the supplier-lot trace (``scripts/eval_lot_trace.py``), run from this
    module so its live check names a module the live verifier already allows."""
    from scripts import eval_lot_trace

    parser = argparse.ArgumentParser(prog="python3 -m scripts.eval_document_fields_live",
                                     description="supplier-lot trace (--lot-trace)")
    eval_lot_trace.add_arguments(parser)
    args = parser.parse_args(argv)
    for name in ("capture_budget_seconds", "portal_budget_seconds"):
        value = getattr(args, name)
        if value is not None and not 0 < value < float("inf"):   # NaN fails too
            parser.error(f"--{name.replace('_', '-')} must be a positive number of seconds")
    env = os.environ if environ is None else environ
    if portal_client is None and env.get("PLX_WORKBENCH_API_URL") and \
            env.get("PLX_WORKBENCH_API_KEY"):
        from integrations.workbench_read_api import WorkbenchReadApiClient
        portal_client = WorkbenchReadApiClient(env["PLX_WORKBENCH_API_URL"],
                                               env["PLX_WORKBENCH_API_KEY"])
    report = eval_lot_trace.run(args, environ=environ, sender=sender,
                                portal_client=portal_client, monotonic=monotonic)
    print(json.dumps(report, indent=1, sort_keys=True))
    return check.exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
