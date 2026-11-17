"""Labels and trimming of ``CaptureCaseHostTools``: labelled objects, size bounds and
the captured read.

Split from ``operating/case_tools_host.py``; moved unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from ontology.json_contract import validate
from operating import ontology_tools as ot
from operating.capture_age import capture_decides
from operating.ontology_tools_identity import _error, _gap
from operating.ontology_tools_evidence import kept_mentions, skipped_gap
from operating.playbook_originals import extraction_skipped
from .case_tools_host_rules import (
    SEARCH_TOOLS, USE_CASE_GAPS, _ROW_FIELD, _output_properties,
)
from .case_tools_host_citations import _ACTIVE_CALL, _SEARCH_HITS


class _LabelsMixin:
    """Methods of ``CaptureCaseHostTools`` (``case_tools_host_labels``); mixed in there, never used alone."""

    # ---- SharePoint: labels -----------------------------------------------------

    def _labels(self, handle: str) -> list[dict[str, Any]]:
        """The gaps that label one served captured version (module docstring)."""
        from integrations.capture_sharepoint import (CONTENT_SKIPPED, EXCEEDS_CAP, OCR_FAILED,
                                                     OCR_UNAVAILABLE, TEXT_OCR)
        from integrations.captured_text import TEXT_EXCEEDS_BOUND, text_status_kind
        info = self._served.get(handle)
        if info is None:
            return []
        failure = info["failure"] or ""
        if info["basis"] == "live":
            basis = "its permissions were re-read live now"
        else:
            basis = "its permissions are as recorded at that capture" + (
                " (a search result is not re-read live; plx.read of it re-reads them)"
                if failure == "search"
                else " (this call's live re-read bound is reached)" if failure == "budget"
                else " (the live re-read failed now)" if failure.startswith("failed:")
                else "")
        newer = "; the live re-read found a newer version at the source" \
            if info["changed"] else ""
        out = [_gap("stale" if info["changed"] else "unknown", "as_of_capture", handle,
                    f"as of capture {info['captured_at']}: served from the Workbench's "
                    f"SharePoint capture, not read live now; {basis}{newer}")]
        if failure.startswith("failed:"):
            out.append(_gap("unknown", "live_reread_failed", handle,
                            f"the live SharePoint re-read failed now "
                            f"({failure.partition(':')[2][:80]}); the captured file is "
                            "served as captured, within the capture age bound, not "
                            "confirmed against the source"))
        content = str(info["content_status"] or "")
        if content == EXCEEDS_CAP:
            size = info["size"] if isinstance(info["size"], int) else "unknown"
            out.append(_gap("not_ingested", "content_exceeds_cap", handle,
                            f"the file ({size} bytes) is over the capture's content cap: "
                            "its metadata, permissions and version are captured, its "
                            "content was skipped, so nothing in it is searched or shown"))
        elif content == CONTENT_SKIPPED:
            out.append(_gap("not_ingested", "content_not_read", handle,
                            "a metadata-only capture recorded this file's metadata, "
                            "permissions and version but never read its bytes; nothing in "
                            "it is searched or shown"))
        elif content.startswith("unavailable:"):
            out.append(_gap("not_ingested", "content_unavailable", handle,
                            "the capture could not read this file's bytes (retried by "
                            "later captures); nothing in it is searched or shown"))
        status = text_status_kind(str(info["text_status"]))
        if status == TEXT_OCR:
            out.append(_gap("unknown", "ocr_text_unconfirmed", handle,
                            "machine-read OCR text: what the scan appears to say, for a "
                            "person to confirm, never a verified value"))
        elif status in (OCR_UNAVAILABLE, OCR_FAILED):
            out.append(_gap("not_ingested", status, handle,
                            "scanned pages of this PDF were not OCR-read on the capturing "
                            "host; identifiers on them are neither searched nor shown"))
        elif status == TEXT_EXCEEDS_BOUND:
            out.append(_gap("not_ingested", status, handle,
                            "the file's bytes are captured but over the text extractor's "
                            "bound, so its own text was never read whole and is neither "
                            "searched nor shown (a zip, .msg or .eml is read member by "
                            "member)"))
        return out

    def _label(self, name: str, result: dict[str, Any], actor: Any) -> dict[str, Any]:
        """``result`` with an ``as_of_capture`` label for every captured SharePoint
        version it serves, within the tool's gap limit and its size bound (past either,
        one summary gap for them all); a result that cannot carry them is an error, never
        unlabelled. A plx.investigate page leaves ``LABEL_RESERVE_BYTES`` free for this."""
        served: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str) and node.startswith("sharepoint://"):
                served.add(node.split("#", 1)[0])

        walk(result)
        for handle in sorted(served - set(self._served)):
            self.captured(actor, handle)   # served by another path in this call
        handles = sorted(h for h in served if h in self._served)
        # Admitted to the computation (counts, totals, filters) but not listed.
        counted = sorted(set(self._served) - served)
        if not handles and not counted:
            # Nothing captured served: a use-case block alone is still held to the bound.
            return self._bounded(name, result) \
                if "use_case" in result or name in SEARCH_TOOLS else result
        admitted = sorted(self._served[h]["captured_at"] for h in (*handles, *counted))
        output = self.tools.contracts["tools"][name]["output"]
        shape = _output_properties(self.tools.contracts, output)
        out = dict(result)
        meta = out.get("meta")
        if isinstance(meta, dict):
            freshness = list(meta.get("freshness") or [])
            # A search serves captures of any age on their recorded permissions; past the
            # host's age bound the oldest one says so (no gap, not incomplete).
            old = self._call_bound.search and not capture_decides(
                admitted[0], self.clock(), self.max_capture_age)
            note = (f"host captures as of {admitted[0]}, permissions as of capture; "
                    "plx.read re-checks live")
            at = next((i for i, f in enumerate(freshness) if f.get("source") == "sharepoint"),
                      None)
            if at is None and len(freshness) < 8:
                entry = {"source": "sharepoint", "completeness": "unknown", "stale": False,
                         "captured_at": admitted[0]}
                if old:
                    entry["label"] = note
                freshness.append(entry)
                out["meta"] = dict(meta, freshness=freshness)
            elif at is not None and old and "label" not in freshness[at]:
                freshness[at] = dict(freshness[at], label=note)
                out["meta"] = dict(meta, freshness=freshness)
        if "gaps" not in shape:
            return self._bounded(name, out)
        cap = shape["gaps"].get("maxItems", 20)
        existing = list(out.get("gaps") or [])
        labels = [gap for handle in handles for gap in self._labels(handle)]
        if counted:
            times = sorted(self._served[h]["captured_at"] for h in counted)
            labels.append(_gap("unknown", "as_of_capture", "sharepoint",
                               f"{len(counted)} further SharePoint files from the "
                               f"Workbench's capture as of {times[0]} to {times[-1]} "
                               "entered this answer's counts, totals or filters without "
                               "being listed; they were not read live now"))
        failed = sum(1 for h in (*handles, *counted)
                     if str(self._served[h]["failure"] or "").startswith("failed:"))
        summary = _gap("unknown", "as_of_capture", "sharepoint",
                       f"{len(handles) + len(counted)} SharePoint files served or "
                       f"counted from the Workbench's capture as of {admitted[0]} to "
                       f"{admitted[-1]}, not read live now"
                       + (f"; the live re-read failed for {failed}" if failed else "")
                       + "; per-file labels beyond the gap limit or the size bound are "
                       "not listed")
        room = cap - len(existing)
        if len(labels) > room:
            labels = labels[:max(room - 1, 0)] + [summary]
            existing = existing[:cap - len(labels)]
        out["gaps"] = existing + labels
        if not self._fits(out):
            out["gaps"] = existing[:cap - 1] + [summary]
        return self._bounded(name, out)

    def _fits(self, result: Mapping[str, Any]) -> bool:
        """Whether ``result`` fits one response as it is sent: to a client, every text
        wrapped by the output boundary and the freshness meta's reserve counted
        (``mcp_server._envelope_bytes``; a fitted page of documents grew some 19 KB
        when wrapped and was refused whole as ``result_too_large``)."""
        from operating import mcp_server
        limit = min(self.tools.contracts["limits"]["max_result_bytes"],
                    mcp_server.MAX_RESULT_BYTES)
        return mcp_server._envelope_bytes(dict(result)) <= limit

    def _bounded(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        contracts = self.tools.contracts
        problems = validate(result, contracts["tools"][name]["output"], root=contracts, limit=5)
        if problems:
            return _error("contract_violation", ("output failed the contract: "
                                                 + "; ".join(problems))[:600])
        if not self._fits(result) and self._hits_key(result) is not None:
            result = self._trimmed(name, result, _SEARCH_HITS.get())
            problems = validate(result, contracts["tools"][name]["output"], root=contracts,
                                limit=5)
            if problems:
                return _error("contract_violation", ("trimmed output failed the contract: "
                                                     + "; ".join(problems))[:600])
        if not self._fits(result) and name == "plx.object":
            trimmed = self._trimmed_object(result)
            if trimmed is not None:
                problems = validate(trimmed, contracts["tools"][name]["output"],
                                    root=contracts, limit=5)
                if problems:
                    return _error("contract_violation", ("trimmed output failed the "
                                                         "contract: " + "; ".join(problems))[:600])
                result = trimmed
        if not self._fits(result):
            return _error("result_too_large", "the answer and its capture labels do not "
                          "fit one response", "narrow the question or lower limit")
        return result

    def _trimmed_object(self, result: Mapping[str, Any]) -> dict[str, Any] | None:
        out = json.loads(json.dumps(result))   # never edit the caller's answer
        texts = [value["value"] for value in out.get("values") or ()
                 if isinstance(value, dict) and isinstance(value.get("value"), dict)
                 and isinstance(value["value"].get("untrusted_text"), str)]
        if not texts:
            return None
        output = self.tools.contracts["tools"]["plx.object"]["output"]
        cap = _output_properties(self.tools.contracts, output).get(
            "gaps", {}).get("maxItems", 20)
        gaps = [g for g in out.get("gaps") or [] if g.get("reason") != "result_trimmed"]
        for limit in self.TRIM_OBJECT_TEXT_CHARS:
            cut = 0
            for envelope in sorted(texts, key=lambda e: -len(e["untrusted_text"])):
                if len(envelope["untrusted_text"]) > limit:
                    envelope["untrusted_text"] = envelope["untrusted_text"][:limit]
                    envelope["truncated"] = True
                    cut += 1
            if not cut:
                continue
            shortened = sum(1 for envelope in texts if envelope.get("truncated"))
            note = _gap("unknown", "result_trimmed", "plx.object", (
                f"this record was over the response bound and was trimmed to fit: "
                f"{shortened} long text value(s) cut to at most {limit} characters (marked "
                "truncated). plx.read a cut value's citation for its whole text")[:600])
            trial = dict(out, gaps=gaps[:cap - 1] + [note])
            if self._fits(trial):
                return trial
        return None

    @staticmethod
    def _hits_key(result: Mapping[str, Any]) -> str | None:
        for key in ("documents", "candidates"):
            if isinstance(result.get(key), list):
                return key
        return None

    def _trimmed(self, name: str, result: Mapping[str, Any], paged: int | None = None
                 ) -> dict[str, Any]:
        """``paged``: how many of the hits, from the first, the search's cursor pages
        (None: all); the rest (captured files a host appended) are on no other page."""
        out = json.loads(json.dumps(result))   # never edit the caller's answer
        key = self._hits_key(out)
        noun = "documents" if key == "documents" else "candidates"
        hits: list[dict[str, Any]] = out[key]
        total = len(hits)
        paged = total if paged is None else max(0, min(paged, total))
        cursor = (out.get("completeness") or {}).get("next_cursor")
        # plx.find keeps its status's own candidates: a found answer its one, an ambiguous
        # one two (never a lone apparent winner); a document list may empty.
        floor = 0 if key == "documents" else {"found": 1, "ambiguous": 2}.get(
            str(out.get("status")), 0)
        steps: list[str] = []
        output = self.tools.contracts["tools"][name]["output"]
        shape = _output_properties(self.tools.contracts, output)
        cap = shape.get("gaps", {}).get("maxItems", 20)

        def attempt(*extra: str) -> dict[str, Any] | None:
            gaps = [g for g in out.get("gaps") or [] if g.get("reason") != "result_trimmed"]
            note = _gap("unknown", "result_trimmed", name, (
                "this answer was over the response bound and was trimmed to fit: "
                + "; ".join([*steps, *extra]) + ". plx.read a listed document for its "
                "full text; its mention_summary counts its mentions and next_mention_offset, "
                "given as mention_offset, shows more")[:600])
            trial = dict(out, gaps=gaps[:cap - 1] + [note])
            return trial if self._fits(trial) else None

        def counts() -> tuple[int, int]:
            """(search hits, appended captured files) dropped so far."""
            searched = max(0, paged - len(hits))
            return searched, total - len(hits) - searched

        def dropped() -> str:
            searched, captured = counts()
            parts = []
            if searched:
                head = (f"the last {searched} of {paged}" if hits else f"all {paged}") \
                    + f" {noun} the search paged left out"
                if out["completeness"]["next_cursor"] is not None:
                    head += "; completeness.next_cursor continues from the first left out"
                elif cursor is None:
                    head += (f"; ask again with limit {max(len(hits), 1)} to page through "
                             "them")
                else:
                    head += "; no next page can be offered"
                parts.append(head)
            if captured:
                parts.append(f"{captured} more captured SharePoint file(s) matching this "
                             "query left out; they are on no page: narrow the question to "
                             "see them")
            return "; ".join(parts)

        def drop_one() -> None:
            hits.pop()
            searched, _captured = counts()
            # The page's cursor moves back over the search's own hits dropped here (a
            # resumable offset cursor), else none is offered and the note says how to
            # page; dropped captured files are on no page: the page is partial.
            out["completeness"] = dict(
                out.get("completeness") or {"total": None}, complete=False, truncated=True,
                reason="result_too_large", returned=len(hits),
                next_cursor=(ot._rewound_cursor(cursor, searched) if searched
                             else cursor))

        for keep in self.TRIM_MENTIONS:
            cut = 0
            for at, doc in enumerate(hits):
                if len(doc.get("mentions") or []) > keep:
                    hits[at], cut = kept_mentions(doc, keep), cut + 1
            if cut:
                steps.append(f"mentions kept to {keep} on {cut} document(s)")
                if (answer := attempt()) is not None:
                    return answer
        for chars in self.TRIM_TEXT_CHARS:
            cut = 0
            for doc in hits:
                for field in ("snippet", "title"):
                    text = doc.get(field)
                    if isinstance(text, dict) and len(text.get("untrusted_text", "")) > chars:
                        doc[field] = dict(text, untrusted_text=text["untrusted_text"][:chars],
                                          truncated=True)
                        cut += 1
            if cut:
                steps.append(f"{cut} snippet(s) or title(s) cut to {chars} characters")
                if (answer := attempt()) is not None:
                    return answer
        while len(hits) > floor:
            drop_one()
            if (answer := attempt(dropped())) is not None:
                return answer
        if total > len(hits):
            steps.append(dropped())
        block = out.get("use_case")
        entries = [] if not isinstance(block, dict) else [block, *(block.get("also") or [])]
        if any(d.get("title") for entry in entries for d in entry.get("documents") or []):
            for entry in entries:
                for doc in entry.get("documents") or []:
                    doc["title"] = None
            steps.append("the use cases' document titles left out (their citations and "
                         "counts stay)")
            if (answer := attempt()) is not None:
                return answer
        also = [entry for entry in entries[1:] if entry.get("documents")]
        if also:
            for entry in also:
                entry["documents"] = []
            steps.append("the second use case's documents left out (its id and withheld "
                         "count stay): ask about it by name to list them")
            if (answer := attempt()) is not None:
                return answer
        # Last, the gaps from the end, a use case's own gaps after every other (the
        # trimming note itself always stays); an answer that does not fit even so is
        # returned as it was, and fails as too large.
        gaps = [g for g in out.get("gaps") or [] if g.get("reason") != "result_trimmed"]
        gaps = ([g for g in gaps if g.get("reason") in USE_CASE_GAPS]
                + [g for g in gaps if g.get("reason") not in USE_CASE_GAPS])
        for keep in range(min(len(gaps), cap - 1) - 1, -1, -1):
            out["gaps"] = gaps[:keep]
            if (answer := attempt(f"{len(gaps) - keep} further gap(s) left out")) is not None:
                return answer
        return dict(result)

    # ---- SharePoint: reads and document search -----------------------------------

    def captured_read(self, handle: str, actor: Any) -> dict[str, Any] | None:
        """``plx.read`` of a captured SharePoint version (``CitationReader.read`` shape):
        the file, or the refusal (with its reason) of a captured version the gate
        refuses; None when the capture does not hold it for this reader. Inside a tool
        call it shares that call's re-read bound."""
        if not self._in_call:
            self._new_call(_ACTIVE_CALL.get())
        view = self.captured(actor, handle, text=True)
        if view is None:
            return self._refusal(str(handle)) or self._page_not_held(str(handle), actor)
        row = dict(view["row"])
        text = row.pop("content_text", None)
        fields = {f"row.{name}": value for name, value in row.items()
                  if _ROW_FIELD.match(str(name))}
        out: dict[str, Any] = {
            "status": "resolved", "reason": "ok", "lineage": "sharepoint",
            "digest": view["digest"], "fields": fields, "document": True,
            "values_citation": handle, "content_citation": handle,
            "content_field": "row.content_text", "gaps": self._labels(view["l1_handle"]),
            # Why a file read holds no text (``text_not_extracted``): a member's own
            # status, else the capture's (its content status when its bytes were not read).
            "text_status": str(row.get("text_status") or row.get("content_status")
                               or "text_not_read")}
        if isinstance(text, str):
            out["content"] = text
            skipped = extraction_skipped(row)
            if skipped is not None and "page" not in view:
                out["gaps"].append(skipped_gap(handle, skipped))
        if not (isinstance(text, str) and text.strip()) and "page" not in view:
            original = self._text_original(actor, view)
            if original is not None:
                out["gaps"].append(_gap(
                    "not_ingested", "text_in_case_original", original,
                    "the SharePoint capture holds no text of this version; the captured "
                    "case original cited here holds the same bytes (SHA-256) with their "
                    "extracted text: read it (and its members) for what this file says"))
        if view["superseded_at"] is not None:
            current = self._current_read(handle, actor)
            if current is not None and not (isinstance(text, str) and text.strip()):
                # The cited version's text is gone (pruned) or was never read: the
                # current version is returned under its own citation, as a live reader
                # returns it, never labelled with the cited one.
                current["gaps"].append(_gap(
                    "stale", "cited_version_not_retained", handle[:700],
                    "the capture holds no text of the cited version, superseded since; "
                    "the values shown are the item's current captured version, cited at "
                    "current_citation"))
                return {**current, "status": "changed_since",
                        "current_citation": current["values_citation"]}
            if current is not None:
                out.update(status="changed_since",
                           current_citation=current["values_citation"])
            out["gaps"].append(_gap("stale", "superseded_in_capture", handle[:700],
                                    "the capture no longer holds this version as current"
                                    + ("; read current_citation for its current version"
                                       if current is not None else "")))
        return out
