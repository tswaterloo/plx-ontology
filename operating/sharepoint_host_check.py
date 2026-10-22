"""Live check: the case host answers a SharePoint document question from the capture.

``verify/live-checks/case.sharepoint_documents.json``. One GET-only capture run of the
configured scope (``integrations.capture_sharepoint``, OCR of scanned PDFs on this
host), a projection of the capture alone (no Portal source: the Portal-down shape),
then the real user path: ``plx.documents`` through ``CaptureCaseHostTools`` with the
host's live SharePoint readers re-reading each served file's permissions. The questions
are the identifiers the captured text itself names (parsed by the one identifier
grammar; never a configured case value), each asked as written with its label when the
value alone names no identifier (``PO: 5280``, not ``5280``, as an employee would ask it):
each must come back as a captured file with a page-cited mention labelled
``as_of_capture``. A question that does not is counted under one reason (``unanswered``):
``scan_cap`` (the answer was cut at a scan or answer bound: what it holds is not all
there is), ``unpaged_mention`` (a captured file came back, no mention of it cites a page),
``withheld`` (no file came back and the host refused a captured file in that call:
``CaptureCaseHostTools.refusals``) or ``no_document`` (nothing came back). The same
questions asked by a reader without a grant and by a folder-only reader (the folder
granted, no source identity, item permissions required) must return nothing.

``requests_sent`` counts every Graph request of the check: the capture's
(``capture_requests_sent``) and the host's (``host_requests_sent``, its OAuth token
POSTs included). ``host_gets_sent`` counts the host's Graph GETs alone (item metadata
and permissions of its live re-reads), so a positive count proves a re-read was sent.

With ``--document-class <profile>`` (``verify/live-checks/
ontology.document_fields_sharepoint.json``) the same capture and projection
measure that class of ``ontology/schema/document_fields.json`` instead of asking
questions: the objects projected, the fields stated, the gaps read, per field how many
documents state it and how many hold each gap reason (``fields``), each distinct
combination of one document's field outcomes with its number of documents
(``document_outcomes``: what a read per document is told from), per document whose
handwriting OCR could not key the positions it is cited at (``handwritten_unkeyed``:
page and span, never its text), the keyed entries and
whether every page/span citation of their evidence resolves against the captured bytes
and OCR text (``ontology.document_fields.resolve_citation``; tests:
``ontology/tests/test_quality_packets.py``). The caller may add counts read from the same
capture store and report (``evaluate``): the eval tooling's live entry scores the exposed
extraction items there and turns ``fields`` into its denominators; this product module
never reads evaluation data. ``--capture-budget-seconds`` bounds the capture walk (a root
it cuts is incomplete, ``capture_complete`` false) so the check ends with a verdict
inside its live timeout; without it, ``CAPTURE_BUDGET_SECONDS`` (the rest of the check:
projection and questions).

Prints counts, booleans and enums only: never a name, path, identifier, id or text.
Live-unverified until the verifier records a result; the tests run it over an injected
transport (``operating/tests/test_case_host_sharepoint.py``).

With ``--document-class`` the report also carries ``portal_metadata`` (QA-06): the
projected ``SharePointFile.portal_metadata`` values (the Portal-taxonomy columns the Portal
states on each captured version, ``operating.doc_metadata_taxonomy.stated_metadata``):
``documents_with_portal_metadata`` (versions stating at least one column), ``documents_read``
(versions whose columns were read), ``columns_stated`` and ``gap_reasons`` (per column name
and per typed gap), ``served_to_reader`` (of those stating a column, how many the capture's
gate serves the check's reader with the same value) and ``visible_to_withheld`` (how many it
serves a reader the audience withholds: must be 0).

With ``--document-class`` the report also carries ``stages``: where the reading of each
current captured file the reader may see stopped (``ontology.document_fields.stages``:
text status, class or the reason it has none), which the eval tooling turns into its
stage funnel; ``stages.truncated`` (more current files than the store's read-back
bound, ``capture_sharepoint.INDEX_LIMIT``) makes the counts a lower bound and the
report never ``ok``. A report the caller marks ``blocked`` (a typed cause outside the product,
e.g. ``ocr_unavailable``) is never ``ok`` but exits 0, so the verifier records the
cause (``blocked_when``) instead of a bare failure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Callable, Mapping

from integrations import capture_sharepoint as cs
from integrations.live_source_guard import LiveSourceAudience
from integrations.source_mirror import AUDIENCE_SCHEMA
from ontology import document_fields, mentions
from ontology.types import Actor

MAX_QUESTIONS = 5
CAPTURE_BUDGET_SECONDS = 1500   # the CLI's default capture bound (module docstring)
DOCUMENT_CHECK = "ontology.document_fields_sharepoint"
UNANSWERED = ("no_document", "scan_cap", "unpaged_mention", "withheld")


def _asked(text: str, found: Mapping[str, Any]) -> str:
    """The question for one mention: its value as written when that alone reads as the
    same identifier, else the label and value as written."""
    key = (found["identifier_kind"], found["normalized"])
    if any((m["identifier_kind"], m["normalized"]) == key for m in mentions.extract(found["text"])):
        return found["text"]
    start = found.get("label_start")
    return text[start:found["end"]] if isinstance(start, int) else found["text"]


def _questions(store: Any, actor: Actor, audience: LiveSourceAudience) -> list[str]:
    """Up to ``MAX_QUESTIONS`` identifiers (as asked, :func:`_asked`) the captured text
    names on a page, one per (kind, normalized) value, in a deterministic order."""
    seen: dict[tuple[str, str], str] = {}
    for item in store.index(actor, audience, limit=100_000)["items"]:
        view = store.resolve(item["l1_handle"], actor, audience)
        text = None if view is None else view["row"].get("content_text")
        if not isinstance(text, str):
            continue
        for field in mentions.file_text_fields(view["l1_handle"], "row.content_text", text,
                                               paged=mentions.is_paged_file(view["row"])):
            for found in mentions.extract(field.text):
                key = (found["identifier_kind"], found["normalized"])
                if found["normalized"] and "#page=" in field.citation and key not in seen:
                    seen[key] = _asked(field.text, found)
    return [seen[key] for key in sorted(seen)][:MAX_QUESTIONS]


def _answered(answer: Mapping[str, Any]) -> tuple[bool, bool]:
    """(a captured file came back with a page-cited mention, every served file is
    labelled as of capture)."""
    labelled = {g["subject"] for g in answer.get("gaps", ())
                if g.get("reason") == "as_of_capture"}
    paged = [d for d in answer.get("documents", ()) if d.get("kind") == "sharepoint_file"
             and any("#page=" in m["span"]["origin"]["citation"]
                     for m in d.get("mentions", ()))]
    return bool(paged), all(d["citation"] in labelled or "sharepoint" in labelled
                            for d in answer.get("documents", ()))


def _unanswered(answer: Mapping[str, Any], refused: Mapping[str, str]) -> str | None:
    """Why a question has no page-cited answer (``UNANSWERED``), or None when it has."""
    if _answered(answer)[0]:
        return None
    if (answer.get("completeness") or {}).get("reason") == "scan_cap" \
            or any(g.get("reason") == "scan_cap" for g in answer.get("gaps", ())):
        return "scan_cap"
    if any(d.get("kind") == "sharepoint_file" for d in answer.get("documents", ())):
        return "unpaged_mention"
    return "withheld" if refused else "no_document"


def _citations(evidence: Mapping[str, Any]) -> list[dict[str, Any]]:
    out = [c for entry in evidence.get("class_evidence") or () for c in entry["citations"]]
    for field in (evidence.get("fields") or {}).values():
        out += field.get("citations") or []
        out += [c for entry in field.get("entries") or () for c in entry["citations"]]
        out += [c for reading in field.get("alternates") or () for c in reading["citations"]]
    out += [cell["citation"] for item in evidence.get("line_items") or ()
            for cell in item["cells"].values()]
    out += [ref["citation"] for ref in evidence.get("references") or ()]
    return [document_fields.full_citation(evidence, c) for c in out]


def document_class_counts(projection: Path, store: Any, actor: Actor,
                          audience: LiveSourceAudience, object_type: str) -> dict[str, Any]:
    """Counts of the projected objects of one document class and of their citations
    that resolve against the captured bytes (and pinned OCR text)."""
    conn = sqlite3.connect(f"file:{projection}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT v.value FROM objects o JOIN object_values v ON v.object_id = o.object_id "
            "WHERE o.object_type = ? AND v.property = 'evidence' AND v.superseded_at IS NULL",
            (object_type,)).fetchall()
        links = dict(conn.execute(
            "SELECT l.link_type, COUNT(*) FROM links l JOIN objects o ON o.object_id = "
            "l.from_object WHERE o.object_type = ? GROUP BY l.link_type",
            (object_type,)).fetchall())
    finally:
        conn.close()
    gaps: dict[str, int] = {}
    fields: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, int] = {}   # one document's {field: outcome}, canonical JSON
    handwritten: list[dict[str, Any]] = []
    counts = {"documents": 0, "documents_ocr": 0, "fields_stated": 0, "keyed_entries": 0,
              "citations": 0, "citations_resolved": 0}
    for (raw,) in rows:
        evidence = json.loads(raw)
        counts["documents"] += 1
        counts["documents_ocr"] += evidence.get("text_source") == "ocr"
        for name, field in evidence["fields"].items():
            counts["fields_stated"] += field.get("value") is not None
            counts["keyed_entries"] += sum(1 for e in field.get("entries") or ()
                                           if e.get("value") is not None)
            _tally(fields.setdefault(name, {}), field)
        key = json.dumps({name: _outcome(field) for name, field in evidence["fields"].items()},
                         sort_keys=True)
        outcomes[key] = outcomes.get(key, 0) + 1
        # Where a document's handwriting OCR could not key is: positions only, no text.
        unkeyed = {name: [{k: c[k] for k in ("page", "start", "end")}
                          for c in field["citations"]]
                   for name, field in sorted(evidence["fields"].items())
                   if _outcome(field) == document_fields.HANDWRITTEN_UNKEYED}
        if unkeyed:
            handwritten.append({"fields": unkeyed})
        for gap in evidence["gaps"]:
            gaps[gap["reason"]] = gaps.get(gap["reason"], 0) + 1
        content = store.content(evidence["l1_handle"], actor, audience)
        text = (store.blob(evidence["text_sha256"]).decode("utf-8")
                if evidence.get("text_sha256") else None)
        for citation in _citations(evidence):
            counts["citations"] += 1
            counts["citations_resolved"] += content is not None and \
                document_fields.resolve_citation(content, citation,
                                                 mime_type=evidence["mime_type"],
                                                 text=text) == citation["text"]
    return {**counts, "gaps": dict(sorted(gaps.items())), "links": dict(sorted(links.items())),
            "fields": {name: dict(sorted(tally.items())) for name, tally in sorted(fields.items())},
            "document_outcomes": [{"documents": n, "fields": json.loads(key)}
                                  for key, n in sorted(outcomes.items())],
            "handwritten_unkeyed": sorted(handwritten, key=lambda d: json.dumps(
                d, sort_keys=True))}


WITHHELD_PRINCIPAL = "staff:sharepoint-host-withheld"


def portal_metadata_counts(projection: Path, store: Any, actor: Actor,
                           audience: LiveSourceAudience) -> dict[str, Any]:
    """What the projection states the Portal filled on captured files (module docstring),
    each version re-read through the capture's gate for ``actor`` and for a withheld
    reader."""
    conn = sqlite3.connect(f"file:{projection}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT v.value, v.l1_handle FROM objects o JOIN object_values v ON v.object_id = "
            "o.object_id WHERE o.object_type = 'SharePointFile' AND v.property = "
            "'portal_metadata' AND v.value IS NOT NULL AND v.superseded_at IS NULL").fetchall()
    finally:
        conn.close()
    withheld = Actor(audience.tenant, frozenset({WITHHELD_PRINCIPAL}), "restricted")
    out: dict[str, Any] = {"documents_read": len(rows), "documents_with_portal_metadata": 0,
                           "served_to_reader": 0, "visible_to_withheld": 0}
    stated: dict[str, int] = {}
    gaps: dict[str, int] = {}
    for raw, handle in rows:
        value = json.loads(raw)
        for name in value["columns"]:
            stated[name] = stated.get(name, 0) + 1
        for reason in value["gaps"].values():
            gaps[reason] = gaps.get(reason, 0) + 1
        if not value["columns"]:
            continue
        out["documents_with_portal_metadata"] += 1
        served = store.resolve(handle, actor, audience, text=False)
        out["served_to_reader"] += served is not None and \
            served["row"].get("portal_metadata") == value
        out["visible_to_withheld"] += store.resolve(handle, withheld, audience,
                                                    text=False) is not None
    return {**out, "columns_stated": dict(sorted(stated.items())),
            "gap_reasons": dict(sorted(gaps.items()))}


def document_stages(store: Any, actor: Actor, audience: LiveSourceAudience, *,
                    limit: int | None = None) -> dict[str, Any]:
    """``document_fields.stages`` of the current captured files ``actor`` may see, listed
    by the store's reader (``index``, at most ``limit``, default ``cs.INDEX_LIMIT``:
    ``truncated`` when it cut the list) and each re-gated by ``resolve``, read as the
    projection reads them."""
    index = store.index(actor, audience, limit=cs.INDEX_LIMIT if limit is None else limit)
    rows = []
    for item in index["items"]:
        if store.resolve(item["l1_handle"], actor, audience, text=False) is None:
            continue
        row = store.current(item["drive_id"], item["item_id"])
        if row is not None and row["l1_handle"] == item["l1_handle"]:
            rows.append({**json.loads(row["record"]), **{key: row[key] for key in (
                "content_status", "content_sha256", "text_status", "text_sha256")}})
    return {**document_fields.stages(rows, store.blobs), "truncated": index["truncated"]}


def _outcome(field: Mapping[str, Any]) -> str:
    """One document's read of a field: ``stated`` or its gap reason; a keyed field is
    stated when one entry is; a label left unread where only handwriting OCR could not
    key is written is ``document_fields.HANDWRITTEN_UNKEYED`` (a reader limit told apart)."""
    if field.get("unread") == document_fields.HANDWRITTEN_UNKEYED:
        return document_fields.HANDWRITTEN_UNKEYED
    entries = field.get("entries")
    if entries is None:
        return "stated" if field.get("value") is not None else str(field.get("gap"))
    if any(entry.get("value") is not None for entry in entries):
        return "stated"
    return str(field.get("gap") or next((e.get("gap") for e in entries), None))


def _tally(tally: dict[str, Any], field: Mapping[str, Any]) -> None:
    """Count one document's read of a field (:func:`_outcome`); a keyed field's entries
    are counted the same way (``entries``), a seen label left unread by why
    (``unread_by``: ``ontology.document_fields.UNREAD_REASONS``)."""
    if field.get("entries") is not None:
        each = tally.setdefault("entries", {})
        for entry in field["entries"]:
            key = "stated" if entry.get("value") is not None else str(entry.get("gap"))
            each[key] = each.get(key, 0) + 1
    key = _outcome(field)
    tally[key] = tally.get(key, 0) + 1
    if field.get("unread"):   # why a seen label was left unread
        by = tally.setdefault("unread_by", {})
        by[field["unread"]] = by.get(field["unread"], 0) + 1


def run(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None,
        sender: Any = None, sleep: Callable[[float], None] | None = None,
        evaluate: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]] | None = None
        ) -> dict[str, Any]:
    from ontology import schema as schema_loader
    from ontology.project import SharePointCaptures, build_store
    from . import build_steps
    from .case_runtime import CaseRuntime, build_live_readers
    from .case_tools_host import CaptureCaseHostTools

    state = Path(args.state_dir).expanduser()
    capture = cs.run_cli(argparse.Namespace(
        state_dir=str(state), scope=args.scope, live_audience=args.live_audience,
        principal=args.principal, withheld_principal=WITHHELD_PRINCIPAL,
        folder_only_principal="staff:sharepoint-host-folder-only", runs=1,
        max_store_bytes=None, budget_seconds=getattr(args, "capture_budget_seconds", None)),
        environ=environ, sender=sender, sleep=sleep)
    audience = LiveSourceAudience.from_file(args.live_audience)
    actor = Actor(audience.tenant, frozenset({args.principal}), "restricted")
    store = cs.SharePointCaptureStore.open_read_only(state)
    try:
        questions = _questions(store, actor, audience)
        roots = [(r["drive_id"], r["root_path"]) for r in store.db.execute(
            "SELECT drive_id, root_path FROM roots ORDER BY drive_id, root_path")]
    finally:
        store.close()
    drives = sorted({drive for drive, _ in roots})
    work = Path(tempfile.mkdtemp(prefix="plx-sharepoint-host-", dir=state))
    projection = work / "ontology.sqlite"
    with SharePointCaptures(state) as files:
        build_store(projection, schema_loader.load_schema(), {"sharepoint": files},
                    post_project=build_steps.post_project(state, audience.tenant))
    if getattr(args, "document_class", None) is not None:
        return _document_class_report(args, capture, projection, actor, audience, evaluate)
    mirror_audience = work / "mirror-audience.json"
    mirror_audience.write_text(json.dumps({"schema": AUDIENCE_SCHEMA,
                                           "tenant": audience.tenant, "audiences": {}}),
                               encoding="utf-8")

    host_sent, host_gets = [0], [0]

    def ask(reader: Actor, reader_audience: LiveSourceAudience, name: str
            ) -> list[tuple[dict[str, Any], dict[str, str]]]:
        live = build_live_readers(reader_audience, environ, sharepoint_drives=drives,
                                  graph_sender=sender)
        runtime = CaseRuntime(work / name, client=None, audience=mirror_audience,
                              tenant=audience.tenant, live=live)
        host = CaptureCaseHostTools(runtime, reader, store_path=projection,
                                    sharepoint_capture_dir=state)
        try:
            return [(host.call("plx.documents", {"query": question, "sources": ["sharepoint"],
                                                 "limit": 50}, reader), host.refusals())
                    for question in questions]
        finally:
            host_sent[0] += getattr(live.graph_transport, "requests_sent", 0) or 0
            host_gets[0] += getattr(live.graph_transport, "gets_sent", 0) or 0
            host.close()
            runtime.close()

    asked = ask(actor, audience, "reader")
    answers = [answer for answer, _refused in asked]
    outsider = Actor(audience.tenant, frozenset({"staff:sharepoint-host-outsider"}),
                     "restricted")
    folder_principal = "staff:sharepoint-host-folder-only"
    folder_actor = Actor(audience.tenant, frozenset({folder_principal}), "restricted")
    hidden = [answer for answer, _refused in ask(outsider, audience, "outsider") + ask(
        folder_actor, cs.folder_only_audience(audience, folder_principal, roots), "folder")]
    checks = [_answered(answer) for answer in answers]
    reasons = [_unanswered(answer, refused) for answer, refused in asked]
    report = {
        "check": "case.sharepoint_documents",
        "evidence_label": capture["evidence_label"],
        "read_only_guard": capture["read_only_guard"],
        "capture": {key: capture["summary"][key] for key in (
            "roots", "roots_complete", "files_current", "contents_captured",
            "content_exceeds_cap", "content_unavailable", "texts_extracted", "texts_ocr",
            "texts_ocr_unavailable")},
        "questions": len(questions),
        "errors": sum(1 for answer in answers + hidden if "error" in answer),
        "answered_with_page_citation": sum(1 for paged, _ in checks if paged),
        "labelled_as_of_capture": sum(1 for _, labelled in checks if labelled),
        "unanswered": {reason: reasons.count(reason) for reason in UNANSWERED},
        "leak_check": {"denied_documents": sum(len(a.get("documents", ())) for a in hidden)},
        "capture_requests_sent": capture["requests_sent"],
        "host_requests_sent": host_sent[0],
        "host_gets_sent": host_gets[0],
        "requests_sent": capture["requests_sent"] + host_sent[0],
    }
    report["ok"] = bool(
        questions and not report["errors"]
        and report["answered_with_page_citation"] == len(questions)
        and report["labelled_as_of_capture"] == len(questions)
        and not report["leak_check"]["denied_documents"])
    return report


def _document_class_report(args: argparse.Namespace, capture: Mapping[str, Any],
                           projection: Path, actor: Actor, audience: LiveSourceAudience,
                           evaluate: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]]
                           | None) -> dict[str, Any]:
    profile = next((p for p in document_fields.profiles()["profiles"]
                    if p["id"] == args.document_class), None)
    if profile is None:
        return {"check": DOCUMENT_CHECK, "ok": False, "error": "document_class_unknown"}
    store = cs.SharePointCaptureStore.open_read_only(Path(args.state_dir).expanduser())
    try:
        found = document_class_counts(projection, store, actor, audience,
                                      profile["object_type"])
        report = {"check": DOCUMENT_CHECK, "evidence_label": capture["evidence_label"],
                  "read_only_guard": capture["read_only_guard"],
                  "document_class": args.document_class, "object_type": profile["object_type"],
                  "capture": {key: capture["summary"][key] for key in (
                      "files_current", "contents_captured", "texts_extracted", "texts_ocr",
                      "texts_ocr_unavailable")},
                  "capture_complete": capture["summary"]["roots"] > 0 and
                  capture["summary"]["roots_complete"] == capture["summary"]["roots"],
                  "requests_sent": capture["requests_sent"], **found,
                  "portal_metadata": portal_metadata_counts(projection, store, actor,
                                                            audience),
                  "stages": document_stages(store, actor, audience)}
        if evaluate is not None:
            report.update(evaluate(store, report))
    finally:
        store.close()
    report["ok"] = not report.get("blocked") and not report["stages"]["truncated"] and \
        found["documents"] >= getattr(args, "min_documents", 1) and \
        found["citations"] == found["citations_resolved"]
    return report


def exit_code(report: Mapping[str, Any]) -> int:
    """0 for an ``ok`` report or one ``blocked`` by a typed cause (module docstring)."""
    return 0 if report["ok"] or report.get("blocked") else 1


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None,
         evaluate: Callable[[Any, Mapping[str, Any]], Mapping[str, Any]] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m operating.sharepoint_host_check",
                                     description=__doc__.splitlines()[0])
    parser.add_argument("--state-dir", required=True,
                        help="Workbench state directory (outside every git work tree)")
    parser.add_argument("--scope", default=str(cs.DEFAULT_SCOPE),
                        help="plx-sharepoint-capture-scope/v1 file")
    parser.add_argument("--live-audience", required=True,
                        help="plx-live-source-audience/v1 file granting the reader")
    parser.add_argument("--principal", default=cs.CAPTURE_PRINCIPAL)
    parser.add_argument("--document-class",
                        help="measure this class of ontology/schema/document_fields.json "
                             "instead of asking questions")
    parser.add_argument("--min-documents", type=int, default=1)
    parser.add_argument("--capture-budget-seconds", type=float,
                        default=CAPTURE_BUDGET_SECONDS,
                        help="stop the capture walk after this long (roots left incomplete)")
    args = parser.parse_args(argv)
    report = run(args, environ=environ, evaluate=evaluate)
    print(json.dumps(report, indent=1, sort_keys=True))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
