"""Captured documents of ``CaptureCaseHostTools``: early and newer mail, use-case
documents, named and mentioning files.

Split from ``operating/case_tools_host.py`` (``_captured_documents`` and
``_identifier_lookup`` stay there); moved unchanged.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from ontology import mentions as mn
from operating import ontology_tools as ot
from operating.ontology_tools_identity import _gap
from .case_tools_host_rules import USE_CASE_NOTE, _DATE, _mail_store, _output_properties
from .case_tools_host_index import _name_terms


class _DocumentsMixin:
    """Methods of ``CaptureCaseHostTools`` (``case_tools_host_documents``); mixed in there, never used alone."""

    def _early_mail(self, arguments: Any, actor: Any) -> Any:
        """This search's mail top-up scan started now under the call's pinned policy, on
        its own read-only connection to the capture (``mail_topup.EarlyScan``), or None
        (not a query search, no capture, any failure: the top-up then scans itself)."""
        from operating.mail_topup import EarlyScan
        from integrations.capture_mail import MailCaptureStore, store_exists
        directory = Path(self.mail_capture_dir)
        try:
            if not store_exists(directory) or self.tools.store.format_problem() is not None:
                return None
            return EarlyScan.start(
                self.tools, arguments, actor, self.policy_for(actor),
                lambda: contextlib.closing(MailCaptureStore.open_read_only(directory)))
        except Exception:  # noqa: BLE001 - never fails the search; the top-up scans itself
            return None

    def _newer_mail(self, arguments: Mapping[str, Any], actor: Any,
                    result: dict[str, Any], early: Any = None) -> dict[str, Any]:
        """``result`` with the mail captured since the served generation was built that
        this reader may see (``operating.mail_topup``: the call's read policy grants
        and this host's per-item mail gate, as for projected mail), each marked
        ``captured_after_refresh``."""
        from operating.mail_topup import top_up
        from integrations.capture_mail import store_exists
        if not store_exists(Path(self.mail_capture_dir)):
            return result
        return top_up(self.tools, arguments, actor, result,
                      lambda: _mail_store(self.runtime, Path(self.mail_capture_dir)),
                      early=early)

    def _use_case_documents(self, name: str, arguments: Mapping[str, Any], actor: Any,
                            result: dict[str, Any]) -> dict[str, Any]:
        """``result`` with the source documents of each use case the query names
        (``operating.use_case_catalog``) for this reader, listed in ``use_case`` alone:
        they are never search hits, so the search's page, counts and ``no_match`` gap
        stay as the search produced them. Only the source kinds the call searches
        (``sources``: ``sharepoint``, ``original``) are resolved; those held in a source
        the call does not search are counted per source in a
        ``use_case_documents_not_searched`` gap (never named, never resolved), so a
        narrowed search is never told the catalog lists none. Each goes through
        its source's gate (a SharePoint item through :meth:`captured`, a case original
        through the runtime's captured-original gate), and those not served to this
        reader are counted, never named. A document the use case needs that no source
        holds is a named ``use_case_document_not_held`` gap, so a question about the use
        case is never answered from another use case's map. Only a first page (no
        cursor) is resolved. A query saying "use case" that names none in the catalog
        is an ``unknown_use_case`` gap; an unreadable catalog a
        ``use_case_catalog_unreadable`` gap."""
        from operating import use_case_catalog as ucc
        query = arguments.get("query")
        if not isinstance(query, str) or arguments.get("cursor"):
            return result
        kinds = {"sharepoint": "sharepoint_file", "original": "original"}
        wanted = {kinds[source] for source in (arguments.get("sources") or kinds)
                  if source in kinds}
        if not wanted:
            return result   # a mail-only search: no use-case documents
        gaps: list[dict[str, Any]] = []
        try:
            catalog = self.use_cases if self.use_cases is not None else ucc.load()
            cases, unknown = catalog.named(query)
        except ucc.CatalogError:
            cases, unknown = [], False
            if "use case" in " ".join(ucc.words(query)):
                gaps.append(_gap("unknown", "use_case_catalog_unreadable", "use_case_catalog",
                                 "the Workbench's use-case catalog could not be read; this "
                                 "answer matched documents by their words only"))
        if unknown:
            gaps.append(_gap("unknown", "unknown_use_case",
                             f"query:{' '.join(query.split())}"[:700],
                             "the query names a use case the Workbench's catalog does not "
                             "list; documents were matched by their words only, and none of "
                             "them is known to belong to that use case"))
        if not cases and not gaps:
            return result
        blocks = []
        for case in cases:
            listed, withheld, unsearched = self._use_case_served(case, actor, wanted)
            block = {"id": case.id, "title": case.title,
                     "documents": [{"citation": d["citation"], "kind": d["kind"],
                                    "role": d["role"], "title": d["title"]}
                                   for d in listed],
                     "withheld": withheld,
                     "note": USE_CASE_NOTE}
            blocks.append(block)
            if withheld:
                gaps.append(_gap("inaccessible", "use_case_documents_withheld",
                                 f"use_case:{case.id}",
                                 f"{withheld} source document(s) of this use case are not "
                                 "served to you (not held by the Workbench, or not openable "
                                 "with your permissions); they are not named"))
            if unsearched:
                held_in = ", ".join(f"{count} in {source}"
                                    for source, count in sorted(unsearched.items()))
                gaps.append(_gap("unknown", "use_case_documents_not_searched",
                                 f"use_case:{case.id}",
                                 f"{sum(unsearched.values())} source document(s) of this use "
                                 f"case are held in a source this search did not include "
                                 f"({held_in}); search again with those sources (or with no "
                                 "sources) to list the ones you may open"))
            for missing in case.gaps:
                gaps.append(_gap("not_ingested", "use_case_document_not_held",
                                 f"use_case:{case.id}",
                                 f"{missing['document']}: {missing['detail']}. This use "
                                 "case's own document is not held, and another use case's "
                                 "map does not stand in for it; records and files held "
                                 "elsewhere (SharePoint files, mail, Portal or ERP records, "
                                 "task boards) may still answer the question"))
            if not case.documents and not case.gaps:
                gaps.append(_gap("not_ingested", "use_case_document_not_held",
                                 f"use_case:{case.id}",
                                 "the Workbench's catalog lists no source document for this "
                                 "use case"))
        out = dict(result)
        if blocks:
            out["use_case"] = blocks[0] if len(blocks) == 1 else blocks[0] | {
                "also": [{"id": b["id"], "title": b["title"], "documents": b["documents"],
                          "withheld": b["withheld"]} for b in blocks[1:]]}
        output = self.tools.contracts["tools"][name]["output"]
        shape = _output_properties(self.tools.contracts, output)
        cap = shape.get("gaps", {}).get("maxItems", 20)
        # The use case's documents are never search hits: they are listed in
        # ``use_case`` alone, and the search's own page, counts and no_match gap stay
        # exactly as the search produced them.
        # Too large with the block, the search's own hits are trimmed to fit
        # (:meth:`_trimmed`), never the block dropped.
        out["gaps"] = (gaps + list(out.get("gaps") or []))[:cap]
        return out

    def _use_case_served(self, case: Any, actor: Any, kinds: set[str]
                         ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """(the documents of ``case`` of ``kinds`` served to this reader now, in catalog
        order, each {citation, kind, role, title}; how many of them are not; how many
        of its documents are of a kind not in ``kinds``, by ``sources`` name: never
        resolved, so never gated, listed or withheld here)."""
        from operating.captured_originals import SCHEME as ORIGINAL
        from operating import use_case_catalog as ucc
        listed: list[dict[str, Any]] = []
        withheld = 0
        unsearched: dict[str, set[Any]] = {}   # each document once, however often named
        store = self._sharepoint()
        originals = getattr(self.runtime, "captured_originals", None)
        # Originals listed beside a textless SharePoint copy, within the list's bound.
        room = ucc.MAX_DOCUMENTS - len(case.documents)
        for doc in case.documents:
            if ("sharepoint_file" if "sharepoint" in doc else "original") not in kinds:
                source = "sharepoint" if "sharepoint" in doc else "original"
                unsearched.setdefault(source, set()).add(
                    doc.get("original", doc.get("sharepoint")))
                continue   # a source this call does not search: counted, not resolved
            if any(d.get("_key") == doc.get("original", doc.get("sharepoint"))
                   or "original" in doc and d["citation"] == ORIGINAL + doc["original"]
                   for d in listed):
                continue   # named twice (or listed with its textless copy): listed once
            if "sharepoint" in doc:
                try:
                    row = None if store is None else store.current(*doc["sharepoint"])
                except (sqlite3.Error, OSError, ValueError):
                    row = None
                view = None if row is None or row.get("removed_reason") is not None \
                    else self.captured(actor, row["l1_handle"])
                if view is None:
                    withheld += 1
                    continue
                handle, kind = view["l1_handle"], "sharepoint_file"
                name = view["row"].get("name")
                # A copy held without text: the case original holding its text (same
                # bytes, same item gate) is listed after it, while the list has room.
                original = None if "original" not in kinds or room <= 0 \
                    else self._text_original(actor, view)
                if original is not None and all(d["citation"] != original for d in listed):
                    room -= 1
                    entry = originals.entries().get(original[len(ORIGINAL):])
                    listed.append(self._use_case_document(doc, handle, kind, name))
                    handle, kind = original, "original"
                    name = None if entry is None else entry.receipt.get("name")
            else:
                handle, kind = ORIGINAL + doc["original"], "original"
                try:
                    opened = originals is not None \
                        and originals.refusal_kind(handle, actor) is None
                    entry = originals.entries().get(doc["original"]) if opened else None
                except Exception:  # noqa: BLE001 - a failing gate serves nothing
                    entry = None
                if entry is None:
                    withheld += 1
                    continue
                name = entry.receipt.get("name")
            listed.append(self._use_case_document(doc, handle, kind, name))
        return listed, withheld, {source: len(keys) for source, keys in unsearched.items()}

    @staticmethod
    def _use_case_document(doc: Mapping[str, Any], handle: str, kind: str, name: Any
                           ) -> dict[str, Any]:
        return {"_key": doc.get("original", doc.get("sharepoint")),
                "citation": handle, "kind": kind, "role": doc["role"],
                "title": None if not isinstance(name, str) or not name
                else {"untrusted_text": name[:400], "origin": {"citation": handle}}}

    def _captured_search(self, arguments: Mapping[str, Any], actor: Any,
                         store: Any = None) -> tuple | None:
        """(query, identifiers, name terms, store, audience, gated reader, the reader's
        identifier lookup) of a ``plx.documents`` call :meth:`_captured_documents`
        searches the SharePoint capture for, or None when it does not. A documents call
        whose mail top-up scans early reads it while that scan runs and hands it to
        :meth:`_captured_documents` (one lookup per call, never one per step)."""
        query = arguments.get("query")
        if not isinstance(query, str) or arguments.get("cursor") or arguments.get("since") \
                or arguments.get("until") or arguments.get("object_id") is not None \
                or arguments.get("participant") is not None \
                or "sharepoint" not in (arguments.get("sources") or ("sharepoint",)):
            # a participant's search is of their mail alone: no file is anyone's mail
            return None
        wanted = {(m["identifier_kind"], m["normalized"]) for m in mn.extract(query)
                  if m["normalized"]}
        terms = _name_terms(query)
        store, audience = store if store is not None else self._sharepoint(), self._audience()
        if not (wanted or terms) or store is None or audience is None:
            return None
        try:
            gated = self.runtime.employee_gate(actor)
        except Exception:  # noqa: BLE001 - a refused employee gate reads nothing
            return None
        return (query, wanted, terms, store, audience, gated,
                self._identifier_lookup(store, gated, audience))

    def _named(self, handle: str, name: Any, path: Any, modified: Any) -> dict[str, Any]:
        """One captured file as a ``plx.documents`` item found by its name or folder path
        (no mention: nothing in its text was matched), the path quoted as the snippet."""
        limit = self.tools.contracts["limits"]["max_untrusted_text_chars"]
        title = (ot._envelope(name, handle, "row.name", limit)
                 if isinstance(name, str) and name else None)
        return {"citation": handle, "kind": "sharepoint_file", "title": title,
                "date": modified if isinstance(modified, str) and _DATE.match(modified)
                else None,
                "snippet": (ot._envelope(path, handle, "row.path", limit)
                            if isinstance(path, str) and path else title),
                "mentions": []}

    def _mentioning(self, view: Mapping[str, Any], wanted: set[tuple[str, str]],
                    pages: set[int | None] | None = None) -> dict[str, Any] | None:
        """One captured file as a ``plx.documents`` item when its text names one of
        ``wanted`` (the one identifier grammar, page by page for a PDF), read on
        ``pages`` alone when given (``None``: an unpaged file's one field)."""
        handle, row = view["l1_handle"], view["row"]
        paged = mn.is_paged_file(row)
        fields = mn.file_text_fields(handle, "row.content_text", row["content_text"],
                                     paged=paged)
        limit = self.tools.contracts["limits"]["max_untrusted_text_chars"]
        mentions = []
        for field in fields:
            if pages is not None \
                    and (mn.cited_page(field.citation)[1] if paged else None) not in pages:
                continue
            for found in mn.extract(field.text):
                if (found["identifier_kind"], found["normalized"]) not in wanted:
                    continue
                mentions.append({
                    "identifier_kind": found["identifier_kind"],
                    "normalized": str(found["normalized"])[:200],
                    "span": ot._envelope(field.text[found["start"]:found["end"]],
                                         field.citation, field.field_path, limit),
                    "tier": "lead", "external_assertion": True})
        if not mentions:
            return None
        name, date = row.get("name"), row.get("last_modified")
        return {"citation": handle, "kind": "sharepoint_file",
                "title": (ot._envelope(name, handle, "row.name", limit)
                          if isinstance(name, str) and name else None),
                "date": date if isinstance(date, str) and _DATE.match(date) else None,
                "snippet": mentions[0]["span"], "mentions": mentions}
